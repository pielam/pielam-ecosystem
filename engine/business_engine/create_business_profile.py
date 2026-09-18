from django.core.exceptions import ValidationError
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.db.models import Count, Q
from django.shortcuts import get_object_or_404, redirect, render

from apps.customer.models.account import User
from apps.customer.models.profile_info import ProfileInfo
from apps.customer.models.business_info import BusinessInfo
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

    # BusinessInfo is lazily created (see its docstring), same as
    # LocationInfo/SocialInfo elsewhere -- but unlike those, its
    # business_name field is required (no default, blank=False). Using
    # get_or_create_for_user() here would build-and-save a brand-new row
    # with business_name="" on the very first GET, and BusinessInfo.save()
    # runs full_clean() on every full save -- so that save would raise
    # ValidationError before the page even renders, for any user who has
    # never filled the form out. Fetch instead, and only construct an
    # *unsaved* instance if none exists yet; it isn't persisted until the
    # POST below supplies (and validates) real data.
    business_info = BusinessInfo.objects.for_user(user)
    if business_info is None:
        business_info = BusinessInfo(user=user)

    if request.method == "POST":

        business_info.business_name = (
            request.POST.get("business_name", "").strip() or None
        )

        # business_type is required (has a default but no blank=True),
        # so fall back to OTHER rather than persisting None if the
        # form field is left empty.
        business_info.business_type = (
            request.POST.get("business_type", "").strip()
            or BusinessInfo.BusinessType.OTHER
        )

        business_info.business_email = (
            request.POST.get("business_email", "").strip() or None
        )

        business_info.business_phone = (
            request.POST.get("business_phone", "").strip() or None
        )

        business_info.website = (
            request.POST.get("business_website", "").strip() or None
        )

        business_info.description = (
            request.POST.get("business_description", "").strip() or None
        )

        try:
            with transaction.atomic():
                # business_info.save() runs full_clean(), which enforces
                # business_name being required on BusinessInfo. Only
                # flip the user's role once that validation (and the
                # save) actually succeeds, so we never end up with
                # role=BUSINESS and an invalid/missing business profile.
                business_info.save()

                if user.role != User.Role.BUSINESS:
                    user.change_role(User.Role.BUSINESS)

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
        "business_info": business_info,

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