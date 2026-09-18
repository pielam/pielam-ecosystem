"""
content_extractor.py

Deep, best-effort extraction from a parsed HTML page. Every extractor
is wrapped individually (see `_safe`) so one bad selector or malformed
tag can't take the rest of the page down with it — you always get back
whatever could be extracted, plus a `warnings` list for anything that
failed.

Data-quality extras beyond a naive scraper:
  - OG/Twitter-card tags, falling back to JSON-LD when a field is
    missing (JSON-LD is usually more complete/accurate for commerce
    pages since it's built for search engines, not just link previews).
  - schema.org Product extraction (name/brand/price/currency/
    availability) from JSON-LD, falling back to CSS-selector heuristics
    for sites that don't emit structured data.
  - Favicon match uses exact rel-token matching (icon > shortcut icon >
    apple-touch-icon), not substring matching — a naive substring check
    on the joined rel string collapses all three into whichever tag
    happens to appear first, silently defeating the preference order.
  - Tracking-pixel/spacer filtering on images (1x1 GIFs, data URIs,
    known ad/analytics beacon patterns).
  - Videos are run through video_platforms.get_video_info() so each
    entry also carries its detected platform/embed URL, not just the
    raw src.

    pip install beautifulsoup4 lxml --break-system-packages
"""

import json
import logging
import re
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from video_platforms import get_video_info, is_recognized_platform

logger = logging.getLogger(__name__)

MAX_IMAGES = 50
MAX_VIDEOS = 20
MAX_LINKS = 100

_FAVICON_RELS_IN_PRIORITY = ("icon", "shortcut icon", "apple-touch-icon")
_SPEC_KEYWORDS = ("specification", "specs", "features", "details", "technical")
_TRACKING_HINTS = ("pixel", "spacer", "blank.gif", "tracking", "beacon",
                    "doubleclick", "analytics", "facebook.com/tr", "google-analytics")


def _safe(fn, default, warnings, label, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except Exception as exc:
        logger.debug("content_extractor: %s failed: %s", label, exc)
        warnings.append(f"{label}: {exc}")
        return default


def make_soup(html: str) -> BeautifulSoup:
    try:
        return BeautifulSoup(html, "lxml")
    except Exception:
        return BeautifulSoup(html, "html.parser")


def _abs(href, base):
    try:
        return urljoin(base, href.strip())
    except Exception:
        return ""


def _rel_tokens(rel_attr) -> set:
    if rel_attr is None:
        return set()
    if isinstance(rel_attr, list):
        return {t.lower() for t in rel_attr}
    return {rel_attr.lower()}


def is_probably_tracking_pixel(url: str, width=None, height=None) -> bool:
    if not url:
        return True
    if url.startswith("data:"):
        return True
    low = url.lower()
    if any(hint in low for hint in _TRACKING_HINTS):
        return True
    try:
        if width is not None and height is not None and int(width) <= 2 and int(height) <= 2:
            return True
    except (TypeError, ValueError):
        pass
    return False


# ---------------------------------------------------------------------------
# Field extractors
# ---------------------------------------------------------------------------

def extract_json_ld(soup: BeautifulSoup) -> list:
    blocks = []
    for tag in soup.find_all("script", type="application/ld+json"):
        if not tag.string:
            continue
        try:
            blocks.append(json.loads(tag.string))
        except (ValueError, TypeError):
            continue
    return blocks


def extract_og(soup: BeautifulSoup, structured_data: list = None) -> dict:
    og = {}

    def _meta(attr, value, content="content"):
        tag = soup.find("meta", attrs={attr: value})
        return tag.get(content, "").strip() if tag else ""

    og["title"] = (_meta("property", "og:title") or _meta("name", "twitter:title")
                   or (soup.title.string.strip() if soup.title and soup.title.string else ""))
    og["description"] = (_meta("property", "og:description") or _meta("name", "description")
                          or _meta("name", "twitter:description"))
    og["thumbnail"] = _meta("property", "og:image") or _meta("name", "twitter:image")
    og["site_name"] = _meta("property", "og:site_name")
    og["type"] = _meta("property", "og:type")

    if structured_data and (not og["description"] or not og["thumbnail"]):
        for block in structured_data:
            for item in (block if isinstance(block, list) else [block]):
                if not isinstance(item, dict):
                    continue
                if not og["description"] and item.get("description"):
                    og["description"] = item["description"]
                if not og["thumbnail"]:
                    image = item.get("image")
                    if isinstance(image, list):
                        image = image[0] if image else None
                    if isinstance(image, dict):
                        image = image.get("url")
                    if image:
                        og["thumbnail"] = image
    return og


def extract_canonical(soup: BeautifulSoup, fallback_url: str) -> str:
    tag = soup.find("link", rel=lambda x: x and "canonical" in x)
    href = tag.get("href") if tag else None
    return _abs(href, fallback_url) if href else fallback_url


def extract_favicon(soup: BeautifulSoup, base_url: str) -> str:
    tags = soup.find_all("link", href=True)
    for rel_name in _FAVICON_RELS_IN_PRIORITY:
        wanted = set(rel_name.split())
        for tag in tags:
            if wanted <= _rel_tokens(tag.get("rel")):
                return _abs(tag["href"], base_url)
    return _abs("/favicon.ico", base_url)


def extract_author(soup: BeautifulSoup) -> str:
    tag = (soup.find("meta", attrs={"name": "author"})
           or soup.find("meta", attrs={"property": "article:author"}))
    return tag.get("content", "").strip() if tag else ""


def extract_published_time(soup: BeautifulSoup) -> str:
    tag = (soup.find("meta", attrs={"property": "article:published_time"})
           or soup.find("meta", attrs={"name": "date"}))
    if tag:
        return tag.get("content", "").strip()
    time_tag = soup.find("time", attrs={"datetime": True})
    return time_tag.get("datetime", "").strip() if time_tag else ""


def extract_keywords(soup: BeautifulSoup) -> str:
    tag = soup.find("meta", attrs={"name": "keywords"})
    return tag.get("content", "").strip() if tag else ""


def extract_headings(soup: BeautifulSoup) -> dict:
    return {
        level: [h.get_text(strip=True) for h in soup.find_all(level) if h.get_text(strip=True)]
        for level in ("h1", "h2", "h3")
    }


def extract_product_info(structured_data: list, soup: BeautifulSoup) -> dict:
    """JSON-LD first (authoritative), CSS heuristics fill in the rest."""

    def _iter_nodes(block):
        for node in (block if isinstance(block, list) else [block]):
            if not isinstance(node, dict):
                continue
            if isinstance(node.get("@graph"), list):
                yield from (n for n in node["@graph"] if isinstance(n, dict))
            else:
                yield node

    info = {}
    for block in structured_data or []:
        for node in _iter_nodes(block):
            types = node.get("@type")
            types = types if isinstance(types, list) else [types]
            if "Product" not in (types or []):
                continue
            if node.get("name"):
                info.setdefault("name", node["name"])
            brand = node.get("brand")
            if isinstance(brand, dict):
                brand = brand.get("name")
            if brand:
                info.setdefault("brand", brand)
            offers = node.get("offers") or {}
            if isinstance(offers, list):
                offers = offers[0] if offers else {}
            if offers.get("price"):
                info.setdefault("price", str(offers["price"]))
            if offers.get("priceCurrency"):
                info.setdefault("currency", offers["priceCurrency"])
            if offers.get("availability"):
                info.setdefault("availability", str(offers["availability"]).rsplit("/", 1)[-1])
            if info:
                break
        if info:
            break

    if "price" not in info:
        for selector in [{"class": "price"}, {"class": "product-price"}, {"itemprop": "price"},
                          {"class": lambda x: x and "price" in x.lower()}]:
            elem = soup.find(["span", "div", "p"], selector)
            if elem:
                info["price"] = elem.get_text(strip=True)
                break

    specs = {}
    for keyword in _SPEC_KEYWORDS:
        section = soup.find(["div", "section", "table"], class_=lambda x: x and keyword in x.lower())
        if section:
            for item in section.find_all(["li", "tr", "div"]):
                text = item.get_text(strip=True)
                if ":" in text:
                    k, v = text.split(":", 1)
                    k, v = k.strip(), v.strip()
                    if k and v:
                        specs[k] = v
            if specs:
                break
    if specs:
        info["specifications"] = specs

    return info


def extract_images(soup: BeautifulSoup, base_url: str) -> list:
    images, seen = [], set()
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
        images.append({"url": src, "alt": tag.get("alt", "").strip(), "href": parent_href})
        if len(images) >= MAX_IMAGES:
            break
    return images


def extract_videos(soup: BeautifulSoup, base_url: str, resolve_short_links: bool = True) -> list:
    """Returns platform-normalized video entries (see video_platforms.get_video_info),
    combining <video>/<source> tags (always direct media) with <iframe>
    embeds that resolve to a recognized platform (ads/maps/chat widgets
    are filtered out by requiring a recognized platform)."""
    videos, seen = [], set()

    for tag in soup.find_all(["video", "source"]):
        src = tag.get("src") or tag.get("data-src")
        if src:
            src = _abs(src, base_url)
            if src and src not in seen:
                seen.add(src)
                info = get_video_info(src, resolve_short_links=resolve_short_links)
                videos.append({**info, "url": src, "source_tag": tag.name})

    for tag in soup.find_all("iframe", src=True):
        src = _abs(tag["src"], base_url)
        if not src or src in seen:
            continue
        info = get_video_info(src, resolve_short_links=resolve_short_links)
        if is_recognized_platform(info):
            seen.add(src)
            videos.append({**info, "url": src, "source_tag": "iframe"})
        if len(videos) >= MAX_VIDEOS:
            break

    return videos[:MAX_VIDEOS]


def extract_links(soup: BeautifulSoup, base_url: str) -> list:
    links, seen = [], set()
    for tag in soup.find_all("a", href=True):
        href = _abs(tag["href"], base_url)
        if href and href not in seen and href.startswith(("http://", "https://")):
            seen.add(href)
            links.append({"href": href, "text": tag.get_text(strip=True)[:200]})
        if len(links) >= MAX_LINKS:
            break
    return links


def extract_text(soup: BeautifulSoup) -> str:
    for tag in soup(["script", "style", "noscript", "head"]):
        tag.decompose()
    for selector in ("main", "article", "body"):
        container = soup.find(selector)
        if container:
            text = container.get_text(separator="\n", strip=True)
            return re.sub(r"\n{3,}", "\n\n", text).strip()
    return ""


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def extract_all(html: str, base_url: str, resolve_video_short_links: bool = True) -> dict:
    """
    Runs every extractor against `html`, returns one dict:

        {og, canonical_url, favicon, author, published_time, keywords,
         headings, structured_data, product_info, images, videos,
         links, text, warnings}

    Never raises — a total HTML-parse failure still returns a valid
    (mostly empty) dict with a warning, rather than propagating.
    """
    warnings = []
    result = {
        "og": {}, "canonical_url": base_url, "favicon": "", "author": "",
        "published_time": "", "keywords": "", "headings": {"h1": [], "h2": [], "h3": []},
        "structured_data": [], "product_info": {}, "images": [], "videos": [],
        "links": [], "text": "", "warnings": warnings,
    }

    soup = _safe(make_soup, None, warnings, "parse_html", html)
    if soup is None:
        warnings.append("HTML parsing failed entirely")
        result["text"] = re.sub(r"<[^>]+>", " ", html)[:5000].strip()
        return result

    structured_data = _safe(extract_json_ld, [], warnings, "json_ld", soup)
    result["structured_data"] = structured_data
    result["og"] = _safe(extract_og, {}, warnings, "og", soup, structured_data)
    result["canonical_url"] = _safe(extract_canonical, base_url, warnings, "canonical", soup, base_url)
    result["favicon"] = _safe(extract_favicon, "", warnings, "favicon", soup, base_url)
    result["author"] = _safe(extract_author, "", warnings, "author", soup)
    result["published_time"] = _safe(extract_published_time, "", warnings, "published_time", soup)
    result["keywords"] = _safe(extract_keywords, "", warnings, "keywords", soup)
    result["headings"] = _safe(extract_headings, result["headings"], warnings, "headings", soup)
    result["product_info"] = _safe(extract_product_info, {}, warnings, "product_info", structured_data, soup)
    result["images"] = _safe(extract_images, [], warnings, "images", soup, base_url)
    result["videos"] = _safe(extract_videos, [], warnings, "videos", soup, base_url, resolve_video_short_links)
    result["links"] = _safe(extract_links, [], warnings, "links", soup, base_url)
    result["text"] = _safe(extract_text, "", warnings, "text", soup)

    if not result["text"]:
        result["text"] = _safe(
            lambda: re.sub(r"\s+", " ", soup.get_text(" ", strip=True))[:5000],
            "", warnings, "text_fallback",
        )

    return result