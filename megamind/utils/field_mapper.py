# megamind/utils/field_mapper.py

"""


Runs the full pipeline (fetch + content extraction + extended
metadata) for one URL and returns a flat dict whose keys match the
ConnectedService model's field names exactly — so the result can be
handed straight to `ConnectedService(**mapped)` / `.objects.create(**mapped)`
/ `.objects.update_or_create(defaults=mapped)` without any manual
field-by-field wiring, whether or not Django is actually installed in
this environment (this module has no Django dependency itself).

    from field_mapper import extract_for_model
    fields = extract_for_model(url)
    # fields.keys() ⊆ ConnectedService's own field names

Fields intentionally left out because they only make sense inside a
real crawl-queue/worker system (not derivable from a single one-off
fetch): user, profile, organization_id, domain_policy, crawl_priority,
crawl_frequency_minutes, next_crawl_at, lock_id/locked_at/locked_by,
retry_count/circuit_breaker_*, created_by. Set those yourself before
saving.
"""

from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, Optional
from urllib.parse import urlparse

try:  # pragma: no cover - import shape depends on call context
    from .http_fetcher import resilient_get, UnsafeURLError
    from .content_extractor import extract_all, make_soup
    from .metadata_extractor import extract_extended_metadata
    from .readability_extractor import extract_main_content, estimate_reading_time
except ImportError:  # pragma: no cover
    from http_fetcher import resilient_get, UnsafeURLError
    from content_extractor import extract_all, make_soup
    from metadata_extractor import extract_extended_metadata
    from readability_extractor import extract_main_content, estimate_reading_time

logger = logging.getLogger(__name__)


def _hash_text(text: str) -> str:
    if not text:
        return ""
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()


def _get_header(resp_headers: Dict[str, str], name: str, default: str = "") -> str:
    for key, value in (resp_headers or {}).items():
        if key.lower() == name.lower():
            return value
    return default


def _to_decimal(value: Any) -> Optional[Decimal]:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None


def _to_int(value: Any) -> Optional[int]:
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (ValueError, TypeError):
        # e.g. rating_count scraped as "5 reviews" or similar junk —
        # this previously raised straight out of extract_for_model,
        # discarding an otherwise fully-successful extraction over one
        # bad structured-data field.
        return None


def extract_for_model(
    url: str,
    timeout: int = 12,
    use_readability: bool = True,
    resolve_video_short_links: bool = True,
) -> Dict[str, Any]:
    """
    Fetches `url` once and returns a dict of ConnectedService-shaped
    fields. Always returns a dict — on total fetch failure, only
    service_url/domain/fetch_status='error'/fetch_error(_category) are
    set, matching how ConnectedService.mark_fetch_error() leaves a row
    (see apps/customer/services/connect_url.py's "never raises, always
    creates the row" contract). The same applies if extraction itself
    fails after a successful fetch (malformed page content) — that's
    also reported via fetch_status='error' rather than raising, so a
    single page that trips up the extractor doesn't take down whatever
    batch job called this.

    Raises UnsafeCrawlURLError-equivalent (http_fetcher.UnsafeURLError)
    up front for a URL that resolves to a private/internal address —
    that's a caller bug matching validate_crawl_url()'s behavior in the
    model file, not a fetch condition to record on the row.
    """
    domain = urlparse(url).netloc

    base_fields = {
        "service_url": url,
        "domain": domain,
        "last_fetch_time": datetime.now(timezone.utc).isoformat(),
    }

    raw_bytes, resp_headers, final_url, fetch_error = resilient_get(url, timeout=timeout)
    if raw_bytes is None:
        return {
            **base_fields,
            "fetch_status": "error",
            "fetch_error": (fetch_error or "")[:5000],
            "fetch_error_category": "connection_error",
            "is_connected": False,
        }

    content_type = _get_header(resp_headers, "Content-Type")
    if "text/html" not in content_type and "application/xhtml" not in content_type:
        return {
            **base_fields,
            "fetch_status": "error",
            "fetch_error": f"Non-HTML content-type: {content_type or 'unknown'}",
            "fetch_error_category": "parse_error",
            "content_type": content_type,
            "is_connected": False,
        }

    encoding = None
    if "charset=" in content_type:
        encoding = content_type.split("charset=")[-1].split(";")[0].strip()
    try:
        html_text = raw_bytes.decode(encoding or "utf-8", errors="replace")
    except (LookupError, UnicodeDecodeError):
        html_text = raw_bytes.decode("utf-8", errors="replace")

    try:
        return _build_fields_from_html(
            base_fields, html_text, final_url, raw_bytes, resp_headers, content_type,
            use_readability=use_readability, resolve_video_short_links=resolve_video_short_links,
        )
    except Exception as exc:
        # Extraction/metadata parsing blew up on something in the page
        # content itself (not the fetch, which already succeeded). Per
        # this function's "always returns a dict" contract, that's a
        # recorded row state, not an unhandled exception.
        logger.exception("field_mapper: extraction pipeline failed for %s", final_url or url)
        return {
            **base_fields,
            "fetch_status": "error",
            "fetch_error": f"Extraction failed: {exc}"[:5000],
            "fetch_error_category": "parse_error",
            "content_type": content_type,
            "is_connected": False,
        }


def _build_fields_from_html(
    base_fields: Dict[str, Any],
    html_text: str,
    final_url: str,
    raw_bytes: bytes,
    resp_headers: Dict[str, str],
    content_type: str,
    *,
    use_readability: bool,
    resolve_video_short_links: bool,
) -> Dict[str, Any]:
    extracted = extract_all(html_text, final_url, resolve_video_short_links=resolve_video_short_links)
    soup = make_soup(html_text)
    extended = extract_extended_metadata(soup, final_url, extracted["structured_data"], resp_headers)

    text = extracted["text"]
    word_count = len(text.split()) if text else 0
    reading_time = estimate_reading_time(text)

    if use_readability:
        try:
            readable_text, _debug = extract_main_content(make_soup(html_text))
        except Exception as exc:
            logger.debug("field_mapper: readability pass failed for %s: %s", final_url, exc)
            readable_text = None
        if readable_text and len(readable_text) > len(text) * 0.3:  # sanity floor — don't swap in something suspiciously thin
            text = readable_text
            word_count = len(text.split())
            reading_time = estimate_reading_time(text)

    og = extracted["og"]

    fields = {
        **base_fields,
        "fetch_status": "success",
        "fetch_error": "; ".join(extracted["warnings"]) or None,
        "fetch_error_category": "none",
        "is_connected": True,
        "http_status_code": 200,
        "content_type": content_type,
        "response_headers": dict(resp_headers or {}),
        "page_size_bytes": len(raw_bytes),

        "og_title": og.get("title") or "",
        "og_description": og.get("description") or "",
        "og_thumbnail": og.get("thumbnail") or "",
        "og_site_name": og.get("site_name") or "",
        "og_type": og.get("type") or "",
        "og_locale": extended.get("og_locale") or "",
        "og_video": extended.get("og_video") or "",
        "og_audio": extended.get("og_audio") or "",

        "twitter_card": extended.get("twitter_card") or "",
        "twitter_title": extended.get("twitter_title") or "",
        "twitter_description": extended.get("twitter_description") or "",
        "twitter_image": extended.get("twitter_image") or "",
        "twitter_site": extended.get("twitter_site") or "",
        "twitter_creator": extended.get("twitter_creator") or "",

        "page_title": extended.get("page_title") or "",
        "meta_description": extended.get("meta_description") or "",
        "meta_keywords": extended.get("meta_keywords") or "",
        "meta_generator": extended.get("meta_generator") or "",
        "charset": extended.get("charset") or "",
        "content_language": extended.get("content_language") or "",
        "theme_color": extended.get("theme_color") or "",
        "viewport": extended.get("viewport") or "",

        "canonical_url": extracted["canonical_url"] or "",
        "favicon": extracted["favicon"] or "",
        "apple_touch_icon": extended.get("apple_touch_icon") or "",
        "manifest_url": extended.get("manifest_url") or "",
        "amp_url": extended.get("amp_url") or "",
        "rss_feed_url": extended.get("rss_feed_url") or "",
        "atom_feed_url": extended.get("atom_feed_url") or "",
        "author": extracted["author"] or "",
        "author_url": extended.get("author_url") or "",
        "published_time": extracted["published_time"] or "",
        "modified_time": extended.get("modified_time") or "",
        "article_section": extended.get("article_section") or "",
        "article_tags": extended.get("article_tags") or [],
        "category_breadcrumb": extended.get("category_breadcrumb") or [],
        "structured_data": extracted["structured_data"] or [],
        "alternate_languages": extended.get("alternate_languages") or [],
        "same_as_links": extended.get("same_as_links") or [],

        "price_amount": _to_decimal(extended.get("price_amount")),
        "price_currency": extended.get("price_currency"),
        "price_original": _to_decimal(extended.get("price_original")),
        "availability": extended.get("availability"),
        "product_sku": extended.get("product_sku"),
        "product_gtin": extended.get("product_gtin"),
        "product_condition": extended.get("product_condition"),
        "brand_name": extended.get("brand_name"),
        "seller_name": extended.get("seller_name"),
        "rating_value": _to_decimal(extended.get("rating_value")),
        "rating_count": _to_int(extended.get("rating_count")),

        "robots_meta": extended.get("robots_meta") or "",
        "x_robots_tag": extended.get("x_robots_tag") or "",
        "is_indexable": extended.get("is_indexable"),

        "extracted_images": extracted["images"] or [],
        "extracted_videos": extracted["videos"] or [],
        "extracted_links": extracted["links"] or [],
        "extracted_headings": extracted["headings"] or {},
        "extracted_text": text,
        "word_count": word_count,
        "reading_time_minutes": reading_time,
        "content_hash": _hash_text(text),

        "last_fetched_data": {
            "og": og, "canonical_url": extracted["canonical_url"], "favicon": extracted["favicon"],
            "product_info": extracted["product_info"], "text_snippet": text[:500],
        },
    }
    return fields