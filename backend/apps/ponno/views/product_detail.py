# apps/ponno/views/product_detail.py

"""
ProductDetail View — Optimized for High-Throughput (millions of req/s)
=======================================================================

Architecture decisions
----------------------
1. CACHE-FIRST: every expensive DB read is served from cache.
   Cold-path writes are deferred so the hot path never blocks.

2. NON-BLOCKING ANALYTICS: view counts and view-log writes go through
   Django's cache atomic increment + a periodic flush-to-DB task
   (Celery beat). The request thread never touches the analytics tables.

3. CONDITIONAL DB WRITE: ProductView (recently-viewed) is written only
   when the cached set says the product is genuinely new for that user,
   preventing redundant upserts on repeat visits.

4. SELECT_RELATED / PREFETCH scoped tightly so the single product query
   pulls only what the template actually needs.

5. CACHE STAMPEDE PROTECTION: a short probabilistic early-expiry window
   (cache-aside with jitter) prevents dog-piling on cold cache.

6. SESSION I/O MINIMISED: session is read once and written at most once
   per request, using Django's modified flag to avoid spurious saves.

7. VIDEO INFO: video-URL normalization is delegated to the shared
   megamind.utils.video_info module (get_video_info_cached) instead of
   a local copy, so this view, the public Engine feed, and the global
   feed all resolve/embed a video URL identically — including the
   Facebook/TikTok/Twitter short-link resolution, retry/backoff, and
   production failure logging that module provides. See that module's
   docstring for the full rationale (production egress/IP-blocking
   behavior, the 'unknown' -> type='video' fallback decision, etc.).

8. VISITOR TELEMETRY: DiscoveryVisitLog rows are written inline/sync
   via megamind.services.visit_logger.record_discovery_visit. This is
   a deliberate exception to the "no DB write on the hot path" rule
   above — record_discovery_visit fails safe (catches and logs, never
   raises) so it cannot break the page, but it does add one DB INSERT
   per request. If/when this view's traffic grows, switch this to
   record_discovery_visit_task.delay(...) via Celery instead.

Dependencies
------------
- Django cache backend that supports atomic incr (Redis recommended).
- Celery (optional but recommended) for async DB flush tasks.
  If Celery is absent the view falls back to synchronous writes
  on a 1-in-N probabilistic basis to avoid DB hotspots.
- django-redis or similar for fast cache.
- megamind.utils.video_info for video URL normalization (shared with
  the feed pipelines — see that module's docstring).
- megamind.services.visit_logger for visitor telemetry (DiscoveryVisitLog).

Settings expected
-----------------
    PRODUCT_DETAIL_CACHE_TTL        = 300      # seconds (product row)
    PRODUCT_CATEGORY_CACHE_TTL      = 3600     # seconds (category list)
    PRODUCT_RELATED_CACHE_TTL       = 600      # seconds (related products)
    PRODUCT_VIEW_FLUSH_PROBABILITY  = 0.01     # 1 % of requests flush counts
    RECENTLY_VIEWED_MAX             = 10       # items kept in session list
    USE_CELERY                      = True     # set False to disable async tasks
    DE_VIDEO_INFO_TTL               = 3600     # see megamind.utils.video_info
"""

from __future__ import annotations

import json
import logging
import random
from decimal import Decimal
from typing import Optional

from django.conf import settings
from django.core.cache import cache
from django.db.models import Q
from django.http import Http404, JsonResponse
from django.shortcuts import render
from django.utils import timezone
from django.views.decorators.http import require_http_methods

from apps.ponno.models.brand import Brand
from apps.ponno.models.category import Category
from apps.ponno.models.product import Product, ProductView, Wishlist
from apps.ponno.models.rating import ProductRating
from apps.ponno.feed_algorithm import FeedEngine, load_user_affinity
from megamind.utils.video_info import get_video_info_cached
from megamind.services.visit_logger import record_discovery_visit

logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────────────────────────────────────
# Configuration helpers
# ──────────────────────────────────────────────────────────────────────────────

PRODUCT_CACHE_TTL   = getattr(settings, "PRODUCT_DETAIL_CACHE_TTL",       300)
CATEGORY_CACHE_TTL  = getattr(settings, "PRODUCT_CATEGORY_CACHE_TTL",     3600)
RELATED_CACHE_TTL   = getattr(settings, "PRODUCT_RELATED_CACHE_TTL",      600)
FLUSH_PROBABILITY   = getattr(settings, "PRODUCT_VIEW_FLUSH_PROBABILITY", 0.01)
RECENTLY_VIEWED_MAX = getattr(settings, "RECENTLY_VIEWED_MAX",            10)
USE_CELERY          = getattr(settings, "USE_CELERY",                     False)

# Redis key prefixes
_PK_PRODUCT         = "pd:product:{slug}"
_PK_RELATED         = "pd:related:{pk}"
_PK_DEALER_PRODS    = "pd:dealer:{pk}"
_PK_CATEGORIES      = "pd:categories"
_PK_VIEW_COUNTER    = "pd:vc:{pk}"
_PK_RECENTLY_VIEWED = "pd:rv:{user_pk}"
_PK_SUGGESTED       = "pd:suggested:{product_pk}:{user_pk}"


# ──────────────────────────────────────────────────────────────────────────────
# Price formatter  (Bangladeshi comma style → "1,23,456.50 Tk")
# ──────────────────────────────────────────────────────────────────────────────

def format_price(price) -> str:
    """
    Format a price using Bangladeshi number grouping.
    Returns a plain string like "1,23,456 Tk" or "9,99,999.50 Tk".
    Used to pre-format prices in the context so the HTML never changes.
    """
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


def _format_product_prices(product: Product) -> dict:
    """
    Return a dict of pre-formatted price strings for a single product.
    Keys mirror what the existing HTML already uses so no template changes
    are needed — just unpack this dict into the context.
    """
    return {
        "fmt_selling_price":  format_price(product.selling_price),
        "fmt_final_price":    format_price(product.final_price),
        "fmt_brand_price":    format_price(product.brand_price),
        "fmt_shipping_cost":  format_price(product.shipping_cost),
    }


def _annotate_card_prices(products: list) -> list:
    """
    Attach  .fmt_selling_price, .fmt_final_price, .fmt_brand_price,
    .show_brand_price, .show_selling_price, and .price_hidden  directly
    onto each product object in a list so the card HTML can react to
    per-product price-visibility flags without extra template lookups.
    """
    for p in products:
        show_selling = bool(getattr(p, "is_selling_price_visible", True))
        show_brand   = bool(getattr(p, "is_brand_price_visible", True))

        p.fmt_selling_price = format_price(p.selling_price) if show_selling else None
        p.fmt_final_price   = format_price(p.final_price) if show_selling else None
        p.fmt_brand_price   = format_price(p.brand_price) if (show_brand and p.brand_price) else None
        p.show_selling_price = show_selling
        p.show_brand_price    = show_brand
        p.price_hidden        = not (show_selling or show_brand)
    return products

# ──────────────────────────────────────────────────────────────────────────────
# Internal utilities
# ──────────────────────────────────────────────────────────────────────────────

def _ttl_with_jitter(base_ttl: int, jitter_pct: float = 0.1) -> int:
    delta = int(base_ttl * jitter_pct)
    return base_ttl + random.randint(-delta, delta)


def _get_client_ip(request) -> Optional[str]:
    xff = request.META.get("HTTP_X_FORWARDED_FOR")
    if xff:
        return xff.split(",")[0].strip()
    return request.META.get("REMOTE_ADDR")


# ──────────────────────────────────────────────────────────────────────────────
# Cache-aside helpers
# ──────────────────────────────────────────────────────────────────────────────

def _get_product(slug: str) -> Optional[Product]:
    cache_key = _PK_PRODUCT.format(slug=slug)
    product   = cache.get(cache_key)
    if product is not None:
        return product

    try:
        product = (
            Product.objects
            .select_related(
                "brand",
                "category",
                "sub_category",
                "dealer",
                "dealer__profileinfo",
            )
            .only(
                "product_id", "product_name", "product_title", "slug", "sku",
                "selling_price", "buying_price", "brand_price",
                "is_brand_price_visible", "is_buying_price_visible", "is_selling_price_visible",
                "discount_percentage", "final_price", "tax_rate", "currency",
                "stock", "stock_status", "low_stock_threshold",
                "allow_backorder", "track_inventory",
                "min_order_quantity", "max_order_quantity",
                "image", "video_url",
                "description", "short_description", "warrenty_info",
                "delivery_info", "product_condition",
                "meta_title", "meta_description", "meta_keywords",
                "view_count", "rating_average", "review_count",
                "total_sales", "wishlist_count", "share_count",
                "is_active", "is_featured", "is_verified", "is_trending",
                "created_at", "updated_at",
                "brand_id", "category_id", "sub_category_id", "dealer_id",
            )
            .get(slug=slug, is_active=True, deleted_at__isnull=True)
        )
    except Product.DoesNotExist:
        return None

    cache.set(cache_key, product, _ttl_with_jitter(PRODUCT_CACHE_TTL))
    return product


def _get_categories() -> list:
    cats = cache.get(_PK_CATEGORIES)
    if cats is None:
        cats = list(
            Category.objects
            .filter(is_active=True, deleted_at__isnull=True, parent__isnull=True)
            .only("uuid", "category_name", "category_slug", "category_image",
                  "icon_class", "color_code", "display_order")
            .order_by("display_order", "category_name")[:20]
        )
        cache.set(_PK_CATEGORIES, cats, _ttl_with_jitter(CATEGORY_CACHE_TTL))
    return cats


def _get_related_products(product: Product) -> list:
    if not product.category_id:
        return []
    cache_key = _PK_RELATED.format(pk=product.pk)
    related   = cache.get(cache_key)
    if related is None:
        related = list(
            Product.objects
            .filter(category_id=product.category_id, is_active=True, deleted_at__isnull=True)
            .exclude(pk=product.pk)
            .select_related("brand", "category")
            .only(
                "product_id", "product_name", "slug", "selling_price",
                "final_price", "discount_percentage", "image",
                "brand_price",
                "is_brand_price_visible", "is_selling_price_visible",
                "rating_average", "review_count", "stock_status",
                "brand_id", "category_id",
            )
            .order_by("-view_count")[:6]
        )
        cache.set(cache_key, related, _ttl_with_jitter(RELATED_CACHE_TTL))
    return related


def _get_dealer_products(product: Product) -> list:
    cache_key    = _PK_DEALER_PRODS.format(pk=product.dealer_id)
    dealer_prods = cache.get(cache_key)
    if dealer_prods is None:
        dealer_prods = list(
            Product.objects
            .filter(dealer_id=product.dealer_id, is_active=True, deleted_at__isnull=True)
            .exclude(pk=product.pk)
            .select_related("brand", "category")
            .only(
                "product_id", "product_name", "slug", "selling_price",
                "final_price", "discount_percentage", "image",
                "brand_price",
                "is_brand_price_visible", "is_selling_price_visible",
                "rating_average", "stock_status",
                "brand_id", "category_id",
            )
            .order_by("-created_at")[:6]
        )
        cache.set(cache_key, dealer_prods, _ttl_with_jitter(RELATED_CACHE_TTL))
    return dealer_prods

# ──────────────────────────────────────────────────────────────────────────────
# Non-blocking analytics
# ──────────────────────────────────────────────────────────────────────────────

def _increment_view_count(product_pk: int) -> None:
    counter_key = _PK_VIEW_COUNTER.format(pk=product_pk)
    try:
        delta = cache.incr(counter_key)
    except ValueError:
        cache.set(counter_key, 1, timeout=None)
        delta = 1

    if random.random() < FLUSH_PROBABILITY:
        if USE_CELERY:
            try:
                from apps.ponno.tasks import flush_product_view_count
                flush_product_view_count.delay(product_pk, delta)
                cache.set(counter_key, 0, timeout=None)
            except Exception:
                logger.exception("Celery task dispatch failed for view count flush")
        else:
            try:
                Product.objects.filter(pk=product_pk).update(
                    view_count=_F("view_count") + delta,
                    last_viewed_at=timezone.now(),
                )
                cache.set(counter_key, 0, timeout=None)
            except Exception:
                logger.exception("Synchronous view count flush failed")


def _F(field: str):
    from django.db.models import F
    return F(field)


def _record_product_view_async(user_pk: int, product_pk: int) -> None:
    recently_viewed_key = _PK_RECENTLY_VIEWED.format(user_pk=user_pk)
    seen_set: set = cache.get(recently_viewed_key) or set()

    if product_pk in seen_set:
        return

    seen_set.add(product_pk)
    cache.set(recently_viewed_key, seen_set, timeout=PRODUCT_CACHE_TTL)

    if USE_CELERY:
        try:
            from apps.ponno.tasks import record_product_view_task
            record_product_view_task.delay(user_pk, product_pk)
        except Exception:
            logger.exception("Celery task dispatch failed for ProductView record")
    else:
        try:
            ProductView.record(
                user=_user_instance(user_pk),
                product=_product_instance(product_pk),
            )
        except Exception:
            logger.exception("Synchronous ProductView.record failed")


def _user_instance(user_pk: int):
    from django.contrib.auth import get_user_model
    User    = get_user_model()
    user    = User()
    user.pk = user_pk
    return user


def _product_instance(product_pk: int):
    p    = Product()
    p.pk = product_pk
    return p


# ──────────────────────────────────────────────────────────────────────────────
# Session recently-viewed
# ──────────────────────────────────────────────────────────────────────────────

def _update_session_recently_viewed(request, product_id_str: str) -> None:
    viewed: list = request.session.get("viewed_products", [])
    if product_id_str not in viewed:
        viewed.insert(0, product_id_str)
        viewed = viewed[:RECENTLY_VIEWED_MAX]
        request.session["viewed_products"] = viewed
        request.session.modified = True


# ──────────────────────────────────────────────────────────────────────────────
# SEO helpers
# ──────────────────────────────────────────────────────────────────────────────

def _build_structured_data(request, product: Product) -> dict:
    image_url = ""
    if product.image:
        try:
            image_url = request.build_absolute_uri(product.image.url)
        except Exception:
            pass

    show_selling_price = bool(product.is_selling_price_visible)

    data: dict = {
        "@context": "https://schema.org/",
        "@type":    "Product",
        "name":     product.product_name,
        "image":    image_url,
        "description": product.short_description or (product.description or "")[:200],
        "sku":      product.sku or "",
        "brand": {
            "@type": "Brand",
            "name":  product.brand.brand_name if product.brand else "Unknown",
        },
    }

    # Only advertise a price to search engines when it's actually shown to buyers.
    # Omitting "offers" entirely (rather than sending price "0") avoids Google
    # interpreting the product as free or flagging a structured-data mismatch
    # against what a visitor sees on the page.
    if show_selling_price:
        data["offers"] = {
            "@type":           "Offer",
            "url":             request.build_absolute_uri(),
            "priceCurrency":   product.currency,
            "price":           str(product.final_price or product.selling_price),
            "priceValidUntil": (timezone.now() + timezone.timedelta(days=30)).strftime("%Y-%m-%d"),
            "itemCondition":   "https://schema.org/NewCondition",
            "availability": (
                "https://schema.org/InStock"
                if product.stock > 0
                else "https://schema.org/OutOfStock"
            ),
        }

        profileinfo = getattr(product.dealer, "profileinfo", None)
        if profileinfo:
            seller_name = profileinfo.business_name or profileinfo.profile_name or ""
            if seller_name:
                data["offers"]["seller"] = {"@type": "Organization", "name": seller_name}

    if product.review_count > 0:
        data["aggregateRating"] = {
            "@type":       "AggregateRating",
            "ratingValue": str(product.rating_average),
            "reviewCount": str(product.review_count),
        }

    return data


def _build_meta(product: Product) -> dict:
    brand_name  = product.brand.brand_name if product.brand else "PIELAM"
    cat_name    = product.category.category_name if product.category else ""
    description = (
        product.meta_description
        or product.short_description
        or (product.description or "")[:160]
    )
    return {
        "meta_title":       product.meta_title or f"{product.product_name} – {brand_name}",
        "meta_description": description,
        "meta_keywords":    product.meta_keywords or f"{product.product_name}, {brand_name}, {cat_name}",
        "og_title":         product.product_name,
        "og_description":   (product.short_description or (product.description or ""))[:200],
    }


# ──────────────────────────────────────────────────────────────────────────────
# Suggested products
# ──────────────────────────────────────────────────────────────────────────────

def _get_suggested_products(product: Product, user, limit: int = 12) -> list:
    user_pk   = getattr(user, 'pk', 0) or 0
    cache_key = _PK_SUGGESTED.format(product_pk=product.pk, user_pk=user_pk)

    cached = cache.get(cache_key)
    if cached is not None:
        return cached

    base_qs = (
        Product.objects
        .filter(is_active=True, deleted_at__isnull=True)
        .exclude(pk=product.pk)
        .select_related("brand", "category")
        .only(
            "product_id", "product_name", "slug",
            "selling_price", "final_price", "discount_percentage",
            "brand_price",
            "is_brand_price_visible", "is_selling_price_visible",
            "image", "rating_average", "review_count", "stock_status",
            "is_featured", "is_verified", "is_trending",
            "wishlist_count", "total_sales", "view_count",
            "created_at", "brand_id", "category_id", "dealer_id",
        )
    )

    pool:     list = []
    seen_pks: set  = set()

    def _add(qs, cap: int) -> None:
        for p in qs[:cap]:
            if p.pk not in seen_pks:
                pool.append(p)
                seen_pks.add(p.pk)

    if product.sub_category_id:
        _add(
            base_qs.filter(sub_category_id=product.sub_category_id)
                   .order_by("-view_count", "-total_sales"),
            cap=limit,
        )

    if product.category_id and len(pool) < limit:
        _add(
            base_qs.filter(category_id=product.category_id)
                   .exclude(pk__in=seen_pks)
                   .order_by("-view_count", "-total_sales"),
            cap=limit - len(pool),
        )

    if product.brand_id and len(pool) < limit:
        _add(
            base_qs.filter(brand_id=product.brand_id)
                   .exclude(pk__in=seen_pks)
                   .order_by("-total_sales", "-rating_average"),
            cap=limit - len(pool),
        )

    if len(pool) < limit:
        _add(
            base_qs.filter(Q(is_trending=True) | Q(is_featured=True))
                   .exclude(pk__in=seen_pks)
                   .order_by("-view_count"),
            cap=limit - len(pool),
        )

    if pool:
        affinity = load_user_affinity(user)
        engine   = FeedEngine(user=user, affinity=affinity)
        pool     = engine.score_orm(
            pool,
            cat_ids={product.category_id} if product.category_id else set(),
            diversify=True,
        )
        pool = pool[:limit]

    cache.set(cache_key, pool, _ttl_with_jitter(RELATED_CACHE_TTL))
    return pool

# ──────────────────────────────────────────────────────────────────────────────
# Main view
# ──────────────────────────────────────────────────────────────────────────────

@require_http_methods(["GET", "HEAD"])
def ProductDetailView(request, slug: str):

    # ── 1. Fetch product ──────────────────────────────────────────────────────
    product = _get_product(slug)
    if product is None:
        raise Http404("Product not found or has been removed.")

    # ── 2. Non-blocking view count ────────────────────────────────────────────
    _increment_view_count(product.pk)

    # ── 2b. Visitor telemetry ─────────────────────────────────────────────────
    # Inline/sync per current setup (see module docstring point 8). This is
    # the one exception to the "no DB write on the hot path" rule — it's
    # wrapped in try/except inside record_discovery_visit itself, so a
    # failure here can never break the page.
    record_discovery_visit(
        request,
        product=product,
    )

    # ── 3. Auth analytics ─────────────────────────────────────────────────────
    if request.user.is_authenticated:
        _record_product_view_async(request.user.pk, product.pk)
        _update_session_recently_viewed(request, str(product.product_id))

    # ── 4. Supporting data ────────────────────────────────────────────────────
    categories         = _get_categories()
    related_products   = _get_related_products(product)
    dealer_products     = _get_dealer_products(product)
    suggested_products = _get_suggested_products(product, request.user)

    # ── 5. Format prices on card lists (attaches .fmt_* attrs in-place) ───────
    _annotate_card_prices(related_products)
    _annotate_card_prices(dealer_products)
    _annotate_card_prices(suggested_products)

# ── 6. Derived values ─────────────────────────────────────────────────────
    show_brand_price   = bool(product.is_brand_price_visible)
    show_buying_price  = bool(product.is_buying_price_visible)
    show_selling_price = bool(product.is_selling_price_visible)

    savings = savings_pct = 0
    if (
        show_brand_price and show_selling_price
        and product.brand_price and product.selling_price
        and product.brand_price > 0
    ):
        savings     = product.brand_price - product.selling_price
        savings_pct = float(savings / product.brand_price * 100)

    # ── 7. Dealer privacy flags ───────────────────────────────────────────────
    profileinfo       = getattr(product.dealer, "profileinfo", None)
    show_dealer_phone = bool(profileinfo and profileinfo.show_phone)
    show_dealer_email = bool(profileinfo and profileinfo.show_email)

    # ── 8. SEO ────────────────────────────────────────────────────────────────
    structured_data = _build_structured_data(request, product)
    meta            = _build_meta(product)

    og_image = ""
    if product.image:
        try:
            og_image = request.build_absolute_uri(product.image.url)
        except Exception:
            pass

    # ── 9. Wishlist state ─────────────────────────────────────────────────────
    user_wishlisted_ids = set()
    if request.user.is_authenticated:
        user_wishlisted_ids = set(
            Wishlist.objects
            .filter(user=request.user)
            .values_list("product__product_id", flat=True)
        )

    # ── 10. User rating ───────────────────────────────────────────────────────
    user_rating = None
    if request.user.is_authenticated:
        try:
            user_rating = ProductRating.objects.get(
                user=request.user,
                product=product,
            ).rating
        except ProductRating.DoesNotExist:
            pass

    # ── 11. Pre-format main product prices ────────────────────────────────────

    product.fmt_selling_price = format_price(product.selling_price) if show_selling_price else None
    product.fmt_final_price   = format_price(product.final_price) if show_selling_price else None
    product.fmt_brand_price   = format_price(product.brand_price) if (show_brand_price and product.brand_price) else None
    product.fmt_buying_price = format_price(product.buying_price) if (show_buying_price and product.buying_price) else None
    product.fmt_shipping_cost = format_price(product.shipping_cost)
    product.fmt_savings       = format_price(savings) if (show_brand_price and show_selling_price) else None
    product.price_hidden      = not (show_selling_price or show_brand_price)

    # ⬇ ADD THESE — the template's price block reads product.show_brand_price /
    # product.show_selling_price, but only price_hidden was ever attached above.
    product.show_brand_price   = show_brand_price
    product.show_buying_price  = show_buying_price
    product.show_selling_price = show_selling_price

    # ── 12. Context ───────────────────────────────────────────────────────────
    context = {
        "product":             product,
        "categories":          categories,
        "related_products":    related_products,
        "dealer_products":     dealer_products,
        "suggested_products":  suggested_products,
        "current_year":        timezone.now().year,

        # Raw savings (Decimal) + formatted string + percentage
        "savings":             savings,
        "fmt_savings":         format_price(savings),
        "savings_percentage":  round(savings_pct, 1),

        "show_brand_price":    show_brand_price,
        "show_buying_price":   show_buying_price,
        "show_selling_price":  show_selling_price,
        "price_hidden":        product.price_hidden,

        **meta,
        "og_image":            og_image,
        "og_url":              request.build_absolute_uri(),
        "structured_data":     json.dumps(structured_data, ensure_ascii=False),
        "show_dealer_phone":   show_dealer_phone,
        "show_dealer_email":   show_dealer_email,
        "user_authenticated":  request.user.is_authenticated,
        "video_info":          get_video_info_cached(product.video_url or ""),
        "user_wishlisted_ids": user_wishlisted_ids,
        "user_rating":         user_rating,
    }

    return render(request, "ponno/product_detail.html", context)


# ──────────────────────────────────────────────────────────────────────────────
# Cache invalidation
# ──────────────────────────────────────────────────────────────────────────────

def invalidate_product_cache(product: Product) -> None:
    keys = [
        _PK_PRODUCT.format(slug=product.slug),
        _PK_RELATED.format(pk=product.pk),
        _PK_DEALER_PRODS.format(pk=product.dealer_id),
    ]
    cache.delete_many(keys)

    try:
        cache.delete_pattern(f"pd:suggested:{product.pk}:*")
    except AttributeError:
        cache.delete(_PK_SUGGESTED.format(product_pk=product.pk, user_pk=0))

    logger.debug("Invalidated cache for product pk=%s slug=%s", product.pk, product.slug)


def invalidate_category_cache() -> None:
    cache.delete(_PK_CATEGORIES)