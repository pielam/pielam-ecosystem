"""
apps/customer/views/cover_photo.py

Handles updating the profile cover photo for the logged-in user.
"""

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.shortcuts import redirect, render
from django.views.decorators.http import require_http_methods

from apps.customer.models.profile_info import ProfileInfo


@login_required
@require_http_methods(["GET", "POST"])
def UpdateCoverPhotoView(request):
    """
    GET  -> render a small form/page to upload a new cover photo
    POST -> handle the uploaded file and update ProfileInfo.profile_cover_photo
    """
    profile, _ = ProfileInfo.objects.get_or_create(user=request.user)

    if request.method == "POST":
        cover_photo = request.FILES.get("profile_cover_photo")

        if not cover_photo:
            error_msg = "Please select an image to upload."
            if request.headers.get("x-requested-with") == "XMLHttpRequest":
                return JsonResponse({"success": False, "error": error_msg}, status=400)
            messages.error(request, error_msg)
            return redirect("customer:update_cover_photo")

        # Basic validation
        max_size_mb = 5
        if cover_photo.size > max_size_mb * 1024 * 1024:
            error_msg = f"Image must be smaller than {max_size_mb}MB."
            if request.headers.get("x-requested-with") == "XMLHttpRequest":
                return JsonResponse({"success": False, "error": error_msg}, status=400)
            messages.error(request, error_msg)
            return redirect("customer:update_cover_photo")

        allowed_types = ("image/jpeg", "image/png", "image/webp")
        if cover_photo.content_type not in allowed_types:
            error_msg = "Only JPEG, PNG, or WEBP images are allowed."
            if request.headers.get("x-requested-with") == "XMLHttpRequest":
                return JsonResponse({"success": False, "error": error_msg}, status=400)
            messages.error(request, error_msg)
            return redirect("customer:update_cover_photo")

        # Uses the model helper already defined on ProfileInfo
        profile.set_profile_cover_photo(cover_photo, save=True)

        if request.headers.get("x-requested-with") == "XMLHttpRequest":
            return JsonResponse({
                "success": True,
                "cover_photo_url": profile.get_profile_cover_photo_url(),
            })

        messages.success(request, "Cover photo updated successfully.")
        return redirect("customer:profile")

    # GET
    context = {"profile": profile}
    return render(request, "customer/update_cover_photo.html", context)