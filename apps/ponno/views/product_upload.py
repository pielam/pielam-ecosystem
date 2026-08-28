# apps/ponno/views/product_upload.py
"""
Product upload view.

Design notes:
- `product_title` is the ONLY required field on this form. `image` and
  `selling_price` are optional (`image` is nullable on Product;
  `selling_price` defaults to 0.00 when omitted).
- `buying_price` and `video_url` are intentionally omitted — both are
  nullable/blank on the model and no longer collected here.
- SKU generation avoids a pre-save existence-check query; the DB's unique
  constraint is the source of truth, with a single retry on the
  astronomically rare collision.
- Validators/constants are module-level so they're built once at import
  time, not re-instantiated per request.

ConnectedService:
- Uploading a product does NOT create a ConnectedService row anymore.
  Product and ConnectedService are independent on the create path —
  a dealer uploading a product is not the same action as connecting an
  external URL for crawling/mirroring, and conflating the two here
  meant every upload silently created a second row (with its own
  service_type, og_*, structured_data, etc.) that most callers never
  asked for.
  If a product ever needs a mirrored ConnectedService row (e.g. so it
  can be surfaced through whatever consumes ConnectedService), call
  megamind.services.connected_service_sync.sync_product_connected_service()
  explicitly wherever that's actually needed — it's unchanged and
  still the single shared bridge — this view just no longer calls it
  as a side effect of upload.
"""

import logging
import secrets
import string
from decimal import Decimal, InvalidOperation

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import IntegrityError, transaction
from django.shortcuts import redirect, render
from django.views.decorators.http import require_http_methods

from apps.ponno.models.product import Product

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Module-level constants (built once, reused across requests)
# ---------------------------------------------------------------------------

SKU_ALPHABET = string.ascii_uppercase + string.digits
SKU_LENGTH = 12
SKU_MAX_ATTEMPTS = 2  # initial attempt + 1 retry on collision

MIN_TITLE_LENGTH = 3
DEFAULT_SELLING_PRICE = Decimal("0.00")

# Hardcoded for now — swap for settings.SITE_URL later if that setting
# gets added.
UPLOAD_TEMPLATE = "ponno/product_upload.html"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def generate_sku(length: int = SKU_LENGTH) -> str:
    """
    Generate a collision-resistant SKU without querying the DB.

    36^12 ≈ 4.7 x 10^18 possible values, so a collision is vanishingly
    unlikely. Uniqueness is still enforced by the DB's unique constraint;
    the rare collision is handled reactively via IntegrityError, not by
    a defensive SELECT on every request.
    """
    return "".join(secrets.choice(SKU_ALPHABET) for _ in range(length))


def _validate_upload_form(request) -> tuple[dict, list[str]]:
    """
    Extract and validate POST data for the Product being created.

    Returns a (cleaned_data, errors) tuple. `cleaned_data` is only safe
    to use for object creation when `errors` is empty.
    """
    product_title = request.POST.get("product_title", "").strip()
    selling_price_str = request.POST.get("selling_price", "").strip()
    image = request.FILES.get("image")

    errors: list[str] = []

    if not product_title or len(product_title) < MIN_TITLE_LENGTH:
        errors.append(f"Product title is required (min {MIN_TITLE_LENGTH} characters).")

    selling_price = DEFAULT_SELLING_PRICE
    if selling_price_str:
        try:
            selling_price = Decimal(selling_price_str)
            if selling_price < 0:
                errors.append("Selling price cannot be negative.")
        except (InvalidOperation, ValueError):
            errors.append("Invalid selling price format.")

    cleaned_data = {
        "product_title": product_title,
        "selling_price": selling_price,
        "image": image,
    }
    return cleaned_data, errors


def _create_product(dealer, cleaned_data: dict) -> Product:
    """
    Persist a Product, retrying on the rare collision.

    Two independently-generated fields can collide under concurrent
    writes:
      - sku: generated via generate_sku() in this module (random,
        no pre-save existence check by design).
      - slug: generated via Product.generate_slug() inside
        Product.save(). That method does a check-then-write
        (`while Product.objects.filter(slug=slug)...exists()`) with
        no locking, so two requests creating products with the same
        product_title at nearly the same instant can both see a slug
        as free and both attempt to save it — one wins, the other
        hits the DB's unique constraint.

    Both are handled the same way: catch the IntegrityError, log
    which field collided, and retry with a fresh Product instance —
    a fresh instance means generate_sku() picks a new value AND
    Product.save() re-runs generate_slug() with a fresh existence
    check (the failed attempt was rolled back by the inner
    transaction.atomic() block, so it won't falsely block the retry).

    Raises IntegrityError if both attempts fail for a reason other
    than a sku/slug collision, or if the retry also collides.
    """
    last_exc: IntegrityError | None = None

    for _ in range(SKU_MAX_ATTEMPTS):
        sku = generate_sku()
        try:
            with transaction.atomic():
                return Product.objects.create(
                    dealer=dealer,
                    product_title=cleaned_data["product_title"],
                    product_name=cleaned_data["product_title"],
                    selling_price=cleaned_data["selling_price"],
                    image=cleaned_data["image"],
                    sku=sku,
                    stock=0,
                    currency="BDT",
                    is_active=True,
                    is_verified=False,
                    is_selling_price_visible=False,
                )
        except IntegrityError as exc:
            exc_text = str(exc).lower()
            if "sku" in exc_text:
                last_exc = exc
                logger.warning("SKU collision on %s, retrying", sku)
            elif "slug" in exc_text:
                last_exc = exc
                logger.warning(
                    "Slug collision for product_title=%r, retrying",
                    cleaned_data["product_title"],
                )
            else:
                raise  # not a sku/slug collision — surface immediately

    raise last_exc


# ---------------------------------------------------------------------------
# View
# ---------------------------------------------------------------------------

@require_http_methods(["GET", "POST"])
@login_required
def ProductUploadView(request):
    """
    Dealer product upload.

    GET  -> render the upload form.
    POST -> validate and create the Product, redirect on success.
    """
    user = request.user

    # Gate on role for BOTH verbs — a non-dealer shouldn't be able to
    # load the upload form on GET either, not just get turned away on
    # submit.
    if user.role != "dealer":
        messages.error(request, "❌ User must be a dealer to upload products.")
        return redirect("customer:profile")

    if request.method == "GET":
        return render(request, UPLOAD_TEMPLATE, {"user": user})

    # ==================== POST ====================
    cleaned_data, errors = _validate_upload_form(request)

    if errors:
        for err in errors:
            messages.error(request, f"❌ {err}")
        return render(request, UPLOAD_TEMPLATE, {
            "user": user,
            "form_data": request.POST,
        })

    try:
        product = _create_product(dealer=user, cleaned_data=cleaned_data)
    except IntegrityError:
        logger.exception("Product upload failed for dealer_id=%s", user.id)
        messages.error(request, "❌ Upload failed — please try again.")
        return render(request, UPLOAD_TEMPLATE, {
            "user": user,
            "form_data": request.POST,
        })
    except Exception:
        logger.exception("Unexpected error during product upload for dealer_id=%s", user.id)
        messages.error(request, "❌ Something went wrong — please try again.")
        return render(request, UPLOAD_TEMPLATE, {
            "user": user,
            "form_data": request.POST,
        })

    messages.success(
        request,
        f"✅ Product '{product.product_title}' uploaded successfully! "
        f"SKU: {product.sku}"
    )
    return redirect("business_profile")