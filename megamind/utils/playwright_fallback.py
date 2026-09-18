# megamind/utils/playwright_fallback.py
"""
Part 3 — headless-browser fallback for JS-rendered (SPA) pages.

requirements.txt:
    playwright>=1.40.0
Then once, at deploy/build time (not per-request):
    playwright install --with-deps chromium

Only invoked by scraper.py when the plain-HTTP fetch "succeeded" but
came back suspiciously thin (typical of a React/Vue shell that's empty
until JS runs). This is deliberately NOT the default path — a real
browser is 10-50x slower and heavier than requests+BeautifulSoup, so
it's reserved for pages that actually need it.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)

# Below this many characters of extracted text, we suspect the page is
# a JS shell rather than truly having a thin/short article.
THIN_TEXT_THRESHOLD = 200

RENDER_TIMEOUT_MS = 15_000
RENDER_WAIT_UNTIL = "networkidle"  # wait for JS-driven requests to settle

# Resource types that add render latency without helping text extraction.
_BLOCKED_RESOURCE_TYPES = frozenset({"image", "media", "font"})

_playwright_available: Optional[bool] = None  # cached after first import check


class RenderResult:
    """Outcome of a headless-browser render attempt."""

    __slots__ = ("html", "final_url")

    def __init__(self, html: str, final_url: str) -> None:
        self.html = html
        self.final_url = final_url

    def __iter__(self):
        # Preserves the old `html, final_url = render_with_browser(...)`
        # tuple-unpacking call sites while giving new callers named access.
        yield self.html
        yield self.final_url

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"RenderResult(final_url={self.final_url!r}, html_len={len(self.html)})"


def is_playwright_installed() -> bool:
    """Check (and cache) whether the playwright package is importable."""
    global _playwright_available
    if _playwright_available is None:
        try:
            import playwright.sync_api  # noqa: F401
            _playwright_available = True
        except ImportError:
            _playwright_available = False
            logger.warning(
                "playwright_fallback: playwright not installed — "
                "JS-rendered pages will fall back to plain-HTML results only."
            )
    return _playwright_available


def render_with_browser(
    url: str,
    user_agent: str,
    timeout_ms: int = RENDER_TIMEOUT_MS,
) -> Optional[RenderResult]:
    """
    Loads *url* in headless Chromium and returns fully-rendered HTML
    (post-JS-execution), or None if rendering failed for any reason.

    Never raises — this is a best-effort fallback, so any failure here
    should just mean "stick with the plain-HTTP result", not crash the
    caller.
    """
    if not url or not isinstance(url, str):
        logger.warning("playwright_fallback: refusing to render invalid url %r", url)
        return None

    if not is_playwright_installed():
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
                page = context.new_page()
                # Block heavy resource types we don't need — this is
                # a scraper, not a real browsing session, so skipping
                # images/fonts/media cuts render time significantly.
                page.route("**/*", _block_heavy_resources)

                try:
                    page.goto(url, timeout=timeout_ms, wait_until=RENDER_WAIT_UNTIL)
                except PWTimeoutError:
                    # networkidle never arrived (e.g. page keeps a
                    # long-poll connection open) — grab whatever
                    # rendered by the "load"/current DOM instead of nothing.
                    logger.debug(
                        "playwright_fallback: networkidle timeout for %s, using current DOM",
                        url,
                    )
                except Exception as goto_exc:
                    # Navigation-level failures (bad cert, DNS, aborted
                    # connection, etc.) — still try to salvage whatever
                    # the page currently holds rather than bailing entirely.
                    logger.debug(
                        "playwright_fallback: goto raised for %s: %s", url, goto_exc
                    )

                html = page.content()
                final_url = page.url
                return RenderResult(html=html, final_url=final_url)
            finally:
                context.close()
    except Exception as exc:
        logger.warning("playwright_fallback: render failed for %s: %s", url, exc)
        return None
    finally:
        if browser is not None:
            try:
                browser.close()
            except Exception:  # pragma: no cover - best-effort cleanup
                logger.debug("playwright_fallback: browser.close() failed", exc_info=True)


def _block_heavy_resources(route: Any) -> None:
    """Route handler: abort image/media/font requests, continue everything else."""
    if route.request.resource_type in _BLOCKED_RESOURCE_TYPES:
        route.abort()
    else:
        route.continue_()


def should_attempt_render(scrape_result: dict) -> bool:
    """
    Heuristic: only worth the cost of a real browser if the plain-HTTP
    scrape succeeded (no fetch error) but text/images/links all came
    back thin — the classic signature of an unrendered SPA shell.
    """
    if not isinstance(scrape_result, dict):
        return False
    if scrape_result.get("error"):
        return False  # dead URL — a browser won't fix that either
    text_len = len(scrape_result.get("text") or "")
    has_links = bool(scrape_result.get("links"))
    return text_len < THIN_TEXT_THRESHOLD and not has_links