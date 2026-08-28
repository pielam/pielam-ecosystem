# backend/apps/ponno/views/category_list.py
"""
CategoryListView — Production-grade, high-traffic category listing view.

Brought up to parity with apps/ponno/views/brand_list.py: same L1+L2
cache, circuit breaker, single-SQL stats manager, infinite-scroll
handling, and backfill-utility conventions — just pointed at Category
instead of Brand.

Architecture (all in one file, sectioned — mirrors brand_list.py)
──────────────────────────────────────────────────────────────────
§1  Constants & config
§2  L1 in-process LRU cache      (_L1Cache)
§3  Two-layer cache              (TwoLayerCache)  L1 + Redis
§4  Circuit breaker              (CircuitBreaker) DB cascade protection
§5  Stats manager                (get_category_stats) single-SQL aggregation
§5b Hierarchical rollup          (_total_product_count_for_path)
§6  Core data builder            (_build_context)
§7  View                         (CategoryListView) — full page + infinite scroll
§8  Backfill utility             (backfill_category_product_counts)

What's new vs. the old version
────────────────────────────────
- **Total product count per category.** Each card now carries
  `total_product_count` — the sum of `product_count` across the
  category *and every active descendant*, not just its own direct
  count. `Category.get_total_product_count()` on the model computes
  this by recursively walking `get_descendants()`, which is one ORM
  query per descendant node — fine for a single detail page, much too
  slow to call 24 times per listing page. §5b instead does it with one
  small aggregate query per category using the materialized `path`
  column (`path = X OR path LIKE 'X/%'`), which is exact and O(1)
  queries per category regardless of how many descendants it has.
  NOTE: `Category.path` has no `db_index=True` on the model yet — for
  large catalogs it's worth adding one (see the model file); this view
  works correctly without it, just not maximally fast.
- **L1 (in-process) + L2 (Redis) two-layer cache** with request
  coalescing and stale-while-revalidate, replacing the old single
  `django.core.cache` get/set. A cold cache no longer lets N concurrent
  requests all hit the DB at once — the first thread builds, the rest
  wait on a per-key lock.
- **Circuit breaker** around all DB access (`_category_db_breaker`),
  so a struggling DB fails fast and falls back to stale cache / a 503
  instead of piling up slow queries.
- **Stats manager** (`get_category_stats`) — single aggregation query
  (like `get_brand_stats`) instead of four separate `.count()` calls,
  with its own fresh/stale cache tier and lock-guarded recompute.
- **Infinite scroll.** AJAX "load more" requests (X-Requested-With:
  XMLHttpRequest, or `?ajax=1`) render only the category cards, with
  pagination state in response headers (X-Has-Next-Page, X-Next-Page,
  X-Total-Count), same contract as BrandListView. `Vary` includes
  `X-Requested-With` so a shared cache never mixes up a partial with a
  full page at the same URL.
  NOTE: this assumes `ponno/category_list.html` grows the same
  request.META-gated AJAX branch that `ponno/brand_list.html` has (bare
  `<li class="category-card">` fragment vs. the full document). If the
  template doesn't have that branch yet, infinite scroll will still
  work — it'll just fetch and re-parse the full page HTML per "page"
  instead of a lightweight fragment.
- Conditional GET (ETag / If-Modified-Since) → 304s, same as brand.
- Everything from the old view is preserved: search, ?parent=<slug>
  hierarchical browsing, ?category=<slug> highlighting, breadcrumbs.
- **Cache invalidation (§5c).** `invalidate_category_cache(instance)`
  is called from apps/ponno/signals.py's `clear_category_cache`
  receiver on every Category post_save/post_delete — including the
  product_count/view_count updates that ripple through Category.save()
  — so listing pages never serve counts more than one write-cycle
  stale. See §5c docstring for exactly what it does and doesn't do.

Product count backfill (§8)
────────────────────────────
Category.product_count is a cached column. A signal keeps it correct
going forward for Product saves/deletes (see apps/ponno/signals.py's
brand/category handling); this is the one-off repair for categories
whose count predates that signal:

    python manage.py shell -c "
    from apps.ponno.views.category_list import backfill_category_product_counts
    backfill_category_product_counts()
    "

This does not need to touch total_product_count anywhere — that's
never cached on the model, it's computed fresh (within CACHE_TTL +
STALE_TTL) every time §5b runs.
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
from django.db.models import Q, Sum
from django.http import Http404, HttpResponse
from django.shortcuts import render
from django.urls import reverse
from django.utils.http import http_date, parse_http_date_safe
from django.views.decorators.http import require_GET
from django.views.decorators.vary import vary_on_headers

from apps.ponno.models.category import Category

log = logging.getLogger(__name__)


# ════════════════════════════════════════════════════════════════════════════
# §1  CONSTANTS & CONFIG
# ════════════════════════════════════════════════════════════════════════════

CATEGORIES_PER_PAGE = 24
CACHE_TTL            = 60      # seconds — fresh window
STALE_TTL            = 30      # seconds — serve stale while refreshing
MAX_PAGE             = 500
MAX_Q_LEN            = 100
MAX_SLUG_LEN         = 160     # Category.category_slug is max_length=160

VALID_SORTS = frozenset({
    'popular', 'newest', 'name_asc', 'name_desc', 'order',
})
VALID_FILTERS = frozenset({
    'all', 'featured', 'trending', 'root', 'menu',
})

SORT_MAP = {
    'popular':   ('-popularity_score', '-product_count'),
    'newest':    ('-category_created_at',),
    'name_asc':  ('category_name',),
    'name_desc': ('-category_name',),
    'order':     ('display_order', 'category_name'),
}

# Fields actually needed to render a category card + breadcrumb parent.
# Parent fields are listed explicitly so select_related().only() doesn't
# silently trigger a second query per row when the template touches
# cat.parent.category_name / cat.parent.category_slug.
ONLY_FIELDS = (
    'pk', 'uuid', 'category_name', 'category_slug',
    'category_short_description', 'category_image', 'category_icon',
    'category_type', 'color_code', 'icon_class',
    'parent', 'parent__category_name', 'parent__category_slug',
    'level', 'path',
    'is_featured', 'is_trending', 'is_visible_in_menu',
    'product_count', 'view_count', 'popularity_score',
    'display_order', 'category_created_at',
)

# L1 cache limits
_L1_MAX_ENTRIES = 512
_L1_TTL         = 5

# Stats cache keys & TTLs
_STATS_CACHE_KEY = 'categorylist_stats_v1'
_STATS_LOCK_KEY  = 'categorylist_stats_lock_v1'
_STATS_STALE_KEY = 'categorylist_stats_stale_v1'
_STATS_CACHE_TTL = 300
_STATS_STALE_TTL = 3600
_STATS_LOCK_TTL  = 15

# Page-cache version key — bumped by invalidate_category_cache() so
# every _cache_key() computed afterwards is new. See §5c.
_CACHE_VERSION_KEY = 'categorylist_cache_version_v1'

_EMPTY_STATS: dict = {
    'total': 0, 'featured': 0, 'trending': 0, 'root': 0, 'menu': 0,
}

# Backfill defaults (§8)
_BACKFILL_BATCH_SIZE = 200

# Infinite scroll (§7) — same template renders both full page + partial,
# branching internally on request.META.HTTP_X_REQUESTED_WITH (see brand_list.html).
CATEGORY_LIST_TEMPLATE = 'ponno/category_list.html'


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
                _coalesce_locks.clear()
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
_category_db_breaker = CircuitBreaker(
    failure_threshold=5,
    reset_timeout=30.0,
    success_threshold=2,
    name="category_db",
)


# ════════════════════════════════════════════════════════════════════════════
# §5  STATS MANAGER
# ════════════════════════════════════════════════════════════════════════════

def _compute_stats_sql() -> dict:
    """
    Single aggregation query — one DB round-trip for all five counts.
    Matches Category's real flags (Meta.db_table = 'categories').
    """
    sql = """
        SELECT
            COUNT(*)                                            AS total,
            SUM(CASE WHEN is_featured THEN 1 ELSE 0 END)        AS featured,
            SUM(CASE WHEN is_trending THEN 1 ELSE 0 END)        AS trending,
            SUM(CASE WHEN parent_id IS NULL THEN 1 ELSE 0 END)  AS root,
            SUM(CASE WHEN is_visible_in_menu THEN 1 ELSE 0 END) AS menu
        FROM categories
        WHERE is_active = TRUE
          AND deleted_at IS NULL
    """
    with connection.cursor() as cur:
        cur.execute(sql)
        row = cur.fetchone()

    if not row:
        return _EMPTY_STATS.copy()

    total, featured, trending, root, menu = row
    return {
        'total':    int(total    or 0),
        'featured': int(featured or 0),
        'trending': int(trending or 0),
        'root':     int(root     or 0),
        'menu':     int(menu     or 0),
    }


def get_category_stats() -> dict:
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
            log.debug("Category stats: serving stale while peer recomputes")
            return stale
        log.warning("Category stats: no stale available, returning zeros")
        return _EMPTY_STATS.copy()

    try:
        stats = _category_db_breaker.call(_compute_stats_sql)
        cache.set(_STATS_CACHE_KEY, stats, _STATS_CACHE_TTL)
        cache.set(_STATS_STALE_KEY, stats, _STATS_STALE_TTL)
        return stats
    except CircuitBreakerOpen:
        log.warning("Category stats: circuit breaker open")
    except OperationalError:
        log.exception("Category stats: DB error")
    finally:
        cache.delete(_STATS_LOCK_KEY)

    stale = cache.get(_STATS_STALE_KEY)
    return stale if stale else _EMPTY_STATS.copy()


def invalidate_stats() -> None:
    """Call from admin actions / post_save signals when category flags change."""
    cache.delete_many([_STATS_CACHE_KEY, _STATS_STALE_KEY, _STATS_LOCK_KEY])
    log.info("Category stats cache invalidated")


# ════════════════════════════════════════════════════════════════════════════
# §5c  PAGE-CACHE INVALIDATION — called from apps/ponno/signals.py
# ════════════════════════════════════════════════════════════════════════════

def _get_cache_version() -> int:
    """
    Current cache "epoch" for _cache_key(). Bumped by
    invalidate_category_cache() whenever a category is written to, so
    every _cache_key() call after a write produces fresh keys — old
    Redis/L1 entries are simply never looked up again and expire on
    their own TTL instead of needing an explicit per-key delete (which
    isn't possible anyway — see invalidate_category_cache's docstring).
    """
    version = cache.get(_CACHE_VERSION_KEY)
    if version is None:
        cache.set(_CACHE_VERSION_KEY, 1, None)  # no expiry
        return 1
    return version


def _bump_cache_version() -> None:
    try:
        cache.incr(_CACHE_VERSION_KEY)
    except ValueError:
        # Key didn't exist yet (e.g. cache was flushed) — seed it.
        cache.set(_CACHE_VERSION_KEY, 2, None)


def invalidate_category_cache(instance) -> None:
    """
    Call this from signals whenever a Category is created, updated
    (product_count / view_count / any other field), or deleted.
    Mirrors invalidate_product_cache(instance) for Product.

    What it does:
    - Bumps the cache version so every _cache_key() computed from now
      on is new — old page-cache entries (L1 + Redis) are orphaned and
      simply expire on their own TTL (CACHE_TTL + STALE_TTL, 90s)
      rather than needing per-key deletion.
    - Clears the in-process L1 cache immediately, since L1 is
      process-local and cheap to drop entirely (worst case: a few
      extra DB hits in this one worker until it repopulates).
    - Invalidates the aggregate stats cache (get_category_stats()),
      since is_featured / is_trending / parent / is_visible_in_menu
      flips would otherwise show stale tab counts for up to 5 minutes.

    NOTE: this does not attempt to delete specific Redis keys from
    prior versions — there's no registry mapping "category X -> its
    cache keys" (keys are opaque SHA-256 hashes of query params), so a
    targeted Redis delete isn't possible without one. The version bump
    makes targeted deletion unnecessary: stale entries are simply
    unreachable going forward and fall out of Redis on their own TTL.
    """
    _bump_cache_version()
    _l1.clear()
    invalidate_stats()

    log.info(
        "Category cache invalidated (category_id=%s, slug=%s)",
        getattr(instance, "pk", None),
        getattr(instance, "category_slug", None),
    )


# ════════════════════════════════════════════════════════════════════════════
# §5b  HIERARCHICAL ROLLUP — total product count including descendants
# ════════════════════════════════════════════════════════════════════════════

def _total_product_count_for_path(path: str) -> int:
    """
    Sum of `product_count` across this category and every active,
    non-deleted descendant — in a single aggregate query.

    Deliberately does NOT call Category.get_total_product_count(): that
    model method recurses through get_descendants(), which is one ORM
    query per descendant node. Fine to call once on a category detail
    page; far too slow to call once per card on a 24-item listing page.

    Instead this uses the materialized `path` column that
    Category.update_level() already maintains on every save
    (e.g. "/electronics/laptops/gaming"). A category's full subtree is
    exactly the set of active categories whose path equals its own path
    or starts with "<path>/" — the trailing slash matters, otherwise
    "/electronics" would wrongly match a sibling like
    "/electronics-accessories".

    NOTE: Category.path has no explicit `db_index=True` yet on the
    model. This still returns the correct result without one — it's
    just a full-table LIKE scan rather than an index range scan. Worth
    adding an index on `path` if the categories table gets large.
    """
    if not path:
        return 0

    result = Category.objects.filter(
        Q(path=path) | Q(path__startswith=f"{path}/"),
        is_active=True,
        deleted_at__isnull=True,
    ).aggregate(total=Sum('product_count'))

    return int(result['total'] or 0)


# ════════════════════════════════════════════════════════════════════════════
# §6  CORE DATA BUILDER
# ════════════════════════════════════════════════════════════════════════════

def _category_url(category: Category) -> str:
    """
    Build a category's page URL from urls.py (single source of truth),
    instead of relying on the model's hardcoded `category_url` property.

    Defensively regenerates the slug if it's missing/blank, since
    category_slug is nullable/blank on the model and reverse() will
    raise NoReverseMatch on an empty string.
    """
    slug = category.category_slug
    if not slug:
        slug = category.generate_slug(save=True)
    return reverse('ponno:category_products', kwargs={'category_slug': slug})


def _build_context(
    query: str,
    highlight_slug: str,
    parent_slug: str,
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
        qs = (
            Category.objects
            .active_categories()
            .select_related('parent')
            .only(*ONLY_FIELDS)
        )

        # ── Highlighted category (e.g. from a discovery filter chip) ─────
        highlighted_pk       = None
        highlighted_category = None
        if highlight_slug:
            try:
                hc = qs.get(category_slug=highlight_slug)
                highlighted_pk       = hc.pk
                highlighted_category = {
                    'pk':            hc.pk,
                    'category_name': hc.category_name,
                    'slug':          hc.category_slug,
                }
            except Category.DoesNotExist:
                pass
            except Category.MultipleObjectsReturned:
                # category_slug is unique=True on the model so this
                # shouldn't happen, but guard anyway rather than 500ing.
                hc = qs.filter(category_slug=highlight_slug).first()
                if hc:
                    highlighted_pk       = hc.pk
                    highlighted_category = {
                        'pk':            hc.pk,
                        'category_name': hc.category_name,
                        'slug':          hc.category_slug,
                    }

        # ── Parent filter (browse children) + breadcrumb ─────────────────
        parent_category_obj  = None   # live object, used only to build the chain below
        parent_category_dict = None
        breadcrumb            = []
        if parent_slug:
            try:
                parent_category_obj = Category.objects.active_categories().only(
                    'pk', 'category_name', 'category_slug', 'parent', 'path'
                ).get(category_slug=parent_slug)
            except Category.DoesNotExist:
                parent_category_obj = None
            except Category.MultipleObjectsReturned:
                parent_category_obj = Category.objects.active_categories().only(
                    'pk', 'category_name', 'category_slug', 'parent', 'path'
                ).filter(category_slug=parent_slug).first()

            if parent_category_obj:
                qs = qs.filter(parent=parent_category_obj)
                parent_category_dict = {
                    'pk':            parent_category_obj.pk,
                    'category_name': parent_category_obj.category_name,
                    'slug':          parent_category_obj.category_slug,
                }

                # Walk the ancestor chain (bounded to 10 levels by the
                # model's own level constraint) to build the breadcrumb.
                chain = []
                node = parent_category_obj
                while node:
                    chain.insert(0, node)
                    node = node.parent
                breadcrumb = [
                    {
                        'name': n.category_name,
                        'slug': n.category_slug,
                        'url':  _category_url(n),
                    }
                    for n in chain
                ]

        # ── Search ─────────────────────────────────────────────────────
        if query:
            qs = qs.filter(
                Q(category_name__icontains=query) |
                Q(category_description__icontains=query) |
                Q(category_slug__icontains=query) |
                Q(category_short_description__icontains=query)
            )

        # ── Type filter ────────────────────────────────────────────────
        _filter_map = {
            'featured': {'is_featured': True},
            'trending': {'is_trending': True},
            'root':     {'parent__isnull': True},
            'menu':     {'is_visible_in_menu': True},
        }
        if filter_type in _filter_map:
            qs = qs.filter(**_filter_map[filter_type])

        # ── Sort ───────────────────────────────────────────────────────
        qs = qs.order_by(*SORT_MAP[sort_by]).distinct()  # search can duplicate rows

        # ── Paginate ───────────────────────────────────────────────────
        paginator = Paginator(qs, CATEGORIES_PER_PAGE)
        try:
            page_obj = paginator.get_page(page_number)
        except InvalidPage:
            return None

        # ── Serialise — evaluate queryset HERE, cursor closes before return
        categories = [
            {
                'id':                 c.pk,
                'uuid':               str(c.uuid),
                'name':               c.category_name,
                'slug':               c.category_slug,
                'short_description':  c.category_short_description or '',
                'image':              c.image_url,
                'icon':               c.icon_url,
                'icon_class':         c.icon_class or '',
                'color_code':         c.color_code or '',
                'type':               c.category_type,
                'level':              c.level,
                'is_root':            c.parent_id is None,
                'parent_name':        c.parent.category_name if c.parent_id else '',
                'parent_slug':        c.parent.category_slug if c.parent_id else '',
                'is_featured':        c.is_featured,
                'is_trending':        c.is_trending,
                'is_visible_in_menu': c.is_visible_in_menu,
                # Direct count on this category only.
                'product_count':       c.product_count,
                # Rolled-up count across this category + all active
                # descendants — see §5b for why this isn't
                # c.get_total_product_count().
                'total_product_count': _total_product_count_for_path(c.path),
                'view_count':          c.view_count,
                'popularity_score':    c.popularity_score,
                'has_children':        c.has_children,
                'child_count':         c.child_count,
                'category_url':        _category_url(c),
                'highlighted':         (c.pk == highlighted_pk) if highlighted_pk else False,
            }
            for c in page_obj
        ]

        # ── Stats (independent cache — not re-queried per page request) ──
        stats = get_category_stats()

        # ── UI metadata ────────────────────────────────────────────────
        sort_options = [
            {'value': 'popular',   'label': 'Most Popular',  'active': sort_by == 'popular'},
            {'value': 'newest',    'label': 'Newest First',  'active': sort_by == 'newest'},
            {'value': 'name_asc',  'label': 'Name A → Z',    'active': sort_by == 'name_asc'},
            {'value': 'name_desc', 'label': 'Name Z → A',    'active': sort_by == 'name_desc'},
            {'value': 'order',     'label': 'Display Order', 'active': sort_by == 'order'},
        ]
        filter_tabs = [
            {'value': 'all',      'label': 'All',       'count': stats['total'],    'active': filter_type == 'all'},
            {'value': 'featured', 'label': 'Featured',  'count': stats['featured'], 'active': filter_type == 'featured'},
            {'value': 'trending', 'label': 'Trending',  'count': stats['trending'], 'active': filter_type == 'trending'},
            {'value': 'root',     'label': 'Top Level', 'count': stats['root'],     'active': filter_type == 'root'},
            {'value': 'menu',     'label': 'In Menu',   'count': stats['menu'],     'active': filter_type == 'menu'},
        ]

        return {
            'categories':           categories,
            'page_obj':             page_obj,
            'query':                query,
            'category_slug':        highlight_slug,
            'highlighted_category': highlighted_category,
            'parent_category':      parent_category_dict,
            'breadcrumb':           breadcrumb,
            'current_sort':         sort_by,
            'current_filter':       filter_type,
            'sort_options':         sort_options,
            'filter_tabs':          filter_tabs,
            'total_count':          paginator.count,
            'stats':                stats,
            '_built_at':            time.time(),
            '_page_count':          paginator.num_pages,
        }

    except OperationalError:
        log.exception("DB error in _build_context")
        return None


# ════════════════════════════════════════════════════════════════════════════
# §7  VIEW
# ════════════════════════════════════════════════════════════════════════════

_page_cache = TwoLayerCache(ttl=CACHE_TTL, stale_ttl=STALE_TTL)

_refresh_pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix='categorylist_refresh')


def _cache_key(
    query: str, highlight_slug: str, parent_slug: str,
    filter_type: str, sort_by: str, page: int,
) -> str:
    """
    SHA-256 of canonical params + current cache version — fixed length,
    no raw user input in key, and automatically "invalidated" whenever
    invalidate_category_cache() bumps the version (see §5c).
    """
    version = _get_cache_version()
    raw = f"{version}|{query}|{highlight_slug}|{parent_slug}|{filter_type}|{sort_by}|{page}"
    digest = hashlib.sha256(raw.encode()).hexdigest()
    return f"categorylist_v1:{digest}"


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
    See BrandListView._is_ajax_request — same contract.
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
    'categories': [], 'stats': _EMPTY_STATS,
    'error': True, 'query': raw_q,
    'filter_tabs': [], 'sort_options': [],
    'current_filter': filter_type, 'current_sort': sort_by,
    'total_count': 0,
    'highlighted_category': None, 'parent_category': None, 'breadcrumb': [],
}


@require_GET
@vary_on_headers('Accept-Encoding', 'X-Requested-With')
def CategoryListView(request):
    """
    Request lifecycle — identical shape to BrandListView.
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
    raw_q          = request.GET.get('q',        '').strip()[:MAX_Q_LEN]
    raw_category    = request.GET.get('category', '').strip()[:MAX_SLUG_LEN]   # highlight
    raw_parent      = request.GET.get('parent',   '').strip()[:MAX_SLUG_LEN]   # browse children

    sort_by     = request.GET.get('sort', 'popular').strip()
    filter_type = request.GET.get('type', 'all').strip()
    raw_page    = request.GET.get('page', '1')

    if sort_by     not in VALID_SORTS:   sort_by     = 'popular'
    if filter_type not in VALID_FILTERS: filter_type = 'all'

    page_number = _safe_positive_int(raw_page, default=1, maximum=MAX_PAGE)
    query       = ' '.join(raw_q.lower().split())
    is_ajax     = _is_ajax_request(request)

    # ── 2. Cache key ────────────────────────────────────────────────────
    ck = _cache_key(query, raw_category, raw_parent, filter_type, sort_by, page_number)

    # ── 3. Cache / DB ───────────────────────────────────────────────────
    def _protected_build(*args, **kwargs):
        """Wrap _build_context with the circuit breaker."""
        return _category_db_breaker.call(_build_context, *args, **kwargs)

    try:
        context = _page_cache.get_or_build(
            key           = ck,
            builder       = _protected_build,
            args          = (query, raw_category, raw_parent, filter_type, sort_by, page_number),
            warm_async_fn = _warm_async,
        )
    except CircuitBreakerOpen:
        context = _page_cache.get(ck)
        if context is None:
            log.error("Circuit breaker open and no stale cache for key=%s", ck)
            if is_ajax:
                return HttpResponse(status=503)
            return render(
                request, CATEGORY_LIST_TEMPLATE,
                _ERROR_CONTEXT(raw_q, filter_type, sort_by),
                status=503,
            )

    if context is None:
        if page_number > 1:
            raise Http404("Invalid page number")
        if is_ajax:
            return HttpResponse(status=503)
        return render(
            request, CATEGORY_LIST_TEMPLATE,
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
        response = render(request, CATEGORY_LIST_TEMPLATE, context)
        response['X-Has-Next-Page'] = 'true' if page_obj.has_next() else 'false'
        response['X-Next-Page']     = str(page_number + 1) if page_obj.has_next() else ''
        response['X-Total-Count']   = str(context['total_count'])
    else:
        response = render(request, CATEGORY_LIST_TEMPLATE, context)

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
# §8  BACKFILL UTILITY — one-off repair for Category.product_count
# ════════════════════════════════════════════════════════════════════════════
#
# Category.product_count is a cached column (Category.update_product_count()).
# This is the one-off repair for categories whose count was already
# stale/zero before a signal existed to keep it fresh. Run it once from
# a shell:
#
#     python manage.py shell -c "
#     from apps.ponno.views.category_list import backfill_category_product_counts
#     backfill_category_product_counts()
#     "
#
# It intentionally does NOT touch `_page_cache` or `get_category_stats()`
# — same reasoning as backfill_brand_product_counts() in brand_list.py:
# `_page_cache` keys are SHA-256 hashes with no registry of "all keys in
# use", so worst case a page shows pre-backfill counts for up to
# CACHE_TTL + STALE_TTL (90s), then stale-while-revalidate rebuilds it.
# get_category_stats() aggregates by boolean flag, not product_count, so
# it's unaffected either way. total_product_count is never cached on the
# model at all — it's recomputed from live data every _build_context()
# call — so there's nothing to backfill for it.
#
# If you want the backfill to reflect immediately rather than waiting up
# to 90s, call invalidate_category_cache(None) once after it finishes —
# it doesn't need a real instance, only instance.pk/category_slug are
# read for the log line, both of which are handled via getattr().

def backfill_category_product_counts(
    include_inactive: bool = False,
    batch_size: int = _BACKFILL_BATCH_SIZE,
    dry_run: bool = False,
) -> dict:
    """
    Recompute Category.product_count for every category from real
    Product rows.

    Args:
        include_inactive: also backfill soft-deleted/inactive categories.
            Defaults to False, matching Category.objects.active_categories()
            (the same manager method _build_context uses).
        batch_size: categories processed per DB transaction.
        dry_run: report what would change without writing anything.

    Returns:
        {'checked': int, 'changed': int, 'errored': int, 'total': int}
    """
    base_qs = Category.objects.all() if include_inactive else Category.objects.active_categories()
    qs = base_qs.only('pk', 'category_name', 'product_count').order_by('pk')

    total   = qs.count()
    checked = 0
    changed = 0
    errored = 0

    log.info("Backfilling product_count for %d categor(ies)%s", total, ' (dry run)' if dry_run else '')

    batch = []
    for category in qs.iterator(chunk_size=batch_size):
        batch.append(category)
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


def _backfill_batch(batch: list[Category], dry_run: bool) -> tuple[int, int, int]:
    """Process one batch inside its own transaction. Returns (checked, changed, errored)."""
    checked = changed = errored = 0

    with transaction.atomic():
        for category in batch:
            before = category.product_count
            try:
                if dry_run:
                    after = category.products.filter(is_active=True).count()
                else:
                    after = category.update_product_count()
            except Exception:
                errored += 1
                log.exception("Failed to update product_count for category pk=%s", category.pk)
                continue

            checked += 1
            if before != after:
                changed += 1
                log.info(
                    "Category %s (pk=%s): product_count %d -> %d",
                    category.category_name, category.pk, before, after,
                )

        if dry_run:
            transaction.set_rollback(True)

    return checked, changed, errored