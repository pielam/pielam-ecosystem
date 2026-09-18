# apps/customer/web_views/signin.py

from django.shortcuts import render, redirect
from django.contrib import messages
from django.contrib.auth import authenticate, login, get_user_model
from django.conf import settings
import requests

User = get_user_model()


def _get_client_ip(request):
    forwarded_for = request.META.get("HTTP_X_FORWARDED_FOR")
    if forwarded_for:
        # First entry is the original client when behind a trusted proxy.
        return forwarded_for.split(",")[0].strip()
    return request.META.get("REMOTE_ADDR")


def SignInView(request):
    if request.method == "POST":
        email_or_phone = request.POST.get("email_or_phone", "").strip()
        password = request.POST.get("password", "")

        recaptcha_response = request.POST.get("g-recaptcha-response", "")

        context = {
            "email_or_phone": email_or_phone,
            "RECAPTCHA_SITE_KEY": settings.RECAPTCHA_SITE_KEY,
        }

        # -----------------------------
        # 1. Validate reCAPTCHA
        # -----------------------------
        # --- reCAPTCHA temporarily disabled -------------------------------
        if not recaptcha_response:
            messages.error(request, "Please complete the reCAPTCHA.")
            return render(request, "customer/signin.html", context)

        try:
            r = requests.post(
                "https://www.google.com/recaptcha/api/siteverify",
                data={
                    "secret": settings.RECAPTCHA_SECRET_KEY,
                    "response": recaptcha_response,
                },
                timeout=5,
            )
            result = r.json()
        except requests.RequestException:
            messages.error(request, "Could not verify reCAPTCHA. Please try again.")
            return render(request, "customer/signin.html", context)

        if not result.get("success"):
            messages.error(request, "Invalid reCAPTCHA. Please try again.")
            return render(request, "customer/signin.html", context)
        # --------------------------------------------------------------------

        # -----------------------------
        # 2. Validate email/phone & password
        # -----------------------------
        if not email_or_phone or not password:
            messages.error(request, "Please enter both email/phone and password.")
            return render(request, "customer/signin.html", context)

        # Look the account up ourselves (in addition to authenticate())
        # so lockout can be enforced BEFORE checking the password, and
        # so a failed attempt can be recorded against the right user
        # even when the password itself was correct-looking but the
        # account is locked. authenticate() alone can't do either of
        # these since it only returns a user or None.
        try:
            target_user = User.objects.get_by_natural_key(email_or_phone)
        except User.DoesNotExist:
            target_user = None

        if target_user and target_user.is_locked:
            messages.error(
                request,
                "This account is temporarily locked due to too many failed "
                "login attempts. Please try again later."
            )
            return render(request, "customer/signin.html", context)

        user = authenticate(request, username=email_or_phone, password=password)

        if user is not None:
            login(request, user)
            user.record_successful_login(
                ip_address=_get_client_ip(request),
                user_agent=request.META.get("HTTP_USER_AGENT", ""),
            )
            messages.success(request, f"Welcome back, {email_or_phone}!")
            return redirect("customer:profile")  # Replace with your dashboard URL

        # authenticate() returns None both for "wrong password" and for
        # "user exists but is_active=False" (ModelBackend filters
        # inactive users out internally) â€” so if we found a real,
        # active account above but still landed here, the password was
        # wrong. If the account is inactive, say so explicitly instead
        # of a generic error.
        if target_user and not target_user.is_active:
            messages.error(request, "Your account is inactive. Please contact support.")
        else:
            if target_user:
                target_user.record_failed_login()
            messages.error(request, "Invalid email/phone or password.")

        return render(request, "customer/signin.html", context)

    # GET request
    return render(request, "customer/signin.html", {"RECAPTCHA_SITE_KEY": settings.RECAPTCHA_SITE_KEY})
