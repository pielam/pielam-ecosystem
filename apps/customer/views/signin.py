from django.shortcuts import render, redirect
from django.contrib import messages
from django.contrib.auth import authenticate, login, get_user_model
from django.conf import settings
import requests

User = get_user_model()

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
        if not recaptcha_response:
            messages.error(request, "Please complete the reCAPTCHA.")
            return render(request, "customer/signin.html", context)

        data = {
            "secret": settings.RECAPTCHA_SECRET_KEY,
            "response": recaptcha_response
        }
        r = requests.post("https://www.google.com/recaptcha/api/siteverify", data=data)
        result = r.json()
        if not result.get("success"):
            messages.error(request, "Invalid reCAPTCHA. Please try again.")
            return render(request, "customer/signin.html", context)

        # -----------------------------
        # 2. Validate email/phone & password
        # -----------------------------
        if not email_or_phone or not password:
            messages.error(request, "Please enter both email/phone and password.")
            return render(request, "customer/signin.html", context)

        # Authenticate user
        user = authenticate(request, username=email_or_phone, password=password)

        if user is not None:
            if user.is_active:
                login(request, user)
                messages.success(request, f"Welcome back, {email_or_phone}!")
                return redirect("customer:profile")  # Replace with your dashboard URL
            else:
                messages.error(request, "Your account is inactive. Please contact support.")
        else:
            messages.error(request, "Invalid email/phone or password.")

        return render(request, "customer/signin.html", context)

    # GET request
    return render(request, "customer/signin.html", {"RECAPTCHA_SITE_KEY": settings.RECAPTCHA_SITE_KEY})
