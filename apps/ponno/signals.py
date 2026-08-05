# apps/ponno/signals.py

"""
Signals that mirror ponno content models (Brand, Category, SubCategory)
into megamind's ConnectedService model, so each one shows up as a feed
item in HomeEngineView / PersonalEngineView.

Every receiver follows the same shape:
    1. Guard on the owning user field being set (created_by).
    2. Build a stable, absolute service_url via reverse().
    3. update_or_create() on (user, service_type, service_url) so
       repeated saves update the same ConnectedService row instead of
       creating duplicates.

NOTE: service_url is part of the lookup key. If a model's slug is
regenerated after creation (none of these currently do that — all
`generate_slug()` calls are guarded with `if not self.slug`), a slug
change would produce a second ConnectedService row rather than
updating the original. Flagging this here since it applies uniformly
across all receivers below.

── Product is no longer mirrored via signal ──
Product → ConnectedService is now created explicitly in
apps.ponno.views.product_upload.ProductUploadView, in the same DB
transaction as the Product create, so a failure on either side rolls
back both and there's a single, explicit place that owns the write.
The post_save receiver that used to mirror Product here (and its
video_url routing / extracted_images / extracted_links helpers) has
been removed to avoid two code paths writing (and disagreeing about)
the same ConnectedService row. Product cache invalidation stays here
since it's unrelated to the ConnectedService mirror.
"""

from django.db.models.signals import post_save, post_delete
from django.dispatch import receiver
from django.urls import reverse
from django.utils import timezone

from apps.ponno.models.product import Product
from apps.ponno.models.brand import Brand
from apps.ponno.models.category import Category
from apps.ponno.models.sub_category import SubCategory
from apps.ponno.views.product_detail import invalidate_product_cache

from megamind.models.connected_service import ConnectedService

# Hardcoded for now — swap for settings.SITE_URL later if that setting
# gets added. Per user: no SITE_URL setting yet, not needed right now.
SITE_BASE_URL = "https://pielam.com"


# ════════════════════════════════════════════════════════════════════
# PRODUCT — cache invalidation only (ConnectedService owned by the view)
# ════════════════════════════════════════════════════════════════════

@receiver(post_save, sender=Product)
@receiver(post_delete, sender=Product)
def clear_product_cache(sender, instance, **kwargs):
    invalidate_product_cache(instance)


# ════════════════════════════════════════════════════════════════════
# BRAND → ConnectedService
# ════════════════════════════════════════════════════════════════════

def _build_brand_service_url(brand: Brand) -> str:
    """
    Absolute URL for the brand's product-listing page, built from
    'ponno:brand_products' (/brand/<brand_slug>/) — the only route
    that exists for a brand's slug.
    """
    path = reverse("ponno:brand_products", kwargs={"brand_slug": brand.brand_slug})
    return f"{SITE_BASE_URL}{path}"[:500]


def _build_brand_extracted_images(brand: Brand) -> list:
    images = []
    if brand.brand_logo:
        images.append({"url": brand.brand_logo.url, "alt": f"{brand.brand_name} logo"})
    if brand.brand_banner:
        images.append({"url": brand.brand_banner.url, "alt": f"{brand.brand_name} banner"})
    if brand.brand_icon:
        images.append({"url": brand.brand_icon.url, "alt": f"{brand.brand_name} icon"})
    return images


def _build_brand_extracted_links(brand: Brand) -> list:
    links = []
    social_fields = {
        "Facebook":  brand.social_facebook,
        "Instagram": brand.social_instagram,
        "Twitter/X": brand.social_twitter,
        "LinkedIn":  brand.social_linkedin,
        "YouTube":   brand.social_youtube,
    }
    for label, url in social_fields.items():
        if url:
            links.append({"href": url, "text": label})
    return links


def create_or_update_connected_service_for_brand(brand: Brand) -> ConnectedService:
    if not brand.brand_slug:
        # brand_slug is generated in Brand.save() before this fires, but
        # guard anyway since brand_slug is nullable at the DB level.
        return None

    service_url = _build_brand_service_url(brand)

    service, _created = ConnectedService.objects.update_or_create(
        user=brand.created_by,
        service_type="brand",
        service_url=service_url,
        defaults={
            "service_name":      brand.brand_name,
            "status":            "public" if brand.is_active else "private",
            "is_connected":      True,

            "og_title":          brand.meta_title or brand.brand_name,
            "og_description":    brand.meta_description or brand.brand_description or "",
            "og_thumbnail":      brand.brand_logo.url if brand.brand_logo else "",
            "og_site_name":      "Pielam",
            "og_type":           "brand",

            "extracted_images":  _build_brand_extracted_images(brand),
            "extracted_videos":  [],
            "extracted_links":   _build_brand_extracted_links(brand),
            "extracted_text":    brand.brand_description or brand.brand_story or "",

            "last_fetched_data": None,
            "last_fetch_time":   timezone.now(),
            "fetch_status":      "success",
            "fetch_error":       None,
        },
    )
    return service


@receiver(post_save, sender=Brand)
def sync_brand_to_connected_service(sender, instance: Brand, created, **kwargs):
    """
    Whenever a Brand is created OR updated, mirror it into ConnectedService.
    NOTE: created_by is nullable — brands created without an owning user
    (e.g. via Django admin without setting created_by) won't sync.
    """
    if kwargs.get("raw", False):
        return

    if not instance.created_by_id:
        return
    create_or_update_connected_service_for_brand(instance)


# ════════════════════════════════════════════════════════════════════
# CATEGORY → ConnectedService
# ════════════════════════════════════════════════════════════════════

def _build_category_service_url(category: Category) -> str:
    """
    Absolute URL for the category's product-listing page, built from
    'ponno:category_products' (/category/<category_slug>/).
    """
    path = reverse("ponno:category_products", kwargs={"category_slug": category.category_slug})
    return f"{SITE_BASE_URL}{path}"[:500]


def _build_category_extracted_images(category: Category) -> list:
    images = []
    if category.category_image:
        images.append({"url": category.category_image.url, "alt": f"{category.category_name} image"})
    if category.category_icon:
        images.append({"url": category.category_icon.url, "alt": f"{category.category_name} icon"})
    if category.category_thumbnail:
        images.append({"url": category.category_thumbnail.url, "alt": f"{category.category_name} thumbnail"})
    return images


def create_or_update_connected_service_for_category(category: Category) -> ConnectedService:
    if not category.category_slug:
        return None

    service_url = _build_category_service_url(category)

    service, _created = ConnectedService.objects.update_or_create(
        user=category.created_by,
        service_type="category",
        service_url=service_url,
        defaults={
            "service_name":      category.category_name,
            "status":            "public" if category.is_active else "private",
            "is_connected":      True,

            "og_title":          category.meta_title or category.category_name,
            "og_description":    category.meta_description or category.category_short_description or "",
            "og_thumbnail":      category.category_image.url if category.category_image else "",
            "og_site_name":      "Pielam",
            "og_type":           "category",

            "extracted_images":  _build_category_extracted_images(category),
            "extracted_videos":  [],
            "extracted_links":   [],
            "extracted_text":    category.category_description or "",

            "last_fetched_data": None,
            "last_fetch_time":   timezone.now(),
            "fetch_status":      "success",
            "fetch_error":       None,
        },
    )
    return service


@receiver(post_save, sender=Category)
def sync_category_to_connected_service(sender, instance: Category, created, **kwargs):
    """
    Whenever a Category is created OR updated, mirror it into ConnectedService.
    NOTE: created_by is nullable — same caveat as Brand above.
    """
    if kwargs.get("raw", False):
        return

    if not instance.created_by_id:
        return
    create_or_update_connected_service_for_category(instance)


# ════════════════════════════════════════════════════════════════════
# SUBCATEGORY → ConnectedService
# ════════════════════════════════════════════════════════════════════

def _build_subcategory_service_url(sub_category: SubCategory) -> str:
    """
    Absolute URL for the subcategory's product-listing page, built from
    'ponno:subcategory_products' (/subcategory/<slug>/).

    NOTE: the URL kwarg is named `category_slug` in web_urls.py even
    though the view is SubCategoryProducts and it actually consumes
    the subcategory's own slug. Reversing against the real kwarg name.
    """
    path = reverse(
        "ponno:subcategory_products",
        kwargs={"category_slug": sub_category.sub_category_slug},
    )
    return f"{SITE_BASE_URL}{path}"[:500]


def _build_subcategory_extracted_images(sub_category: SubCategory) -> list:
    images = []
    if sub_category.sub_category_image:
        images.append({"url": sub_category.sub_category_image.url, "alt": f"{sub_category.sub_category_name} image"})
    if sub_category.sub_category_icon:
        images.append({"url": sub_category.sub_category_icon.url, "alt": f"{sub_category.sub_category_name} icon"})
    if sub_category.sub_category_thumbnail:
        images.append({"url": sub_category.sub_category_thumbnail.url, "alt": f"{sub_category.sub_category_name} thumbnail"})
    return images


def create_or_update_connected_service_for_subcategory(sub_category: SubCategory) -> ConnectedService:
    if not sub_category.sub_category_slug:
        return None

    service_url = _build_subcategory_service_url(sub_category)

    service, _created = ConnectedService.objects.update_or_create(
        user=sub_category.created_by,
        service_type="subcategory",
        service_url=service_url,
        defaults={
            "service_name":      sub_category.sub_category_name,
            "status":            "public" if sub_category.is_active else "private",
            "is_connected":      True,

            "og_title":          sub_category.meta_title or sub_category.sub_category_name,
            "og_description":    sub_category.meta_description or sub_category.sub_category_short_description or "",
            "og_thumbnail":      sub_category.sub_category_image.url if sub_category.sub_category_image else "",
            "og_site_name":      "Pielam",
            "og_type":           "subcategory",

            "extracted_images":  _build_subcategory_extracted_images(sub_category),
            "extracted_videos":  [],
            "extracted_links":   [],
            "extracted_text":    sub_category.sub_category_description or "",

            "last_fetched_data": None,
            "last_fetch_time":   timezone.now(),
            "fetch_status":      "success",
            "fetch_error":       None,
        },
    )
    return service


@receiver(post_save, sender=SubCategory)
def sync_subcategory_to_connected_service(sender, instance: SubCategory, created, **kwargs):
    """
    Whenever a SubCategory is created OR updated, mirror it into ConnectedService.
    NOTE: created_by is nullable — same caveat as Brand/Category above.
    """
    if kwargs.get("raw", False):
        return

    if not instance.created_by_id:
        return
    create_or_update_connected_service_for_subcategory(instance)