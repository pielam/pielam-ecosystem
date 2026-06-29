# apps/ponno/views/product_actions.py

from django.http import JsonResponse
from django.contrib.auth.decorators import login_required
from django.views.decorators.http import require_POST
from django.shortcuts import get_object_or_404
from django.db.models import Avg, Count
from django.utils import timezone

from apps.ponno.models.product import Product, Wishlist


# ────────────────────────────────────────────────────────────────────
# WISHLIST TOGGLE
# ────────────────────────────────────────────────────────────────────

@require_POST
@login_required(login_url='/customer/signin/')
def WishlistToggleView(request, product_id):
    product = get_object_or_404(
        Product,
        product_id=product_id,
        is_active=True,
        deleted_at__isnull=True,
    )

    _, added = Wishlist.toggle(request.user, product)

    # Refresh so wishlist_count reflects the just-made change
    product.refresh_from_db(fields=['wishlist_count'])

    return JsonResponse({
        "wishlisted":     added,
        "wishlist_count": product.wishlist_count,
    })


# ────────────────────────────────────────────────────────────────────
# PRODUCT RATING
# ────────────────────────────────────────────────────────────────────

from apps.ponno.models.rating import ProductRating   # see model below


@require_POST
@login_required(login_url='/customer/signin/')
def ProductRatingView(request, product_id):
    product = get_object_or_404(
        Product,
        product_id=product_id,
        is_active=True,
        deleted_at__isnull=True,
    )

    try:
        rating_value = int(request.POST.get('rating', 0))
    except (ValueError, TypeError):
        return JsonResponse({"error": "Invalid rating value."}, status=400)

    if rating_value not in range(1, 6):
        return JsonResponse({"error": "Rating must be between 1 and 5."}, status=400)

    # Upsert — one rating per user per product
    obj, created = ProductRating.objects.update_or_create(
        user=request.user,
        product=product,
        defaults={
            "rating":     rating_value,
            "rated_at":   timezone.now(),
        },
    )

    # Recalculate average + count directly from DB (always accurate)
    agg = ProductRating.objects.filter(product=product).aggregate(
        avg=Avg('rating'),
        cnt=Count('id'),
    )
    new_avg   = round(float(agg['avg'] or 0), 2)
    new_count = agg['cnt'] or 0

    # Persist denormalised fields on Product for fast reads
    Product.objects.filter(pk=product.pk).update(
        rating_average=new_avg,
        review_count=new_count,
    )

    return JsonResponse({
        "rated":          True,
        "user_rating":    rating_value,
        "rating_average": new_avg,
        "review_count":   new_count,
        "created":        created,   # False = updated existing rating
    })


# ────────────────────────────────────────────────────────────────────
# USER'S EXISTING RATING  (GET — for pre-filling the modal)
# ────────────────────────────────────────────────────────────────────

@login_required(login_url='/customer/signin/')
def UserProductRatingView(request, product_id):
    """Returns the authenticated user's existing rating for a product."""
    product = get_object_or_404(Product, product_id=product_id, is_active=True)

    try:
        rating = ProductRating.objects.get(user=request.user, product=product)
        return JsonResponse({"user_rating": rating.rating})
    except ProductRating.DoesNotExist:
        return JsonResponse({"user_rating": None})


# apps/ponno/views/product_actions.py — add this

from django.db.models import F

@require_POST
def ProductShareView(request, product_id):
    """
    Increments share_count atomically.
    No login required — sharing is public.
    """
    product = get_object_or_404(
        Product,
        product_id=product_id,
        is_active=True,
        deleted_at__isnull=True,
    )

    Product.objects.filter(pk=product.pk).update(
        share_count=F('share_count') + 1
    )

    product.refresh_from_db(fields=['share_count'])

    return JsonResponse({
        "shared":       True,
        "share_count":  product.share_count,
    })