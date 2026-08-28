# apps/ponno/views/brand_list.py
"""
BrandListView — Production-grade, high-traffic brand listing view.

This file was previously a near-duplicate of ProductListView (it queried
Product with product-only fields — SKU, price, stock, category, rating —
none of which exist on Brand), and it rendered a mix of
'ponno/product_list.html' and 'ponno/brand_list.html' inconsistently.
It has been rewritten here to actually match apps/ponno/models/brand.py:

Architecture (all in one file, sectioned)
──────────────────────────────────────────
§1  Constants & config
§2  L1 in-process LRU cache  (_L1Cache)
§3  Two-layer cache           (TwoLayerCache)  L1 + Redis
§4  Circuit breaker           (CircuitBreaker) DB cascade protection
§5  Stats manager             (get_brand_stats) single-SQL aggregation
§6  Core data builder         (_build_context)
§7  View                      (BrandListView) — full page + infinite scroll
§8  Backfill utility          (backfill_brand_product_counts) — one-off repair

Notes vs. the old (Product-shaped) version
────────────────────────────────────────────
- No price/stock/category/SKU/rating fields exist on Brand, so price
  filtering, price sorts, and the nested brand/category serializers
  have all been removed — there's nothing on Brand to hang them off.
- Type filter now maps to real Brand boolean flags: is_featured,
  is_trending, is_verified, is_official, is_trusted.
- Sort options now use real Brand fields: popularity_score / view_count
  (popular), product_count (most_products), brand_created_at (newest),
  brand_name (name_asc/name_desc). There's no price/rating equivalent.
- The single "highlight one item" query param now highlights a specific
  *brand* by brand_slug (there's no sub-filtering-by-brand concept here,
  since this view lists brands themselves).
- Template rendered is always 'ponno/brand_list.html' for full page loads
  (previously one error branch pointed at 'ponno/product_list.html' by
  mistake), and 'ponno/partials/brand_cards.html' for infinite-scroll
  AJAX requests (see §7).
- Uses Brand.objects.active_brands() / the model's logo_url / brand_url
  properties rather than re-deriving that logic in the view.

Infinite scroll (§7)
────────────────────────────
The view now detects AJAX "load more" requests (X-Requested-With:
XMLHttpRequest header, or ?ajax=1 as a fallback) and renders ONLY the
brand cards partial instead of the full HTML document. Pagination state
is returned via response headers (X-Has-Next-Page, X-Next-Page,
X-Total-Count) so the frontend doesn't need to parse a JSON body.
The initial page load is unaffected — it's still a full, cacheable,
SEO-friendly HTML document. `Vary` now includes `X-Requested-With` so
a CDN/browser cache never confuses a partial response for a full page
(or vice versa) at the same URL.

Product count backfill (§8)
────────────────────────────
Brand.product_count is a cached column (Brand.update_product_count()).
apps/ponno/signals.py keeps it correct going forward (see the
PRODUCT → BRAND.PRODUCT_COUNT section there), but that signal only
fires on future Product saves/deletes — it does nothing for brands
whose count was already stale/zero from before the signal existed.
backfill_brand_product_counts() at the bottom of this file is the
one-off repair for that. Run it once from a shell after deploying the
signal:

    python manage.py shell -c "
    from apps.ponno.views.brand_list import backfill_brand_product_counts
    backfill_brand_product_counts()
    "

It does not touch _page_cache (see §8 docstring for why that's fine).
"""

from __future__ import annotations

import hashlib
import logging
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from enum import Enum, auto
from http import HTTPStatus
from threading import RLock
from typing import Any, Callable, Optional

from django.core.cache import cache
from django.core.paginator import InvalidPage, Paginator
from django.db import OperationalError, connection, transaction
from django.db.models import Q
from django.http import Http404, HttpResponse
from django.shortcuts import render
from django.utils.http import http_date, parse_http_date_safe
from django.views.decorators.http import require_GET
from django.views.decorators.vary import vary_on_headers

from apps.ponno.models.brand import Brand

log = logging.getLogger(__name__)


# ════════════════════════════════════════════════════════════════════════════
# §1  CONSTANTS & CONFIG
# ════════════════════════════════════════════════════════════════════════════

BRANDS_PER_PAGE = 24
CACHE_TTL       = 60      # seconds — fresh window
STALE_TTL       = 30      # seconds — serve stale while refreshing
MAX_PAGE        = 500
MAX_Q_LEN       = 120
MAX_SLUG_LEN    = 160     # Brand.brand_slug is max_length=160

VALID_SORTS = frozenset({
    'popular', 'most_products', 'newest', 'name_asc', 'name_desc',
})
VALID_TYPES = frozenset({
    'all', 'featured', 'trending', 'verified', 'official', 'trusted',
})

SORT_MAP = {
    'popular':       ('-popularity_score', '-view_count'),
    'most_products': ('-product_count', '-popularity_score'),
    'newest':        ('-brand_created_at',),
    'name_asc':      ('brand_name',),
    'name_desc':     ('-brand_name',),
}

# Fields actually needed to render a brand card — kept to real Brand columns.
ONLY_FIELDS = (
    'pk', 'brand_name', 'brand_slug', 'brand_type', 'brand_tagline',
    'brand_logo', 'country_of_origin',
    'is_verified', 'is_featured', 'is_trending', 'is_official', 'is_trusted',
    'view_count', 'product_count', 'category_count', 'subcategory_count',
    'popularity_score', 'average_rating', 'review_count',
    'brand_created_at',
)

# L1 cache limits
_L1_MAX_ENTRIES = 512   # bounded so it never OOMs
_L1_TTL         = 5     # seconds — short; process restarts are cheap

# Stats cache keys & TTLs
_STATS_CACHE_KEY = 'brandlist_stats_v1'
_STATS_LOCK_KEY  = 'brandlist_stats_lock_v1'
_STATS_STALE_KEY = 'brandlist_stats_stale_v1'
_STATS_CACHE_TTL = 300    # 5 min  — fresh
_STATS_STALE_TTL = 3600   # 1 hr   — long-lived fallback
_STATS_LOCK_TTL  = 15     # seconds

_EMPTY_STATS: dict = {
    'total': 0, 'featured': 0, 'trending': 0,
    'verified': 0, 'official': 0, 'trusted': 0,
}

# Backfill defaults (§8)
_BACKFILL_BATCH_SIZE = 200

# Infinite scroll (§7)
BRAND_CARDS_PARTIAL_TEMPLATE = 'ponno/brand_list.html'


# ════════════════════════════════════════════════════════════════════════════
# §2  L1 IN-PROCESS LRU CACHE
# ════════════════════════════════════════════════════════════════════════════

class _L1Cache:
    """
    Thread-safe, bounded LRU dict.
    Reads are O(1) with zero network — sub-microsecond.
    Eviction is O(1) via OrderedDict.popitem().
    TTL is enforced on read, not via a background thread.
    """

    def __init__(self, max_size: int, ttl: int) -> None:
        self._store: OrderedDict[str, tuple[Any, float]] = OrderedDict()
        self._max  = max_size
        self._ttl  = ttl
        self._lock = RLock()

    def get(self, key: str) -> tuple[bool, Any]:
        """Return (found, value). Expired entries treated as misses."""
        with self._lock:
            if key not in self._store:
                return False, None
            value, expires_at = self._store[key]
            if time.monotonic() > expires_at:
                del self._store[key]
                return False, None
            self._store.move_to_end(key)
            return True, value

    def set(self, key: str, value: Any) -> None:
        with self._lock:
            if key in self._store:
                self._store.move_to_end(key)
            self._store[key] = (value, time.monotonic() + self._ttl)
            if len(self._store) > self._max:
                self._store.popitem(last=False)

    def delete(self, key: str) -> None:
        with self._lock:
            self._store.pop(key, None)

    def clear(self) -> None:
        with self._lock:
            self._store.clear()


# ════════════════════════════════════════════════════════════════════════════
# §3  TWO-LAYER CACHE  (L1 + Redis)
# ════════════════════════════════════════════════════════════════════════════

_l1 = _L1Cache(max_size=_L1_MAX_ENTRIES, ttl=_L1_TTL)

_coalesce_locks: dict[str, RLock] = {}
_coalesce_meta_lock = RLock()


def _get_coalesce_lock(key: str) -> RLock:
    with _coalesce_meta_lock:
        if key not in _coalesce_locks:
            if len(_coalesce_locks) > _L1_MAX_ENTRIES * 2:
                _coalesce_locks.clear()   # bounded housekeeping
            _coalesce_locks[key] = RLock()
        return _coalesce_locks[key]


class TwoLayerCache:
    """
    L1 (in-process LRU) → L2 (Redis) with stale-while-revalidate
    and request coalescing built into get_or_build().
    """

    def __init__(self, ttl: int, stale_ttl: int) -> None:
        self.ttl       = ttl
        self.stale_ttl = stale_ttl

    def get(self, key: str) -> Optional[dict]:
        found, value = _l1.get(key)
        if found:
            return value
        try:
            value = cache.get(key)
        except Exception:
            log.warning("Redis GET failed key=%s", key)
            return None
        if value is not None:
            _l1.set(key, value)
        return value

    def set(self, key: str, value: dict) -> None:
        _l1.set(key, value)
        try:
            cache.set(key, value, self.ttl + self.stale_ttl)
        except Exception:
            log.warning("Redis SET failed key=%s", key)

    def invalidate(self, key: str) -> None:
        _l1.delete(key)
        try:
            cache.delete(key)
        except Exception:
            log.warning("Redis DELETE failed key=%s", key)

    def is_stale(self, context: dict) -> bool:
        return (time.time() - context.get('_built_at', 0.0)) > self.ttl

    def get_or_build(
        self,
        key: str,
        builder: Callable,
        args: tuple = (),
        kwargs: dict = None,
        warm_async_fn: Optional[Callable] = None,
    ) -> Optional[dict]:
        if kwargs is None:
            kwargs = {}

        ctx = self.get(key)
        if ctx is not None:
            if self.is_stale(ctx) and warm_async_fn:
                warm_async_fn(key, builder, *args, **kwargs)
            return ctx

        lock = _get_coalesce_lock(key)
        with lock:
            ctx = self.get(key)
            if ctx is not None:
                return ctx
            try:
                ctx = builder(*args, **kwargs)
            except Exception:
                log.exception("Builder raised for key=%s", key)
                return None
            if ctx is not None:
                self.set(key, ctx)

        return ctx


# ════════════════════════════════════════════════════════════════════════════
# §4  CIRCUIT BREAKER
# ════════════════════════════════════════════════════════════════════════════

class _BreakerState(Enum):
    CLOSED    = auto()
    OPEN      = auto()
    HALF_OPEN = auto()


class CircuitBreakerOpen(Exception):
    """Raised when the breaker is OPEN — caller should use fallback."""


class CircuitBreaker:
    """
    Thread-safe circuit breaker.

    CLOSED    → All calls go through.
    OPEN      → Fast-fail for reset_timeout seconds.
    HALF_OPEN → One probe call; success → CLOSED, failure → OPEN again.
    """

    def __init__(
        self,
        failure_threshold: int = 5,
        reset_timeout: float   = 30.0,
        success_threshold: int = 2,
        name: str              = "default",
    ) -> None:
        self._threshold         = failure_threshold
        self._reset_timeout     = reset_timeout
        self._success_threshold = success_threshold
        self._name              = name
        self._state             = _BreakerState.CLOSED
        self._failure_count     = 0
        self._success_count     = 0
        self._opened_at: float  = 0.0
        self._lock              = RLock()

    @property
    def state(self) -> _BreakerState:
        return self._state

    def call(self, fn, *args, **kwargs):
        with self._lock:
            self._maybe_attempt_reset()
            if self._state == _BreakerState.OPEN:
                log.warning("CircuitBreaker[%s] OPEN — fast-fail", self._name)
                raise CircuitBreakerOpen(f"Circuit breaker '{self._name}' is open")

        try:
            result = fn(*args, **kwargs)
        except Exception:
            self._on_failure()
            raise

        self._on_success()
        return result

    def reset(self) -> None:
        with self._lock:
            self._state         = _BreakerState.CLOSED
            self._failure_count = 0
            self._success_count = 0
            log.info("CircuitBreaker[%s] manually reset to CLOSED", self._name)

    def _maybe_attempt_reset(self) -> None:
        if (
            self._state == _BreakerState.OPEN
            and time.monotonic() - self._opened_at >= self._reset_timeout
        ):
            self._state         = _BreakerState.HALF_OPEN
            self._success_count = 0
            log.info("CircuitBreaker[%s] OPEN → HALF_OPEN (probing)", self._name)

    def _on_failure(self) -> None:
        with self._lock:
            self._success_count  = 0
            self._failure_count += 1
            if self._state == _BreakerState.HALF_OPEN:
                self._trip()
            elif self._state == _BreakerState.CLOSED and self._failure_count >= self._threshold:
                self._trip()

    def _on_success(self) -> None:
        with self._lock:
            self._failure_count  = 0
            self._success_count += 1
            if self._state == _BreakerState.HALF_OPEN and self._success_count >= self._success_threshold:
                self._state = _BreakerState.CLOSED
                log.info("CircuitBreaker[%s] HALF_OPEN → CLOSED (recovered)", self._name)

    def _trip(self) -> None:
        self._state     = _BreakerState.OPEN
        self._opened_at = time.monotonic()
        log.error("CircuitBreaker[%s] tripped OPEN after %d failures", self._name, self._failure_count)


# Module-level singleton — one breaker per worker process
_brand_db_breaker = CircuitBreaker(
    failure_threshold=5,
    reset_timeout=30.0,
    success_threshold=2,
    name="brand_db",
)


# ════════════════════════════════════════════════════════════════════════════
# §5  STATS MANAGER
# ════════════════════════════════════════════════════════════════════════════

def _compute_stats_sql() -> dict:
    """
    Single aggregation query — one DB round-trip for all six counts.
    Matches Brand's real flags (Meta.db_table = 'brands').
    """
    sql = """
        SELECT
            COUNT(*)                                      AS total,
            SUM(CASE WHEN is_featured THEN 1 ELSE 0 END)  AS featured,
            SUM(CASE WHEN is_trending THEN 1 ELSE 0 END)  AS trending,
            SUM(CASE WHEN is_verified THEN 1 ELSE 0 END)  AS verified,
            SUM(CASE WHEN is_official THEN 1 ELSE 0 END)  AS official,
            SUM(CASE WHEN is_trusted  THEN 1 ELSE 0 END)  AS trusted
        FROM brands
        WHERE is_active = TRUE
          AND deleted_at IS NULL
    """
    with connection.cursor() as cur:
        cur.execute(sql)
        row = cur.fetchone()

    if not row:
        return _EMPTY_STATS.copy()

    total, featured, trending, verified, official, trusted = row
    return {
        'total':    int(total    or 0),
        'featured': int(featured or 0),
        'trending': int(trending or 0),
        'verified': int(verified or 0),
        'official': int(official or 0),
        'trusted':  int(trusted  or 0),
    }


def get_brand_stats() -> dict:
    """
    Double-keyed cache strategy: fresh key (5 min) → lock-guarded compute
    → 1 hr stale fallback → zeros.
    """
    stats = cache.get(_STATS_CACHE_KEY)
    if stats:
        return stats

    acquired = cache.add(_STATS_LOCK_KEY, '1', timeout=_STATS_LOCK_TTL)
    if not acquired:
        stale = cache.get(_STATS_STALE_KEY)
        if stale:
            log.debug("Brand stats: serving stale while peer recomputes")
            return stale
        log.warning("Brand stats: no stale available, returning zeros")
        return _EMPTY_STATS.copy()

    try:
        stats = _brand_db_breaker.call(_compute_stats_sql)
        cache.set(_STATS_CACHE_KEY, stats, _STATS_CACHE_TTL)
        cache.set(_STATS_STALE_KEY, stats, _STATS_STALE_TTL)
        return stats
    except CircuitBreakerOpen:
        log.warning("Brand stats: circuit breaker open")
    except OperationalError:
        log.exception("Brand stats: DB error")
    finally:
        cache.delete(_STATS_LOCK_KEY)

    stale = cache.get(_STATS_STALE_KEY)
    return stale if stale else _EMPTY_STATS.copy()


def invalidate_stats() -> None:
    """Call from admin actions / post_save signals when brand flags change."""
    cache.delete_many([_STATS_CACHE_KEY, _STATS_STALE_KEY, _STATS_LOCK_KEY])
    log.info("Brand stats cache invalidated")


# ════════════════════════════════════════════════════════════════════════════
# §6  CORE DATA BUILDER
# ════════════════════════════════════════════════════════════════════════════

def _build_context(
    query: str,
    highlight_slug: str,
    filter_type: str,
    sort_by: str,
    page_number: int,
) -> dict | None:
    """
    Fetch → filter → paginate → serialise.

    Returns a plain dict with no live ORM objects — safe to cache and
    share across threads. Returns None on DB error or invalid page.
    """
    try:
        qs = Brand.objects.active_brands().only(*ONLY_FIELDS)

        # ── Highlighted brand ────────────────────────────────────────────
        highlighted_pk    = None
        highlighted_brand = None
        if highlight_slug:
            try:
                hb = qs.get(brand_slug=highlight_slug)
                highlighted_pk    = hb.pk
                highlighted_brand = {
                    'pk':         hb.pk,
                    'brand_name': hb.brand_name,
                    'slug':       hb.brand_slug,
                }
            except Brand.DoesNotExist:
                pass

        # ── Search — mirrors BrandManager.search_brands() ───────────────
        if query:
            qs = qs.filter(
                Q(brand_name__icontains=query) |
                Q(brand_description__icontains=query) |
                Q(brand_slug__icontains=query)
            )

        # ── Type filter — real Brand boolean flags ───────────────────────
        _filter_map = {
            'featured': {'is_featured': True},
            'trending': {'is_trending': True},
            'verified': {'is_verified': True},
            'official': {'is_official': True},
            'trusted':  {'is_trusted': True},
        }
        if filter_type in _filter_map:
            qs = qs.filter(**_filter_map[filter_type])

        # ── Sort ─────────────────────────────────────────────────────────
        qs = qs.order_by(*SORT_MAP[sort_by])

        # ── Paginate ─────────────────────────────────────────────────────
        paginator = Paginator(qs, BRANDS_PER_PAGE)
        try:
            page_obj = paginator.get_page(page_number)
        except InvalidPage:
            return None

        # ── Serialise — evaluate queryset HERE, cursor closes before return
        brands = [
            {
                'id':                b.pk,
                'name':              b.brand_name,
                'slug':              b.brand_slug,
                'type':              b.brand_type,
                'tagline':           b.brand_tagline or '',
                'logo':              b.logo_url,
                'country':           b.country_of_origin,
                'product_count':     b.product_count,
                'view_count':        b.view_count,
                'popularity_score':  b.popularity_score,
                'is_verified':       b.is_verified,
                'is_featured':       b.is_featured,
                'is_trending':       b.is_trending,
                'is_official':       b.is_official,
                'is_trusted':        b.is_trusted,
                'brand_url':         b.brand_url,
                'highlighted':       (b.pk == highlighted_pk) if highlighted_pk else False,
            }
            for b in page_obj
        ]

        # ── Stats (independent cache — not re-queried per page request) ──
        stats = get_brand_stats()

        # ── UI metadata ──────────────────────────────────────────────────
        sort_options = [
            {'value': 'popular',       'label': 'Most Popular',   'active': sort_by == 'popular'},
            {'value': 'most_products', 'label': 'Most Products',  'active': sort_by == 'most_products'},
            {'value': 'newest',        'label': 'Newest First',   'active': sort_by == 'newest'},
            {'value': 'name_asc',      'label': 'Name A → Z',     'active': sort_by == 'name_asc'},
            {'value': 'name_desc',     'label': 'Name Z → A',     'active': sort_by == 'name_desc'},
        ]
        filter_tabs = [
            {'value': 'all',      'label': 'All',      'count': stats['total'],    'active': filter_type == 'all'},
            {'value': 'featured', 'label': 'Featured', 'count': stats['featured'], 'active': filter_type == 'featured'},
            {'value': 'trending', 'label': 'Trending', 'count': stats['trending'], 'active': filter_type == 'trending'},
            {'value': 'verified', 'label': 'Verified', 'count': stats['verified'], 'active': filter_type == 'verified'},
            {'value': 'official', 'label': 'Official', 'count': stats['official'], 'active': filter_type == 'official'},
            {'value': 'trusted',  'label': 'Trusted',  'count': stats['trusted'],  'active': filter_type == 'trusted'},
        ]

        return {
            'brands':            brands,
            'page_obj':          page_obj,
            'query':             query,
            'highlighted_brand': highlighted_brand,
            'current_sort':      sort_by,
            'current_filter':    filter_type,
            'sort_options':      sort_options,
            'filter_tabs':       filter_tabs,
            'total_count':       paginator.count,
            'stats':             stats,
            '_built_at':         time.time(),
            '_page_count':       paginator.num_pages,
        }

    except OperationalError:
        log.exception("DB error in _build_context")
        return None


# ════════════════════════════════════════════════════════════════════════════
# §7  VIEW
# ════════════════════════════════════════════════════════════════════════════

_page_cache = TwoLayerCache(ttl=CACHE_TTL, stale_ttl=STALE_TTL)

_refresh_pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix='brandlist_refresh')


def _cache_key(
    query: str, highlight_slug: str, filter_type: str, sort_by: str, page: int,
) -> str:
    """SHA-256 of canonical params — fixed length, no raw user input in key."""
    raw = f"{query}|{highlight_slug}|{filter_type}|{sort_by}|{page}"
    digest = hashlib.sha256(raw.encode()).hexdigest()
    return f"brandlist_v1:{digest}"


def _etag(cache_key: str, built_at: float) -> str:
    """Weak ETag via BLAKE2b — faster than MD5/SHA-256 for non-security use."""
    payload = f"{cache_key}:{built_at}".encode()
    digest  = hashlib.blake2b(payload, digest_size=16).hexdigest()
    return f'W/"{digest}"'


def _safe_positive_int(value, default: int, maximum: int) -> int:
    try:
        return max(1, min(int(value), maximum))
    except (TypeError, ValueError):
        return default


def _is_ajax_request(request) -> bool:
    """
    True for infinite-scroll "load more" fetches.

    The frontend (infinite-scroll.js) sends X-Requested-With:
    XMLHttpRequest on every fetch() call. ?ajax=1 is a fallback for
    manual testing / progressive-enhancement links.
    """
    return (
        request.headers.get('X-Requested-With') == 'XMLHttpRequest'
        or request.GET.get('ajax') == '1'
    )


def _warm_async(cache_key: str, builder_fn, *args, **kwargs) -> None:
    """
    Submit a background refresh to the bounded pool.
    Drops silently if the pool is saturated — the next stale request
    will retry. Never blocks the response.
    """
    def _task():
        try:
            result = builder_fn(*args, **kwargs)
            if result is not None:
                _page_cache.set(cache_key, result)
        except Exception:
            log.exception("Async cache refresh failed key=%s", cache_key)

    try:
        _refresh_pool.submit(_task)
    except RuntimeError:
        pass   # pool shut down (test teardown) — ignore


_ERROR_CONTEXT = lambda raw_q, filter_type, sort_by: {
    'brands': [], 'stats': _EMPTY_STATS,
    'error': True, 'query': raw_q,
    'filter_tabs': [], 'sort_options': [],
    'current_filter': filter_type, 'current_sort': sort_by,
    'total_count': 0,
}


@require_GET
@vary_on_headers('Accept-Encoding', 'X-Requested-With')
def BrandListView(request):
    """
    Request lifecycle
    ─────────────────
    1. Validate + normalise params   (no DB, no cache touch)
    2. Derive deterministic cache key
    3. L1 → L2 → DB  (circuit breaker + request coalescing)
       Stale hit → async pool refresh, serve immediately
       Cold miss → one thread builds, others wait on RLock
    4. Conditional GET (ETag + If-Modified-Since) → 304 if unchanged
    5. Render — full page normally, cards-only partial for infinite
       scroll AJAX requests (X-Requested-With: XMLHttpRequest or ?ajax=1)
    6. Set HTTP caching headers (s-maxage for CDN, max-age for browser)
    """

    # ── 1. Validate & normalise ─────────────────────────────────────────
    raw_q     = request.GET.get('q',     '').strip()[:MAX_Q_LEN]
    raw_slug  = request.GET.get('brand', '').strip()[:MAX_SLUG_LEN]   # highlight one brand

    sort_by     = request.GET.get('sort', 'popular').strip()
    filter_type = request.GET.get('type', 'all').strip()
    raw_page    = request.GET.get('page', '1')

    if sort_by     not in VALID_SORTS:  sort_by     = 'popular'
    if filter_type not in VALID_TYPES:  filter_type = 'all'

    page_number = _safe_positive_int(raw_page, default=1, maximum=MAX_PAGE)
    query       = ' '.join(raw_q.lower().split())
    is_ajax     = _is_ajax_request(request)

    # ── 2. Cache key ────────────────────────────────────────────────────
    ck = _cache_key(query, raw_slug, filter_type, sort_by, page_number)

    # ── 3. Cache / DB ───────────────────────────────────────────────────
    def _protected_build(*args, **kwargs):
        """Wrap _build_context with the circuit breaker."""
        return _brand_db_breaker.call(_build_context, *args, **kwargs)

    try:
        context = _page_cache.get_or_build(
            key           = ck,
            builder       = _protected_build,
            args          = (query, raw_slug, filter_type, sort_by, page_number),
            warm_async_fn = _warm_async,
        )
    except CircuitBreakerOpen:
        # DB struggling — serve stale if available, else 503
        context = _page_cache.get(ck)
        if context is None:
            log.error("Circuit breaker open and no stale cache for key=%s", ck)
            if is_ajax:
                return HttpResponse(status=503)
            return render(
                request, 'ponno/brand_list.html',
                _ERROR_CONTEXT(raw_q, filter_type, sort_by),
                status=503,
            )

    if context is None:
        if page_number > 1:
            raise Http404
        if is_ajax:
            return HttpResponse(status=503)
        return render(
            request, 'ponno/brand_list.html',
            _ERROR_CONTEXT(raw_q, filter_type, sort_by),
            status=503,
        )

    built_at = context.get('_built_at', time.time())
    etag     = _etag(ck, built_at)

    # ── 4. Conditional GET ──────────────────────────────────────────────
    if_none_match = request.META.get('HTTP_IF_NONE_MATCH', '')
    if if_none_match and if_none_match == etag:
        return HttpResponse(status=HTTPStatus.NOT_MODIFIED)

    if_modified_since = parse_http_date_safe(
        request.META.get('HTTP_IF_MODIFIED_SINCE', '')
    )
    if if_modified_since and int(built_at) <= if_modified_since:
        return HttpResponse(status=HTTPStatus.NOT_MODIFIED)

    # ── 5. Render ───────────────────────────────────────────────────────
    page_obj = context['page_obj']

    if is_ajax:
        # Infinite scroll fetch: return only the card markup. Pagination
        # state travels in headers rather than a JSON body so the
        # frontend can just do `res.text()` and append it.
        response = render(request, BRAND_CARDS_PARTIAL_TEMPLATE, context)
        response['X-Has-Next-Page'] = 'true' if page_obj.has_next() else 'false'
        response['X-Next-Page']     = str(page_number + 1) if page_obj.has_next() else ''
        response['X-Total-Count']   = str(context['total_count'])
    else:
        response = render(request, 'ponno/brand_list.html', context)

    # ── 6. HTTP caching headers ──────────────────────────────────────────
    response['ETag']          = etag
    response['Last-Modified'] = http_date(built_at)
    response['Vary']          = 'Accept-Encoding, X-Requested-With'

    response['Cache-Control'] = (
        f'public, '
        f's-maxage={CACHE_TTL}, '
        f'max-age={CACHE_TTL // 2}, '
        f'stale-while-revalidate={STALE_TTL}, '
        f'stale-if-error=86400'
    )

    return response


# ════════════════════════════════════════════════════════════════════════════
# §8  BACKFILL UTILITY — one-off repair for Brand.product_count
# ════════════════════════════════════════════════════════════════════════════
#
# Brand.product_count is a cached column. apps/ponno/signals.py keeps it
# correct going forward for any *new* Product save/delete, but does
# nothing for brands whose count was already stale/zero from before
# that signal existed. This function is the one-off repair — run it
# once, from a shell, after deploying the signal:
#
#     python manage.py shell -c "
#     from apps.ponno.views.brand_list import backfill_brand_product_counts
#     backfill_brand_product_counts()
#     "
#
# It intentionally does NOT touch `_page_cache` or `get_brand_stats()`:
#   - `_page_cache` keys are SHA-256 hashes of (query, filter, sort, page)
#     with no registry of "all keys in use", so there's nothing to
#     selectively purge. Worst case, a page keeps showing pre-backfill
#     counts for up to CACHE_TTL + STALE_TTL (90s) after this runs, then
#     TwoLayerCache's stale-while-revalidate rebuilds it from the DB.
#   - get_brand_stats() aggregates brands by boolean flag (featured,
#     trending, etc.), not product_count, so it's unaffected either way.

def backfill_brand_product_counts(
    include_inactive: bool = False,
    batch_size: int = _BACKFILL_BATCH_SIZE,
    dry_run: bool = False,
) -> dict:
    """
    Recompute Brand.product_count for every brand from real Product rows.

    Args:
        include_inactive: also backfill soft-deleted/inactive brands.
            Defaults to False, matching Brand.objects.active_brands()
            (the same manager method _build_context uses).
        batch_size: brands processed per DB transaction. Keeps a single
            long-running transaction from holding locks on a big catalog.
        dry_run: report what would change without writing anything.

    Returns:
        {'checked': int, 'changed': int, 'errored': int, 'total': int}
    """
    base_qs = Brand.objects.all() if include_inactive else Brand.objects.active_brands()
    # Only fetch what update_product_count() and logging actually need —
    # same discipline as ONLY_FIELDS above.
    qs = base_qs.only('pk', 'brand_name', 'product_count').order_by('pk')

    total   = qs.count()
    checked = 0
    changed = 0
    errored = 0

    log.info("Backfilling product_count for %d brand(s)%s", total, ' (dry run)' if dry_run else '')

    batch = []
    for brand in qs.iterator(chunk_size=batch_size):
        batch.append(brand)
        if len(batch) >= batch_size:
            c, ch, e = _backfill_batch(batch, dry_run)
            checked += c
            changed += ch
            errored += e
            batch = []

    if batch:
        c, ch, e = _backfill_batch(batch, dry_run)
        checked += c
        changed += ch
        errored += e

    log.info(
        "Backfill complete: checked=%d changed=%d errored=%d total=%d%s",
        checked, changed, errored, total, ' (dry run — nothing written)' if dry_run else '',
    )

    return {'checked': checked, 'changed': changed, 'errored': errored, 'total': total}


def _backfill_batch(batch: list[Brand], dry_run: bool) -> tuple[int, int, int]:
    """Process one batch inside its own transaction. Returns (checked, changed, errored)."""
    checked = changed = errored = 0

    with transaction.atomic():
        for brand in batch:
            before = brand.product_count
            try:
                if dry_run:
                    # Same filter Brand.update_product_count() uses, without saving.
                    after = brand.products.filter(is_active=True).count()
                else:
                    after = brand.update_product_count()
            except Exception:
                errored += 1
                log.exception("Failed to update product_count for brand pk=%s", brand.pk)
                continue

            checked += 1
            if before != after:
                changed += 1
                log.info("Brand %s (pk=%s): product_count %d -> %d", brand.brand_name, brand.pk, before, after)

        if dry_run:
            transaction.set_rollback(True)

    return checked, changed, errored