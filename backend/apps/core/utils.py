"""
Small, dependency-free helpers shared across the API layer.

Nothing in here may import from a concrete app -- `apps.core` sits at the
bottom of the dependency graph so every other app can depend on it without
creating an import cycle.
"""

from __future__ import annotations

import ipaddress
from typing import Optional

from django.conf import settings

__all__ = [
    "get_client_ip",
    "get_user_agent",
    "is_private_ip",
    "boolean_param",
    "int_param",
]


def _trusted_proxy_count() -> int:
    """
    How many reverse proxies sit in front of this app.

    ``X-Forwarded-For`` is client-controlled, so the only trustworthy entries
    are the last N appended by our own infrastructure. Defaults to 0, which
    means the header is ignored entirely -- the safe default for a box that is
    reached directly.
    """
    return int(getattr(settings, "TRUSTED_PROXY_COUNT", 0))


def _is_valid_ip(value: Optional[str]) -> bool:
    if not value:
        return False
    try:
        ipaddress.ip_address(value)
    except ValueError:
        return False
    return True


def get_client_ip(request) -> Optional[str]:
    """
    Resolve the client IP, honouring ``TRUSTED_PROXY_COUNT``.

    With N trusted proxies the real client address is the Nth entry counted
    from the right of ``X-Forwarded-For``; everything to the left of it was
    supplied by the caller and can be forged. With no trusted proxies we use
    ``REMOTE_ADDR`` and nothing else.
    """
    proxies = _trusted_proxy_count()

    if proxies > 0:
        forwarded = request.META.get("HTTP_X_FORWARDED_FOR", "")
        if forwarded:
            chain = [part.strip() for part in forwarded.split(",") if part.strip()]
            # chain[-1] is our closest proxy; chain[-proxies] is the real client.
            if len(chain) >= proxies:
                candidate = chain[-proxies]
                if _is_valid_ip(candidate):
                    return candidate

    remote = request.META.get("REMOTE_ADDR")
    return remote if _is_valid_ip(remote) else None


def get_user_agent(request, max_length: int = 512) -> str:
    """Truncated User-Agent string, safe to store in a TextField."""
    return (request.META.get("HTTP_USER_AGENT") or "")[:max_length]


def is_private_ip(value: str) -> bool:
    """
    True for any address an outbound request must never reach: loopback,
    RFC1918, link-local (incl. cloud metadata at 169.254.169.254), multicast,
    reserved and unspecified ranges.
    """
    try:
        ip = ipaddress.ip_address(value)
    except ValueError:
        return True  # Unparseable -> treat as unsafe.

    return any(
        (
            ip.is_private,
            ip.is_loopback,
            ip.is_link_local,
            ip.is_multicast,
            ip.is_reserved,
            ip.is_unspecified,
        )
    )


_TRUE_VALUES = {"1", "true", "yes", "on"}
_FALSE_VALUES = {"0", "false", "no", "off"}


def boolean_param(raw, default=None):
    """Parse a query-string boolean. Returns ``default`` when unrecognised."""
    if raw is None:
        return default
    value = str(raw).strip().lower()
    if value in _TRUE_VALUES:
        return True
    if value in _FALSE_VALUES:
        return False
    return default


def int_param(raw, default=None, minimum=None, maximum=None):
    """Parse and clamp an integer query-string parameter."""
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    if minimum is not None:
        value = max(minimum, value)
    if maximum is not None:
        value = min(maximum, value)
    return value
