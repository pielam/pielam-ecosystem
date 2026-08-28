# megamind/utils/bind_content.py

"""
Fetches a single URL and binds together everything extracted from it —
images, videos, links, and text — into one structured result.

Standalone: only depends on `requests` and `beautifulsoup4` (lxml is
used if installed, falls back to the stdlib html.parser otherwise).

    pip install requests beautifulsoup4 lxml --break-system-packages

Usage:
    python bind_content.py https://example.com
    python bind_content.py https://example.com --json out.json
    python bind_content.py https://example.com --timeout 20
    python bind_content.py https://example.com --max-bytes 10000000

As a library:
    from bind_content import bind_content
    result = bind_content("https://example.com")
    result["text"], result["images"], result["videos"], result["links"]
"""

import argparse
import hashlib
import ipaddress
import json
import re
import socket
import sys
from datetime import datetime, timezone
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

VIDEO_EXTS = (".mp4", ".webm", ".ogg", ".mov", ".m4v", ".mkv", ".avi")
KNOWN_VIDEO_EMBED_HOSTS = (
    "youtube.com", "youtu.be", "player.vimeo.com", "vimeo.com",
    "dailymotion.com", "twitch.tv", "streamable.com", "rumble.com",
)

MAX_IMAGES = 50
MAX_VIDEOS = 20
MAX_LINKS = 100
MAX_REDIRECTS = 5
DEFAULT_MAX_CONTENT_BYTES = 25 * 1024 * 1024


class UnsafeURLError(ValueError):
    """Raised when a URL resolves to a private/loopback/link-local address,
    or when a redirect chain tries to steer the fetch to one."""


def _assert_public_url(url: str) -> None:
    """
    SSRF guard: refuses to fetch localhost/private/link-local/reserved
    addresses. Called on the original URL AND, via _safe_get below, on
    every redirect hop the response chain follows — a single check on
    just the starting URL is not enough, since a malicious or
    compromised server can 302 the request to
    http://169.254.169.254/latest/meta-data/ or http://localhost:6379
    *after* that initial check already passed. This still doesn't
    close a pure DNS-rebinding race (hostname resolves safely here,
    then differently at actual connect time) — closing that requires
    controlling the connection at the socket level, which plain
    `requests` doesn't expose — but it does close the much more common
    "redirect to an internal address" vector.
    """
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise UnsafeURLError(f"Unsupported scheme: {parsed.scheme!r}")
    if not parsed.hostname:
        raise UnsafeURLError("URL has no hostname")
    if parsed.hostname.lower() in ("localhost", "0.0.0.0"):
        raise UnsafeURLError("Refusing to fetch localhost")
    try:
        infos = socket.getaddrinfo(parsed.hostname, None)
    except socket.gaierror as exc:
        raise UnsafeURLError(f"DNS resolution failed: {exc}") from exc
    for _, _, _, _, sockaddr in infos:
        ip = ipaddress.ip_address(sockaddr[0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
            raise UnsafeURLError(f"Resolved to non-routable address {ip}")


def _safe_get(url: str, headers: dict, timeout: int, max_redirects: int = MAX_REDIRECTS):
    """
    GET with redirects followed manually (allow_redirects=False + our
    own loop), re-running _assert_public_url() at every hop before
    connecting. Returns (response, final_url); the caller is
    responsible for closing the response.

    Raises UnsafeURLError if any hop in the chain is unsafe, or if the
    chain exceeds max_redirects (an unbounded/malicious redirect loop
    otherwise wastes the socket budget the same way an infinite one
    would).
    """
    current_url = url
    for _ in range(max_redirects + 1):
        _assert_public_url(current_url)
        resp = requests.get(current_url, headers=headers, timeout=timeout, allow_redirects=False, stream=True)
        if resp.is_redirect or resp.is_permanent_redirect:
            location = resp.headers.get("Location")
            resp.close()
            if not location:
                raise UnsafeURLError("Redirect response missing Location header")
            current_url = urljoin(current_url, location)
            continue
        return resp, current_url
    raise UnsafeURLError(f"Too many redirects (> {max_redirects})")


def _read_capped(resp, max_bytes: int) -> bytes:
    """Reads a streamed response body up to `max_bytes`, aborting (and
    closing the connection) rather than buffering an unbounded amount
    of memory for a huge or malicious response."""
    chunks = []
    total = 0
    for chunk in resp.iter_content(chunk_size=65536):
        if not chunk:
            continue
        total += len(chunk)
        if total > max_bytes:
            resp.close()
            raise ValueError(f"Response exceeded {max_bytes}-byte cap")
        chunks.append(chunk)
    return b"".join(chunks)


def _abs(href: str, base: str) -> str:
    try:
        return urljoin(base, href.strip())
    except Exception:
        return ""


def _hash_text(text: str) -> str:
    if not text:
        return ""
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()


def _has_lxml() -> bool:
    try:
        import lxml  # noqa: F401
        return True
    except ImportError:
        return False


def _make_soup(html: str) -> BeautifulSoup:
    return BeautifulSoup(html, "lxml") if _has_lxml() else BeautifulSoup(html, "html.parser")


# ---------------------------------------------------------------------------
# Extractors
# ---------------------------------------------------------------------------

def _extract_text(soup: BeautifulSoup) -> str:
    for tag in soup(["script", "style", "noscript", "head", "nav", "footer"]):
        tag.decompose()
    for selector in ("main", "article", "body"):
        container = soup.find(selector)
        if container:
            text = container.get_text(separator="\n", strip=True)
            return re.sub(r"\n{3,}", "\n\n", text).strip()
    return ""


def _extract_images(soup: BeautifulSoup, base_url: str) -> list:
    images, seen = [], set()
    for tag in soup.find_all("img", src=True):
        src = _abs(tag["src"], base_url)
        if not src or src in seen or src.startswith("data:"):
            continue
        seen.add(src)
        images.append({"url": src, "alt": tag.get("alt", "").strip()})
        if len(images) >= MAX_IMAGES:
            break
    return images


def _extract_videos(soup: BeautifulSoup, base_url: str) -> list:
    videos, seen = [], set()

    for tag in soup.find_all(["video", "source"]):
        src = tag.get("src") or tag.get("data-src")
        if src:
            src = _abs(src, base_url)
            if src and src not in seen:
                seen.add(src)
                videos.append({"url": src, "type": tag.get("type", "video/*")})

    for tag in soup.find_all("iframe", src=True):
        src = _abs(tag["src"], base_url)
        if src and src not in seen and any(host in src for host in KNOWN_VIDEO_EMBED_HOSTS):
            seen.add(src)
            videos.append({"url": src, "type": "embed"})
        if len(videos) >= MAX_VIDEOS:
            break

    for tag in soup.find_all("a", href=True):
        href = _abs(tag["href"], base_url)
        if href and href.lower().endswith(VIDEO_EXTS) and href not in seen:
            seen.add(href)
            videos.append({"url": href, "type": "video/*"})

    return videos[:MAX_VIDEOS]


def _extract_links(soup: BeautifulSoup, base_url: str) -> list:
    links, seen = [], set()
    for tag in soup.find_all("a", href=True):
        href = _abs(tag["href"], base_url)
        if href and href not in seen and href.startswith(("http://", "https://")):
            seen.add(href)
            links.append({"href": href, "text": tag.get_text(strip=True)[:200]})
        if len(links) >= MAX_LINKS:
            break
    return links


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def bind_content(url: str, timeout: int = 12, max_content_bytes: int = DEFAULT_MAX_CONTENT_BYTES) -> dict:
    """
    Fetches `url` and returns one dict binding together everything
    extracted from the page:

        {
            "url": original URL requested,
            "final_url": URL after redirects,
            "fetched_at": ISO-8601 UTC timestamp,
            "text": extracted body text,
            "content_hash": sha256 of `text` (change-detection),
            "images": [{"url", "alt"}, ...],
            "videos": [{"url", "type"}, ...],
            "links":  [{"href", "text"}, ...],
            "counts": {"images": N, "videos": N, "links": N},
            "error": None, or a string describing why extraction failed,
        }

    Never raises for ordinary fetch/parse failures (bad status, timeout,
    non-HTML content, oversized response) — those go into result["error"]
    so a batch of URLs can be processed without one bad URL stopping
    the rest.

    Does raise UnsafeURLError if `url` (or any hop it redirects
    through) resolves to a private/loopback/link-local address —
    that's treated as a security-relevant condition the caller must
    see, not a network condition to swallow into result["error"].
    """
    result = {
        "url": url,
        "final_url": url,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "text": "",
        "content_hash": "",
        "images": [],
        "videos": [],
        "links": [],
        "counts": {"images": 0, "videos": 0, "links": 0},
        "error": None,
    }

    resp = None
    try:
        resp, final_url = _safe_get(url, DEFAULT_HEADERS, timeout)
        result["final_url"] = final_url

        if resp.status_code >= 400:
            result["error"] = f"HTTP {resp.status_code}"
            return result

        content_type = resp.headers.get("Content-Type", "")
        if "text/html" not in content_type and "application/xhtml" not in content_type:
            result["error"] = f"Non-HTML content-type: {content_type or 'unknown'}"
            return result

        raw = _read_capped(resp, max_content_bytes)
    except UnsafeURLError:
        raise
    except (requests.RequestException, ValueError) as exc:
        result["error"] = str(exc)
        return result
    finally:
        if resp is not None:
            resp.close()

    encoding = resp.encoding or "utf-8"
    html_text = raw.decode(encoding, errors="replace")

    soup = _make_soup(html_text)
    base_url = result["final_url"]

    result["text"] = _extract_text(soup)
    result["images"] = _extract_images(soup, base_url)
    result["videos"] = _extract_videos(soup, base_url)
    result["links"] = _extract_links(soup, base_url)
    result["content_hash"] = _hash_text(result["text"])
    result["counts"] = {
        "images": len(result["images"]),
        "videos": len(result["videos"]),
        "links": len(result["links"]),
    }
    return result


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Fetch a URL and bind its extracted images, videos, links, and text together."
    )
    parser.add_argument("url", help="URL to fetch and extract")
    parser.add_argument("--json", metavar="FILE", help="Write result as JSON to FILE instead of stdout")
    parser.add_argument("--timeout", type=int, default=12, help="Request timeout in seconds (default: 12)")
    parser.add_argument(
        "--max-bytes", type=int, default=DEFAULT_MAX_CONTENT_BYTES,
        help=f"Abort if the response body exceeds this many bytes (default: {DEFAULT_MAX_CONTENT_BYTES})",
    )
    args = parser.parse_args()

    try:
        data = bind_content(args.url, timeout=args.timeout, max_content_bytes=args.max_bytes)
    except UnsafeURLError as exc:
        print(f"Refusing to fetch: {exc}", file=sys.stderr)
        sys.exit(1)

    output = json.dumps(data, indent=2, ensure_ascii=False)
    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            f.write(output)
        print(f"Wrote {args.json}")
    else:
        print(output)


if __name__ == "__main__":
    main()