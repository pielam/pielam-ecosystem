# megamind/utils/image_analyzer.py

"""
image_analyzer.py

Ranks and filters extracted images using their REAL dimensions/file
size, not the width/height HTML attributes (which are frequently
missing, wrong, or CSS-driven placeholders on modern sites).

content_extractor.py's tracking-pixel filter only catches width="1"
height="1" when that attribute is actually present — most tracking
pixels and tiny icons in the wild have no width/height attribute at
all, so they sail through as "real" images. This module fetches just
enough of each image (a small byte range, not the whole file, so
scoring 20 images stays cheap) to read the real dimensions from the
file header, then ranks by pixel area — the same signal a human uses
to tell "hero photo" from "footer icon".

    from image_analyzer import rank_images
    ranked = rank_images(images, max_to_check=15)
    # ranked[0] is the largest real image found

    pip install requests --break-system-packages
"""

from __future__ import annotations

import logging
import struct
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Dict, List, Optional, Tuple

import requests

from megamind.utils.http_fetcher import assert_public_url, DEFAULT_HEADERS, UnsafeURLError

logger = logging.getLogger(__name__)

# Bytes needed to read dimensions from common formats' headers. PNG/GIF
# store dimensions in the first ~30 bytes; JPEG requires walking marker
# segments so needs more; a generous cap keeps the request cheap either way.
_HEADER_FETCH_BYTES = 65536
_REQUEST_TIMEOUT = 5

MIN_CONTENT_WIDTH = 100   # below this, treat as icon/decoration, not content
MIN_CONTENT_HEIGHT = 100
MIN_CONTENT_AREA = 100 * 100

Dimensions = Tuple[int, int]


def _get_image_dimensions(data: bytes) -> Optional[Dimensions]:
    """Parses PNG/GIF/JPEG/WEBP headers to get (width, height) without
    a full image-decoding library. Returns None if the format isn't
    recognized or the header is truncated/malformed."""
    if len(data) < 10:
        return None

    try:
        # PNG
        if data[:8] == b"\x89PNG\r\n\x1a\n":
            if len(data) < 24:
                return None
            width, height = struct.unpack(">II", data[16:24])
            return width, height

        # GIF
        if data[:6] in (b"GIF87a", b"GIF89a"):
            if len(data) < 10:
                return None
            width, height = struct.unpack("<HH", data[6:10])
            return width, height

        # JPEG — walk marker segments looking for an SOFn frame
        if data[:2] == b"\xff\xd8":
            i = 2
            while i < len(data) - 9:
                if data[i] != 0xFF:
                    break
                marker = data[i + 1]
                if marker in (0xC0, 0xC1, 0xC2, 0xC3):  # SOF0-3 markers carry dimensions
                    height, width = struct.unpack(">HH", data[i + 5:i + 9])
                    return width, height
                if marker in (0xD8, 0xD9):  # SOI/EOI, no length field
                    i += 2
                    continue
                seg_len = struct.unpack(">H", data[i + 2:i + 4])[0]
                i += 2 + seg_len
            return None

        # WEBP (VP8 lossy)
        if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
            chunk = data[12:16]
            if chunk == b"VP8 " and len(data) >= 30:
                width = struct.unpack("<H", data[26:28])[0] & 0x3FFF
                height = struct.unpack("<H", data[28:30])[0] & 0x3FFF
                return width, height
            if chunk == b"VP8L" and len(data) >= 25:
                b = data[21:25]
                width = (b[0] | ((b[1] & 0x3F) << 8)) + 1
                height = ((b[1] >> 6) | (b[2] << 2) | ((b[3] & 0xF) << 10)) + 1
                return width, height

        return None
    except struct.error:
        # Truncated/malformed header that slipped past the length
        # checks above (e.g. a JPEG marker claiming a segment length
        # that runs past the end of the buffer) — treat like "unrecognized"
        # rather than propagating.
        return None


def probe_image(image: Dict[str, Any], timeout: int = _REQUEST_TIMEOUT) -> Dict[str, Any]:
    """
    Fetches a small byte range of image["url"] and enriches the dict
    in place with real_width/real_height/probe_error (if any). Never
    raises — a failed probe just leaves dimensions unset so the caller
    can still fall back to whatever the HTML attributes said.
    """
    enriched = dict(image)
    url = image.get("url")
    if not url:
        enriched["probe_error"] = "no url"
        return enriched

    resp = None
    try:
        assert_public_url(url)
        resp = requests.get(
            url,
            headers={**DEFAULT_HEADERS, "Range": f"bytes=0-{_HEADER_FETCH_BYTES - 1}"},
            timeout=timeout,
            stream=True,
        )
        data = resp.raw.read(_HEADER_FETCH_BYTES)
        dims = _get_image_dimensions(data)
        if dims:
            enriched["real_width"], enriched["real_height"] = dims
            enriched["real_area"] = dims[0] * dims[1]
        else:
            enriched["probe_error"] = "unrecognized format or truncated header"
    except UnsafeURLError as exc:
        enriched["probe_error"] = f"blocked: {exc}"
    except requests.RequestException as exc:
        enriched["probe_error"] = str(exc)
    except Exception as exc:
        # Belt-and-braces: this function's whole contract is "never
        # raises" (rank_images fans this out across a thread pool, and
        # one unexpected exception type here shouldn't drop every other
        # image's result). Anything not already handled above still
        # gets recorded as a probe error instead of propagating.
        logger.warning("probe_image: unexpected error probing %s: %s", url, exc)
        enriched["probe_error"] = f"unexpected error: {exc}"
    finally:
        if resp is not None:
            resp.close()

    return enriched


def rank_images(
    images: Optional[List[Dict[str, Any]]],
    max_to_check: int = 15,
    max_workers: int = 6,
    min_area: int = MIN_CONTENT_AREA,
) -> List[Dict[str, Any]]:
    """
    Probes up to `max_to_check` images concurrently (real network calls
    are the slow part, so this is worth parallelizing even for a
    single-URL extraction) and returns them sorted largest-real-area
    first. Images below `min_area` (default 100x100) are demoted to
    the end rather than dropped, since a probe failure shouldn't
    silently delete a real image the HTML attributes already vouched for.

    Un-probed images (beyond max_to_check) are appended at the end in
    their original order.
    """
    images = images or []
    to_check = images[:max_to_check] if max_to_check > 0 else []
    remainder = images[len(to_check):]

    probed: List[Dict[str, Any]] = []
    if to_check:
        with ThreadPoolExecutor(max_workers=max(1, max_workers)) as pool:
            futures = {pool.submit(probe_image, img): img for img in to_check}
            for future in as_completed(futures):
                original = futures[future]
                try:
                    probed.append(future.result())
                except Exception as exc:
                    # probe_image is designed not to raise, but if a
                    # future itself fails (e.g. pool shutdown mid-flight)
                    # fall back to the original, un-probed entry rather
                    # than losing the image from the results entirely.
                    logger.warning("rank_images: probe future failed for %s: %s", original.get("url"), exc)
                    probed.append(dict(original, probe_error=f"probe failed: {exc}"))

    def _sort_key(img: Dict[str, Any]) -> Tuple[bool, int]:
        area = img.get("real_area", 0) or 0
        is_small_or_unknown = area < min_area
        return (is_small_or_unknown, -area)

    probed.sort(key=_sort_key)
    return probed + remainder