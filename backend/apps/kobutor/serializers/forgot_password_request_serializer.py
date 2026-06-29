from rest_framework import serializers
from apps.kobutor.models.forgot_password_otp import ForgotPasswordOTP

from rest_framework import serializers
from django.contrib.auth import get_user_model

from django.utils import timezone
import random
from apps.kobutor.emails.forgot_password_otp_email import send_reset_password_otp_email
User = get_user_model()

class ForgotPasswordRequestSerializer(serializers.Serializer):
    email_or_phone = serializers.CharField(max_length=100)

    def validate_email_or_phone(self, value):
        if not User.objects.filter(email_or_phone=value).exists():
            raise serializers.ValidationError("User not found.")
        return value

    def save(self):
        email_or_phone = self.validated_data['email_or_phone']
        user = User.objects.get(email_or_phone=email_or_phone)

        otp_code = str(random.randint(100000, 999999))

        otp_obj, created = ForgotPasswordOTP.objects.update_or_create(
            user=user,
            defaults={
                'reset_otp': otp_code,
                'reset_otp_created_at': timezone.now()
            }
        )
        # Add sending OTP logic here if needed
        send_reset_password_otp_email(user, otp_code)
        return otp_obj
