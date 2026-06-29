"""
apps/ponno/views/discovery_engine.py
=====================================
Discovery Engine View — Enterprise Edition

Architecture overview
─────────────────────
Request
  │
  ├─ Rate limit  (Redis sliding window)        → 429 if exceeded
  ├─ ETag check                                → 304 if unchanged
  ├─ FilterParams.from_request(request)        parse all params
  ├─ Load fragments (brands/cats/subcats)      fragment cache
  ├─ FilterPipeline.run(base_qs, params)       all filters + sort
  ├─ Sort path:
  │     name_az   → natural_sort_pks() (PKs, then Case/When)
  │     discount   → DB ORDER BY
  │     explicit   → DB ORDER BY
  │     feed sorts → FeedEngine.rank_dicts() + two-tier cache
  ├─ Inject ConnectedService cards (UPGRADE-13)
  ├─ Inject per-user wishlist state            Redis → DB fallback
  ├─ Async view-log dispatch                   Celery → Redis INCR
  └─ Render + ETag / Cache-Control headers

Cache key namespace
───────────────────
  de:brands            fragment: active brand list
  de:cats              fragment: active category list
  de:subcats           fragment: active sub-category list
  de:total             fragment: total product count
  de:brand_cat_map     fragment: brand→[category slugs] map
  de:brand_subcat_map  fragment: brand→[subcat slugs] map
  de:base:<hash>       Tier-1 feed: full scored product list
  de:page:<hash>       Tier-2 feed: paginated slice
  wl:<user_pk>         per-user wishlist UUID set
  rl:ip:<ip>           rate limit counter (anonymous)
  rl:u:<user_pk>       rate limit counter (authenticated)
  lock:<key>           stampede guard lock
"""

from __future__ import annotations

# ═══════════════════════════════════════════════════════════════════
# ██  IMPORTS
# ═══════════════════════════════════════════════════════════════════

import hashlib
import json
import logging
import random
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from decimal import Decimal
from typing import Any
from urllib.parse import parse_qs, urlparse

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.core.cache import cache
from django.core.paginator import Paginator
from django.db.models import (
    Avg, Case, Count, DecimalField, ExpressionWrapper,
    F, IntegerField, Q, Sum, Value, When,
)
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, render
from django.utils import timezone
from django.utils.html import escape
from django.views.decorators.cache import cache_control
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_http_methods
from django.views.decorators.vary import vary_on_cookie

from rest_framework import serializers, status
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.viewsets import ModelViewSet

from apps.customer.models.profile_info import ProfileInfo
from apps.ponno.feed_algorithm import (
    FeedEngine,
    TAB_FILTER_SLUGS,
    get_tab_filter_map,
    invalidate_user_affinity,
)
from apps.ponno.filter_products import (
    EXPLICIT_SORT_BYPASSES,
    SORT_MAP,
    FilterParams,
    FilterPipeline,
    build_sort_options,
    build_tab_options,
    natural_sort_pks,
)
from apps.ponno.models.brand import Brand
from apps.ponno.models.category import Category
from apps.ponno.models.product import Order, Product, ProductView, SearchHistory, Wishlist
from apps.ponno.models.product_view_log import ProductViewLog
from apps.ponno.models.rating import ProductRating
from apps.ponno.models.sub_category import SubCategory
from apps.ponno.product_badges import attach_badges_to_products, resolve_badges_from_dict
from megamind.models.connected_service import ConnectedService
from megamind.services.scraper import scrape_url
from megamind.utils.service_fetcher import fetch_service_data

logger = logging.getLogger(__name__)
User   = get_user_model()


# ═══════════════════════════════════════════════════════════════════
# ██  TUNEABLE CONSTANTS
# ═══════════════════════════════════════════════════════════════════

PRODUCTS_PER_PAGE  = getattr(settings, 'DE_PRODUCTS_PER_PAGE',  20)
TTL_FEED_PAGE      = getattr(settings, 'DE_TTL_FEED_PAGE',       60)
TTL_FEED_BASE      = getattr(settings, 'DE_TTL_FEED_BASE',      120)
TTL_FRAGMENT_LIST  = getattr(settings, 'DE_TTL_FRAGMENT_LIST',  180)
TTL_TOTAL_COUNT    = getattr(settings, 'DE_TTL_TOTAL_COUNT',    120)
TTL_FILTER_BUTTONS = getattr(settings, 'DE_TTL_FILTER_BUTTONS', 120)
TTL_WISHLIST       = getattr(settings, 'DE_TTL_WISHLIST',       300)
TTL_LOCK           = getattr(settings, 'DE_TTL_LOCK',            10)

_RL_WINDOW_S    = 60
_RL_ANON_LIMIT  = 120
_RL_AUTH_LIMIT  = 600

_VIEWCNT_PREFIX  = 'de:viewcnt'
_WISHLIST_PREFIX = 'wl'

# Profile view cache TTLs
PROFILE_TTL  = 60 * 5   # 5 min
ACTIVITY_TTL = 60 * 3   # 3 min
RECENT_TTL   = 60 * 2   # 2 min
ENGINE_TTL   = 60 * 4   # 4 min
ETAG_TTL     = 60 * 2   # 2 min

# Columns the ORM fetches — keeps DB rows narrow
PRODUCT_FIELDS = [
    'pk', 'product_id', 'slug', 'sku',
    'product_title', 'product_name', 'short_description',
    'brand_price', 'selling_price', 'final_price', 'discount_percentage',
    'image', 'video_url', 'free_shipping',
    'view_count', 'rating_average', 'review_count', 'total_sales',
    'wishlist_count',
    'stock', 'stock_status', 'low_stock_threshold',
    'is_featured', 'is_verified', 'is_trending',
    'product_condition',
    'brand_id', 'category_id', 'sub_category_id', 'dealer_id',
    'created_at',
]

# Icon per ConnectedService.service_type for the badge stack
_SERVICE_TYPE_BADGE_ICON = {
    'product':   'fa-solid fa-bag-shopping',
    'business':  'fa-solid fa-store',
    'person':    'fa-solid fa-user',
    'location':  'fa-solid fa-location-dot',
    'news':      'fa-solid fa-newspaper',
    'education': 'fa-solid fa-graduation-cap',
    'api':       'fa-solid fa-plug',
}

# ConnectedService card injection settings
SERVICE_CARDS_LIMIT   = getattr(settings, 'DE_SERVICE_CARDS_LIMIT', 6)
SERVICE_CARD_INTERVAL = getattr(settings, 'DE_SERVICE_CARD_INTERVAL', 4)

# Thread pool for parallel profile data loaders
_MAX_WORKERS = getattr(settings, 'PROFILE_VIEW_THREAD_POOL_WORKERS', 4)
_EXECUTOR = ThreadPoolExecutor(max_workers=_MAX_WORKERS, thread_name_prefix="profile_loader")

# JS helper injected into every page (needed by wishlist / follow AJAX)
_GET_COOKIE_JS = """
<script>
function getCookie(name) {
  const v = document.cookie.match('(^|;)\\\\s*' + name + '\\\\s*=\\\\s*([^;]+)');
  return v ? v.pop() : '';
}
</script>
""".strip()

_DIGIT_SPLIT = re.compile(r'(\d+)')


# ═══════════════════════════════════════════════════════════════════
# ██  SAFE CACHE WRAPPERS  (graceful Redis degradation)
# ═══════════════════════════════════════════════════════════════════

def _safe_cache_get(key: str):
    try:
        return cache.get(key)
    except Exception as exc:
        logger.warning('cache.get failed key=%s err=%s', key, exc)
        return None


def _safe_cache_set(key: str, value, ttl: int) -> None:
    try:
        cache.set(key, value, ttl)
    except Exception as exc:
        logger.warning('cache.set failed key=%s err=%s', key, exc)


def _safe_cache_add(key: str, value, ttl: int) -> bool:
    try:
        return bool(cache.add(key, value, ttl))
    except Exception as exc:
        logger.warning('cache.add failed key=%s err=%s', key, exc)
        return False


def _safe_cache_delete(key: str) -> None:
    try:
        cache.delete(key)
    except Exception as exc:
        logger.warning('cache.delete failed key=%s err=%s', key, exc)


def _safe_cache_incr(key: str) -> None:
    try:
        cache.incr(key)
    except Exception:
        try:
            cache.set(key, 1, TTL_FEED_BASE)
        except Exception as exc:
            logger.warning('cache.incr/set failed key=%s err=%s', key, exc)


# ═══════════════════════════════════════════════════════════════════
# ██  CACHE KEY HELPERS
# ═══════════════════════════════════════════════════════════════════

def _ck(*parts) -> str:
    raw = ':'.join(str(p) for p in parts)
    return 'de:' + hashlib.blake2b(raw.encode(), digest_size=10).hexdigest()


def _feed_base_key(user_id, query, filter_slug, sort_by) -> str:
    return _ck('base', user_id, query, filter_slug, sort_by)


def _feed_page_key(user_id, query, filter_slug, sort_by,
                   page, min_p, max_p, in_stock) -> str:
    return _ck('page', user_id, query, filter_slug, sort_by,
               page, min_p, max_p, in_stock)


def _key_ctx(uid: int)      -> str: return f"profile:ctx:{uid}"
def _key_activity(uid: int) -> str: return f"profile:activity:{uid}"
def _key_recent(uid: int)   -> str: return f"profile:recent:{uid}"
def _key_engine(uid: int)   -> str: return f"profile:engine:{uid}"
def _key_etag(uid: int)     -> str: return f"profile:etag:{uid}"


def _jittered_ttl(ttl: int, spread: float = 0.10) -> int:
    delta = int(ttl * spread)
    return ttl + random.randint(-delta, delta) if delta else ttl


# ═══════════════════════════════════════════════════════════════════
# ██  STAMPEDE GUARD
# ═══════════════════════════════════════════════════════════════════

def _lock(key: str) -> bool:
    return _safe_cache_add(f'lock:{key}', 1, TTL_LOCK)


def _unlock(key: str) -> None:
    _safe_cache_delete(f'lock:{key}')


# ═══════════════════════════════════════════════════════════════════
# ██  RATE LIMITER
# ═══════════════════════════════════════════════════════════════════

def _check_rate_limit(request: HttpRequest) -> bool:
    """Sliding-window counter in Redis. Fails open when Redis is unavailable."""
    try:
        if request.user.is_authenticated:
            key   = f'rl:u:{request.user.pk}'
            limit = _RL_AUTH_LIMIT
        else:
            ip  = (
                request.META.get('HTTP_X_FORWARDED_FOR', '')
                            .split(',')[0].strip()
                or request.META.get('REMOTE_ADDR', 'unknown')
            )
            key   = f'rl:ip:{ip}'
            limit = _RL_ANON_LIMIT

        count = cache.get(key) or 0
        if count >= limit:
            return False
        cache.get_or_set(key, 0, _RL_WINDOW_S)
        cache.incr(key)
        return True
    except Exception as exc:
        logger.warning('rate_limit check failed: %s', exc)
        return True  # fail open


# ═══════════════════════════════════════════════════════════════════
# ██  ETAG / CACHE-CONTROL
# ═══════════════════════════════════════════════════════════════════

def _build_etag(products: list[dict], user_pk) -> str:
    ids = ','.join(str(p.get('uuid', p.get('id', ''))) for p in products)
    raw = f'{ids}:{user_pk}'
    return hashlib.md5(raw.encode(), usedforsecurity=False).hexdigest()  # noqa: S324


def _build_profile_etag(
    uid: int,
    ctx: dict[str, Any],
    activity: dict[str, Any],
    recently_viewed: list[dict[str, Any]],
    engine: dict[str, Any],
) -> str:
    ctx             = ctx or {}
    activity        = activity or {}
    recently_viewed = recently_viewed or []
    engine          = engine or {}
    fingerprint = {
        'uid':      uid,
        'pc':       ctx.get('profile_completion'),
        'fc':       ctx.get('followers_count'),
        'to':       activity.get('total_orders'),
        'rv_count': len(recently_viewed),
        'svc':      engine.get('images_count'),
    }
    raw = json.dumps(fingerprint, sort_keys=True, default=str)
    return hashlib.md5(raw.encode(), usedforsecurity=False).hexdigest()  # noqa: S324


def _set_cache_headers(response: HttpResponse, etag: str, user) -> None:
    response['ETag'] = f'"{etag}"'
    response['Vary'] = 'Accept-Encoding, X-Requested-With'
    if getattr(user, 'is_authenticated', False):
        response['Cache-Control'] = f'private, max-age={TTL_FEED_PAGE}'
    else:
        response['Cache-Control'] = (
            f'public, max-age=30, s-maxage={TTL_FEED_PAGE}, stale-while-revalidate=15'
        )


# ═══════════════════════════════════════════════════════════════════
# ██  MEDIA URL HELPER
# ═══════════════════════════════════════════════════════════════════

def _media(path: str | None) -> str | None:
    """
    Turn a bare FileField path (e.g. 'products/img.jpg') into a
    root-relative URL ('/media/products/img.jpg').
    Already-absolute paths (http/https) are returned unchanged.
    None / empty → None.
    """
    if not path:
        return None
    s = str(path)
    if s.startswith(('http://', 'https://', '/', 'data:')):
        return s
    media_url = getattr(settings, 'MEDIA_URL', '/media/')
    if not media_url.endswith('/'):
        media_url += '/'
    return media_url + s.lstrip('/')


# ═══════════════════════════════════════════════════════════════════
# ██  FORMATTING HELPERS
# ═══════════════════════════════════════════════════════════════════

def format_count(count) -> str:
    try:
        n = int(count)
        if n >= 1_000_000: return f'{n / 1_000_000:.1f}M'.replace('.0M', 'M')
        if n >= 1_000:     return f'{n / 1_000:.1f}k'.replace('.0k', 'k')
        return str(n)
    except (ValueError, TypeError):
        return '0'


def format_views(views) -> str:
    try:
        n = int(views)
        if n >= 1_000_000: return f'{n / 1_000_000:.1f}M'.replace('.0M', 'M')
        if n >= 1_000:     return f'{n / 1_000:.1f}K'.replace('.0K', 'K')
        return str(n)
    except (ValueError, TypeError):
        return '0'


def format_price(price) -> str:
    try:
        if price is None:
            return '0 Tk'
        d = Decimal(str(price))

        def bd_format(n: int) -> str:
            s = str(n)
            if len(s) <= 3:
                return s
            result = s[-3:]
            s = s[:-3]
            while s:
                result = s[-2:] + ',' + result
                s = s[:-2]
            return result

        if d == d.to_integral_value():
            return f'{bd_format(int(d))} Tk'

        integer_part = int(d)
        decimal_part = f'{d:.2f}'.split('.')[1]
        return f'{bd_format(integer_part)}.{decimal_part} Tk'

    except (ValueError, TypeError):
        return '0 Tk'


def _format_age(days: float) -> str:
    total_seconds = round(days * 86400)
    if total_seconds < 5:    return 'Just now'
    if total_seconds < 60:   return f'{total_seconds}s ago'
    minutes = total_seconds // 60
    if minutes < 60:         return f'{minutes}m ago'
    hours = minutes // 60
    if hours < 24:           return f'{hours}h ago'
    d = hours // 24
    if d == 1:               return 'Yesterday'
    if d < 7:                return f'{d}d ago'
    weeks = d // 7
    if weeks < 5:            return f'{weeks}w ago'
    months = d // 30
    if months < 12:          return f'{months}mo ago'
    years  = d // 365
    rem    = (d % 365) // 30
    return f'{years}y {rem}mo ago' if rem else f'{years}y ago'


# ═══════════════════════════════════════════════════════════════════
# ██  VIDEO INFO HELPER
# ═══════════════════════════════════════════════════════════════════

# How long to cache a parsed video_info dict per URL.
# URLs are stable (they come from the DB), so a long TTL is fine.
_VIDEO_INFO_TTL = getattr(settings, 'DE_VIDEO_INFO_TTL', 3600)


def _get_video_info(url: str) -> dict:
    """
    Parse a raw video URL and return a platform-normalised dict that
    the template can use to render an <iframe> or <video> element.

    Supported platforms: YouTube, Vimeo, Dailymotion, Rumble,
    Streamable, Twitch (channel + clip), Facebook, TikTok, Twitter/X,
    and any direct video file (.mp4, .webm, .ogg, .mov, .m4v, .mkv, .avi).

    Always returns a dict (never None / raises).  Empty URL → {}.

    Returned keys
    -------------
    platform   str   youtube | vimeo | dailymotion | rumble | streamable |
                     twitch | twitch_clip | facebook | tiktok | twitter |
                     direct | unknown
    embed_url  str   URL suitable for an <iframe src="...">
    watch_url  str   Human-facing watch/share link
    thumbnail  str   Preview image URL (empty string when unavailable)
    type       str   "iframe" | "video"  — tells the template which tag to use
    mime_type  str   Only present when type == "video"
    """
    if not url:
        return {}

    url      = url.strip()
    parsed   = urlparse(url)
    hostname = parsed.netloc.lower().replace('www.', '')

    # ── YouTube ───────────────────────────────────────────────────
    if hostname in ('youtube.com', 'youtu.be', 'm.youtube.com', 'music.youtube.com'):
        vid_id = None
        if hostname == 'youtu.be':
            vid_id = parsed.path.lstrip('/').split('/')[0]
        elif '/shorts/' in parsed.path:
            vid_id = parsed.path.split('/shorts/')[1].split('/')[0]
        elif '/live/' in parsed.path:
            vid_id = parsed.path.split('/live/')[1].split('/')[0]
        elif '/embed/' in parsed.path:
            vid_id = parsed.path.split('/embed/')[1].split('/')[0]
        else:
            vid_id = parse_qs(parsed.query).get('v', [None])[0]
        if vid_id:
            vid_id = re.sub(r'[^a-zA-Z0-9_-]', '', vid_id)
            return {
                'platform':  'youtube',
                'embed_url': f'https://www.youtube.com/embed/{vid_id}?rel=0&modestbranding=1',
                'watch_url': f'https://www.youtube.com/watch?v={vid_id}',
                'thumbnail': f'https://img.youtube.com/vi/{vid_id}/hqdefault.jpg',
                'type':      'iframe',
            }

    # ── Vimeo ─────────────────────────────────────────────────────
    if hostname in ('vimeo.com', 'player.vimeo.com'):
        vid_id = (
            parsed.path.split('/video/')[1].split('/')[0]
            if '/video/' in parsed.path
            else parsed.path.lstrip('/').split('/')[0]
        )
        vid_id = re.sub(r'[^0-9]', '', vid_id)
        if vid_id:
            return {
                'platform':  'vimeo',
                'embed_url': f'https://player.vimeo.com/video/{vid_id}?badge=0&autopause=0',
                'watch_url': f'https://vimeo.com/{vid_id}',
                'thumbnail': '',
                'type':      'iframe',
            }

    # ── Dailymotion ───────────────────────────────────────────────
    if hostname in ('dailymotion.com', 'dai.ly'):
        if hostname == 'dai.ly':
            vid_id = parsed.path.lstrip('/').split('/')[0]
        elif '/video/' in parsed.path:
            vid_id = parsed.path.split('/video/')[1].split('_')[0].split('/')[0]
        else:
            vid_id = parsed.path.lstrip('/').split('/')[0]
        vid_id = re.sub(r'[^a-zA-Z0-9]', '', vid_id)
        if vid_id:
            return {
                'platform':  'dailymotion',
                'embed_url': f'https://www.dailymotion.com/embed/video/{vid_id}',
                'watch_url': f'https://www.dailymotion.com/video/{vid_id}',
                'thumbnail': f'https://www.dailymotion.com/thumbnail/video/{vid_id}',
                'type':      'iframe',
            }

    # ── Rumble ────────────────────────────────────────────────────
    if hostname == 'rumble.com':
        m = re.search(r'rumble\.com/embed/([^/?&]+)', url)
        vid_id = m.group(1) if m else None
        if not vid_id:
            m = re.search(r'rumble\.com/([^/?&]+)', url)
            vid_id = m.group(1) if m else None
        if vid_id:
            return {
                'platform':  'rumble',
                'embed_url': f'https://rumble.com/embed/{vid_id}/',
                'watch_url': url,
                'thumbnail': '',
                'type':      'iframe',
            }

    # ── Streamable ────────────────────────────────────────────────
    if hostname == 'streamable.com':
        vid_id = parsed.path.lstrip('/').split('/')[0]
        if vid_id:
            return {
                'platform':  'streamable',
                'embed_url': f'https://streamable.com/e/{vid_id}',
                'watch_url': url,
                'thumbnail': '',
                'type':      'iframe',
            }

    # ── Twitch ────────────────────────────────────────────────────
    if hostname in ('twitch.tv', 'clips.twitch.tv'):
        if '/clip/' in parsed.path or hostname == 'clips.twitch.tv':
            clip_id = parsed.path.lstrip('/').split('/')[-1]
            return {
                'platform':  'twitch_clip',
                'embed_url': f'https://clips.twitch.tv/embed?clip={clip_id}&parent={parsed.hostname}',
                'watch_url': url,
                'thumbnail': '',
                'type':      'iframe',
            }
        channel = parsed.path.lstrip('/').split('/')[0]
        return {
            'platform':  'twitch',
            'embed_url': f'https://player.twitch.tv/?channel={channel}&parent=yourdomain.com',
            'watch_url': url,
            'thumbnail': '',
            'type':      'iframe',
        }

    # ── Facebook ──────────────────────────────────────────────────
    if hostname in ('facebook.com', 'fb.watch', 'fb.com'):
        return {
            'platform':  'facebook',
            'embed_url': f'https://www.facebook.com/plugins/video.php?href={url}&show_text=false&width=560',
            'watch_url': url,
            'thumbnail': '',
            'type':      'iframe',
        }

    # ── TikTok ────────────────────────────────────────────────────
    if hostname in ('tiktok.com', 'vm.tiktok.com'):
        m = re.search(r'/video/(\d+)', parsed.path)
        if m:
            return {
                'platform':  'tiktok',
                'embed_url': f'https://www.tiktok.com/embed/v2/{m.group(1)}',
                'watch_url': url,
                'thumbnail': '',
                'type':      'iframe',
            }

    # ── Twitter / X ───────────────────────────────────────────────
    if hostname in ('twitter.com', 'x.com', 't.co'):
        return {
            'platform':  'twitter',
            'embed_url': f'https://platform.twitter.com/embed/Tweet.html?id={parsed.path.split("/")[-1]}',
            'watch_url': url,
            'thumbnail': '',
            'type':      'iframe',
        }

    # ── Direct video file ─────────────────────────────────────────
    _VIDEO_EXTS = ('.mp4', '.webm', '.ogg', '.mov', '.m4v', '.mkv', '.avi')
    if any(parsed.path.lower().endswith(ext) for ext in _VIDEO_EXTS):
        ext = parsed.path.lower().rsplit('.', 1)[-1]
        mime_map = {
            'mp4': 'video/mp4', 'webm': 'video/webm', 'ogg': 'video/ogg',
            'mov': 'video/mp4', 'm4v': 'video/mp4',
            'mkv': 'video/x-matroska', 'avi': 'video/x-msvideo',
        }
        return {
            'platform':  'direct',
            'embed_url': url,
            'watch_url': url,
            'thumbnail': '',
            'type':      'video',
            'mime_type': mime_map.get(ext, 'video/mp4'),
        }

    # ── Unknown / fallback ────────────────────────────────────────
    return {
        'platform':  'unknown',
        'embed_url': url,
        'watch_url': url,
        'thumbnail': '',
        'type':      'iframe',
    }


def _get_video_info_cached(url: str | None) -> dict:
    """
    Thin cache wrapper around _get_video_info().
    Cache key: de:vi:<blake2b of url>  TTL: _VIDEO_INFO_TTL (default 1 h).
    Returns {} immediately for falsy URLs — no cache I/O.
    """
    if not url:
        return {}
    key    = 'de:vi:' + hashlib.blake2b(url.encode(), digest_size=10).hexdigest()
    cached = _safe_cache_get(key)
    if cached is not None:
        return cached
    info = _get_video_info(url)
    if info:
        _safe_cache_set(key, info, _VIDEO_INFO_TTL)
    return info


# ═══════════════════════════════════════════════════════════════════
# ██  COERCION HELPERS  (handles both dict and bare-string payloads)
# ═══════════════════════════════════════════════════════════════════

def _coerce_image(img) -> dict:
    if isinstance(img, dict):
        return {
            'url': img.get('url') or img.get('src') or '',
            'alt': img.get('alt') or '',
            'href': img.get('href') or '',
        }
    return {'url': str(img), 'alt': '', 'href': ''}


def _coerce_video(v) -> dict:
    if isinstance(v, dict):
        return {
            'url':  v.get('url') or v.get('src') or '',
            'type': v.get('type') or '',
        }
    return {'url': str(v), 'type': ''}


def _coerce_link(lnk) -> dict:
    if isinstance(lnk, dict):
        return {
            'href': lnk.get('href') or lnk.get('url')   or '',
            'text': lnk.get('text') or lnk.get('title') or '',
        }
    return {'href': str(lnk), 'text': ''}


# ═══════════════════════════════════════════════════════════════════
# ██  FRAGMENT CACHE HELPERS
# ═══════════════════════════════════════════════════════════════════

def _get_active_brands() -> list[dict]:
    key  = 'de:brands'
    data = _safe_cache_get(key)
    if data is not None:
        return data

    got_lock = _lock(key)
    if not got_lock:
        return _safe_cache_get(key) or []

    try:
        qs = (
            Brand.objects
            .active_brands()
            .annotate(
                live_product_count=Count(
                    'products',
                    filter=Q(products__is_active=True, products__deleted_at__isnull=True),
                    distinct=True,
                ),
                live_category_count=Count(
                    'products__category',
                    filter=Q(products__is_active=True, products__deleted_at__isnull=True),
                    distinct=True,
                ),
            )
            .values('pk', 'brand_name', 'brand_slug', 'brand_logo',
                    'is_featured', 'live_product_count', 'live_category_count')
            .order_by('-live_product_count')
        )
        data = []
        for row in qs:
            lp = row['brand_logo']
            data.append({
                'pk':             row['pk'],
                'brand_name':     row['brand_name'],
                'brand_slug':     row['brand_slug'],
                'brand_logo':     lp,
                'is_featured':    row['is_featured'],
                'product_count':  row['live_product_count'],
                'category_count': row['live_category_count'],
                'brand_logo_url': _media(lp),
            })
        _safe_cache_set(key, data, TTL_FRAGMENT_LIST)
        return data
    finally:
        _unlock(key)


def _get_active_categories() -> list[dict]:
    key  = 'de:cats'
    data = _safe_cache_get(key)
    if data is not None:
        return data

    got_lock = _lock(key)
    if not got_lock:
        return _safe_cache_get(key) or []

    try:
        qs = (
            Category.objects
            .active_categories()
            .annotate(
                live_product_count=Count(
                    'products',
                    filter=Q(products__is_active=True, products__deleted_at__isnull=True),
                    distinct=True,
                )
            )
            .values('pk', 'category_name', 'category_slug', 'is_featured',
                    'path', 'level', 'live_product_count')
            .order_by('-live_product_count')
        )
        data = [
            {
                'pk':            r['pk'],
                'category_name': r['category_name'],
                'category_slug': r['category_slug'],
                'is_featured':   r['is_featured'],
                'path':          r['path'],
                'level':         r['level'],
                'product_count': r['live_product_count'],
            }
            for r in qs
        ]
        _safe_cache_set(key, data, TTL_FRAGMENT_LIST)
        return data
    finally:
        _unlock(key)


def _get_active_sub_categories() -> list[dict]:
    key  = 'de:subcats'
    data = _safe_cache_get(key)
    if data is not None:
        return data

    got_lock = _lock(key)
    if not got_lock:
        return _safe_cache_get(key) or []

    try:
        qs = (
            SubCategory.objects
            .active()
            .select_related('category')
            .annotate(
                live_product_count=Count(
                    'products',
                    filter=Q(products__is_active=True, products__deleted_at__isnull=True),
                    distinct=True,
                )
            )
            .values(
                'pk', 'sub_category_name', 'sub_category_slug', 'is_featured',
                'category_id', 'category__category_name', 'category__category_slug',
                'live_product_count',
            )
            .order_by('category__category_name', 'display_order', 'sub_category_name')
        )
        data = [
            {
                'pk':                      r['pk'],
                'sub_category_name':       r['sub_category_name'],
                'sub_category_slug':       r['sub_category_slug'],
                'is_featured':             r['is_featured'],
                'category_id':             r['category_id'],
                'category__category_name': r['category__category_name'],
                'category__category_slug': r['category__category_slug'],
                'product_count':           r['live_product_count'],
            }
            for r in qs
        ]
        _safe_cache_set(key, data, TTL_FRAGMENT_LIST)
        return data
    finally:
        _unlock(key)


def _get_brand_category_map() -> dict[str, list]:
    key  = 'de:brand_cat_map'
    data = _safe_cache_get(key)
    if data is not None:
        return data

    got_lock = _lock(key)
    if not got_lock:
        return _safe_cache_get(key) or {}

    try:
        pairs = (
            Product.objects.active_products()
            .filter(brand__brand_slug__isnull=False, category__category_slug__isnull=False)
            .values('brand__brand_slug', 'category__category_slug')
            .distinct()
        )
        result: dict[str, list] = {}
        for row in pairs:
            result.setdefault(row['brand__brand_slug'], []).append(row['category__category_slug'])
        _safe_cache_set(key, result, TTL_FRAGMENT_LIST)
        return result
    finally:
        _unlock(key)


def _get_brand_subcategory_map() -> dict[str, list]:
    key  = 'de:brand_subcat_map'
    data = _safe_cache_get(key)
    if data is not None:
        return data

    got_lock = _lock(key)
    if not got_lock:
        return _safe_cache_get(key) or {}

    try:
        pairs = (
            Product.objects.active_products()
            .filter(
                brand__brand_slug__isnull=False,
                sub_category__sub_category_slug__isnull=False,
            )
            .values('brand__brand_slug', 'sub_category__sub_category_slug')
            .distinct()
        )
        result: dict[str, list] = {}
        for row in pairs:
            result.setdefault(row['brand__brand_slug'], []).append(
                row['sub_category__sub_category_slug']
            )
        _safe_cache_set(key, result, TTL_FRAGMENT_LIST)
        return result
    finally:
        _unlock(key)


def _get_total_product_count() -> int:
    key   = 'de:total'
    count = _safe_cache_get(key)
    if count is not None:
        return count

    got_lock = _lock(key)
    if not got_lock:
        return _safe_cache_get(key) or 0

    try:
        count = Product.objects.active_products().count()
        _safe_cache_set(key, count, TTL_TOTAL_COUNT)
        return count
    finally:
        _unlock(key)


# ═══════════════════════════════════════════════════════════════════
# ██  ASYNC VIEW-LOG DISPATCH
# ═══════════════════════════════════════════════════════════════════

def _dispatch_view_logs(product_db_ids: list[int], user_pk) -> None:
    """Celery primary, Redis INCR fallback."""
    if not product_db_ids:
        return
    try:
        from apps.ponno.tasks import record_product_views_task  # noqa: PLC0415
        record_product_views_task.delay(product_db_ids, user_pk)
    except Exception as exc:
        logger.debug('Celery unavailable for view logging: %s', exc)
        for pid in product_db_ids:
            _safe_cache_incr(f'{_VIEWCNT_PREFIX}:{pid}')


# ═══════════════════════════════════════════════════════════════════
# ██  WISHLIST CACHE
# ═══════════════════════════════════════════════════════════════════

def _get_wishlisted_ids(user) -> set[str]:
    """Per-user wishlist UUID set. Redis-cached; DB fallback on cold miss."""
    if not user or not getattr(user, 'is_authenticated', False):
        return set()

    key    = f'{_WISHLIST_PREFIX}:{user.pk}'
    cached = _safe_cache_get(key)
    if cached is not None:
        return cached

    try:
        ids = set(
            str(uid)
            for uid in user.wishlist_items
                              .values_list('product__product_id', flat=True)
        )
    except Exception:
        ids = set()

    _safe_cache_set(key, ids, TTL_WISHLIST)
    return ids


def invalidate_wishlist_cache(user_pk: int) -> None:
    _safe_cache_delete(f'{_WISHLIST_PREFIX}:{user_pk}')


# ═══════════════════════════════════════════════════════════════════
# ██  BATCH DEALER PRODUCT COUNTS
# ═══════════════════════════════════════════════════════════════════

def _batch_dealer_product_counts(product_list: list) -> dict[int, int]:
    dealer_ids = {p.dealer_id for p in product_list if p.dealer_id}
    if not dealer_ids:
        return {}
    rows = (
        Product.objects
        .filter(dealer_id__in=dealer_ids, is_active=True)
        .values('dealer_id')
        .annotate(cnt=Count('pk'))
    )
    return {row['dealer_id']: row['cnt'] for row in rows}


# ═══════════════════════════════════════════════════════════════════
# ██  SHARED SELLER/DEALER RESOLVER
# ═══════════════════════════════════════════════════════════════════

def _resolve_seller_info(user) -> dict:
    """
    Shared seller/dealer info resolver used by BOTH Product cards and
    ConnectedService cards, so both item types render an identical
    dealer strip in the template.
    """
    info = {
        'seller_name':        'Seller',
        'seller_photo':       '/static/defaults/default-profile-picture.png',
        'seller_verified':    False,
        'seller_profile_url': '#',
        'seller_views':       0,
        'seller_followers':   0,
        'seller_username':    '',
    }
    if not user:
        return info

    info['seller_username'] = getattr(user, 'email_or_phone', '') or ''

    try:
        profile = user.profileinfo
        info['seller_name'] = (
            getattr(profile, 'full_name', None)
            or getattr(profile, 'profile_name', None)
            or info['seller_username']
            or 'Seller'
        )
        raw_photo = None
        if hasattr(profile, 'get_profile_photo_url'):
            raw_photo = profile.get_profile_photo_url()
        elif getattr(profile, 'profile_photo', None):
            raw_photo = _media(str(profile.profile_photo))
        info['seller_photo']       = raw_photo or info['seller_photo']
        info['seller_verified']    = getattr(profile, 'is_verified', False)
        info['seller_profile_url'] = getattr(profile, 'profile_url', '#') or '#'
        info['seller_views']       = getattr(profile, 'profile_views', 0) or 0
        info['seller_followers']   = getattr(profile, 'follower_count', 0) or 0
    except Exception:
        info['seller_name'] = info['seller_username'] or 'Seller'

    return info


# ═══════════════════════════════════════════════════════════════════
# ██  PRODUCT FORMATTER
# ═══════════════════════════════════════════════════════════════════

def _format_products(
    product_list: list,
    query: str = '',
    dealer_counts: dict | None = None,
) -> list[dict]:
    """
    Convert ORM Product instances into template-ready dicts.
    Badge resolution delegated to attach_badges_to_products().
    Seller info delegated to the shared _resolve_seller_info() helper.
    """
    if dealer_counts is None:
        dealer_counts = {}

    keywords = [w for w in query.split() if w][:10]

    def highlight(text: str) -> str:
        """XSS-safe: escape first, then wrap with <mark>."""
        safe = escape(text or '')
        if not keywords:
            return safe
        for word in keywords:
            pattern = re.compile(re.escape(escape(word)), re.IGNORECASE)
            safe    = pattern.sub(
                lambda m: f'<mark class="highlight">{m.group()}</mark>',
                safe,
            )
        return safe

    formatted = []

    for product in product_list:
        seller_info           = _resolve_seller_info(product.dealer)
        seller_total_products = dealer_counts.get(product.dealer_id, 0)

        # ── Stock ──────────────────────────────────────────────────
        stock        = product.stock or 0
        threshold    = product.low_stock_threshold or 10
        is_in_stock  = stock > 0
        is_low_stock = 0 < stock <= threshold

        if stock == 0:
            stock_badge  = 'Out of Stock'
            stock_class  = 'out-of-stock'
            stock_status = 'Out of Stock'
        elif is_low_stock:
            stock_badge  = f'Only {stock} left'
            stock_class  = 'low-stock'
            stock_status = 'Low Stock'
        else:
            stock_badge  = None
            stock_class  = 'in-stock'
            stock_status = 'In Stock'

        # ── Pricing ────────────────────────────────────────────────
        discount_pct   = float(product.discount_percentage or 0)
        discount_badge = f'{int(discount_pct)}% OFF' if discount_pct > 0 else None
        final_price    = product.final_price or product.selling_price

        # ── Relations ─────────────────────────────────────────────
        brand    = product.brand
        category = product.category
        sub_cat  = product.sub_category

        brand_logo_url = None
        if brand:
            raw_logo = None
            if hasattr(brand, 'logo_url'):
                raw_logo = brand.logo_url
            elif hasattr(brand, 'brand_logo') and brand.brand_logo:
                raw_logo = _media(str(brand.brand_logo))
            brand_logo_url = raw_logo or None

        product_image_url = None
        if hasattr(product, 'image_url'):
            product_image_url = product.image_url
        elif product.image:
            product_image_url = _media(str(product.image))
        product_image_url = product_image_url or '/static/defaults/default-product.png'

        rating_int = int(float(product.rating_average or 0))

        entry = {
            # Identity
            'id':   product.pk,
            'uuid': str(product.product_id),
            'slug': product.slug,
            'sku':  product.sku,

            # Display names
            'title':                    product.product_title or product.product_name or '',
            'product_name':             product.product_name or '',
            'highlighted_title':        highlight(product.product_title or product.product_name or ''),
            'highlighted_product_name': highlight(product.product_name or ''),
            'short_description':        product.short_description or '',

            # Pricing
            'brand_price':         format_price(product.brand_price) if product.brand_price else None,
            'original_price':      format_price(product.selling_price),
            'final_price':         format_price(final_price),
            'discount_percentage': discount_pct,
            'discount_pct':        discount_pct,
            'discount_badge':      discount_badge,
            'on_sale':             discount_pct > 0,

            # Media
            'image':      product_image_url,
            'video_url':  product.video_url,
            'video_info': _get_video_info_cached(product.video_url),

            # Seller — shared resolver
            **seller_info,
            'seller_views':          format_views(seller_info['seller_views']),
            'seller_followers':      format_count(seller_info['seller_followers']),
            'seller_total_products': seller_total_products,

            # Analytics
            'views':          format_views(product.view_count),
            'views_raw':      product.view_count or 0,
            'rating':         rating_int,
            'rating_raw':     float(product.rating_average or 0),
            'review_count':   product.review_count   or 0,
            'total_sales':    product.total_sales     or 0,
            'wishlist_count': product.wishlist_count  or 0,

            # Stock
            'stock':        stock,
            'stock_count':  format_count(stock),
            'stock_status': stock_status,
            'stock_badge':  stock_badge,
            'stock_class':  stock_class,
            'in_stock':     is_in_stock,
            'is_low_stock': is_low_stock,

            # Classification
            'brand':             brand.brand_name        if brand    else 'No Brand',
            'brand_slug':        brand.brand_slug        if brand    else None,
            'brand_logo':        brand_logo_url,
            'brand_is_verified': getattr(brand, 'is_verified', False) if brand else False,
            'category':          category.category_name  if category else 'Uncategorized',
            'category_slug':     category.category_slug  if category else None,
            'sub_category':      sub_cat.sub_category_name if sub_cat else None,
            'sub_category_slug': sub_cat.sub_category_slug if sub_cat else None,

            # Status flags
            'is_featured':   product.is_featured,
            'is_verified':   product.is_verified,
            'is_trending':   product.is_trending,
            'free_shipping': product.free_shipping,
            'condition':     product.get_product_condition_display(),

            # URLs
            'product_url': product.product_url,

            # Feed scoring inputs
            'created_at': product.created_at,
            'dealer_id':  product.dealer_id,

            'age_days': _format_age(
                (timezone.now() - product.created_at).total_seconds() / 86400
            ) if product.created_at else '?',

            # Discriminator
            'item_type': 'product',
        }

        formatted.append(entry)

    attach_badges_to_products(formatted)
    return formatted


# ═══════════════════════════════════════════════════════════════════
# ██  CONNECTED-SERVICE → CARD ADAPTER
# ═══════════════════════════════════════════════════════════════════

def _format_connected_service_items(
    query: str = '',
    limit: int = SERVICE_CARDS_LIMIT,
    exclude_user_ids: set | None = None,
) -> list[dict]:
    """
    Convert public, successfully-fetched ConnectedService rows into
    dicts shaped like _format_products() output, so they render inside
    the same .product-card grid without template branching (beyond the
    link target — 'item_type' == 'service').

    Eligibility:
      - status == 'public'
      - is_connected == True
      - fetch_status == 'success'
      - has at least one extracted_image
    """
    exclude_user_ids = exclude_user_ids or set()

    qs = (
        ConnectedService.objects
        .filter(status='public', is_connected=True, fetch_status='success')
        .exclude(user_id__in=exclude_user_ids)
        .select_related('user', 'user__profileinfo')
        .order_by('-updated_at')
    )

    if query:
        qs = qs.filter(
            Q(service_name__icontains=query) |
            Q(og_title__icontains=query) |
            Q(og_description__icontains=query) |
            Q(extracted_text__icontains=query)
        )

    qs = qs[: max(limit * 3, limit)]  # overfetch; some rows will lack images

    keywords = [w for w in query.split() if w][:10]

    def highlight(text: str) -> str:
        safe = escape(text or '')
        for word in keywords:
            pattern = re.compile(re.escape(escape(word)), re.IGNORECASE)
            safe = pattern.sub(
                lambda m: f'<mark class="highlight">{m.group()}</mark>', safe
            )
        return safe

    formatted: list[dict] = []

    for svc in qs:
        images = svc.extracted_images or []
        if not images:
            continue

        lead_img  = images[0] if isinstance(images[0], dict) else {'url': str(images[0])}
        image_url = lead_img.get('url') or svc.og_thumbnail or None
        if not image_url:
            continue

        seller_info = _resolve_seller_info(svc.user)
        title       = svc.og_title or svc.service_name or 'Untitled'
        description = svc.og_description or ''
        badge_icon  = _SERVICE_TYPE_BADGE_ICON.get(svc.service_type, 'fa-solid fa-link')

        videos    = svc.extracted_videos or []
        video_url = videos[0].get('url') if videos and isinstance(videos[0], dict) else None

        entry = {
            # Identity
            'id':   f'svc-{svc.pk}',
            'uuid': f'svc-{svc.pk}',
            'slug': None,
            'sku':  None,

            # Display names
            'title':                    title,
            'product_name':             title,
            'highlighted_title':        highlight(title),
            'highlighted_product_name': highlight(title),
            'short_description':        description[:160],

            # Pricing — inert for services
            'brand_price':         None,
            'original_price':      None,
            'final_price':         None,
            'discount_percentage': 0,
            'discount_pct':        0,
            'discount_badge':      None,
            'on_sale':             False,

            # Media
            'image':      image_url,
            'video_url':  video_url,
            'video_info': _get_video_info_cached(video_url),

            # Seller — shared resolver
            **seller_info,
            'seller_total_products': 0,

            # Analytics — no product analytics for services
            'views':          '0',
            'views_raw':      0,
            'rating':         0,
            'rating_raw':     0.0,
            'review_count':   0,
            'total_sales':    0,
            'wishlist_count': 0,

            # Stock — neutralized so stock badges never fire
            'stock':        0,
            'stock_count':  '0',
            'stock_status': '',
            'stock_badge':  None,
            'stock_class':  '',
            'in_stock':     True,
            'is_low_stock': False,

            # Classification
            'brand':             svc.og_site_name or svc.service_name,
            'brand_slug':        None,
            'brand_logo':        None,
            'brand_is_verified': False,
            'category':          svc.get_service_type_display(),
            'category_slug':     svc.service_type,
            'sub_category':      None,
            'sub_category_slug': None,

            # Status flags
            'is_featured':   False,
            'is_verified':   False,
            'is_trending':   False,
            'free_shipping': False,
            'condition':     '',

            # URL — points at the external source
            'product_url': svc.service_url,

            'created_at': svc.updated_at,
            'dealer_id':  svc.user_id,

            'age_days': _format_age(
                (timezone.now() - svc.updated_at).total_seconds() / 86400
            ) if svc.updated_at else '?',

            # Discriminators
            'item_type':     'service',
            'service_id':    svc.pk,
            'service_type':  svc.service_type,
            'service_icon':  badge_icon,
            'source_domain': svc.og_site_name or '',
        }

        formatted.append(entry)
        if len(formatted) >= limit:
            break

    attach_badges_to_products(formatted)
    return formatted


def _interleave_service_cards(
    formatted_products: list[dict],
    service_items: list[dict],
    interval: int = SERVICE_CARD_INTERVAL,
) -> list[dict]:
    """Insert one service card every `interval` product slots."""
    if not service_items:
        return formatted_products

    merged: list[dict] = []
    si = iter(service_items)
    for idx, p in enumerate(formatted_products, start=1):
        merged.append(p)
        if idx % interval == 0:
            nxt = next(si, None)
            if nxt:
                merged.append(nxt)
    merged.extend(si)  # any leftover service items go at the end
    return merged


# ═══════════════════════════════════════════════════════════════════
# ██  NULL PAGE OBJECT
# ═══════════════════════════════════════════════════════════════════

class _NullPage:
    """
    Returned as page_obj when the product list is empty so that the
    inline JS in the template never sees an undefined variable:
        const totalPages = {{ page_obj.paginator.num_pages }};
        let page = {{ page_obj.number }};
    """
    number = 1

    class paginator:
        num_pages = 1

    def __bool__(self):
        return False


_NULL_PAGE = _NullPage()


# ═══════════════════════════════════════════════════════════════════
# ██  TWO-TIER SCORED FEED CACHE
# ═══════════════════════════════════════════════════════════════════

def _get_scored_feed(
    products_qs,
    user,
    params: FilterParams,
) -> tuple[object, int]:
    """
    Tier-2 (page slice) → Tier-1 (full scored list) → DB + FeedEngine.
    Returns (page_obj, total_count).
    page_obj.object_list contains formatted product dicts.
    """
    # Never use cache for search queries — always fetch fresh
    if params.query:
        raw_list      = list(products_qs.order_by(*SORT_MAP.get(params.sort_by, ['-created_at'])))
        dealer_counts = _batch_dealer_product_counts(raw_list)
        formatted     = _format_products(raw_list, params.query, dealer_counts)
        engine        = FeedEngine(user)
        scored_list   = engine.rank_dicts(formatted, diversify=True)
        paginator     = Paginator(scored_list, PRODUCTS_PER_PAGE)
        safe_page     = min(params.page, paginator.num_pages) if paginator.num_pages else 1
        return paginator.get_page(safe_page), len(scored_list)
    
    user_id  = user.pk if (user and user.is_authenticated) else 0
    base_key = _feed_base_key(user_id, params.query, params.filter_slug, params.sort_by)
    page_key = _feed_page_key(
        user_id, params.query, params.filter_slug, params.sort_by,
        params.page, params.min_price, params.max_price, params.in_stock,
    )

    cached_page = _safe_cache_get(page_key)
    if cached_page is not None:
        if isinstance(cached_page, tuple):
            return cached_page
        _safe_cache_delete(page_key)  # evict stale legacy format

    scored_list = _safe_cache_get(base_key)

    if scored_list is None:
        got_lock = _lock(base_key)
        if not got_lock:
            time.sleep(0.06)
            scored_list = _safe_cache_get(base_key)

        if scored_list is None and got_lock:
            try:
                raw_list      = list(products_qs.order_by(*SORT_MAP.get(params.sort_by, ['-created_at'])))
                dealer_counts = _batch_dealer_product_counts(raw_list)
                formatted     = _format_products(raw_list, params.query, dealer_counts)
                engine        = FeedEngine(user)
                scored_list   = engine.rank_dicts(formatted, diversify=True)
                _safe_cache_set(base_key, scored_list, TTL_FEED_BASE)
            finally:
                _unlock(base_key)

    scored_list = scored_list or []
    total_count = len(scored_list)

    paginator = Paginator(scored_list, PRODUCTS_PER_PAGE)
    safe_page = min(params.page, paginator.num_pages) if paginator.num_pages else 1
    page_obj  = paginator.get_page(safe_page)

    _safe_cache_set(page_key, (page_obj, total_count), TTL_FEED_PAGE)
    return page_obj, total_count


# ═══════════════════════════════════════════════════════════════════
# ██  PAGINATE + FORMAT HELPER
# ═══════════════════════════════════════════════════════════════════

def _paginate_and_format(
    products_qs,
    page_number: int,
    query: str,
) -> tuple[int, object, list[dict]]:
    """Returns (total_count, page_obj, formatted_products)."""
    total_count = products_qs.count()
    paginator   = Paginator(products_qs, PRODUCTS_PER_PAGE)
    safe_page   = min(page_number, paginator.num_pages) if paginator.num_pages else 1
    page_obj    = paginator.get_page(safe_page)

    raw_list      = list(page_obj.object_list)
    dealer_counts = _batch_dealer_product_counts(raw_list)
    formatted     = _format_products(raw_list, query, dealer_counts)
    return total_count, page_obj, formatted


# ═══════════════════════════════════════════════════════════════════
# ██  PROFILE VIEW LOG
# ═══════════════════════════════════════════════════════════════════

def _record_profile_views(formatted_products: list[dict], viewer) -> None:
    """
    For each product on this page whose seller is not the viewer,
    upsert a ProfileViewLog row (one row per viewer × profile_user).
    Service cards (item_type == 'service') are skipped.
    """
    if not viewer or not getattr(viewer, 'is_authenticated', False):
        return
    try:
        from apps.customer.models.account import ProfileViewLog  # noqa: PLC0415
        seen_dealer_ids: set[int] = set()
        for p in formatted_products:
            if p.get('item_type') == 'service':
                continue
            dealer_id = p.get('dealer_id')
            if not dealer_id or dealer_id == viewer.pk:
                continue
            if dealer_id in seen_dealer_ids:
                continue
            seen_dealer_ids.add(dealer_id)

            profile_user = User.objects.filter(pk=dealer_id).first()
            if not profile_user:
                continue

            obj, created = ProfileViewLog.objects.get_or_create(
                profile_user=profile_user,
                viewer=viewer,
                defaults={'viewed_at': timezone.now(), 'ip_address': None}
            )
            if not created:
                obj.viewed_at = timezone.now()
                obj.save(update_fields=['viewed_at'])
    except Exception:
        logger.debug('_record_profile_views failed', exc_info=True)


# ═══════════════════════════════════════════════════════════════════
# ██  FOLLOW RELATIONSHIP RESOLVER
# ═══════════════════════════════════════════════════════════════════

def _resolve_following_ids(user) -> list[int]:
    """
    Return the list of User PKs that `user` is currently following.
    ProfileInfo.following is a M2M → User (AUTH_USER_MODEL).
    """
    try:
        from apps.customer.models.profile_info import ProfileInfo
        current_profile = ProfileInfo.objects.using('default').get(user=user)
        return list(
            current_profile.following
            .using('default')
            .values_list('id', flat=True)
        )
    except Exception:
        logger.warning(
            'DiscoveryEngine: _resolve_following_ids failed for user=%s',
            user.pk, exc_info=True,
        )
        return []


# ═══════════════════════════════════════════════════════════════════
# ██  PROFILE DATA LOADERS  (parallel, pickle-safe plain dicts)
# ═══════════════════════════════════════════════════════════════════

def _find_href_for_alt(alt: str, alt_to_href: dict, fallback: str) -> str:
    if not alt:
        return fallback
    alt_lower = alt.strip().lower()
    if alt_lower in alt_to_href:
        return alt_to_href[alt_lower]
    for text, href in alt_to_href.items():
        if text.startswith(alt_lower):
            return href
    return fallback


def _load_profile_context(user) -> dict[str, Any]:
    """~6 DB queries. CRITICAL — raises on miss (Http404 or 500)."""
    profile_info = get_object_or_404(
        ProfileInfo.objects.select_related('user'), user=user
    )

    product_stats = (
        Product.objects.filter(dealer=user).aggregate(
            products_listed_count=Count('id'),
            active_products_count=Count('id', filter=Q(is_active=True)),
            out_of_stock_count=Count('id', filter=Q(is_active=True, stock_status='out_of_stock')),
            low_stock_count=Count('id', filter=Q(is_active=True, stock_status='low_stock')),
            featured_products_count=Count('id', filter=Q(is_active=True, is_featured=True)),
            total_product_views=Sum('view_count'),
            total_revenue=Sum(
                ExpressionWrapper(F('revenue_generated'), output_field=DecimalField()),
                filter=Q(is_active=True),
            ),
            avg_product_rating=Avg('rating_average', filter=Q(is_active=True)),
        )
    )

    brands_count = (
        Brand.objects.filter(created_by=user, deleted_at__isnull=True).count()
    )

    categories_count = (
        Product.objects
        .filter(dealer=user, category__isnull=False, deleted_at__isnull=True)
        .values('category').distinct().count()
    )

    followers_count  = profile_info.followers.count()
    followings_count = profile_info.following.count()

    following_profiles = list(
        ProfileInfo.objects
        .filter(user__in=profile_info.following.all(), is_profile_archived=False)
        .values(
            'user_id', 'user__email_or_phone', 'profile_name',
            'profile_name_slug', 'profile_photo',
            'is_profile_verified', 'profile_type',
        )
    )

    default_photo = '/static/defaults/default-profile-picture.png'
    for fp in following_profiles:
        raw = fp.get('profile_photo') or ''
        fp['profile_photo_url'] = f"/media/{raw}" if raw else default_photo

    ratings_received = (
        ProductRating.objects.filter(product__dealer=user)
        .aggregate(total_ratings=Count('id'), avg_rating=Avg('rating'))
    )

    return {
        'profile_info': {
            'profile_name':               profile_info.profile_name,
            'profile_name_slug':          profile_info.profile_name_slug,
            'profile_photo':              profile_info.get_profile_photo_url(),
            'profile_cover_photo':        profile_info.get_profile_cover_photo_url(),
            'profile_tagline':            profile_info.profile_tagline,
            'profile_type':               profile_info.profile_type,
            'get_profile_type_display':   profile_info.get_profile_type_display(),
            'profile_gender':             profile_info.profile_gender,
            'get_profile_gender_display': (
                profile_info.get_profile_gender_display()
                if profile_info.profile_gender else ''
            ),
            'profile_dob':                profile_info.profile_dob,
            'show_dob':                   profile_info.show_dob,
            'show_location':              profile_info.show_location,
            'location_display':           profile_info.location_display,
            'profile_address':            profile_info.profile_address,
            'profile_postal_code':        profile_info.profile_postal_code,
            'profile_language':           profile_info.profile_language,
            'is_profile_verified':        profile_info.is_profile_verified,
            'verification_level':         profile_info.verification_level,
            'verified_at':                profile_info.verified_at,
            'profile_creation_time':      profile_info.profile_creation_time,
            'profile_updated_time':       profile_info.profile_updated_time,
            'business_name':              profile_info.business_name,
            'business_type':              profile_info.business_type,
            'business_registration':      profile_info.business_registration,
            'business_tax_id':            profile_info.business_tax_id,
            'business_website':           profile_info.business_website,
            'business_email':             profile_info.business_email,
            'business_phone':             profile_info.business_phone,
            'business_description':       profile_info.business_description,
            'social_facebook':            profile_info.social_facebook,
            'social_twitter':             profile_info.social_twitter,
            'social_instagram':           profile_info.social_instagram,
            'social_linkedin':            profile_info.social_linkedin,
            'social_youtube':             profile_info.social_youtube,
            'social_tiktok':              profile_info.social_tiktok,
            'social_whatsapp':            profile_info.social_whatsapp,
        },
        'profile_completion':      profile_info.completion_percentage,
        'engagement_score':        round(profile_info.get_engagement_score(), 1),
        'followers_count':         followers_count,
        'followings_count':        followings_count,
        'following_profiles':      following_profiles,
        'products_listed_count':   product_stats['products_listed_count'] or 0,
        'active_products_count':   product_stats['active_products_count'] or 0,
        'out_of_stock_count':      product_stats['out_of_stock_count'] or 0,
        'low_stock_count':         product_stats['low_stock_count'] or 0,
        'featured_products_count': product_stats['featured_products_count'] or 0,
        'total_product_views':     product_stats['total_product_views'] or 0,
        'total_revenue':           float(product_stats['total_revenue'] or 0),
        'avg_product_rating':      round(product_stats['avg_product_rating'] or 0, 2),
        'brands_count':            brands_count,
        'categories_count':        categories_count,
        'ratings_received_count':  ratings_received['total_ratings'] or 0,
        'avg_rating_received':     round(ratings_received['avg_rating'] or 0, 2),
    }


def _load_activity_context(user) -> dict[str, Any]:
    """~5 DB queries. Non-critical — degrades gracefully."""
    order_stats = (
        Order.objects.filter(user=user).aggregate(
            total_orders=Count('id'),
            total_spent=Sum('grand_total'),
            pending_orders=Count('id', filter=Q(status='pending')),
            delivered_orders=Count('id', filter=Q(status='delivered')),
            cancelled_orders=Count('id', filter=Q(status='cancelled')),
        )
    )

    wishlist_count = Wishlist.objects.filter(user=user).count()

    ratings_given = (
        ProductRating.objects.filter(user=user)
        .aggregate(total_given=Count('id'), avg_given=Avg('rating'))
    )

    recent_searches = list(
        SearchHistory.objects.filter(user=user)
        .order_by('-searched_at')
        .values('query', 'result_count', 'searched_at', 'search_count')[:5]
    )

    view_log_stats = (
        ProductViewLog.objects.filter(viewer=user, deleted_at__isnull=True)
        .aggregate(
            total_views_made=Count('id'),
            desktop_views=Count('id', filter=Q(device_type='desktop')),
            mobile_views=Count('id', filter=Q(device_type='mobile')),
        )
    )

    return {
        'total_orders':        order_stats['total_orders'] or 0,
        'total_spent':         float(order_stats['total_spent'] or 0),
        'pending_orders':      order_stats['pending_orders'] or 0,
        'delivered_orders':    order_stats['delivered_orders'] or 0,
        'cancelled_orders':    order_stats['cancelled_orders'] or 0,
        'wishlist_count':      wishlist_count,
        'ratings_given_count': ratings_given['total_given'] or 0,
        'avg_rating_given':    round(ratings_given['avg_given'] or 0, 2),
        'recent_searches':     recent_searches,
        'total_views_made':    view_log_stats['total_views_made'] or 0,
        'desktop_views':       view_log_stats['desktop_views'] or 0,
        'mobile_views':        view_log_stats['mobile_views'] or 0,
    }


def _load_recently_viewed(user) -> list[dict[str, Any]]:
    """1 DB query. Non-critical — degrades gracefully."""
    rows = (
        ProductView.objects
        .filter(user=user, product__is_active=True, product__deleted_at__isnull=True)
        .select_related('product', 'product__brand', 'product__category', 'product__sub_category')
        .only(
            'viewed_at', 'view_count',
            'product__product_title', 'product__product_name', 'product__slug',
            'product__image', 'product__selling_price', 'product__final_price',
            'product__brand_price', 'product__discount_percentage',
            'product__rating_average', 'product__review_count', 'product__stock_status',
            'product__wishlist_count', 'product__currency',
            'product__brand__brand_name', 'product__category__category_name',
            'product__sub_category__sub_category_name',
        )
        .order_by('-viewed_at')[:20]
    )

    result = []
    for pv in rows:
        p = pv.product
        result.append({
            'viewed_at':  pv.viewed_at,
            'view_count': pv.view_count,
            'product': {
                'product_title':       p.product_title,
                'product_name':        p.product_name,
                'slug':                p.slug,
                'image_url':           p.image_url,
                'selling_price':       float(p.selling_price or 0),
                'final_price':         float(p.final_price or 0),
                'brand_price':         float(p.brand_price or 0),
                'discount_percentage': float(p.discount_percentage or 0),
                'rating_average':      float(p.rating_average or 0),
                'review_count':        p.review_count,
                'stock_status':        p.stock_status,
                'wishlist_count':      p.wishlist_count,
                'currency':            p.currency,
                'is_on_sale':          p.discount_percentage > 0,
                'brand_name':          p.brand.brand_name if p.brand else '',
                'category_name':       p.category.category_name if p.category else '',
                'sub_category_name':   p.sub_category.sub_category_name if p.sub_category else '',
            }
        })
    return result


def _load_engine_context(user) -> dict[str, Any]:
    """1 DB query. Non-critical — degrades gracefully."""
    services = list(
        ConnectedService.objects
        .filter(user=user, is_connected=True)
        .only(
            'service_name', 'service_url', 'service_type',
            'og_title', 'og_description', 'og_thumbnail', 'og_site_name',
            'extracted_images', 'extracted_videos', 'extracted_links', 'extracted_text',
            'fetch_status', 'last_fetch_time',
        )
    )

    extracted_images = []
    extracted_videos = []
    extracted_links  = []
    extracted_texts  = []

    for svc in services:
        domain = urlparse(svc.service_url).netloc
        title  = svc.og_title or svc.service_name
        site   = svc.og_site_name or domain

        meta = {
            'source_url':      svc.service_url,
            'source_domain':   site,
            'service_name':    svc.service_name,
            'service_type':    svc.service_type,
            'fetch_status':    svc.fetch_status,
            'last_fetch_time': svc.last_fetch_time,
            'og_description':  svc.og_description,
            'og_thumbnail':    svc.og_thumbnail,
        }

        links = svc.extracted_links or []
        alt_to_href: dict[str, str] = {}
        for lnk in links:
            href = lnk.get('href', '')
            text = lnk.get('text', '').strip().lower()
            if href and text:
                alt_to_href[text] = href

        used_hrefs: set[str] = set()

        for img in (svc.extracted_images or []):
            img_url  = img.get('url', '')
            img_alt  = img.get('alt', '')
            img_href = _find_href_for_alt(img_alt, alt_to_href, svc.service_url)

            if img_href in used_hrefs or not img_href:
                img_href = img_url or svc.service_url

            used_hrefs.add(img_href)

            extracted_images.append({
                **meta,
                'file_url':      img_url,
                'alt':           img_alt,
                'title':         title,
                'source_url':    img_href,
                'source_domain': urlparse(img_href).netloc or site,
            })

        for vid in (svc.extracted_videos or []):
            extracted_videos.append({
                **meta,
                'title':      title,
                'source_url': vid.get('url') or svc.service_url,
                'duration':   None,
            })

        for link in (svc.extracted_links or []):
            href = link.get('href', '')
            extracted_links.append({
                **meta,
                'url':    href,
                'title':  link.get('text') or title,
                'domain': urlparse(href).netloc or domain,
            })

        if svc.extracted_text and svc.extracted_text.strip():
            extracted_texts.append({**meta, 'title': title, 'content': svc.extracted_text})

    return {
        'connected_services': [
            {
                'service_name': s.service_name,
                'service_url':  s.service_url,
                'service_type': s.service_type,
                'og_thumbnail': s.og_thumbnail,
                'og_site_name': s.og_site_name,
                'fetch_status': s.fetch_status,
            }
            for s in services
        ],
        'extracted_images': extracted_images,
        'extracted_videos': extracted_videos,
        'extracted_links':  extracted_links,
        'extracted_texts':  extracted_texts,
        'images_count':     len(extracted_images),
        'videos_count':     len(extracted_videos),
        'links_count':      len(extracted_links),
        'text_count':       len(extracted_texts),
    }


# ═══════════════════════════════════════════════════════════════════
# ██  PROFILE LOADER FALLBACKS
# ═══════════════════════════════════════════════════════════════════

_EMPTY_ACTIVITY: dict[str, Any] = {
    'total_orders': 0, 'total_spent': 0.0, 'pending_orders': 0,
    'delivered_orders': 0, 'cancelled_orders': 0, 'wishlist_count': 0,
    'ratings_given_count': 0, 'avg_rating_given': 0.0,
    'recent_searches': [], 'total_views_made': 0,
    'desktop_views': 0, 'mobile_views': 0,
}

_EMPTY_RECENT: list[dict[str, Any]] = []

_EMPTY_ENGINE: dict[str, Any] = {
    'connected_services': [], 'extracted_images': [], 'extracted_videos': [],
    'extracted_links': [], 'extracted_texts': [],
    'images_count': 0, 'videos_count': 0, 'links_count': 0, 'text_count': 0,
}

_FALLBACKS: dict[str, Any] = {
    'activity': _EMPTY_ACTIVITY,
    'recent':   _EMPTY_RECENT,
    'engine':   _EMPTY_ENGINE,
}


# ═══════════════════════════════════════════════════════════════════
# ██  CACHE INVALIDATION
# ═══════════════════════════════════════════════════════════════════

def invalidate_wishlist_cache(user_pk: int) -> None:
    _safe_cache_delete(f'{_WISHLIST_PREFIX}:{user_pk}')


def invalidate_profile_cache(user_id: int) -> None:
    cache.delete_many([
        _key_ctx(user_id), _key_activity(user_id),
        _key_recent(user_id), _key_engine(user_id), _key_etag(user_id),
    ])


def invalidate_recent_views(user_id: int) -> None:
    cache.delete_many([_key_recent(user_id), _key_etag(user_id)])


def invalidate_activity(user_id: int) -> None:
    cache.delete_many([_key_activity(user_id), _key_etag(user_id)])


def invalidate_engine(user_id: int) -> None:
    cache.delete_many([_key_engine(user_id), _key_etag(user_id)])


def invalidate_user_feed(user_id: int) -> None:
    """Call when a user buys, wishlists, views, or follows."""
    invalidate_user_affinity(user_id)
    invalidate_wishlist_cache(user_id)
    for slug in ('all', ''):
        for sort in ('newest', 'popular', 'trending'):
            _safe_cache_delete(_feed_base_key(user_id, '', slug, sort))
            _safe_cache_delete(_feed_page_key(user_id, '', slug, sort, 1, None, None, False))


def invalidate_fragment_caches() -> None:
    """Call from a post_save signal on Brand / Category / Product."""
    fragment_keys = [
        'de:brands', 'de:cats', 'de:subcats', 'de:total',
        'de:brand_cat_map', 'de:brand_subcat_map',
    ]
    fb_slugs = list(TAB_FILTER_SLUGS) + ['all', '', 'brands', 'categories', 'sub_categories']
    fb_keys  = [f'de:fb:{slug}' for slug in fb_slugs]
    try:
        cache.delete_many(fragment_keys + fb_keys)
    except Exception as exc:
        logger.warning('invalidate_fragment_caches failed: %s', exc)


def invalidate_service_cards() -> None:
    """Call from a post_save signal on ConnectedService."""
    try:
        cache.delete_pattern('de:base:*')
        cache.delete_pattern('de:page:*')
    except AttributeError:
        logger.debug('Cache backend has no delete_pattern; relying on TTL_FEED_BASE expiry')
    except Exception as exc:
        logger.warning('invalidate_service_cards failed: %s', exc)


# ═══════════════════════════════════════════════════════════════════
# ██  SERIALIZERS  (DRF)
# ═══════════════════════════════════════════════════════════════════

class ConnectedServiceSerializer(serializers.ModelSerializer):
    """Read serializer — all fields including scraped data."""
    class Meta:
        model = ConnectedService
        fields = [
            'id', 'service_name', 'service_url', 'service_type',
            'status', 'is_connected',
            'og_title', 'og_description', 'og_thumbnail', 'og_site_name', 'og_type',
            'extracted_images', 'extracted_videos', 'extracted_links', 'extracted_text',
            'fetch_status', 'fetch_error', 'last_fetch_time',
            'created_at', 'updated_at',
        ]
        read_only_fields = [
            'og_title', 'og_description', 'og_thumbnail', 'og_site_name', 'og_type',
            'extracted_images', 'extracted_videos', 'extracted_links', 'extracted_text',
            'fetch_status', 'fetch_error', 'last_fetch_time', 'last_fetched_data',
            'created_at', 'updated_at',
        ]


class ConnectedServiceWriteSerializer(serializers.ModelSerializer):
    """Write serializer — only user-editable fields."""
    class Meta:
        model = ConnectedService
        fields = [
            'service_name', 'service_url', 'service_type',
            'status', 'is_connected', 'api_key', 'auth_token', 'profile',
        ]


# ═══════════════════════════════════════════════════════════════════
# ██  SERVICE SERIALIZER HELPERS
# ═══════════════════════════════════════════════════════════════════

def _should_fetch(service: ConnectedService, max_age_seconds: int = 3600) -> bool:
    """Stale if never fetched, or older than max_age. 10-min backoff after errors."""
    if service.last_fetch_time is None:
        return True
    age = (timezone.now() - service.last_fetch_time).total_seconds()
    if service.fetch_status == 'error':
        return age > 600
    return age > max_age_seconds


def _serialize_service(service: ConnectedService) -> dict:
    """
    Flatten a ConnectedService to a plain dict.
    Falls back to last_fetched_data for services saved before the flat
    OG/extracted columns existed.
    """
    og_title       = service.og_title       or ''
    og_description = service.og_description or ''
    og_thumbnail   = service.og_thumbnail   or ''
    og_site_name   = service.og_site_name   or ''
    og_type        = service.og_type        or ''
    images         = service.extracted_images or []
    videos         = service.extracted_videos or []
    links          = service.extracted_links  or []
    text           = service.extracted_text   or ''

    raw = service.last_fetched_data or {}
    if raw and not og_title:
        og_title       = raw.get('title')       or raw.get('og_title')       or ''
        og_description = raw.get('description') or raw.get('og_description') or ''
        og_thumbnail   = (raw.get('og_image')   or raw.get('og_thumbnail')
                          or raw.get('thumbnail') or '')
        og_site_name   = raw.get('site_name')   or raw.get('og_site_name')   or ''
        og_type        = raw.get('og_type')     or raw.get('type')           or ''
    if raw and not images:
        images = [_coerce_image(i) for i in (raw.get('images') or []) if i]
    if raw and not videos:
        videos = [_coerce_video(v) for v in (raw.get('videos') or []) if v]
    if raw and not links:
        links  = [_coerce_link(l)  for l in (raw.get('links')  or []) if l]
    if raw and not text:
        text = (raw.get('full_content') or raw.get('text_content')
                or raw.get('content')   or raw.get('text') or '')

    domain = urlparse(service.service_url).netloc

    return {
        'id':               service.id,
        'service_name':     service.service_name,
        'service_url':      service.service_url,
        'service_type':     service.service_type,
        'service_domain':   domain,
        'status':           service.status,
        'is_connected':     service.is_connected,
        'og_title':         og_title,
        'og_description':   og_description,
        'og_thumbnail':     og_thumbnail,
        'og_site_name':     og_site_name or domain,
        'og_type':          og_type,
        'extracted_images': images,
        'extracted_videos': videos,
        'extracted_links':  links,
        'extracted_text':   text,
        'images_count':     len(images),
        'videos_count':     len(videos),
        'links_count':      len(links),
        'has_text':         bool(text.strip()),
        'fetch_status':     service.fetch_status,
        'fetch_error':      service.fetch_error or '',
        'last_fetch_time':  (
            service.last_fetch_time.isoformat()
            if service.last_fetch_time else None
        ),
        'is_stale': _should_fetch(service),
    }


# ═══════════════════════════════════════════════════════════════════
# ██  BACKGROUND REFRESH
# ═══════════════════════════════════════════════════════════════════

def _background_refresh(service_id: int, user_id: int) -> None:
    """
    Fetch one service in a daemon thread so the view never blocks.
    Busts the engine cache on success.
    Replace threading.Thread with a Celery task when available.
    """
    try:
        service = ConnectedService.objects.get(pk=service_id)
        fetch_service_data(service)
        cache.delete(_key_engine(user_id))
        logger.info(
            'Background refresh complete service_id=%s uid=%s status=%s',
            service_id, user_id, service.fetch_status,
        )
    except Exception:
        logger.exception(
            'Background refresh failed service_id=%s uid=%s', service_id, user_id,
        )


def _spawn_refresh(service_id: int, user_id: int) -> None:
    t = threading.Thread(
        target=_background_refresh,
        args=(service_id, user_id),
        daemon=True,
    )
    t.start()


# ═══════════════════════════════════════════════════════════════════
# ██  MAIN DISCOVERY ENGINE VIEW
# ═══════════════════════════════════════════════════════════════════

@ensure_csrf_cookie
@vary_on_cookie
@cache_control(private=True, max_age=0, must_revalidate=True)
def DiscoveryEngineView(request: HttpRequest) -> HttpResponse:
    """
    Request flow
    ────────────
    0.  Rate limit check
    1.  Parse all params via FilterParams.from_request()
    2.  Load filter fragments  (brand / cat / subcat)
    3.  Build base queryset    (.only() + select_related, zero N+1)
    4.  FilterPipeline.run()   all panel filters + sort applied
    5.  Sort path:
          name_az    → natural_sort_pks() + Case/When re-fetch
          discount    → DB filter + ORDER BY
          explicit    → DB ORDER BY
          feed sorts → FeedEngine.rank_dicts() + two-tier cache
    5b. Inject ConnectedService cards alongside products
    6.  Inject per-user wishlist state
    7.  Record ProfileViewLog entries for seller profiles
    8.  Suggested dealers
    9.  Build context + render
    """

    # ── 0. Rate limit ─────────────────────────────────────────────
    if not _check_rate_limit(request):
        return HttpResponse(
            'Too many requests.', status=429,
            headers={'Retry-After': str(_RL_WINDOW_S)},
        )

    # ── 1. Parse params ───────────────────────────────────────────
    params = FilterParams.from_request(request)

    # ── 2. Load filter fragments ──────────────────────────────────
    brands         = _get_active_brands()
    categories     = _get_active_categories()
    sub_categories = _get_active_sub_categories()
    total_count    = _get_total_product_count()

    brand_category_map    = _get_brand_category_map()
    brand_subcategory_map = _get_brand_subcategory_map()

    # ── 3. Base queryset ──────────────────────────────────────────
    base_qs = (
        Product.objects
        .active_products()
        .only(*PRODUCT_FIELDS)
        .select_related('brand', 'category', 'sub_category', 'dealer__profileinfo')
    )

    # ── 4. FilterPipeline ─────────────────────────────────────────
    pipeline = FilterPipeline(
        brands=brands,
        categories=categories,
        sub_categories=sub_categories,
    )
    products_qs, filter_meta = pipeline.run(base_qs, params)

    if filter_meta.unknown_filter:
        messages.warning(request, f'Filter "{escape(params.filter_slug)}" not found.')

    # ── 5. Sort + paginate + (optionally) rank ────────────────────
    page_obj           = _NULL_PAGE
    formatted_products = []

    if params.sort_by == 'name_az':
        all_sorted_pks = natural_sort_pks(products_qs)
        paginator = Paginator(all_sorted_pks, PRODUCTS_PER_PAGE)
        safe_page = min(params.page, paginator.num_pages) if paginator.num_pages else 1
        page_obj  = paginator.get_page(safe_page)
        page_pks  = list(page_obj)

        if page_pks:
            ordering_case = Case(
                *[When(pk=pk, then=Value(idx)) for idx, pk in enumerate(page_pks)],
                output_field=IntegerField(),
            )
            page_qs = (
                products_qs
                .filter(pk__in=page_pks)
                .annotate(_sort_order=ordering_case)
                .order_by('_sort_order')
            )
            raw_list           = list(page_qs)
            dealer_counts      = _batch_dealer_product_counts(raw_list)
            formatted_products = _format_products(raw_list, params.query, dealer_counts)

    elif params.sort_by == 'discount':
        _, page_obj, formatted_products = _paginate_and_format(
            products_qs, params.page, params.query
        )

    elif params.sort_by in EXPLICIT_SORT_BYPASSES:
        _, page_obj, formatted_products = _paginate_and_format(
            products_qs, params.page, params.query
        )

    else:
        # Feed sorts: two-tier cache + FeedEngine.rank_dicts()
        page_obj, _ = _get_scored_feed(
            products_qs=products_qs,
            user=request.user,
            params=params,
        )
        formatted_products = list(page_obj)

    # ── 5b. Inject ConnectedService cards ─────────────────────────
    show_service_cards = (
        params.page == 1
        and params.filter_slug in ('', 'all', None)
        and params.sort_by not in EXPLICIT_SORT_BYPASSES
    )

    if show_service_cards:
        seen_dealer_ids = {
            p.get('dealer_id') for p in formatted_products if p.get('dealer_id')
        }
        service_items = _format_connected_service_items(
            query=params.query,
            limit=SERVICE_CARDS_LIMIT,
            exclude_user_ids=seen_dealer_ids,
        )
        if service_items:
            formatted_products = _interleave_service_cards(formatted_products, service_items)

    # ── 6. Per-user wishlist state ─────────────────────────────────
    wishlisted_ids = _get_wishlisted_ids(request.user)

    # ── 7. ProfileViewLog ─────────────────────────────────────────
    _record_profile_views(formatted_products, request.user)

    # ── 8. Suggested dealers ──────────────────────────────────────
    already_following: list[int] = []
    if request.user.is_authenticated:
        already_following = _resolve_following_ids(request.user)

    try:
        suggested_dealers = list(
            User.objects
            .filter(role='dealer', is_active=True, deleted_at__isnull=True)
            .exclude(id__in=already_following)
            .select_related('profileinfo')
            .annotate(product_count=Count('products', filter=Q(products__is_active=True)))
            .order_by('-product_count')[:6]
        )
    except Exception:
        suggested_dealers = []

    for dealer in suggested_dealers:
        try:
            pi = dealer.profileinfo
            if not getattr(pi, 'profile_name', None):
                pi.profile_name = (
                    getattr(pi, 'full_name', None)
                    or getattr(dealer, 'email_or_phone', '')
                    or ''
                )
        except Exception:
            pass

    # ── Insert this BEFORE the "context = {...}" block in DiscoveryEngineView ──

    # ── 8b. Connected services + engine context (Formats 4/5/6/7/9/10) ────
    engine_context = {'extracted_images': [], 'images_count': 0, 'links_count': 0}
    connected_services = []
    error_service_count = success_service_count = pending_service_count = 0
    type_breakdown = []

    if request.user.is_authenticated:
        engine_context = _load_engine_context(request.user)
        connected_services = engine_context.get('connected_services', [])

        for svc in connected_services:
            status = svc.get('fetch_status')
            if status == 'success':
                success_service_count += 1
            elif status == 'error':
                error_service_count += 1
            else:
                pending_service_count += 1

        type_counts: dict[str, int] = {}
        for svc in connected_services:
            t = svc.get('service_type', 'other')
            type_counts[t] = type_counts.get(t, 0) + 1

        _TYPE_COLORS = {
            'product':   '#0d47a1', 'education': '#7c3aed', 'news': '#ec4899',
            'business':  '#00b894', 'api':       '#d4af37', 'person': '#f97316',
            'location':  '#14b8a6',
        }
        type_breakdown = [
            {'type': t, 'count': c, 'color': _TYPE_COLORS.get(t, '#7c3aed')}
            for t, c in type_counts.items()
        ]


    # ── Then add these keys to the existing context dict: ─────────────────
    #

    # ── 9. Context + render ───────────────────────────────────────
    context = {
        # Feed
        'products':           formatted_products,
        'page_obj':           page_obj,
        'query':              params.query,
        'total_count':        total_count,

        # Filters / sort
        'current_filter':     params.filter_slug,
        'active_filter_type': filter_meta.active_filter_type,
        'active_tab':         filter_meta.active_tab,
        'applied_filters':    filter_meta.applied_filters,
        'sort_by':            params.sort_by,
        'sort_options':       build_sort_options(params.sort_by),
        'tab_options':        build_tab_options(filter_meta.active_tab),
        'in_stock':           params.in_stock,
        'min_price':          params.min_price,
        'max_price':          params.max_price,

        # Panel data
        'all_brands':         brands,
        'all_categories':     categories,
        'all_sub_categories': sub_categories,

        # JS maps
        'brand_category_map':    brand_category_map,
        'brand_subcategory_map': brand_subcategory_map,

        # Per-user state
        'wishlisted_ids':    list(wishlisted_ids),
        'already_following': already_following,
        'suggested_dealers': suggested_dealers,

        # getCookie JS helper required by wishlist/follow AJAX
        'get_cookie_js': _GET_COOKIE_JS,


        'connected_services':     connected_services,
        'engine_context':         engine_context,
        'error_service_count':    error_service_count,
        'success_service_count':  success_service_count,
        'pending_service_count':  pending_service_count,
        'type_breakdown':         json.dumps(type_breakdown),

    }

    return render(request, 'ponno/discovery_engine.html', context)


# ═══════════════════════════════════════════════════════════════════
# ██  SERVICE API VIEWS
# ═══════════════════════════════════════════════════════════════════

@login_required(login_url='/customer/signin/')
@require_http_methods(['POST'])
def add_service(request):
    try:
        data         = json.loads(request.body)
        service_name = data.get('service_name')
        service_url  = data.get('service_url')
        service_type = data.get('service_type', 'other')

        if not service_name or not service_url:
            return JsonResponse(
                {'success': False, 'error': 'Service name and URL are required'}, status=400
            )

        service = ConnectedService.objects.create(
            user=request.user,
            service_name=service_name,
            service_url=service_url,
            service_type=service_type,
            is_connected=False,
            status='private',
        )

        return JsonResponse({
            'success': True,
            'service': {
                'id':           service.id,
                'service_name': service.service_name,
                'service_url':  service.service_url,
                'service_type': service.service_type,
                'is_connected': service.is_connected,
                'status':       service.status,
            },
        })
    except Exception as e:
        return JsonResponse({'success': False, 'error': str(e)}, status=500)


@login_required(login_url='/customer/signin/')
@require_http_methods(['POST'])
def toggle_service_connection(request, service_id):
    try:
        service = get_object_or_404(ConnectedService, id=service_id, user=request.user)
        service.is_connected = not service.is_connected
        service.save()

        if service.is_connected:
            fetch_service_data(service)

        return JsonResponse({
            'success':      True,
            'is_connected': service.is_connected,
            'status':       service.status,
        })
    except Exception as e:
        return JsonResponse({'success': False, 'error': str(e)}, status=500)


@login_required(login_url='/customer/signin/')
@require_http_methods(['DELETE'])
def delete_service(request, service_id):
    try:
        service = get_object_or_404(ConnectedService, id=service_id, user=request.user)
        service.delete()
        return JsonResponse({'success': True})
    except Exception as e:
        return JsonResponse({'success': False, 'error': str(e)}, status=500)


@login_required(login_url='/customer/signin/')
@require_http_methods(['POST'])
def refresh_service_data(request, service_id):
    """POST engine/api/services/<id>/refresh/ — refresh one service."""
    try:
        service = get_object_or_404(ConnectedService, id=service_id, user=request.user)

        if not service.is_connected:
            return JsonResponse({'success': False, 'error': 'Service is not connected'}, status=400)

        fetch_service_data(service)
        service.refresh_from_db()
        return JsonResponse({'success': True, **_serialize_service(service)})

    except Exception as e:
        logger.exception('refresh_service_data failed for service_id=%s', service_id)
        return JsonResponse({'success': False, 'error': str(e)}, status=500)


@login_required(login_url='/customer/signin/')
@require_http_methods(['POST'])
def batch_refresh_services(request):
    """POST engine/api/services/batch-refresh/ — refresh multiple services."""
    try:
        body        = json.loads(request.body or '{}')
        service_ids = body.get('service_ids', [])

        qs = ConnectedService.objects.filter(user=request.user, is_connected=True)
        if service_ids:
            qs = qs.filter(id__in=service_ids)

        results = []
        for service in qs:
            fetch_service_data(service)
            service.refresh_from_db()
            results.append(_serialize_service(service))

        return JsonResponse({
            'success':    True,
            'results':    results,
            'total':      len(results),
            'successful': sum(1 for r in results if r['fetch_status'] == 'success'),
        })

    except Exception as e:
        logger.exception('batch_refresh_services failed')
        return JsonResponse({'success': False, 'error': str(e)}, status=500)


@login_required(login_url='/customer/signin/')
def get_service_media(request, service_id):
    """GET engine/api/services/<id>/media/ — return cached scraped media."""
    try:
        service = get_object_or_404(ConnectedService, id=service_id, user=request.user)

        if service.fetch_status != 'success':
            return JsonResponse({'success': False, 'error': 'No data available'}, status=404)

        return JsonResponse({'success': True, **_serialize_service(service)})

    except Exception as e:
        logger.exception('get_service_media failed for service_id=%s', service_id)
        return JsonResponse({'success': False, 'error': str(e)}, status=500)


# ═══════════════════════════════════════════════════════════════════
# ██  DRF VIEWSET  (mounted via megamind/urls.py)
# ═══════════════════════════════════════════════════════════════════

class ConnectedServiceViewSet(ModelViewSet):
    """
    GET    /api/connected-services/              → list
    POST   /api/connected-services/              → create
    GET    /api/connected-services/{id}/         → retrieve
    PUT    /api/connected-services/{id}/         → update
    PATCH  /api/connected-services/{id}/         → partial_update
    DELETE /api/connected-services/{id}/         → destroy
    POST   /api/connected-services/{id}/fetch/   → scrape & persist
    POST   /api/connected-services/{id}/refresh/ → alias of fetch/
    GET    /api/connected-services/{id}/preview/ → cached data only
    """
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        return ConnectedService.objects.filter(user=self.request.user)

    def get_serializer_class(self):
        if self.action in ('create', 'update', 'partial_update'):
            return ConnectedServiceWriteSerializer
        return ConnectedServiceSerializer

    def perform_create(self, serializer):
        serializer.save(user=self.request.user)

    def _do_scrape(self, service: ConnectedService):
        logger.info('Scraping service_id=%s url=%s', service.id, service.service_url)

        result = scrape_url(
            url=service.service_url,
            api_key=service.api_key,
            auth_token=service.auth_token,
        )

        if result['error']:
            logger.warning(
                'Scrape failed for service_id=%s url=%s error=%s',
                service.id, service.service_url, result['error'],
            )
            service.fetch_status = 'error'
            service.fetch_error  = result['error']
            service.save(update_fields=['fetch_status', 'fetch_error', 'updated_at'])

            return False, Response(
                {
                    'success':      False,
                    'fetch_status': 'error',
                    'fetch_error':  result['error'],
                    'detail':       'Could not fetch data from this URL. The site may block automated access.',
                },
                status=status.HTTP_200_OK,
            )

        og = result['og']
        service.og_title         = og.get('title')       or ''
        service.og_description   = og.get('description') or ''
        service.og_thumbnail     = og.get('thumbnail')   or ''
        service.og_site_name     = og.get('site_name')   or ''
        service.og_type          = og.get('type')        or ''
        service.extracted_images = result['images']
        service.extracted_videos = result['videos']
        service.extracted_links  = result['links']
        service.extracted_text   = result['text']
        service.last_fetched_data = result['raw']
        service.last_fetch_time  = timezone.now()
        service.fetch_status     = 'success'
        service.fetch_error      = None

        service.save(update_fields=[
            'og_title', 'og_description', 'og_thumbnail', 'og_site_name', 'og_type',
            'extracted_images', 'extracted_videos', 'extracted_links', 'extracted_text',
            'last_fetched_data', 'last_fetch_time', 'fetch_status', 'fetch_error',
            'updated_at',
        ])

        return True, Response(
            {'success': True, **ConnectedServiceSerializer(service).data},
            status=status.HTTP_200_OK,
        )

    @action(detail=True, methods=['post'], url_path='fetch')
    def fetch(self, request, pk=None):
        """Scrape service_url and persist all extracted fields."""
        _, response = self._do_scrape(self.get_object())
        return response

    @action(detail=True, methods=['post'], url_path='refresh')
    def refresh(self, request, pk=None):
        """Dashboard refresh — identical to fetch/ but at /refresh/ path."""
        _, response = self._do_scrape(self.get_object())
        return response

    @action(detail=True, methods=['get'], url_path='preview')
    def preview(self, request, pk=None):
        """Return cached scraped content without triggering a new scrape."""
        service = self.get_object()

        if service.fetch_status != 'success':
            return Response(
                {
                    'detail':       'No data fetched yet. POST to /fetch/ first.',
                    'fetch_status': service.fetch_status,
                },
                status=status.HTTP_404_NOT_FOUND,
            )

        return Response(
            {
                'id':              service.id,
                'service_name':    service.service_name,
                'service_url':     service.service_url,
                'last_fetch_time': service.last_fetch_time,
                'og': {
                    'title':       service.og_title,
                    'description': service.og_description,
                    'thumbnail':   service.og_thumbnail,
                    'site_name':   service.og_site_name,
                    'type':        service.og_type,
                },
                'images': service.extracted_images,
                'videos': service.extracted_videos,
                'links':  service.extracted_links,
                'text':   service.extracted_text,
            },
            status=status.HTTP_200_OK,
        )