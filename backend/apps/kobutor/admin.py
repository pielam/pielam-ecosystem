from django.contrib import admin

# Register your models here.
from django.contrib import admin
from apps.kobutor.models.forgot_password_otp import ForgotPasswordOTP

@admin.register(ForgotPasswordOTP)
class ForgotPasswordOTPAdmin(admin.ModelAdmin):
    list_display = ('user', 'reset_otp', 'reset_otp_created_at', 'otp_is_valid')
    search_fields = ('user__email_or_phone', 'reset_otp')
    readonly_fields = ('otp_is_valid',)

    def otp_is_valid(self, obj):
        return obj.otp_is_valid()
    otp_is_valid.boolean = True  # Show as a boolean icon
    otp_is_valid.short_description = 'OTP Valid?'
