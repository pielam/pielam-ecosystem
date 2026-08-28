# megamind/services/connected_service_sync.py
"""
Shared Product -> ConnectedService field bridge + sync helper.

Single place responsible for keeping a Product's mirrored
ConnectedService row up to date. Called explicitly by both:
    - apps.ponno.views.product_upload.ProductUploadView  (create)
    - apps.ponno.views.product_edit.ProductEditView       (update)

This replaces the old Product post_save signal — there's now one
first-party writer of this mapping instead of duplicating the field
bridge in two views (which would drift: the create path and the edit
path would silently disagree about what "og_title" or "status" means
for a product).

── Lookup key: (user, service_url) ──
service_url is deterministic — built from product.slug via
build_product_service_url() — so it's already a stable per-product
key. `user` (the product's dealer) is kept in the lookup to match the
semantics of the original signal, which scoped ConnectedService rows
per-owner.

── service_type handling ──
product_upload.py collects service_type from the form at creation
time and passes it in explicitly — that value is used as-is.
product_edit.py does NOT collect service_type (editing a product
never changes which feed category it posts under), so it's omitted
there, and this function falls back to whatever the existing row
already has. If no row exists yet at all (self-healing case: a
product saved before this sync existed, or a ConnectedService row
deleted out-of-band), it falls back to DEFAULT_SERVICE_TYPE.

    effective_type = service_type or existing_row.service_type or DEFAULT_SERVICE_TYPE

This is why update_or_create's `defaults` always includes
`service_type` explicitly computed ahead of time, rather than baking
a static value into the call — passing a stale/default value straight
into `defaults` would silently overwrite a dealer's originally-chosen
type every time they edit an unrelated field like `stock`.

── Extended metadata fields (canonical_url / favicon / author /
   published_time / structured_data) ──
These fields exist on ConnectedService to hold what
megamind.utils.service_fetcher.fetch_service_data() extracts from a
*scraped* third-party page. A Product-backed row isn't scraped — it's
built directly from the Product row — but it still needs these fields
populated with the equivalent concepts, otherwise every product's
ConnectedService row would permanently look "incomplete" next to a
scraped one, and any consumer (API serializer, profile page detail
drawer, feed) would have to special-case "did this row come from a
scrape or a Product sync" instead of just reading the field. Unlike
service_type, there's no fallback-to-existing-row logic needed here —
these are fully derived from the current product state, so (like
og_title/og_thumbnail below) they're always recomputed and overwritten
on every sync, never preserved from a stale prior value.

    canonical_url   -> the product's own detail page (it IS canonical)
    favicon         -> site-wide default; products don't have per-item favicons
    author          -> left blank: no product field maps to "content author"
                       cleanly enough to guess at (dealer display name is a
                       distinct concept - "who owns/sells this" - not "who
                       wrote this page"). Revisit if/when there's an actual
                       byline concept for products.
    published_time  -> product.created_at, if the model has that field
    structured_data -> a schema.org Product JSON-LD block built from
                       whatever product fields exist. Deliberately
                       defensive (getattr with defaults) since this
                       sync helper shouldn't break product save() if a
                       given deployment's Product model doesn't have
                       every optional commerce field (price, currency,
                       stock, brand).
"""

import hashlib
from datetime import timedelta

from django.urls import reverse
from django.utils import timezone

from apps.ponno.models.product import Product
from megamind.models.connected_service import ConnectedService
from megamind.utils.video_info import get_video_info

# Hardcoded for now — swap for settings.SITE_URL later if that setting
# gets added.
SITE_BASE_URL = "https://pielam.com"
DEFAULT_SERVICE_TYPE = "product"

# This row isn't a crawl target — it's a mirror of a Product we already
# fully control, kept in sync by this function being called directly
# from the product create/edit views, not by the scheduled crawl queue.
# ConnectedService.objects.due_for_crawl() treats next_crawl_at=None as
# "eligible right now", so leaving it null would let the queue pick this
# row up and overwrite this carefully-built metadata with a generic
# scrape of our own site. Pushing next_crawl_at far into the future
# keeps it out of the queue without needing a dedicated "not crawlable"
# flag on the model.
_NEVER_RECRAWL_HORIZON = timedelta(days=365 * 50)


def _hash_extracted_text(text: str) -> str:
    """Same algorithm as services.scraper._hash_content, so
    content_version/content_hash stay comparable whether a row was
    populated by a real scrape or by this product sync."""
    if not text:
        return ""
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()

# Site-wide favicon — products don't have their own per-item favicon,
# so every Product-backed ConnectedService row shares this one rather
# than being left blank.
DEFAULT_FAVICON_URL = f"{SITE_BASE_URL}/favicon.ico"


# ────────────────────────────────────────────────────────────────────
# FIELD BUILDERS
# ────────────────────────────────────────────────────────────────────

def build_product_service_url(product: Product) -> str:
    """
    Absolute URL for the product's own detail page, built from the
    real 'ponno:product_detail' route (/product/<slug>/).

    Requires product.slug to already exist — true for both callers,
    since Product.save() generates the slug before either view's
    post-save sync code runs.
    """
    path = reverse("ponno:product_detail", kwargs={"slug": product.slug})
    return f"{SITE_BASE_URL}{path}"[:500]


def _build_product_extracted_images(product: Product) -> list:
    if not product.image:
        return []
    return [{"url": product.image.url, "alt": product.product_name}]


def _is_recognized_video_url(video_url: str) -> bool:
    """
    True only when get_video_info() resolves the URL to a known video
    platform or direct video file. product.video_url is raw,
    unscraped user input, so an 'unknown' result means "don't know
    what this is", not "safe to show as a video".
    """
    if not video_url:
        return False
    info = get_video_info(video_url)
    return bool(info) and info.get("platform") != "unknown"


def _build_product_extracted_videos(product: Product) -> list:
    """
    Only wrap product.video_url as a video entry when it resolves to
    a recognized video platform or direct video file. Anything else
    is picked up by _build_product_extracted_links() instead.
    """
    if not _is_recognized_video_url(product.video_url):
        return []
    return [{"url": product.video_url, "type": "embed"}]


def _build_product_extracted_links(product: Product) -> list:
    """
    If video_url is set but not a recognized video, preserve it as a
    plain link instead of dropping it. If it IS a recognized video,
    it's already in extracted_videos, so it's left out here to avoid
    showing the same URL twice on one post.
    """
    if not product.video_url:
        return []
    if _is_recognized_video_url(product.video_url):
        return []
    return [{"href": product.video_url, "text": "External link"}]


def _build_product_extracted_text(product: Product) -> str:
    return product.description or product.short_description or ""


def _build_product_published_time(product: Product) -> str:
    """
    Mirrors what service_fetcher.py stores for scraped pages: the raw
    ISO-8601 string, not a parsed datetime (see model field comment on
    ConnectedService.published_time). Falls back to '' rather than
    raising if this Product model doesn't have created_at.
    """
    created_at = getattr(product, "created_at", None)
    return created_at.isoformat() if created_at else ""


def _build_product_structured_data(product: Product) -> list:
    """
    Minimal schema.org Product JSON-LD block, built defensively from
    whatever fields this Product model actually has. Every field here
    uses getattr(..., default) rather than direct attribute access —
    price/currency/stock/brand aren't fields this file has ever
    referenced before now, so they may not exist on every deployment's
    Product model, and this sync helper must not break product
    save() over an optional commerce field being absent.

    Kept in the same shape megamind.utils.service_fetcher.
    _extract_product_from_jsonld() already knows how to read, so if
    anything downstream ever runs that same JSON-LD-based product
    extraction over a Product-backed ConnectedService row (rather than
    a scraped one), it finds the same shape either way.
    """
    title = product.product_title or product.product_name
    offers = {"@type": "Offer"}

    price = getattr(product, "price", None)
    if price is not None:
        offers["price"] = str(price)
    currency = getattr(product, "currency", None)
    if currency:
        offers["priceCurrency"] = currency
    if len(offers) > 1:
        offers["availability"] = (
            "https://schema.org/InStock" if product.is_active
            else "https://schema.org/OutOfStock"
        )

    block = {
        "@context": "https://schema.org",
        "@type": "Product",
        "name": title,
    }
    if product.image:
        block["image"] = product.image.url
    description = product.meta_description or product.short_description
    if description:
        block["description"] = description
    brand_name = getattr(product, "brand_name", None) or getattr(product, "brand", None)
    if brand_name:
        block["brand"] = {"@type": "Brand", "name": str(brand_name)}
    if len(offers) > 1:
        block["offers"] = offers

    return [block]


# ────────────────────────────────────────────────────────────────────
# SYNC
# ────────────────────────────────────────────────────────────────────

def sync_product_connected_service(
    dealer,
    product: Product,
    service_type: str = None,
) -> ConnectedService:
    """
    Create or update the ConnectedService row mirroring this Product.

    Parameters
    ----------
    dealer       : the product's owner (product.dealer) — used as the
                   ConnectedService.user and part of the lookup key.
                   Passed explicitly rather than read off `product`
                   so callers can't accidentally pass a mismatched
                   product/dealer pair.
    product      : the just-saved Product instance (must have a slug).
    service_type : pass the form value when creating (product_upload.py);
                   omit (None) when updating (product_edit.py) so the
                   existing row's service_type is preserved instead of
                   being reset to the default on every edit.
    """
    service_url = build_product_service_url(product)

    if service_type:
        effective_type = service_type
    else:
        existing_type = (
            ConnectedService.objects
            .filter(user=dealer, service_url=service_url)
            .values_list("service_type", flat=True)
            .first()
        )
        effective_type = existing_type or DEFAULT_SERVICE_TYPE

    title = product.product_title or product.product_name
    extracted_text = _build_product_extracted_text(product)
    now = timezone.now()

    service, _created = ConnectedService.objects.update_or_create(
        user=dealer,
        service_url=service_url,
        defaults={
            "service_type": effective_type,
            "service_name": title,
            "status": "public" if product.is_active else "private",
            "is_connected": True,
            "is_active": True,

            "og_title": product.meta_title or title,
            "og_description": product.meta_description or product.short_description or "",
            "og_thumbnail": product.image.url if product.image else "",
            "og_site_name": "Pielam",
            "og_type": "product",

            "canonical_url": service_url,
            "favicon": DEFAULT_FAVICON_URL,
            "author": "",
            "published_time": _build_product_published_time(product),
            "structured_data": _build_product_structured_data(product),

            "extracted_images": _build_product_extracted_images(product),
            "extracted_videos": _build_product_extracted_videos(product),
            "extracted_links": _build_product_extracted_links(product),
            "extracted_text": extracted_text,
            "content_hash": _hash_extracted_text(extracted_text),

            "last_fetched_data": None,
            "last_fetch_time": now,
            "fetch_status": "success",
            "fetch_error": None,
            "fetch_error_category": "none",

            # Not a real crawl target — see _NEVER_RECRAWL_HORIZON above.
            "crawl_source": "backfill",
            "respect_robots_txt": False,
            "next_crawl_at": now + _NEVER_RECRAWL_HORIZON,

            # Defensive reset in case a stale row (e.g. one that used to
            # be a real scraped ConnectedService before the product was
            # created at the same URL) had these set from a prior life.
            "circuit_breaker_open": False,
            "circuit_breaker_until": None,
            "consecutive_failures": 0,
            "retry_count": 0,
            "next_retry_at": None,
            "lock_id": None,
            "locked_at": None,
            "locked_by": None,
        },
    )
    return service