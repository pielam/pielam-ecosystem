# backend/apps/ponno/views/category_list.py

import hashlib

from django.core.cache import cache
from django.core.paginator import Paginator, InvalidPage
from django.http import Http404
from django.shortcuts import render
from django.urls import reverse
from django.db.models import Q

from apps.ponno.models.category import Category


CACHE_TTL           = 60   # seconds
CATEGORIES_PER_PAGE = 24


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


def CategoryListView(request):
    """
    Category List View
    - Lists all active categories with search + filter
    - Supports hierarchical browsing via ?parent=<slug>
    - Highlights a single category when ?category=<slug> is passed
      (coming from the discovery engine filter chips)
    - Cached per unique URL to handle high traffic
    """

    # ── Params ───────────────────────────────────────────────────────
    query         = request.GET.get('q',        '').strip()[:100]
    category_slug = request.GET.get('category', '').strip()[:80]
    parent_slug   = request.GET.get('parent',   '').strip()[:80]   # browse children of a parent
    filter_type   = request.GET.get('type',     'all').strip()     # all | featured | trending | root
    sort_by       = request.GET.get('sort',     'popular').strip()

    # page_number is used raw in the cache key below, so normalize it
    # to a string safely before hashing (avoids cache-key inconsistency
    # between "1" / 1 / None).
    page_number = request.GET.get('page', '1')

    VALID_SORTS = {'popular', 'newest', 'name_asc', 'name_desc', 'order'}
    if sort_by not in VALID_SORTS:
        sort_by = 'popular'

    VALID_FILTERS = {'all', 'featured', 'trending', 'root', 'menu'}
    if filter_type not in VALID_FILTERS:
        filter_type = 'all'

    # ── Cache key ────────────────────────────────────────────────────
    raw_key   = f"categorylist:{query}:{category_slug}:{parent_slug}:{filter_type}:{sort_by}:{page_number}"
    cache_key = 'category_list_' + hashlib.md5(raw_key.encode()).hexdigest()

    cached = cache.get(cache_key)
    if cached:
        return render(request, 'ponno/category_list.html', cached)

    # ── Base queryset ────────────────────────────────────────────────
    categories = (
        Category.objects
        .active_categories()
        .select_related('parent')
        .only(
            'pk', 'uuid', 'category_name', 'category_slug',
            'category_short_description', 'category_image', 'category_icon',
            'category_type', 'color_code', 'icon_class',
            'parent', 'level', 'path',
            'is_featured', 'is_trending', 'is_visible_in_menu',
            'product_count', 'view_count', 'popularity_score',
            'display_order',
            # 'parent__category_slug' / 'parent__category_name' pulled in
            # automatically via select_related when accessed below.
        )
    )

    # ── Highlight a specific category (coming from filter chip) ──────
    highlighted_category = None
    if category_slug:
        try:
            highlighted_category = categories.get(category_slug=category_slug)
        except Category.DoesNotExist:
            pass
        except Category.MultipleObjectsReturned:
            # category_slug is unique=True on the model so this shouldn't
            # happen, but guard anyway rather than 500ing.
            highlighted_category = categories.filter(category_slug=category_slug).first()

    # ── Parent filter (browse children) ──────────────────────────────
    parent_category = None
    if parent_slug:
        try:
            parent_category = Category.objects.active_categories().get(
                category_slug=parent_slug
            )
            categories = categories.filter(parent=parent_category)
        except Category.DoesNotExist:
            pass
        except Category.MultipleObjectsReturned:
            parent_category = Category.objects.active_categories().filter(
                category_slug=parent_slug
            ).first()
            if parent_category:
                categories = categories.filter(parent=parent_category)

    # ── Search ───────────────────────────────────────────────────────
    if query:
        categories = categories.filter(
            Q(category_name__icontains=query) |
            Q(category_description__icontains=query) |
            Q(category_slug__icontains=query) |
            Q(category_short_description__icontains=query)
        )

    # ── Type filter ──────────────────────────────────────────────────
    if filter_type == 'featured':
        categories = categories.filter(is_featured=True)
    elif filter_type == 'trending':
        categories = categories.filter(is_trending=True)
    elif filter_type == 'root':
        categories = categories.filter(parent__isnull=True)
    elif filter_type == 'menu':
        categories = categories.filter(is_visible_in_menu=True)

    # ── Sort ─────────────────────────────────────────────────────────
    SORT_MAP = {
        'popular':   ['-popularity_score', '-product_count'],
        'newest':    ['-category_created_at'],
        'name_asc':  ['category_name'],
        'name_desc': ['-category_name'],
        'order':     ['display_order', 'category_name'],
    }
    categories = categories.order_by(*SORT_MAP[sort_by])
    categories = categories.distinct()  # search across text fields can duplicate rows

    # ── Stats (cached separately, longer TTL) ────────────────────────
    stats_key = 'category_list_stats'
    stats     = cache.get(stats_key)
    if stats is None:
        all_active = Category.objects.active_categories()
        stats = {
            'total':    all_active.count(),
            'featured': all_active.filter(is_featured=True).count(),
            'trending': all_active.filter(is_trending=True).count(),
            'root':     all_active.filter(parent__isnull=True).count(),
        }
        cache.set(stats_key, stats, 120)

    # ── Paginate ─────────────────────────────────────────────────────
    paginator = Paginator(categories, CATEGORIES_PER_PAGE)
    try:
        page_obj = paginator.get_page(page_number)
    except InvalidPage:
        raise Http404("Invalid page number")

    # ── Format ───────────────────────────────────────────────────────
    formatted_categories = [
        {
            'id':                 cat.pk,
            'uuid':               str(cat.uuid),
            'name':               cat.category_name,
            'slug':               cat.category_slug,
            'short_description':  cat.category_short_description or '',
            'image':              cat.image_url,
            'icon':               cat.icon_url,
            'icon_class':         cat.icon_class or '',
            'color_code':         cat.color_code or '',
            'type':               cat.category_type,
            'level':              cat.level,
            'is_root':            cat.parent_id is None,
            'parent_name':        cat.parent.category_name if cat.parent_id else '',
            'parent_slug':        cat.parent.category_slug if cat.parent_id else '',
            'is_featured':        cat.is_featured,
            'is_trending':        cat.is_trending,
            'is_visible_in_menu': cat.is_visible_in_menu,
            'product_count':      cat.product_count,
            'view_count':         cat.view_count,
            'has_children':       cat.has_children,
            'child_count':        cat.child_count,
            'category_url':       _category_url(cat),
            'highlighted':        bool(highlighted_category) and cat.pk == highlighted_category.pk,
        }
        for cat in page_obj
    ]

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
    ]

    # ── Breadcrumb (when browsing under a parent) ─────────────────────
    # Built manually here rather than via Category.breadcrumb, since that
    # model property also hardcodes /categories/<slug>/ internally.
    breadcrumb = []
    if parent_category:
        chain = []
        node = parent_category
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

    context = {
        'categories':           formatted_categories,
        'page_obj':             page_obj,
        'query':                query,
        'category_slug':        category_slug,
        'highlighted_category': highlighted_category,
        'parent_category':      parent_category,
        'breadcrumb':           breadcrumb,
        'current_sort':         sort_by,
        'current_filter':       filter_type,
        'sort_options':         sort_options,
        'filter_tabs':          filter_tabs,
        'total_count':          paginator.count,
        'stats':                stats,
    }

    cache.set(cache_key, context, CACHE_TTL)
    return render(request, 'ponno/category_list.html', context)