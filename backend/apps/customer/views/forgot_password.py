

from django.shortcuts import render, redirect
from apps.kobutor.serializers.forgot_password_request_serializer import ForgotPasswordRequestSerializer

from django.urls import reverse
from django.http import HttpResponseRedirect
from django.utils.http import urlencode

def ForgotPasswordView(request):
    if request.method == "POST":
        serializer = ForgotPasswordRequestSerializer(data=request.POST)
        if serializer.is_valid():
            serializer.save()
            email_or_phone = serializer.validated_data['email_or_phone']
            # urlencode: an identifier containing '+', '&' or '#' (phone
            # numbers routinely start with '+') was silently corrupted when
            # interpolated straight into the query string.
            query = urlencode({"email_or_phone": email_or_phone})
            url = f"{reverse('kobutor:verify-forgot-password-otp')}?{query}"
            return HttpResponseRedirect(url)
        else:
            return render(request, "customer/forgot_password.html", {
                "error": serializer.errors
            })

    return render(request, "customer/forgot_password.html")
