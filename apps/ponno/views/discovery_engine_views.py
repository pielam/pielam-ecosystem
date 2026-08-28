# apps/ponno/views/discovery_engine.py

"""
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
  ├─ Resolve active Campaign discounts          batch, no N+1
  │     (applied inside _format_products(), display-only —
  │      redemption/usage-limit enforcement stays in
  │      CampaignPricingService at cart/checkout time)
  ├─ Sort path:
  │     name_az   → natural_sort_pks() (PKs, then Case/When)
  │     discount   → DB ORDER BY
  │     explicit   → DB ORDER BY
  │     feed sorts → FeedEngine.rank_dicts() + two-tier cache
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

Role / anonymous-visitor safety
────────────────────────────────
This view is accessed by ALL user types: anonymous visitors, and every
authenticated Role (admin, dealer, customer, staff, moderator). No
sub-section may assume `request.user` is authenticated or has a
particular role. Every per-user block below either:
  (a) checks `request.user.is_authenticated` first, or
  (b) uses `_safe_user_role()` which returns None for anonymous users,
      and never raises.
Sub-sections that are non-critical (suggested dealers, wishlist state)
are wrapped so that a failure there degrades to an empty/default value
instead of a 500 — this must hold regardless of which role is logged in.
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
from django.core.cache import cache
from django.core.paginator import Paginator
from django.db.models import (
    Avg, Case, Count, DecimalField, ExpressionWrapper,
    F, IntegerField, Q, Sum, Value, When,
)
from django.http import HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, render
from django.utils import timezone
from django.utils.html import escape
from django.views.decorators.cache import cache_control
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.vary import vary_on_cookie

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
from apps.ponno.models.campaign import Campaign
from apps.ponno.models.category import Category
from apps.ponno.models.product import Order, Product, ProductView, SearchHistory, Wishlist
from apps.ponno.models.product_view_log import ProductViewLog
from apps.ponno.models.rating import ProductRating
from apps.ponno.models.sub_category import SubCategory
from apps.ponno.product_badges import attach_badges_to_products, resolve_badges_from_dict

logger = logging.getLogger(__name__)
User   = get_user_model()

# ═══════════════════════════════════════════════════════════════════
# ██  CAMPAIGN DISPLAY STRIP  (promo banners above the product grid)
# ═══════════════════════════════════════════════════════════════════
#
# Distinct from _batch_active_campaigns(): that resolves one campaign
# per product for price-badge purposes. This is just "what's currently
# running", for a promo strip — same Campaign.objects.active() scope,
# fragment-cached like brands/cats/subcats since it's identical for
# every visitor (anonymous or any Role).

# ═══════════════════════════════════════════════════════════════════
# ██  CAMPAIGN DISPLAY STRIP  (promo banners above the product grid)
# ═══════════════════════════════════════════════════════════════════
#
# Distinct from _batch_active_campaigns(): that resolves one campaign
# per product for price-badge purposes. This is just "what's currently
# running", for a promo strip — same Campaign.objects.active() scope,
# fragment-cached like brands/cats/subcats since it's identical for
# every visitor (anonymous or any Role).

TTL_CAMPAIGN_STRIP    = getattr(settings, 'DE_TTL_CAMPAIGN_STRIP', 60)
_CAMPAIGN_STRIP_LIMIT = 8


def _get_active_campaigns_display() -> list[dict]:
    """
    Cached list of the top N currently-active campaigns, for a promo
    strip rendered above the product grid. Read-only, display-only —
    does not touch times_used or any redemption logic.

    Never raises: any failure (cache or DB) degrades to an empty list,
    same pattern as the other fragment helpers (_get_active_brands(),
    etc.) so a broken campaign query never breaks page render for any
    Role.
    """
    key  = 'de:campaigns'
    data = _safe_cache_get(key)
    if data is not None:
        return data

    got_lock = _lock(key)
    if not got_lock:
        return _safe_cache_get(key) or []

    try:
        campaigns = (
            Campaign.objects.active()
            .only(
                'campaign_id', 'name', 'slug', 'description', 'banner_image',
                'campaign_type', 'discount_value', 'max_discount_amount',
                'applies_to', 'end_at', 'is_stackable', 'priority',
            )
            .order_by('-priority', '-discount_value')[:_CAMPAIGN_STRIP_LIMIT]
        )
        data = []
        for c in campaigns:
            if c.campaign_type == Campaign.CampaignType.PERCENTAGE:
                badge = f'{c.discount_value:g}% OFF'
            else:
                badge = f'{format_price(c.discount_value)} OFF'

            data.append({
                'campaign_id':   str(c.campaign_id),
                'name':          c.name,
                'slug':          c.slug,
                'description':   c.description or '',
                'banner_url':    _media(str(c.banner_image)) if c.banner_image else None,
                'badge':         badge,
                'campaign_type': c.campaign_type,
                'applies_to':    c.applies_to,
                'is_stackable':  c.is_stackable,
                'ends_at':       c.end_at.isoformat() if c.end_at else None,
            })
        _safe_cache_set(key, data, _jittered_ttl(TTL_CAMPAIGN_STRIP))
        return data
    except Exception:
        logger.warning('_get_active_campaigns_display failed', exc_info=True)
        return []
    finally:
        _unlock(key)

def invalidate_fragment_caches() -> None:
    """Call from a post_save signal on Brand / Category / Product / Campaign."""
    fragment_keys = [
        'de:brands', 'de:cats', 'de:subcats', 'de:total',
        'de:brand_cat_map', 'de:brand_subcat_map',
        'de:campaigns', 'de:has_active_campaigns',
    ]
    fb_slugs = list(TAB_FILTER_SLUGS) + ['all', '', 'brands', 'categories', 'sub_categories']
    fb_keys  = [f'de:fb:{slug}' for slug in fb_slugs]
    try:
        cache.delete_many(fragment_keys + fb_keys)
    except Exception as exc:
        logger.warning('invalidate_fragment_caches failed: %s', exc)

from megamind.services.visit_logger import record_discovery_visit

# ═══════════════════════════════════════════════════════════════════
# ██  DISCOVERY VISIT LOG  (megamind.models.visit_log.DiscoveryVisitLog)
# ═══════════════════════════════════════════════════════════════════
#
# Device/UA parsing, geo lookup, attribution, session flags, etc. are
# all handled inside record_discovery_visit() (megamind/services/
# visit_logger.py) — this view only supplies the view-specific context
# (search_query/filter_slug/sort_by/page/results_count) and fires it
# off the request/response critical path.

def _dispatch_discovery_visit(
    request: HttpRequest,
    params: FilterParams,
    results_count: int | None = None,
) -> None:
    """
    Fire-and-forget visitor log — no Celery available here, so we spawn
    a short-lived daemon thread (same pattern as _spawn_refresh) rather
    than calling record_discovery_visit() inline and adding its DB
    reads/write latency to every page load.
    Safe for anonymous visitors and every authenticated Role —
    record_discovery_visit() never raises.
    """
    try:
        t = threading.Thread(
            target=record_discovery_visit,
            kwargs=dict(
                request=request,
                search_query=(params.query or '')[:255],
                filter_slug=(params.filter_slug or '')[:255],
                sort_by=params.sort_by or '',
                page_number=params.page or 1,
                results_count=results_count,
            ),
            daemon=True,
        )
        t.start()
    except Exception:
        logger.debug('_dispatch_discovery_visit failed', exc_info=True)

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
ETAG_TTL     = 60 * 2   # 2 min

# Columns the ORM fetches — keeps DB rows narrow
PRODUCT_FIELDS = [
    'pk', 'product_id', 'slug', 'sku',
    'product_title', 'product_name', 'short_description',
    'brand_price', 'buying_price', 'selling_price', 'final_price', 'discount_percentage',
    'is_brand_price_visible', 'is_buying_price_visible', 'is_selling_price_visible',
    'image', 'video_url', 'free_shipping',
    'view_count', 'rating_average', 'review_count', 'total_sales',
    'wishlist_count',
    'stock', 'stock_status', 'low_stock_threshold',
    'is_featured', 'is_verified', 'is_trending',
    'product_condition',
    'brand_id', 'category_id', 'sub_category_id', 'dealer_id',
    'created_at',
]

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
# ██  SAFE ROLE / USER HELPERS  (anonymous-visitor + any-Role safe)
# ═══════════════════════════════════════════════════════════════════

def _safe_user_role(user) -> str | None:
    """
    Returns the user's role string (User.Role.* value), or None for
    anonymous visitors. Never raises — AnonymousUser has no `role`
    attribute, and this must work identically for every Role
    (admin / dealer / customer / staff / moderator).
    """
    if not user or not getattr(user, 'is_authenticated', False):
        return None
    return getattr(user, 'role', None)


def _safe_user_pk(user):
    """Returns user.pk if authenticated, else None. Never raises."""
    if not user or not getattr(user, 'is_authenticated', False):
        return None
    return getattr(user, 'pk', None)


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
) -> str:
    ctx             = ctx or {}
    activity        = activity or {}
    recently_viewed = recently_viewed or []
    fingerprint = {
        'uid':      uid,
        'pc':       ctx.get('profile_completion'),
        'fc':       ctx.get('followers_count'),
        'to':       activity.get('total_orders'),
        'rv_count': len(recently_viewed),
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
from megamind.utils.video_info import get_video_info_cached

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
    """
    Per-user wishlist UUID set. Redis-cached; DB fallback on cold miss.
    Anonymous visitors (any Role check aside) always get an empty set —
    never raises.
    """
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
        logger.debug('_get_wishlisted_ids DB fallback failed for uid=%s', user.pk, exc_info=True)
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
# ██  CAMPAIGN PRICING  (display-time discount resolution)
# ═══════════════════════════════════════════════════════════════════
#
# Mirrors Campaign.objects.for_product()'s scope + ordering rules
# (all_products / specific_products / specific_categories /
# specific_brands, highest priority first, largest discount_value as
# tiebreaker) but resolved once for the whole page of products instead
# of once per product — avoids N+1 queries on a 20+ product feed page.
#
# This is read-only, display-time pricing only. Actual redemption
# (usage-limit enforcement, atomic times_used increment, per-user caps)
# stays exclusively in
# apps.ponno.services.campaign_pricing.CampaignPricingService, invoked
# at cart/checkout resolution — never here.

def _any_active_campaigns() -> bool:
    """
    Cheap cached existence check. On most page loads zero campaigns are
    running at all, so this lets us skip _batch_active_campaigns()'s
    heavier query (an OR across 4 scope conditions, some of which join
    through M2M tables) entirely instead of running it — and getting an
    empty result — on every single request. Cached briefly since "a
    campaign just started/ended" doesn't need to be observed within
    milliseconds; fails open (assume True) so a cache/DB hiccup here
    never hides a real campaign, it just costs one extra query.
    """
    key    = 'de:has_active_campaigns'
    cached = _safe_cache_get(key)
    if cached is not None:
        return cached
    try:
        exists = Campaign.objects.active().exists()
    except Exception:
        logger.warning('_any_active_campaigns check failed', exc_info=True)
        return True
    _safe_cache_set(key, exists, 30)
    return exists


def invalidate_campaign_cache() -> None:
    """Call from a post_save/post_delete signal on Campaign."""
    _safe_cache_delete('de:has_active_campaigns')


def _batch_active_campaigns(product_list: list) -> dict[int, Campaign]:
    """
    Resolve the single best-priority active Campaign applicable to each
    product in `product_list`, in a handful of queries regardless of
    page size. Returns {product.pk: best_campaign}; a product with no
    applicable campaign is simply absent from the returned dict.

    Non-stackable-only resolution: even if a product is covered by
    several stackable campaigns, this picks just the top one for
    display purposes (a "from ৳X" style badge). Full stacking math is
    CampaignPricingService's job at checkout, not the feed's.
    """
    if not product_list:
        return {}
    if not _any_active_campaigns():
        return {}

    category_ids = {p.category_id for p in product_list if p.category_id}
    brand_ids    = {p.brand_id for p in product_list if p.brand_id}
    product_ids  = [p.pk for p in product_list]

    scope_q = Q(applies_to=Campaign.AppliesTo.ALL_PRODUCTS)
    scope_q |= Q(applies_to=Campaign.AppliesTo.SPECIFIC_PRODUCTS, products__in=product_ids)
    if category_ids:
        scope_q |= Q(applies_to=Campaign.AppliesTo.SPECIFIC_CATEGORIES, categories__in=category_ids)
    if brand_ids:
        scope_q |= Q(applies_to=Campaign.AppliesTo.SPECIFIC_BRANDS, brands__in=brand_ids)

    try:
        campaigns = list(
            Campaign.objects.active()
            .filter(scope_q)
            .distinct()
            .prefetch_related('products', 'categories', 'brands')
            .order_by('-priority', '-discount_value')
        )
    except Exception:
        # A broken campaign query must never break the product feed —
        # products just render without campaign pricing.
        logger.warning('_batch_active_campaigns query failed', exc_info=True)
        return {}

    if not campaigns:
        return {}

    all_products_campaigns = [
        c for c in campaigns if c.applies_to == Campaign.AppliesTo.ALL_PRODUCTS
    ]

    by_product_id: dict[int, list] = {}
    by_category_id: dict[int, list] = {}
    by_brand_id: dict[int, list] = {}

    for c in campaigns:
        if c.applies_to == Campaign.AppliesTo.SPECIFIC_PRODUCTS:
            for pid in c.products.all().values_list('pk', flat=True):
                by_product_id.setdefault(pid, []).append(c)
        elif c.applies_to == Campaign.AppliesTo.SPECIFIC_CATEGORIES:
            for cid in c.categories.all().values_list('pk', flat=True):
                by_category_id.setdefault(cid, []).append(c)
        elif c.applies_to == Campaign.AppliesTo.SPECIFIC_BRANDS:
            for bid in c.brands.all().values_list('pk', flat=True):
                by_brand_id.setdefault(bid, []).append(c)

    best_by_product: dict[int, Campaign] = {}
    for p in product_list:
        candidates = list(all_products_campaigns)
        candidates += by_product_id.get(p.pk, [])
        if p.category_id:
            candidates += by_category_id.get(p.category_id, [])
        if p.brand_id:
            candidates += by_brand_id.get(p.brand_id, [])
        if not candidates:
            continue
        candidates.sort(key=lambda c: (-c.priority, -c.discount_value))
        best_by_product[p.pk] = candidates[0]

    return best_by_product


# ═══════════════════════════════════════════════════════════════════
# ██  SHARED SELLER/DEALER RESOLVER
# ═══════════════════════════════════════════════════════════════════

def _resolve_seller_info(user) -> dict:
    """
    Shared seller/dealer info resolver used by Product cards, so all
    item types render an identical dealer strip in the template. Works
    for a seller of any Role, and tolerates a missing/None user or
    missing ProfileInfo.
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
    campaign_map: dict | None = None,
) -> list[dict]:
    """
    Convert ORM Product instances into template-ready dicts.
    Badge resolution delegated to attach_badges_to_products().
    Seller info delegated to the shared _resolve_seller_info() helper.
    Campaign discount resolution delegated to _batch_active_campaigns()
    — pass its result in as `campaign_map`; a missing/empty map just
    means no product gets a campaign price (never raises).
    """
    if dealer_counts is None:
        dealer_counts = {}
    if campaign_map is None:
        campaign_map = {}

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

        # ── Pricing visibility flags ─────────────────────────────
        show_brand_price   = bool(product.is_brand_price_visible)
        show_buying_price  = bool(product.is_buying_price_visible)
        show_selling_price = bool(product.is_selling_price_visible)

        # ── Pricing ────────────────────────────────────────────────
        discount_pct   = float(product.discount_percentage or 0)
        discount_badge = f'{int(discount_pct)}% OFF' if discount_pct > 0 else None
        final_price    = product.final_price or product.selling_price

        # ── Campaign discount (on top of the dealer's own price) ────
        # Display-only: computed via Campaign.compute_discount(), the
        # same pure calculation CampaignPricingService uses at
        # checkout, just not redeemed/recorded here.
        campaign             = campaign_map.get(product.pk)
        campaign_info        = None
        campaign_final_price = None

        if campaign and show_selling_price and final_price:
            campaign_discount = campaign.compute_discount(final_price)
            if campaign_discount > 0:
                campaign_final_price = campaign.discounted_price(final_price)
                campaign_info = {
                    'campaign_id':       str(campaign.campaign_id),
                    'campaign_name':     campaign.name,
                    'campaign_slug':     campaign.slug,
                    'campaign_type':     campaign.campaign_type,
                    'discount_amount':   format_price(campaign_discount),
                    'discount_amount_raw': float(campaign_discount),
                    'is_stackable':      campaign.is_stackable,
                }

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
            'brand_price':          format_price(product.brand_price) if (show_brand_price and product.brand_price) else None,
            'buying_price':         format_price(product.buying_price) if (show_buying_price and product.buying_price) else None,
            'original_price':       format_price(product.selling_price) if show_selling_price else None,
            'final_price':          format_price(final_price) if show_selling_price else None,
            'discount_percentage':  discount_pct if show_selling_price else 0,
            'discount_pct':         discount_pct if show_selling_price else 0,
            'discount_badge':       discount_badge if show_selling_price else None,
            'on_sale':              (discount_pct > 0) if show_selling_price else False,

            # Campaign pricing — separate from the dealer's own
            # discount_percentage above. `campaign_price` is what the
            # customer actually pays when a campaign applies;
            # `final_price` above stays the dealer's own price so the
            # template can still show a strikethrough chain if wanted.
            'campaign':          campaign_info,
            'has_campaign':      campaign_info is not None,
            'campaign_price':    format_price(campaign_final_price) if campaign_final_price is not None else None,

            # Visibility flags (template can react, e.g. show "Contact Seller")
            'show_brand_price':    show_brand_price,
            'show_buying_price':   show_buying_price,
            'show_selling_price':  show_selling_price,
            'price_hidden':        not (show_selling_price or show_buying_price),

            # Media
            'image':      product_image_url,
            'video_url':  product.video_url,
            'video_info': get_video_info_cached(product.video_url),

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
    Works identically for anonymous visitors (user_id falls back to 0)
    and for every authenticated Role — FeedEngine(user) already handles
    an AnonymousUser internally via its own guards.
    """
    # Never use cache for search queries — always fetch fresh
    if params.query:
        raw_list      = list(products_qs.order_by(*SORT_MAP.get(params.sort_by, ['-created_at'])))
        dealer_counts = _batch_dealer_product_counts(raw_list)
        campaign_map  = _batch_active_campaigns(raw_list)
        formatted     = _format_products(raw_list, params.query, dealer_counts, campaign_map)
        engine        = FeedEngine(user)
        scored_list   = engine.rank_dicts(formatted, diversify=True)
        paginator     = Paginator(scored_list, PRODUCTS_PER_PAGE)
        safe_page     = min(params.page, paginator.num_pages) if paginator.num_pages else 1
        return paginator.get_page(safe_page), len(scored_list)

    user_id  = _safe_user_pk(user) or 0
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
                campaign_map  = _batch_active_campaigns(raw_list)
                formatted     = _format_products(raw_list, params.query, dealer_counts, campaign_map)
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
    campaign_map  = _batch_active_campaigns(raw_list)
    formatted     = _format_products(raw_list, query, dealer_counts, campaign_map)
    return total_count, page_obj, formatted


# ═══════════════════════════════════════════════════════════════════
# ██  PROFILE VIEW LOG
# ═══════════════════════════════════════════════════════════════════

def _record_profile_views(formatted_products: list[dict], viewer) -> None:
    """
    For each product on this page whose seller is not the viewer,
    upsert a ProfileViewLog row (one row per viewer × profile_user).
    No-op for anonymous visitors, regardless of Role logic elsewhere.
    Never raises — logging failures must not break the page for any
    Role.
    """
    if not viewer or not getattr(viewer, 'is_authenticated', False):
        return
    try:
        # NOTE: ProfileViewLog lives in its own module, not in
        # apps.customer.models.account (that module only defines
        # User / UserManager). Importing from the wrong path here
        # silently broke profile-view logging for every role, since
        # the failure was swallowed by the except clause below.
        from apps.customer.models.profile_view_log import ProfileViewLog  # noqa: PLC0415
        seen_dealer_ids: set[int] = set()
        for p in formatted_products:
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


def _dispatch_profile_view_logging(formatted_products: list[dict], viewer) -> None:
    """
    Fire-and-forget wrapper around _record_profile_views().

    This used to run inline in the request path: up to one
    get_or_create() + possible save() *DB write* per distinct seller
    shown on the page (so up to PRODUCTS_PER_PAGE synchronous
    round-trips), all before the response could be sent. None of that
    needs to finish before the user sees the page — it's the same
    "safe to lose, must not block" profile telemetry as the discovery
    visit log, so it now runs the same way: a short-lived daemon
    thread, same pattern as _dispatch_discovery_visit. Never raises.
    """
    if not viewer or not getattr(viewer, 'is_authenticated', False):
        return
    try:
        t = threading.Thread(
            target=_record_profile_views,
            args=(formatted_products, viewer),
            daemon=True,
        )
        t.start()
    except Exception:
        logger.debug('_dispatch_profile_view_logging failed', exc_info=True)


# ═══════════════════════════════════════════════════════════════════
# ██  FOLLOW RELATIONSHIP RESOLVER
# ═══════════════════════════════════════════════════════════════════

def _resolve_following_ids(user) -> list[int]:
    """
    Return the list of User PKs that `user` is currently following.
    ProfileInfo.following is a M2M → User (AUTH_USER_MODEL).
    Safe for any authenticated Role; caller must not call this for an
    anonymous user (ProfileInfo.objects.get(user=user) would fail).
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
            getattr(user, 'pk', None), exc_info=True,
        )
        return []


# ═══════════════════════════════════════════════════════════════════
# ██  PROFILE DATA LOADERS  (parallel, pickle-safe plain dicts)
# ═══════════════════════════════════════════════════════════════════

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
            'product__is_brand_price_visible', 'product__is_buying_price_visible',
            'product__is_selling_price_visible',
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
        show_brand_price   = bool(p.is_brand_price_visible)
        show_selling_price = bool(p.is_selling_price_visible)
        result.append({
            'viewed_at':  pv.viewed_at,
            'view_count': pv.view_count,
            'product': {
                'product_title':       p.product_title,
                'product_name':        p.product_name,
                'slug':                p.slug,
                'image_url':           p.image_url,
                'selling_price':       float(p.selling_price or 0) if show_selling_price else None,
                'final_price':         float(p.final_price or 0) if show_selling_price else None,
                'brand_price':         float(p.brand_price or 0) if show_brand_price else None,
                'discount_percentage': float(p.discount_percentage or 0) if show_selling_price else 0,
                'rating_average':      float(p.rating_average or 0),
                'review_count':        p.review_count,
                'stock_status':        p.stock_status,
                'wishlist_count':      p.wishlist_count,
                'currency':            p.currency,
                'is_on_sale':          (p.discount_percentage or 0) > 0 if show_selling_price else False,
                'brand_name':          p.brand.brand_name if p.brand else '',
                'category_name':       p.category.category_name if p.category else '',
                'sub_category_name':   p.sub_category.sub_category_name if p.sub_category else '',
            }
        })
    return result


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

_FALLBACKS: dict[str, Any] = {
    'activity': _EMPTY_ACTIVITY,
    'recent':   _EMPTY_RECENT,
}


# ═══════════════════════════════════════════════════════════════════
# ██  CACHE INVALIDATION
# ═══════════════════════════════════════════════════════════════════

def invalidate_profile_cache(user_id: int) -> None:
    cache.delete_many([
        _key_ctx(user_id), _key_activity(user_id),
        _key_recent(user_id), _key_etag(user_id),
    ])


def invalidate_recent_views(user_id: int) -> None:
    cache.delete_many([_key_recent(user_id), _key_etag(user_id)])


def invalidate_activity(user_id: int) -> None:
    cache.delete_many([_key_activity(user_id), _key_etag(user_id)])


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
    6.  Inject per-user wishlist state
    7.  Record ProfileViewLog entries for seller profiles
    8.  Suggested dealers
    9.  Build context + render

    Accessible by ANY visitor:
      - Anonymous (not logged in) — every per-user section below
        degrades to an empty/default value, never raises.
      - Any authenticated Role (admin, dealer, customer, staff,
        moderator) — no section assumes a specific role; non-critical
        sections are wrapped in try/except so one failure never 500s
        the whole page for any role.
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
    active_campaigns   = _get_active_campaigns_display()

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
            campaign_map       = _batch_active_campaigns(raw_list)
            formatted_products = _format_products(raw_list, params.query, dealer_counts, campaign_map)

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
        # Works for anonymous visitors and any Role — FeedEngine(user)
        # and _get_scored_feed() both fall back to a neutral user_id=0
        # bucket when the visitor isn't authenticated.
        page_obj, _ = _get_scored_feed(
            products_qs=products_qs,
            user=request.user,
            params=params,
        )
        formatted_products = list(page_obj)

    # ── 6. Per-user wishlist state ─────────────────────────────────
    # _get_wishlisted_ids() already returns an empty set for anonymous
    # visitors and never raises for any Role.
    try:
        wishlisted_ids = _get_wishlisted_ids(request.user)
    except Exception:
        logger.warning('wishlist lookup failed', exc_info=True)
        wishlisted_ids = set()

    # ── 7. ProfileViewLog ─────────────────────────────────────────
    # Fired off the critical path — see _dispatch_profile_view_logging
    # docstring. Never raises; try/except kept as defense-in-depth
    # around the thread-spawn itself.
    try:
        _dispatch_profile_view_logging(formatted_products, request.user)
    except Exception:
        logger.debug('profile view logging dispatch failed', exc_info=True)

    # ── 8. Suggested dealers ──────────────────────────────────────
    # Anonymous visitors and every Role get a safe default. The
    # "suggested dealers" widget itself is only meaningful for
    # customers / anonymous visitors browsing the marketplace; dealers,
    # staff, admins and moderators simply won't see it (empty list),
    # which the template already handles via `{% if suggested_dealers %}`.
    already_following: list[int] = []
    suggested_dealers: list = []

    user_role = _safe_user_role(request.user)

    if user_role in (None, User.Role.CUSTOMER):
        try:
            if request.user.is_authenticated:
                already_following = _resolve_following_ids(request.user)

            dealer_qs = (
                User.objects
                .filter(role=User.Role.DEALER, is_active=True, deleted_at__isnull=True)
                .exclude(id__in=already_following)
            )
            current_uid = _safe_user_pk(request.user)
            if current_uid:
                dealer_qs = dealer_qs.exclude(id=current_uid)  # never suggest self

            suggested_dealers = list(
                dealer_qs
                .select_related('profileinfo')
                .annotate(product_count=Count('products', filter=Q(products__is_active=True)))
                .order_by('-product_count')[:6]
            )
        except Exception:
            logger.debug('suggested_dealers query failed', exc_info=True)
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

    # ── 8b. Log the visit (background thread, non-blocking, safe for
    #        anonymous visitors and every Role) ────────────────────
    _dispatch_discovery_visit(request, params, results_count=total_count)

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

        # Per-user state (safe defaults for anonymous / any Role)
        'wishlisted_ids':    list(wishlisted_ids),
        'already_following': already_following,
        'suggested_dealers': suggested_dealers,
        'user_role':         user_role,

        # getCookie JS helper required by wishlist/follow AJAX
        'get_cookie_js': _GET_COOKIE_JS,

        'active_campaigns': active_campaigns,
    }

    return render(request, 'ponno/discovery_engine.html', context)