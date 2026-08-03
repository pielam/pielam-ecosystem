from django.contrib.auth import get_user_model
from rest_framework import serializers

from apps.kobutor.emails.forgot_password_otp_email import send_reset_password_otp_email
from apps.kobutor.models.forgot_password_otp import ForgotPasswordOTP

User = get_user_model()


class ForgotPasswordRequestSerializer(serializers.Serializer):
    """
    Issue a password-reset code.

    Two behaviours changed from the original:

    * It no longer rejects an unknown ``email_or_phone``. Doing so turned this
      endpoint into a user-enumeration oracle -- anyone could probe which
      addresses have accounts. The response is now identical either way and
      the work simply does not happen for an unknown identifier.
    * The code comes from ``secrets`` (via the model) rather than ``random``,
      and only its hash is stored.
    """

    email_or_phone = serializers.CharField(max_length=100)

    def save(self):
        email_or_phone = self.validated_data['email_or_phone']

        user = User.objects.filter(email_or_phone=email_or_phone).first()
        if user is None:
            return None

        otp_obj, _created = ForgotPasswordOTP.objects.get_or_create(user=user)

        # Per-account cooldown, independent of any request throttle.
        if not otp_obj.can_resend():
            return otp_obj

        raw_code = otp_obj.issue_code()
        send_reset_password_otp_email(user, raw_code)
        return otp_obj
