from django.contrib import admin

from apps.kobutor.models.forgot_password_otp import ForgotPasswordOTP


@admin.register(ForgotPasswordOTP)
class ForgotPasswordOTPAdmin(admin.ModelAdmin):
    """
    Read-only view of outstanding reset codes.

    ``reset_otp`` is deliberately absent from ``list_display``, ``search_fields``
    and the edit form: it is a keyed hash now, so showing it is useless to a
    staff member and searching on it only helps someone who already has the
    digest.
    """

    list_display = (
        'user', 'reset_otp_created_at', 'attempts', 'verified_at', 'otp_valid',
    )
    list_filter = ('verified_at',)
    search_fields = ('user__email_or_phone',)
    raw_id_fields = ('user',)
    fields = ('user', 'reset_otp_created_at', 'attempts', 'verified_at', 'otp_valid')
    readonly_fields = ('user', 'reset_otp_created_at', 'attempts', 'verified_at', 'otp_valid')

    def has_add_permission(self, request):
        # Codes are only ever issued by the reset flow.
        return False

    @admin.display(boolean=True, description='OTP valid?')
    def otp_valid(self, obj):
        return obj.otp_is_valid()
