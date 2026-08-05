
import logging
from django.core.mail import EmailMultiAlternatives
from django.template.loader import render_to_string
from django.conf import settings

logger = logging.getLogger(__name__)


def send_reset_password_otp_email(user, otp_code):
    subject = "Your OTP for Password Reset"
    from_email = settings.DEFAULT_FROM_EMAIL

    context = {
        "user": user,
        "otp_code": otp_code
    }

    html_content = render_to_string("kobutor/emails/forgot_password_otp.html", context)

    # Use email_or_phone as email (must be email)
    msg = EmailMultiAlternatives(
        subject=subject,
        body=f"Your OTP for password reset is {otp_code}",
        from_email=from_email,
        to=[user.email_or_phone]
    )

    msg.attach_alternative(html_content, "text/html")

    try:
        msg.send()
    except Exception as e:
        logger.error(f"Failed to send reset OTP to {user.email_or_phone}: {e}")
        raise
