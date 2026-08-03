"""
Validators shared across apps.

The important one is ``validate_external_url``: several features accept a URL
from the user and then fetch it server-side. Without a guard that is a
server-side request forgery hole -- a caller can point the server at
``http://169.254.169.254/`` (cloud metadata) or at an internal admin port and
read the response back through the API.
"""

from __future__ import annotations

import socket
from typing import List
from urllib.parse import urlparse

from django.core.exceptions import ValidationError as DjangoValidationError

from apps.core.utils import is_private_ip

__all__ = [
    "ALLOWED_URL_SCHEMES",
    "validate_external_url",
    "validate_no_control_characters",
]

ALLOWED_URL_SCHEMES = ("http", "https")

#: Ports that are never a legitimate target for a user-supplied feed URL.
_BLOCKED_PORTS = {22, 23, 25, 445, 3306, 5432, 6379, 9200, 11211, 27017}


def _resolve_all(hostname: str) -> List[str]:
    """Every address the hostname resolves to (v4 and v6)."""
    try:
        infos = socket.getaddrinfo(hostname, None)
    except socket.gaierror as exc:
        raise DjangoValidationError(f"Could not resolve host '{hostname}'.") from exc
    return [info[4][0] for info in infos]


def validate_external_url(value: str) -> str:
    """
    Accept only a publicly-routable http(s) URL.

    Checks, in order: scheme allowlist, hostname present, no credentials in
    the URL, port not on the blocklist, and *every* address the hostname
    resolves to is public. Checking every address matters -- a hostname with
    both a public and a loopback record would otherwise slip through.

    Note this cannot fully close a DNS-rebinding race (the address may change
    between validation and fetch); the fetching code should also pin or
    re-check the resolved address.
    """
    if not value:
        raise DjangoValidationError("A URL is required.")

    parsed = urlparse(value.strip())

    if parsed.scheme.lower() not in ALLOWED_URL_SCHEMES:
        raise DjangoValidationError(
            f"URL scheme '{parsed.scheme}' is not allowed. Use http or https."
        )

    if not parsed.hostname:
        raise DjangoValidationError("The URL must include a hostname.")

    if parsed.username or parsed.password:
        raise DjangoValidationError("Credentials embedded in the URL are not allowed.")

    try:
        port = parsed.port
    except ValueError as exc:
        raise DjangoValidationError("The URL contains an invalid port.") from exc

    if port is not None and port in _BLOCKED_PORTS:
        raise DjangoValidationError(f"Connections to port {port} are not allowed.")

    for address in _resolve_all(parsed.hostname):
        if is_private_ip(address):
            raise DjangoValidationError(
                "The URL resolves to a private or reserved address."
            )

    return value.strip()


def validate_no_control_characters(value: str) -> str:
    """Reject NUL and other control characters in free-text fields."""
    if value and any(ord(char) < 32 and char not in "\t\n\r" for char in value):
        raise DjangoValidationError("This value contains invalid control characters.")
    return value
