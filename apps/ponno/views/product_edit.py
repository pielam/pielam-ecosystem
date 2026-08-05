# apps/ponno/views/product_edit.py

"""
Product Edit View
-----------------
Allows a dealer to edit their own product.
Handles GET (render form) and POST (save changes).

Covers every editable field on the Product model:
  Identification   — product_name, product_title, sku, barcode
  Relations        — brand, category, sub_category, dealer (admin only)
  Description      — description, short_description, warrenty_info,
                     delivery_info (JSON), product_condition
  Media            — image, video_url
  Pricing          — brand_price, buying_price, selling_price,
                     discount_percentage, tax_rate, currency
  Inventory        — stock, stock_status, low_stock_threshold,
                     track_inventory, allow_backorder,
                     min_order_quantity, max_order_quantity
  Shipping         — weight, length, width, height,
                     free_shipping, shipping_cost
  Status           — is_active, is_featured, is_trending, is_verified
  SEO              — meta_title, meta_description, meta_keywords
  Metadata         — metadata (raw JSON), specifications & features
                     stored inside metadata for structured display

ConnectedService sync:
- Every field this view lets a dealer change that also feeds the
  Product -> ConnectedService bridge (product_name/product_title,
  image, video_url, description/short_description, meta_title,
  meta_description, is_active) needs the mirrored ConnectedService
  row updated too, or the feed keeps showing stale title/image/
  description/status after an edit indefinitely.
- Handled via the same shared helper product_upload.py uses —
  megamind.services.connected_service_sync.sync_product_connected_service()
  — called with update_or_create semantics right after product.save()
  succeeds. service_type is intentionally NOT passed here (this form
  doesn't collect it), so the helper preserves whatever service_type
  the row already has rather than resetting it to the default on
  every edit. If a product somehow has no ConnectedService row yet
  (e.g. created before this sync existed), this self-heals by
  creating one.
- Scoped to product.dealer (the product's owner), not request.user —
  matters for privileged admin/staff edits of another dealer's
  product, where request.user != product.dealer.
"""

import json
import logging
from decimal import Decimal, InvalidOperation

from django.shortcuts import render, get_object_or_404, redirect
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.views.decorators.http import require_http_methods
from django.core.exceptions import ValidationError

from apps.ponno.models.product  import Product
from apps.ponno.models.brand     import Brand
from apps.ponno.models.category  import Category
from apps.ponno.models.sub_category import SubCategory
from megamind.services.connected_service_sync import sync_product_connected_service

logger = logging.getLogger(__name__)

# ────────────────────────────────────────────────────────────────────
# PRIVILEGE HELPERS
# ────────────────────────────────────────────────────────────────────

_PRIVILEGED_ROLES = {"admin", "staff", "moderator"}


def _is_privileged(user) -> bool:
    return getattr(user, "role", None) in _PRIVILEGED_ROLES


# ────────────────────────────────────────────────────────────────────
# CONTEXT BUILDER
# ────────────────────────────────────────────────────────────────────

def _build_form_context(product, brands, categories, sub_categories,
                        privileged=False):
    """
    Build the full template context for the edit form.
    sub_categories is filtered to the product's current category
    (or all active ones so JS can swap them on category change).
    """

    # Pull structured sub-keys out of metadata so templates can bind them
    meta     = product.metadata or {}
    specs    = meta.get("specifications", {})
    features = meta.get("features", "")

    return {
        "product":              product,
        "brands":               brands,
        "categories":           categories,
        "sub_categories":       sub_categories,

        # Choices for <select> elements
        "condition_choices":    Product.ProductCondition.choices,
        "stock_status_choices": Product.StockStatus.choices,

        # Computed helpers for the live price preview
        "is_on_sale":           product.is_on_sale,
        "is_low_stock":         product.is_low_stock,
        "profit":               product.calculate_profit(),

        # Structured metadata
        "specifications":       specs,
        "features":             features,

        # Role flag — lets template show admin-only fields
        "is_privileged":        privileged,
    }


# ────────────────────────────────────────────────────────────────────
# PARSING UTILITIES
# ────────────────────────────────────────────────────────────────────

def _parse_decimal(value, default=Decimal("0.00")):
    """Safely coerce any POST value to Decimal."""
    try:
        return Decimal(str(value).strip())
    except (InvalidOperation, TypeError, ValueError):
        return default


def _parse_int(value, default=0, minimum=None):
    """Safely coerce any POST value to int, optionally clamped to minimum."""
    try:
        result = int(str(value).strip())
        if minimum is not None:
            result = max(minimum, result)
        return result
    except (ValueError, TypeError):
        return default


def _parse_bool(value) -> bool:
    """HTML checkboxes submit 'on' when checked; absent when unchecked."""
    return value == "on"


def _parse_optional_int(raw: str):
    """Return int if raw is a non-empty digit string, else None."""
    stripped = (raw or "").strip()
    return int(stripped) if stripped.isdigit() else None


def _parse_optional_decimal(raw: str):
    """Return Decimal if raw is non-empty, else None."""
    stripped = (raw or "").strip()
    if not stripped:
        return None
    try:
        return Decimal(stripped)
    except InvalidOperation:
        return None


def _parse_json_field(raw: str, current):
    """
    Try to parse raw as JSON.
    Returns parsed value on success, current value on failure.
    """
    stripped = (raw or "").strip()
    if not stripped:
        return current
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        return current


# ────────────────────────────────────────────────────────────────────
# FIELD MAPPER
# ────────────────────────────────────────────────────────────────────

def _apply_post_data(product, data, files, privileged=False):
    """
    Map every POST field onto the Product instance.
    Does NOT call save() — the caller decides when to persist.

    Parameters
    ----------
    product    : Product instance (already fetched, pre-validated for ownership)
    data       : request.POST (QueryDict)
    files      : request.FILES (MultiValueDict)
    privileged : True when the requesting user is admin/staff/moderator
    """

    # ── Identification ───────────────────────────────────────────────
    product.product_name  = (data.get("product_name") or product.product_name).strip()
    product.product_title = (data.get("product_title") or "").strip() or None
    product.sku           = (data.get("sku")     or "").strip() or None
    product.barcode       = (data.get("barcode") or "").strip() or None

    # ── Relations ────────────────────────────────────────────────────
    # Brand
    brand_id = (data.get("brand") or "").strip()
    if brand_id:
        product.brand = Brand.objects.filter(pk=brand_id).first()
    elif "brand" in data:          # field was submitted but blank → clear it
        product.brand = None

    # Category
    category_id = (data.get("category") or "").strip()
    if category_id:
        product.category = Category.objects.filter(pk=category_id).first()
    elif "category" in data:
        product.category = None

    # SubCategory — depends on category, cleared if mismatched
    sub_category_id = (data.get("sub_category") or "").strip()
    if sub_category_id:
        sc = SubCategory.objects.filter(pk=sub_category_id).first()
        # Guard: sub_category must belong to the selected category
        if sc and product.category and sc.category_id != product.category_id:
            sc = None
        product.sub_category = sc
    elif "sub_category" in data:
        product.sub_category = None

    # Dealer reassignment — admin/staff only
    if privileged:
        dealer_id = (data.get("dealer") or "").strip()
        if dealer_id:
            from apps.customer.models.account import User
            new_dealer = User.objects.filter(pk=dealer_id, role="dealer").first()
            if new_dealer:
                product.dealer = new_dealer

    # ── Description ──────────────────────────────────────────────────
    product.short_description = (data.get("short_description") or "").strip() or None
    product.description       = (data.get("description")       or "").strip() or None
    product.warrenty_info     = (data.get("warrenty_info")     or "").strip() or None

    # delivery_info — stored as JSON dict on the model
    product.delivery_info = _parse_json_field(
        data.get("delivery_info_raw", ""),
        product.delivery_info or {}
    )
    # Also accept individual delivery sub-fields for convenience
    if any(k.startswith("delivery_") for k in data):
        delivery = product.delivery_info or {}
        for key in ("delivery_days", "delivery_note", "delivery_areas",
                    "express_available", "express_days", "express_note"):
            field_name = key                    # e.g. "delivery_days"
            if field_name in data:
                val = data[field_name].strip()
                if val:
                    delivery[key] = val
                else:
                    delivery.pop(key, None)
        product.delivery_info = delivery

    # Condition
    condition = data.get("product_condition", "").strip()
    if condition in dict(Product.ProductCondition.choices):
        product.product_condition = condition

    # ── Media ────────────────────────────────────────────────────────
    if files.get("image"):
        product.image = files["image"]
    product.video_url = (data.get("video_url") or "").strip() or None

# ── Pricing ──────────────────────────────────────────────────────
    product.brand_price          = _parse_optional_decimal(data.get("brand_price"))
    product.buying_price         = _parse_decimal(data.get("buying_price"),  product.buying_price)
    product.selling_price        = _parse_decimal(data.get("selling_price"), product.selling_price)
    product.discount_percentage  = _parse_decimal(data.get("discount_percentage"), product.discount_percentage)
    product.tax_rate             = _parse_decimal(data.get("tax_rate"),  product.tax_rate)

    currency = (data.get("currency") or product.currency or "BDT").strip().upper()
    if currency:
        product.currency = currency

    # Price visibility — independent toggles, any/all can be hidden.
    # Unchecked checkboxes are simply absent from POST data, so each
    # one defaults to False (hidden) unless explicitly checked "on".
    product.is_brand_price_visible   = _parse_bool(data.get("is_brand_price_visible"))
    product.is_buying_price_visible  = _parse_bool(data.get("is_buying_price_visible"))
    product.is_selling_price_visible = _parse_bool(data.get("is_selling_price_visible"))

    # ── Inventory ────────────────────────────────────────────────────
    product.stock               = _parse_int(data.get("stock", product.stock), minimum=0)
    product.low_stock_threshold = _parse_int(data.get("low_stock_threshold", product.low_stock_threshold), default=10, minimum=0)
    product.track_inventory     = _parse_bool(data.get("track_inventory"))
    product.allow_backorder     = _parse_bool(data.get("allow_backorder"))
    product.min_order_quantity  = _parse_int(data.get("min_order_quantity", product.min_order_quantity), default=1, minimum=1)
    product.max_order_quantity  = _parse_optional_int(data.get("max_order_quantity", ""))

    # Stock status — auto-resolved on save() if track_inventory is on,
    # but honour an explicit override if supplied.
    stock_status = data.get("stock_status", "").strip()
    if stock_status in dict(Product.StockStatus.choices):
        product.stock_status = stock_status

    # ── Shipping ─────────────────────────────────────────────────────
    product.free_shipping = _parse_bool(data.get("free_shipping"))
    product.weight        = _parse_optional_decimal(data.get("weight"))
    product.length        = _parse_optional_decimal(data.get("length"))
    product.width         = _parse_optional_decimal(data.get("width"))
    product.height        = _parse_optional_decimal(data.get("height"))
    product.shipping_cost = _parse_optional_decimal(data.get("shipping_cost"))

    # ── Status flags ─────────────────────────────────────────────────
    product.is_active   = _parse_bool(data.get("is_active"))
    product.is_featured = _parse_bool(data.get("is_featured"))
    product.is_trending = _parse_bool(data.get("is_trending"))

    # is_verified — admin/staff only
    if privileged:
        product.is_verified = _parse_bool(data.get("is_verified"))

    # ── SEO ──────────────────────────────────────────────────────────
    product.meta_title       = (data.get("meta_title")       or "").strip()[:60]  or None
    product.meta_description = (data.get("meta_description") or "").strip()[:160] or None
    product.meta_keywords    = (data.get("meta_keywords")    or "").strip()[:255] or None

    # ── Metadata (structured sub-fields stored inside the JSON column) ─
    meta = dict(product.metadata or {})

    # specifications — built from key/value pairs submitted by the spec builder
    raw_specs = data.get("specifications", "").strip()
    if raw_specs:
        parsed_specs = _parse_json_field(raw_specs, meta.get("specifications", {}))
        meta["specifications"] = parsed_specs

    # features — free-text block (one per line) kept inside metadata
    features_raw = (data.get("features") or "").strip()
    meta["features"] = features_raw  # empty string = cleared

    # arbitrary extra metadata — admin only, validated as JSON
    if privileged:
        extra_meta_raw = (data.get("extra_metadata") or "").strip()
        if extra_meta_raw:
            parsed_extra = _parse_json_field(extra_meta_raw, {})
            if isinstance(parsed_extra, dict):
                meta.update(parsed_extra)

    product.metadata = meta

    return product


# ────────────────────────────────────────────────────────────────────
# SUB-CATEGORY AJAX ENDPOINT
# ────────────────────────────────────────────────────────────────────

@login_required
@require_http_methods(["GET"])
def sub_categories_for_category(request):
    """
    AJAX helper: GET /products/sub-categories/?category=<pk>
    Returns JSON list of active sub-categories for a given category.
    Used by the edit form to dynamically populate the SubCategory <select>.
    """
    category_id = request.GET.get("category", "").strip()
    if not category_id:
        return _json_response([])

    qs = SubCategory.objects.filter(
        category_id=category_id,
        is_active=True,
        deleted_at__isnull=True,
    ).order_by("display_order", "sub_category_name").values(
        "pk", "sub_category_name"
    )
    return _json_response(list(qs))


def _json_response(data, status=200):
    from django.http import JsonResponse
    return JsonResponse(data, safe=False, status=status)


# ────────────────────────────────────────────────────────────────────
# MAIN EDIT VIEW
# ────────────────────────────────────────────────────────────────────

@login_required(login_url='/customer/signin/')
@require_http_methods(["GET", "POST"])

def ProductEditView(request, slug):
    """
    Edit an existing product.

    Access rules
    ------------
    - Admin / staff / moderator  →  can edit any non-deleted product,
                                    plus privileged-only fields
                                    (is_verified, dealer reassignment,
                                     extra_metadata).
    - Dealer                     →  can only edit their own products;
                                    is_verified is read-only.

    HTTP methods
    ------------
    GET  → render pre-filled form.
    POST → apply changes, validate, save, sync the mirrored
           ConnectedService row, redirect to product detail.
           On validation failure the form is re-rendered with errors.
    """

    privileged = _is_privileged(request.user)

    # ── Fetch product (scoped to ownership) ──────────────────────────
    if privileged:
        product = get_object_or_404(
            Product,
            slug=slug,
            deleted_at__isnull=True,
        )
    else:
        product = get_object_or_404(
            Product,
            slug=slug,
            dealer=request.user,
            deleted_at__isnull=True,
        )

    # ── Querysets for <select> fields ────────────────────────────────
    brands = Brand.objects.filter(
        is_active=True, deleted_at__isnull=True
    ).order_by("brand_name")

    categories = Category.objects.filter(
        is_active=True, deleted_at__isnull=True
    ).order_by("category_name")

    # Pre-load sub-categories for the product's current category;
    # the template also uses an AJAX call on category change.
    if product.category_id:
        sub_categories = SubCategory.objects.filter(
            category_id=product.category_id,
            is_active=True,
            deleted_at__isnull=True,
        ).order_by("display_order", "sub_category_name")
    else:
        sub_categories = SubCategory.objects.none()

    # ── GET ──────────────────────────────────────────────────────────
    if request.method == "GET":
        context = _build_form_context(
            product, brands, categories, sub_categories, privileged
        )
        return render(request, "ponno/product_edit.html", context)

    # ── POST ─────────────────────────────────────────────────────────
    try:
        _apply_post_data(product, request.POST, request.FILES, privileged)
        product.save()

        # Keep the mirrored feed post in sync with whatever just
        # changed (title, image, description, meta, active/status).
        # service_type is intentionally omitted — this form doesn't
        # collect it, so the helper preserves the row's existing
        # value instead of resetting it. Scoped to product.dealer,
        # not request.user, so a privileged admin editing someone
        # else's product still updates the right owner's row.
        try:
            sync_product_connected_service(dealer=product.dealer, product=product)
        except Exception:
            # Don't fail the whole edit if the mirror sync has a
            # problem (e.g. transient DB issue) — the Product save
            # already succeeded and is the source of truth; log it
            # so the drift is visible instead of silent.
            logger.exception(
                "ConnectedService sync failed after editing product pk=%s",
                product.pk,
            )

        logger.info(
            "Product '%s' (pk=%s) updated by user %s (role=%s)",
            product.product_name,
            product.pk,
            request.user.pk,
            getattr(request.user, "role", "?"),
        )

        messages.success(request, f"'{product.product_name}' updated successfully.")
        if product.is_active:
            return redirect("ponno:product_detail", slug=product.slug)
        return redirect("business_profile")

    except ValidationError as exc:
        logger.warning(
            "Validation error editing product pk=%s: %s",
            product.pk,
            exc.message_dict,
        )
        # Re-load sub_categories in case category changed during POST
        if product.category_id:
            sub_categories = SubCategory.objects.filter(
                category_id=product.category_id,
                is_active=True,
                deleted_at__isnull=True,
            ).order_by("display_order", "sub_category_name")

        context = _build_form_context(
            product, brands, categories, sub_categories, privileged
        )
        context["errors"] = exc.message_dict
        return render(request, "ponno/product_edit.html", context, status=422)

    except Exception as exc:
        logger.exception("Unexpected error editing product pk=%s", product.pk)
        context = _build_form_context(
            product, brands, categories, sub_categories, privileged
        )
        context["errors"] = {"__all__": [str(exc)]}
        return render(request, "ponno/product_edit.html", context, status=500)