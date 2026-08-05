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
"""

from django.urls import reverse
from django.utils import timezone

from apps.ponno.models.product import Product
from megamind.models.connected_service import ConnectedService
from megamind.utils.video_info import get_video_info

# Hardcoded for now — swap for settings.SITE_URL later if that setting
# gets added.
SITE_BASE_URL = "https://pielam.com"
DEFAULT_SERVICE_TYPE = "product"


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

    service, _created = ConnectedService.objects.update_or_create(
        user=dealer,
        service_url=service_url,
        defaults={
            "service_type": effective_type,
            "service_name": title,
            "status": "public" if product.is_active else "private",
            "is_connected": True,

            "og_title": product.meta_title or title,
            "og_description": product.meta_description or product.short_description or "",
            "og_thumbnail": product.image.url if product.image else "",
            "og_site_name": "Pielam",
            "og_type": "product",

            "extracted_images": _build_product_extracted_images(product),
            "extracted_videos": _build_product_extracted_videos(product),
            "extracted_links": _build_product_extracted_links(product),
            "extracted_text": _build_product_extracted_text(product),

            "last_fetched_data": None,
            "last_fetch_time": timezone.now(),
            "fetch_status": "success",
            "fetch_error": None,
        },
    )
    return service