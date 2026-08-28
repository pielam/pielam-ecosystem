# apps/ponno/views/home.py
#
# See original module docstring for full architecture notes (kept
# below, unchanged) — this version applies two fixes on top of it:
#
#   PATCH 1 (CRITICAL): _personalize_feed() was leaking raw datetime
#   objects ('_created_at_dt' and 'created_at') into dicts that get
#   passed straight to JsonResponse() from feed_load_more() and
#   refresh_feed_post(). That's a live TypeError/500 any time
#   GLOBAL_FEED_RANKING_ENABLED is True (the default). Fixed by
#   stripping both keys right after HomeFeedEngine ranking runs.
#
#   PATCH 2: the PUBLIC/ENGINE feed's per-page cache
#   (`engine:feed:{cursor}:{limit}:{type}`) had no bounded key set to
#   invalidate on write, so it only self-healed after FEED_CACHE_TTL
#   (5s) — a refresh via refresh_feed_post could take up to 5s to show
#   in engine_feed_api. Fixed with a cheap cache "version" counter:
#   every _fetch_page cache key now includes the current version, and
#   the post_save/post_delete signal bumps the version instead of
#   trying to enumerate/delete every possible key. Bumping the version
#   makes every previously-cached page key unreachable instantly,
#   without needing delete_many() over an unbounded key space.
#
#   VERIFIED AGAINST: megamind/models/connected_service.py (enterprise
#   crawling schema — DomainCrawlPolicy / CrawlAttempt / ConnectedService,
#   robots.txt support intentionally removed), megamind/services/scraper.py,
#   and megamind/services/intelligence_scraper.py. Every ConnectedService
#   field/constant this module reads (status, is_connected, is_active,
#   fetch_status values, circuit_breaker_open, max_crawl_depth,
#   cached_feed_payload, domain, og_*/extracted_*/word_count/
#   reading_time_minutes, service_type, created_at, last_fetch_time) and
#   the UnsafeCrawlURLError import path match the current model — no
#   further wiring changes were needed here.
#
#   NOTE (post ponno/home.html redesign): the feed's "Details" popup
#   now renders its rich content (Basic info / OG / Page identity /
#   Headings / Images-Videos-Links-Intelligence-Text-Crawl tabs) by
#   calling the DRF ConnectedServiceViewSet's existing `preview` and
#   `link-scan` actions directly (engine/profile_views/engine_profile.py,
#   mounted at /api/connected-services/) — the same endpoints the
#   personal engine dashboard already uses. That view already permits a
#   logged-in viewer to preview any public, connected, active service
#   (not just their own), so no new endpoint was needed here.
#   feed_post_detail() below is kept as-is (still callable) even though
#   the current home.html JS no longer calls it, in case anything else
#   in the app still depends on it.

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
from django.shortcuts import get_object_or_404, render
from django.urls import reverse
from django.utils import timezone
from django.utils.cache import patch_cache_control
from django.views.decorators.cache import cache_control
from django.views.decorators.http import require_GET, require_http_methods

from apps.customer.models.account import User
from apps.customer.models.profile_info import ProfileInfo
from megamind.models.connected_service import ConnectedService, UnsafeCrawlURLError
from megamind.utils.feed_cache import refresh_feed_cache
from megamind.utils.link_info import prepare_links_for_display
from megamind.utils.media_info import normalize_images
from megamind.utils.service_fetcher import fetch_service_data  # noqa: F401  (used by Celery task elsewhere)
from megamind.utils.video_info import get_video_info_cached, resolve_post_video

from megamind.home_feed_algorithm import HomeFeedEngine

from engine.profile_views.engine_profile import (
    ServiceRefreshBlockedError,
    ServiceRefreshLockedError,
    _check_manual_refresh_cooldown,
    _run_guarded_fetch,
    _serialize_service as _engine_serialize_service,
)

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════
# ═══  PUBLIC / ENGINE FEED  (status='public', precomputed payload)  ═
# ═══════════════════════════════════════════════════════════════════

DEFAULT_PAGE_SIZE = 12
MAX_PAGE_SIZE      = 30
VALID_MEDIA_TYPES  = ('all', 'image', 'video', 'link')

CONNECTED_SERVICE_REFRESH_URL_TEMPLATE = "/api/connected-services/{pk}/refresh/"
CONNECTED_SERVICE_PREVIEW_URL_TEMPLATE = "/api/connected-services/{pk}/preview/"

FEED_CACHE_TTL = getattr(settings, 'ENGINE_FEED_CACHE_TTL', 5)  # seconds
CDN_MAX_AGE = getattr(settings, 'ENGINE_FEED_CDN_MAX_AGE', 5)  # seconds

# ── PATCH 2: cache-version key ──────────────────────────────────────
# Every _fetch_page() cache key is namespaced with this version
# number. Bumping it (see _bust_engine_cache_on_service_change below)
# makes every previously-cached PUBLIC/ENGINE feed page unreachable on
# the next read, without needing to enumerate/delete an unbounded set
# of cursor/limit/type combinations. Falls back to 1 if the counter
# itself isn't in cache yet (cold cache / first request).
KEY_PUBLIC_FEED_CACHE_VERSION = "engine:feed:cache_version"


def _get_public_feed_cache_version() -> int:
    version = cache.get(KEY_PUBLIC_FEED_CACHE_VERSION)
    if version is None:
        version = 1
        cache.set(KEY_PUBLIC_FEED_CACHE_VERSION, version, None)  # no expiry
    return version


def _bump_public_feed_cache_version() -> None:
    try:
        cache.incr(KEY_PUBLIC_FEED_CACHE_VERSION)
    except ValueError:
        # Key didn't exist yet (nothing cached since last restart) —
        # nothing to invalidate.
        cache.set(KEY_PUBLIC_FEED_CACHE_VERSION, 1, None)


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
    qs = (
        ConnectedService.objects
        .filter(status='public', is_connected=True, fetch_status='success', is_active=True)
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


def _serialize_public_post(svc: ConnectedService, media_type: str = 'all') -> dict:
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
    Shared page-fetch logic used by engine_feed_api. Wrapped in a
    short-TTL Redis cache keyed on the exact query params (plus the
    current cache version — see PATCH 2 above), so a burst of
    concurrent/duplicate requests collapses into a single DB read, and
    a refresh elsewhere in the app can invalidate this instantly by
    bumping the version instead of waiting out FEED_CACHE_TTL.
    """
    version = _get_public_feed_cache_version()
    cache_key = f'engine:feed:v{version}:{cursor or "-"}:{limit}:{media_type}'
    cached = cache.get(cache_key)
    if cached is not None:
        return cached

    qs = _get_public_feed_queryset(cursor=cursor)
    rows = list(qs[:limit + 1])       # fetch one extra row to detect has_more
    has_more = len(rows) > limit
    rows = rows[:limit]

    feed = [_serialize_public_post(svc, media_type) for svc in rows]
    next_cursor = feed[-1]['cursor'] if feed and has_more else None

    result = (feed, has_more, next_cursor)
    cache.set(cache_key, result, FEED_CACHE_TTL)
    return result


# ═══════════════════════════════════════════════════════════════════
# ═══  GLOBAL FEED  (public + connected services across ALL users)  ═
# ═══════════════════════════════════════════════════════════════════

FEED_PAGE_TTL       = 30
CONTRIBUTORS_TTL    = 60 * 5
STATS_TTL           = 60 * 2
ERROR_BACKOFF_TTL   = 60 * 10
STALE_AFTER         = 3600

FEED_PAGE_SIZE      = 30
FEED_PAGE_SIZE_MAX  = 100

GLOBAL_FEED_RANKING_ENABLED = getattr(settings, 'PONNO_GLOBAL_FEED_RANKING_ENABLED', True)

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

KEY_FEED_FIRST_PAGE = "engine:feed:first_page"
KEY_CONTRIBUTORS    = "engine:top_contributors"
KEY_STATS           = "engine:stats"


def invalidate_engine_cache() -> None:
    """Call whenever a ConnectedService is created/updated/deleted.
    Wired below via post_save/post_delete signals."""
    cache.delete_many([KEY_FEED_FIRST_PAGE, KEY_CONTRIBUTORS, KEY_STATS])


@receiver(post_save, sender=ConnectedService)
@receiver(post_delete, sender=ConnectedService)
def _bust_engine_cache_on_service_change(sender, instance, **kwargs):
    invalidate_engine_cache()
    # PATCH 2: bump the PUBLIC/ENGINE feed's cache version so every
    # previously-cached page (_fetch_page) is unreachable on the very
    # next request, instead of relying solely on FEED_CACHE_TTL (5s)
    # to self-heal. Cheap: one INCR, no key enumeration needed.
    _bump_public_feed_cache_version()


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


def _should_fetch(service: ConnectedService) -> bool:
    if service.fetch_status == ConnectedService.FetchStatus.LOCKED:
        return False
    if getattr(service, 'circuit_breaker_open', False):
        return False
    if service.last_fetch_time is None:
        return True
    age = (timezone.now() - service.last_fetch_time).total_seconds()
    if service.fetch_status == 'error':
        return age > ERROR_BACKOFF_TTL
    return age > STALE_AFTER


def _time_ago(dt) -> str:
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


def _format_count(n: int) -> str:
    if n is None:
        return "0"
    if n >= 1000:
        return f"{n / 1000:.1f}k".replace(".0k", "k")
    return str(n)


def _coerce_video(v) -> Optional[str]:
    if isinstance(v, dict):
        return v.get('url') or v.get('embed_url') or v.get('src')
    return v or None


def _normalize_videos(raw_videos) -> list:
    normalized = []
    for v in raw_videos or []:
        url = _coerce_video(v)
        if not url:
            continue
        info = get_video_info_cached(url)
        if info:
            normalized.append(info)
    return normalized


def _serialize_service(svc: ConnectedService, profile: Optional[ProfileInfo]) -> dict:
    og_title       = svc.og_title       or ""
    og_description = svc.og_description or ""
    og_thumbnail   = svc.og_thumbnail   or ""
    og_site_name   = svc.og_site_name   or ""
    og_type        = svc.og_type        or ""
    favicon        = svc.favicon        or ""
    canonical_url  = svc.canonical_url  or ""

    images_source = svc.extracted_images or []
    videos_raw    = svc.extracted_videos or []
    links_source  = svc.extracted_links  or []
    text          = svc.extracted_text   or ""
    word_count           = svc.word_count or 0
    reading_time_minutes = svc.reading_time_minutes or 0

    raw = svc.last_fetched_data or {}
    if raw and not og_title:
        og_title       = raw.get("title")       or raw.get("og_title")       or ""
        og_description = raw.get("description") or raw.get("og_description") or ""
        og_thumbnail   = (raw.get("og_image")   or raw.get("og_thumbnail")
                          or raw.get("thumbnail") or "")
        og_site_name   = raw.get("site_name")   or raw.get("og_site_name")   or ""
        og_type        = raw.get("og_type")     or raw.get("type")           or ""
    if raw and not favicon:
        favicon = raw.get("favicon") or ""
    if raw and not canonical_url:
        canonical_url = raw.get("canonical_url") or ""
    if raw and not images_source:
        images_source = raw.get("images") or []
    if raw and not videos_raw:
        videos_raw = raw.get("videos") or []
    if raw and not links_source:
        links_source = raw.get("links") or []
    if raw and not text:
        text = (raw.get("full_content") or raw.get("text_content")
                or raw.get("content")   or raw.get("text") or "")

    images = normalize_images(images_source)
    videos = _normalize_videos(videos_raw)
    links  = prepare_links_for_display(links_source, base_domain=svc.domain)

    video_info = resolve_post_video(
        extracted_videos=videos_raw,
        source_url=svc.service_url,
        seed=str(svc.pk),
        service_type=svc.service_type,
    )

    return {
        'user_id':          svc.user_id,
        'user_uuid':        str(svc.user.uuid),
        'username':         svc.user.email_or_phone,
        'user_role':        svc.user.role,
        'profile_name':     profile.profile_name if profile else svc.user.email_or_phone,
        'profile_photo':    profile.get_profile_photo_url() if profile else '/static/defaults/default-profile-picture.png',
        'profile_tagline':  profile.profile_tagline if profile else '',
        'profile_location': profile.location_display if profile else '',
        'profile_verified': profile.is_profile_verified if profile else False,
        'profile_type':     profile.profile_type if profile else 'personal',
        'profile_url':      reverse('customer:profile_view', kwargs={'username': svc.user.email_or_phone}),
        'follower_count':   getattr(svc, 'follower_count', 0) or 0,

        'svc_id':            svc.pk,
        'svc_name':           svc.service_name,
        'svc_url':            svc.service_url,
        'svc_type':           svc.service_type,
        'svc_status':         svc.status,
        'fetch_status':       svc.fetch_status,
        'fetch_error':        svc.fetch_error or '',
        'circuit_breaker_open': svc.circuit_breaker_open,
        'action_label':       ACTION_LABELS.get(svc.service_type, 'connected a new source'),
        'max_crawl_depth':    svc.max_crawl_depth,

        'og_title':          og_title or svc.service_name,
        'og_description':    og_description,
        'og_thumbnail':      og_thumbnail,
        'og_site_name':      og_site_name,
        'og_type':           og_type,
        'favicon':           favicon,
        'canonical_url':     canonical_url,
        'service_domain':    svc.domain or urlparse(svc.service_url).netloc,

        'images':            images,
        'videos':            videos,
        'video_info':         video_info,
        'links':             links,
        'text':              text,

        'images_count':      len(images),
        'videos_count':      len(videos_raw),
        'links_count':       len(links),
        'word_count':        word_count,
        'word_count_display': _format_count(word_count),
        'reading_time_minutes': reading_time_minutes,
        'has_text':          bool(text.strip()),

        'is_stale':          _should_fetch(svc),

        'posted_at':          svc.created_at.isoformat(),
        'posted_at_human':    _time_ago(svc.created_at),
        'last_fetch':         svc.last_fetch_time.isoformat() if svc.last_fetch_time else None,
        'last_fetch_human':   _time_ago(svc.last_fetch_time),
        # Raw datetime, kept only for HomeFeedEngine's recency scoring
        # in _personalize_feed — PATCH 1 strips this (and the
        # 'created_at' key it seeds) before any dict reaches
        # JsonResponse(), since datetime objects aren't JSON-serializable.
        '_created_at_dt':     svc.created_at,

        '_cursor':            _encode_cursor(svc.created_at, svc.pk),

        'refresh_url':        CONNECTED_SERVICE_REFRESH_URL_TEMPLATE.format(pk=svc.pk),
        'detail_url':         CONNECTED_SERVICE_PREVIEW_URL_TEMPLATE.format(pk=svc.pk),
    }


def _feed_base_queryset():
    return (
        ConnectedService.objects
        .filter(is_connected=True, status='public', is_active=True)
        .select_related('user', 'user__profileinfo')
        .annotate(follower_count=Count('user__profileinfo__followers', distinct=True))
        .order_by('-created_at', '-id')
    )


def get_feed_page(cursor: Optional[str] = None, page_size: int = FEED_PAGE_SIZE) -> dict:
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
        qs = (
            qs.filter(created_at__lt=created_at)
            | qs.filter(created_at=created_at, id__lt=pk)
        ).order_by('-created_at', '-id')

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


def _personalize_feed(items: list, viewer_id: int, viewer=None) -> list:
    """
    Adds is_following / is_own_post per item, and (when ranking is on)
    re-orders via HomeFeedEngine.

    PATCH 1: previously returned dicts still carrying raw datetime
    objects under '_created_at_dt' (set by _serialize_service) and
    'created_at' (set below, to feed HomeFeedEngine). Every caller of
    this function passes its return value straight to JsonResponse —
    feed_load_more() and refresh_feed_post() both do — which raises
    TypeError: Object of type datetime is not JSON serializable the
    moment ranking is enabled (GLOBAL_FEED_RANKING_ENABLED defaults to
    True). Both keys are now stripped right after ranking runs, before
    this function returns anything to a caller.
    """
    owner_ids = {item['user_id'] for item in items}
    following_ids: set = set()

    if owner_ids:
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

    if viewer is not None and GLOBAL_FEED_RANKING_ENABLED and personalized:
        try:
            engine = HomeFeedEngine(viewer)
            for item in personalized:
                item['created_at'] = item.get('_created_at_dt')
            personalized = engine.rank_dicts(personalized)
        except Exception:
            logger.exception(
                'home_feed_algorithm ranking failed for viewer_id=%s, '
                'falling back to chronological order', viewer_id,
            )

    # PATCH 1: strip non-JSON-serializable datetime fields before this
    # list can reach any JsonResponse() call site. 'posted_at' (an ISO
    # string, set in _serialize_service) remains as the public,
    # JSON-safe timestamp for consumers.
    for item in personalized:
        item.pop('_created_at_dt', None)
        item.pop('created_at', None)

    return personalized


def _get_top_contributors(limit: int = 10) -> list:
    cached = cache.get(KEY_CONTRIBUTORS)
    if cached is not None:
        return cached

    rows = list(
        ConnectedService.objects
        .filter(is_connected=True, is_active=True)
        .values('user_id')
        .annotate(service_count=Count('id'))
        .order_by('-service_count')[:limit]
    )
    if not rows:
        cache.set(KEY_CONTRIBUTORS, [], CONTRIBUTORS_TTL)
        return []

    user_ids = [r['user_id'] for r in rows]
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


def _get_global_stats() -> dict:
    cached = cache.get(KEY_STATS)
    if cached is not None:
        return cached

    type_counts = dict(
        ConnectedService.objects
        .filter(is_connected=True, is_active=True)
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
# VIEWS — page shell + pagination
# ═══════════════════════════════════════════════════════════════════

@login_required(login_url='/customer/signin/')
@cache_control(private=True, max_age=15, must_revalidate=True)
def HomeEngineView(request):
    page = get_feed_page(cursor=None, page_size=FEED_PAGE_SIZE)
    feed_items = _personalize_feed(page['items'], request.user.id, viewer=request.user)
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
    patch_cache_control(response, public=True, max_age=CDN_MAX_AGE, s_maxage=CDN_MAX_AGE)
    response['Vary'] = 'Accept-Encoding'
    return response


@require_GET
@cache_control(public=True, max_age=20, s_maxage=30, stale_while_revalidate=30)
def feed_page_api(request):
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
    cursor = request.GET.get('cursor')
    if not cursor:
        return HttpResponseBadRequest("cursor is required")

    try:
        page = get_feed_page(cursor=cursor, page_size=FEED_PAGE_SIZE)
    except ValueError:
        return HttpResponseBadRequest("invalid cursor")

    page['items'] = _personalize_feed(page['items'], request.user.id, viewer=request.user)
    return JsonResponse(page)


# ═══════════════════════════════════════════════════════════════════
# VIEWS — per-post Refresh / Details (the card buttons in the image)
# ═══════════════════════════════════════════════════════════════════

@login_required(login_url='/customer/signin/')
@require_http_methods(["POST"])
def refresh_feed_post(request, service_id):
    service = get_object_or_404(
        ConnectedService, pk=service_id, is_active=True, is_connected=True, status='public',
    )

    if not _check_manual_refresh_cooldown(service.pk):
        return JsonResponse(
            {
                'success': False,
                'error': 'This post was just refreshed — please wait a few seconds before trying again.',
            },
            status=429,
        )

    try:
        _run_guarded_fetch(service, worker_id=f"feed-refresh-user-{request.user.pk}")
    except ServiceRefreshLockedError as exc:
        return JsonResponse({'success': False, 'error': str(exc)}, status=409)
    except (UnsafeCrawlURLError, ServiceRefreshBlockedError) as exc:
        service.refresh_from_db()
        profile = getattr(service.user, 'profileinfo', None)
        return JsonResponse(
            {'success': False, 'error': str(exc), **_serialize_service(service, profile)},
            status=200,
        )

    service.refresh_from_db()
    profile = getattr(service.user, 'profileinfo', None)
    post = _serialize_service(service, profile)
    post = _personalize_feed([post], request.user.id, viewer=None)[0]
    return JsonResponse({'success': True, **post})


@login_required(login_url='/customer/signin/')
@require_GET
def feed_post_detail(request, service_id):
    """
    Kept for backward compatibility — the ponno/home.html Details popup
    now calls the DRF ConnectedServiceViewSet's `preview` action
    (/api/connected-services/<id>/preview/) directly instead, since
    that endpoint already serves richer, field-complete scraped data
    and already permits a logged-in viewer to preview any public,
    connected, active service — not just their own. This view is left
    in place in case anything else still links to it.
    """
    service = get_object_or_404(
        ConnectedService, pk=service_id, is_active=True, is_connected=True, status='public',
    )

    detail = _engine_serialize_service(service)

    profile = getattr(service.user, 'profileinfo', None)
    detail['username'] = service.user.email_or_phone
    detail['profile_name'] = profile.profile_name if profile else service.user.email_or_phone
    detail['profile_photo'] = (
        profile.get_profile_photo_url() if profile else '/static/defaults/default-profile-picture.png'
    )
    detail['profile_url'] = reverse('customer:profile_view', kwargs={'username': service.user.email_or_phone})
    detail['posted_at_human'] = _time_ago(service.created_at)
    detail['last_fetch_human'] = _time_ago(service.last_fetch_time)

    return JsonResponse({'success': True, **detail})


@login_required(login_url='/customer/signin/')
def PersonalEngineView(request):
    context = {}
    return render(request, "business/personal_engine.html", context)