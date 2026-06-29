from django.shortcuts import render, redirect
from django.contrib import messages
from django.contrib.auth import authenticate, login, get_user_model
from django.conf import settings

User = get_user_model()

def SignInView(request):
    if request.method == "POST":
        email_or_phone = request.POST.get("email_or_phone", "").strip()
        password = request.POST.get("password", "")
        
        context = {
            "email_or_phone": email_or_phone,
        
        }
        # -----------------------------
        # 2. Validate email/phone & password
        # -----------------------------
        if not email_or_phone or not password:
            messages.error(request, "Please enter both email/phone and password.")
            return render(request, "auth/sign_in.html", context)

        # Authenticate user
        user = authenticate(request, username=email_or_phone, password=password)

        if user is not None:
            if user.is_active:
                login(request, user)
                messages.success(request, f"Welcome back, {email_or_phone}!")
                return redirect("profile")  # Replace with your dashboard URL
            else:
                messages.error(request, "Your account is inactive. Please contact support.")
        else:
            messages.error(request, "Invalid email/phone or password.")

        return render(request, "auth/sign_in.html", context)

    # GET request
    return render(request, "auth/sign_in.html")
