# apps/customer/web_views/signup.py

import logging
import random
import requests
from django.shortcuts import render, redirect, get_object_or_404
from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.hashers import make_password, check_password
from django.contrib.auth.tokens import default_token_generator
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.core.mail import EmailMessage
from django.template.loader import render_to_string
from django.utils.encoding import force_bytes, force_str
from django.utils.http import urlsafe_base64_encode, urlsafe_base64_decode
from django.urls import reverse
from django.conf import settings

from apps.customer.validators import email_or_phone_validator, strong_password_validator
from apps.customer.serializers.sign_up_serializer import UserCreateSerializer

logger = logging.getLogger(__name__)

User = get_user_model()

# Roles a user can self-assign at public signup. Admin/staff/moderator
# must be granted through admin tooling, never through this form.
SELF_SIGNUP_ROLES = {User.Role.USER, User.Role.BUSINESS}

OTP_LENGTH = 6
OTP_VALIDITY_MINUTES = 10
OTP_CACHE_KEY = "phone_verification_otp:{user_id}"


# ---------------------------------------------------------------------------
# Email verification
# ---------------------------------------------------------------------------

def _build_email_verification_link(user, request):
    """
    Single-purpose, non-reusable verification token (not a JWT access
    token). default_token_generator ties the token to the user's pk,
    password hash and last_login, so it self-invalidates once used to
    verify or once the password changes, and it isn't a bearer
    credential if the email is intercepted.
    """
    uid = urlsafe_base64_encode(force_bytes(user.pk))
    token = default_token_generator.make_token(user)
    path = reverse("customer:verify-email", kwargs={"uidb64": uid, "token": token})
    return request.build_absolute_uri(path)


def send_verification_email(user, link):
    subject = "Verify your email address"
    # ASSUMPTION: template lives at this path; adjust to match your project.
    html_message = render_to_string(
        "customer/emails/verify_email.html",
        {"user": user, "verification_link": link},
    )
    email = EmailMessage(
        subject,
        html_message,
        settings.DEFAULT_FROM_EMAIL,
        [user.email],
    )
    email.content_subtype = "html"
    email.send()


def VerifyEmailView(request, uidb64, token):
    try:
        uid = force_str(urlsafe_base64_decode(uidb64))
        user = get_object_or_404(User, pk=uid)
    except (TypeError, ValueError, OverflowError, User.DoesNotExist):
        messages.error(request, "Invalid verification link.")
        return redirect("customer:signin")

    if user.email_verified:
        messages.success(request, "Email already verified. You can log in.")
        return redirect("customer:signin")

    if not default_token_generator.check_token(user, token):
        messages.error(
            request,
            "This verification link is invalid or has expired. "
            "Please request a new one from the login page.",
        )
        return redirect("customer:signin")

    # User.verify_email() flips email_verified/email_verified_at and,
    # if the account is still PENDING_VERIFICATION, activates it —
    # defined on the model itself, so no manual status juggling here.
    user.verify_email()

    messages.success(request, "Email verified successfully. You can now log in.")
    return redirect("customer:signin")


# ---------------------------------------------------------------------------
# Phone (OTP) verification
# ---------------------------------------------------------------------------

def _generate_phone_otp(user):
    """
    Generates a numeric OTP and stores only its hash in cache, keyed by
    user id, with a TTL. The plaintext code is returned once so it can
    be sent via SMS.

    ASSUMPTION: OTP state is kept in the cache backend (Django's
    CACHES setting) rather than on the User model, since the model has
    no otp/expiry fields and adding them would need a migration. If
    you're running with LocMemCache in a multi-process deployment,
    switch to a shared backend (Redis/Memcached) or this won't be
    visible across workers.
    """
    code = "".join(random.choices("0123456789", k=OTP_LENGTH))
    cache.set(
        OTP_CACHE_KEY.format(user_id=user.pk),
        make_password(code),
        timeout=OTP_VALIDITY_MINUTES * 60,
    )
    return code


def send_verification_sms(user, otp):
    # ASSUMPTION: replace with your actual SMS provider call
    # (e.g. Twilio, a custom gateway, etc). Kept provider-agnostic here.
    raise NotImplementedError(
        "Wire this up to your SMS provider. Expected signature: "
        "send_verification_sms(user, otp) -> None, raising on failure."
    )


def VerifyPhoneView(request):
    """
    Follow-up screen after phone signup: user submits the code they
    were texted. Expects `request.session['pending_phone_verification_user_id']`
    to have been set by SignUpView.
    """
    user_id = request.session.get("pending_phone_verification_user_id")
    if not user_id:
        messages.error(request, "No pending phone verification found. Please sign up again.")
        return redirect("customer:signup")

    user = get_object_or_404(User, pk=user_id)

    if request.method == "POST":
        submitted_code = request.POST.get("otp", "").strip()
        cache_key = OTP_CACHE_KEY.format(user_id=user.pk)
        stored_hash = cache.get(cache_key)

        if not stored_hash:
            messages.error(request, "This code has expired. Please request a new one.")
            return render(request, "customer/phones/verify_phone.html")

        if not check_password(submitted_code, stored_hash):
            messages.error(request, "Incorrect code. Please try again.")
            return render(request, "customer/phones/verify_phone.html")

        # User.verify_phone() flips phone_verified/phone_verified_at and
        # activates the account if it was still PENDING_VERIFICATION.
        user.verify_phone()
        cache.delete(cache_key)

        request.session.pop("pending_phone_verification_user_id", None)
        messages.success(request, "Phone verified successfully. You can now log in.")
        return redirect("customer:signin")

    return render(request, "customer/verify_phone.html")


# ---------------------------------------------------------------------------
# Signup
# ---------------------------------------------------------------------------

def SignUpView(request):
    if request.method == "POST":
        email_or_phone = request.POST.get("email_or_phone", "").strip()
        password1 = request.POST.get("password1", "")
        password2 = request.POST.get("password2", "")
        role = request.POST.get("role", User.Role.USER)

        recaptcha_response = request.POST.get("g-recaptcha-response", "")

        context = {
            "email_or_phone": email_or_phone,
            "role": role,
            "RECAPTCHA_SITE_KEY": settings.RECAPTCHA_SITE_KEY,
        }

    # Check reCAPTCHA first
        if not recaptcha_response:
            messages.error(request, "Please complete the reCAPTCHA.")
            return render(request, "customer/signup.html", context)

        # Verify reCAPTCHA with Google
        data = {
            "secret": settings.RECAPTCHA_SECRET_KEY,
            "response": recaptcha_response
        }
        r = requests.post("https://www.google.com/recaptcha/api/siteverify", data=data)
        result = r.json()
        if not result.get("success"):
            messages.error(request, "Invalid reCAPTCHA. Please try again.")
            return render(request, "customer/signup.html", context)

        if not email_or_phone or not password1 or not password2:
            messages.error(request, "All fields are required.")
            return render(request, "customer/signup.html", context)

        if password1 != password2:
            messages.error(request, "Passwords do not match.")
            return render(request, "customer/signup.html", context)

        # Role whitelist — prevents POSTing role=admin/staff/moderator
        # to self-elevate. Matches User.Role choices on the model.
        if role not in SELF_SIGNUP_ROLES:
            messages.error(request, "Invalid role selected.")
            return render(request, "customer/signup.html", context)

        try:
            email_or_phone_validator(email_or_phone)
        except ValidationError as e:
            messages.error(request, "; ".join(e.messages))
            return render(request, "customer/signup.html", context)

        try:
            strong_password_validator(password1)
        except ValidationError as e:
            messages.error(request, "; ".join(e.messages))
            return render(request, "customer/signup.html", context)

        if User.objects.filter(email_or_phone=email_or_phone).exists():
            messages.error(request, "User with this email or phone already exists.")
            return render(request, "customer/signup.html", context)

        serializer = UserCreateSerializer(data={
            "email_or_phone": email_or_phone,
            "password": password1,
            "role": role,
        })

        if not serializer.is_valid():
            error_messages = []
            for field, errors in serializer.errors.items():
                joined = "; ".join([str(e) for e in errors])
                error_messages.append(f"{field}: {joined}")
            messages.error(request, " ".join(error_messages))
            return render(request, "customer/signup.html", context)

        try:
            user = serializer.save()
        except Exception:
            messages.error(request, "An error occurred while creating your account.")
            return render(request, "customer/signup.html", context)

        # Account is created with account_status=PENDING_VERIFICATION
        # (model default). Confirm the identifier is real before
        # activating — link for email, OTP for phone.
        # NOTE: is_email / is_phone are model @property attributes,
        # not methods — no () on either.
        if user.is_email:
            link = _build_email_verification_link(user, request)
            try:
                send_verification_email(user, link)
            except Exception:
                logger.exception(
                    "Failed to send verification email to user id=%s email=%r",
                    user.pk, user.email,
                )
                messages.warning(
                    request,
                    "Account created, but the verification email failed to send. "
                    "You can request a new one from the login page."
                )
            messages.success(request, "Account created. Check your email to verify your address.")
            return redirect("customer:signin")

        elif user.is_phone:
            otp = _generate_phone_otp(user)
            try:
                send_verification_sms(user, otp)
            except Exception:
                logger.exception(
                    "Failed to send verification SMS to user id=%s phone=%r",
                    user.pk, user.phone,
                )
                messages.warning(
                    request,
                    "Account created, but the verification SMS failed to send. "
                    "You can request a new code from the login page."
                )
            request.session["pending_phone_verification_user_id"] = user.pk
            messages.success(request, "Account created. Enter the code we texted you.")
            return redirect("customer:verify-phone")

        # Neither branch matched (shouldn't happen given
        # email_or_phone_validator, but fail safe rather than silent).
        messages.success(request, "Account created. Please log in.")
        return redirect("customer:signin")

    return render(request, "customer/signup.html", {"RECAPTCHA_SITE_KEY": settings.RECAPTCHA_SITE_KEY})