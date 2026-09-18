from django.shortcuts import render, redirect
from django.contrib import messages
from django.views.decorators.http import require_http_methods
from django.views.decorators.csrf import csrf_protect
from django.contrib.auth import login as auth_login
from django.core.exceptions import ValidationError
from django.db import transaction
import logging

from megamind.validators.email_or_phone_validators import (
    email_or_phone_validator,
    parse_identity
)
from megamind.models.engine_users import User

logger = logging.getLogger(__name__)


@csrf_protect
@require_http_methods(["GET", "POST"])
def SignUpView(request):
    """
    Production-ready signup view aligned with custom User model
    """

    if request.method == "POST":
        email_or_phone = request.POST.get("email_or_phone", "").strip()
        password = request.POST.get("password", "")
        password2 = request.POST.get("password2", "")
        user_timezone = request.POST.get("timezone", "UTC")

        context = {
            "email_or_phone": email_or_phone,
            "timezone": user_timezone,
        }

        # ========= BASIC VALIDATION =========
        if not email_or_phone:
            messages.error(request, "Please enter your email or phone number.")
            return render(request, "auth/sign_up.html", context)

        if not password or not password2:
            messages.error(request, "Please enter and confirm your password.")
            return render(request, "auth/sign_up.html", context)

        if password != password2:
            messages.error(request, "Passwords do not match.")
            return render(request, "auth/sign_up.html", context)

        if len(password) < 8:
            messages.error(request, "Password must be at least 8 characters long.")
            return render(request, "auth/sign_up.html", context)

        if password.isdigit():
            messages.error(request, "Password cannot be entirely numeric.")
            return render(request, "auth/sign_up.html", context)

        # ========= IDENTITY VALIDATION =========
        try:
            email_or_phone_validator(email_or_phone)
            identity_type = parse_identity(email_or_phone)
        except ValidationError as e:
            messages.error(request, str(e))
            return render(request, "auth/sign_up.html", context)

        # ========= DUPLICATE CHECK =========
        if User.objects.filter(email_or_phone=email_or_phone).exists():
            messages.error(
                request,
                f"An account already exists with this {identity_type}."
            )
            return render(request, "auth/sign_up.html", context)

        # ========= USER CREATION =========
        try:
            with transaction.atomic():
                user = User.objects.create_user(
                    email_or_phone=email_or_phone,
                    password=password,
                    role="user",
                )

                user.user_timezone = user_timezone
                user.save(update_fields=["user_timezone", "updated_at"])

                logger.info(
                    f"User registered: {user.user_uuid} ({user.email_or_phone})"
                )

                # ========= VERIFICATION =========
                user.generate_verification_token()

                if identity_type == "email":
                    messages.success(
                        request,
                        "Account created successfully. "
                        "Please verify your email to continue."
                    )
                    # send_verification_email(user)

                else:
                    messages.success(
                        request,
                        "Account created successfully. "
                        "Please verify your phone number."
                    )
                    # send_verification_sms(user)

                # ❌ DO NOT auto-login unverified users
                return redirect("signin")

        except ValidationError as e:
            messages.error(request, str(e))
            logger.error(f"Signup validation error: {e}")
            return render(request, "auth/sign_up.html", context)

        except Exception as e:
            messages.error(
                request,
                "Something went wrong while creating your account."
            )
            logger.error("Unexpected signup error", exc_info=True)
            return render(request, "auth/sign_up.html", context)

    # ========= GET REQUEST =========
    return render(request, "auth/sign_up.html", {
        "timezones": [
            "UTC", "Asia/Dhaka", "Asia/Kolkata",
            "Asia/Tokyo", "Europe/London",
            "America/New_York"
        ]
    })
