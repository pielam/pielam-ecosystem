"""
apps/ponno/filter_products.py
=============================
Enterprise-grade filter / sort pipeline.

Usable from any view — class-based, function-based, DRF APIView,
async views, management commands, or background tasks.

Responsibilities
────────────────
1.  Parse & validate GET / POST / programmatic parameters into a typed,
    immutable FilterParams dataclass.

2.  Apply panel-driven filters to any active Product queryset:
        Brand filter         (brand_slug)
        Category filter      (slug + hierarchical path descent)
        SubCategory filter   (slug)
        Price range          (min_price / max_price)
        Stock filter         (in_stock=true)
        Tab filters          (trending / new / sale / featured / verified / instock)
        Full-text search     (q)

3.  Resolve filter metadata (FilterMeta) from pre-loaded fragment
    dicts — zero extra DB calls.

4.  Apply sort ordering:
        name_az        → Python natural sort (returns sorted PKs for caller)
        discount       → ORM filter + ORDER BY
        explicit sorts → ORM ORDER BY via SORT_MAP
        feed sorts     → no ORDER BY here; caller applies rank_and_diversify()

5.  Build sort option lists for template contexts.

6.  Provide a FilterPipeline builder for reusable, composable filter
    configurations across different views.

Usage in any view
──────────────────
    from apps.ponno.filter_products import FilterParams, apply_filters

    # Class-based or function-based view:
    params = FilterParams.from_request(request)
    qs, meta = apply_filters(products_qs, params, brands, categories, sub_categories)

    # Programmatic (management command, Celery task, test):
    params = FilterParams(filter_slug='samsung', sort_by='price_low', in_stock=True)
    qs, meta = apply_filters(products_qs, params, brands, categories, sub_categories)

    # DRF / API view (parse from query_params dict):
    params = FilterParams.from_dict(request.query_params)
    qs, meta = apply_filters(products_qs, params, brands, categories, sub_categories)

    # Reusable pipeline (register once, call from multiple views):
    from apps.ponno.filter_products import FilterPipeline
    pipeline = FilterPipeline(brands=brands, categories=cats, sub_categories=sub_cats)
    qs, meta = pipeline.run(products_qs, params)

    # Sort options for template context:
    from apps.ponno.filter_products import build_sort_options
    context['sort_options'] = build_sort_options(params.sort_by)

Out of scope (handled in the view / feed_algorithm)
────────────────────────────────────────────────────
• Two-tier feed cache + rank_and_diversify
• Django cache calls
• Template rendering / HTTP logic
• Authentication / permission checks
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Dict, FrozenSet, Iterable, List, Optional, Sequence, Tuple

from django.db.models import Q, QuerySet
from django.utils import timezone

logger = logging.getLogger(__name__)


# ═════════════════════════════════════════════════════════════════════
# ██  CONSTANTS
# ═════════════════════════════════════════════════════════════════════

PRODUCTS_PER_PAGE: int = 20

# All valid sort keys
VALID_SORTS: FrozenSet[str] = frozenset({
    'newest', 'popular', 'price_low',
    'price_high', 'rating', 'discount', 'name_az',
})

# Sorts that bypass the feed-ranking algorithm and go straight to DB / Python
EXPLICIT_SORT_BYPASSES: FrozenSet[str] = frozenset({
    'price_low', 'price_high', 'rating', 'discount', 'name_az',
})

# ORM ORDER BY fields per sort key
SORT_MAP: Dict[str, List[str]] = {
    'newest':     ['-created_at'],
    'popular':    ['-view_count', '-total_sales'],
    'price_low':  ['selling_price'],
    'price_high': ['-selling_price'],
    'rating':     ['-rating_average', '-review_count'],
    'discount':   ['-discount_percentage'],
}

# Sort option labels for the template panel
SORT_LABELS: Dict[str, str] = {
    'newest':     'Newest First',
    'popular':    'Most Popular',
    'price_low':  'Price: Low to High',
    'price_high': 'Price: High to Low',
    'rating':     'Highest Rated',
    'discount':   'Best Discount',
    'name_az':    'Name: A → Z',
}

# Slugs that are UI navigation only — never map to a product queryset filter
_UI_ONLY_SLUGS: FrozenSet[str] = frozenset({
    'all', '', 'brands', 'categories', 'sub_categories',
})

# Natural sort digit splitter
_DIGIT_SPLIT = re.compile(r'(\d+)')

# Tab filter time window for "new" arrivals (days)
_NEW_ARRIVAL_DAYS: int = 7


# ═════════════════════════════════════════════════════════════════════
# ██  TAB FILTER MAP
# ═════════════════════════════════════════════════════════════════════

def get_tab_filter_map() -> Dict[str, Q]:
    """
    Returns a fresh dict each call so timezone.now() is always current.

    Intentionally NOT cached at module level — the 'new' filter uses a
    live timestamp. Safe to call repeatedly; cheap (no DB).
    """
    return {
        'trending': Q(is_trending=True),
        'new':      Q(created_at__gte=timezone.now() - timezone.timedelta(days=_NEW_ARRIVAL_DAYS)),
        'sale':     Q(discount_percentage__gt=0),
        'featured': Q(is_featured=True),
        'verified': Q(is_verified=True),
        'instock':  Q(stock__gt=0),
    }


TAB_FILTER_NAMES: Dict[str, str] = {
    'trending': 'Trending',
    'new':      'New Arrivals',
    'sale':     'On Sale',
    'featured': 'Featured',
    'verified': 'Verified',
    'instock':  'In Stock',
}

TAB_FILTER_SLUGS: FrozenSet[str] = frozenset(TAB_FILTER_NAMES.keys())


# ═════════════════════════════════════════════════════════════════════
# ██  FILTER PARAMS
# ═════════════════════════════════════════════════════════════════════

@dataclass(frozen=True)
class FilterParams:
    """
    Typed, validated, immutable snapshot of all filter / sort parameters.

    Can be constructed from:
    - A Django HttpRequest          → FilterParams.from_request(request)
    - A plain dict (DRF, tests)     → FilterParams.from_dict(data)
    - Direct instantiation          → FilterParams(filter_slug='samsung', ...)

    Does NOT include authentication or permission state.
    """

    filter_slug: str  = 'all'
    sort_by:     str  = 'newest'
    min_price:   str  = ''
    max_price:   str  = ''
    in_stock:    bool = False
    tab:         str  = ''       # e.g. 'trending', 'sale', 'new', …
    query:       str  = ''       # full-text search term
    page:        int  = 1

    # ── Convenience flags ──────────────────────────────────────────

    @property
    def has_price_filter(self) -> bool:
        return bool(self.min_price or self.max_price)

    @property
    def has_query(self) -> bool:
        return bool(self.query)

    @property
    def has_tab(self) -> bool:
        return bool(self.tab)

    @property
    def is_ui_only_slug(self) -> bool:
        return self.filter_slug in _UI_ONLY_SLUGS

    @property
    def is_feed_sort(self) -> bool:
        """True when rank_and_diversify() should be applied."""
        return self.sort_by not in EXPLICIT_SORT_BYPASSES

    @property
    def orm_order(self) -> List[str]:
        """ORM ORDER BY fields for the current sort_by value."""
        return SORT_MAP.get(self.sort_by, ['-created_at'])

    @property
    def is_tab_valid(self) -> bool:
        return self.tab in TAB_FILTER_SLUGS

    # ── Constructors ───────────────────────────────────────────────

    @classmethod
    def from_request(cls, request) -> 'FilterParams':
        """
        Parse and validate from a Django HttpRequest.
        Works with GET and POST; GET takes priority.
        """
        source = request.GET if request.method == 'GET' else (request.POST or request.GET)
        return cls.from_dict(source)

    @classmethod
    def from_dict(cls, data: Any) -> 'FilterParams':
        """
        Parse and validate from any dict-like object.
        Accepts Django QueryDict, plain dict, or DRF request.query_params.
        """
        def _get(key: str, default: str = '') -> str:
            val = data.get(key, default)
            if isinstance(val, (list, tuple)):
                val = val[0] if val else default
            return str(val).strip() if val is not None else default

        filter_slug = _get('filter', 'all')[:80]
        sort_by     = _get('sort',   'newest')
        min_price   = _get('min_price')
        max_price   = _get('max_price')
        in_stock    = _get('in_stock', 'false').lower() == 'true'
        tab         = _get('tab')
        query       = _get('q')[:200]
        raw_page    = _get('page', '1')

        if sort_by not in VALID_SORTS:
            sort_by = 'newest'

        if tab not in TAB_FILTER_SLUGS:
            tab = ''

        try:
            page = max(1, int(raw_page))
        except (ValueError, TypeError):
            page = 1

        return cls(
            filter_slug=filter_slug,
            sort_by=sort_by,
            min_price=min_price,
            max_price=max_price,
            in_stock=in_stock,
            tab=tab,
            query=query,
            page=page,
        )

    def replace(self, **kwargs) -> 'FilterParams':
        """
        Return a new FilterParams with the given fields overridden.
        Useful for building modified params without mutating the original.

        Example::
            new_params = params.replace(sort_by='price_low', page=2)
        """
        current = {
            'filter_slug': self.filter_slug,
            'sort_by':     self.sort_by,
            'min_price':   self.min_price,
            'max_price':   self.max_price,
            'in_stock':    self.in_stock,
            'tab':         self.tab,
            'query':       self.query,
            'page':        self.page,
        }
        current.update(kwargs)
        return FilterParams(**current)

    def to_dict(self) -> Dict[str, Any]:
        """Serialise to a plain dict (useful for cache keys, logging)."""
        return {
            'filter':    self.filter_slug,
            'sort':      self.sort_by,
            'min_price': self.min_price,
            'max_price': self.max_price,
            'in_stock':  self.in_stock,
            'tab':       self.tab,
            'q':         self.query,
            'page':      self.page,
        }

    def cache_key_fragment(self) -> str:
        """
        Compact string suitable for embedding in a Django cache key.
        Stable across Python versions (no hash()).
        """
        d = self.to_dict()
        parts = [f"{k}={v}" for k, v in sorted(d.items()) if v not in ('', False, 1)]
        return ':'.join(parts) or 'default'

    def __repr__(self) -> str:
        return (
            f"<FilterParams filter={self.filter_slug!r} sort={self.sort_by!r} "
            f"tab={self.tab!r} q={self.query!r} page={self.page}>"
        )


# ═════════════════════════════════════════════════════════════════════
# ██  FILTER META
# ═════════════════════════════════════════════════════════════════════

@dataclass
class FilterMeta:
    """
    Result metadata returned alongside the filtered queryset.

    active_filter_type  — 'all' | 'brand' | 'category' | 'sub_category'
    active_tab          — active tab slug or empty string
    sorted_pks          — populated only for name_az sort; use for pagination
    unknown_filter      — True when filter_slug matched nothing in the maps
    applied_filters     — human-readable list of active filter descriptions
    total_before_page   — set by view after paginating; FilterMeta itself does not set it
    """
    active_filter_type: str       = 'all'
    active_tab:         str       = ''
    sorted_pks:         List[int] = field(default_factory=list)
    unknown_filter:     bool      = False
    applied_filters:    List[str] = field(default_factory=list)
    total_before_page:  int       = 0

    def add_filter_label(self, label: str) -> None:
        """Record a human-readable description of an applied filter."""
        if label and label not in self.applied_filters:
            self.applied_filters.append(label)

    @property
    def has_active_filters(self) -> bool:
        return bool(self.applied_filters)

    def to_dict(self) -> Dict[str, Any]:
        return {
            'active_filter_type': self.active_filter_type,
            'active_tab':         self.active_tab,
            'unknown_filter':     self.unknown_filter,
            'applied_filters':    self.applied_filters,
            'total_before_page':  self.total_before_page,
        }


# ═════════════════════════════════════════════════════════════════════
# ██  NATURAL SORT HELPER
# ═════════════════════════════════════════════════════════════════════

def _natural_sort_key(name: str) -> List:
    if not name:
        return [(1, '')]
    dash_idx = name.find('-')
    segment  = name[dash_idx + 1:] if dash_idx != -1 else name
    parts    = _DIGIT_SPLIT.split(segment)
    return [
        (0, int(p)) if p.isdigit() else (1, p.upper())
        for p in parts if p
    ]


def natural_sort_pks(products_qs: QuerySet) -> List[int]:
    """
    Fetch (pk, product_name) pairs from the queryset and return PKs
    sorted in natural A→Z order. Cheap: fetches only two columns.
    """
    rows = list(products_qs.values_list('pk', 'product_name'))
    rows.sort(key=lambda r: _natural_sort_key(r[1] or ''))
    return [r[0] for r in rows]


# ═════════════════════════════════════════════════════════════════════
# ██  PRICE / STOCK FILTER
# ═════════════════════════════════════════════════════════════════════

def _apply_price_stock(
    qs: QuerySet,
    params: FilterParams,
    meta: FilterMeta,
) -> QuerySet:
    try:
        if params.min_price:
            qs = qs.filter(selling_price__gte=float(params.min_price))
            meta.add_filter_label(f"Min price: {params.min_price}")
    except ValueError:
        logger.debug('filter_products: invalid min_price=%r', params.min_price)

    try:
        if params.max_price:
            qs = qs.filter(selling_price__lte=float(params.max_price))
            meta.add_filter_label(f"Max price: {params.max_price}")
    except ValueError:
        logger.debug('filter_products: invalid max_price=%r', params.max_price)

    if params.in_stock:
        qs = qs.filter(stock__gt=0)
        meta.add_filter_label('In stock only')

    return qs


# ═════════════════════════════════════════════════════════════════════
# ██  TAB FILTER
# ═════════════════════════════════════════════════════════════════════

def _apply_tab_filter(
    qs: QuerySet,
    params: FilterParams,
    meta: FilterMeta,
) -> QuerySet:
    if not params.has_tab or not params.is_tab_valid:
        return qs

    tab_map = get_tab_filter_map()
    q_filter = tab_map.get(params.tab)
    if q_filter is not None:
        qs = qs.filter(q_filter)
        meta.active_tab = params.tab
        meta.add_filter_label(TAB_FILTER_NAMES.get(params.tab, params.tab))

    return qs


# ═════════════════════════════════════════════════════════════════════
# ██  FULL-TEXT SEARCH FILTER
# ═════════════════════════════════════════════════════════════════════
def _apply_search_filter(
    qs: QuerySet,
    params: FilterParams,
    meta: FilterMeta,
) -> QuerySet:
    if not params.has_query:
        return qs

    q = params.query
    qs = qs.filter(
        Q(product_title__icontains=q) |
        Q(product_name__icontains=q)  |
        Q(short_description__icontains=q) |
        Q(sku__icontains=q) |
        Q(brand__brand_name__icontains=q) |
        Q(category__category_name__icontains=q)
    ).distinct()

    meta.add_filter_label(f'Search: "{q}"')
    return qs

# ═════════════════════════════════════════════════════════════════════
# ██  ENTITY FILTER RESOLVER
# ═════════════════════════════════════════════════════════════════════

def _apply_entity_filter(
    qs:               QuerySet,
    filter_slug:      str,
    meta:             FilterMeta,
    brand_map:        Dict[str, Any],
    category_map:     Dict[str, Any],
    sub_category_map: Dict[str, Any],
) -> Tuple[QuerySet, FilterMeta]:
    """
    Match filter_slug against brand / category / sub_category maps and
    apply the correct ORM filter. Sets meta.active_filter_type.

    Category filtering is hierarchical — descendants (path prefix match)
    are included along with the exact slug match.
    """
    if filter_slug in brand_map:
        qs = qs.filter(brand__brand_slug=filter_slug)
        meta.active_filter_type = 'brand'
        meta.add_filter_label(f"Brand: {brand_map[filter_slug].get('brand_name', filter_slug)}")

    elif filter_slug in category_map:
        cat_info = category_map[filter_slug]
        qs = qs.filter(
            Q(category__category_slug=filter_slug) |
            Q(category__path__startswith=cat_info.get('path', '') + '/')
        )
        meta.active_filter_type = 'category'
        meta.add_filter_label(
            f"Category: {cat_info.get('category_name', filter_slug)}"
        )

    elif filter_slug in sub_category_map:
        qs = qs.filter(sub_category__sub_category_slug=filter_slug)
        meta.active_filter_type = 'sub_category'
        meta.add_filter_label(
            f"SubCategory: {sub_category_map[filter_slug].get('sub_category_name', filter_slug)}"
        )

    else:
        meta.unknown_filter = True
        logger.debug('filter_products: unknown filter_slug=%r', filter_slug)

    return qs, meta


# ═════════════════════════════════════════════════════════════════════
# ██  SORT APPLICATION
# ═════════════════════════════════════════════════════════════════════

def _apply_sort(
    qs:     QuerySet,
    params: FilterParams,
    meta:   FilterMeta,
) -> Tuple[QuerySet, FilterMeta]:
    if params.sort_by == 'name_az':
        # Fetch two columns only; caller paginates from meta.sorted_pks
        meta.sorted_pks = natural_sort_pks(qs)

    elif params.sort_by == 'discount':
        qs = qs.filter(discount_percentage__gt=0).order_by(*SORT_MAP['discount'])

    elif params.sort_by in EXPLICIT_SORT_BYPASSES:
        qs = qs.order_by(*SORT_MAP.get(params.sort_by, ['-created_at']))

    # Feed sorts (newest / popular): no ORDER BY — caller applies rank_and_diversify()

    return qs, meta


# ═════════════════════════════════════════════════════════════════════
# ██  MAIN ENTRY POINT
# ═════════════════════════════════════════════════════════════════════

def apply_filters(
    qs:             QuerySet,
    params:         FilterParams,
    brands:         List[Dict],
    categories:     List[Dict],
    sub_categories: List[Dict],
) -> Tuple[QuerySet, FilterMeta]:
    """
    Apply the full filter + sort pipeline to the given Product queryset.

    Usable from **any** view type — class-based, function-based, DRF,
    async, management commands, Celery tasks.

    Args:
        qs:             Active Product queryset (filters like is_active=True,
                        deleted_at__isnull=True already applied by the view).
        params:         Parsed params from FilterParams.from_request() or
                        FilterParams.from_dict() or direct construction.
        brands:         Fragment list — each dict must have 'brand_slug'.
        categories:     Fragment list — each dict must have 'category_slug' and 'path'.
        sub_categories: Fragment list — each dict must have 'sub_category_slug'.

    Returns:
        (filtered_qs, FilterMeta)

        filtered_qs  — lazy queryset with filters + sort applied.
                       The caller decides when to evaluate (paginator,
                       list(), iterator(), etc.).
        meta         — FilterMeta with:
                         .active_filter_type  — 'all'|'brand'|'category'|'sub_category'
                         .active_tab          — active tab slug or ''
                         .applied_filters     — human-readable label list
                         .sorted_pks          — non-empty only for name_az sort
                         .unknown_filter      — True → filter_slug matched nothing

    Pipeline (in order)
    ───────────────────
    1. Entity filter      (brand / category / sub_category panel selection)
    2. Tab filter         (trending / new / sale / featured / verified / instock)
    3. Full-text search   (q parameter)
    4. Price / stock      (min_price / max_price / in_stock)
    5. Sort ordering      (name_az / discount / explicit / feed)
    """
    meta = FilterMeta()

    # ── Build lookup dicts ────────────────────────────────────────
    brand_map        = {b['brand_slug']:        b for b in brands        if b.get('brand_slug')}
    category_map     = {c['category_slug']:     c for c in categories    if c.get('category_slug')}
    sub_category_map = {s['sub_category_slug']: s for s in sub_categories if s.get('sub_category_slug')}

    # ── 1. Entity filter ──────────────────────────────────────────
    if not params.is_ui_only_slug and params.filter_slug:
        qs, meta = _apply_entity_filter(
            qs, params.filter_slug, meta,
            brand_map, category_map, sub_category_map,
        )

    # ── 2. Tab filter ─────────────────────────────────────────────
    qs = _apply_tab_filter(qs, params, meta)

    # ── 3. Search ─────────────────────────────────────────────────
    qs = _apply_search_filter(qs, params, meta)

    # ── 4. Price / stock ──────────────────────────────────────────
    qs = _apply_price_stock(qs, params, meta)

    # ── 5. Sort ───────────────────────────────────────────────────
    qs, meta = _apply_sort(qs, params, meta)

    return qs, meta


# ═════════════════════════════════════════════════════════════════════
# ██  FILTER PIPELINE  (reusable across views)
# ═════════════════════════════════════════════════════════════════════

class FilterPipeline:
    """
    Encapsulates pre-loaded fragment data and exposes a simple .run()
    method. Build once (e.g. in a mixin or service layer), reuse across
    requests.

    Example::

        # In a mixin / service:
        pipeline = FilterPipeline(
            brands=_get_active_brands(),
            categories=_get_active_categories(),
            sub_categories=_get_active_sub_categories(),
        )

        # In HomeView.get():
        params = FilterParams.from_request(request)
        qs, meta = pipeline.run(base_qs, params)

        # In DiscoveryEngineView.get():
        params = FilterParams.from_request(request)
        qs, meta = pipeline.run(base_qs, params)
    """

    def __init__(
        self,
        brands:         List[Dict],
        categories:     List[Dict],
        sub_categories: List[Dict],
    ) -> None:
        self.brands         = brands
        self.categories     = categories
        self.sub_categories = sub_categories

    def run(
        self,
        qs:     QuerySet,
        params: FilterParams,
    ) -> Tuple[QuerySet, FilterMeta]:
        """Apply the full filter pipeline. See apply_filters() for details."""
        return apply_filters(qs, params, self.brands, self.categories, self.sub_categories)

    def with_brands(self, brands: List[Dict]) -> 'FilterPipeline':
        """Return a new pipeline with updated brands list."""
        return FilterPipeline(brands, self.categories, self.sub_categories)

    def with_categories(self, categories: List[Dict]) -> 'FilterPipeline':
        """Return a new pipeline with updated categories list."""
        return FilterPipeline(self.brands, categories, self.sub_categories)

    def with_sub_categories(self, sub_categories: List[Dict]) -> 'FilterPipeline':
        """Return a new pipeline with updated sub_categories list."""
        return FilterPipeline(self.brands, self.categories, sub_categories)

    def __repr__(self) -> str:
        return (
            f"<FilterPipeline brands={len(self.brands)} "
            f"categories={len(self.categories)} "
            f"sub_categories={len(self.sub_categories)}>"
        )


# ═════════════════════════════════════════════════════════════════════
# ██  SORT OPTIONS  (template context helper)
# ═════════════════════════════════════════════════════════════════════

def build_sort_options(
    sort_by: str,
    *,
    include: Optional[Iterable[str]] = None,
    exclude: Optional[Iterable[str]] = None,
) -> List[Dict[str, Any]]:
    """
    Return the sort option list consumed by the sort panel template.

    Args:
        sort_by:  Currently active sort key.
        include:  Optional whitelist of sort keys to include.
        exclude:  Optional blacklist of sort keys to exclude.

    Returns:
        List of dicts: [{'value': ..., 'label': ..., 'active': bool}, ...]

    Example::

        # Full list:
        context['sort_options'] = build_sort_options(params.sort_by)

        # Exclude name_az for mobile:
        context['sort_options'] = build_sort_options(params.sort_by, exclude=['name_az'])
    """
    include_set = frozenset(include) if include else frozenset(SORT_LABELS.keys())
    exclude_set = frozenset(exclude) if exclude else frozenset()

    return [
        {
            'value':  value,
            'label':  label,
            'active': sort_by == value,
        }
        for value, label in SORT_LABELS.items()
        if value in include_set and value not in exclude_set
    ]


def get_active_sort_label(sort_by: str) -> str:
    """Return the human-readable label for the active sort key."""
    return SORT_LABELS.get(sort_by, SORT_LABELS['newest'])


# ═════════════════════════════════════════════════════════════════════
# ██  TAB OPTIONS  (template context helper)
# ═════════════════════════════════════════════════════════════════════

def build_tab_options(
    active_tab: str,
    *,
    include: Optional[Iterable[str]] = None,
    exclude: Optional[Iterable[str]] = None,
) -> List[Dict[str, Any]]:
    """
    Return the tab option list consumed by the tab panel template.

    Args:
        active_tab: Currently active tab slug ('' for no tab).
        include:    Optional whitelist of tab slugs to include.
        exclude:    Optional blacklist of tab slugs to exclude.

    Returns:
        List of dicts: [{'value': ..., 'label': ..., 'active': bool}, ...]
    """
    include_set = frozenset(include) if include else frozenset(TAB_FILTER_NAMES.keys())
    exclude_set = frozenset(exclude) if exclude else frozenset()

    return [
        {
            'value':  slug,
            'label':  label,
            'active': active_tab == slug,
        }
        for slug, label in TAB_FILTER_NAMES.items()
        if slug in include_set and slug not in exclude_set
    ]


# ═════════════════════════════════════════════════════════════════════
# ██  RE-EXPORTS
# ═════════════════════════════════════════════════════════════════════

__all__ = [
    # Core types
    'FilterParams',
    'FilterMeta',
    'FilterPipeline',

    # Main pipeline
    'apply_filters',

    # Tab helpers
    'get_tab_filter_map',
    'TAB_FILTER_NAMES',
    'TAB_FILTER_SLUGS',

    # Sort helpers
    'build_sort_options',
    'build_tab_options',
    'get_active_sort_label',
    'natural_sort_pks',

    # Constants
    'VALID_SORTS',
    'EXPLICIT_SORT_BYPASSES',
    'SORT_MAP',
    'SORT_LABELS',
    'PRODUCTS_PER_PAGE',
]