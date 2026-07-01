# backend/apps/ponno/views/subcategory_products.py

import hashlib
from decimal import Decimal, InvalidOperation

from django.core.cache import cache
from django.core.paginator import Paginator, InvalidPage
from django.http import Http404
from django.shortcuts import render
from django.urls import reverse
from django.db.models import Q

from apps.ponno.models.product import Product
from apps.ponno.models.sub_category import SubCategory
from apps.ponno.models.brand import Brand


CACHE_TTL         = 60   # seconds
PRODUCTS_PER_PAGE = 12   # fallback if subcategory.products_per_page isn't set


def _decimal_or_none(value: str):
    """Safely parse a GET param into a Decimal, or None if invalid/blank."""
    value = (value or '').strip()
    if not value:
        return None
    try:
        return Decimal(value)
    except (InvalidOperation, ValueError):
        return None


def SubCategoryProducts(request, category_slug):
    """
    SubCategory Products View

    NOTE: the URL kwarg is named `category_slug` (from urls.py:
    path('subcategory/<slug:category_slug>/', SubCategoryProducts, name='subcategory_products'))
    but it actually carries the *subcategory's* slug — SubCategory.objects
    is looked up on sub_category_slug, not the parent Category's slug.
    Kept as-is here rather than renaming, to avoid a breaking urls.py change.

    - Shows all active products belonging to a subcategory
    - Supports search, brand filter, price range, stock filter, and sorting
    - Cached per unique URL to handle high traffic
    """

    # ── SubCategory ──────────────────────────────────────────────────
    try:
        subcategory = SubCategory.objects.get_by_slug(category_slug)
    except SubCategory.DoesNotExist:
        raise Http404("SubCategory not found")

    category = subcategory.category

    # ── Params ───────────────────────────────────────────────────────
    query         = request.GET.get('q', '').strip()[:100]
    brand_slug    = request.GET.get('brand', '').strip()[:80]
    sort_by       = request.GET.get('sort', 'popular').strip()
    in_stock_only = request.GET.get('in_stock') == '1'
    min_price     = _decimal_or_none(request.GET.get('min_price'))
    max_price     = _decimal_or_none(request.GET.get('max_price'))
    page_number   = request.GET.get('page', '1')

    VALID_SORTS = {'popular', 'newest', 'price_asc', 'price_desc', 'rating', 'bestseller'}
    if sort_by not in VALID_SORTS:
        sort_by = 'popular'

    # ── Cache key ────────────────────────────────────────────────────
    raw_key = (
        f"subcategoryproducts:{category_slug}:{query}:{brand_slug}:"
        f"{sort_by}:{in_stock_only}:{min_price}:{max_price}:{page_number}"
    )
    cache_key = 'subcategory_products_' + hashlib.md5(raw_key.encode()).hexdigest()

    cached = cache.get(cache_key)
    if cached:
        # View count is tracked once per request regardless of cache hit,
        # so recency stays accurate even when the page body is cached.
        subcategory.increment_view_count()
        return render(request, 'ponno/subcategory_products.html', cached)

    # ── Base queryset (active, non-deleted products in this subcategory) ─
    products = Product.objects.active_products().filter(sub_category=subcategory)

    # ── Brand filter ─────────────────────────────────────────────────
    selected_brand = None
    if brand_slug:
        selected_brand = Brand.objects.active_brands().filter(
            brand_slug=brand_slug
        ).first()
        if selected_brand:
            products = products.filter(brand=selected_brand)

    # ── Search (within subcategory) ─────────────────────────────────
    if query:
        products = products.filter(
            Q(product_name__icontains=query) |
            Q(product_title__icontains=query) |
            Q(description__icontains=query) |
            Q(short_description__icontains=query) |
            Q(sku__icontains=query) |
            Q(brand__brand_name__icontains=query)
        )

    # ── Price range ──────────────────────────────────────────────────
    if min_price is not None:
        products = products.filter(selling_price__gte=min_price)
    if max_price is not None:
        products = products.filter(selling_price__lte=max_price)

    # ── Stock filter ─────────────────────────────────────────────────
    if in_stock_only:
        products = products.filter(stock__gt=0)

    # ── Sort ─────────────────────────────────────────────────────────
    SORT_MAP = {
        'popular':    ['-view_count', '-total_sales'],
        'newest':     ['-created_at'],
        'price_asc':  ['selling_price'],
        'price_desc': ['-selling_price'],
        'rating':     ['-rating_average', '-review_count'],
        'bestseller': ['-total_sales'],
    }
    products = products.order_by(*SORT_MAP[sort_by])
    products = products.distinct()  # search across joined fields can duplicate rows

    # ── Stats (cached separately, longer TTL) ────────────────────────
    stats_key = f'subcategory_products_stats_{subcategory.pk}'
    stats = cache.get(stats_key)
    if stats is None:
        all_in_subcategory = Product.objects.active_products().filter(sub_category=subcategory)
        stats = {
            'total':    all_in_subcategory.count(),
            'in_stock': all_in_subcategory.filter(stock__gt=0).count(),
            'on_sale':  all_in_subcategory.filter(discount_percentage__gt=0).count(),
            'featured': all_in_subcategory.filter(is_featured=True).count(),
        }
        cache.set(stats_key, stats, 120)

    # ── Brands present among this subcategory's products (for filter) ─
    brands = (
        Brand.objects.active_brands()
        .filter(products__sub_category=subcategory, products__is_active=True)
        .distinct()
        .order_by('brand_name')
    )

    # ── Paginate ─────────────────────────────────────────────────────
    per_page = subcategory.products_per_page or PRODUCTS_PER_PAGE
    paginator = Paginator(products, per_page)
    try:
        page_obj = paginator.get_page(page_number)
    except InvalidPage:
        raise Http404("Invalid page number")

    # ── Format ───────────────────────────────────────────────────────
    formatted_products = [
        {
            'id':                  p.pk,
            'product_id':          str(p.product_id),
            'name':                p.product_name,
            'slug':                p.slug,
            'short_description':   p.short_description or '',
            'image':               p.image_url,
            'brand_name':          p.brand.brand_name if p.brand_id else '',
            'brand_slug':          p.brand.brand_slug if p.brand_id else '',
            'brand_logo':          p.brand.logo_url if p.brand_id else '',
            'condition':           p.product_condition,
            'selling_price':       p.selling_price,
            'final_price':         p.final_price or p.selling_price,
            'discount_percentage': p.discount_percentage,
            'is_on_sale':          p.discount_percentage > 0,
            'currency':            p.currency,
            'stock':               p.stock,
            'stock_status':        p.stock_status,
            'is_in_stock':         p.stock > 0,
            'is_featured':         p.is_featured,
            'is_trending':         p.is_trending,
            'is_verified':         p.is_verified,
            'rating_average':      p.rating_average,
            'review_count':        p.review_count,
            'view_count':          p.view_count,
            'product_url':         reverse('ponno:product_detail', kwargs={'slug': p.slug}) if p.slug else '#',
        }
        for p in page_obj
    ]

    sort_options = [
        {'value': 'popular',    'label': 'Most Popular',       'active': sort_by == 'popular'},
        {'value': 'newest',     'label': 'Newest First',       'active': sort_by == 'newest'},
        {'value': 'price_asc',  'label': 'Price: Low to High', 'active': sort_by == 'price_asc'},
        {'value': 'price_desc', 'label': 'Price: High to Low', 'active': sort_by == 'price_desc'},
        {'value': 'rating',     'label': 'Top Rated',          'active': sort_by == 'rating'},
        {'value': 'bestseller', 'label': 'Best Selling',       'active': sort_by == 'bestseller'},
    ]

    context = {
        'products':       formatted_products,
        'page_obj':       page_obj,
        'subcategory':    subcategory,
        'category':       category,
        'breadcrumb':     subcategory.breadcrumb,
        'brands':         brands,
        'selected_brand': selected_brand,
        'query':          query,
        'current_sort':   sort_by,
        'sort_options':   sort_options,
        'in_stock_only':  in_stock_only,
        'min_price':      min_price,
        'max_price':      max_price,
        'total_count':    paginator.count,
        'stats':          stats,
    }

    # Track a view (once per request, after context is built so a failed
    # render doesn't still count as a "view").
    subcategory.increment_view_count()

    cache.set(cache_key, context, CACHE_TTL)
    return render(request, 'ponno/subcategory_products.html', context)