from django.shortcuts import render, redirect
from django.contrib import messages
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.conf import settings
import requests

from apps.customer.validators import email_or_phone_validator, strong_password_validator
from apps.kobutor.emails.welcome_email import send_welcome_email
from apps.kobutor.sms import send_welcome_sms 
from apps.customer.serializers.sign_up_serializer import UserCreateSerializer

User = get_user_model()

def SignUpView(request):
    if request.method == "POST":
        email_or_phone = request.POST.get("email_or_phone", "").strip()
        password1 = request.POST.get("password1", "")
        password2 = request.POST.get("password2", "")
        role = request.POST.get("role", "customer")

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

        # Quick form-level validation
        if not email_or_phone or not password1 or not password2:
            messages.error(request, "All fields are required.")
            return render(request, "customer/signup.html", context)

        if password1 != password2:
            messages.error(request, "Passwords do not match.")
            return render(request, "customer/signup.html", context)
        
        try:
            email_or_phone_validator(email_or_phone)
        except ValidationError as e:
            messages.error(request, e.message)
            return render(request, "customer/signup.html", context)

        try:
            strong_password_validator(password1)
        except ValidationError as e:
            messages.error(request, e.message)
            return render(request, "customer/signup.html", context)
        
        if User.objects.filter(email_or_phone=email_or_phone).exists():
            messages.error(request, "User with this email or phone already exists.")
            return render(request, "customer/signup.html", context)

        # Serializer handles creation
        serializer = UserCreateSerializer(data={
            "email_or_phone": email_or_phone,
            "password": password1,
            "role": role
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

        # Send welcome messages
        if user.role == "customer":
            if user.is_email:
                try:
                    send_welcome_email(user)
                except Exception:
                    messages.warning(request, "Account created, but welcome email failed.")
            elif user.is_phone():
                try:
                    send_welcome_sms(user)
                except Exception:
                    messages.warning(request, "Account created, but welcome SMS failed.")

        messages.success(request, "Account created successfully. Please log in.")
        return redirect("customer:signin")

    return render(request, "customer/signup.html", {"RECAPTCHA_SITE_KEY": settings.RECAPTCHA_SITE_KEY})

