# megamind/utils/http_fetcher.py

"""

Resilient, SSRF-safe HTTP GET for scraping arbitrary third-party URLs.

Why this exists as its own file: every extractor in this toolkit needs
the same hardening (retries, backoff, a couple of user-agent fallbacks,
a byte cap so a huge response can't blow up memory, and SSRF protection
so a malicious/misconfigured URL can't be used to reach internal
services). Centralizing it here means content_extractor.py,
video_platforms.py, and bind_content.py all get identical, correct
behavior instead of three slightly-different copies.

    pip install requests --break-system-packages
"""

import ipaddress
import logging
import socket
import time
from typing import Optional
from urllib.parse import urljoin, urlparse

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 12
DEFAULT_MAX_RETRIES = 2
DEFAULT_BACKOFF_FACTOR = 0.5
DEFAULT_MAX_CONTENT_BYTES = 15 * 1024 * 1024  # 15 MB
DEFAULT_MAX_REDIRECTS = 5

# Tried in order on failure. Some sites block the default UA specifically
# (bot lists match it) but allow Googlebot or another browser UA.
FALLBACK_USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.0 Safari/605.1.15",
]

DEFAULT_HEADERS = {
    "User-Agent": FALLBACK_USER_AGENTS[0],
    "Accept-Language": "en-US,en;q=0.9",
}


class UnsafeURLError(ValueError):
    """URL (or a redirect target) resolves to a private/loopback/link-local address."""


class ContentTooLarge(Exception):
    pass


def _is_public_ip(ip_str: str) -> bool:
    try:
        ip = ipaddress.ip_address(ip_str)
    except ValueError:
        return False
    return not (
        ip.is_private or ip.is_loopback or ip.is_link_local
        or ip.is_multicast or ip.is_reserved or ip.is_unspecified
    )


def assert_public_url(url: str) -> None:
    """Raises UnsafeURLError if `url` isn't safe to fetch server-side.
    Checks every resolved A/AAAA record, not just the first (DNS can
    return multiple answers)."""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise UnsafeURLError(f"Unsupported scheme: {parsed.scheme!r}")
    hostname = parsed.hostname
    if not hostname:
        raise UnsafeURLError("URL has no hostname")
    if hostname.lower() in ("localhost", "0.0.0.0"):
        raise UnsafeURLError("Refusing to fetch localhost")
    try:
        addrinfo = socket.getaddrinfo(hostname, None)
    except socket.gaierror as exc:
        raise UnsafeURLError(f"DNS resolution failed for {hostname!r}: {exc}") from exc
    for info in addrinfo:
        ip_str = info[4][0]
        if not _is_public_ip(ip_str):
            raise UnsafeURLError(f"Host {hostname!r} resolves to non-public address {ip_str}")


def build_session(max_retries: int = DEFAULT_MAX_RETRIES,
                   backoff_factor: float = DEFAULT_BACKOFF_FACTOR) -> requests.Session:
    """Session with retry/backoff on connection errors and 429/5xx. Only
    GET is retried — this toolkit never posts to a scraped site."""
    session = requests.Session()
    retry = Retry(
        total=max_retries, connect=max_retries, read=max_retries,
        backoff_factor=backoff_factor,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset(["GET"]),
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


def guarded_get(session: requests.Session, url: str, headers: dict,
                 timeout: int = DEFAULT_TIMEOUT, verify: bool = True,
                 max_redirects: int = DEFAULT_MAX_REDIRECTS) -> requests.Response:
    """Like session.get(url, allow_redirects=True) but re-validates
    every hop against assert_public_url() — a URL that looks public can
    still 302 to an internal address, so allow_redirects=True alone
    isn't a safe way to follow untrusted URLs."""
    current_url = url
    for _ in range(max_redirects + 1):
        assert_public_url(current_url)
        response = session.get(
            current_url, headers=headers, timeout=timeout,
            allow_redirects=False, stream=True, verify=verify,
        )
        if response.is_redirect or response.is_permanent_redirect:
            location = response.headers.get("Location")
            response.close()
            if not location:
                return response
            current_url = urljoin(current_url, location)
            continue
        return response
    raise UnsafeURLError(f"Exceeded max redirects ({max_redirects}) for {url}")


def read_capped(response: requests.Response, max_bytes: int = DEFAULT_MAX_CONTENT_BYTES) -> bytes:
    declared = response.headers.get("Content-Length")
    if declared is not None:
        try:
            if int(declared) > max_bytes:
                raise ContentTooLarge(f"Declared Content-Length {declared} exceeds cap of {max_bytes}")
        except ValueError:
            pass
    chunks, total = [], 0
    for chunk in response.iter_content(chunk_size=65536):
        if not chunk:
            continue
        total += len(chunk)
        if total > max_bytes:
            raise ContentTooLarge(f"Response exceeded cap of {max_bytes} bytes while streaming")
        chunks.append(chunk)
    return b"".join(chunks)


def resilient_get(
    url: str,
    extra_headers: Optional[dict] = None,
    timeout: int = DEFAULT_TIMEOUT,
    max_retries: int = DEFAULT_MAX_RETRIES,
    backoff_factor: float = DEFAULT_BACKOFF_FACTOR,
    max_content_bytes: int = DEFAULT_MAX_CONTENT_BYTES,
):
    """
    Fetch `url`, retrying across a couple of user-agents and — as a
    last resort — with SSL verification relaxed. Never raises for
    ordinary network failures; returns them via `error` instead.

    Returns: (raw_bytes, response_headers, final_url, error)
        raw_bytes is None iff error is set.

    Raises UnsafeURLError immediately (not retried — it's a policy
    decision, not a transient failure) if the URL or any redirect hop
    resolves to a private/internal address.
    """
    assert_public_url(url)  # fail fast before burning any retries

    last_error = None
    for ua_index, ua in enumerate(FALLBACK_USER_AGENTS):
        headers = dict(DEFAULT_HEADERS, **(extra_headers or {}))
        headers["User-Agent"] = ua
        # Only relax SSL verification on the final attempt.
        for verify_ssl in ((True,) if ua_index < len(FALLBACK_USER_AGENTS) - 1 else (True, False)):
            session = build_session(max_retries, backoff_factor)
            try:
                response = guarded_get(session, url, headers, timeout=timeout, verify=verify_ssl)
                if response.status_code >= 400:
                    last_error = f"HTTP {response.status_code}"
                    response.close()
                    continue
                try:
                    raw_bytes = read_capped(response, max_content_bytes)
                except ContentTooLarge as exc:
                    last_error = str(exc)
                    response.close()
                    continue
                result_headers = dict(response.headers)
                final_url = response.url
                response.close()
                return raw_bytes, result_headers, final_url, None
            except UnsafeURLError:
                raise  # redirect landed on a blocked address — don't keep retrying
            except requests.RequestException as exc:
                last_error = str(exc)
                time.sleep(0.2)
                continue
            finally:
                session.close()

    logger.warning("resilient_get exhausted all attempts for %s: %s", url, last_error)
    return None, {}, url, last_error



"""
http_fetcher.py

Resilient, SSRF-safe HTTP GET for fetching an arbitrary, user-supplied
URL.

- Retries with backoff on transient failures (429/5xx/connection errors)
- Rotates across a few User-Agents if a site blocks the default one
- Caps response size so a huge/streaming response can't blow up memory
- Validates every redirect hop (not just the original URL) resolves to
  a public IP, so a URL that looks safe can't 302 its way into an
  internal service

    pip install requests --break-system-packages
"""

import ipaddress
import logging
import socket
import time
from typing import Optional
from urllib.parse import urljoin, urlparse

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 12
DEFAULT_MAX_RETRIES = 2
DEFAULT_BACKOFF_FACTOR = 0.5
DEFAULT_MAX_CONTENT_BYTES = 15 * 1024 * 1024  # 15 MB
DEFAULT_MAX_REDIRECTS = 5

FALLBACK_USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.0 Safari/605.1.15",
]

DEFAULT_HEADERS = {
    "User-Agent": FALLBACK_USER_AGENTS[0],
    "Accept-Language": "en-US,en;q=0.9",
}


class UnsafeURLError(ValueError):
    """URL (or a redirect target) resolves to a private/loopback/link-local address."""


class ContentTooLarge(Exception):
    pass


def _is_public_ip(ip_str: str) -> bool:
    try:
        ip = ipaddress.ip_address(ip_str)
    except ValueError:
        return False
    return not (
        ip.is_private or ip.is_loopback or ip.is_link_local
        or ip.is_multicast or ip.is_reserved or ip.is_unspecified
    )


def assert_public_url(url: str) -> None:
    """Raises UnsafeURLError if `url` isn't safe to fetch server-side.
    Checks every resolved A/AAAA record, not just the first."""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise UnsafeURLError(f"Unsupported scheme: {parsed.scheme!r}")
    hostname = parsed.hostname
    if not hostname:
        raise UnsafeURLError("URL has no hostname")
    if hostname.lower() in ("localhost", "0.0.0.0"):
        raise UnsafeURLError("Refusing to fetch localhost")
    try:
        addrinfo = socket.getaddrinfo(hostname, None)
    except socket.gaierror as exc:
        raise UnsafeURLError(f"DNS resolution failed for {hostname!r}: {exc}") from exc
    for info in addrinfo:
        ip_str = info[4][0]
        if not _is_public_ip(ip_str):
            raise UnsafeURLError(f"Host {hostname!r} resolves to non-public address {ip_str}")


def build_session(max_retries: int = DEFAULT_MAX_RETRIES,
                   backoff_factor: float = DEFAULT_BACKOFF_FACTOR) -> requests.Session:
    """Session with retry/backoff on connection errors and 429/5xx. Only
    GET is retried — this toolkit never posts to a fetched site."""
    session = requests.Session()
    retry = Retry(
        total=max_retries, connect=max_retries, read=max_retries,
        backoff_factor=backoff_factor,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset(["GET"]),
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


def guarded_get(session: requests.Session, url: str, headers: dict,
                 timeout: int = DEFAULT_TIMEOUT, verify: bool = True,
                 max_redirects: int = DEFAULT_MAX_REDIRECTS) -> requests.Response:
    """Like session.get(url, allow_redirects=True) but re-validates
    every hop against assert_public_url() — a URL that looks public can
    still 302 to an internal address, so allow_redirects=True alone
    isn't a safe way to follow untrusted URLs."""
    current_url = url
    for _ in range(max_redirects + 1):
        assert_public_url(current_url)
        response = session.get(
            current_url, headers=headers, timeout=timeout,
            allow_redirects=False, stream=True, verify=verify,
        )
        if response.is_redirect or response.is_permanent_redirect:
            location = response.headers.get("Location")
            response.close()
            if not location:
                return response
            current_url = urljoin(current_url, location)
            continue
        return response
    raise UnsafeURLError(f"Exceeded max redirects ({max_redirects}) for {url}")


def read_capped(response: requests.Response, max_bytes: int = DEFAULT_MAX_CONTENT_BYTES) -> bytes:
    declared = response.headers.get("Content-Length")
    if declared is not None:
        try:
            if int(declared) > max_bytes:
                raise ContentTooLarge(f"Declared Content-Length {declared} exceeds cap of {max_bytes}")
        except ValueError:
            pass
    chunks, total = [], 0
    for chunk in response.iter_content(chunk_size=65536):
        if not chunk:
            continue
        total += len(chunk)
        if total > max_bytes:
            raise ContentTooLarge(f"Response exceeded cap of {max_bytes} bytes while streaming")
        chunks.append(chunk)
    return b"".join(chunks)


def resilient_get(
    url: str,
    extra_headers: Optional[dict] = None,
    timeout: int = DEFAULT_TIMEOUT,
    max_retries: int = DEFAULT_MAX_RETRIES,
    backoff_factor: float = DEFAULT_BACKOFF_FACTOR,
    max_content_bytes: int = DEFAULT_MAX_CONTENT_BYTES,
):
    """
    Fetch `url`, retrying across a couple of user-agents and — as a
    last resort — with SSL verification relaxed. Never raises for
    ordinary network failures; returns them via `error` instead.

    Returns: (raw_bytes, response_headers, final_url, error)
        raw_bytes is None iff error is set.

    Raises UnsafeURLError immediately (not retried — it's a policy
    decision, not a transient failure) if the URL or any redirect hop
    resolves to a private/internal address.
    """
    assert_public_url(url)  # fail fast before burning any retries

    last_error = None
    for ua_index, ua in enumerate(FALLBACK_USER_AGENTS):
        headers = dict(DEFAULT_HEADERS, **(extra_headers or {}))
        headers["User-Agent"] = ua
        for verify_ssl in ((True,) if ua_index < len(FALLBACK_USER_AGENTS) - 1 else (True, False)):
            session = build_session(max_retries, backoff_factor)
            try:
                response = guarded_get(session, url, headers, timeout=timeout, verify=verify_ssl)
                if response.status_code >= 400:
                    last_error = f"HTTP {response.status_code}"
                    response.close()
                    continue
                try:
                    raw_bytes = read_capped(response, max_content_bytes)
                except ContentTooLarge as exc:
                    last_error = str(exc)
                    response.close()
                    continue
                result_headers = dict(response.headers)
                final_url = response.url
                response.close()
                return raw_bytes, result_headers, final_url, None
            except UnsafeURLError:
                raise
            except requests.RequestException as exc:
                last_error = str(exc)
                time.sleep(0.2)
                continue
            finally:
                session.close()

    logger.warning("resilient_get exhausted all attempts for %s: %s", url, last_error)
    return None, {}, url, last_error