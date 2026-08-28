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

── Brand.product_count sync ──
Brand.product_count is a cached column (Brand.update_product_count())
that nothing was ever calling, so every brand showed "0 products"
regardless of how many products it actually had. The three receivers
in the PRODUCT → BRAND.PRODUCT_COUNT section below keep it correct:
capture the prior brand_id/is_active before save, then recompute
whichever brand(s) are affected after save/delete — but only when
brand or is_active actually changed, so routine product saves (a
view_count bump, a price edit) don't trigger an extra COUNT query.

NOTE: Brand.update_product_count() calls brand.save(update_fields=
['product_count']), which fires sync_brand_to_connected_service()
below (Brand.save() unconditionally runs full_clean() too). That
means a product count change now also rewrites the brand's
ConnectedService row and re-validates the whole Brand row, not just
the one field. Functionally harmless (update_or_create is idempotent)
but worth knowing if brand-count-heavy periods (e.g. a bulk import)
show up as ConnectedService write load.

── Category.product_count sync (atomic, no COUNT query) ──
Same "nothing was ever calling it" problem as Brand.product_count,
but fixed differently here on purpose. Category.product_count is kept
correct by the PRODUCT → CATEGORY.PRODUCT_COUNT section below using a
direct `F('product_count') + delta` UPDATE instead of
Category.update_product_count() (which runs a full COUNT(*) query and
goes through Category.save(), triggering full_clean() and re-writing
the category's ConnectedService row on every single product save).

The atomic approach:
  - Issues a single `UPDATE categories SET product_count =
    product_count + delta WHERE pk = ...` — no COUNT query, no
    full_clean(), no ConnectedService rewrite.
  - Is race-safe under concurrent product saves against the same
    category (the delta is applied in SQL, not computed in Python
    and written back).
  - Bypasses Category.save(), so it does NOT fire
    clear_category_cache() on its own — _bump_category_count() below
    calls invalidate_category_cache() explicitly so listing pages
    don't serve a stale count for a full CACHE_TTL + STALE_TTL window.

Only a create, a hard delete, an is_active flip, or a category
reassignment changes the *counted* state of a product, so only those
transitions touch the DB — a routine save (view_count bump, price
edit, description tweak) is a no-op here.

backfill_category_product_counts() (apps/ponno/views/category_list.py)
remains the correct repair tool for drift — e.g. rows that predate
this signal, or any path that writes Product outside the ORM.

── Brand.category_count / subcategory_count / rating stats sync ──
Same problem as Brand.product_count, same COUNT-based fix, for the
other three cached Brand columns: category_count, subcategory_count,
average_rating, and review_count. Category and SubCategory both carry
a direct `brand` FK (not a chain through Category), so their sync
sections below mirror the PRODUCT → BRAND.PRODUCT_COUNT pattern
exactly: capture prior brand_id/is_active in pre_save, recompute the
affected brand(s) in post_save only when brand or is_active changed,
and handle hard deletes in post_delete.

ProductRating has no brand FK at all — it only has `product`, and
brand lives two hops away (ProductRating → Product → Brand). So the
CATEGORY/SUBCATEGORY pattern is adapted for that indirection: capture
the prior product_id (in case a rating is ever re-pointed at a
different product), then after save/delete, refresh
Brand.refresh_rating_stats() for the current product's brand and, if
different, the previous product's brand. Because
Brand.refresh_rating_stats() aggregates every ProductRating across
every active product for that brand in one query, this is heavier
than a plain COUNT — fine for normal rating traffic, but if a brand
ever gets rated in a tight loop (a review-import script, load test,
etc.) consider debouncing this with a task queue instead of running
it synchronously per save.

NOTE: all four of Brand's cached-stat save(update_fields=[...]) calls
funnel through the same Brand.save() override, so the same
"also touches ConnectedService" caveat above applies to all of them,
not just product_count.

── Category cache invalidation ──
clear_category_cache() (post_save/post_delete on Category) keeps
CategoryListView's L1/Redis page cache and stats cache fresh whenever
a Category row is saved or deleted through Category.save() itself
(e.g. is_featured toggles, display_order edits, soft_delete/restore).
It does NOT cover the atomic product_count bump above, since that
bypasses save() entirely — that path calls invalidate_category_cache()
directly instead. Mirrors clear_product_cache for Product.
"""

from django.db.models import F
from django.db.models.signals import post_save, post_delete, pre_save
from django.dispatch import receiver
from django.urls import reverse
from django.utils import timezone

from apps.ponno.models.product import Product
from apps.ponno.models.brand import Brand
from apps.ponno.models.category import Category
from apps.ponno.models.sub_category import SubCategory
from apps.ponno.models.rating import ProductRating
from apps.ponno.views.product_detail import invalidate_product_cache
from apps.ponno.views.category_list import invalidate_category_cache

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
# PRODUCT → BRAND.PRODUCT_COUNT
# ════════════════════════════════════════════════════════════════════

@receiver(pre_save, sender=Product, dispatch_uid='product_capture_prior_state')
def capture_prior_product_state(sender, instance, **kwargs):
    """Stash the DB's current brand_id/is_active before this save overwrites it."""
    if not instance.pk:
        instance._prior_brand_id = None
        instance._prior_is_active = None
        return

    try:
        prior = Product.objects.only('brand_id', 'is_active').get(pk=instance.pk)
        instance._prior_brand_id = prior.brand_id
        instance._prior_is_active = prior.is_active
    except Product.DoesNotExist:
        instance._prior_brand_id = None
        instance._prior_is_active = None


@receiver(post_save, sender=Product, dispatch_uid='product_sync_brand_count_on_save')
def sync_brand_product_count_on_save(sender, instance, created, **kwargs):
    """
    Recompute product_count for whichever brand(s) are affected —
    but only when brand or is_active actually changed, so routine
    saves (view_count bumps, price edits, etc.) don't trigger a COUNT.
    """
    prior_brand_id = getattr(instance, '_prior_brand_id', None)
    prior_is_active = getattr(instance, '_prior_is_active', None)

    brand_changed = (not created) and prior_brand_id != instance.brand_id
    active_changed = (not created) and prior_is_active != instance.is_active

    if not (created or brand_changed or active_changed):
        return

    if instance.brand_id:
        try:
            instance.brand.update_product_count()
        except Brand.DoesNotExist:
            pass

    if brand_changed and prior_brand_id:
        try:
            Brand.objects.get(pk=prior_brand_id).update_product_count()
        except Brand.DoesNotExist:
            pass


@receiver(post_delete, sender=Product, dispatch_uid='product_sync_brand_count_on_delete')
def sync_brand_product_count_on_delete(sender, instance, **kwargs):
    """Hard deletes (Product.delete(hard_delete=True)) need a recount too."""
    if instance.brand_id:
        try:
            Brand.objects.get(pk=instance.brand_id).update_product_count()
        except Brand.DoesNotExist:
            pass


# ════════════════════════════════════════════════════════════════════
# PRODUCT → CATEGORY.PRODUCT_COUNT  (atomic increment/decrement)
# ════════════════════════════════════════════════════════════════════
#
# Unlike the BRAND sync above, this does NOT call
# Category.update_product_count() (which runs a full COUNT(*) query
# and goes through Category.save() → full_clean() → ConnectedService
# rewrite). Instead it issues a single atomic
# `UPDATE categories SET product_count = product_count + delta`,
# which is cheaper and race-safe, then explicitly invalidates the
# category's page cache since bypassing save() also bypasses
# clear_category_cache().
#
# NOTE: assumes Product has a FK named `category` (i.e. `category_id`
# on the DB row, `instance.category` for the related object). If
# products in this codebase are only ever linked via SubCategory and
# never directly via Category, this section should key off
# `instance.category.parent_id`-style traversal instead — confirm the
# real FK name on Product before relying on this in production.

@receiver(pre_save, sender=Product, dispatch_uid='product_capture_prior_category_state')
def capture_prior_product_category_state(sender, instance, **kwargs):
    """Stash the DB's current category_id/is_active before this save overwrites it."""
    if not instance.pk:
        instance._prior_category_id = None
        instance._prior_category_is_active = None
        return

    try:
        prior = Product.objects.only('category_id', 'is_active').get(pk=instance.pk)
        instance._prior_category_id = prior.category_id
        instance._prior_category_is_active = prior.is_active
    except Product.DoesNotExist:
        instance._prior_category_id = None
        instance._prior_category_is_active = None


def _bump_category_count(category_id, delta):
    """
    Atomic +1/-1 on Category.product_count — no COUNT query, no
    full_clean(), no ConnectedService rewrite. Explicitly invalidates
    the category page cache since this bypasses Category.save()
    (and therefore bypasses clear_category_cache() too).
    """
    if not category_id or delta == 0:
        return

    updated = Category.objects.filter(pk=category_id).update(
        product_count=F('product_count') + delta
    )
    if updated:
        invalidate_category_cache(Category(pk=category_id))


@receiver(post_save, sender=Product, dispatch_uid='product_sync_category_count_on_save')
def sync_category_product_count_on_save(sender, instance, created, **kwargs):
    """
    Bump product_count by ±1 for whichever category(ies) are affected,
    instead of recomputing via COUNT(). Only touches the DB when the
    product's *counted* state actually changed:
      - created & active                      -> +1 new category
      - deactivated / is_active flips False    -> -1 old category
      - reactivated / is_active flips True     -> +1 current category
      - moved between categories               -> -1 old, +1 new
    A routine save (view_count bump, price edit, description tweak)
    changes none of the above and is correctly a no-op.
    """
    prior_category_id = getattr(instance, '_prior_category_id', None)
    prior_is_active = getattr(instance, '_prior_category_is_active', None)

    was_counted = bool(prior_category_id) and bool(prior_is_active)
    is_counted = bool(instance.category_id) and bool(instance.is_active)

    if created:
        if is_counted:
            _bump_category_count(instance.category_id, +1)
        return

    if prior_category_id == instance.category_id:
        # Same category — only is_active could have flipped.
        if was_counted and not is_counted:
            _bump_category_count(instance.category_id, -1)
        elif not was_counted and is_counted:
            _bump_category_count(instance.category_id, +1)
        return

    # Category actually changed (reparented product).
    if was_counted:
        _bump_category_count(prior_category_id, -1)
    if is_counted:
        _bump_category_count(instance.category_id, +1)


@receiver(post_delete, sender=Product, dispatch_uid='product_sync_category_count_on_delete')
def sync_category_product_count_on_delete(sender, instance, **kwargs):
    """Hard deletes (Product.delete(hard_delete=True)) need a decrement too."""
    if instance.category_id and instance.is_active:
        _bump_category_count(instance.category_id, -1)


# ════════════════════════════════════════════════════════════════════
# CATEGORY — cache invalidation (mirrors clear_product_cache)
# ════════════════════════════════════════════════════════════════════

@receiver(post_save, sender=Category)
@receiver(post_delete, sender=Category)
def clear_category_cache(sender, instance, **kwargs):
    """
    Keeps CategoryListView's L1/Redis page cache and stats cache fresh
    whenever a Category row is saved or deleted through Category.save()
    itself — is_featured/is_trending toggles, display_order edits,
    soft_delete()/restore(), view_count bumps via
    increment_view_count(), etc.

    Does NOT cover the atomic product_count bump in the
    PRODUCT → CATEGORY.PRODUCT_COUNT section above — that bypasses
    Category.save() on purpose (to avoid a COUNT query + full_clean()
    + ConnectedService rewrite per product save) and instead calls
    invalidate_category_cache() directly from _bump_category_count().
    """
    invalidate_category_cache(instance)


# ════════════════════════════════════════════════════════════════════
# CATEGORY → BRAND.CATEGORY_COUNT
# ════════════════════════════════════════════════════════════════════

@receiver(pre_save, sender=Category, dispatch_uid='category_capture_prior_state')
def capture_prior_category_state(sender, instance, **kwargs):
    """Stash the DB's current brand_id/is_active before this save overwrites it."""
    if not instance.pk:
        instance._prior_brand_id = None
        instance._prior_is_active = None
        return

    try:
        prior = Category.objects.only('brand_id', 'is_active').get(pk=instance.pk)
        instance._prior_brand_id = prior.brand_id
        instance._prior_is_active = prior.is_active
    except Category.DoesNotExist:
        instance._prior_brand_id = None
        instance._prior_is_active = None


@receiver(post_save, sender=Category, dispatch_uid='category_sync_brand_count_on_save')
def sync_brand_category_count_on_save(sender, instance, created, **kwargs):
    """
    Recompute category_count for whichever brand(s) are affected —
    but only when brand or is_active actually changed, so routine
    saves (view_count bumps, display_order edits, etc.) don't trigger
    an extra COUNT query. Category's soft_delete()/restore() both go
    through save(), so they're covered here without needing their own
    branch.
    """
    prior_brand_id = getattr(instance, '_prior_brand_id', None)
    prior_is_active = getattr(instance, '_prior_is_active', None)

    brand_changed = (not created) and prior_brand_id != instance.brand_id
    active_changed = (not created) and prior_is_active != instance.is_active

    if not (created or brand_changed or active_changed):
        return

    if instance.brand_id:
        try:
            instance.brand.update_category_count()
        except Brand.DoesNotExist:
            pass

    if brand_changed and prior_brand_id:
        try:
            Brand.objects.get(pk=prior_brand_id).update_category_count()
        except Brand.DoesNotExist:
            pass


@receiver(post_delete, sender=Category, dispatch_uid='category_sync_brand_count_on_delete')
def sync_brand_category_count_on_delete(sender, instance, **kwargs):
    """Hard deletes (Category.delete(hard_delete=True)) need a recount too."""
    if instance.brand_id:
        try:
            Brand.objects.get(pk=instance.brand_id).update_category_count()
        except Brand.DoesNotExist:
            pass


# ════════════════════════════════════════════════════════════════════
# SUBCATEGORY → BRAND.SUBCATEGORY_COUNT
# ════════════════════════════════════════════════════════════════════

@receiver(pre_save, sender=SubCategory, dispatch_uid='subcategory_capture_prior_state')
def capture_prior_subcategory_state(sender, instance, **kwargs):
    """Stash the DB's current brand_id/is_active before this save overwrites it."""
    if not instance.pk:
        instance._prior_brand_id = None
        instance._prior_is_active = None
        return

    try:
        prior = SubCategory.objects.only('brand_id', 'is_active').get(pk=instance.pk)
        instance._prior_brand_id = prior.brand_id
        instance._prior_is_active = prior.is_active
    except SubCategory.DoesNotExist:
        instance._prior_brand_id = None
        instance._prior_is_active = None


@receiver(post_save, sender=SubCategory, dispatch_uid='subcategory_sync_brand_count_on_save')
def sync_brand_subcategory_count_on_save(sender, instance, created, **kwargs):
    """
    Recompute subcategory_count for whichever brand(s) are affected —
    same brand/is_active change-detection as CATEGORY above. Note that
    SubCategory.brand is a separate FK from SubCategory.category — a
    subcategory's category can be reparented without ever touching its
    brand, so that case correctly triggers nothing here.
    """
    prior_brand_id = getattr(instance, '_prior_brand_id', None)
    prior_is_active = getattr(instance, '_prior_is_active', None)

    brand_changed = (not created) and prior_brand_id != instance.brand_id
    active_changed = (not created) and prior_is_active != instance.is_active

    if not (created or brand_changed or active_changed):
        return

    if instance.brand_id:
        try:
            instance.brand.update_subcategory_count()
        except Brand.DoesNotExist:
            pass

    if brand_changed and prior_brand_id:
        try:
            Brand.objects.get(pk=prior_brand_id).update_subcategory_count()
        except Brand.DoesNotExist:
            pass


@receiver(post_delete, sender=SubCategory, dispatch_uid='subcategory_sync_brand_count_on_delete')
def sync_brand_subcategory_count_on_delete(sender, instance, **kwargs):
    """Hard deletes (SubCategory.delete(hard_delete=True)) need a recount too."""
    if instance.brand_id:
        try:
            Brand.objects.get(pk=instance.brand_id).update_subcategory_count()
        except Brand.DoesNotExist:
            pass


# ════════════════════════════════════════════════════════════════════
# PRODUCTRATING → BRAND.AVERAGE_RATING / REVIEW_COUNT
# ════════════════════════════════════════════════════════════════════
#
# ProductRating has no brand FK of its own — brand is reached via
# ProductRating.product.brand. Brand.refresh_rating_stats() re-
# aggregates every ProductRating across all of the brand's active
# products in one query, so unlike the count syncs above there's no
# cheap per-row increment/decrement available; every rating change
# recomputes the full average for that brand.

@receiver(pre_save, sender=ProductRating, dispatch_uid='rating_capture_prior_product')
def capture_prior_rating_product(sender, instance, **kwargs):
    """
    Stash the DB's current product_id before this save overwrites it.
    In practice a ProductRating's product is set once and never
    changed, but this guards against that case (e.g. a future admin
    action) without extra cost on the common path.
    """
    if not instance.pk:
        instance._prior_product_id = None
        return

    try:
        prior = ProductRating.objects.only('product_id').get(pk=instance.pk)
        instance._prior_product_id = prior.product_id
    except ProductRating.DoesNotExist:
        instance._prior_product_id = None


def _refresh_brand_rating_stats_for_product_id(product_id):
    """Look up product -> brand and refresh that brand's rating stats, if any."""
    if not product_id:
        return
    try:
        product = Product.objects.only('brand_id').get(pk=product_id)
    except Product.DoesNotExist:
        return
    if not product.brand_id:
        return
    try:
        Brand.objects.get(pk=product.brand_id).refresh_rating_stats()
    except Brand.DoesNotExist:
        pass


@receiver(post_save, sender=ProductRating, dispatch_uid='rating_sync_brand_stats_on_save')
def sync_brand_rating_stats_on_save(sender, instance, created, **kwargs):
    """
    A rating was created or its star value changed — recompute the
    affected brand's average_rating/review_count. Also handles the
    (currently theoretical) case of a rating being re-pointed at a
    different product by refreshing both the old and new product's
    brand.
    """
    prior_product_id = getattr(instance, '_prior_product_id', None)
    product_changed = (not created) and prior_product_id != instance.product_id

    _refresh_brand_rating_stats_for_product_id(instance.product_id)

    if product_changed and prior_product_id:
        _refresh_brand_rating_stats_for_product_id(prior_product_id)


@receiver(post_delete, sender=ProductRating, dispatch_uid='rating_sync_brand_stats_on_delete')
def sync_brand_rating_stats_on_delete(sender, instance, **kwargs):
    """A rating was removed — recompute the affected brand's stats."""
    _refresh_brand_rating_stats_for_product_id(instance.product_id)


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
    if not instance.created_by_id:
        return
    create_or_update_connected_service_for_subcategory(instance)