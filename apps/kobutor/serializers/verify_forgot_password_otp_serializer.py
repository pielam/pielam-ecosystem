
from rest_framework import serializers
from django.contrib.auth import get_user_model
from apps.kobutor.models.forgot_password_otp import ForgotPasswordOTP

User = get_user_model()

class VerifyForgotPasswordOTPSerializer(serializers.Serializer):
    email_or_phone = serializers.CharField(max_length=100)
    otp = serializers.CharField(max_length=6)

    def validate(self, data):
        email_or_phone = data['email_or_phone']
        otp = data['otp']

        # Validate user
        try:
            user = User.objects.get(email_or_phone=email_or_phone)
        except User.DoesNotExist:
            raise serializers.ValidationError("User not found.")

        # Validate OTP record
        try:
            otp_record = ForgotPasswordOTP.objects.get(user=user)
        except ForgotPasswordOTP.DoesNotExist:
            raise serializers.ValidationError("OTP not found. Please request again.")

        # Check OTP code
        if otp_record.reset_otp != otp:
            raise serializers.ValidationError("Incorrect OTP.")

        # Check OTP validity
        if not otp_record.otp_is_valid():
            raise serializers.ValidationError("OTP expired. Please request again.")

        data['user'] = user
        data['otp_record'] = otp_record
        return data

    def save(self):
        otp_record = self.validated_data['otp_record']
        # Mark OTP as used by clearing it (optional)
        otp_record.reset_otp = None
        otp_record.save()
        return self.validated_data['user']