from django.urls import path
from apps.kobutor.views.verify_forgot_password_otp import VerifyForgotPasswordOTPView
from apps.kobutor.views.reset_password import ResetPasswordView


app_name = "kobutor"

from django.urls import path

urlpatterns = [
    path("verify-forgot-password-otp/", VerifyForgotPasswordOTPView, name="verify-forgot-password-otp"),
    path("reset-password/", ResetPasswordView, name="reset-password"),


    
    
]
