# engine/business_engine/business_profile.py

"""
Business Profile View — role-aware, optimised for high throughput.

Cache topology
──────────────
  biz:base:{uid}            8 min   Core identity (profile, social counts)
  biz:role:{role}:{uid}     5 min   Role-specific context
  biz:brands:{uid}          5 min   Dealer brands list
  biz:cats:{uid}            5 min   Dealer categories list
  biz:subcats:{uid}         5 min   Dealer subcategories list
  biz:analytics:{uid}       5 min   Dealer product + order analytics
  biz:activity:{uid}        3 min   Recent views, wishlists, searches (higher churn)
  biz:ratings:{uid}         5 min   Per-product rating aggregates

Query budget (all cache-miss paths)
────────────────────────────────────
  _load_base_context          →  3 queries  (profile, followers COUNT, following COUNT)
  _load_customer_context      →  0 queries  (pure config)
  _load_dealer_context        →  6 queries  (catalog + brands + categories + subcategories)
  _load_dealer_analytics      →  6 queries  (products, orders, views, wishlists, ratings, searches)
  _load_dealer_activity       →  3 queries  (recent views, top searches, top wishlist products)
  _load_staff_context         →  0 queries  (metadata read from profile_info)
  _load_moderator_context     →  0 queries  (pure config)
  _load_admin_context         →  1 query    (user aggregate)
  ─────────────────────────────────────────
  Worst-case cold path        → 19 queries  (dealer: 3 base + 6 catalog + 6 analytics + 3 activity + 1 ratings)
  Warm path                   →  0 DB queries
"""

import logging

from django.shortcuts import render, get_object_or_404
from django.contrib.auth.decorators import login_required
from django.core.cache import cache
from django.db.models import (
    Avg, Count, DecimalField, ExpressionWrapper, F,
    Max, Min, Q, Sum,
)
from django.utils import timezone

from django.db.models import Avg, Count, Q, Sum
from apps.customer.models.account import User
from apps.customer.models.profile_info import ProfileInfo

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════
# TTLs
# ═══════════════════════════════════════════════════════════════════

BASE_TTL      = 60 * 8   # 8 min — identity / social changes infrequently
ROLE_TTL      = 60 * 5   # 5 min — product stats are mid-frequency
CATALOG_TTL   = 60 * 5   # 5 min — brands / categories / subcategories
ANALYTICS_TTL = 60 * 5   # 5 min — aggregated order / view / rating data
ACTIVITY_TTL  = 60 * 3   # 3 min — recent views / searches churn faster
RATINGS_TTL   = 60 * 5   # 5 min — rating aggregates


# ═══════════════════════════════════════════════════════════════════
# CACHE KEY HELPERS
# ═══════════════════════════════════════════════════════════════════

def _key_base(uid: int)              -> str: return f"biz:base:{uid}"
def _key_role(role: str, uid: int)   -> str: return f"biz:role:{role}:{uid}"
def _key_brands(uid: int)            -> str: return f"biz:brands:{uid}"
def _key_cats(uid: int)              -> str: return f"biz:cats:{uid}"
def _key_subcats(uid: int)           -> str: return f"biz:subcats:{uid}"
def _key_analytics(uid: int)         -> str: return f"biz:analytics:{uid}"
def _key_activity(uid: int)          -> str: return f"biz:activity:{uid}"
def _key_ratings(uid: int)           -> str: return f"biz:ratings:{uid}"


# ═══════════════════════════════════════════════════════════════════
# CACHE INVALIDATION
# ═══════════════════════════════════════════════════════════════════

def invalidate_business_cache(user_id: int, role: str) -> None:
    """
    Full invalidation — call from signals on ProfileInfo / Product /
    Brand / Category / SubCategory / Order / Rating changes.
    """
    cache.delete_many([
        _key_base(user_id),
        _key_role(role, user_id),
        _key_brands(user_id),
        _key_cats(user_id),
        _key_subcats(user_id),
        _key_analytics(user_id),
        _key_activity(user_id),
        _key_ratings(user_id),
    ])


def invalidate_base_cache(user_id: int) -> None:
    """
    Lightweight bust — call after profile photo, bio, or
    follow/unfollow changes (does not touch role/catalog slices).
    """
    cache.delete(_key_base(user_id))


def invalidate_role_cache(user_id: int, role: str) -> None:
    """Bust after product create / update / delete."""
    cache.delete(_key_role(role, user_id))


def invalidate_brand_cache(user_id: int) -> None:
    """Bust after brand create / update / delete."""
    cache.delete(_key_brands(user_id))


def invalidate_category_cache(user_id: int) -> None:
    """Bust after category create / update / delete."""
    cache.delete(_key_cats(user_id))


def invalidate_sub_category_cache(user_id: int) -> None:
    """Bust after subcategory create / update / delete."""
    cache.delete(_key_subcats(user_id))


def invalidate_analytics_cache(user_id: int) -> None:
    """
    Bust after any order status change, payment update,
    or bulk product operation.
    """
    cache.delete(_key_analytics(user_id))


def invalidate_activity_cache(user_id: int) -> None:
    """Bust after view / wishlist / search record changes."""
    cache.delete(_key_activity(user_id))


def invalidate_ratings_cache(user_id: int) -> None:
    """Bust after any ProductRating insert or update."""
    cache.delete(_key_ratings(user_id))


# ═══════════════════════════════════════════════════════════════════
# DATA LOADERS  (each called only on a cache miss)
# ═══════════════════════════════════════════════════════════════════

def _load_base_context(user) -> dict:
    """
    Fields every role gets: profile, social counts.
    ~3 DB queries on cache miss.

    Queries
    ───────
    1. ProfileInfo + user  (select_related)
    2. followers M2M       COUNT(*)
    3. following M2M       COUNT(*)
    """
    profile_info = get_object_or_404(
        ProfileInfo.objects.select_related('user'),
        user=user,
    )

    follower_count  = profile_info.followers.count()
    following_count = profile_info.following.count()

    return {
        "user":                  user,
        "profile_info":          profile_info,
        "role":                  user.role,
        "is_verified":           profile_info.is_profile_verified,
        "verification_level":    profile_info.verification_level,
        "completion_percentage": profile_info.completion_percentage,
        "profile_complete":      profile_info.is_complete,
        "follower_count":        follower_count,
        "following_count":       following_count,
    }


def _load_customer_context(user, profile_info) -> dict:
    """
    Extra context for Role.CUSTOMER.
    Pure config — 0 DB queries.
    """
    return {
        "show_upgrade_prompt":   True,
        "show_business_section": False,
        "show_moderation_tools": False,
        "show_admin_panel_link": False,

        "privacy": {
            "show_email":      profile_info.show_email,
            "show_phone":      profile_info.show_phone,
            "show_location":   profile_info.show_location,
            "show_followers":  profile_info.show_followers,
            "allow_messages":  profile_info.allow_messages,
        },

        "notifications": {
            "email":      profile_info.email_notifications,
            "sms":        profile_info.sms_notifications,
            "on_follow":  profile_info.notify_on_follow,
            "on_message": profile_info.notify_on_message,
        },
    }


def _load_dealer_context(user, profile_info) -> dict:
    """
    Core catalog context for Role.DEALER.
    ~6 DB queries on cache miss.

    Queries
    ───────
    1. Product aggregate  — total / active / pending / featured / trending
    2. Brand list         — filter(created_by=user)
    3. Category list      — filter(brand__created_by=user)
    4. SubCategory list   — filter(brand__created_by=user)
    5. Stock summary      — in_stock / low_stock / out_of_stock counts
    6. Top-5 products     — by total_sales

    business_completion is derived from already-loaded profile_info — no extra DB hit.
    """
    from apps.ponno.models.product      import Product
    from apps.ponno.models.brand        import Brand
    from apps.ponno.models.category     import Category
    from apps.ponno.models.sub_category import SubCategory

    base_qs = Product.objects.filter(dealer=user, deleted_at__isnull=True)

    # ── 1. Product aggregate ─────────────────────────────────────
    product_stats = base_qs.aggregate(
        total_products    = Count('id'),
        active_products   = Count('id', filter=Q(is_active=True)),
        pending_products  = Count('id', filter=Q(is_active=False)),
        featured_products = Count('id', filter=Q(is_featured=True)),
        trending_products = Count('id', filter=Q(is_trending=True)),
    )

    # ── 2. Brands ────────────────────────────────────────────────
    brands = list(
        Brand.objects
        .filter(created_by=user, deleted_at__isnull=True)
        .only(
            'id', 'brand_name', 'brand_slug', 'brand_type',
            'brand_logo', 'brand_tagline',
            'is_verified', 'verification_status',
            'is_active', 'is_featured', 'is_official',
            'is_trusted', 'is_trending', 'is_exclusive',
            'product_count', 'view_count',
            'company_name', 'country_of_origin',
            'brand_website', 'brand_email', 'brand_phone',
            'brand_created_at',
        )
        .order_by('display_order', 'brand_name')
    )

    total_brands          = len(brands)
    active_brands         = sum(1 for b in brands if b.is_active)
    verified_brands_count = sum(1 for b in brands if b.is_verified)

    # ── 3. Categories ────────────────────────────────────────────
# ── 3. Categories ────────────────────────────────────────────
    categories = list(
        Category.objects
        .filter(created_by=user, deleted_at__isnull=True)   # ← was brand__created_by=user
        .select_related('parent', 'brand')
        .only(
            'id', 'category_name', 'category_slug', 'category_type',
            'category_image', 'category_icon',
            'icon_class', 'color_code',
            'is_active', 'is_featured', 'is_trending',
            'level', 'path', 'parent',
            'product_count', 'view_count',
            'display_order', 'category_created_at',
            'brand',
        )
        .order_by('display_order', 'category_name')
    )

    total_categories  = len(categories)
    active_categories = sum(1 for c in categories if c.is_active)
    root_categories   = sum(1 for c in categories if c.parent is None)

    # ── 4. SubCategories ─────────────────────────────────────────
    sub_categories = list(
        SubCategory.objects
        .filter(created_by=user, deleted_at__isnull=True)
        .select_related('category', 'brand')
        .only(
            'id', 'uuid', 'sub_category_name', 'sub_category_slug', 'sub_category_type',
            'sub_category_image', 'sub_category_icon', 'sub_category_thumbnail',
            'icon_class', 'color_code',
            'is_active', 'is_featured', 'is_trending',
            'is_visible_in_menu', 'is_visible_on_homepage',
            'category', 'category__category_name', 'category__category_slug',
            'brand', 'brand__brand_name',
            'product_count', 'view_count', 'popularity_score',
            'display_order', 'sub_category_created_at',
        )
        .order_by('display_order', 'sub_category_name')
    )

    total_sub_categories    = len(sub_categories)
    active_sub_categories   = sum(1 for s in sub_categories if s.is_active)
    featured_sub_categories = sum(1 for s in sub_categories if s.is_featured)
    trending_sub_categories = sum(1 for s in sub_categories if s.is_trending)

    # ── 5. Stock summary ─────────────────────────────────────────
    stock_stats = base_qs.filter(is_active=True).aggregate(
        in_stock     = Count('id', filter=Q(stock_status='in_stock')),
        low_stock    = Count('id', filter=Q(stock_status='low_stock')),
        out_of_stock = Count('id', filter=Q(stock_status='out_of_stock')),
        pre_order    = Count('id', filter=Q(stock_status='pre_order')),
    )

    # ── 6. Top-5 products by sales ───────────────────────────────
    top_products = list(
        base_qs
        .filter(is_active=True)
        .only(
            'id', 'product_name', 'slug', 'image',
            'selling_price', 'final_price', 'total_sales',
            'revenue_generated', 'rating_average', 'review_count',
        )
        .order_by('-total_sales')[:5]
    )

    # ── Business completion ──────────────────────────────────────
    business_fields = [
        profile_info.business_name,
        profile_info.business_email,
        profile_info.business_phone,
        profile_info.business_registration,
        profile_info.business_description,
        profile_info.business_website,
    ]
    business_completion = int(
        sum(1 for f in business_fields if f) / len(business_fields) * 100
    )

    return {
        "show_upgrade_prompt":   False,
        "show_business_section": True,
        "show_moderation_tools": False,
        "show_admin_panel_link": False,

        # ── Business info ────────────────────────────────────────
        "business_name":         profile_info.business_name,
        "business_type":         profile_info.business_type,
        "business_email":        profile_info.business_email,
        "business_phone":        profile_info.business_phone,
        "business_website":      profile_info.business_website,
        "business_description":  profile_info.business_description,
        "business_registration": profile_info.business_registration,
        "business_completion":   business_completion,
        "business_verified": (
            profile_info.verification_level == ProfileInfo.VerificationLevel.BUSINESS
        ),

        # ── Products ─────────────────────────────────────────────
        "total_products":     product_stats["total_products"]    or 0,
        "active_products":    product_stats["active_products"]   or 0,
        "pending_products":   product_stats["pending_products"]  or 0,
        "featured_products":  product_stats["featured_products"] or 0,
        "trending_products":  product_stats["trending_products"] or 0,

        # ── Stock ────────────────────────────────────────────────
        "stock_in_stock":     stock_stats["in_stock"]     or 0,
        "stock_low_stock":    stock_stats["low_stock"]    or 0,
        "stock_out_of_stock": stock_stats["out_of_stock"] or 0,
        "stock_pre_order":    stock_stats["pre_order"]    or 0,

        # ── Top products ─────────────────────────────────────────
        "top_products": top_products,

        # ── Brands ───────────────────────────────────────────────
        "dealer_brands":          brands,
        "total_brands":           total_brands,
        "active_brands":          active_brands,
        "verified_brands_count":  verified_brands_count,

        # ── Categories ───────────────────────────────────────────
        "dealer_categories":  categories,
        "total_categories":   total_categories,
        "active_categories":  active_categories,
        "root_categories":    root_categories,

        # ── SubCategories ────────────────────────────────────────
        "dealer_sub_categories":  sub_categories,
        "total_sub_categories":   total_sub_categories,
        "active_sub_categories":  active_sub_categories,

        # ── Privacy ──────────────────────────────────────────────
        "privacy": {
            "show_email":     profile_info.show_email,
            "show_phone":     profile_info.show_phone,
            "show_location":  profile_info.show_location,
            "show_followers": profile_info.show_followers,
            "allow_messages": profile_info.allow_messages,
        },
    }


def _load_dealer_analytics(user) -> dict:
    """
    Order, revenue, rating, and view analytics for Role.DEALER.
    ~6 DB queries on cache miss.

    Queries
    ───────
    1. Order aggregate    — totals, status breakdown, revenue
    2. OrderItem totals   — units sold, top-selling products
    3. ProductViewLog     — total views + device breakdown (30-day)
    4. Wishlist count     — how many users have wishlisted dealer products
    5. ProductRating      — avg rating, rating distribution (1–5)
    6. Revenue by month   — last 6 months (for sparkline)
    """
    from apps.ponno.models.product        import Product
    from apps.ponno.models.product_view_log import ProductViewLog
    from apps.ponno.models.rating         import ProductRating
    from apps.ponno.models.product  import Order, OrderItem, Wishlist

    dealer_product_ids = list(
        Product.objects
        .filter(dealer=user, deleted_at__isnull=True)
        .values_list('id', flat=True)
    )

    # ── 1. Order aggregate ───────────────────────────────────────
    order_stats = (
        Order.objects
        .filter(items__product_id__in=dealer_product_ids)
        .aggregate(
            total_orders      = Count('id', distinct=True),
            pending_orders    = Count('id', distinct=True, filter=Q(status='pending')),
            confirmed_orders  = Count('id', distinct=True, filter=Q(status='confirmed')),
            processing_orders = Count('id', distinct=True, filter=Q(status='processing')),
            shipped_orders    = Count('id', distinct=True, filter=Q(status='shipped')),
            delivered_orders  = Count('id', distinct=True, filter=Q(status='delivered')),
            cancelled_orders  = Count('id', distinct=True, filter=Q(status='cancelled')),
            refunded_orders   = Count('id', distinct=True, filter=Q(status='refunded')),
            paid_orders       = Count('id', distinct=True, filter=Q(payment_status='paid')),
            unpaid_orders     = Count('id', distinct=True, filter=Q(payment_status='unpaid')),
        )
    )

    # ── 2. OrderItem totals ──────────────────────────────────────
    item_stats = (
        OrderItem.objects
        .filter(product_id__in=dealer_product_ids)
        .aggregate(
            total_units_sold = Sum('quantity'),
            gross_revenue    = Sum('line_total'),
        )
    )

# ── 3. Product views ─────────────────────────────────────────
    # All-time: sum cached view_count on Product rows (no extra join)
    total_product_views = (
        Product.objects
        .filter(dealer=user, deleted_at__isnull=True)
        .aggregate(total=Sum('view_count'))
    )['total'] or 0

    # Last 30 days: detailed breakdown from ProductViewLog
    thirty_days_ago = timezone.now() - timezone.timedelta(days=30)
    view_qs = (
        ProductViewLog.objects
        .filter(
            product_id__in=dealer_product_ids,
            deleted_at__isnull=True,
            viewed_at__gte=thirty_days_ago,
        )
    )
    view_stats = view_qs.aggregate(
        total_view_events = Count('id'),
        unique_viewers    = Count('viewer', distinct=True),
        desktop_views     = Count('id', filter=Q(device_type='desktop')),
        mobile_views      = Count('id', filter=Q(device_type='mobile')),
        tablet_views      = Count('id', filter=Q(device_type='tablet')),
        direct_views      = Count('id', filter=Q(source='direct')),
        search_views      = Count('id', filter=Q(source='search')),
        social_views      = Count('id', filter=Q(source='social')),
        referral_views    = Count('id', filter=Q(source='referral')),
    )

    # ── 4. Wishlist count ────────────────────────────────────────
    wishlist_total = (
        Wishlist.objects
        .filter(product_id__in=dealer_product_ids)
        .count()
    )

    # ── 5. ProductRating aggregates ──────────────────────────────
    rating_qs = (
        ProductRating.objects
        .filter(product_id__in=dealer_product_ids)
    )
    rating_stats = rating_qs.aggregate(
        avg_rating    = Avg('rating'),
        total_ratings = Count('id'),
        five_star     = Count('id', filter=Q(rating=5)),
        four_star     = Count('id', filter=Q(rating=4)),
        three_star    = Count('id', filter=Q(rating=3)),
        two_star      = Count('id', filter=Q(rating=2)),
        one_star      = Count('id', filter=Q(rating=1)),
    )

    # ── 6. Monthly revenue — last 6 months ───────────────────────
    #
    # Annotate each OrderItem with its order's placed_at month, then
    # group and sum. We pull raw rows and bucket in Python to avoid
    # DB-specific date-truncation functions.
    six_months_ago = timezone.now() - timezone.timedelta(days=183)
    monthly_rows = (
        OrderItem.objects
        .filter(
            product_id__in=dealer_product_ids,
            order__placed_at__gte=six_months_ago,
        )
        .annotate(month=F('order__placed_at'))
        .values('month', 'line_total')
    )

    monthly_revenue: dict[str, float] = {}
    for row in monthly_rows:
        key = row['month'].strftime('%Y-%m')
        monthly_revenue[key] = monthly_revenue.get(key, 0.0) + float(row['line_total'] or 0)

    # Sort ascending for sparkline rendering
    monthly_revenue_series = [
        {"month": k, "revenue": v}
        for k, v in sorted(monthly_revenue.items())
    ]

    return {
        # ── Orders ───────────────────────────────────────────────
        "total_orders":      order_stats["total_orders"]      or 0,
        "pending_orders":    order_stats["pending_orders"]    or 0,
        "confirmed_orders":  order_stats["confirmed_orders"]  or 0,
        "processing_orders": order_stats["processing_orders"] or 0,
        "shipped_orders":    order_stats["shipped_orders"]    or 0,
        "delivered_orders":  order_stats["delivered_orders"]  or 0,
        "cancelled_orders":  order_stats["cancelled_orders"]  or 0,
        "refunded_orders":   order_stats["refunded_orders"]   or 0,
        "paid_orders":       order_stats["paid_orders"]       or 0,
        "unpaid_orders":     order_stats["unpaid_orders"]     or 0,

        # ── Revenue ──────────────────────────────────────────────
        "total_units_sold":        int(item_stats["total_units_sold"] or 0),
        "gross_revenue":           float(item_stats["gross_revenue"]  or 0),
        "monthly_revenue_series":  monthly_revenue_series,

        # ── Views (30-day) ────────────────────────────────────────
        "total_view_events": view_stats["total_view_events"] or 0,
        "unique_viewers":    view_stats["unique_viewers"]    or 0,
        "total_product_views": total_product_views,
        "device_breakdown": {
            "desktop":  view_stats["desktop_views"]  or 0,
            "mobile":   view_stats["mobile_views"]   or 0,
            "tablet":   view_stats["tablet_views"]   or 0,
        },
        "source_breakdown": {
            "direct":   view_stats["direct_views"]   or 0,
            "search":   view_stats["search_views"]   or 0,
            "social":   view_stats["social_views"]   or 0,
            "referral": view_stats["referral_views"] or 0,
        },

        # ── Wishlists ─────────────────────────────────────────────
        "wishlist_total": wishlist_total,

        # ── Ratings ──────────────────────────────────────────────
        "avg_rating":    round(float(rating_stats["avg_rating"] or 0), 2),
        "total_ratings": rating_stats["total_ratings"] or 0,
        "rating_distribution": {
            5: rating_stats["five_star"]  or 0,
            4: rating_stats["four_star"]  or 0,
            3: rating_stats["three_star"] or 0,
            2: rating_stats["two_star"]   or 0,
            1: rating_stats["one_star"]   or 0,
        },
    }


def _load_dealer_activity(user) -> dict:
    """
    Recent activity signals for Role.DEALER — high-churn data kept
    in a short-TTL cache slice (3 min).
    ~3 DB queries on cache miss.

    Queries
    ───────
    1. ProductViewLog  — 10 most-recently-viewed products (7 days)
    2. SearchHistory   — top-10 search terms for dealer's products (30 days)
    3. Wishlist        — top-5 most-wishlisted dealer products
    """
    from apps.ponno.models.product          import Product
    from apps.ponno.models.product_view_log import ProductViewLog
    from apps.ponno.models.product    import Wishlist, SearchHistory

    dealer_product_ids = list(
        Product.objects
        .filter(dealer=user, deleted_at__isnull=True)
        .values_list('id', flat=True)
    )

    seven_days_ago  = timezone.now() - timezone.timedelta(days=7)
    thirty_days_ago = timezone.now() - timezone.timedelta(days=30)

    # ── 1. Recently viewed products ──────────────────────────────
    recent_views = list(
        ProductViewLog.objects
        .filter(
            product_id__in=dealer_product_ids,
            deleted_at__isnull=True,
            viewed_at__gte=seven_days_ago,
        )
        .select_related('product')
        .only(
            'product__id', 'product__product_name',
            'product__slug', 'product__image',
            'viewed_at', 'viewer_id', 'device_type', 'source',
        )
        .order_by('-viewed_at')[:10]
    )

    # ── 2. Top search terms (approximate — by result product overlap) ──
    #
    # SearchHistory has no direct product FK; we surface the top terms
    # by total search_count across all users for the last 30 days.
    # This is a platform-wide signal, not dealer-scoped, so it's useful
    # as a "what are people searching for" hint.
    top_searches = list(
        SearchHistory.objects
        .filter(searched_at__gte=thirty_days_ago)
        .values('query')
        .annotate(total=Sum('search_count'))
        .order_by('-total')[:10]
    )

    # ── 3. Most-wishlisted dealer products ───────────────────────
    top_wishlisted = list(
        Wishlist.objects
        .filter(product_id__in=dealer_product_ids)
        .values('product__id', 'product__product_name', 'product__slug')
        .annotate(wishlist_count=Count('id'))
        .order_by('-wishlist_count')[:5]
    )

    return {
        "recent_views":   recent_views,
        "top_searches":   top_searches,
        "top_wishlisted": top_wishlisted,
    }


def _load_dealer_ratings(user) -> dict:
    """
    Per-product rating details — kept in its own cache slice so
    a single new rating only busts this key, not the full role context.
    ~1 DB query on cache miss.

    Query
    ─────
    1. ProductRating annotated per product — avg + count
    """
    from apps.ponno.models.product import Product
    from apps.ponno.models.rating  import ProductRating

    dealer_product_ids = list(
        Product.objects
        .filter(dealer=user, deleted_at__isnull=True, is_active=True)
        .values_list('id', flat=True)
    )

    per_product_ratings = list(
        ProductRating.objects
        .filter(product_id__in=dealer_product_ids)
        .values('product__id', 'product__product_name', 'product__slug')
        .annotate(
            avg_rating    = Avg('rating'),
            total_ratings = Count('id'),
        )
        .order_by('-avg_rating')[:20]
    )

    return {"per_product_ratings": per_product_ratings}


def _load_staff_context(user, profile_info) -> dict:
    """
    Extra context for Role.STAFF.
    0 DB queries — reads from profile_info.metadata.
    """
    return {
        "show_upgrade_prompt":   False,
        "show_business_section": False,
        "show_moderation_tools": False,
        "show_admin_panel_link": False,
        "show_staff_badge":      True,
        "department":            profile_info.metadata.get("department", ""),
        "staff_since":           profile_info.metadata.get("staff_since", ""),
    }


def _load_moderator_context(user, profile_info) -> dict:
    """
    Extra context for Role.MODERATOR.
    0 DB queries — pure config.
    """
    return {
        "show_upgrade_prompt":   False,
        "show_business_section": False,
        "show_moderation_tools": True,
        "show_admin_panel_link": False,
        "show_staff_badge":      True,
        "moderation_note":       "You have content moderation privileges.",
    }


def _load_admin_context(user, profile_info) -> dict:
    """
    Extra context for Role.ADMIN.
    ~1 DB query — user counts via single aggregate.
    """
    user_stats = (
        User.objects
        .filter(is_active=True, deleted_at__isnull=True)
        .aggregate(
            total_users   = Count('id'),
            total_dealers = Count('id', filter=Q(role=User.Role.DEALER)),
        )
    )

    return {
        "show_upgrade_prompt":   False,
        "show_business_section": False,
        "show_moderation_tools": True,
        "show_admin_panel_link": True,
        "show_staff_badge":      True,

        "admin_stats": {
            "total_users":   user_stats["total_users"]   or 0,
            "total_dealers": user_stats["total_dealers"] or 0,
        },
    }


# ═══════════════════════════════════════════════════════════════════
# ROLE DISPATCH MAP
# ═══════════════════════════════════════════════════════════════════

_ROLE_LOADERS = {
    User.Role.USER:  _load_customer_context,
    User.Role.BUSINESS:    _load_dealer_context,
    User.Role.STAFF:     _load_staff_context,
    User.Role.MODERATOR: _load_moderator_context,
    User.Role.ADMIN:     _load_admin_context,
}


# ═══════════════════════════════════════════════════════════════════
# MAIN VIEW
# ═══════════════════════════════════════════════════════════════════

@login_required
def BusinessProfileView(request):
    """
    Business profile view — role-aware, cache-optimised.

    Cache strategy
    ──────────────
    Dealers get five independent cache keys, all read in one
    get_many() round-trip:

      biz:base:{uid}         — shared across all roles
      biz:role:{role}:{uid}  — catalog context (brands, categories, products)
      biz:analytics:{uid}    — orders, revenue, views, ratings, wishlists
      biz:activity:{uid}     — recent views, top searches, top wishlisted (3-min TTL)
      biz:ratings:{uid}      — per-product rating breakdown

    Non-dealer roles only use biz:base and biz:role.

    On a miss, only the missing slice hits the DB.
    Writes back via a single set_many() call.
    """
    uid  = request.user.pk
    role = str(request.user.role)
    is_dealer = (request.user.role == User.Role.DEALER)

    cache_keys = [_key_base(uid), _key_role(role, uid)]
    if is_dealer:
        cache_keys += [_key_analytics(uid), _key_activity(uid), _key_ratings(uid)]

    # ── Batch cache read ─────────────────────────────────────────
    cached       = cache.get_many(cache_keys)
    base_ctx     = cached.get(_key_base(uid))
    role_ctx     = cached.get(_key_role(role, uid))
    analytics_ctx = cached.get(_key_analytics(uid)) if is_dealer else None
    activity_ctx  = cached.get(_key_activity(uid))  if is_dealer else None
    ratings_ctx   = cached.get(_key_ratings(uid))   if is_dealer else None

    to_set = {}

    # ── Cache misses → DB loaders ─────────────────────────────────
    if base_ctx is None:
        try:
            base_ctx = _load_base_context(request.user)
            to_set[_key_base(uid)] = (base_ctx, BASE_TTL)
        except Exception:
            logger.exception("biz base ctx load failed for user %s", uid)
            raise

    profile_info = base_ctx["profile_info"]

    if role_ctx is None:
        loader = _ROLE_LOADERS.get(request.user.role)
        if loader:
            try:
                role_ctx = loader(request.user, profile_info)
                to_set[_key_role(role, uid)] = (role_ctx, ROLE_TTL)
            except Exception:
                logger.exception(
                    "biz role ctx load failed for user %s (role=%s)", uid, role
                )
                raise
        else:
            logger.warning(
                "BusinessProfileView: unrecognised role '%s' for user %s", role, uid
            )
            role_ctx = {
                "show_upgrade_prompt":   False,
                "show_business_section": False,
                "show_moderation_tools": False,
                "show_admin_panel_link": False,
            }

    # Dealer-only extra slices
    if is_dealer:
        if analytics_ctx is None:
            try:
                analytics_ctx = _load_dealer_analytics(request.user)
                to_set[_key_analytics(uid)] = (analytics_ctx, ANALYTICS_TTL)
            except Exception:
                logger.exception("biz analytics ctx load failed for user %s", uid)
                raise

        if activity_ctx is None:
            try:
                activity_ctx = _load_dealer_activity(request.user)
                to_set[_key_activity(uid)] = (activity_ctx, ACTIVITY_TTL)
            except Exception:
                logger.exception("biz activity ctx load failed for user %s", uid)
                raise

        if ratings_ctx is None:
            try:
                ratings_ctx = _load_dealer_ratings(request.user)
                to_set[_key_ratings(uid)] = (ratings_ctx, RATINGS_TTL)
            except Exception:
                logger.exception("biz ratings ctx load failed for user %s", uid)
                raise

    # ── Repopulate cache (single set_many call) ───────────────────
    if to_set:
        # Each entry's TTL may differ; set_many takes a single timeout.
        # Use the minimum so nothing overstays. For finer control you
        # can call cache.set() individually per key.
        cache.set_many(
            {k: v for k, (v, _) in to_set.items()},
            timeout=min(ttl for _, ttl in to_set.values()),
        )

    # ── Assemble final context ────────────────────────────────────
    context = {**base_ctx, **role_ctx}

    if is_dealer:
        context.update(analytics_ctx or {})
        context.update(activity_ctx  or {})
        context.update(ratings_ctx   or {})

    return render(request, "business/business_profile.html", context)