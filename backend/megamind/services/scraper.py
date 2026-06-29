"""
megamind/services/scraper.py

On-demand URL scraper.
Extracts: OG meta tags, images, videos, links, and visible text.

Requirements (add to requirements.txt):
    requests>=2.31.0
    beautifulsoup4>=4.12.0
    lxml>=5.0.0          # faster BS4 parser (optional but recommended)
"""

import re
import logging
from datetime import datetime, timezone
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
DEFAULT_TIMEOUT = 10  # seconds
DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (compatible; ConnectedServiceBot/1.0; "
        "+https://yourapp.com/bot)"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}
MAX_IMAGES = 50
MAX_VIDEOS = 20
MAX_LINKS = 100


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

from typing import Union

def scrape_url(
    url: str,
    api_key: Union[str, None] = None,
    auth_token: Union[str, None] = None
) -> dict:
    """
    Fetch and parse *url*.

    Returns a dict with keys:
        og          – OG / meta data
        images      – list of {url, alt}
        videos      – list of {url, type}
        links       – list of {href, text}
        text        – main visible text (str)
        raw         – full parsed payload (stored in last_fetched_data)
        error       – None on success, error message string on failure
        fetched_at  – ISO-8601 timestamp
    """
    result = _empty_result()
    headers = dict(DEFAULT_HEADERS)

    if auth_token:
        headers["Authorization"] = f"Bearer {auth_token}"
    if api_key:
        headers["X-Api-Key"] = api_key

    try:
        response = requests.get(url, headers=headers, timeout=DEFAULT_TIMEOUT)
        response.raise_for_status()
    except requests.RequestException as exc:
        logger.warning("scrape_url failed for %s: %s", url, exc)
        result["error"] = str(exc)
        return result

    content_type = response.headers.get("Content-Type", "")
    if "text/html" not in content_type and "application/xhtml" not in content_type:
        result["error"] = f"Unsupported content-type: {content_type}"
        return result

    soup = BeautifulSoup(response.text, "lxml")
    base_url = _base_url(soup, url)

    result["og"]     = _extract_og(soup)
    result["images"] = _extract_images(soup, base_url)
    result["videos"] = _extract_videos(soup, base_url)
    result["links"]  = _extract_links(soup, base_url)
    result["text"]   = _extract_text(soup)
    result["raw"]    = {
        "og":     result["og"],
        "images": result["images"],
        "videos": result["videos"],
        "links":  result["links"],
        "text_snippet": result["text"][:500] if result["text"] else "",
    }
    result["fetched_at"] = datetime.now(timezone.utc).isoformat()
    return result


# ---------------------------------------------------------------------------
# Extraction helpers
# ---------------------------------------------------------------------------

def _extract_og(soup: BeautifulSoup) -> dict:
    og = {}

    def _meta(prop_attr, prop_value, content_attr="content"):
        tag = soup.find("meta", attrs={prop_attr: prop_value})
        return tag.get(content_attr, "").strip() if tag else ""

    og["title"]       = (_meta("property", "og:title")
                         or _meta("name", "twitter:title")
                         or (soup.title.string.strip() if soup.title else ""))
    og["description"] = (_meta("property", "og:description")
                         or _meta("name", "description")
                         or _meta("name", "twitter:description"))
    og["thumbnail"]   = (_meta("property", "og:image")
                         or _meta("name", "twitter:image"))
    og["site_name"]   = _meta("property", "og:site_name")
    og["type"]        = _meta("property", "og:type")
    return og


def _extract_images(soup: BeautifulSoup, base_url: str) -> list[dict]:
    images = []
    seen = set()
    for tag in soup.find_all("img", src=True):
        src = _abs(tag["src"], base_url)
        if src and src not in seen and _is_valid_url(src):
            seen.add(src)

            # Walk up the DOM to find the nearest wrapping <a> tag
            parent_href = ""
            for parent in tag.parents:
                if parent.name == "a" and parent.get("href"):
                    parent_href = _abs(parent["href"], base_url)
                    break

            images.append({
                "url":  src,
                "alt":  tag.get("alt", "").strip(),
                "href": parent_href,   # ← the link this image belongs to
            })
        if len(images) >= MAX_IMAGES:
            break
    return images

def _extract_videos(soup: BeautifulSoup, base_url: str) -> list[dict]:
    videos = []
    seen = set()

    # <video src="..."> or <video><source src="...">
    for tag in soup.find_all(["video", "source"]):
        src = tag.get("src") or tag.get("data-src")
        if src:
            src = _abs(src, base_url)
            if src and src not in seen and _is_valid_url(src):
                seen.add(src)
                videos.append({"url": src, "type": tag.get("type", "video/*")})

    # <iframe> embeds (YouTube, Vimeo, etc.)
    for tag in soup.find_all("iframe", src=True):
        src = tag["src"]
        if any(domain in src for domain in ("youtube", "vimeo", "dailymotion", "twitch")):
            src = _abs(src, base_url)
            if src and src not in seen:
                seen.add(src)
                videos.append({"url": src, "type": "embed"})

        if len(videos) >= MAX_VIDEOS:
            break

    return videos[:MAX_VIDEOS]


def _extract_links(soup: BeautifulSoup, base_url: str) -> list[dict]:
    links = []
    seen = set()
    for tag in soup.find_all("a", href=True):
        href = _abs(tag["href"], base_url)
        if href and href not in seen and _is_valid_url(href):
            seen.add(href)
            links.append({"href": href, "text": tag.get_text(strip=True)[:200]})
        if len(links) >= MAX_LINKS:
            break
    return links


def _extract_text(soup: BeautifulSoup) -> str:
    # Remove script / style noise
    for tag in soup(["script", "style", "noscript", "head"]):
        tag.decompose()

    # Try <main>, <article>, <body> in priority order
    for selector in ["main", "article", "body"]:
        container = soup.find(selector)
        if container:
            text = container.get_text(separator="\n", strip=True)
            text = re.sub(r"\n{3,}", "\n\n", text)
            return text.strip()
    return ""


# ---------------------------------------------------------------------------
# Utility
# ---------------------------------------------------------------------------

def _abs(href: str, base: str) -> str:
    try:
        return urljoin(base, href.strip())
    except Exception:
        return ""


def _base_url(soup: BeautifulSoup, fallback: str) -> str:
    base_tag = soup.find("base", href=True)
    return base_tag["href"] if base_tag else fallback


def _is_valid_url(url: str) -> bool:
    try:
        parsed = urlparse(url)
        return parsed.scheme in ("http", "https") and bool(parsed.netloc)
    except Exception:
        return False


def _empty_result() -> dict:
    return {
        "og": {},
        "images": [],
        "videos": [],
        "links": [],
        "text": "",
        "raw": {},
        "error": None,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }