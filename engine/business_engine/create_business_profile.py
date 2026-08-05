from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.db.models import Count, Q

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
        
        user.role = 'dealer'
        user.save(update_fields=['role'])
        try:
            profile_info.save()

            messages.success(
                request,
                "Business profile updated successfully."
            )

            return redirect("business_profile")

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

    # Social statistics
    social_stats = ProfileInfo.objects.filter(
        user=user
    ).aggregate(
        followers_count=Count("followers"),
        followings_count=Count("following"),
    )

    context = {
        "user": user,
        "profile_info": profile_info,

        "followers_count":
            social_stats["followers_count"] or 0,

        "followings_count":
            social_stats["followings_count"] or 0,

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