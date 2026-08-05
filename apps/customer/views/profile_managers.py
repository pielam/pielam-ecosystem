"""
Standard Profile & Account Settings View
-----------------------------------------
Single settings endpoint that reads (GET) and updates (POST) everything
exposed by the User, ProfileInfo, and ProfileViewLog models:

- Basic profile info (name, bio, DOB, location, photos)
- Business/professional info (dealer profiles)
- Social media links
- Privacy toggles
- Notification preferences
- Account settings (language, country, currency, marketing consent)
- Security (password change, MFA enable/disable)
- GDPR actions (deactivate, soft-delete account)
- "Who viewed your profile" panel

POST requests carry a hidden `action` field identifying which section
is being submitted, so one view + one template (with multiple forms)
can drive the whole settings page without extra endpoints.
"""

from django.contrib import messages
from django.contrib.auth import get_user_model, update_session_auth_hash
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.db import transaction
from django.shortcuts import get_object_or_404, redirect, render

from apps.customer.models.profile_info import ProfileInfo
from apps.customer.models.profile_view_log import ProfileViewLog

from apps.ponno.models.brand import Brand
from apps.ponno.models.category import Category
from apps.ponno.models.product import Product

User = get_user_model()


# ----------------------------------------------------------------------
# Field whitelists — only these are ever mass-assigned from POST data.
# Keeping this explicit avoids accidentally exposing fields like
# `is_profile_verified`, `metadata`, or FK fields to user-controlled input.
# ----------------------------------------------------------------------
PROFILE_TEXT_FIELDS = [
    "profile_name", "profile_bio", "profile_tagline", "profile_gender",
    "profile_dob", "profile_phone", "profile_language",
    "profile_country", "profile_state", "profile_city",
    "profile_address", "profile_postal_code",
]

BUSINESS_FIELDS = [
    "business_name", "business_type", "business_registration",
    "business_tax_id", "business_website", "business_email",
    "business_phone", "business_description",
]

SOCIAL_FIELDS = [
    "social_facebook", "social_twitter", "social_instagram",
    "social_linkedin", "social_youtube", "social_tiktok", "social_whatsapp",
]

PRIVACY_BOOL_FIELDS = [
    "is_profile_public", "show_email", "show_phone", "show_dob",
    "show_age", "show_location", "show_followers", "show_following",
    "allow_messages", "allow_follow",
]

NOTIFICATION_BOOL_FIELDS = [
    "notify_on_follow", "notify_on_message", "notify_on_comment",
    "notify_on_mention", "email_notifications", "sms_notifications",
]

ACCOUNT_FIELDS = ["language", "country", "currency"]


def _update_text_fields(instance, post_data, field_names):
    """Assign only the whitelisted fields that were actually submitted."""
    for field in field_names:
        if field in post_data:
            value = post_data.get(field, "").strip()
            setattr(instance, field, value or None)


def _update_boolean_fields(instance, post_data, field_names):
    """Checkbox semantics: a field's absence in POST means False/unchecked."""
    for field in field_names:
        setattr(instance, field, field in post_data)


def _error_message(exc: ValidationError) -> str:
    if hasattr(exc, "message_dict"):
        return " ".join(f"{k}: {', '.join(v)}" for k, v in exc.message_dict.items())
    return "; ".join(exc.messages) if hasattr(exc, "messages") else str(exc)


@login_required(login_url="/customer/signin/")
def ProfileManagerView(request):
    user = request.user
    profile_info = get_object_or_404(ProfileInfo, user=user)

    # ------------------------------------------------------------------
    # POST — one of several settings sections being saved
    # ------------------------------------------------------------------
    if request.method == "POST":
        action = request.POST.get("action")

        try:
            with transaction.atomic():

                if action == "update_profile":
                    _update_text_fields(profile_info, request.POST, PROFILE_TEXT_FIELDS)
                    if request.FILES.get("profile_photo"):
                        profile_info.profile_photo = request.FILES["profile_photo"]
                    if request.FILES.get("profile_cover_photo"):
                        profile_info.profile_cover_photo = request.FILES["profile_cover_photo"]
                    if profile_info.profile_name:
                        profile_info.generate_slug()
                    profile_info.full_clean()
                    profile_info.save()
                    messages.success(request, "Profile updated successfully.")

                elif action == "update_business":
                    if not user.is_dealer:
                        messages.error(request, "Only dealer accounts can edit business info.")
                    else:
                        _update_text_fields(profile_info, request.POST, BUSINESS_FIELDS)
                        profile_info.full_clean()
                        profile_info.save()
                        messages.success(request, "Business information updated.")

                elif action == "update_social":
                    _update_text_fields(profile_info, request.POST, SOCIAL_FIELDS)
                    profile_info.full_clean()
                    profile_info.save()
                    messages.success(request, "Social links updated.")

                elif action == "update_privacy":
                    _update_boolean_fields(profile_info, request.POST, PRIVACY_BOOL_FIELDS)
                    profile_info.save()
                    messages.success(request, "Privacy settings updated.")

                elif action == "update_notifications":
                    _update_boolean_fields(profile_info, request.POST, NOTIFICATION_BOOL_FIELDS)
                    profile_info.save()
                    messages.success(request, "Notification preferences updated.")

                elif action == "update_account":
                    for field in ACCOUNT_FIELDS:
                        if field in request.POST:
                            setattr(user, field, request.POST.get(field))
                    user.full_clean()
                    user.save()
                    messages.success(request, "Account settings updated.")

                elif action == "update_marketing_consent":
                    if "marketing_consent" in request.POST:
                        user.give_marketing_consent()
                    else:
                        user.revoke_marketing_consent()
                    messages.success(request, "Marketing preferences updated.")

                elif action == "change_password":
                    current_password = request.POST.get("current_password", "")
                    new_password = request.POST.get("new_password", "")
                    confirm_password = request.POST.get("confirm_password", "")

                    if not user.check_password(current_password):
                        messages.error(request, "Current password is incorrect.")
                    elif len(new_password) < 8:
                        messages.error(request, "New password must be at least 8 characters.")
                    elif new_password != confirm_password:
                        messages.error(request, "New passwords do not match.")
                    else:
                        user.set_password(new_password)
                        user.save()
                        update_session_auth_hash(request, user)  # keep the user logged in
                        messages.success(request, "Password changed successfully.")

                elif action == "enable_mfa":
                    method = request.POST.get("mfa_method", "totp")
                    user.enable_mfa(method=method)
                    messages.success(request, "Two-factor authentication enabled.")

                elif action == "disable_mfa":
                    user.disable_mfa()
                    messages.success(request, "Two-factor authentication disabled.")

                elif action == "deactivate_account":
                    user.deactivate_account()
                    messages.success(request, "Your account has been deactivated.")
                    return redirect("/customer/signin/")

                elif action == "delete_account":
                    # Require the user to type their own identifier to confirm,
                    # since soft_delete() irreversibly wipes email/phone fields.
                    confirm = request.POST.get("confirm_delete", "")
                    if confirm == user.email_or_phone:
                        user.soft_delete()
                        messages.success(request, "Your account has been deleted.")
                        return redirect("/customer/signin/")
                    messages.error(request, "Confirmation text did not match. Account not deleted.")

                else:
                    messages.error(request, "Unknown settings action.")

        except ValidationError as exc:
            messages.error(request, _error_message(exc))

        return redirect(request.path)

    # ------------------------------------------------------------------
    # GET — render the settings page
    # ------------------------------------------------------------------
    followings_count = profile_info.following.count()
    followers_count = profile_info.followers.count()

    if user.is_dealer:
        products_listed_count = Product.objects.filter(dealer=user).count()
        active_products_count = Product.objects.filter(dealer=user, is_active=True).count()
    else:
        products_listed_count = active_products_count = 0

    brands_count = Brand.objects.count()
    categories_count = Category.objects.count()
    brands = Brand.objects.all()
    categories = Category.objects.all()

    # "Who viewed your profile" — most recent identifiable viewers first
    recent_viewers = (
        ProfileViewLog.objects
        .filter(profile_user=user, viewer__isnull=False)
        .exclude(viewer=user)
        .select_related("viewer")
        .order_by("-viewed_at")[:20]
    )

    context = {
        "user": user,
        "profile_info": profile_info,
        "followings_count": followings_count,
        "followers_count": followers_count,
        "products_listed_count": products_listed_count,
        "active_products_count": active_products_count,
        "brands_count": brands_count,
        "categories_count": categories_count,
        "brands": brands,
        "categories": categories,
        "recent_viewers": recent_viewers,
        "profile_view_count": profile_info.profile_views,
        "is_dealer": user.is_dealer,
        "is_fully_verified": user.is_fully_verified,
        "needs_password_rotation": user.needs_password_rotation,
        "is_locked": user.is_locked,
        "completion_percentage": profile_info.completion_percentage,
    }
    return render(request, "customer/profile_managers.html", context)