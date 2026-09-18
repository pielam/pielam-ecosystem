# megamind/utils/extractor.py

"""
extractor.py

Extracts exactly four things from a parsed HTML page: images, videos,
links, text. Nothing else — no OG tags, no JSON-LD, no favicon/author/
product info.

Quality choices, each swappable independently if you disagree with one:
  - Images: dedup'd, absolute-URL'd, tracking-pixel/spacer filtered.
  - Videos: <video>/<source> (always direct media) + <iframe> embeds
    that resolve to a *recognized* platform via video_platforms.py
    (this is what filters out ad/chat/map iframes instead of treating
    every iframe as a "video").
  - Links: absolute-URL'd, deduped, http(s) only.
  - Text: density-scored main-content extraction (readability_extractor)
    instead of a naive "grab everything inside <main>/<article>/<body>"
    — that naive approach pulls in nav menus, cookie banners, and
    related-article widgets on any page that doesn't isolate its
    content in a dedicated tag, which is most real-world pages.

Every extractor is wrapped individually so one bad selector or
malformed tag can't take the rest of the page down with it — you
always get back whatever could be extracted, plus a `warnings` list
for anything that failed.

NOTE: `is_probably_tracking_pixel` / `_TRACKING_HINTS` duplicate
megamind/utils/media_info.py's version of the same heuristic. Left
duplicated rather than importing (this module is written to also run
standalone, see the import fallback below) — if the heuristic changes,
it needs to change in both places.

    pip install beautifulsoup4 lxml requests --break-system-packages
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional, Union
from urllib.parse import urljoin

from bs4 import BeautifulSoup

try:  # pragma: no cover - import shape depends on call context
    from .video_platforms import get_video_info, is_recognized_platform
    from .readability_extractor import extract_main_content
except ImportError:  # pragma: no cover
    from video_platforms import get_video_info, is_recognized_platform
    from readability_extractor import extract_main_content

logger = logging.getLogger(__name__)

MAX_IMAGES = 50
MAX_VIDEOS = 20
MAX_LINKS = 100

_TRACKING_HINTS = ("pixel", "spacer", "blank.gif", "tracking", "beacon",
                    "doubleclick", "analytics", "facebook.com/tr", "google-analytics")


def _safe(fn, default, warnings: List[str], label: str, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except Exception as exc:
        logger.debug("extractor: %s failed: %s", label, exc)
        warnings.append(f"{label}: {exc}")
        return default


def make_soup(html: str) -> BeautifulSoup:
    try:
        return BeautifulSoup(html, "lxml")
    except Exception:
        return BeautifulSoup(html, "html.parser")


def _abs(href: Optional[str], base: str) -> str:
    if not href:
        return ""
    try:
        return urljoin(base, href.strip())
    except Exception:
        return ""


def is_probably_tracking_pixel(
    url: Optional[str],
    width: Optional[Union[int, float, str]] = None,
    height: Optional[Union[int, float, str]] = None,
) -> bool:
    if not url:
        return True
    if url.startswith("data:"):
        return True
    low = url.lower()
    if any(hint in low for hint in _TRACKING_HINTS):
        return True
    try:
        # float(), not int(): scraped width/height attributes routinely
        # arrive as strings like "1.0", which int() rejects outright —
        # that raised a ValueError that got swallowed here, silently
        # skipping the size check instead of applying it.
        if width is not None and height is not None and float(width) <= 2 and float(height) <= 2:
            return True
    except (TypeError, ValueError):
        pass
    return False


# ---------------------------------------------------------------------------
# The four extractors
# ---------------------------------------------------------------------------

def extract_images(soup: BeautifulSoup, base_url: str) -> List[Dict[str, str]]:
    images: List[Dict[str, str]] = []
    seen = set()
    for tag in soup.find_all("img", src=True):
        src = _abs(tag["src"], base_url)
        if not src or src in seen or is_probably_tracking_pixel(src, tag.get("width"), tag.get("height")):
            continue
        seen.add(src)
        parent_href = ""
        for parent in tag.parents:
            if getattr(parent, "name", None) == "a" and parent.get("href"):
                parent_href = _abs(parent["href"], base_url)
                break
        images.append({"url": src, "alt": (tag.get("alt") or "").strip(), "href": parent_href})
        if len(images) >= MAX_IMAGES:
            break
    return images


def extract_videos(
    soup: BeautifulSoup,
    base_url: str,
    resolve_short_links: bool = True,
    warnings: Optional[List[str]] = None,
) -> List[Dict[str, Any]]:
    """Combines <video>/<source> tags (always direct media) with
    <iframe> embeds that resolve to a recognized platform (this is
    what keeps ads/maps/chat-widget iframes out of the results).

    Each get_video_info() call is individually try/excepted: previously
    a single URL that raised inside get_video_info (a malformed embed
    URL, a platform-resolver bug, etc.) propagated out of this whole
    function — since extract_all() wraps extract_videos() as one unit,
    that meant one bad video URL discarded every OTHER video already
    found on the page, not just the failing one.
    """
    videos: List[Dict[str, Any]] = []
    seen = set()

    def _video_info(src: str) -> Dict[str, Any]:
        try:
            return get_video_info(src, resolve_short_links=resolve_short_links)
        except Exception as exc:
            logger.debug("extractor: get_video_info failed for %s: %s", src, exc)
            if warnings is not None:
                warnings.append(f"video_info({src}): {exc}")
            return {}

    for tag in soup.find_all(["video", "source"]):
        if len(videos) >= MAX_VIDEOS:
            break
        src = tag.get("src") or tag.get("data-src")
        if not src:
            continue
        src = _abs(src, base_url)
        if not src or src in seen:
            continue
        seen.add(src)
        info = _video_info(src)
        videos.append({**info, "url": src, "source_tag": tag.name})

    for tag in soup.find_all("iframe", src=True):
        if len(videos) >= MAX_VIDEOS:
            break
        src = _abs(tag["src"], base_url)
        if not src or src in seen:
            continue
        info = _video_info(src)
        if is_recognized_platform(info):
            seen.add(src)
            videos.append({**info, "url": src, "source_tag": "iframe"})

    return videos[:MAX_VIDEOS]


def extract_links(soup: BeautifulSoup, base_url: str) -> List[Dict[str, str]]:
    links: List[Dict[str, str]] = []
    seen = set()
    for tag in soup.find_all("a", href=True):
        href = _abs(tag["href"], base_url)
        if href and href not in seen and href.startswith(("http://", "https://")):
            seen.add(href)
            links.append({"href": href, "text": tag.get_text(strip=True)[:200]})
        if len(links) >= MAX_LINKS:
            break
    return links


def extract_text(soup: BeautifulSoup) -> str:
    """Density-scored main-content extraction (see readability_extractor).
    Falls back to a raw whole-page text dump only if scoring finds
    nothing at all."""
    text, _debug = extract_main_content(soup)
    return (text or "").strip()


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def extract_all(html: str, base_url: str, resolve_video_short_links: bool = True) -> Dict[str, Any]:
    """
    Runs all four extractors against `html`. Returns exactly:

        {images, videos, links, text, warnings}

    Never raises — a total HTML-parse failure still returns a valid
    (mostly empty) dict with a warning, rather than propagating.
    """
    warnings: List[str] = []
    result: Dict[str, Any] = {"images": [], "videos": [], "links": [], "text": "", "warnings": warnings}

    if not html or not isinstance(html, str):
        warnings.append("empty or non-string html input")
        return result

    soup = _safe(make_soup, None, warnings, "parse_html", html)
    if soup is None:
        warnings.append("HTML parsing failed entirely")
        result["text"] = re.sub(r"<[^>]+>", " ", html)[:5000].strip()
        return result

    result["images"] = _safe(extract_images, [], warnings, "images", soup, base_url)
    result["videos"] = _safe(
        extract_videos, [], warnings, "videos", soup, base_url, resolve_video_short_links, warnings
    )
    result["links"] = _safe(extract_links, [], warnings, "links", soup, base_url)

    # extract_text uses readability_extractor, which decomposes junk
    # tags (nav/footer/aside/etc.) on the soup it's given — that would
    # corrupt image/video/link extraction if run on the same soup, so
    # text gets its own fresh parse of the same html.
    text_soup = _safe(make_soup, soup, warnings, "parse_html_for_text", html)
    result["text"] = _safe(extract_text, "", warnings, "text", text_soup)

    if not result["text"]:
        result["text"] = _safe(
            lambda: re.sub(r"\s+", " ", soup.get_text(" ", strip=True))[:5000],
            "", warnings, "text_fallback",
        )

    return result