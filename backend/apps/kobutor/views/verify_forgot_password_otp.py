from django.shortcuts import render, redirect
from apps.kobutor.serializers.verify_forgot_password_otp_serializer import VerifyForgotPasswordOTPSerializer

def VerifyForgotPasswordOTPView(request):
    email_or_phone = request.GET.get("email_or_phone", "") if request.method == "GET" else request.POST.get("email_or_phone", "")


    if request.method == "POST":
        serializer = VerifyForgotPasswordOTPSerializer(data=request.POST)

        if serializer.is_valid():
            user = serializer.save()
            request.session['password_reset_user_id'] = user.id
            return redirect("kobutor:reset-password")

        return render(request, "kobutor/otp/verify_forget_password_otp.html", {
            "error": serializer.errors,
            "email_or_phone": email_or_phone,
        })

    return render(request, "kobutor/otp/verify_forget_password_otp.html", {
        "email_or_phone": email_or_phone,
    })
