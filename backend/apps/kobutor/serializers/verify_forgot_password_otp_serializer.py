from django.contrib.auth import get_user_model
from rest_framework import serializers

from apps.kobutor.models.forgot_password_otp import ForgotPasswordOTP

User = get_user_model()

#: One message for every failure mode. Distinguishing "user not found" from
#: "wrong code" from "expired" tells an attacker which accounts exist and how
#: close they are; the user only needs to know to request a new code.
INVALID_OTP = "That code is not valid. Please request a new one."


class VerifyForgotPasswordOTPSerializer(serializers.Serializer):
    email_or_phone = serializers.CharField(max_length=100)
    otp = serializers.CharField(max_length=6, min_length=6, trim_whitespace=True)

    def validate(self, data):
        user = User.objects.filter(email_or_phone=data['email_or_phone']).first()
        if user is None:
            raise serializers.ValidationError(INVALID_OTP)

        otp_record = ForgotPasswordOTP.objects.filter(user=user).first()
        if otp_record is None:
            raise serializers.ValidationError(INVALID_OTP)

        # ``check_code`` is constant-time, counts the attempt, and refuses
        # expired / already-consumed / attempt-exhausted records.
        if not otp_record.check_code(data['otp']):
            raise serializers.ValidationError(INVALID_OTP)

        data['user'] = user
        data['otp_record'] = otp_record
        return data

    def save(self):
        # Burn the code: verified once, never replayable.
        self.validated_data['otp_record'].consume()
        return self.validated_data['user']
