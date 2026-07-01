# backend/apps/ponno/views/subcategory_list.py

import hashlib

from django.core.cache import cache
from django.core.paginator import Paginator, InvalidPage
from django.http import Http404
from django.shortcuts import render
from django.db.models import Q

from apps.ponno.models.sub_category import SubCategory
from apps.ponno.models.category import Category


CACHE_TTL              = 60   # seconds
SUBCATEGORIES_PER_PAGE = 24


def SubCategoryListView(request):
    """
    SubCategory List View
    - Lists all active subcategories with search + filter
    - Supports filtering by parent category via ?category=<slug>
    - Cached per unique URL to handle high traffic

    Note: this view hands the template real SubCategory model instances
    (via page_obj), not flattened dicts — the template reads model
    fields/properties directly (sub.sub_category_name, sub.category.*,
    sub.image_url, sub.popularity_score, etc).
    """

    # ── Params ───────────────────────────────────────────────────────
    query         = request.GET.get('q',        '').strip()[:100]
    category_slug = request.GET.get('category', '').strip()[:80]
    filter_type   = request.GET.get('type',     'all').strip()   # all | featured | trending | menu
    sort_by       = request.GET.get('sort',     'popular').strip()

    # page_number is used raw in the cache key below, so normalize it
    # to a string safely before hashing (avoids cache-key inconsistency
    # between "1" / 1 / None).
    page_number = request.GET.get('page', '1')

    VALID_SORTS = {'popular', 'newest', 'name_asc', 'name_desc', 'order'}
    if sort_by not in VALID_SORTS:
        sort_by = 'popular'

    VALID_FILTERS = {'all', 'featured', 'trending', 'menu'}
    if filter_type not in VALID_FILTERS:
        filter_type = 'all'

    # ── Cache key ────────────────────────────────────────────────────
    raw_key   = f"subcategorylist:{query}:{category_slug}:{filter_type}:{sort_by}:{page_number}"
    cache_key = 'subcategory_list_' + hashlib.md5(raw_key.encode()).hexdigest()

    cached = cache.get(cache_key)
    if cached:
        return render(request, 'ponno/subcategory_list.html', cached)

    # ── Base queryset ────────────────────────────────────────────────
    subcategories = (
        SubCategory.objects
        .active()
        .select_related('category', 'brand')
    )

    # ── Category filter ─────────────────────────────────────────────
    selected_category = None
    if category_slug:
        try:
            selected_category = Category.objects.active_categories().get(
                category_slug=category_slug
            )
            subcategories = subcategories.filter(category=selected_category)
        except Category.DoesNotExist:
            pass
        except Category.MultipleObjectsReturned:
            selected_category = Category.objects.active_categories().filter(
                category_slug=category_slug
            ).first()
            if selected_category:
                subcategories = subcategories.filter(category=selected_category)

    # ── Search ───────────────────────────────────────────────────────
    if query:
        subcategories = subcategories.filter(
            Q(sub_category_name__icontains=query) |
            Q(sub_category_description__icontains=query) |
            Q(sub_category_slug__icontains=query) |
            Q(sub_category_short_description__icontains=query) |
            Q(category__category_name__icontains=query)
        )

    # ── Type filter ──────────────────────────────────────────────────
    if filter_type == 'featured':
        subcategories = subcategories.filter(is_featured=True)
    elif filter_type == 'trending':
        subcategories = subcategories.filter(is_trending=True)
    elif filter_type == 'menu':
        subcategories = subcategories.filter(is_visible_in_menu=True)

    # ── Sort ─────────────────────────────────────────────────────────
    SORT_MAP = {
        'popular':   ['-popularity_score', '-product_count'],
        'newest':    ['-sub_category_created_at'],
        'name_asc':  ['sub_category_name'],
        'name_desc': ['-sub_category_name'],
        'order':     ['display_order', 'sub_category_name'],
    }
    subcategories = subcategories.order_by(*SORT_MAP[sort_by])
    subcategories = subcategories.distinct()  # search across joined fields can duplicate rows

    # ── Stats (cached separately, longer TTL) ────────────────────────
    stats_key = 'subcategory_list_stats'
    stats     = cache.get(stats_key)
    if stats is None:
        all_active = SubCategory.objects.active()
        stats = {
            'total':    all_active.count(),
            'featured': all_active.filter(is_featured=True).count(),
            'trending': all_active.filter(is_trending=True).count(),
            'menu':     all_active.filter(is_visible_in_menu=True).count(),
        }
        cache.set(stats_key, stats, 120)

    # ── Categories with active subcategories (available for a filter
    #    dropdown if the template adds one later) ─────────────────────
    categories = (
        Category.objects.active_categories()
        .filter(sub_categories__is_active=True, sub_categories__deleted_at__isnull=True)
        .distinct()
        .order_by('display_order', 'category_name')
    )

    # ── Paginate ─────────────────────────────────────────────────────
    paginator = Paginator(subcategories, SUBCATEGORIES_PER_PAGE)
    try:
        page_obj = paginator.get_page(page_number)
    except InvalidPage:
        raise Http404("Invalid page number")

    # ── Guarantee every row on this page has a slug ───────────────────
    # sub_category_slug is nullable/blank on the model. The template
    # builds product-list links directly from it
    # ({% url 'ponno:subcategory_products' category_slug=sub.sub_category_slug %}),
    # and reverse() raises NoReverseMatch on an empty string — so backfill
    # any missing slugs before rendering rather than letting the page 500.
    for sub in page_obj:
        if not sub.sub_category_slug:
            sub.generate_slug(save=True)

    sort_options = [
        {'value': 'popular',   'label': 'Most Popular',  'active': sort_by == 'popular'},
        {'value': 'newest',    'label': 'Newest First',  'active': sort_by == 'newest'},
        {'value': 'name_asc',  'label': 'Name A → Z',    'active': sort_by == 'name_asc'},
        {'value': 'name_desc', 'label': 'Name Z → A',    'active': sort_by == 'name_desc'},
        {'value': 'order',     'label': 'Display Order', 'active': sort_by == 'order'},
    ]

    filter_tabs = [
        {'value': 'all',      'label': 'All',      'count': stats['total'],    'active': filter_type == 'all'},
        {'value': 'featured', 'label': 'Featured', 'count': stats['featured'], 'active': filter_type == 'featured'},
        {'value': 'trending', 'label': 'Trending', 'count': stats['trending'], 'active': filter_type == 'trending'},
        {'value': 'menu',     'label': 'In Menu',  'count': stats['menu'],     'active': filter_type == 'menu'},
    ]

    context = {
        'subcategories':     page_obj,   # real SubCategory instances for this page
        'page_obj':          page_obj,
        'query':             query,
        'categories':        categories,
        'selected_category': selected_category,
        'current_sort':      sort_by,
        'current_filter':    filter_type,
        'sort_options':      sort_options,
        'filter_tabs':       filter_tabs,
        'total_count':       paginator.count,
        'stats':             stats,
    }

    cache.set(cache_key, context, CACHE_TTL)
    return render(request, 'ponno/subcategory_list.html', context)