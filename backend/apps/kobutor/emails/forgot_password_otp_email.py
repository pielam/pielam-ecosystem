
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

    # ``email_or_phone`` is the login identifier and may hold a phone number,
    # which is not a deliverable address. The dedicated ``email`` column is
    # populated by User.clean() when the identifier is an address, so prefer it.
    recipient = getattr(user, "email", None) or user.email_or_phone
    if "@" not in (recipient or ""):
        logger.warning("No email address on file for user %s; OTP not emailed", user.pk)
        return

    msg = EmailMultiAlternatives(
        subject=subject,
        body=f"Your OTP for password reset is {otp_code}",
        from_email=from_email,
        to=[recipient],
    )

    msg.attach_alternative(html_content, "text/html")

    try:
        msg.send()
    except Exception as e:
        logger.error(f"Failed to send reset OTP to {user.email_or_phone}: {e}")
        raise
