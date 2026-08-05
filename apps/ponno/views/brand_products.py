# apps/ponno/views/brand_products_view.py

"""
BrandProducts View
------------------
Displays a brand's full profile page including:
- Complete brand information (logo, banner, description, contact, social, stats)
- Categories & SubCategories this brand's products fall under
- Paginated product grid with full product details
- Each product's dealer info:
    profile photo, display name, product count,
    follower count, profile view count
- Sort & filter by category / sub_category / stock / price
- Cache for brand header data (longer TTL) and product pages (short TTL)
"""

import hashlib

from django.conf import settings
from django.core.cache import cache
from django.core.paginator import Paginator, InvalidPage
from django.db.models import Count, Q, Avg
from django.http import Http404
from django.shortcuts import get_object_or_404, render
from django.utils import timezone

from apps.ponno.models.brand import Brand
from apps.ponno.models.category import Category
from apps.ponno.models.sub_category import SubCategory
from apps.ponno.models.product import Product


# ─────────────────────────────────────────────────────────────────────
# CONSTANTS
# ─────────────────────────────────────────────────────────────────────

PRODUCTS_PER_PAGE      = 20
CACHE_TTL_BRAND_HEADER = 300   # 5 min  — brand info changes infrequently
CACHE_TTL_PRODUCT_PAGE = 30    # 30 sec — product grid refreshes quickly

VALID_SORTS = {
    'newest', 'popular', 'price_low', 'price_high',
    'rating', 'discount', 'name_az',
}

SORT_MAP = {
    'newest':     ['-created_at'],
    'popular':    ['-view_count', '-total_sales'],
    'price_low':  ['final_price'],
    'price_high': ['-final_price'],
    'rating':     ['-rating_average', '-review_count'],
    'discount':   ['-discount_percentage'],
    'name_az':    ['product_name'],
}

# Minimal columns for the product queryset (avoids fetching heavy text fields)
PRODUCT_FIELDS = [
    'pk', 'product_id', 'slug', 'sku',
    'product_title', 'product_name', 'short_description',
    'selling_price', 'final_price', 'discount_percentage',
    'image', 'free_shipping',
    'view_count', 'rating_average', 'review_count', 'total_sales',
    'stock', 'stock_status', 'low_stock_threshold',
    'is_featured', 'is_verified', 'is_trending',
    'product_condition',
    'brand_id', 'category_id', 'sub_category_id', 'dealer_id',
    'created_at',
]


# ─────────────────────────────────────────────────────────────────────
# MAIN VIEW
# ─────────────────────────────────────────────────────────────────────

def BrandProducts(request, brand_slug):
    """
    Full brand profile page.

    URL params   : brand_slug  (from path)
    GET params   :
        category   – filter by category slug
        sub        – filter by sub_category slug
        sort       – sort key (see VALID_SORTS)
        in_stock   – 'true' → show only in-stock products
        min_price  – minimum selling price
        max_price  – maximum selling price
        page       – page number
    """

    # ── 1. Fetch brand (404 if inactive / deleted) ────────────────────
    brand = get_object_or_404(
        Brand.objects.select_related('verified_by', 'created_by'),
        brand_slug=brand_slug,
        is_active=True,
        deleted_at__isnull=True,
    )

    # Increment brand view count (fire-and-forget; no save() overhead)
    Brand.objects.filter(pk=brand.pk).update(
        view_count=brand.view_count + 1,
        last_viewed_at=timezone.now(),
    )

    # ── 2. Parse GET params ───────────────────────────────────────────
    category_slug = request.GET.get('category', '').strip()[:80]
    sub_slug      = request.GET.get('sub',      '').strip()[:80]
    sort_by       = request.GET.get('sort',     'newest').strip()
    in_stock      = request.GET.get('in_stock', 'false') == 'true'
    page_number   = request.GET.get('page', 1)

    try:
        min_price = float(request.GET.get('min_price', '') or 0) or None
    except ValueError:
        min_price = None

    try:
        max_price = float(request.GET.get('max_price', '') or 0) or None
    except ValueError:
        max_price = None

    if sort_by not in VALID_SORTS:
        sort_by = 'newest'

    # ── 3. Brand header data (cached) ─────────────────────────────────
    brand_cache_key = f'brand_header:{brand.pk}'
    brand_data      = cache.get(brand_cache_key)

    if brand_data is None:
        brand_data = _build_brand_data(brand)
        cache.set(brand_cache_key, brand_data, CACHE_TTL_BRAND_HEADER)

    # ── 4. Categories & SubCategories for this brand (cached) ─────────
    taxonomy_cache_key = f'brand_taxonomy:{brand.pk}'
    taxonomy           = cache.get(taxonomy_cache_key)

    if taxonomy is None:
        taxonomy = _build_taxonomy(brand)
        cache.set(taxonomy_cache_key, taxonomy, CACHE_TTL_BRAND_HEADER)

    # ── 5. Product queryset ───────────────────────────────────────────
    products_qs = (
        Product.objects
        .filter(
            brand=brand,
            is_active=True,
            deleted_at__isnull=True,
        )
        .only(*PRODUCT_FIELDS)
        .select_related(
            'category',
            'sub_category',
            'dealer__profileinfo',
        )
        .order_by(*SORT_MAP.get(sort_by, ['-created_at']))
    )

    # ── 6. Apply filters ──────────────────────────────────────────────
    active_category    = None
    active_sub         = None

    if category_slug:
        cat = taxonomy['category_map'].get(category_slug)
        if cat:
            active_category = cat
            products_qs = products_qs.filter(
                Q(category__category_slug=category_slug) |
                Q(category__path__startswith=cat['path'] + '/')
            )

    if sub_slug:
        sub = taxonomy['sub_map'].get(sub_slug)
        if sub:
            active_sub  = sub
            products_qs = products_qs.filter(sub_category__sub_category_slug=sub_slug)

    if in_stock:
        products_qs = products_qs.filter(stock__gt=0)

    if min_price is not None:
        products_qs = products_qs.filter(final_price__gte=min_price)

    if max_price is not None:
        products_qs = products_qs.filter(final_price__lte=max_price)

    if sort_by == 'discount':
        products_qs = products_qs.filter(discount_percentage__gt=0)

    # ── 7. Cache key for this specific page ───────────────────────────
    raw_key = (
        f"bp:{brand.pk}:{category_slug}:{sub_slug}:{sort_by}:"
        f"{min_price}:{max_price}:{in_stock}:{page_number}"
    )
    page_cache_key = 'brand_products_page_' + hashlib.md5(raw_key.encode()).hexdigest()

    cached_products = cache.get(page_cache_key)
    if cached_products:
        formatted_products, page_obj, total_count = cached_products
    else:
        total_count = products_qs.count()
        paginator   = Paginator(products_qs, PRODUCTS_PER_PAGE)
        try:
            page_obj = paginator.get_page(page_number)
        except InvalidPage:
            raise Http404

        formatted_products = _format_products(list(page_obj))
        cache.set(page_cache_key, (formatted_products, page_obj, total_count), CACHE_TTL_PRODUCT_PAGE)

    # ── 8. Sort options ───────────────────────────────────────────────
    sort_options = [
        {'value': 'newest',     'label': 'Newest First',       'active': sort_by == 'newest'},
        {'value': 'popular',    'label': 'Most Popular',        'active': sort_by == 'popular'},
        {'value': 'price_low',  'label': 'Price: Low → High',   'active': sort_by == 'price_low'},
        {'value': 'price_high', 'label': 'Price: High → Low',   'active': sort_by == 'price_high'},
        {'value': 'rating',     'label': 'Highest Rated',       'active': sort_by == 'rating'},
        {'value': 'discount',   'label': 'Best Discount',       'active': sort_by == 'discount'},
        {'value': 'name_az',    'label': 'Name: A → Z',         'active': sort_by == 'name_az'},
    ]

    # ── 9. Context ────────────────────────────────────────────────────
    context = {
        # ── Brand ─────────────────────────────────────────────────────
        'brand':               brand,
        'brand_data':          brand_data,

        # ── Taxonomy ──────────────────────────────────────────────────
        'brand_categories':    taxonomy['categories'],     # list of dicts with product_count
        'brand_sub_categories':taxonomy['sub_categories'], # list of dicts with parent slug
        'active_category':     active_category,
        'active_sub':          active_sub,
        'active_category_slug':category_slug,
        'active_sub_slug':     sub_slug,

        # ── Products ──────────────────────────────────────────────────
        'products':            formatted_products,
        'page_obj':            page_obj,
        'total_count':         total_count,

        # ── Filters / Sort ────────────────────────────────────────────
        'sort_by':             sort_by,
        'sort_options':        sort_options,
        'in_stock_only':       in_stock,
        'min_price':           min_price or '',
        'max_price':           max_price or '',
    }

    return render(request, 'ponno/brand_products.html', context)


# ─────────────────────────────────────────────────────────────────────
# BRAND DATA BUILDER
# ─────────────────────────────────────────────────────────────────────

def _build_brand_data(brand: Brand) -> dict:
    """
    Build a rich dict of brand info including live stats.
    Cached separately from product pages so brand header
    stays fast even without a full page cache hit.
    """
    from django.conf import settings as _settings

    media_url = _settings.MEDIA_URL

    def _img_url(field_value, default):
        if field_value:
            return media_url + str(field_value)
        return default

    # Live product stats for this brand
    stats = (
        Product.objects
        .filter(brand=brand, is_active=True, deleted_at__isnull=True)
        .aggregate(
            total_products=Count('pk'),
            total_views=Count('view_count'),
            avg_rating=Avg('rating_average'),
            total_sales=Count('total_sales'),
        )
    )

    return {
        # Identity
        'pk':               brand.pk,
        'uuid':             str(brand.uuid),
        'brand_name':       brand.brand_name,
        'brand_slug':       brand.brand_slug,
        'brand_type':       brand.get_brand_type_display(),
        'brand_tagline':    brand.brand_tagline or '',
        'brand_description':brand.brand_description or '',
        'brand_story':      brand.brand_story or '',

        # Media — full URLs
        'logo_url':         _img_url(brand.brand_logo,   '/static/defaults/default-brand-logo.png'),
        'banner_url':       _img_url(brand.brand_banner, '/static/defaults/default-brand-banner.png'),
        'icon_url':         _img_url(brand.brand_icon,   '/static/defaults/default-brand-icon.png'),

        # Company
        'company_name':     brand.company_name or '',
        'founded_year':     brand.founded_year,
        'age_in_years':     brand.age_in_years,
        'country_of_origin':brand.country_of_origin or '',
        'headquarters':     brand.headquarters or '',

        # Contact
        'brand_website':    brand.brand_website or '',
        'brand_email':      brand.brand_email or '',
        'brand_phone':      brand.brand_phone or '',
        'support_email':    brand.support_email or '',
        'support_phone':    brand.support_phone or '',

        # Social media
        'social_facebook':  brand.social_facebook or '',
        'social_instagram': brand.social_instagram or '',
        'social_twitter':   brand.social_twitter or '',
        'social_linkedin':  brand.social_linkedin or '',
        'social_youtube':   brand.social_youtube or '',

        # Trust & verification
        'is_verified':      brand.is_verified,
        'verification_status': brand.get_verification_status_display(),
        'verified_at':      brand.verified_at,
        'is_official':      brand.is_official,
        'is_trusted':       brand.is_trusted,
        'is_featured':      brand.is_featured,
        'is_trending':      brand.is_trending,
        'is_exclusive':     brand.is_exclusive,

        # Live stats
        'total_products':   stats['total_products'] or 0,
        'view_count':       brand.view_count,
        'avg_rating':       round(float(stats['avg_rating'] or 0), 2),
        'popularity_score': float(brand.popularity_score),

        # SEO
        'meta_title':       brand.meta_title or brand.brand_name,
        'meta_description': brand.meta_description or brand.brand_description or '',
    }


# ─────────────────────────────────────────────────────────────────────
# TAXONOMY BUILDER
# ─────────────────────────────────────────────────────────────────────

def _build_taxonomy(brand: Brand) -> dict:
    """
    Build lists of categories and sub_categories that have at least one
    active product for this brand. Also returns lookup dicts for fast
    slug → info resolution during filtering.
    """
    # Categories with live product count
    cat_qs = (
        Category.objects
        .filter(
            products__brand=brand,
            products__is_active=True,
            products__deleted_at__isnull=True,
            is_active=True,
            deleted_at__isnull=True,
        )
        .annotate(live_count=Count('products', distinct=True))
        .values(
            'pk', 'category_name', 'category_slug',
            'path', 'level', 'live_count',
        )
        .order_by('-live_count', 'category_name')
    )

    categories = [
        {**row, 'product_count': row.pop('live_count')}
        for row in cat_qs
    ]
    category_map = {c['category_slug']: c for c in categories}

    # SubCategories with live product count
    sub_qs = (
        SubCategory.objects
        .filter(
            products__brand=brand,
            products__is_active=True,
            products__deleted_at__isnull=True,
            is_active=True,
            deleted_at__isnull=True,
        )
        .annotate(live_count=Count('products', distinct=True))
        .select_related('category')
        .values(
            'pk',
            'sub_category_name',
            'sub_category_slug',
            'category__category_name',
            'category__category_slug',
            'live_count',
        )
        .order_by('category__category_name', 'sub_category_name')
    )

    sub_categories = [
        {**row, 'product_count': row.pop('live_count')}
        for row in sub_qs
    ]
    sub_map = {s['sub_category_slug']: s for s in sub_categories}

    return {
        'categories':    categories,
        'sub_categories':sub_categories,
        'category_map':  category_map,
        'sub_map':       sub_map,
    }


# ─────────────────────────────────────────────────────────────────────
# PRODUCT FORMATTER
# ─────────────────────────────────────────────────────────────────────

def _format_products(product_list: list) -> list:
    """
    Convert Product model instances into plain dicts for the template.
    Includes full dealer info: photo, display name, product count,
    follower count, profile view count.
    """
    from django.conf import settings as _settings
    media_url = _settings.MEDIA_URL

    def _img(field_value, default):
        if field_value:
            return media_url + str(field_value)
        return default

    formatted = []

    for product in product_list:

        # ── Dealer info ───────────────────────────────────────────────
        dealer_name           = 'PIELAM Seller'
        dealer_username       = ''
        dealer_photo_url      = '/static/defaults/default-profile-picture.png'
        dealer_verified       = False
        dealer_profile_url    = '#'
        dealer_product_count  = 0
        dealer_follower_count = 0
        dealer_profile_views  = 0

        try:
            dealer  = product.dealer
            profile = dealer.profileinfo

            # Safe username — used by the template for {% url 'customer:profile_view' %}
            dealer_username      = dealer.email_or_phone or ''

            dealer_name          = profile.full_name
            dealer_photo_url     = profile.get_profile_photo_url()
            dealer_verified      = profile.is_profile_verified
            dealer_profile_url   = profile.profile_url
            dealer_profile_views = profile.profile_views

            # Follower count from the M2M on ProfileInfo
            dealer_follower_count = profile.followers.count()

            # Live product count for this dealer
            dealer_product_count = (
                Product.objects
                .filter(dealer=dealer, is_active=True, deleted_at__isnull=True)
                .count()
            )

        except Exception:
            if hasattr(product, 'dealer') and product.dealer:
                dealer_name     = product.dealer.email_or_phone or 'PIELAM Seller'
                dealer_username = product.dealer.email_or_phone or ''

        # ── Stock ─────────────────────────────────────────────────────
        stock         = product.stock
        low_threshold = getattr(product, 'low_stock_threshold', 10)
        is_in_stock   = stock > 0
        is_low_stock  = 0 < stock <= low_threshold

        if stock == 0:
            stock_badge, stock_class, stock_status = 'Out of Stock', 'out-of-stock', 'Out of Stock'
        elif is_low_stock:
            stock_badge, stock_class, stock_status = f'Only {stock} left', 'low-stock', 'Low Stock'
        else:
            stock_badge, stock_class, stock_status = None, 'in-stock', 'In Stock'

        # ── Pricing ───────────────────────────────────────────────────
        discount_pct   = float(product.discount_percentage or 0)
        discount_badge = f'{int(discount_pct)}% OFF' if discount_pct > 0 else None
        final_price    = product.final_price or product.selling_price

        # ── Category / SubCategory ────────────────────────────────────
        category_name     = product.category.category_name if product.category else 'Uncategorized'
        category_slug_val = product.category.category_slug if product.category else None
        sub_category_name = None
        sub_category_slug = None
        if product.sub_category:
            sub_category_name = product.sub_category.sub_category_name
            sub_category_slug = product.sub_category.sub_category_slug

        formatted.append({
            # Identity
            'id':                product.pk,
            'uuid':              str(product.product_id),
            'slug':              product.slug,
            'sku':               product.sku or '',
            'title':             product.product_title or '',
            'product_name':      product.product_name,
            'short_description': product.short_description or '',
            'condition':         product.get_product_condition_display(),

            # Pricing
            'selling_price':       _format_price(product.selling_price),
            'final_price':         _format_price(final_price),
            'final_price_raw':     float(final_price),
            'discount_percentage': discount_pct,
            'discount_badge':      discount_badge,
            'on_sale':             discount_pct > 0,

            # Media
            'image':       _img(product.image, '/static/defaults/default-product.png'),
            'product_url': product.product_url,

            # Dealer
            'dealer_name':           dealer_name,
            'dealer_username':        dealer_username,        # ← used by profile_view URL
            'dealer_photo':           dealer_photo_url,
            'dealer_verified':        dealer_verified,
            'dealer_profile_url':     dealer_profile_url,
            'dealer_product_count':   dealer_product_count,
            'dealer_follower_count':  dealer_follower_count,
            'dealer_profile_views':   dealer_profile_views,

            # Analytics
            'views':        _format_views(product.view_count),
            'views_raw':    product.view_count,
            'rating':       float(product.rating_average or 0),
            'review_count': product.review_count,
            'total_sales':  product.total_sales,

            # Stock
            'stock':        stock,
            'stock_count':  _format_count(stock),
            'stock_status': stock_status,
            'stock_badge':  stock_badge,
            'stock_class':  stock_class,
            'is_in_stock':  is_in_stock,
            'is_low_stock': is_low_stock,
            'free_shipping':product.free_shipping,

            # Classification
            'category':          category_name,
            'category_slug':     category_slug_val,
            'sub_category':      sub_category_name,
            'sub_category_slug': sub_category_slug,

            # Flags
            'is_featured': product.is_featured,
            'is_verified': product.is_verified,
            'is_trending': product.is_trending,
        })

    return formatted


# ─────────────────────────────────────────────────────────────────────
# FORMATTING HELPERS
# ─────────────────────────────────────────────────────────────────────

def _format_count(n) -> str:
    try:
        n = int(n)
        if n >= 1_000_000:
            return f'{n/1_000_000:.1f}M'.replace('.0M', 'M')
        if n >= 1_000:
            return f'{n/1_000:.1f}k'.replace('.0k', 'k')
        return str(n)
    except (ValueError, TypeError):
        return '0'


def _format_views(n) -> str:
    try:
        n = int(n)
        if n >= 1_000_000:
            return f'{n/1_000_000:.1f}M'.replace('.0M', 'M')
        if n >= 1_000:
            return f'{n/1_000:.1f}K'.replace('.0K', 'K')
        return str(n)
    except (ValueError, TypeError):
        return '0'


def _format_price(price) -> str:
    try:
        return f'{int(price):,} Tk' if price else '0 Tk'
    except (ValueError, TypeError):
        return '0 Tk'

