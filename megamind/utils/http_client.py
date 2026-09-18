# megamind/utils/http_client.py

"""
Shared HTTP client layer for outbound scraping/fetching.

Every module in this package that makes an outbound request to a
user-supplied or scraped third-party URL — service_fetcher.py,
resilient_fetch.py, robots_checker.py, video_info.py's redirect
resolution, and megamind/services/scraper.py — builds on this module
so that retry/backoff, connection pooling, byte caps, and (crucially)
SSRF protection behave identically everywhere, rather than drifting
across copy-pasted Session/HTTPAdapter setups.

Public API (stable — imported by name elsewhere in this codebase,
keep signatures/behavior backward compatible when touching this file):
    build_session, guarded_get, read_capped, decode_text,
    assert_public_url, ContentTooLarge, SSRFBlockedError,
    DEFAULT_TIMEOUT, DEFAULT_MAX_RETRIES, DEFAULT_BACKOFF_FACTOR,
    DEFAULT_MAX_CONTENT_BYTES, DEFAULT_USER_AGENT

Additions in this revision (all additive — nothing above changed shape):
    guarded_head          cheap HEAD probe (Content-Type/Length) under
                           the same SSRF guard, before committing to a
                           full GET — useful for media/video size checks.
    iter_capped            streaming generator twin of read_capped, for
                           callers that want to process large bodies
                           (video/image probes) without buffering the
                           whole thing in memory first.
    stream_response         context manager that guarantees .close() on
                           a guarded_get() response even if the caller
                           raises partway through.
    HttpClientError         common base for this module's exceptions, so
                           callers can `except HttpClientError:` once
                           instead of listing ContentTooLarge/SSRFBlockedError.
    on_response hook        optional callback threaded through guarded_get
                           for lightweight observability (status, elapsed,
                           bytes) without every caller wiring its own timing.

Config (all optional, read from Django settings):
    SCRAPER_TIMEOUT / _MAX_RETRIES / _BACKOFF_FACTOR / _PROXY
    SCRAPER_MAX_CONTENT_BYTES
    SCRAPER_POOL_CONNECTIONS (default 20) / _POOL_MAXSIZE (default 50)
    SCRAPER_SSRF_PROTECTION_ENABLED (default True)
    SCRAPER_SSRF_ALLOWED_HOSTS (default [])
    SCRAPER_MAX_REDIRECTS (default 5)
Callers can layer their own prefix on top of these same defaults
(e.g. service_fetcher.py reads SERVICE_FETCH_* first, falling back to
these) the same way video_info.py layers FACEBOOK_RESOLVE_* etc. over
VIDEO_RESOLVE_*.

A note on DNS rebinding (read before "fixing" this with a DNS cache):
assert_public_url() re-resolves and re-validates the hostname on
every single hop inside guarded_get(), specifically so a URL that
looks public on the first check can't sneak past on a later redirect.
There remains an inherent, small TOCTOU window between that check and
the socket connect() a couple of lines later, which urllib3 performs
via its own independent DNS resolution — an attacker controlling DNS
for the target host with a very short TTL could theoretically swap
the answer in that window. Caching the resolved address to "save a
lookup" would not close this window, it would only make it wider and
turn it into a *standing* bypass (the whole point of the check is
that it happens right before each connect). The robust fix is
connection-level IP pinning with the TLS SNI/hostname verification
kept separate from the socket target; that is meaningfully more
complex and risks silently weakening certificate verification if
done carelessly, so it is intentionally not attempted with a quick
patch here. If your threat model needs to close this residual window
completely, do it with network-level egress controls (an allowlisting
egress proxy, or iptables/eBPF rules that block RFC1918/link-local
destinations at the OS level) as defense-in-depth alongside this
application-level check, not instead of it.
"""

from __future__ import annotations

import contextlib
import ipaddress
import logging
import socket
import time
from typing import Callable, Dict, Iterator, Optional, Union
from urllib.parse import urljoin, urlparse

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from django.conf import settings

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT           = getattr(settings, 'SCRAPER_TIMEOUT', 12)
DEFAULT_MAX_RETRIES        = getattr(settings, 'SCRAPER_MAX_RETRIES', 2)
DEFAULT_BACKOFF_FACTOR     = getattr(settings, 'SCRAPER_BACKOFF_FACTOR', 0.5)
DEFAULT_MAX_CONTENT_BYTES  = getattr(settings, 'SCRAPER_MAX_CONTENT_BYTES', 15 * 1024 * 1024)  # 15 MB
DEFAULT_POOL_CONNECTIONS  = getattr(settings, 'SCRAPER_POOL_CONNECTIONS', 20)
DEFAULT_POOL_MAXSIZE      = getattr(settings, 'SCRAPER_POOL_MAXSIZE', 50)
# Cap how long a single Retry-After-driven wait can be — a malicious
# or misconfigured upstream sending `Retry-After: 999999` shouldn't
# be able to make a crawl worker sleep for hours.
MAX_RETRY_AFTER_SECONDS   = getattr(settings, 'SCRAPER_MAX_RETRY_AFTER_SECONDS', 30)

DEFAULT_USER_AGENT = (
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
    '(KHTML, like Gecko) Chrome/124.0 Safari/537.36'
)

# Optional, faster/more-accurate charset sniffing for decode_text().
# requests already vendors charset_normalizer as a transitive dep in
# modern versions, so this is usually free; degrade gracefully if not.
try:
    from charset_normalizer import from_bytes as _cn_from_bytes
except ImportError:  # pragma: no cover - optional dependency
    _cn_from_bytes = None


class HttpClientError(Exception):
    """Common base for this module's exceptions. Prefer catching this
    over listing every subclass when the caller just wants "something
    went wrong in the guarded fetch layer"."""
    pass


class ContentTooLarge(HttpClientError):
    """Raised when a response body exceeds the configured byte cap."""
    pass


class SSRFBlockedError(HttpClientError):
    """Raised when a URL (or a redirect target) is not safe to fetch
    server-side and SSRF protection is enabled."""
    pass


def _normalize_proxy(value: Union[str, dict, None]) -> Optional[Dict[str, str]]:
    if not value:
        return None
    if isinstance(value, str):
        return {'http': value, 'https': value}
    if isinstance(value, dict):
        return value
    return None


PROXIES = _normalize_proxy(getattr(settings, 'SCRAPER_PROXY', None))


def build_session(max_retries: int = DEFAULT_MAX_RETRIES,
                   backoff_factor: float = DEFAULT_BACKOFF_FACTOR,
                   pool_connections: int = DEFAULT_POOL_CONNECTIONS,
                   pool_maxsize: int = DEFAULT_POOL_MAXSIZE) -> requests.Session:
    """
    requests.Session with retry/backoff on connection errors and
    429/5xx responses, and a connection pool sized for crawler-style
    fan-out (many hosts, several requests each) rather than the
    default of 10/10 which starts recycling connections quickly under
    concurrent use.

    Only GET/HEAD are retried — every caller of this helper only ever
    reads third-party pages, never posts to them, so retrying is safe
    (idempotent) by construction.
    """
    session = requests.Session()
    retry_kwargs = dict(
        total=max_retries,
        connect=max_retries,
        read=max_retries,
        backoff_factor=backoff_factor,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset(['GET', 'HEAD']),
        raise_on_status=False,
        respect_retry_after_header=True,
    )
    try:
        # backoff_jitter is only available on urllib3>=1.26.9; spreads
        # retries out so a thundering herd of workers all hitting the
        # same flaky upstream don't all retry in lockstep.
        retry = Retry(backoff_jitter=0.3, **retry_kwargs)
    except TypeError:
        retry = Retry(**retry_kwargs)
    adapter = HTTPAdapter(
        max_retries=retry,
        pool_connections=pool_connections,
        pool_maxsize=pool_maxsize,
    )
    session.mount('https://', adapter)
    session.mount('http://', adapter)
    return session


# ---------------------------------------------------------------------------
# SSRF protection
# ---------------------------------------------------------------------------
#
# Every caller of this module fetches a URL an end user supplied (a
# "connected service" URL, or a src/href found while scraping one of
# those pages). Without a check like this, a user could point a
# "connected service" at http://169.254.169.254/latest/meta-data/
# (the AWS/GCP/Azure cloud metadata endpoint — a link-local address),
# at http://localhost:6379/ (an internal Redis/admin port), or at any
# other RFC1918/loopback address, and this server would fetch it with
# its own network access and hand the response back to that user.
#
# A check on the *original* URL alone isn't sufficient: requests'
# built-in allow_redirects=True follows redirects transparently, so a
# URL that looks entirely public (passes the check) could still 302
# to an internal target. guarded_get() below re-validates on every
# redirect hop, not just the first request.

SSRF_PROTECTION_ENABLED = getattr(settings, 'SCRAPER_SSRF_PROTECTION_ENABLED', True)
# Hostnames explicitly exempted from the resolved-IP check (e.g. an
# internal service intentionally added as a connected service in a
# staging environment). Empty by default — opt in per-deployment.
SSRF_ALLOWED_HOSTS = {h.lower() for h in getattr(settings, 'SCRAPER_SSRF_ALLOWED_HOSTS', [])}
MAX_REDIRECTS = getattr(settings, 'SCRAPER_MAX_REDIRECTS', 5)


def _is_public_ip(ip_str: str) -> bool:
    try:
        ip = ipaddress.ip_address(ip_str)
    except ValueError:
        return False
    return not (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local       # covers 169.254.169.254, the cloud metadata endpoint
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )


def assert_public_url(url: str) -> None:
    """
    Raises SSRFBlockedError if *url* is not safe to fetch server-side:
    a non-http(s) scheme, no hostname, an unresolvable hostname, or a
    hostname that resolves (via ANY of its A/AAAA records) to a
    private/loopback/link-local/reserved address.

    Checking every resolved address (not just the first) matters:
    a multi-answer DNS record could otherwise put a public IP in
    front of the check and a private one behind it for the actual
    connection. Note this check is deliberately NOT cached — see the
    module docstring's "DNS rebinding" note for why caching here
    would widen, not close, the TOCTOU window this function exists to
    minimize.

    Also rejects hostnames that are bare numeric-IP encodings (e.g.
    "2130706433" or "0x7f.0.0.1", both alternate spellings of
    127.0.0.1 that some HTTP clients' host parsers accept) — these
    are caught for free here because getaddrinfo() resolves them to a
    concrete IP before _is_public_ip() ever runs, so no special-casing
    is needed, but it's worth knowing this class of bypass is covered.
    """
    if not SSRF_PROTECTION_ENABLED:
        return

    if not url or not isinstance(url, str):
        raise SSRFBlockedError("URL is empty or not a string")

    parsed = urlparse(url)
    if parsed.scheme not in ('http', 'https'):
        raise SSRFBlockedError(f"Unsupported URL scheme: {parsed.scheme!r}")

    try:
        hostname = parsed.hostname
    except ValueError as exc:
        # urlparse can raise on a malformed netloc (e.g. bad IPv6
        # literal) rather than just returning None — fail closed.
        raise SSRFBlockedError(f"Could not parse hostname from URL: {exc}")

    if not hostname:
        raise SSRFBlockedError("URL has no hostname")

    if hostname.lower() in SSRF_ALLOWED_HOSTS:
        return

    try:
        addrinfo = socket.getaddrinfo(hostname, None)
    except (socket.gaierror, UnicodeError) as exc:
        # UnicodeError: getaddrinfo raises this (not just gaierror) for
        # a hostname that fails IDNA encoding — a real case (malformed
        # punycode in a scraped href), not hypothetical. Either way,
        # "can't resolve it" must fail closed here, the same as an
        # explicit gaierror.
        raise SSRFBlockedError(f"Could not resolve host {hostname!r}: {exc}")
    except Exception as exc:
        # This function's entire purpose is to be a safety gate before
        # a network fetch — any unexpected failure resolving the host
        # must block the request rather than let it through unchecked.
        logger.warning("assert_public_url: unexpected DNS resolution error for %r: %s", hostname, exc)
        raise SSRFBlockedError(f"Could not resolve host {hostname!r}: {exc}")

    resolved_ips = {info[4][0] for info in addrinfo}
    if not resolved_ips:
        raise SSRFBlockedError(f"Host {hostname!r} did not resolve to any address")

    for ip_str in resolved_ips:
        if not _is_public_ip(ip_str):
            raise SSRFBlockedError(
                f"Host {hostname!r} resolves to non-public address {ip_str} — blocked"
            )


OnResponseHook = Callable[[str, Optional[int], float], None]


def _do_guarded_request(
    method: str,
    session: requests.Session,
    url: str,
    headers: Optional[Dict[str, str]],
    timeout: int,
    proxies: Optional[Dict[str, str]],
    verify: bool,
    max_redirects: int,
    on_response: Optional[OnResponseHook],
) -> requests.Response:
    """Shared redirect-walking core for guarded_get()/guarded_head()."""
    current_url = url
    for _ in range(max_redirects + 1):
        assert_public_url(current_url)
        started = time.monotonic()
        response = session.request(
            method,
            current_url,
            headers=headers,
            timeout=timeout,
            allow_redirects=False,
            stream=True,
            proxies=proxies,
            verify=verify,
        )
        elapsed = time.monotonic() - started
        if on_response is not None:
            try:
                on_response(current_url, response.status_code, elapsed)
            except Exception:
                # Observability must never break the actual fetch.
                logger.debug("on_response hook raised", exc_info=True)

        if response.is_redirect or response.is_permanent_redirect:
            location = response.headers.get('Location')
            response.close()
            if not location:
                return response
            current_url = urljoin(current_url, location)
            continue
        return response

    raise SSRFBlockedError(f"Exceeded max redirects ({max_redirects}) for {url}")


def guarded_get(
    session: requests.Session,
    url: str,
    headers: Optional[Dict[str, str]] = None,
    timeout: int = DEFAULT_TIMEOUT,
    proxies: Optional[Dict[str, str]] = PROXIES,
    verify: bool = True,
    max_redirects: int = MAX_REDIRECTS,
    on_response: Optional[OnResponseHook] = None,
) -> requests.Response:
    """
    Drop-in replacement for session.get(url, allow_redirects=True,
    stream=True) that validates every hop — the original URL AND
    every redirect target — against assert_public_url() before it's
    requested.

    This is the only entry point callers in this codebase should use
    to fetch a user-supplied URL. Using session.get(allow_redirects=True)
    directly (even after checking the original URL) is not safe: a
    URL that passes the check can still redirect to a blocked target,
    and requests would follow it transparently.

    `on_response`, if given, is called after every hop (including
    redirects) as on_response(url, status_code, elapsed_seconds) —
    useful for lightweight per-request metrics/logging without every
    caller re-timing its own requests. Exceptions from the hook are
    swallowed (logged at debug) so a bad metrics callback can never
    break a crawl.
    """
    return _do_guarded_request(
        'GET', session, url, headers, timeout, proxies, verify, max_redirects, on_response
    )


def guarded_head(
    session: requests.Session,
    url: str,
    headers: Optional[Dict[str, str]] = None,
    timeout: int = DEFAULT_TIMEOUT,
    proxies: Optional[Dict[str, str]] = PROXIES,
    verify: bool = True,
    max_redirects: int = MAX_REDIRECTS,
    on_response: Optional[OnResponseHook] = None,
) -> requests.Response:
    """
    Same guarantees as guarded_get() but sends HEAD instead of GET —
    for callers (video_info.py, media_info.py, image_analyzer.py) that
    only need Content-Type/Content-Length to decide whether a full GET
    is even worth issuing, e.g. to skip a 400MB video file before
    downloading a single byte of it.

    Not every server implements HEAD correctly (some 405, some return
    stale/absent Content-Length) — callers should treat a missing or
    untrustworthy header as "unknown" and fall back to guarded_get()
    with read_capped()/iter_capped() rather than trusting HEAD blindly.
    """
    return _do_guarded_request(
        'HEAD', session, url, headers, timeout, proxies, verify, max_redirects, on_response
    )


@contextlib.contextmanager
def stream_response(response: requests.Response) -> Iterator[requests.Response]:
    """
    Context manager guaranteeing response.close() runs even if the
    caller raises while consuming a streamed guarded_get()/guarded_head()
    response — e.g. read_capped() raising ContentTooLarge partway
    through, or a caller-side parsing error. Leaving a streamed
    response un-closed leaks the underlying connection back to the
    pool in a bad state under exactly the failure conditions this
    module exists to guard against.

        with stream_response(guarded_get(session, url)) as resp:
            data = read_capped(resp)
    """
    try:
        yield response
    finally:
        response.close()


def read_capped(response: requests.Response, max_bytes: int = DEFAULT_MAX_CONTENT_BYTES) -> bytes:
    """
    Read a streamed response body up to max_bytes.

    Protects against a third-party URL — arbitrary user input, either
    a "connected service" URL or a src attribute found while scraping
    a page — serving an enormous or effectively infinite response
    body, which `response.content` would otherwise read entirely into
    memory with no limit.

    Checks Content-Length up front when present (cheap, avoids
    reading anything for the common oversized-file case), then
    enforces the same cap while streaming in case the header is
    missing, wrong, or the server uses chunked transfer with no
    declared length.
    """
    declared = response.headers.get('Content-Length')
    if declared is not None:
        try:
            if int(declared) > max_bytes:
                raise ContentTooLarge(
                    f"Declared Content-Length {declared} exceeds cap of {max_bytes} bytes"
                )
        except ValueError:
            pass  # non-numeric header — fall through to the streaming cap

    chunks = []
    total = 0
    for chunk in response.iter_content(chunk_size=65536):
        if not chunk:
            continue
        total += len(chunk)
        if total > max_bytes:
            raise ContentTooLarge(f"Response exceeded cap of {max_bytes} bytes while streaming")
        chunks.append(chunk)
    return b''.join(chunks)


def iter_capped(response: requests.Response,
                 max_bytes: int = DEFAULT_MAX_CONTENT_BYTES,
                 chunk_size: int = 65536) -> Iterator[bytes]:
    """
    Streaming twin of read_capped(): yields chunks instead of
    buffering the whole body, for callers that can process a response
    incrementally (e.g. sniffing the first few KB of a video/image
    for magic bytes/dimensions without pulling the whole file into
    memory). Still enforces the same Content-Length pre-check and
    running-total cap as read_capped(); raises ContentTooLarge exactly
    the same way if the cap is exceeded mid-stream.
    """
    declared = response.headers.get('Content-Length')
    if declared is not None:
        try:
            if int(declared) > max_bytes:
                raise ContentTooLarge(
                    f"Declared Content-Length {declared} exceeds cap of {max_bytes} bytes"
                )
        except ValueError:
            pass

    total = 0
    for chunk in response.iter_content(chunk_size=chunk_size):
        if not chunk:
            continue
        total += len(chunk)
        if total > max_bytes:
            raise ContentTooLarge(f"Response exceeded cap of {max_bytes} bytes while streaming")
        yield chunk


def decode_text(content: bytes, declared_encoding: Optional[str] = None) -> str:
    """
    Decode a capped response body to text.

    Order of attempts:
      1. The encoding the server declared (or that requests inferred
         from headers), if given.
      2. charset_normalizer's statistical detection, if the package is
         available — noticeably more accurate than a blind utf-8
         attempt for pages with missing/wrong charset headers (common
         on scraped third-party sites), and cheap since requests
         already depends on it transitively in modern versions.
      3. utf-8 with lossy replacement, which always succeeds — a
         scraped third-party page having a wrong/missing/undetectable
         charset shouldn't take down the whole fetch.
    """
    if declared_encoding:
        try:
            return content.decode(declared_encoding)
        except (LookupError, UnicodeDecodeError):
            pass

    if _cn_from_bytes is not None and content:
        try:
            best = _cn_from_bytes(content).best()
            if best is not None:
                return str(best)
        except Exception:
            # Detection is a best-effort improvement, never load-bearing.
            logger.debug("charset_normalizer detection failed", exc_info=True)

    return content.decode('utf-8', errors='replace')