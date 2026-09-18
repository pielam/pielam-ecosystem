# megamind/utils/browser_render.py

"""
browser_render.py

Headless-browser fallback for JS-rendered (SPA) pages — the plain-HTTP
fetch in http_fetcher.py only sees server-rendered HTML, so a React/Vue
site that's an empty shell until JS runs comes back with almost no
text/links. This is only invoked when that thin-content signature is
detected (see should_attempt_render) — a real browser is far slower
than a plain request, so it's the last resort, not the default path.

Optional dependency. If playwright isn't installed, is_playwright_installed()
returns False and render_with_browser() no-ops (returns None) — callers
should treat that as "stick with the plain-HTTP result", not an error.

SSRF: render_with_browser() re-validates the target host itself rather
than trusting the caller to have done it. This matters more here than
for a plain HTTP fetch: a real browser doesn't just GET one URL, it
follows every redirect *and* runs the page's own JS, which can issue
arbitrary fetch()/XHR/WebSocket calls of its own. A URL that was safe
when the caller's plain-HTTP pass validated it can still lead to an
internal address once a real browser is let loose on it — via a
redirect the plain pass's own client didn't validate, or via
JS-initiated requests the caller never sees at all. Every request the
page makes (navigation, redirects, and script-initiated requests
alike) is checked through Playwright's request routing before being
allowed to proceed, not just the initial URL.

NOTE: megamind/utils/playwright_fallback.py duplicates most of this
module's structure (same is_playwright_installed/render_with_browser
shape) but WITHOUT the SSRF re-validation this module does. This
module is the one that should be used wherever a real browser touches
a URL that isn't already fully trusted; the duplication itself is
worth resolving (e.g. by having playwright_fallback.py delegate here)
but is left as-is in this pass since collapsing them changes which
module callers import from.

Setup:
    pip install playwright --break-system-packages
    playwright install --with-deps chromium

FIX LOG (this revision):
  * should_attempt_render() now also accepts the plain-HTTP pass's
    extracted `images` list and treats a page with zero images as a
    render-worthy "thin" signal on its own, independent of text/link
    length. The original text/links-only heuristic missed an entire
    class of JS-rendered page: sites (manga/comic readers, galleries,
    infinite-scroll feeds) that server-render plenty of surrounding
    text and navigation links but inject the page's actual images
    client-side after load. Such a page never looked "thin" by the
    old text/link check, so the browser fallback never fired and the
    scrape came back with real surrounding content but none of the
    images that were the entire point of the page. See scraper.py's
    call site, which now passes `result["images"]` through.
"""

from __future__ import annotations

import ipaddress
import logging
import socket
from typing import Any, List, Optional, Tuple
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

THIN_TEXT_THRESHOLD = 200  # below this many chars, suspect a JS shell
RENDER_TIMEOUT_MS = 15_000
RENDER_WAIT_UNTIL = "networkidle"

# FIX: many lazy-loaded image galleries (manga/comic readers especially)
# don't set a real <img src> until that image scrolls into the viewport,
# via an IntersectionObserver. A headless browser that never scrolls
# only ever "sees" whatever was already near the top of the page at
# load time — so even after should_attempt_render() correctly triggers
# a render, page.content() can still come back with most/all image
# placeholders unresolved. _auto_scroll() walks the page down in
# bounded increments after navigation, pausing briefly at each step so
# each newly-visible image's observer callback has a chance to fire,
# before content() is captured. Capped by both a max step count and a
# max total scroll time so a page with infinite-scroll pagination (which
# would otherwise grow document.body.scrollHeight forever) can't turn
# this into an unbounded loop.
AUTO_SCROLL_MAX_STEPS = 40
AUTO_SCROLL_STEP_PAUSE_MS = 250
AUTO_SCROLL_MAX_DURATION_MS = 8_000

# FIX: some JS-rendered pages are the opposite of an "empty SPA shell" —
# they server-render plenty of surrounding text/links/nav (breadcrumbs,
# related-content cards, footer boilerplate) but leave the page's ONE
# actual content payload — its images — to be injected by client-side
# JS after load (common on manga/comic reader sites, image galleries,
# infinite-scroll feeds). The original text/links-only heuristic never
# caught this, since those pages don't look "thin" by word count or
# link count at all. A page with zero extracted images is a strong
# enough universal signal on its own to warrant a browser render,
# regardless of how much surrounding text/links it has.
MIN_IMAGES_THRESHOLD = 1

_BLOCKED_RESOURCE_TYPES = frozenset({"image", "media", "font"})

_playwright_available: Optional[bool] = None


def is_playwright_installed() -> bool:
    global _playwright_available
    if _playwright_available is None:
        try:
            import playwright.sync_api  # noqa: F401
            _playwright_available = True
        except ImportError:
            _playwright_available = False
            logger.info(
                "browser_render: playwright not installed — JS-rendered "
                "pages will fall back to the plain-HTML result only. "
                "Install with: pip install playwright && playwright install chromium"
            )
    return _playwright_available


def should_attempt_render(
    text: Optional[str],
    links: Optional[List[Any]],
    images: Optional[List[Any]] = None,
) -> bool:
    """
    Heuristic: worth a real browser render if the plain-HTTP pass shows
    either classic signature of JS-dependent content:

      1. An empty-SPA-shell page — thin on BOTH text and links (the
         original signature: a React/Vue app that hasn't hydrated).
      2. A zero-image page — `images` was passed and came back empty.
         This catches the opposite failure mode: a page that already
         has substantial server-rendered text/links (so signature #1
         never fires) but whose actual image content — manga/comic
         reader pages, galleries, infinite-scroll feeds — is injected
         entirely by client-side JS after load. `images` is optional
         and defaults to None so existing callers that don't pass it
         keep the original text/links-only behavior unchanged.

    Either signature alone is enough to trigger a render attempt —
    render_with_browser() is still a no-op if playwright isn't
    installed, so this heuristic firing more often costs nothing on a
    deployment without the optional dependency.
    """
    thin_shell = len(text or "") < THIN_TEXT_THRESHOLD and not links
    if thin_shell:
        return True
    if images is not None and len(images) < MIN_IMAGES_THRESHOLD:
        return True
    return False


def _is_public_host(hostname: Optional[str]) -> bool:
    """Same private/loopback/link-local/reserved check used across this
    codebase's other fetchers (see connected_service.validate_crawl_url,
    bind_content._assert_public_url) — kept as a self-contained copy
    here rather than importing the Django model version, since this
    module is meant to stay usable without a Django app loaded."""
    if not hostname or hostname.lower() in ("localhost", "0.0.0.0"):
        return False
    try:
        infos = socket.getaddrinfo(hostname, None)
    except (socket.gaierror, UnicodeError):
        # UnicodeError: getaddrinfo can raise this (not just gaierror)
        # for a hostname that fails IDNA encoding — a real case, not
        # hypothetical. This function is a safety predicate, so any
        # unexpected failure here must fail closed (unsafe) rather than
        # propagate and potentially violate render_with_browser's
        # documented "never raises" contract.
        return False
    except Exception as exc:
        logger.debug("browser_render: getaddrinfo failed unexpectedly for %s: %s", hostname, exc)
        return False
    for _, _, _, _, sockaddr in infos:
        try:
            ip = ipaddress.ip_address(sockaddr[0])
        except ValueError:
            continue
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
            return False
    return True


def _request_is_safe(request_url: str) -> bool:
    parsed = urlparse(request_url)
    if parsed.scheme not in ("http", "https"):
        # data:/blob:/about:blank/chrome-error:// etc. never leave the
        # machine — nothing network-facing to check.
        return True
    return _is_public_host(parsed.hostname)


def _make_request_router(logger_context_url: str):
    """Builds the Playwright route handler: blocks image/media/font
    fetches (this is extraction, not real browsing — cut render time by
    not loading them), and blocks any request — navigation, redirect,
    or JS-initiated fetch/XHR alike — whose host isn't public. Registered
    on the browser *context* (see render_with_browser), not just the one
    page, so it also covers any popup/new-tab the rendered page's JS
    opens via window.open() — a page-level route wouldn't apply to those,
    since they're separate Page objects within the same context.

    NOTE: blocking the "image" resource type here only stops the actual
    image BYTES from being downloaded — it does not stop a page's JS
    from setting a real image URL into an <img src="..."> (or data-src,
    etc.) attribute in the DOM. render_with_browser() returns page
    content() (the DOM's HTML), not rendered pixels, and that DOM still
    carries whatever real src URLs the page's JS assigned — which is
    exactly what scraper.py's own _extract_images() re-parses afterward.
    So blocking image fetches here is a pure speed optimization and
    does not interfere with the should_attempt_render() image-count fix
    above; it only prevents downloading bytes nothing in this pipeline
    needs.
    """
    def _handler(route: Any) -> None:
        request = route.request
        if request.resource_type in _BLOCKED_RESOURCE_TYPES:
            route.abort()
            return
        try:
            safe = _request_is_safe(request.url)
        except Exception as exc:
            logger.debug("browser_render: _request_is_safe raised for %s: %s", request.url, exc)
            safe = False
        if not safe:
            logger.warning(
                "browser_render: blocked unsafe request to %s while rendering %s",
                request.url, logger_context_url,
            )
            route.abort()
            return
        route.continue_()
    return _handler


def _auto_scroll(page: Any, url: str) -> None:
    """
    Walks the page down in bounded increments so lazy-loaded content
    gated behind an IntersectionObserver (the common pattern for manga/
    comic readers and image galleries — a real <img src> is only set
    once that image enters the viewport) actually gets a chance to
    resolve before the caller captures page.content(). Best-effort:
    any failure here is logged and swallowed, never raised — a scroll
    problem should degrade to "whatever the page had at load time",
    the same graceful degradation this whole module already applies to
    navigation failures.

    Bounded on two axes independently (step count AND wall-clock time)
    so a page with genuine infinite-scroll pagination — where
    document.body.scrollHeight keeps growing forever — can't turn this
    into an unbounded loop; either cap stops it.
    """
    import time as _time

    started = _time.monotonic()
    try:
        previous_height = 0
        for _ in range(AUTO_SCROLL_MAX_STEPS):
            if (_time.monotonic() - started) * 1000 >= AUTO_SCROLL_MAX_DURATION_MS:
                break
            current_height = page.evaluate("document.body ? document.body.scrollHeight : 0")
            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            page.wait_for_timeout(AUTO_SCROLL_STEP_PAUSE_MS)
            if current_height <= previous_height:
                # Scroll height stopped growing — either everything
                # lazy-loadable has already loaded, or there's nothing
                # left to reveal. Either way, further scrolling won't
                # help.
                break
            previous_height = current_height
        # Scroll back to top: some sites' lazy-load JS only arms the
        # observer for images near the current viewport on first paint,
        # and a couple of these reader themes re-render the top of the
        # DOM once the bottom sentinel is reached — leaving the scroll
        # position at the bottom is occasionally why a subsequent
        # content() read misses the top images. Cheap and harmless
        # either way.
        page.evaluate("window.scrollTo(0, 0)")
        page.wait_for_timeout(AUTO_SCROLL_STEP_PAUSE_MS)
    except Exception as exc:
        logger.debug("browser_render: auto-scroll failed for %s: %s", url, exc)


def render_with_browser(
    url: str,
    user_agent: str,
    timeout_ms: int = RENDER_TIMEOUT_MS,
) -> Optional[Tuple[str, str]]:
    """
    Loads `url` in headless Chromium and returns (html, final_url), or
    None if rendering failed, isn't available, or `url` isn't a safe
    target — never raises, this is best-effort; failure just means
    "use the plain-HTTP result".
    """
    if not url or not isinstance(url, str):
        logger.warning("browser_render: refusing to render invalid url %r", url)
        return None

    if not is_playwright_installed():
        return None

    if not _request_is_safe(url):
        logger.warning("browser_render: refusing to render unsafe URL %s", url)
        return None

    from playwright.sync_api import sync_playwright, TimeoutError as PWTimeoutError

    browser = None
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
                args=["--disable-gpu", "--no-sandbox", "--disable-dev-shm-usage"],
            )
            context = browser.new_context(user_agent=user_agent)
            try:
                # Routed on the context, not the page, so a popup/new-tab
                # opened by the rendered page's own JS is covered too —
                # see _make_request_router's docstring.
                context.route("**/*", _make_request_router(url))
                page = context.new_page()
                try:
                    page.goto(url, timeout=timeout_ms, wait_until=RENDER_WAIT_UNTIL)
                except PWTimeoutError:
                    logger.debug("browser_render: networkidle timeout for %s, using current DOM", url)
                except Exception as goto_exc:
                    # Navigation failures other than a timeout (bad
                    # cert, connection reset, blocked-by-router mid-
                    # redirect, etc.) — still try to read whatever the
                    # page currently holds instead of treating this as
                    # a hard failure.
                    logger.debug("browser_render: goto raised for %s: %s", url, goto_exc)

                # FIX: give viewport-triggered lazy-load images a chance
                # to resolve before reading content() — see _auto_scroll
                # docstring. Runs regardless of whether goto() timed out
                # above, since a networkidle timeout on a page with a
                # lazy-load reader is often itself caused by ongoing
                # image fetches that only start once we scroll.
                _auto_scroll(page, url)

                html = page.content()
                final_url = page.url
                return html, final_url
            finally:
                context.close()
    except Exception as exc:
        logger.warning("browser_render: render failed for %s: %s", url, exc)
        return None
    finally:
        if browser is not None:
            try:
                browser.close()
            except Exception:  # pragma: no cover - best-effort cleanup
                logger.debug("browser_render: browser.close() failed", exc_info=True)