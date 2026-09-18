# megamind/utils/media_info.py

"""
Shared image/link normalizers.

extracted_images and extracted_links get written by more than one
producer (megamind/services/scraper.py, megamind/utils/service_fetcher.py,
and potentially manual entry down the line), and each has used slightly
different key names over time — 'url' vs 'src', 'href' vs 'url',
'alt' vs 'title'. These helpers collapse all of that into one stable
shape so the frontend never has to guess which key a given row used.

Used by personal_engine.py (public Engine feed) and discovery_engine.py
(legacy last_fetched_data fallback in _serialize_service) so an image or
link renders identically everywhere, regardless of which code path wrote it.
"""

from __future__ import annotations

import re
from typing import Any, Dict, Iterable, List, Optional, TypedDict, Union

__all__ = [
    "NormalizedImage",
    "NormalizedLink",
    "is_probably_tracking_pixel",
    "normalize_image",
    "normalize_images",
    "normalize_link",
    "normalize_links",
]

_TRACKING_HINTS = (
    "pixel", "spacer", "blank.gif", "tracking", "beacon",
    "doubleclick", "analytics", "facebook.com/tr", "google-analytics",
)

# Compiled once at import time. A single regex alternation scan is
# noticeably faster than looping over `_TRACKING_HINTS` with `in` checks
# on every call, especially when normalizing large scraped image lists.
_TRACKING_HINTS_RE = re.compile("|".join(re.escape(h) for h in _TRACKING_HINTS))


def is_probably_tracking_pixel(
    url: Optional[str],
    width: Optional[Union[int, float, str]] = None,
    height: Optional[Union[int, float, str]] = None,
) -> bool:
    """
    Heuristic filter for scraped <img> sources that are tracking
    pixels/spacers rather than real content images — 1x1 GIFs, ad/
    analytics beacons, inline base64 data URIs (almost always icons
    or spacers when found loose in page markup, not real photos).

    Shared by megamind/services/scraper.py and
    megamind/utils/service_fetcher.py so "what counts as junk" is
    defined once instead of drifting between the two extractors.

    Conservative on purpose: a false negative (keeping a pixel) is
    harmless clutter in extracted_images; a false positive (dropping
    a real product photo) is a real regression — so this only flags
    the obvious cases.
    """
    if not url:
        return True
    if url.startswith("data:"):
        return True
    if _TRACKING_HINTS_RE.search(url.lower()):
        return True
    if width is not None and height is not None:
        try:
            if float(width) <= 2 and float(height) <= 2:
                return True
        except (TypeError, ValueError):
            pass
    return False


class NormalizedImage(TypedDict):
    url: str
    alt: str
    href: str


class NormalizedLink(TypedDict):
    href: str
    text: str


def normalize_image(img: Union[Dict[str, Any], str, None]) -> Optional[NormalizedImage]:
    """
    Accepts a dict (various key names) or a bare URL string.
    Returns {'url', 'alt', 'href'} or None.

    Display contract: a photo is only ever shown alongside the exact
    link it came from. If the source data doesn't carry a real 'href'
    (or 'url'/'link' alias) for that image, there is nothing honest to
    link it to, so the image itself is dropped here rather than passed
    through with an empty href — callers should never have to guess
    or fabricate a link for a photo that didn't come with one.

    A bare URL string (no surrounding dict) carries no href information
    at all, so it never has a link and is always dropped.
    """
    if isinstance(img, dict):
        url = str(img.get("url") or img.get("src") or "").strip()
        if not url:
            return None
        alt = str(img.get("alt") or img.get("title") or "").strip()
        href = str(img.get("href") or img.get("link") or "").strip()
        if not href:
            return None
    else:
        # Bare string (or None) — no place for a link to live, so there's
        # never an "exact link" to pair with the photo.
        return None

    return {"url": url, "alt": alt, "href": href}


def normalize_images(
    raw_images: Optional[Iterable[Any]],
    limit: Optional[int] = None,
) -> List[NormalizedImage]:
    """Normalize a whole extracted_images list. Entries with no usable URL
    or no exact href to link to are dropped (see normalize_image)."""
    if not raw_images:
        return []

    normalized: List[NormalizedImage] = []
    for img in raw_images:
        entry = normalize_image(img)
        if entry:
            normalized.append(entry)
            if limit is not None and len(normalized) >= limit:
                break
    return normalized


def normalize_link(lnk: Union[Dict[str, Any], str, None]) -> Optional[NormalizedLink]:
    """
    Accepts a dict (various key names) or a bare URL string.
    Returns {'href', 'text'} or None if there's no usable href.
    """
    if isinstance(lnk, dict):
        href = str(lnk.get("href") or lnk.get("url") or "").strip()
        if not href:
            return None
        text = str(lnk.get("text") or lnk.get("title") or "").strip()
    elif lnk is None:
        return None
    else:
        href = str(lnk).strip()
        if not href:
            return None
        text = ""

    return {"href": href, "text": text}


def normalize_links(
    raw_links: Optional[Iterable[Any]],
    limit: Optional[int] = None,
) -> List[NormalizedLink]:
    """Normalize a whole extracted_links list, dropping unusable entries."""
    if not raw_links:
        return []

    normalized: List[NormalizedLink] = []
    for lnk in raw_links:
        entry = normalize_link(lnk)
        if entry:
            normalized.append(entry)
            if limit is not None and len(normalized) >= limit:
                break
    return normalized