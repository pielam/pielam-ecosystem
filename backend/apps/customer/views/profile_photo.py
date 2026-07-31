"""
apps/customer/views/profile_photo.py

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
def UpdateProfilePhotoView(request):
    """
    GET  -> render a small form/page to upload a new profile photo
    POST -> handle the uploaded file and update ProfileInfo.profile_photo
    """
    profile, _ = ProfileInfo.objects.get_or_create(user=request.user)

    if request.method == "POST":
        profile_photo = request.FILES.get("profile_photo")

        if not profile_photo:
            error_msg = "Please select an image to upload."
            if request.headers.get("x-requested-with") == "XMLHttpRequest":
                return JsonResponse({"success": False, "error": error_msg}, status=400)
            messages.error(request, error_msg)
            return redirect("customer:update_profile_photo")

        # Basic validation
        max_size_mb = 5
        if profile_photo.size > max_size_mb * 1024 * 1024:
            error_msg = f"Image must be smaller than {max_size_mb}MB."
            if request.headers.get("x-requested-with") == "XMLHttpRequest":
                return JsonResponse({"success": False, "error": error_msg}, status=400)
            messages.error(request, error_msg)
            return redirect("customer:update_profile_photo")

        allowed_types = ("image/jpeg", "image/png", "image/webp")
        if profile_photo.content_type not in allowed_types:
            error_msg = "Only JPEG, PNG, or WEBP images are allowed."
            if request.headers.get("x-requested-with") == "XMLHttpRequest":
                return JsonResponse({"success": False, "error": error_msg}, status=400)
            messages.error(request, error_msg)
            return redirect("customer:update_profile_photo")

        # Uses the model helper already defined on ProfileInfo
        profile.set_profile_photo(profile_photo, save=True)

        if request.headers.get("x-requested-with") == "XMLHttpRequest":
            return JsonResponse({
                "success": True,
                "profile_photo_url": profile.get_profile_photo_url(),
            })

        messages.success(request, "Profile photo updated successfully.")
        return redirect("customer:profile")

    # GET
    context = {"profile": profile}
    return render(request, "customer/update_profile_photo.html", context)