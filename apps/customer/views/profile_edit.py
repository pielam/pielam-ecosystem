from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.db.models import Count, Q

from apps.customer.models.profile_info import ProfileInfo
from apps.ponno.models.brand import Brand
from apps.ponno.models.category import Category
from apps.ponno.models.product import Product


@login_required(login_url='/customer/signin/')
def EditProfileView(request):
    user = request.user
    profile_info = get_object_or_404(
        ProfileInfo.objects.select_related('user'),
        user=user
    )

    if request.method == "POST":
        # --- Text fields ---
        profile_info.profile_name        = request.POST.get("profile_name", "").strip() or None
        profile_info.profile_bio         = request.POST.get("profile_bio", "").strip() or None
        profile_info.profile_tagline     = request.POST.get("profile_tagline", "").strip() or None
        profile_info.profile_address     = request.POST.get("profile_address", "").strip() or None
        profile_info.profile_city        = request.POST.get("profile_city", "").strip() or None
        profile_info.profile_state       = request.POST.get("profile_state", "").strip() or None
        profile_info.profile_country     = request.POST.get("profile_country", "").strip() or None
        profile_info.profile_postal_code = request.POST.get("profile_postal_code", "").strip() or None
        profile_info.profile_gender      = request.POST.get("profile_gender") or None
        profile_info.profile_language    = request.POST.get("profile_language", "").strip() or None
        profile_info.profile_phone       = request.POST.get("profile_phone", "").strip() or None

        # --- Business fields (only relevant for dealers/business profiles) ---
        profile_info.business_name        = request.POST.get("business_name", "").strip() or None
        profile_info.business_type        = request.POST.get("business_type", "").strip() or None
        profile_info.business_email       = request.POST.get("business_email", "").strip() or None
        profile_info.business_phone       = request.POST.get("business_phone", "").strip() or None
        profile_info.business_website     = request.POST.get("business_website", "").strip() or None
        profile_info.business_description = request.POST.get("business_description", "").strip() or None

        # --- Social links ---
        profile_info.social_facebook  = request.POST.get("social_facebook", "").strip() or None
        profile_info.social_twitter   = request.POST.get("social_twitter", "").strip() or None
        profile_info.social_instagram = request.POST.get("social_instagram", "").strip() or None
        profile_info.social_linkedin  = request.POST.get("social_linkedin", "").strip() or None
        profile_info.social_youtube   = request.POST.get("social_youtube", "").strip() or None
        profile_info.social_tiktok    = request.POST.get("social_tiktok", "").strip() or None
        profile_info.social_whatsapp  = request.POST.get("social_whatsapp", "").strip() or None

        # --- Privacy toggles ---
        profile_info.show_email    = request.POST.get("show_email") == "on"
        profile_info.show_phone    = request.POST.get("show_phone") == "on"
        profile_info.show_dob      = request.POST.get("show_dob") == "on"
        profile_info.show_age      = request.POST.get("show_age") == "on"
        profile_info.show_location = request.POST.get("show_location") == "on"

        # --- DOB (only set if provided) ---
        profile_dob = request.POST.get("profile_dob", "").strip()
        if profile_dob:
            profile_info.profile_dob = profile_dob

        # --- Images (only replace if a new file was uploaded) ---
        if request.FILES.get("profile_photo"):
            profile_info.profile_photo = request.FILES["profile_photo"]

        if request.FILES.get("profile_cover_photo"):
            profile_info.profile_cover_photo = request.FILES["profile_cover_photo"]

        try:
            profile_info.save()
            messages.success(request, "Profile updated successfully!")
            return redirect("customer:profile")
        except Exception as e:
            messages.error(request, f"Something went wrong: {e}")

    # --- Aggregated stats (single queries) ---
    product_stats = Product.objects.filter(dealer=user).aggregate(
        products_listed_count=Count('id'),
        active_products_count=Count('id', filter=Q(is_active=True)),
    )

    social_stats = ProfileInfo.objects.filter(user=user).aggregate(
        followers_count=Count('followers'),
        followings_count=Count('following'),
    )

    context = {
        "user": user,
        "profile_info": profile_info,
        "followers_count":        social_stats["followers_count"],
        "followings_count":       social_stats["followings_count"],
        "products_listed_count":  product_stats["products_listed_count"],
        "active_products_count":  product_stats["active_products_count"],
        "brands_count":           Brand.objects.count(),
        "categories_count":       Category.objects.count(),
        # Choices for select fields
        "gender_choices":   ProfileInfo.Gender.choices,
        "language_choices": [
            ('en', 'English'), ('bn', 'Bengali'), ('es', 'Spanish'),
            ('fr', 'French'),  ('de', 'German'),  ('zh', 'Chinese'),
            ('ar', 'Arabic'),  ('hi', 'Hindi'),
        ],
    }

    return render(request, "customer/profile_edit.html", context)