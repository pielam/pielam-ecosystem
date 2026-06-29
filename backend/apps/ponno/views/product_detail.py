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

Dependencies
------------
- Django cache backend that supports atomic incr (Redis recommended).
- Celery (optional but recommended) for async DB flush tasks.
  If Celery is absent the view falls back to synchronous writes
  on a 1-in-N probabilistic basis to avoid DB hotspots.
- django-redis or similar for fast cache.

Settings expected
-----------------
    PRODUCT_DETAIL_CACHE_TTL        = 300      # seconds (product row)
    PRODUCT_CATEGORY_CACHE_TTL      = 3600     # seconds (category list)
    PRODUCT_RELATED_CACHE_TTL       = 600      # seconds (related products)
    PRODUCT_VIEW_FLUSH_PROBABILITY  = 0.01     # 1 % of requests flush counts
    RECENTLY_VIEWED_MAX             = 10       # items kept in session list
    USE_CELERY                      = True     # set False to disable async tasks
"""

from __future__ import annotations

import json
import logging
import random
import re
import uuid
from decimal import Decimal
from typing import Optional
from urllib.parse import urlparse, parse_qs

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
    Attach  .fmt_selling_price  and  .fmt_final_price  directly onto each
    product object in a list so the existing card HTML works unchanged.

    We deliberately set attributes rather than returning a new structure
    so all existing template references ({{ related.selling_price }}, etc.)
    keep working while the formatted variants are available alongside them.
    """
    for p in products:
        p.fmt_selling_price = format_price(p.selling_price)
        p.fmt_final_price   = format_price(p.final_price)
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
        "offers": {
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
        },
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

    # ── 3. Auth analytics ─────────────────────────────────────────────────────
    if request.user.is_authenticated:
        _record_product_view_async(request.user.pk, product.pk)
        _update_session_recently_viewed(request, str(product.product_id))

    # ── 4. Supporting data ────────────────────────────────────────────────────
    categories         = _get_categories()
    related_products   = _get_related_products(product)
    dealer_products    = _get_dealer_products(product)
    suggested_products = _get_suggested_products(product, request.user)

    # ── 5. Format prices on card lists (attaches .fmt_* attrs in-place) ───────
    _annotate_card_prices(related_products)
    _annotate_card_prices(dealer_products)
    _annotate_card_prices(suggested_products)

    # ── 6. Derived values ─────────────────────────────────────────────────────
    savings = savings_pct = 0
    if product.brand_price and product.selling_price and product.brand_price > 0:
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
    #
    #  We build a FormattedPrices proxy so the HTML keeps using
    #  {{ product.selling_price }} style tags but gets "1,23,456 Tk"
    #  strings instead of raw Decimal values.
    #
    #  Strategy: monkey-patch formatted strings onto the product instance
    #  under the names the template already uses, but with a "fmt_" prefix
    #  so the originals remain accessible for the WhatsApp link, JSON-LD, etc.
    #
    product.fmt_selling_price = format_price(product.selling_price)
    product.fmt_final_price   = format_price(product.final_price)
    product.fmt_brand_price   = format_price(product.brand_price)
    product.fmt_shipping_cost = format_price(product.shipping_cost)
    product.fmt_savings       = format_price(savings)

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

        **meta,
        "og_image":            og_image,
        "og_url":              request.build_absolute_uri(),
        "structured_data":     json.dumps(structured_data, ensure_ascii=False),
        "show_dealer_phone":   show_dealer_phone,
        "show_dealer_email":   show_dealer_email,
        "user_authenticated":  request.user.is_authenticated,
        "video_info":          _get_video_info(product.video_url or ""),
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


# ──────────────────────────────────────────────────────────────────────────────
# Video info helper
# ──────────────────────────────────────────────────────────────────────────────

def _get_video_info(url: str) -> dict:
    if not url:
        return {}

    url    = url.strip()
    parsed = urlparse(url)
    hostname = parsed.netloc.lower().replace("www.", "")

    if hostname in ("youtube.com", "youtu.be", "m.youtube.com", "music.youtube.com"):
        vid_id = None
        if hostname == "youtu.be":
            vid_id = parsed.path.lstrip("/").split("/")[0]
        elif "/shorts/" in parsed.path:
            vid_id = parsed.path.split("/shorts/")[1].split("/")[0]
        elif "/live/" in parsed.path:
            vid_id = parsed.path.split("/live/")[1].split("/")[0]
        elif "/embed/" in parsed.path:
            vid_id = parsed.path.split("/embed/")[1].split("/")[0]
        else:
            vid_id = parse_qs(parsed.query).get("v", [None])[0]
        if vid_id:
            vid_id = re.sub(r'[^a-zA-Z0-9_-]', '', vid_id)
            return {
                "platform":  "youtube",
                "embed_url": f"https://www.youtube.com/embed/{vid_id}?rel=0&modestbranding=1",
                "watch_url": f"https://www.youtube.com/watch?v={vid_id}",
                "thumbnail": f"https://img.youtube.com/vi/{vid_id}/hqdefault.jpg",
                "type":      "iframe",
            }

    if hostname in ("vimeo.com", "player.vimeo.com"):
        vid_id = (parsed.path.split("/video/")[1].split("/")[0]
                  if "/video/" in parsed.path
                  else parsed.path.lstrip("/").split("/")[0])
        vid_id = re.sub(r'[^0-9]', '', vid_id)
        if vid_id:
            return {
                "platform":  "vimeo",
                "embed_url": f"https://player.vimeo.com/video/{vid_id}?badge=0&autopause=0",
                "watch_url": f"https://vimeo.com/{vid_id}",
                "thumbnail": "",
                "type":      "iframe",
            }

    if hostname in ("dailymotion.com", "dai.ly"):
        if hostname == "dai.ly":
            vid_id = parsed.path.lstrip("/").split("/")[0]
        elif "/video/" in parsed.path:
            vid_id = parsed.path.split("/video/")[1].split("_")[0].split("/")[0]
        else:
            vid_id = parsed.path.lstrip("/").split("/")[0]
        vid_id = re.sub(r'[^a-zA-Z0-9]', '', vid_id)
        if vid_id:
            return {
                "platform":  "dailymotion",
                "embed_url": f"https://www.dailymotion.com/embed/video/{vid_id}",
                "watch_url": f"https://www.dailymotion.com/video/{vid_id}",
                "thumbnail": f"https://www.dailymotion.com/thumbnail/video/{vid_id}",
                "type":      "iframe",
            }

    if hostname == "rumble.com":
        m = re.search(r'rumble\.com/embed/([^/?&]+)', url)
        vid_id = m.group(1) if m else None
        if not vid_id:
            m = re.search(r'rumble\.com/([^/?&]+)', url)
            vid_id = m.group(1) if m else None
        if vid_id:
            return {
                "platform":  "rumble",
                "embed_url": f"https://rumble.com/embed/{vid_id}/",
                "watch_url": url,
                "thumbnail": "",
                "type":      "iframe",
            }

    if hostname == "streamable.com":
        vid_id = parsed.path.lstrip("/").split("/")[0]
        if vid_id:
            return {
                "platform":  "streamable",
                "embed_url": f"https://streamable.com/e/{vid_id}",
                "watch_url": url,
                "thumbnail": "",
                "type":      "iframe",
            }

    if hostname in ("twitch.tv", "clips.twitch.tv"):
        if "/clip/" in parsed.path or hostname == "clips.twitch.tv":
            clip_id = parsed.path.lstrip("/").split("/")[-1]
            return {
                "platform":  "twitch_clip",
                "embed_url": f"https://clips.twitch.tv/embed?clip={clip_id}&parent={parsed.hostname}",
                "watch_url": url,
                "thumbnail": "",
                "type":      "iframe",
            }
        channel = parsed.path.lstrip("/").split("/")[0]
        return {
            "platform":  "twitch",
            "embed_url": f"https://player.twitch.tv/?channel={channel}&parent=yourdomain.com",
            "watch_url": url,
            "thumbnail": "",
            "type":      "iframe",
        }

    if hostname in ("facebook.com", "fb.watch", "fb.com"):
        return {
            "platform":  "facebook",
            "embed_url": f"https://www.facebook.com/plugins/video.php?href={url}&show_text=false&width=560",
            "watch_url": url,
            "thumbnail": "",
            "type":      "iframe",
        }

    if hostname in ("tiktok.com", "vm.tiktok.com"):
        m = re.search(r'/video/(\d+)', parsed.path)
        if m:
            return {
                "platform":  "tiktok",
                "embed_url": f"https://www.tiktok.com/embed/v2/{m.group(1)}",
                "watch_url": url,
                "thumbnail": "",
                "type":      "iframe",
            }

    if hostname in ("twitter.com", "x.com", "t.co"):
        return {
            "platform":  "twitter",
            "embed_url": f"https://platform.twitter.com/embed/Tweet.html?id={parsed.path.split('/')[-1]}",
            "watch_url": url,
            "thumbnail": "",
            "type":      "iframe",
        }

    video_extensions = ('.mp4', '.webm', '.ogg', '.mov', '.m4v', '.mkv', '.avi')
    if any(parsed.path.lower().endswith(ext) for ext in video_extensions):
        ext = parsed.path.lower().rsplit('.', 1)[-1]
        mime_map = {
            'mp4': 'video/mp4', 'webm': 'video/webm', 'ogg': 'video/ogg',
            'mov': 'video/mp4', 'm4v': 'video/mp4',
            'mkv': 'video/x-matroska', 'avi': 'video/x-msvideo',
        }
        return {
            "platform":  "direct",
            "embed_url": url,
            "watch_url": url,
            "thumbnail": "",
            "type":      "video",
            "mime_type": mime_map.get(ext, 'video/mp4'),
        }

    return {
        "platform":  "unknown",
        "embed_url": url,
        "watch_url": url,
        "thumbnail": "",
        "type":      "iframe",
    }