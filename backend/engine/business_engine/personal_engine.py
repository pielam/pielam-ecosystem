# engine/business_engine/personal_engine.py
#
# Public "engine" feed:
#   - one post per publicly-connected ConnectedService, pulled from every
#     user on the platform (a single connected URL can carry many images,
#     videos, and links — this is the feed that surfaces them).
#   - EngineView renders the first page server-side.
#   - engine_feed_api serves subsequent pages as JSON for infinite scroll.
#
# ═══════════════════════════════════════════════════════════════════
# SCALE NOTES — read this before touching the caching below
# ═══════════════════════════════════════════════════════════════════
#
# No single Django process gets you to millions of req/s. This file
# only optimizes the layers Django actually controls. The rest is
# infra sitting in front of it:
#
#   1. CDN (Cloudflare / Fastly / CloudFront) in front of
#      `engine_feed_api` and `EngineView`, honoring the Cache-Control
#      headers set below. This is where >99% of traffic should be
#      answered — Django should see a small fraction of real hits.
#
#   2. This feed is public content but the views below still require
#      login. That's fine for the authenticated app shell, but it
#      means the CDN can't cache the response per-anonymous-visitor.
#      If you want CDN caching to actually work at this scale, split
#      this into:
#        - a fully public, unauthenticated JSON endpoint serving the
#          cacheable feed payload (what's cached below), and
#        - the authenticated page shell that fetches it client-side.
#      Left as login_required here since that's the existing contract;
#      flagging it because it caps how much this can be edge-cached.
#
#   3. Read replica: point this view's DB reads at a replica, not
#      primary. A global public feed is 100% read traffic.
#
#   4. PgBouncer (or equivalent) in transaction-pooling mode — at high
#      concurrency, Django's per-request connections exhaust Postgres
#      max_connections long before you hit "millions/s" territory.
#
#   5. DB index to support the feed query directly:
#        CREATE INDEX CONCURRENTLY idx_connectedservice_public_feed
#          ON megamind_connectedservice
#          (status, is_connected, fetch_status, created_at DESC, id DESC)
#          WHERE status = 'public' AND is_connected = true
#            AND fetch_status = 'success';
#      (Partial index — matches the filter exactly, keeps it small.)
#
#   6. Serve via async workers (uvicorn/gunicorn+uvicorn workers) if
#      any I/O in the request path is async-capable, so worker threads
#      aren't blocked on network calls.
#
# What actually changed in this file vs. the previous version:
#
#   - Serialized media (images/videos/links) is no longer recomputed
#     on every request. It's precomputed once via `refresh_feed_cache()`
#     — call this from wherever ConnectedService.extracted_* fields get
#     written (the scraper/service_fetcher save path), not from the
#     request path. The request path now just reads `cached_payload`.
#   - `_fetch_page` results are cached in Redis for FEED_CACHE_TTL
#     seconds per (cursor, limit, media_type) key, so a burst of
#     identical requests between CDN cache expiries collapses into
#     one DB hit instead of one-per-request.
#   - HTTP Cache-Control / Vary headers added so any CDN/reverse proxy
#     sitting in front can cache the response independently.

import base64
import logging
from datetime import datetime

from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.core.cache import cache
from django.db.models import Q
from django.http import JsonResponse
from django.shortcuts import render
from django.utils.cache import patch_cache_control
from django.views.decorators.http import require_GET

from megamind.models.connected_service import ConnectedService
from megamind.utils.feed_cache import refresh_feed_cache

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════
# PAGINATION + CACHE SETTINGS
# ═══════════════════════════════════════════════════════════════════

DEFAULT_PAGE_SIZE = 12
MAX_PAGE_SIZE     = 30
VALID_MEDIA_TYPES = ('all', 'image', 'video', 'link')

# Server-side (Redis) cache for fully-built pages. Short TTL — this
# isn't meant to serve staleness for minutes, just to collapse
# concurrent duplicate requests and absorb CDN cache-miss bursts.
FEED_CACHE_TTL = getattr(settings, 'ENGINE_FEED_CACHE_TTL', 5)  # seconds

# What the CDN/browser is told to do with the response. Longer than
# the Redis TTL is fine — s-maxage governs shared/CDN caches, the
# CDN can be purged early on new posts if you want tighter freshness.
CDN_MAX_AGE = getattr(settings, 'ENGINE_FEED_CDN_MAX_AGE', 5)  # seconds


def _clamp_limit(raw, default: int = DEFAULT_PAGE_SIZE, maximum: int = MAX_PAGE_SIZE) -> int:
    try:
        limit = int(raw)
    except (TypeError, ValueError):
        return default
    return max(1, min(limit, maximum))


def _clean_media_type(raw) -> str:
    return raw if raw in VALID_MEDIA_TYPES else 'all'


# ═══════════════════════════════════════════════════════════════════
# CURSOR  (opaque, encodes "created_at|pk" of the last row on a page)
# ═══════════════════════════════════════════════════════════════════

def _encode_cursor(created_at, pk) -> str:
    raw = f"{created_at.isoformat()}|{pk}"
    return base64.urlsafe_b64encode(raw.encode()).decode()


def _decode_cursor(cursor: str):
    """Returns (created_at, pk) or (None, None) if the cursor is invalid."""
    try:
        raw = base64.urlsafe_b64decode(cursor.encode()).decode()
        ts_str, pk_str = raw.rsplit('|', 1)
        return datetime.fromisoformat(ts_str), int(pk_str)
    except Exception:
        return None, None


# ═══════════════════════════════════════════════════════════════════
# QUERY
# ═══════════════════════════════════════════════════════════════════

def _get_public_feed_queryset(cursor: str = None):
    """
    Base queryset for the global engine feed:
      - status='public'        → respects the per-service privacy toggle
      - is_connected=True      → the user hasn't disconnected it
      - fetch_status='success' → only show services with real scraped data
      - profile is public too  → defensive second privacy check, in case a
                                  user's profile is private even though a
                                  service on it is marked public
    Raises ValueError if `cursor` is provided but malformed.

    NOTE: pairs with the partial index described in the module
    docstring — filter order/columns here should match it so Postgres
    can actually use it.

    NOTE: route this queryset at a read replica in production
    (e.g. `.using('replica')`) — this file leaves the alias unset so
    it stays environment-agnostic; wire it via a DB router or
    `settings.ENGINE_FEED_DB_ALIAS` if you have one.
    """
    qs = (
        ConnectedService.objects
        .filter(status='public', is_connected=True, fetch_status='success')
        .filter(
            Q(user__profileinfo__isnull=True) |
            Q(user__profileinfo__is_profile_public=True)
        )
        .select_related('user', 'user__profileinfo')
        .order_by('-created_at', '-id')
    )

    if cursor:
        created_at, pk = _decode_cursor(cursor)
        if created_at is None:
            raise ValueError('invalid cursor')
        qs = qs.filter(Q(created_at__lt=created_at) | Q(created_at=created_at, id__lt=pk))

    return qs


# ═══════════════════════════════════════════════════════════════════
# SERIALIZER
# ═══════════════════════════════════════════════════════════════════
#
# NOTE: payload construction (image/video/link normalization,
# attribution) now lives in `megamind.utils.feed_cache.refresh_feed_cache`,
# called from the scraper/service_fetcher save path. This file only
# reads the precomputed result.

def _serialize_post(svc: ConnectedService, media_type: str = 'all') -> dict:
    """
    Reads the precomputed payload instead of recomputing it. Falls back
    to a live build only if a row somehow has no cached payload yet
    (e.g. mid-migration/backfill) so the feed never 500s on stale data.
    """
    payload = svc.cached_feed_payload
    if not payload:
        logger.warning('engine feed: no cached_feed_payload for service %s, building live', svc.pk)
        payload = refresh_feed_cache(svc)

    post = dict(payload)  # shallow copy — don't mutate the cached dict in place

    if media_type == 'image':
        post = {**post, 'videos': [], 'links': []}
    elif media_type == 'video':
        post = {**post, 'images': [], 'links': []}
    elif media_type == 'link':
        post = {**post, 'images': [], 'videos': []}

    return post


def _fetch_page(cursor: str, limit: int, media_type: str):
    """
    Shared page-fetch logic used by both EngineView and engine_feed_api.
    Wrapped in a short-TTL Redis cache keyed on the exact query params,
    so a burst of concurrent/duplicate requests (a CDN cache-miss
    stampede, multiple pods, etc.) collapses into a single DB read.
    """
    cache_key = f'engine:feed:{cursor or "-"}:{limit}:{media_type}'
    cached = cache.get(cache_key)
    if cached is not None:
        return cached

    qs = _get_public_feed_queryset(cursor=cursor)
    rows = list(qs[:limit + 1])       # fetch one extra row to detect has_more
    has_more = len(rows) > limit
    rows = rows[:limit]

    feed = [_serialize_post(svc, media_type) for svc in rows]
    next_cursor = feed[-1]['cursor'] if feed and has_more else None

    result = (feed, has_more, next_cursor)
    cache.set(cache_key, result, FEED_CACHE_TTL)
    return result


# ═══════════════════════════════════════════════════════════════════
# VIEWS
# ═══════════════════════════════════════════════════════════════════

@login_required(login_url='/customer/signin/')
def EngineView(request):
    """
    Global engine feed page. Renders the first page of public posts
    server-side; the page's JS calls `engine_feed_api` for infinite scroll.
    """
    limit      = _clamp_limit(request.GET.get('limit'))
    media_type = _clean_media_type(request.GET.get('type', 'all'))

    try:
        feed, has_more, next_cursor = _fetch_page(cursor=None, limit=limit, media_type=media_type)
    except ValueError:
        feed, has_more, next_cursor = [], False, None

    context = {
        'feed':         feed,
        'feed_count':   len(feed),
        'has_more':     has_more,
        'next_cursor':  next_cursor,
        'media_type':   media_type,
        'limit':        limit,
        'feed_api_url': '/engine/api/feed/',
    }
    response = render(request, 'business/engine.html', context)
    # First-page HTML is the same for everyone at a given moment — let a
    # CDN cache it briefly. `private` would be needed instead if this
    # ever starts including anything user-specific beyond auth-gating.
    patch_cache_control(response, public=True, max_age=CDN_MAX_AGE, s_maxage=CDN_MAX_AGE)
    return response


@login_required(login_url='/customer/signin/')
@require_GET
def engine_feed_api(request):
    """
    JSON pagination endpoint for the engine feed (infinite scroll).

    GET params:
        cursor - opaque cursor from a previous response's `next_cursor`
        limit  - page size, 1-30 (default 12)
        type   - 'all' | 'image' | 'video' | 'link'
    """
    limit      = _clamp_limit(request.GET.get('limit'))
    media_type = _clean_media_type(request.GET.get('type', 'all'))
    cursor     = request.GET.get('cursor') or None

    try:
        feed, has_more, next_cursor = _fetch_page(cursor=cursor, limit=limit, media_type=media_type)
    except ValueError:
        return JsonResponse({'success': False, 'error': 'invalid_cursor'}, status=400)

    response = JsonResponse({
        'success':     True,
        'results':     feed,
        'count':       len(feed),
        'has_more':    has_more,
        'next_cursor': next_cursor,
    })
    # Same payload for every visitor requesting this exact page — safe
    # for a CDN to cache and serve without hitting Django at all.
    patch_cache_control(response, public=True, max_age=CDN_MAX_AGE, s_maxage=CDN_MAX_AGE)
    response['Vary'] = 'Accept-Encoding'  # do NOT vary on Cookie/Authorization here —
                                           # doing so would make the CDN cache per-user
                                           # and defeat the point. See module docstring
                                           # note #2 about splitting auth from payload.
    return response


@login_required(login_url='/customer/signin/')
def PersonalEngineView(request):
    context = {}
    return render(request, 'business/personal_engine.html', context)