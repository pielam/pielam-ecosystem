from django.core.exceptions import ValidationError
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.db.models import Count, Q
from django.shortcuts import get_object_or_404, redirect, render

from apps.customer.models.account import User
from apps.customer.models.profile_info import ProfileInfo
from apps.ponno.models.brand import Brand
from apps.ponno.models.category import Category
from apps.ponno.models.product import Product


@login_required(login_url='/customer/signin/')
def CreateBusinessProfileView(request):
    user = request.user

    profile_info = get_object_or_404(
        ProfileInfo.objects.select_related("user"),
        user=user
    )

    if request.method == "POST":

        profile_info.profile_type = ProfileInfo.ProfileType.BUSINESS

        profile_info.business_name = (
            request.POST.get("business_name", "").strip() or None
        )

        profile_info.business_type = (
            request.POST.get("business_type", "").strip() or None
        )

        profile_info.business_email = (
            request.POST.get("business_email", "").strip() or None
        )

        profile_info.business_phone = (
            request.POST.get("business_phone", "").strip() or None
        )

        profile_info.business_website = (
            request.POST.get("business_website", "").strip() or None
        )

        profile_info.business_description = (
            request.POST.get("business_description", "").strip() or None
        )

        try:
            with transaction.atomic():
                # profile_info.save() runs full_clean(), which enforces
                # business_name being required for BUSINESS/PROFESSIONAL
                # profile_type. Only flip the user's role once that
                # validation (and the save) actually succeeds, so we
                # never end up with role='dealer' and an invalid/missing
                # business profile.
                profile_info.save()

                if user.role != User.Role.DEALER:
                    user.change_role(User.Role.DEALER)

            messages.success(
                request,
                "Business profile updated successfully."
            )

            return redirect("business_profile")

        except ValidationError as e:
            messages.error(
                request,
                f"Failed to update business profile: {'; '.join(e.messages)}"
                if hasattr(e, "messages") else str(e)
            )
        except Exception as e:
            messages.error(
                request,
                f"Failed to update business profile: {e}"
            )

    # Product statistics
    product_stats = Product.objects.filter(
        dealer=user
    ).aggregate(
        products_listed_count=Count("id"),
        active_products_count=Count(
            "id",
            filter=Q(is_active=True)
        ),
    )

    context = {
        "user": user,
        "profile_info": profile_info,

        # profile_info is already loaded, and these are plain
        # `.count()` calls under the hood on their own M2M managers,
        # so there's no cross-join risk the way there was when
        # followers/following were both Count()'d in one aggregate().
        "followers_count": profile_info.follower_count,
        "followings_count": profile_info.following_count,

        "products_listed_count":
            product_stats["products_listed_count"] or 0,

        "active_products_count":
            product_stats["active_products_count"] or 0,

        "brands_count":
            Brand.objects.count(),

        "categories_count":
            Category.objects.count(),
    }

    return render(
        request,
        "business/create_business_profile.html",
        context,
    )