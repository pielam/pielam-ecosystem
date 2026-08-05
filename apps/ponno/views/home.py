# apps/ponno/views/home.py

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
#   - Every serialized post (both feed systems below) now carries a
#     guaranteed, never-empty `video_info` field via
#     megamind.utils.video_info.resolve_post_video — real extracted
#     video, or the source URL if it resolves to a known platform, or
#     a deterministic themed placeholder. See that module's docstring
#     for the full priority order. This mirrors megamind.utils.
#     feed_cache.refresh_feed_cache, which does the same for the
#     public/engine feed's precomputed payload.
#   - `videos` on the GLOBAL feed (_serialize_service) is no longer
#     the raw scraped {url, type} shape. Every entry is now run through
#     megamind.utils.video_info.get_video_info_cached — the exact same
#     normalizer megamind.utils.feed_cache._normalize_videos uses for
#     the PUBLIC/ENGINE feed's precomputed payload — so both feeds
#     agree on the shape: {platform, embed_url, watch_url, thumbnail,
#     type, mime_type?}. Previously the template guessed "is this
#     embeddable" from the raw URL with a regex that only recognized
#     YouTube/Vimeo, so every other platform (Facebook, TikTok, Twitch,
#     Rumble, ...) silently fell into a plain <video> tag and failed to
#     play. The frontend now just reads `type`/`embed_url` directly —
#     see the videoTagHTML() rewrite in home.html.
#
# ═══════════════════════════════════════════════════════════════════
# NOTE ON TWO FEED IMPLEMENTATIONS LIVING IN THIS FILE
# ═══════════════════════════════════════════════════════════════════
#
# This module contains two independent feed systems that both remain
# in active use (see apps/ponno/web_urls.py):
#
#   "PUBLIC/ENGINE" feed  — status='public' services only, reads the
#   precomputed `cached_feed_payload` (built by megamind.utils.feed_cache).
#   Powers: engine_feed_api.
#
#   "GLOBAL" feed — every is_connected=True service regardless of
#   public/private status, serializes extracted_images/videos/links
#   (with a last_fetched_data fallback) live, adds per-viewer
#   personalization (is_following / is_own_post), plus top-contributor
#   and header-stat aggregates. Powers: HomeEngineView, feed_load_more,
#   feed_page_api.
#
# Both systems independently need a "created_at|pk" opaque cursor, so
# both define an encode/decode helper. They are named distinctly
# below (`_encode_public_cursor`/`_decode_public_cursor` vs.
# `_encode_cursor`/`_decode_cursor`) specifically so one doesn't
# shadow the other — an earlier version of this file defined both
# pairs under the same names, and because Python just rebinds the
# name on the second `def`, every caller of the first pair (the
# PUBLIC/ENGINE queryset + pagination) ended up silently calling the
# GLOBAL feed's decoder instead, which has a different failure
# contract (returns `None` instead of `(None, None)` on a bad cursor)
# and broke invalid-cursor handling. Keep them distinct.
#
# Same footgun previously existed with `_personalize_feed`: an earlier
# revision defined it twice — once (near the top of this file) doing
# video-info enrichment with a hardcoded fallback URL, once (below,
# still present) doing follow/own-post enrichment for the GLOBAL feed.
# The second definition silently shadowed the first, so the video
# enrichment version never actually ran. That logic has since moved to
# megamind.utils.video_info.resolve_post_video (the single, shared
# place both feed pipelines call — see _serialize_post and
# _serialize_service below), and the dead duplicate has been removed.
# Only one `_personalize_feed` — the follow/own-post one — exists in
# this file now.

import base64
import binascii
import logging
from datetime import datetime
from typing import Any, Optional
from urllib.parse import urlparse

from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.core.cache import cache
from django.db.models import Count, Q
from django.db.models.signals import post_delete, post_save
from django.dispatch import receiver
from django.http import HttpResponseBadRequest, JsonResponse
from django.shortcuts import render
from django.urls import reverse
from django.utils import timezone
from django.utils.cache import patch_cache_control
from django.views.decorators.cache import cache_control
from django.views.decorators.http import require_GET

from apps.customer.models.account import User
from apps.customer.models.profile_info import ProfileInfo
from megamind.models.connected_service import ConnectedService
from megamind.utils.feed_cache import refresh_feed_cache
from megamind.utils.service_fetcher import fetch_service_data  # noqa: F401  (used by Celery task elsewhere)
from megamind.utils.video_info import get_video_info_cached, resolve_post_video

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════
# ═══  PUBLIC / ENGINE FEED  (status='public', precomputed payload)  ═
# ═══════════════════════════════════════════════════════════════════

DEFAULT_PAGE_SIZE = 12
MAX_PAGE_SIZE      = 30
VALID_MEDIA_TYPES  = ('all', 'image', 'video', 'link')

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


def _encode_public_cursor(created_at, pk) -> str:
    raw = f"{created_at.isoformat()}|{pk}"
    return base64.urlsafe_b64encode(raw.encode()).decode()


def _decode_public_cursor(cursor: str):
    """Returns (created_at, pk) or (None, None) if the cursor is invalid."""
    try:
        raw = base64.urlsafe_b64decode(cursor.encode()).decode()
        ts_str, pk_str = raw.rsplit('|', 1)
        return datetime.fromisoformat(ts_str), int(pk_str)
    except Exception:
        return None, None


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
        created_at, pk = _decode_public_cursor(cursor)
        if created_at is None:
            raise ValueError('invalid cursor')
        qs = qs.filter(Q(created_at__lt=created_at) | Q(created_at=created_at, id__lt=pk))

    return qs


def _serialize_post(svc: ConnectedService, media_type: str = 'all') -> dict:
    """
    Reads the precomputed payload instead of recomputing it. Falls back
    to a live build only if a row somehow has no cached payload yet
    (e.g. mid-migration/backfill) so the feed never 500s on stale data.

    The cached payload already includes `video_info` (see
    megamind.utils.feed_cache.refresh_feed_cache), so no extra work is
    needed here — it just needs to survive the media_type filtering
    below untouched, since the guaranteed video slot should still
    render even when filtering to media_type='image' or 'link'.
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
    Shared page-fetch logic used by engine_feed_api.
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
# ═══  GLOBAL FEED  (all connected services, live serialization,  ═══
# ═══  per-viewer personalization, contributors/stats)             ═══
# ═══════════════════════════════════════════════════════════════════
#
# All feed/contributor/stat data below is GLOBAL, not per-user — the
# feed shows every connected service to every logged-in user
# identically. An earlier implementation cached this data per-uid,
# which meant N users == N duplicate copies of the same payload in
# the cache and zero cache hits shared across users. Everything here
# uses a single shared key per resource instead.

FEED_PAGE_TTL       = 30          # 30s  — first-page feed (hot path, short so it stays fresh)
CONTRIBUTORS_TTL    = 60 * 5      # 5 min — top contributors (expensive aggregate, changes slowly)
STATS_TTL           = 60 * 2      # 2 min — header counts (can tolerate staleness)
ERROR_BACKOFF_TTL   = 60 * 10     # 10 min — cooldown after a failed background fetch
STALE_AFTER         = 3600        # 1 hour — flag a service as due for refetch
# NOTE: retry/refresh is handled by megamind's existing
#   POST /api/connected-services/{id}/refresh/
# — no duplicate retry endpoint or cooldown lives here. See the
# note near the bottom of this file for the frontend wiring.

FEED_PAGE_SIZE      = 30
FEED_PAGE_SIZE_MAX  = 100         # hard cap, ignore anything a client requests above this

ACTION_LABELS = {
    'product':       'listed a new product',
    'brand':         'added a new brand',
    'category':      'created a new category',
    'subcategory':   'created a new subcategory',
    'education':     'shared an education resource',
    'news':          'posted a news update',
    'location':      'added a new location',
    'api':           'connected an API endpoint',
    'person':        'connected a new profile',
    'business':      'connected a new business source',
    'entertainment': 'shared an entertainment listing',
}


# ═══════════════════════════════════════════════════════════════════
# CACHE KEYS  (global — no uid in the key)
# ═══════════════════════════════════════════════════════════════════

KEY_FEED_FIRST_PAGE = "engine:feed:first_page"
KEY_CONTRIBUTORS    = "engine:top_contributors"
KEY_STATS           = "engine:stats"


def invalidate_engine_cache() -> None:
    """
    Call this whenever a ConnectedService is created/updated/deleted.
    Wired below via post_save/post_delete signals, so callers no
    longer need to remember to invoke it manually.
    """
    cache.delete_many([KEY_FEED_FIRST_PAGE, KEY_CONTRIBUTORS, KEY_STATS])


@receiver(post_save, sender=ConnectedService)
@receiver(post_delete, sender=ConnectedService)
def _bust_engine_cache_on_service_change(sender, instance, **kwargs):
    invalidate_engine_cache()


# ═══════════════════════════════════════════════════════════════════
# CURSOR ENCODING (keyset pagination)
#
# We paginate on (-created_at, -id) rather than OFFSET, so page N is
# a cheap indexed range scan regardless of how deep into the feed you
# are. OFFSET-based pagination gets linearly slower the further in
# you go — unusable at this scale.
# ═══════════════════════════════════════════════════════════════════

def _encode_cursor(created_at, pk: int) -> str:
    raw = f"{created_at.isoformat()}|{pk}".encode()
    return base64.urlsafe_b64encode(raw).decode()


def _decode_cursor(cursor: str) -> Optional[tuple]:
    try:
        raw = base64.urlsafe_b64decode(cursor.encode()).decode()
        ts_str, pk_str = raw.split("|")
        return timezone.datetime.fromisoformat(ts_str), int(pk_str)
    except (ValueError, binascii.Error, UnicodeDecodeError):
        return None


# ═══════════════════════════════════════════════════════════════════
# STALENESS CHECK
# ═══════════════════════════════════════════════════════════════════

def _should_fetch(service: ConnectedService) -> bool:
    if service.last_fetch_time is None:
        return True
    age = (timezone.now() - service.last_fetch_time).total_seconds()
    if service.fetch_status == 'error':
        return age > ERROR_BACKOFF_TTL
    return age > STALE_AFTER


# ═══════════════════════════════════════════════════════════════════
# BACKGROUND REFRESH
#
# Deliberately NOT implemented here. Manual/automatic refresh of a
# ConnectedService goes through megamind's existing endpoint:
#
#     POST /api/connected-services/{id}/refresh/
#
# rather than a second, duplicate retry code path living in ponno.
# _should_fetch() above is still used to compute the `is_stale` flag
# shown in the feed, but this module no longer triggers fetches
# itself — see the frontend wiring note in home.html for how the
# "Retry" button calls the megamind endpoint directly.
# ═══════════════════════════════════════════════════════════════════


# ═══════════════════════════════════════════════════════════════════
# DISPLAY HELPERS
# ═══════════════════════════════════════════════════════════════════

def _time_ago(dt) -> str:
    """Small self-contained relative-time formatter (no django.contrib.humanize dependency)."""
    if dt is None:
        return ''
    seconds = (timezone.now() - dt).total_seconds()
    if seconds < 60:
        return "just now"
    minutes = seconds / 60
    if minutes < 60:
        return f"{int(minutes)}m ago"
    hours = minutes / 60
    if hours < 24:
        return f"{int(hours)}h ago"
    days = hours / 24
    if days < 7:
        return f"{int(days)}d ago"
    weeks = days / 7
    if weeks < 4:
        return f"{int(weeks)}w ago"
    months = days / 30
    if months < 12:
        return f"{int(months)}mo ago"
    return f"{int(days / 365)}y ago"


# ── Coercion helpers (handles both dict and bare-string payloads) ──
# Defensive: the scraper pipeline is expected to always write clean
# dicts into extracted_images/videos/links, but this keeps older or
# malformed rows from throwing instead of just degrading gracefully.

def _coerce_image(img) -> dict:
    if isinstance(img, dict):
        return {
            "url": img.get("url") or img.get("src") or "",
            "alt": img.get("alt") or "",
            "href": img.get("href") or "",
        }
    return {"url": str(img), "alt": "", "href": ""}


def _coerce_link(lnk) -> dict:
    if isinstance(lnk, dict):
        return {
            "href": lnk.get("href") or lnk.get("url")   or "",
            "text": lnk.get("text") or lnk.get("title") or "",
        }
    return {"href": str(lnk), "text": ""}


def _normalize_videos(raw_videos) -> list[dict]:
    """
    Runs every raw scraped video entry through
    megamind.utils.video_info.get_video_info_cached — the SAME
    normalizer megamind.utils.feed_cache._normalize_videos uses to
    build the PUBLIC/ENGINE feed's precomputed payload — so the
    GLOBAL feed's `videos` list agrees with it on shape:

        {platform, embed_url, watch_url, thumbnail, type, mime_type?}

    Accepts scraper dicts like {"url": ..., "type": "video/*"},
    bare URL strings, or already-normalized video_info dicts (a dict
    that already has "embed_url" is still fine — get_video_info_cached
    just re-parses `url`/`embed_url`/`src`, whichever is present).

    This replaces the previous behaviour of passing the raw
    {"url", "type"} shape straight to the template, which forced the
    frontend to guess "is this an iframe" from a regex that only knew
    YouTube/Vimeo — every other platform (Facebook, TikTok, Twitch,
    Rumble, Dailymotion, Streamable, Twitter/X, direct files) silently
    fell into a plain <video> tag and failed to play. Now the
    template can just read `type`/`embed_url` directly.

    Entries that fail to parse (empty/garbage URL) are dropped rather
    than surfaced as broken media — get_video_info_cached only ever
    returns {} for a falsy URL, never raises.
    """
    normalized = []
    for v in raw_videos or []:
        if isinstance(v, dict):
            url = v.get('url') or v.get('embed_url') or v.get('src')
        else:
            url = v
        if not url:
            continue
        info = get_video_info_cached(url)
        if info:
            normalized.append(info)
    return normalized


# ═══════════════════════════════════════════════════════════════════
# SERIALIZATION
# ═══════════════════════════════════════════════════════════════════

def _serialize_service(svc: ConnectedService, profile: Optional[ProfileInfo]) -> dict:
    # ── OG fields ────────────────────────────────────────────────
    og_title       = svc.og_title       or ""
    og_description = svc.og_description or ""
    og_thumbnail   = svc.og_thumbnail   or ""
    og_site_name   = svc.og_site_name   or ""
    og_type        = svc.og_type        or ""

    # ── Extracted media ─────────────────────────────────────────
    images = svc.extracted_images or []
    videos_raw = svc.extracted_videos or []
    links  = svc.extracted_links  or []
    text   = svc.extracted_text   or ""

    # ── Fallback: pull from raw cache blob for rows scraped before
    #    the flat OG/extracted columns existed. Only kicks in when
    #    the flat field is empty, so it costs nothing on the common
    #    path for services already using the new columns. ────────
    raw = svc.last_fetched_data or {}
    if raw and not og_title:
        og_title       = raw.get("title")       or raw.get("og_title")       or ""
        og_description = raw.get("description") or raw.get("og_description") or ""
        og_thumbnail   = (raw.get("og_image")   or raw.get("og_thumbnail")
                          or raw.get("thumbnail") or "")
        og_site_name   = raw.get("site_name")   or raw.get("og_site_name")   or ""
        og_type        = raw.get("og_type")     or raw.get("type")           or ""
    if raw and not images:
        images = [_coerce_image(i) for i in (raw.get("images") or []) if i]
    if raw and not videos_raw:
        videos_raw = raw.get("videos") or []
    if raw and not links:
        links  = [_coerce_link(l)  for l in (raw.get("links")  or []) if l]
    if raw and not text:
        text = (raw.get("full_content") or raw.get("text_content")
                or raw.get("content")   or raw.get("text") or "")

    # Platform-normalized videos — see _normalize_videos docstring.
    # `videos_count` below is intentionally computed from videos_raw,
    # not the normalized list: a video whose URL fails to parse is
    # dropped from `videos` but the post genuinely still "has" that
    # many attached videos for badge/signal-bar purposes.
    videos = _normalize_videos(videos_raw)

    # ── Guaranteed single video for card display ────────────────
    # Same resolver megamind.utils.feed_cache.refresh_feed_cache uses
    # for the public/engine feed's precomputed payload, so both feed
    # pipelines agree on what a post's video looks like. Priority:
    # first usable extracted video → source URL if it resolves to a
    # known platform → deterministic, service-type-themed placeholder.
    # Never empty — see resolve_post_video's docstring.
    video_info = resolve_post_video(
        extracted_videos=videos_raw,
        source_url=svc.service_url,
        seed=str(svc.pk),
        service_type=svc.service_type,
    )

    return {
        # ── User info ──────────────────────────────────
        'user_id':          svc.user_id,
        'user_uuid':        str(svc.user.uuid),
        'username':         svc.user.email_or_phone,
        'user_role':        svc.user.role,
        'user_email':       svc.user.email or svc.user.email_or_phone,

        # ── Profile info ───────────────────────────────
        'profile_name':     profile.profile_name if profile else svc.user.email_or_phone,
        'profile_photo':    profile.get_profile_photo_url() if profile else '/static/defaults/default-profile-picture.png',
        'profile_tagline':  profile.profile_tagline if profile else '',
        'profile_location': profile.location_display if profile else '',
        'profile_verified': profile.is_profile_verified if profile else False,
        'profile_type':     profile.profile_type if profile else 'personal',
        'profile_url':      reverse('customer:profile_view', kwargs={'username': svc.user.email_or_phone}),
        # Annotated on the queryset (see _feed_base_queryset) instead
        # of calling profile.follower_count, which would run a fresh
        # `self.followers.count()` query per row.
        'follower_count':   getattr(svc, 'follower_count', 0) or 0,

        # ── Service info ───────────────────────────────
        'svc_id':            svc.pk,
        'svc_name':           svc.service_name,
        'svc_url':            svc.service_url,
        'svc_type':           svc.service_type,
        'svc_status':         svc.status,
        'fetch_status':       svc.fetch_status,
        'fetch_error':        svc.fetch_error or '',
        'action_label':       ACTION_LABELS.get(svc.service_type, 'connected a new source'),

        # ── OG / meta ──────────────────────────────────
        'og_title':          og_title or svc.service_name,
        'og_description':    og_description,
        'og_thumbnail':      og_thumbnail,
        'og_site_name':      og_site_name,
        'og_type':           og_type,
        'service_domain':    urlparse(svc.service_url).netloc,

        # ── Extracted content ──────────────────────────
        'images':            images,
        # Platform-normalized: {platform, embed_url, watch_url,
        # thumbnail, type, mime_type?} per entry — see _normalize_videos.
        'videos':            videos,
        'video_info':         video_info,   # guaranteed — never empty
        'links':             links,
        'text':              text,

        # ── Counts ─────────────────────────────────────
        'images_count':      len(images),
        'videos_count':      len(videos_raw),
        'links_count':       len(links),
        'has_text':          bool(text.strip()),

        # ── Freshness ──────────────────────────────────
        'is_stale':          _should_fetch(svc),

        # ── Timestamps ─────────────────────────────────
        'posted_at':          svc.created_at.isoformat(),
        'posted_at_human':    _time_ago(svc.created_at),
        'last_fetch':         svc.last_fetch_time.isoformat() if svc.last_fetch_time else None,
        'last_fetch_human':   _time_ago(svc.last_fetch_time),

        # ── Pagination ─────────────────────────────────
        '_cursor':            _encode_cursor(svc.created_at, svc.pk),
    }


# ═══════════════════════════════════════════════════════════════════
# FEED QUERY (keyset-paginated, N+1-free)
# ═══════════════════════════════════════════════════════════════════

def _feed_base_queryset():
    return (
        ConnectedService.objects
        .filter(is_connected=True)
        .select_related('user', 'user__profileinfo')
        # Single JOIN + GROUP BY for follower counts instead of one
        # `.count()` query per feed row. At extreme scale, the next
        # thing to optimize away is denormalizing follower_count onto
        # ProfileInfo itself and dropping this annotation entirely.
        .annotate(follower_count=Count('user__profileinfo__followers', distinct=True))
        .order_by('-created_at', '-id')
    )


def get_feed_page(cursor: Optional[str] = None, page_size: int = FEED_PAGE_SIZE) -> dict:
    """
    Returns {'items': [...], 'next_cursor': str|None}.
    Only the cursor-less first page is cached — that's what >99% of
    traffic hits (new page loads, refreshes, the CDN-facing API).
    Deeper pages are cheap keyset scans and don't need caching.
    """
    page_size = min(max(page_size, 1), FEED_PAGE_SIZE_MAX)
    use_cache = cursor is None and page_size == FEED_PAGE_SIZE

    if use_cache:
        cached = cache.get(KEY_FEED_FIRST_PAGE)
        if cached is not None:
            return cached

    qs = _feed_base_queryset()

    if cursor:
        decoded = _decode_cursor(cursor)
        if decoded is None:
            raise ValueError("invalid cursor")
        created_at, pk = decoded
        # Keyset (seek) pagination: strictly "older" than the cursor
        # row, tie-broken by id. Cheap indexed range scan regardless
        # of how deep into the feed this is — unlike OFFSET, which
        # gets slower the further in you paginate.
        qs = (
            qs.filter(created_at__lt=created_at)
            | qs.filter(created_at=created_at, id__lt=pk)
        ).order_by('-created_at', '-id')

    # Fetch one extra row to know whether there's a next page, without
    # a separate COUNT(*) query.
    rows = list(qs[:page_size + 1])
    has_more = len(rows) > page_size
    rows = rows[:page_size]

    profiles_by_user = {
        row.user_id: getattr(row.user, 'profileinfo', None)
        for row in rows
    }

    items = [_serialize_service(row, profiles_by_user.get(row.user_id)) for row in rows]
    next_cursor = items[-1]['_cursor'] if (has_more and items) else None

    result = {'items': items, 'next_cursor': next_cursor}

    if use_cache:
        cache.set(KEY_FEED_FIRST_PAGE, result, FEED_PAGE_TTL)

    return result


# ═══════════════════════════════════════════════════════════════════
# PER-VIEWER PERSONALIZATION
#
# get_feed_page()'s result is shared/cached across every user, so it
# can never contain anything specific to who's looking at it. This
# layer runs *after* the cache lookup, on the already-serialized
# items, to attach the two things a "social" feed needs per-viewer:
# whether they follow the poster, and whether it's their own post.
# It never mutates the cached objects — .copy() on each item first.
# ═══════════════════════════════════════════════════════════════════

def _personalize_feed(items: list[dict], viewer_id: int) -> list[dict]:
    owner_ids = {item['user_id'] for item in items}
    following_ids: set = set()

    if owner_ids:
        # ProfileInfo.pk == User.pk (OneToOneField primary_key=True),
        # so profileinfo_id on the through table equals the poster's
        # user_id — one query covers every poster on the page.
        following_ids = set(
            ProfileInfo.followers.through.objects
            .filter(profileinfo_id__in=owner_ids, user_id=viewer_id)
            .values_list('profileinfo_id', flat=True)
        )

    personalized = []
    for item in items:
        item = item.copy()
        item['is_following'] = item['user_id'] in following_ids
        item['is_own_post']  = item['user_id'] == viewer_id
        personalized.append(item)
    return personalized


# ═══════════════════════════════════════════════════════════════════
# TOP CONTRIBUTORS  (single aggregate + single batched user fetch)
# ═══════════════════════════════════════════════════════════════════

def _get_top_contributors(limit: int = 10) -> list[dict[str, Any]]:
    cached = cache.get(KEY_CONTRIBUTORS)
    if cached is not None:
        return cached

    rows = list(
        ConnectedService.objects
        .filter(is_connected=True)
        .values('user_id')
        .annotate(service_count=Count('id'))
        .order_by('-service_count')[:limit]
    )
    if not rows:
        cache.set(KEY_CONTRIBUTORS, [], CONTRIBUTORS_TTL)
        return []

    user_ids = [r['user_id'] for r in rows]
    # One query for every user + profile involved, instead of
    # User.objects.get() + .profileinfo in a loop.
    users_by_id = {
        u.pk: u
        for u in User.objects.filter(pk__in=user_ids).select_related('profileinfo')
    }

    contributors = []
    for row in rows:
        user = users_by_id.get(row['user_id'])
        if user is None:
            continue
        profile = getattr(user, 'profileinfo', None)
        contributors.append({
            'user_id':        user.pk,
            'username':       user.email_or_phone,
            'profile_name':   profile.profile_name if profile else user.email_or_phone,
            'profile_photo':  profile.get_profile_photo_url() if profile else '/static/defaults/default-profile-picture.png',
            'profile_url':    reverse('customer:profile_view', kwargs={'username': user.email_or_phone}),
            'service_count':  row['service_count'],
        })

    cache.set(KEY_CONTRIBUTORS, contributors, CONTRIBUTORS_TTL)
    return contributors


# ═══════════════════════════════════════════════════════════════════
# HEADER STATS
#
# Computed as their own small aggregate queries (not by summing over
# an in-memory feed, which only worked when the whole table was
# loaded per request) and cached separately, since a couple minutes
# of staleness on a header counter is invisible to users.
# ═══════════════════════════════════════════════════════════════════

def _get_global_stats() -> dict:
    cached = cache.get(KEY_STATS)
    if cached is not None:
        return cached

    type_counts = dict(
        ConnectedService.objects
        .filter(is_connected=True)
        .values('service_type')
        .annotate(count=Count('id'))
        .values_list('service_type', 'count')
    )

    stats = {
        'total_users':    User.objects.filter(is_active=True, deleted_at__isnull=True).count(),
        'total_services': sum(type_counts.values()),
        'type_counts':    type_counts,
    }

    cache.set(KEY_STATS, stats, STATS_TTL)
    return stats


# ═══════════════════════════════════════════════════════════════════
# VIEWS
# ═══════════════════════════════════════════════════════════════════

@login_required(login_url='/customer/signin/')
@cache_control(private=True, max_age=15, must_revalidate=True)
def HomeEngineView(request):
    """
    Authenticated page shell for the GLOBAL feed. Renders only the
    first feed page server-side (cached, see get_feed_page). Further
    pages are fetched client-side from feed_load_more, which — unlike
    this view — returns cheap keyset pages instead of a full re-render.

    Marked `private` (not CDN-cacheable) because the rendered page is
    personalized per viewer (is_following / is_own_post on each item).
    """
    page = get_feed_page(cursor=None, page_size=FEED_PAGE_SIZE)
    feed_items = _personalize_feed(page['items'], request.user.id)
    top_contributors = _get_top_contributors(limit=10)
    stats = _get_global_stats()

    context = {
        'current_user':      request.user,
        'feed':               feed_items,
        'feed_count':         len(feed_items),
        'next_cursor':        page['next_cursor'],
        'top_contributors':   top_contributors,
        'total_users':        stats['total_users'],
        'total_services':     stats['total_services'],
        'type_counts':        stats['type_counts'],
        'feed_api_url':       reverse('ponno:feed_load_more'),
    }
    return render(request, 'ponno/home.html', context)


@login_required(login_url='/customer/signin/')
@require_GET
def engine_feed_api(request):
    """
    JSON pagination endpoint for the PUBLIC/ENGINE feed (infinite
    scroll), backed by the precomputed cached_feed_payload.

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


@require_GET
@cache_control(public=True, max_age=20, s_maxage=30, stale_while_revalidate=30)
def feed_page_api(request):
    """
    Public, CDN-cacheable JSON feed endpoint (GLOBAL feed, unpersonalized).
    Deliberately NOT login_required: the feed content is identical for
    every viewer, so gating it behind auth would force every request
    through Django instead of letting a CDN/edge cache absorb the bulk
    of traffic. If the product requirements change and the feed needs
    to be personalized or access-restricted, this endpoint must go
    back behind login_required and public caching must be removed.

    Query params:
        cursor    - opaque pagination cursor from a previous response
        page_size - optional, capped at FEED_PAGE_SIZE_MAX
    """
    cursor = request.GET.get('cursor') or None
    try:
        page_size = int(request.GET.get('page_size', FEED_PAGE_SIZE))
    except ValueError:
        return HttpResponseBadRequest("invalid page_size")

    try:
        page = get_feed_page(cursor=cursor, page_size=page_size)
    except ValueError:
        return HttpResponseBadRequest("invalid cursor")

    return JsonResponse(page)


@login_required(login_url='/customer/signin/')
@require_GET
@cache_control(private=True, max_age=15, must_revalidate=True)
def feed_load_more(request):
    """
    "Load more" endpoint for the feed rendered in home.html. Unlike
    feed_page_api, this one IS login-gated and personalized (adds
    is_following / is_own_post per item), so it's marked private and
    is not CDN-cacheable — each request still hits Django, but it's
    a cheap keyset page fetch plus one small batched follow-status
    query, not a full-table scan.

    Query params:
        cursor - required, from the previous page's next_cursor
    """
    cursor = request.GET.get('cursor')
    if not cursor:
        return HttpResponseBadRequest("cursor is required")

    try:
        page = get_feed_page(cursor=cursor, page_size=FEED_PAGE_SIZE)
    except ValueError:
        return HttpResponseBadRequest("invalid cursor")

    page['items'] = _personalize_feed(page['items'], request.user.id)
    return JsonResponse(page)


@login_required(login_url='/customer/signin/')
def PersonalEngineView(request):
    context = {}
    return render(request, "business/personal_engine.html", context)