"""
Scoped throttles for the endpoints worth rate-limiting.

Rates are configured in ``REST_FRAMEWORK["DEFAULT_THROTTLE_RATES"]`` keyed by
the ``scope`` below, so limits can be tuned per environment without touching
code.
"""

from __future__ import annotations

from rest_framework.throttling import ScopedRateThrottle, SimpleRateThrottle

from apps.core.utils import get_client_ip

__all__ = [
    "LoginThrottle",
    "RegistrationThrottle",
    "OTPRequestThrottle",
    "OTPVerifyThrottle",
    "WriteThrottle",
    "ScopedThrottle",
]


class ScopedThrottle(ScopedRateThrottle):
    """``throttle_scope`` on the view picks the rate. Uses the trusted client IP."""

    def get_ident(self, request):
        return get_client_ip(request) or super().get_ident(request)


class _IPThrottle(SimpleRateThrottle):
    """
    Throttles anonymous traffic by trusted client IP.

    Deliberately does *not* fall back to ``X-Forwarded-For`` parsing of its own
    -- ``get_client_ip`` already decides how much of that header to trust, so
    the limit cannot be bypassed by adding a fake hop.
    """

    def get_cache_key(self, request, view):
        ident = get_client_ip(request) or self.get_ident(request)
        return self.cache_format % {"scope": self.scope, "ident": ident}


class LoginThrottle(_IPThrottle):
    """Blunts credential stuffing against the sign-in endpoint."""

    scope = "login"


class RegistrationThrottle(_IPThrottle):
    scope = "registration"


class OTPRequestThrottle(_IPThrottle):
    """Stops an attacker from burning through OTP sends for a victim's account."""

    scope = "otp_request"


class OTPVerifyThrottle(_IPThrottle):
    """A 6-digit OTP is only 10^6 wide -- verification must be rate limited."""

    scope = "otp_verify"


class WriteThrottle(SimpleRateThrottle):
    """Per-user ceiling on mutating requests."""

    scope = "write"

    def get_cache_key(self, request, view):
        if request.method in ("GET", "HEAD", "OPTIONS"):
            return None
        user = request.user
        if user and user.is_authenticated:
            ident = user.pk
        else:
            ident = get_client_ip(request) or self.get_ident(request)
        return self.cache_format % {"scope": self.scope, "ident": ident}
