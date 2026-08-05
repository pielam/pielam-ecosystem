from django.db import models
from django.conf import settings
from django.utils import timezone

class ForgotPasswordOTP(models.Model):
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='otp_info'
    )
    reset_otp = models.CharField(max_length=6, blank=True, null=True)
    reset_otp_created_at = models.DateTimeField(blank=True, null=True)

    def __str__(self):
        return f"OTP info for {self.user.email_or_phone}"

    def otp_is_valid(self):
        """Example helper to check if OTP is still valid (e.g., 5 minutes)"""
        if not self.reset_otp_created_at:
            return False
        return (timezone.now() - self.reset_otp_created_at).total_seconds() < 300
