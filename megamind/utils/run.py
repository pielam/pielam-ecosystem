# megamind/utils/run.py

"""
run.py

Fetch a single URL and extract exactly four things: images, videos,
links, text.

CLI:
    python run.py https://example.com
    python run.py https://example.com --json out.json
    python run.py https://example.com --no-video-resolve   # skip resolving TikTok/Twitter short links (faster)

Library:
    from megamind.utils.run import extract_url
    result = extract_url("https://example.com")
    result["images"], result["videos"], result["links"], result["text"]

Return shape (always this, never more):
    {
        "url": "<original url>",
        "final_url": "<url after redirects>",
        "images": [{"url", "alt", "href"}, ...],
        "videos": [{"platform", "embed_url", "watch_url", "thumbnail",
                     "type", "url", "source_tag", ...}, ...],
        "links": [{"href", "text"}, ...],
        "text": "<main content text>",
        "error": None or "<fetch-level error>",
        "warnings": [...],
    }

`error` is only set if the URL couldn't be fetched at all after every
retry/UA fallback. Individual extraction failures show up in
`warnings` instead — you always get back whatever could be extracted.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from typing import Any, Dict, Optional

try:  # pragma: no cover - import shape depends on call context
    # Package context (normal Django usage): megamind.utils.run
    from .http_fetcher import resilient_get, UnsafeURLError
    from .extractor import extract_all
except ImportError:  # pragma: no cover
    # Script context: `python run.py ...` run directly from this directory
    from http_fetcher import resilient_get, UnsafeURLError
    from extractor import extract_all

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT_SECONDS = 12


def _empty_result(url: str) -> Dict[str, Any]:
    return {
        "url": url,
        "final_url": url,
        "images": [],
        "videos": [],
        "links": [],
        "text": "",
        "error": None,
        "warnings": [],
    }


def extract_url(
    url: str,
    resolve_video_short_links: bool = True,
    timeout: int = DEFAULT_TIMEOUT_SECONDS,
) -> Dict[str, Any]:
    """
    Fetch *url* and extract images/videos/links/text.

    Never raises for fetch or extraction problems — those are reported
    via the `error`/`warnings` keys in the returned dict instead, so
    callers (e.g. management commands, sync jobs) can rely on always
    getting a well-shaped result back.
    """
    if not url or not isinstance(url, str):
        result = _empty_result(url if isinstance(url, str) else "")
        result["error"] = "Invalid or empty URL"
        return result

    result = _empty_result(url)

    try:
        raw_bytes, resp_headers, final_url, fetch_error = resilient_get(url, timeout=timeout)
    except UnsafeURLError as exc:
        result["error"] = f"Blocked (unsafe URL): {exc}"
        return result
    except Exception as exc:
        # Any unexpected failure inside the fetcher should still surface
        # as a normal "couldn't fetch" result rather than propagating
        # and taking down the caller.
        logger.warning("run.extract_url: unexpected fetch error for %s: %s", url, exc)
        result["error"] = f"Unexpected fetch error: {exc}"
        return result

    result["final_url"] = final_url or url

    if raw_bytes is None:
        result["error"] = fetch_error or "Fetch failed for unknown reason"
        return result

    headers_lower = {k.lower(): v for k, v in (resp_headers or {}).items()}
    content_type = headers_lower.get("content-type", "")
    if "text/html" not in content_type and "application/xhtml" not in content_type:
        result["warnings"].append(f"Non-HTML content-type '{content_type}' — nothing to extract")
        return result

    encoding: Optional[str] = None
    if "charset=" in content_type:
        encoding = content_type.split("charset=")[-1].split(";")[0].strip()

    try:
        html_text = raw_bytes.decode(encoding or "utf-8", errors="replace")
    except (LookupError, TypeError) as exc:
        # Unknown/garbled charset name in the header — fall back to utf-8
        # rather than raising, since `errors="replace"` alone won't save
        # us from an invalid codec name.
        logger.debug("run.extract_url: bad charset %r for %s (%s), falling back to utf-8", encoding, url, exc)
        html_text = raw_bytes.decode("utf-8", errors="replace")

    try:
        extracted = extract_all(html_text, result["final_url"], resolve_video_short_links=resolve_video_short_links)
    except Exception as exc:
        logger.warning("run.extract_url: extraction failed for %s: %s", url, exc)
        result["warnings"].append(f"Extraction failed: {exc}")
        return result

    result["images"] = extracted.get("images", [])
    result["videos"] = extracted.get("videos", [])
    result["links"] = extracted.get("links", [])
    result["text"] = extracted.get("text", "")
    result["warnings"].extend(extracted.get("warnings", []))

    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="Extract images, videos, links, and text from a single URL.")
    parser.add_argument("url", help="URL to fetch and extract from")
    parser.add_argument("--json", metavar="PATH", help="Write result as JSON to this path instead of stdout")
    parser.add_argument(
        "--no-video-resolve",
        action="store_true",
        help="Skip resolving TikTok/Twitter short links before classifying video embeds (faster, less accurate)",
    )
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT_SECONDS, help="Fetch timeout in seconds (default: 12)")
    args = parser.parse_args()

    result = extract_url(args.url, resolve_video_short_links=not args.no_video_resolve, timeout=args.timeout)

    output = json.dumps(result, indent=2, ensure_ascii=False)
    if args.json:
        try:
            with open(args.json, "w", encoding="utf-8") as f:
                f.write(output)
        except OSError as exc:
            print(f"Could not write to {args.json}: {exc}", file=sys.stderr)
            sys.exit(2)
        print(f"Wrote result to {args.json}")
    else:
        print(output)

    if result["error"]:
        sys.exit(1)


if __name__ == "__main__":
    main()