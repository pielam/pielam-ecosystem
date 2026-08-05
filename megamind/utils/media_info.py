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
from typing import Optional

def normalize_image(img) -> Optional[dict]:
    """
    Accepts a dict (various key names) or a bare URL string.
    Returns {'url', 'alt', 'href'} or None if there's no usable URL.
    'href' is the link the image should open to, if the scraper found
    one (e.g. an image wrapped in an <a> tag) — empty string otherwise.
    """
    if isinstance(img, dict):
        url  = (img.get('url') or img.get('src') or '').strip()
        alt  = (img.get('alt') or img.get('title') or '').strip()
        href = (img.get('href') or '').strip()
    else:
        url, alt, href = (str(img).strip() if img else ''), '', ''

    if not url:
        return None
    return {'url': url, 'alt': alt, 'href': href}


def normalize_images(raw_images, limit: int = None) -> list[dict]:
    """Normalize a whole extracted_images list, dropping unusable entries."""
    normalized = []
    for img in raw_images or []:
        entry = normalize_image(img)
        if entry:
            normalized.append(entry)
        if limit and len(normalized) >= limit:
            break
    return normalized


def normalize_link(lnk) -> Optional[dict]:
    """
    Accepts a dict (various key names) or a bare URL string.
    Returns {'href', 'text'} or None if there's no usable href.
    """
    if isinstance(lnk, dict):
        href = (lnk.get('href') or lnk.get('url')   or '').strip()
        text = (lnk.get('text') or lnk.get('title') or '').strip()
    else:
        href, text = (str(lnk).strip() if lnk else ''), ''

    if not href:
        return None
    return {'href': href, 'text': text}


def normalize_links(raw_links, limit: int = None) -> list[dict]:
    """Normalize a whole extracted_links list, dropping unusable entries."""
    normalized = []
    for lnk in raw_links or []:
        entry = normalize_link(lnk)
        if entry:
            normalized.append(entry)
        if limit and len(normalized) >= limit:
            break
    return normalized