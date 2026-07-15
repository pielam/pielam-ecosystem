# apps/ponno/views/product_upload.py
"""
Product upload view.

Design notes:
- Only `product_title` and `image` are required; `selling_price` defaults to
  0.00 and `video_url` is optional (validated with Django's own URLValidator).
- `buying_price` is intentionally omitted — it's nullable on the model and
  no longer collected here.
- SKU generation avoids a pre-save existence-check query; the DB's unique
  constraint is the source of truth, with a single retry on the
  astronomically rare collision.
- Validators/constants are module-level so they're built once at import
  time, not re-instantiated per request.
"""

import logging
import secrets
import string
from decimal import Decimal, InvalidOperation

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError as DjangoValidationError
from django.core.validators import URLValidator
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

VIDEO_URL_VALIDATOR = URLValidator(schemes=["http", "https"])

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
    Extract and validate POST data.

    Returns a (cleaned_data, errors) tuple. `cleaned_data` is only safe
    to use for object creation when `errors` is empty.
    """
    product_title = request.POST.get("product_title", "").strip()
    video_url = request.POST.get("video_url", "").strip()
    selling_price_str = request.POST.get("selling_price", "").strip()
    image = request.FILES.get("image")

    errors: list[str] = []

    if not product_title or len(product_title) < MIN_TITLE_LENGTH:
        errors.append(f"Product title is required (min {MIN_TITLE_LENGTH} characters).")

    if not image:
        errors.append("Product image is required.")

    if video_url:
        try:
            VIDEO_URL_VALIDATOR(video_url)
        except DjangoValidationError:
            errors.append("Video URL is not a valid URL.")

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
        "video_url": video_url or None,
        "selling_price": selling_price,
        "image": image,
    }
    return cleaned_data, errors


def _create_product(dealer, cleaned_data: dict) -> Product:
    """
    Persist a Product, retrying once with a fresh SKU on the rare
    collision. Raises IntegrityError if both attempts fail for a
    reason other than SKU collision, or if the retry also collides.
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
                    video_url=cleaned_data["video_url"],
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
            if "sku" not in str(exc).lower():
                raise  # not a SKU collision — surface immediately
            last_exc = exc
            logger.warning("SKU collision on %s, retrying", sku)

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
    POST -> validate, create the Product, redirect on success.
    """
    user = request.user

    if request.method == "GET":
        return render(request, UPLOAD_TEMPLATE, {"user": user})

    # ==================== POST ====================
    if user.role != "dealer":
        messages.error(request, "❌ User must be a dealer to upload products.")
        return redirect("customer:profile")

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