"""
apps/ponno/services/product_sync.py

Bridges a scraped ConnectedService (service_type='product') into a real
Product row. This is the piece that turns "we scraped a page" into
"we have a sellable catalog entry" — ConnectedService alone never creates
or touches Product records on its own.

Design notes:
- `dealer` and `category` cannot be reliably scraped — they're business
  decisions, not page data — so both are required arguments, not inferred.
- `brand` is resolved by name (get_or_create), since the source site's
  brand string won't reliably match your internal Brand catalog by PK.
- This does NOT overwrite selling_price/stock on an existing Product by
  default — see `overwrite_pricing`. A dealer/admin should usually decide
  pricing themselves; the scrape is a starting point, not a live feed,
  unless you explicitly want that.
"""

from decimal import Decimal, InvalidOperation
import logging

from django.core.exceptions import ValidationError, MultipleObjectsReturned
from django.db import IntegrityError

from apps.customer.models.connected_service import ConnectedService
from apps.ponno.models.brand import Brand
from apps.ponno.models.product import Product

logger = logging.getLogger(__name__)


class ProductSyncError(Exception):
    pass


def sync_connected_service_to_product(
    service: ConnectedService,
    *,
    dealer,
    category,
    sub_category=None,
    overwrite_pricing: bool = False,
    overwrite_description: bool = True,
    existing_product: "Product | None" = None,
) -> Product:
    """
    Create or update a Product from a successfully-scraped ConnectedService.

    Args:
        service: a ConnectedService with fetch_status='success' and
            service_type='product'.
        dealer: the User (role='dealer') who will own this listing.
        category / sub_category: your internal taxonomy — not derived
            from the scrape, since the source site's categories won't
            match yours.
        overwrite_pricing: if True and `existing_product` is given,
            scraped price/stock overwrite the existing values. Default
            False so re-syncing an existing listing doesn't clobber a
            dealer's manually-set price.
        overwrite_description: if True, scraped title/description
            overwrite existing values on `existing_product`.
        existing_product: pass an existing Product to update it in place
            instead of creating a new one (e.g. if you're re-syncing after
            a retry_connected_service() call).

    Returns:
        The created or updated Product instance (saved).

    Raises:
        ProductSyncError: if the service hasn't been successfully fetched,
            isn't a product-type service, or is missing required commerce
            data (no title and no price — nothing usable to sync).
    """
    if service.fetch_status != ConnectedService.FetchStatus.SUCCESS:
        raise ProductSyncError(
            f"Cannot sync service {service.pk}: fetch_status is "
            f"'{service.fetch_status}', not 'success'."
        )
    if service.service_type != ConnectedService.ServiceType.PRODUCT:
        raise ProductSyncError(
            f"Cannot sync service {service.pk}: service_type is "
            f"'{service.service_type}', not 'product'."
        )
    if service.content_sensitivity == ConnectedService.ContentSensitivity.PII_DETECTED:
        # A PII scan flagged this scrape before it ever reached this
        # function (see the crawl worker). Refuse to publish it into a
        # public-facing Product listing rather than trusting the caller
        # to have checked — description/extracted_text below is exactly
        # the kind of field a PII scan would be flagging.
        raise ProductSyncError(
            f"Cannot sync service {service.pk}: content_sensitivity is "
            f"'pii_detected' — refusing to publish scraped content that "
            f"may contain personal data into a Product listing."
        )

    if sub_category is not None and sub_category.category_id != category.pk:
        raise ProductSyncError(
            f"Cannot sync service {service.pk}: sub_category "
            f"'{sub_category.sub_category_name}' belongs to category "
            f"'{sub_category.category.category_name}', not the given "
            f"category '{category.category_name}'."
        )

    product_name = (service.page_title or service.og_title or service.service_name or "").strip()
    if not product_name:
        raise ProductSyncError(
            f"Cannot sync service {service.pk}: no usable title "
            f"(page_title/og_title/service_name all empty)."
        )

    brand = _resolve_brand(service.brand_name)
    selling_price = _coerce_price(service.price_amount)

    product = existing_product or Product(dealer=dealer)

    # Identity / relationships — always set on create, only overwritten
    # on update if the caller opted in.
    if existing_product is None or overwrite_description:
        product.product_name = product_name
        product.product_title = product_name
        product.description = service.extracted_text[:5000] if service.extracted_text else (
            service.og_description or service.meta_description
        )
        product.short_description = (service.og_description or service.meta_description or "")[:500] or None

    product.dealer = dealer
    product.category = category
    product.sub_category = sub_category
    if brand:
        product.brand = brand

    # Media
    if service.og_thumbnail and (existing_product is None or overwrite_description):
        # Product.image is an ImageField (local file), not a URLField, so we
        # can't assign a remote URL directly here — store it in metadata and
        # let a separate download-and-attach step populate `image` if needed.
        product.metadata = {**(product.metadata or {}), "scraped_image_url": service.og_thumbnail}
    if service.og_video:
        product.video_url = service.og_video

    # Commerce fields — gated by overwrite_pricing when updating
    if existing_product is None or overwrite_pricing:
        if selling_price is not None:
            product.selling_price = selling_price
        if service.price_currency:
            product.currency = service.price_currency
        if service.product_sku and not product.sku:
            product.sku = service.product_sku[:100]
        if service.availability:
            product.stock_status = _map_availability(service.availability)
            product.track_inventory = False  # we don't have real stock counts, just a status
        if service.product_condition:
            product.product_condition = _map_condition(service.product_condition)

    # SEO
    if service.meta_title or service.og_title:
        product.meta_title = (service.meta_title or service.og_title)[:60]
    if service.meta_description or service.og_description:
        product.meta_description = (service.meta_description or service.og_description)[:160]

    # Provenance — always useful to keep, regardless of overwrite flags
    product.metadata = {
        **(product.metadata or {}),
        "source_connected_service_id": service.pk,
        "source_url": service.service_url,
        "source_domain": service.domain,
        "source_content_version": service.content_version,
        "source_last_fetch_time": service.last_fetch_time.isoformat() if service.last_fetch_time else None,
        "source_rating_value": str(service.rating_value) if service.rating_value else None,
        "source_rating_count": service.rating_count,
        # HINT ONLY — for whoever assigns product.category / sub_category.
        # Never used to create or auto-match an actual Category, since the
        # source site's taxonomy won't line up with yours.
        "source_category_hint": {
            "breadcrumb": service.category_breadcrumb or [],
            "article_section": service.article_section,
        },
    }

    try:
        product.full_clean()
    except ValidationError as exc:
        raise ProductSyncError(f"Product validation failed for service {service.pk}: {exc}") from exc

    product.save()
    return product


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _resolve_brand(brand_name: str | None) -> Brand | None:
    """
    Look up or create a Brand by name. Uses filter().first() rather than
    get_or_create() so a pre-existing case-variant duplicate (e.g. both
    "Nike" and "NIKE" already in the table) degrades to "pick one" instead
    of raising MultipleObjectsReturned.

    Never raises — a bad/invalid scraped brand name should not block
    syncing the rest of the product. Returns None on any failure, and the
    caller just leaves Product.brand unset in that case.
    """
    if not brand_name or not brand_name.strip():
        return None

    # Brand.brand_name is max_length=150 — truncating to match avoids a
    # full_clean() ValidationError on creation below.
    name = brand_name.strip()[:150]
    if not name:
        return None

    try:
        existing = Brand.objects.filter(brand_name__iexact=name).first()
        if existing:
            return existing
        # Brand.save() runs full_clean() on create (no update_fields passed),
        # which can raise ValidationError for reasons unrelated to the name
        # itself (e.g. a stray invalid URL if this ever passes more fields).
        # Only brand_name is set here, so that's not expected, but we still
        # guard against it rather than let one bad brand kill the sync.
        return Brand.objects.create(brand_name=name)
    except (ValidationError, IntegrityError, MultipleObjectsReturned) as exc:
        logger.warning("Could not resolve/create brand %r: %s", name, exc)
        return None


def _coerce_price(value) -> Decimal | None:
    if value is None:
        return None
    try:
        price = Decimal(value)
    except (InvalidOperation, TypeError, ValueError):
        return None
    return price if price >= 0 else None


# schema.org availability values -> your StockStatus choices
_AVAILABILITY_MAP = {
    "instock": Product.StockStatus.IN_STOCK,
    "http://schema.org/instock": Product.StockStatus.IN_STOCK,
    "outofstock": Product.StockStatus.OUT_OF_STOCK,
    "http://schema.org/outofstock": Product.StockStatus.OUT_OF_STOCK,
    "limitedavailability": Product.StockStatus.LOW_STOCK,
    "preorder": Product.StockStatus.PRE_ORDER,
    "http://schema.org/preorder": Product.StockStatus.PRE_ORDER,
}


def _map_availability(raw: str) -> str:
    return _AVAILABILITY_MAP.get(raw.strip().lower(), Product.StockStatus.IN_STOCK)


# schema.org condition values -> your ProductCondition choices
_CONDITION_MAP = {
    "newcondition": Product.ProductCondition.NEW,
    "http://schema.org/newcondition": Product.ProductCondition.NEW,
    "usedcondition": Product.ProductCondition.USED,
    "http://schema.org/usedcondition": Product.ProductCondition.USED,
    "refurbishedcondition": Product.ProductCondition.REFURBISHED,
    "http://schema.org/refurbishedcondition": Product.ProductCondition.REFURBISHED,
}


def _map_condition(raw: str) -> str:
    return _CONDITION_MAP.get(raw.strip().lower(), Product.ProductCondition.NEW)