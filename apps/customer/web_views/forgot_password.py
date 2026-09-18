# apps/customer/web_views/forgot_password.py

import random

from django.contrib.auth import get_user_model
from django.core.mail import EmailMessage
from django.http import HttpResponseRedirect
from django.shortcuts import render, redirect
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils import timezone
from django.conf import settings
from rest_framework import serializers

from apps.customer.models.forgot_password_otp import ForgotPasswordOTP
from apps.customer.validators import strong_password_validator

User = get_user_model()

OTP_LENGTH = 6


def send_reset_password_otp_email(user, otp_code):
    subject = "Your password reset code"
    # ASSUMPTION: template lives at this path; adjust to match your project.
    html_message = render_to_string(
        "customer/emails/forgot_password_otp_email.html",
        {"user": user, "otp_code": otp_code},
    )
    email = EmailMessage(
        subject,
        html_message,
        settings.DEFAULT_FROM_EMAIL,
        [user.email],
    )
    email.content_subtype = "html"
    email.send()


# ---------------------------------------------------------------------------
# Step 1 — request an OTP
# ---------------------------------------------------------------------------

class ForgotPasswordRequestSerializer(serializers.Serializer):
    email_or_phone = serializers.CharField(max_length=100)

    def validate_email_or_phone(self, value):
        # NOTE: this reveals whether an identifier is registered
        # ("User not found." vs a generic message), which is a known
        # user-enumeration trade-off for forgot-password flows. Kept as
        # in the original — flag if you'd rather always return a
        # generic "if an account exists..." response regardless.
        if not User.objects.filter(email_or_phone=value).exists():
            raise serializers.ValidationError("User not found.")
        return value

    def save(self):
        email_or_phone = self.validated_data['email_or_phone']
        user = User.objects.get(email_or_phone=email_or_phone)

        otp_code = str(random.randint(10 ** (OTP_LENGTH - 1), 10 ** OTP_LENGTH - 1))

        otp_obj, _created = ForgotPasswordOTP.objects.update_or_create(
            user=user,
            defaults={
                'reset_otp': otp_code,
                'reset_otp_created_at': timezone.now(),
            },
        )
        send_reset_password_otp_email(user, otp_code)
        return otp_obj


def ForgotPasswordView(request):
    if request.method == "POST":
        serializer = ForgotPasswordRequestSerializer(data=request.POST)
        if serializer.is_valid():
            serializer.save()
            email_or_phone = serializer.validated_data['email_or_phone']
            url = reverse("customer:verify-forgot-password-otp") + f"?email_or_phone={email_or_phone}"
            return HttpResponseRedirect(url)
        else:
            return render(request, "customer/forgot_password.html", {
                "error": serializer.errors,
            })

    return render(request, "customer/forgot_password.html")


# ---------------------------------------------------------------------------
# Step 2 — verify the OTP
# ---------------------------------------------------------------------------

class VerifyForgotPasswordOTPSerializer(serializers.Serializer):
    email_or_phone = serializers.CharField(max_length=100)
    otp = serializers.CharField(max_length=OTP_LENGTH)

    def validate(self, attrs):
        email_or_phone = attrs['email_or_phone']
        otp = attrs['otp'].strip()

        try:
            user = User.objects.get(email_or_phone=email_or_phone)
        except User.DoesNotExist:
            raise serializers.ValidationError("Invalid request. Please start over.")

        try:
            otp_obj = user.otp_info
        except ForgotPasswordOTP.DoesNotExist:
            raise serializers.ValidationError("No code was requested for this account. Please request a new one.")

        if not otp_obj.otp_is_valid():
            raise serializers.ValidationError("This code has expired. Please request a new one.")

        if not otp_obj.reset_otp or otp_obj.reset_otp != otp:
            raise serializers.ValidationError("Incorrect code. Please try again.")

        attrs['user'] = user
        attrs['otp_obj'] = otp_obj
        return attrs

    def save(self):
        user = self.validated_data['user']
        otp_obj = self.validated_data['otp_obj']

        # Burn the OTP immediately on successful verification so it
        # can't be replayed (e.g. via back button + resubmit) — the
        # rest of the flow from here on is gated by the session key
        # set below, not by the OTP anymore.
        otp_obj.reset_otp = None
        otp_obj.reset_otp_created_at = None
        otp_obj.save(update_fields=['reset_otp', 'reset_otp_created_at'])

        return user


def VerifyForgotPasswordOTPView(request):
    email_or_phone = (
        request.GET.get("email_or_phone", "")
        if request.method == "GET"
        else request.POST.get("email_or_phone", "")
    )

    if request.method == "POST":
        serializer = VerifyForgotPasswordOTPSerializer(data=request.POST)

        if serializer.is_valid():
            user = serializer.save()
            request.session['password_reset_user_id'] = user.pk
            return redirect("customer:reset-password")

        return render(request, "customer/otp/verify_forgot_password_otp.html", {
            "error": serializer.errors,
            "email_or_phone": email_or_phone,
        })

    return render(request, "customer/otp/verify_forgot_password_otp.html", {
        "email_or_phone": email_or_phone,
    })


# ---------------------------------------------------------------------------
# Step 3 — set a new password
# ---------------------------------------------------------------------------

class ResetPasswordSerializer(serializers.Serializer):
    new_password = serializers.CharField(
        write_only=True,
        validators=[strong_password_validator],
    )

    def save(self):
        user = self.context.get('user')
        new_password = self.validated_data['new_password']

        # set_password() (see apps/customer/models/account.py) updates
        # three fields on the instance: password itself, plus
        # password_changed_at and require_password_change, which it
        # resets as a side effect. We scope save() to exactly those
        # fields via update_fields.
        #
        # This matters because User.save() runs self.full_clean() on
        # ANY save that does NOT pass update_fields — a full instance
        # validation, including choice-field checks on every field on
        # the row (role, language, etc.), not just the ones being
        # changed here. A password reset has no business failing
        # because some unrelated legacy field (e.g. a stale `role` or
        # `language` value from before a choices list changed) no
        # longer validates. Passing update_fields skips full_clean()
        # entirely for this save (see User.save()'s own docstring/
        # changelog note #2), so this flow can't be blocked by
        # pre-existing, unrelated data-quality issues on the account.
        #
        # NOTE: this does not fix any bad legacy data already in the
        # DB (e.g. rows with role='dealer' or language='en' that don't
        # match the model's current choices) — it only stops THIS flow
        # from tripping over it. The underlying stale values should
        # still be cleaned up separately (data migration / one-off
        # management command).
        user.set_password(new_password)
        user.save(update_fields=[
            'password',
            'password_changed_at',
            'require_password_change',
        ])
        return user


def ResetPasswordView(request):
    user_id = request.session.get('password_reset_user_id')
    if not user_id:
        return redirect('customer:forgot-password')  # User must start from step 1

    user = User.objects.filter(pk=user_id).first()
    if not user:
        return redirect('customer:forgot-password')

    if request.method == "POST":
        serializer = ResetPasswordSerializer(data=request.POST, context={'user': user})
        if serializer.is_valid():
            serializer.save()
            request.session.pop('password_reset_user_id', None)
            return redirect("customer:signin")
        else:
            return render(request, "customer/reset_password.html", {
                "error": serializer.errors,
            })

    return render(request, "customer/reset_password.html")