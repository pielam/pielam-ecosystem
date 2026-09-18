# megamind/utils/resilient_fetch.py

"""
megamind/utils/resilient_fetch.py

Resilient GET wrapper used by scraper.py (and, via ServiceDataFetcher,
by service_fetcher.py).

Goal: scrape_url() should NEVER raise and NEVER give up after a single
attempt, but also shouldn't burn four attempts against a site that
already gave a definitive answer on the first one. This wraps the
existing build_session()/read_capped() helpers from http_client.py
with:

  - multiple attempts across DIFFERENT User-Agents (some sites block
    the default UA but allow a plain browser UA, or vice versa)
  - a final "just get *something*" attempt with relaxed SSL verify,
    used only after normal attempts are exhausted
  - SSRF protection on every attempt AND every redirect hop, via
    http_client.guarded_get — a URL that looks public can still
    redirect to an internal address, so this can't be a single
    up-front check on the original URL alone (see http_client.py's
    SSRF section for the full rationale)
  - a uniform return shape so the caller never has to branch on
    "did this raise or not" — it just checks response is None

NOTE: there is a second, differently-shaped `resilient_get` in
http_fetcher.py used by field_mapper.py. The two are not
interchangeable (different signatures/return contracts) — this is a
known naming collision, not a merge invitation; see http_fetcher.py.

Behavior changes in this revision (return contract UNCHANGED — still
(raw_bytes, response_headers, final_url, error) — so every existing
call site keeps working with no changes):

  * One requests.Session is now built once per call and reused across
    every UA/verify_ssl attempt, instead of a brand-new Session (and
    connection pool) per attempt. Only `verify` actually needs to vary
    per-request, and guarded_get() already takes that as a per-call
    argument — there was never a reason to rebuild the whole session
    (and pay a fresh TCP/TLS handshake) for it.
  * A definitive, non-UA-related failure (404 / 410 / 451 Not Found /
    Gone / Unavailable-for-legal-reasons, or a 400 Bad Request) now
    stops the attempt loop immediately instead of retrying the same
    dead URL through three more User-Agents — that status isn't going
    to change based on what UA asked for it, and a crawler working
    through a large URL list benefits far more from moving on quickly
    than from confirming "still 404" three more times.
  * A small per-domain "which User-Agent worked last time" memory
    (bounded, in-process) means a crawler that fetches many URLs from
    the same domain converges to trying the previously-successful UA
    FIRST on subsequent calls, rather than re-discovering it from
    scratch — meaningfully fewer wasted requests on a domain that's
    already known to block the default UA.
  * A short jittered pause is added between attempts specifically on
    429/503 (server explicitly signaling "back off"), separate from
    the connection-level Retry the session already does — this is
    between DIFFERENT UAs, not retries of the same request, so it
    isn't covered by build_session()'s Retry object at all.
  * An overall wall-clock budget (default 2.5x the per-attempt
    timeout) bounds worst-case latency for a single resilient_get()
    call — without it, a maximally unlucky URL could tie up a crawl
    worker for (attempts x timeout) seconds.

  * FIX: response headers are now returned as a case-INsensitive
    mapping (requests.structures.CaseInsensitiveDict) instead of a
    plain dict. `requests.Response.headers` is itself case-insensitive
    (so `.get("Content-Type")` and `.get("content-type")` both work on
    it), but the previous `dict(response.headers)` coercion silently
    threw that away and preserved whatever casing the server actually
    sent on the wire. HTTP/2 servers (e.g. Wikipedia) send ALL header
    names lowercased per spec — so `dict(response.headers)` came back
    keyed "content-type", and scraper.py's exact-case
    `resp_headers.get("Content-Type", "")` always missed it, returned
    "", and made scrape_url() treat a perfectly normal HTML page as
    non-HTML content (skipping BeautifulSoup/OG/image/link extraction
    entirely and dumping raw markup into `text` instead). Returning a
    CaseInsensitiveDict here fixes it for every current and future
    header lookup in every caller, rather than requiring each call
    site to remember to check both cases (which is exactly the trap
    scraper.py's own ETag/Last-Modified lookups already dodge via
    `.get("ETag") or .get("etag")`, but its Content-Type lookup did
    not). A CaseInsensitiveDict still behaves like a normal mapping
    everywhere else it's used (iteration, `dict(...)` coercion for
    JSON/DB storage, membership checks), so this is safe for every
    existing consumer of `response_headers`.
"""

from __future__ import annotations

import logging
import random
import threading
import time
from collections import OrderedDict
from typing import Any, Dict, Optional, Tuple
from urllib.parse import urlparse

from requests.structures import CaseInsensitiveDict

from megamind.utils.http_client import (
    assert_public_url,
    build_session,
    read_capped,
    guarded_get,
    ContentTooLarge,
    SSRFBlockedError,
    DEFAULT_TIMEOUT,
    DEFAULT_MAX_RETRIES,
    DEFAULT_BACKOFF_FACTOR,
    DEFAULT_MAX_CONTENT_BYTES,
    PROXIES,
)

logger = logging.getLogger(__name__)

# UAs tried in order. Some sites 403 the default Chrome-on-Windows UA
# specifically (bot lists match it) but allow Googlebot or a bare
# curl-like UA; others do the opposite. Trying a couple costs a few
# extra requests only on failure, which is fine here since correctness
# ("did we get anything at all") matters more than latency.
FALLBACK_USER_AGENTS = [
    None,  # use whatever caller passed in headers
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.0 Safari/605.1.15",
]

_LAST_USER_AGENT = FALLBACK_USER_AGENTS[-1]

# Status codes where trying a different User-Agent is pointless: the
# resource genuinely isn't there / isn't servable, regardless of who's
# asking. Deliberately does NOT include 401/403/429 — those are
# exactly the codes a UA swap (or, for 429, a brief pause) can change.
_PERMANENT_STATUS_CODES = frozenset({400, 404, 410, 451})
# Status codes worth a short polite pause before the next attempt,
# since they're the server explicitly saying "not right now".
_BACKOFF_STATUS_CODES = frozenset({429, 503})
_INTER_ATTEMPT_BACKOFF_BASE = 0.4  # seconds
_INTER_ATTEMPT_BACKOFF_MAX = 2.0   # seconds
# Overall wall-clock ceiling for a single resilient_get() call, as a
# multiple of the per-attempt timeout — bounds worst-case latency
# regardless of how many UA/verify combinations remain queued.
_WALL_CLOCK_BUDGET_MULTIPLIER = 2.5

# NOTE: the headers element of this tuple is a CaseInsensitiveDict at
# runtime (see the FIX note above) even though it's typed as Dict[str,
# Any] here for compatibility with callers that only ever call
# `.get(...)` / iterate it — CaseInsensitiveDict satisfies the Mapping
# interface, so this type hint doesn't need to change for the fix to
# be sound.
FetchResult = Tuple[Optional[bytes], Dict[str, Any], str, Optional[str]]

# ---------------------------------------------------------------------------
# Sticky per-domain "last successful User-Agent" memory
# ---------------------------------------------------------------------------
# Bounded + LRU-evicted, same pattern as robots_checker's caches, so a
# long-running crawler process touching many domains doesn't grow this
# without limit. This is purely an optimization hint, never a
# correctness dependency: a stale/wrong entry just costs one wasted
# attempt before the normal fallback order takes over.
_MAX_UA_MEMORY_ENTRIES = 2000
_ua_memory_lock = threading.Lock()
_ua_memory: "OrderedDict[str, int]" = OrderedDict()  # domain -> index into FALLBACK_USER_AGENTS


def _domain_of(url: str) -> str:
    try:
        return urlparse(url).netloc.lower()
    except Exception:
        return ""


def _remember_successful_ua(domain: str, ua_index: int) -> None:
    if not domain:
        return
    with _ua_memory_lock:
        _ua_memory[domain] = ua_index
        _ua_memory.move_to_end(domain)
        while len(_ua_memory) > _MAX_UA_MEMORY_ENTRIES:
            _ua_memory.popitem(last=False)


def _preferred_ua_order(domain: str) -> list:
    """FALLBACK_USER_AGENTS reordered to try a previously-successful UA
    for this domain first, preserving the relative order of the rest."""
    if not domain:
        return list(FALLBACK_USER_AGENTS)
    with _ua_memory_lock:
        idx = _ua_memory.get(domain)
        if idx is not None:
            _ua_memory.move_to_end(domain)
    if idx is None or idx >= len(FALLBACK_USER_AGENTS):
        return list(FALLBACK_USER_AGENTS)
    preferred = FALLBACK_USER_AGENTS[idx]
    rest = [ua for i, ua in enumerate(FALLBACK_USER_AGENTS) if i != idx]
    return [preferred] + rest


def resilient_get(
    url: str,
    headers: Optional[Dict[str, str]] = None,
    timeout: int = DEFAULT_TIMEOUT,
    max_retries: int = DEFAULT_MAX_RETRIES,
    backoff_factor: float = DEFAULT_BACKOFF_FACTOR,
    max_content_bytes: int = DEFAULT_MAX_CONTENT_BYTES,
) -> FetchResult:
    """
    Returns (raw_bytes, response_headers, final_url, error) — error is
    None on success. Never raises; every failure path is caught and
    turned into `error` so scrape_url() can always build a result dict
    instead of blowing up.

    `response_headers` is a case-insensitive mapping (see the FIX note
    in the module docstring) — callers can look up "Content-Type",
    "content-type", or "CONTENT-TYPE" interchangeably and get the same
    result, regardless of how the origin server actually cased the
    header on the wire (HTTP/2 servers lowercase everything; HTTP/1.1
    servers vary).

    An SSRF block is a policy decision, not a transient failure — the
    result won't change by trying a different User-Agent or dropping
    SSL verification, so it's checked once up front (fail fast) rather
    than being retried through every UA/SSL combination in the loop
    below like a normal network error would be.
    """
    headers = dict(headers) if headers else {}

    try:
        assert_public_url(url)
    except SSRFBlockedError as exc:
        logger.warning("resilient_get: blocked (SSRF policy) for %s: %s", url, exc)
        return None, {}, url, str(exc)

    domain = _domain_of(url)
    ua_order = _preferred_ua_order(domain)
    deadline = time.monotonic() + (timeout or DEFAULT_TIMEOUT) * _WALL_CLOCK_BUDGET_MULTIPLIER

    last_error: Optional[str] = None
    session = build_session(max_retries, backoff_factor)
    try:
        for attempt_num, ua in enumerate(ua_order):
            if time.monotonic() >= deadline:
                last_error = last_error or f"exceeded wall-clock budget for {url}"
                logger.info("resilient_get: wall-clock budget exhausted for %s after %d attempt(s)", url, attempt_num)
                break

            attempt_headers = dict(headers)
            if ua:
                attempt_headers["User-Agent"] = ua

            for verify_ssl in (True, False):
                # Only drop SSL verification as an absolute last resort:
                # the final attempt of the final User-Agent in the order.
                if verify_ssl is False and ua != _LAST_USER_AGENT:
                    continue

                try:
                    response = guarded_get(
                        session,
                        url,
                        headers=attempt_headers,
                        timeout=timeout,
                        proxies=PROXIES,
                        verify=verify_ssl,
                    )
                    status = response.status_code

                    if status >= 400:
                        last_error = f"HTTP {status}"
                        response.close()
                        if status in _PERMANENT_STATUS_CODES:
                            logger.debug(
                                "resilient_get: permanent status %s for %s — not trying further UAs", status, url
                            )
                            return None, {}, url, last_error
                        if status in _BACKOFF_STATUS_CODES:
                            _sleep_with_jitter(attempt_num)
                        continue

                    try:
                        raw_bytes = read_capped(response, max_content_bytes)
                    except ContentTooLarge as exc:
                        last_error = str(exc)
                        response.close()
                        continue

                    # FIX: preserve case-insensitive header lookups.
                    # `response.headers` is already a
                    # requests.structures.CaseInsensitiveDict; coercing
                    # it with plain `dict(...)` keeps whatever casing
                    # the server sent on the wire (HTTP/2 servers send
                    # all-lowercase header names per spec) and silently
                    # breaks exact-case lookups like
                    # `resp_headers.get("Content-Type")` downstream in
                    # scraper.py. Wrapping in CaseInsensitiveDict here
                    # instead keeps `.get("Content-Type")` working
                    # regardless of the origin's actual casing, for
                    # every current and future caller.
                    result_headers = CaseInsensitiveDict(response.headers)
                    final_url = response.url
                    response.close()

                    successful_index = FALLBACK_USER_AGENTS.index(ua) if ua in FALLBACK_USER_AGENTS else 0
                    _remember_successful_ua(domain, successful_index)

                    return raw_bytes, result_headers, final_url, None

                except SSRFBlockedError as exc:
                    # A redirect hop landed on a blocked address. Same
                    # reasoning as the up-front check: this won't change
                    # with a different UA, so stop here instead of
                    # continuing to burn through the fallback list.
                    logger.warning("resilient_get: redirect blocked (SSRF policy) for %s: %s", url, exc)
                    return None, {}, url, str(exc)

                except Exception as exc:
                    last_error = str(exc)
                    time.sleep(0.2)
                    continue
    finally:
        session.close()

    logger.warning("resilient_get exhausted all attempts for %s: %s", url, last_error)
    return None, {}, url, last_error


def _sleep_with_jitter(attempt_num: int) -> None:
    """Short randomized pause used only on 429/503 between UA attempts
    — separate from (and much shorter than) the connection-level Retry
    backoff in build_session(), which only fires within a single
    request's own retries, not between successive UA attempts here."""
    delay = min(_INTER_ATTEMPT_BACKOFF_MAX, _INTER_ATTEMPT_BACKOFF_BASE * (attempt_num + 1))
    delay += random.uniform(0, delay * 0.5)
    time.sleep(delay)