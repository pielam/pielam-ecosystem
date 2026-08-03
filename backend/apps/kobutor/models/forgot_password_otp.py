"""
Password-reset OTP storage.

The original version stored the six-digit code in plaintext with no attempt
counter and no consumed flag. That is a practical account-takeover path: the
code space is only 10**6, the record survives until the next request, and
nothing stopped a client from trying every value. This version keeps a keyed
hash, counts attempts, and marks the code consumed once used.
"""

from __future__ import annotations

import secrets

from django.conf import settings
from django.db import models
from django.utils import timezone
from django.utils.crypto import constant_time_compare, salted_hmac
from django.utils.translation import gettext_lazy as _

#: How long a freshly issued code stays usable.
OTP_TTL_SECONDS = 300
#: Wrong guesses allowed before the code is burned and must be re-requested.
OTP_MAX_ATTEMPTS = 5
#: Minimum gap between two "send me a code" requests for the same user.
OTP_RESEND_COOLDOWN_SECONDS = 60
#: Digits in a generated code.
OTP_LENGTH = 6


def generate_otp_code(length: int = OTP_LENGTH) -> str:
    """
    Return a zero-padded numeric code from a cryptographically secure source.

    ``random.randint`` -- used by the previous request serializer -- is a
    Mersenne Twister: observing a couple of outputs is enough to predict the
    rest, so it must never issue security tokens.
    """
    upper = 10 ** length
    return str(secrets.randbelow(upper)).zfill(length)


def hash_otp_code(raw_code: str) -> str:
    """
    Keyed hash of a code, so a database leak does not hand over live codes.

    A keyed HMAC is used rather than a password hasher: the code lives for
    five minutes and is protected by an attempt counter, so the cost of a slow
    KDF buys nothing while adding latency to every verification. Rotating
    ``SECRET_KEY`` invalidates outstanding codes, which is acceptable for a
    five-minute token.
    """
    return salted_hmac(
        "apps.kobutor.forgot_password_otp",
        raw_code,
        algorithm="sha256",
    ).hexdigest()


class ForgotPasswordOTP(models.Model):
    """One outstanding password-reset code per user."""

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='otp_info',
        verbose_name=_("user"),
    )
    # Widened from 6: this column now holds a hex digest, never the code.
    reset_otp = models.CharField(
        _("hashed reset code"),
        max_length=128,
        blank=True,
        null=True,
    )
    reset_otp_created_at = models.DateTimeField(
        _("issued at"),
        blank=True,
        null=True,
    )
    attempts = models.PositiveSmallIntegerField(
        _("failed attempts"),
        default=0,
        help_text=_("Wrong guesses against the current code."),
    )
    verified_at = models.DateTimeField(
        _("verified at"),
        blank=True,
        null=True,
        help_text=_("Set once the code has been accepted; blocks reuse."),
    )

    class Meta:
        verbose_name = _("password reset code")
        verbose_name_plural = _("password reset codes")

    def __str__(self):
        return f"OTP info for {self.user.email_or_phone}"

    # ------------------------------------------------------------------
    # State
    # ------------------------------------------------------------------
    @property
    def is_expired(self) -> bool:
        if not self.reset_otp_created_at:
            return True
        age = (timezone.now() - self.reset_otp_created_at).total_seconds()
        return age >= OTP_TTL_SECONDS

    @property
    def is_consumed(self) -> bool:
        return self.verified_at is not None

    @property
    def attempts_exhausted(self) -> bool:
        return self.attempts >= OTP_MAX_ATTEMPTS

    def otp_is_valid(self) -> bool:
        """
        Whether a code is currently outstanding and still usable.

        Name kept from the original model because the admin lists it.
        """
        return bool(
            self.reset_otp
            and not self.is_expired
            and not self.is_consumed
            and not self.attempts_exhausted
        )

    def can_resend(self) -> bool:
        """Rate-limit re-issuing so the OTP channel cannot be used to spam."""
        if not self.reset_otp_created_at:
            return True
        age = (timezone.now() - self.reset_otp_created_at).total_seconds()
        return age >= OTP_RESEND_COOLDOWN_SECONDS

    # ------------------------------------------------------------------
    # Transitions
    # ------------------------------------------------------------------
    def issue_code(self, raw_code: str | None = None, commit: bool = True) -> str:
        """Store a new code and return the raw value to send to the user."""
        raw_code = raw_code or generate_otp_code()
        self.reset_otp = hash_otp_code(raw_code)
        self.reset_otp_created_at = timezone.now()
        self.attempts = 0
        self.verified_at = None
        if commit:
            self.save(update_fields=[
                'reset_otp', 'reset_otp_created_at', 'attempts', 'verified_at',
            ])
        return raw_code

    def check_code(self, raw_code: str) -> bool:
        """
        Verify a submitted code, counting the attempt.

        Returns ``False`` for expired, already-used and attempt-exhausted
        records too, so callers can surface a single generic message instead
        of telling an attacker which condition they hit.
        """
        if not self.otp_is_valid():
            return False

        if constant_time_compare(self.reset_otp, hash_otp_code(raw_code)):
            return True

        self.attempts += 1
        self.save(update_fields=['attempts'])
        return False

    def consume(self) -> None:
        """Burn the code so a verified OTP cannot be replayed."""
        self.reset_otp = None
        self.verified_at = timezone.now()
        self.save(update_fields=['reset_otp', 'verified_at'])

    def clear(self) -> None:
        """Drop the outstanding code entirely (e.g. after a password change)."""
        self.reset_otp = None
        self.reset_otp_created_at = None
        self.attempts = 0
        self.verified_at = None
        self.save(update_fields=[
            'reset_otp', 'reset_otp_created_at', 'attempts', 'verified_at',
        ])
