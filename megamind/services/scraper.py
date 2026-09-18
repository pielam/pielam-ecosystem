"""
megamind/services/scraper.py

Resilient, best-effort, "universal" URL scraper.

Design principle: scrape_url() NEVER raises. Every extractor is
wrapped individually so one bad selector / malformed tag can't take
the rest of the page down with it — you always get back whatever
could be extracted, plus a non-fatal `warnings` list for anything that
failed, and `error` is only set when literally nothing could be
fetched at all (dead host, timeout after every retry/UA/SSL fallback
in resilient_fetch.py).

"Universal" here means the result dict (and scrape_and_store's mapping
of it onto ConnectedService) covers every metadata surface a page can
reasonably expose, not just OG tags: Twitter Cards, page-level <meta>
(title/description/keywords/generator/charset/language/theme-color/
viewport), icons and feeds (apple-touch-icon, web manifest, AMP,
RSS/Atom), robots directives (both <meta name=robots> and the
X-Robots-Tag response header) with a derived is_indexable, article
metadata (section/tags/modified-time/author URL), hreflang alternates,
JSON-LD `sameAs` links, JSON-LD BreadcrumbList (falling back to a
`.breadcrumb`-ish DOM block), and an extended product_info block (sku,
gtin, condition, seller, rating) on top of what was already extracted
(name/brand/price/currency/availability). Every one of these has a
corresponding column on ConnectedService — see the field-by-field
mapping in scrape_and_store.

JS-rendered pages: the plain-HTTP pass is tried first (cheap, fast).
If it "succeeds" but comes back suspiciously thin (empty React/Vue
shell), we fall back to a real headless-browser render via
playwright_fallback.py — see `_maybe_render_with_browser` below. That
fallback only fills in fields the plain-HTTP pass came back empty on;
it never clobbers real server-rendered <head> metadata (OG tags,
JSON-LD, canonical) that a lot of SPAs still emit correctly for SEO
even when the visible body needs JS.

FIX LOG (this revision):
  * `_extract_canonical` now matches the <link rel="canonical"> tag
    via `_rel_tokens()` (exact token membership) instead of a raw
    substring check on `rel`. BeautifulSoup normally hands back `rel`
    as a list (["canonical"]), so `"canonical" in rel` behaved as a
    token check by accident — but in the parser/attr combinations
    where `rel` comes back as a plain string instead (the same
    footgun `_extract_favicon`'s docstring already documents for
    "icon" vs "apple-touch-icon"/"shortcut icon"), the same check
    silently became a substring check and could match an unrelated
    rel value that happens to contain "canonical" as a substring,
    writing the wrong URL into canonical_url with no warning raised.
    Routing through `_rel_tokens()` (already used by
    `_extract_favicon`, `_extract_alternate_languages`, and
    `_extract_article_meta`) makes canonical-link matching consistent
    with the rest of this module and closes that gap.

  * The `should_attempt_render(...)` call now also passes
    `result["images"]`, so a page whose plain-HTTP pass came back with
    plenty of text/links but zero images (manga/comic reader sites,
    galleries, infinite-scroll feeds that inject their actual images
    via client-side JS after load) correctly triggers the headless-
    browser fallback instead of being treated as a fully server-
    rendered success. See browser_render.py's updated
    should_attempt_render() for the new heuristic.
"""

import hashlib
import json
import re
import logging
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from urllib.parse import urljoin, urlparse
from typing import Union

from bs4 import BeautifulSoup
from django.conf import settings
from django.utils import timezone as dj_timezone

from megamind.utils.http_client import DEFAULT_USER_AGENT
from megamind.utils.resilient_fetch import resilient_get
from megamind.utils.media_info import is_probably_tracking_pixel
from megamind.utils.video_info import get_video_info_cached
from megamind.utils.browser_render import should_attempt_render, render_with_browser

# NOTE: import path assumed to match connect_url.py / product_sync.py
# (apps.customer.models.connected_service). If this deployment's model
# actually lives under megamind.models.connected_service instead (see
# connected_service_sync.py), update this one line — everything else in
# this file is agnostic to which app owns the model.
from megamind.models.connected_service import (
    CrawlAttempt,
    DomainCrawlPolicy,
    UnsafeCrawlURLError,
    validate_crawl_url,
)

logger = logging.getLogger(__name__)

DEFAULT_HEADERS = {
    "User-Agent": getattr(settings, "SCRAPER_USER_AGENT", DEFAULT_USER_AGENT),
    "Accept-Language": "en-US,en;q=0.9",
}
MAX_IMAGES = 50
MAX_VIDEOS = 20
MAX_LINKS = 100
WORDS_PER_MINUTE = 200

# rel="icon" / "shortcut icon" / "apple-touch-icon", in preference
# order. Matched via exact token membership (see _rel_tokens /
# _extract_favicon), not substring — "icon" is a substring of
# "apple-touch-icon" and "shortcut icon", so a naive `in` check on the
# joined rel string collapses all three into whichever tag happens to
# appear first in the document, silently defeating this preference
# order.
_FAVICON_RELS_IN_PRIORITY = ("icon", "shortcut icon", "apple-touch-icon")

# Section-heading keywords used to locate a specs/features block when
# JSON-LD doesn't provide one.
_SPEC_KEYWORDS = ("specification", "specs", "features", "details", "technical")


# ---------------------------------------------------------------------------
# Safety wrapper — turns "extractor threw" into "warning + empty default"
# ---------------------------------------------------------------------------

def _safe(fn, default, warnings, label, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except Exception as exc:
        logger.debug("scrape_url: %s failed: %s", label, exc)
        warnings.append(f"{label}: {exc}")
        return default


def _backfill(dest: dict, src: dict) -> None:
    """Fill only the empty/falsy keys of `dest` from `src`, in place.
    Used by the browser-render fallback so it only supplements what the
    plain-HTTP pass came back empty on, never overwrites real data."""
    for key, value in (src or {}).items():
        if value and not dest.get(key):
            dest[key] = value


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def scrape_url(
    url: str,
    api_key: Union[str, None] = None,
    auth_token: Union[str, None] = None,
    extra_headers: Union[dict, None] = None,
) -> dict:
    """
    Fetch and parse *url*. NEVER raises.

    Always returns a dict (see _empty_result). `error` is set only if
    the URL could not be fetched at all after every retry/UA/SSL
    fallback. `warnings` lists any individual field extractions that
    failed but didn't stop the rest of the scrape.

    `extra_headers` lets callers pass conditional-GET headers
    (If-None-Match / If-Modified-Since) built from a previously stored
    ETag/Last-Modified — see scrape_and_store. This function doesn't
    special-case a 304 response itself (resilient_get doesn't currently
    surface the status code, only raw_bytes/headers/error), so a 304
    still comes back as a normal "fetched, but body is empty/unchanged"
    result. If resilient_get is extended to expose the status code,
    wire a fast path here instead of re-parsing an unchanged page.

    SSRF note: callers (scrape_and_store) are responsible for calling
    validate_crawl_url() before invoking this function. This function
    does not re-validate itself so it stays usable for one-off/manual
    scrapes where the caller has already made that decision, but it
    means this must never be called directly on unvalidated user input.
    """
    result = _empty_result()
    warnings = result["warnings"]
    headers = dict(DEFAULT_HEADERS)

    if auth_token:
        headers["Authorization"] = f"Bearer {auth_token}"
    if api_key:
        headers["X-Api-Key"] = api_key
    if extra_headers:
        headers.update(extra_headers)

    raw_bytes, resp_headers, final_url, fetch_error = resilient_get(url, headers)
    resp_headers = resp_headers or {}

    result["etag"] = resp_headers.get("ETag") or resp_headers.get("etag") or ""
    result["last_modified_header"] = (
        resp_headers.get("Last-Modified") or resp_headers.get("last-modified") or ""
    )
    result["response_headers"] = dict(resp_headers)

    if raw_bytes is None:
        result["error"] = fetch_error or "Fetch failed for unknown reason"
        return result

    content_type = resp_headers.get("Content-Type", "")
    result["content_type"] = content_type
    result["page_size_bytes"] = len(raw_bytes)
    encoding = _guess_encoding(resp_headers)

    # Non-HTML content: still return something useful instead of
    # erroring out — size/type metadata plus raw text if it decodes.
    if "text/html" not in content_type and "application/xhtml" not in content_type:
        result["canonical_url"] = final_url or url
        result["raw"] = {"content_type": content_type, "size_bytes": len(raw_bytes)}
        try:
            result["text"] = raw_bytes.decode(encoding or "utf-8", errors="replace")[:5000]
        except Exception:
            pass
        result["fetched_at"] = datetime.now(timezone.utc).isoformat()
        result["content_hash"] = _hash_content(result.get("text", ""))
        warnings.append(f"Non-HTML content-type '{content_type}' — returning metadata only")
        return result

    html_text = _safe(
        lambda: raw_bytes.decode(encoding or "utf-8", errors="replace"),
        raw_bytes.decode("utf-8", errors="replace"),
        warnings, "decode_text",
    )

    soup = _safe(lambda: BeautifulSoup(html_text, "lxml"), None, warnings, "parse_lxml")
    if soup is None:
        # lxml can choke on badly malformed markup — fall back to the
        # stdlib parser before giving up on structured extraction.
        soup = _safe(lambda: BeautifulSoup(html_text, "html.parser"), None, warnings, "parse_html_parser")

    if soup is None:
        # Total parse failure: still return raw text so the caller has
        # *something*, rather than an all-empty result.
        result["canonical_url"] = final_url or url
        result["text"] = re.sub(r"<[^>]+>", " ", html_text)[:5000].strip()
        result["word_count"] = len(result["text"].split())
        result["fetched_at"] = datetime.now(timezone.utc).isoformat()
        result["content_hash"] = _hash_content(result["text"])
        warnings.append("HTML parsing failed entirely — returned stripped-tag text fallback")
        return result

    base_url = _safe(_base_url, final_url or url, warnings, "base_url", soup, final_url or url)

    structured_data = _safe(_extract_json_ld, [], warnings, "json_ld", soup)

    result["og"] = _safe(_extract_og, {}, warnings, "og", soup, structured_data)
    result["twitter"] = _safe(_extract_twitter, {}, warnings, "twitter", soup)
    result["meta"] = _safe(_extract_page_meta, {}, warnings, "page_meta", soup)
    result["icons_feeds"] = _safe(_extract_icons_and_feeds, {}, warnings, "icons_feeds", soup, base_url)
    result["robots"] = _safe(
        _extract_robots_meta, {"robots_meta": "", "x_robots_tag": "", "is_indexable": None},
        warnings, "robots_meta", soup, resp_headers,
    )
    result["article"] = _safe(_extract_article_meta, {}, warnings, "article_meta", soup, base_url)
    result["alternate_languages"] = _safe(_extract_alternate_languages, [], warnings, "alt_languages", soup, base_url)
    result["same_as"] = _safe(_extract_same_as, [], warnings, "same_as", structured_data)
    result["category_breadcrumb"] = _safe(_extract_breadcrumb, [], warnings, "breadcrumb", structured_data, soup)

    result["canonical_url"] = _safe(_extract_canonical, final_url or url, warnings, "canonical", soup, final_url or url)
    result["favicon"] = _safe(_extract_favicon, "", warnings, "favicon", soup, base_url)
    result["author"] = _safe(_extract_author, "", warnings, "author", soup)
    result["published_time"] = _safe(_extract_published_time, "", warnings, "published_time", soup)
    result["headings"] = _safe(_extract_headings, {"h1": [], "h2": [], "h3": []}, warnings, "headings", soup)
    result["structured_data"] = structured_data
    result["product_info"] = _safe(_extract_product_info, {}, warnings, "product_info", structured_data, soup)
    result["images"] = _safe(_extract_images, [], warnings, "images", soup, base_url)
    result["videos"] = _safe(_extract_videos, [], warnings, "videos", soup, base_url)
    result["links"] = _safe(_extract_links, [], warnings, "links", soup, base_url)
    result["text"] = _safe(_extract_text, "", warnings, "text", soup)

    # If the primary text extraction came back empty (e.g. an
    # unfamiliar page structure with no <main>/<article>/<body>),
    # fall back to a raw stripped-tag dump rather than leaving it blank.
    if not result["text"]:
        result["text"] = _safe(
            lambda: re.sub(r"\s+", " ", soup.get_text(" ", strip=True))[:5000],
            "", warnings, "text_fallback",
        )

    # JS-rendered SPA fallback. Only fires when the plain-HTTP pass
    # "succeeded" (no fetch error) but text/links came back thin — the
    # classic signature of an unrendered React/Vue shell. Costs a real
    # headless-browser launch, so it's the last thing tried, not the
    # first.
    if should_attempt_render(result["text"], result["links"], result["images"]):
        _safe(
            _apply_browser_render, None, warnings, "playwright_render",
            result, final_url or url, headers["User-Agent"],
        )

    result["word_count"] = len(result["text"].split()) if result["text"] else 0
    result["reading_time_minutes"] = (
        max(1, round(result["word_count"] / WORDS_PER_MINUTE)) if result["word_count"] else 0
    )

    result["raw"] = {
        "og": result["og"],
        "twitter": result["twitter"],
        "meta": result["meta"],
        "icons_feeds": result["icons_feeds"],
        "robots": result["robots"],
        "article": result["article"],
        "alternate_languages": result["alternate_languages"],
        "same_as": result["same_as"],
        "category_breadcrumb": result["category_breadcrumb"],
        "canonical_url": result["canonical_url"],
        "favicon": result["favicon"],
        "author": result["author"],
        "published_time": result["published_time"],
        "headings": result["headings"],
        "product_info": result["product_info"],
        "images": result["images"],
        "videos": result["videos"],
        "links": result["links"],
        "rendered_with_browser": result["rendered_with_browser"],
        "text_snippet": result["text"][:500] if result["text"] else "",
        "warnings": list(result["warnings"]),
        "_html": html_text,   # <-- NEW: lets downstream callers (intelligence_scraper.py)
                           #     re-run a different text extractor without a second
                           #     network fetch. Purely additive — no existing reader
                           #     of `raw` breaks from one extra key.
    }
    result["fetched_at"] = datetime.now(timezone.utc).isoformat()
    result["content_hash"] = _hash_content(result.get("text", ""))
    return result


def _hash_content(text: str) -> str:
    """SHA-256 of extracted text, used to detect page changes between
    fetches without diffing the full text. Empty text hashes to '' (not
    the hash of an empty string) so an empty extraction doesn't look
    like a stable, unchanging page."""
    if not text:
        return ""
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()


# ---------------------------------------------------------------------------
# Model-field coercion helpers — scraped values are untrusted strings;
# the model has strict CharField max_lengths and Decimal/Integer fields
# that raise at the DB layer on bad input, which would violate this
# module's "never raises" contract if assigned unchecked.
# ---------------------------------------------------------------------------

def _truncate(value, max_length: int):
    if not value:
        return None
    value = str(value).strip()
    return value[:max_length] if value else None


def _to_decimal(value, max_digits: int, decimal_places: int):
    if value in (None, ""):
        return None
    try:
        cleaned = re.sub(r"[^0-9.\-]", "", str(value))
        if not cleaned or cleaned in ("-", "."):
            return None
        dec = Decimal(cleaned).quantize(Decimal(1).scaleb(-decimal_places))
    except (InvalidOperation, ValueError, ArithmeticError):
        return None
    int_digits = max_digits - decimal_places
    if abs(dec) >= Decimal(10) ** int_digits:
        return None
    return dec


def _to_positive_int(value):
    if value in (None, ""):
        return None
    try:
        n = int(re.sub(r"[^0-9\-]", "", str(value)) or "x")
    except ValueError:
        return None
    return n if n >= 0 else None


# ---------------------------------------------------------------------------
# Save straight onto a ConnectedService row
# ---------------------------------------------------------------------------

def scrape_and_store(service, save: bool = True, worker_id: str = None) -> dict:
    """
    Scrape service.service_url and write the results onto the given
    ConnectedService instance's fields. Always writes something:
    fetch_status is 'success' whenever ANY data was retrieved (even
    partial, with warnings), and only 'error' when the URL could not
    be reached at all.

    Enterprise crawling guards, applied BEFORE any network request:
      1. SSRF re-validation — service.service_url is re-checked against
         validate_crawl_url() at fetch time, not just at model-clean
         time, since DNS can change between when a row was created and
         when a worker gets around to crawling it.
      2. Domain policy — a manually blocked domain or a robots.txt
         `disallow_all` short-circuits before any request is issued;
         a domain currently over its rate-limit window is deferred
         with fetch_status='error'/RATE_LIMITED rather than fetched.
    Conditional GET (If-None-Match / If-Modified-Since) is sent using
    the service's stored etag/last_modified_header, and whatever the
    response returns is written back for next time.
    """
    started_at = dj_timezone.now()

    try:
        validate_crawl_url(service.service_url)
    except UnsafeCrawlURLError as exc:
        service.mark_fetch_error(
            str(exc), error_category=CrawlAttempt.ErrorCategory.SSRF_BLOCKED,
            worker_id=worker_id, save=save,
        )
        return {**_empty_result(), "error": str(exc)}

    policy = service.domain_policy
    if policy is None and service.domain:
        policy, _ = DomainCrawlPolicy.objects.get_or_create(domain=service.domain)
        service.domain_policy = policy

    if policy and (policy.is_blocked or (service.respect_robots_txt and policy.disallow_all)):
        reason = policy.blocked_reason or "Domain is blocked by crawl policy (manual block or robots.txt)"
        service.mark_fetch_error(
            reason, error_category=CrawlAttempt.ErrorCategory.ROBOTS_BLOCKED,
            worker_id=worker_id, save=save,
        )
        return {**_empty_result(), "error": reason}

    if policy and not policy.is_request_allowed_now():
        reason = "Domain rate limit exceeded for the current window — deferring"
        service.mark_fetch_error(
            reason, error_category=CrawlAttempt.ErrorCategory.RATE_LIMITED,
            worker_id=worker_id, save=save,
        )
        return {**_empty_result(), "error": reason}

    conditional_headers = {}
    if service.etag:
        conditional_headers["If-None-Match"] = service.etag
    if service.last_modified_header:
        conditional_headers["If-Modified-Since"] = service.last_modified_header

    data = scrape_url(
        service.service_url,
        api_key=service.api_key,
        auth_token=service.auth_token,
        extra_headers=conditional_headers or None,
    )

    if policy:
        policy.record_request()

    duration_ms = int((dj_timezone.now() - started_at).total_seconds() * 1000)

    og = data.get("og") or {}
    twitter = data.get("twitter") or {}
    meta = data.get("meta") or {}
    icons_feeds = data.get("icons_feeds") or {}
    robots = data.get("robots") or {}
    article = data.get("article") or {}
    product_info = data.get("product_info") or {}

    # --- Open Graph -----------------------------------------------------
    service.og_title = _truncate(og.get("title"), 500) or ""
    service.og_description = og.get("description") or ""
    service.og_thumbnail = og.get("thumbnail") or ""
    service.og_site_name = _truncate(og.get("site_name"), 200) or ""
    service.og_type = _truncate(og.get("type"), 100) or ""
    service.og_locale = _truncate(og.get("locale"), 20) or ""
    service.og_video = og.get("video") or ""
    service.og_audio = og.get("audio") or ""

    # --- Twitter / X Card -------------------------------------------------
    service.twitter_card = _truncate(twitter.get("card"), 50) or ""
    service.twitter_title = _truncate(twitter.get("title"), 500) or ""
    service.twitter_description = twitter.get("description") or ""
    service.twitter_image = twitter.get("image") or ""
    service.twitter_site = _truncate(twitter.get("site"), 100) or ""
    service.twitter_creator = _truncate(twitter.get("creator"), 100) or ""

    # --- Raw page identity ------------------------------------------------
    service.page_title = _truncate(meta.get("title"), 500) or ""
    service.meta_description = meta.get("description") or ""
    service.meta_keywords = _truncate(meta.get("keywords"), 1000) or ""
    service.meta_generator = _truncate(meta.get("generator"), 200) or ""
    service.charset = _truncate(meta.get("charset"), 50) or ""
    service.content_language = _truncate(meta.get("language"), 20) or ""
    service.theme_color = _truncate(meta.get("theme_color"), 20) or ""
    service.viewport = _truncate(meta.get("viewport"), 200) or ""

    # --- Extended page metadata ---------------------------------------------
    service.canonical_url = data.get("canonical_url") or ""
    service.favicon = data.get("favicon") or ""
    service.apple_touch_icon = icons_feeds.get("apple_touch_icon") or ""
    service.manifest_url = icons_feeds.get("manifest_url") or ""
    service.amp_url = icons_feeds.get("amp_url") or ""
    service.rss_feed_url = icons_feeds.get("rss_feed_url") or ""
    service.atom_feed_url = icons_feeds.get("atom_feed_url") or ""
    service.author = data.get("author") or ""
    service.author_url = article.get("author_url") or ""
    service.published_time = data.get("published_time") or ""
    service.modified_time = article.get("modified_time") or ""
    service.article_section = _truncate(article.get("section"), 200) or ""
    service.article_tags = article.get("tags") or []
    service.category_breadcrumb = data.get("category_breadcrumb") or []
    service.structured_data = data.get("structured_data") or []
    service.alternate_languages = data.get("alternate_languages") or []
    service.same_as_links = data.get("same_as") or []

    # --- Commerce -------------------------------------------------------------
    service.price_amount = _to_decimal(product_info.get("price"), 12, 2)
    service.price_currency = _truncate(product_info.get("currency"), 3) or ""
    service.availability = _truncate(product_info.get("availability"), 50) or ""
    service.product_sku = _truncate(product_info.get("sku"), 100) or ""
    service.product_gtin = _truncate(product_info.get("gtin"), 50) or ""
    service.product_condition = _truncate(product_info.get("condition"), 50) or ""
    service.brand_name = _truncate(product_info.get("brand"), 200) or ""
    service.seller_name = _truncate(product_info.get("seller"), 200) or ""
    service.rating_value = _to_decimal(product_info.get("rating_value"), 4, 2)
    service.rating_count = _to_positive_int(product_info.get("rating_count"))

    # --- Robots / indexability -------------------------------------------------
    service.robots_meta = _truncate(robots.get("robots_meta"), 200) or ""
    service.x_robots_tag = _truncate(robots.get("x_robots_tag"), 200) or ""
    service.is_indexable = robots.get("is_indexable")

    # --- Extracted media & content -----------------------------------------------
    service.extracted_images = data.get("images", [])
    service.extracted_videos = data.get("videos", [])
    service.extracted_links = data.get("links", [])
    service.extracted_headings = data.get("headings") or {"h1": [], "h2": [], "h3": []}
    service.extracted_text = data.get("text") or ""
    service.word_count = data.get("word_count") or 0
    service.reading_time_minutes = data.get("reading_time_minutes") or 0

    # --- Fetch / response diagnostics --------------------------------------------
    service.content_type = _truncate(data.get("content_type"), 200) or ""
    service.response_headers = data.get("response_headers") or {}
    service.page_size_bytes = data.get("page_size_bytes") or None
    # resilient_get doesn't expose a network-only timing breakdown, so
    # this is the whole fetch+parse duration, not a pure load-time
    # measurement — the closest real number available rather than a
    # fabricated one.
    service.load_time_ms = duration_ms

    if data.get("etag"):
        service.etag = data["etag"][:255]
    if data.get("last_modified_header"):
        service.last_modified_header = data["last_modified_header"][:255]

    if data.get("error"):
        service.mark_fetch_error(
            data["error"],
            error_category=CrawlAttempt.ErrorCategory.CONNECTION_ERROR,
            duration_ms=duration_ms,
            worker_id=worker_id,
            save=save,
        )
    else:
        service.mark_fetch_success(
            content_hash=data.get("content_hash") or None,
            http_status_code=200,
            duration_ms=duration_ms,
            worker_id=worker_id,
            save=save,
        )
        # mark_fetch_success() unconditionally resets fetch_error to
        # None as part of clearing the error state, so warnings must be
        # applied AFTER it runs — setting them first (as an earlier
        # version of this function did) meant they were silently wiped
        # out on every successful scrape, in memory as well as in the
        # DB, regardless of the `save` flag.
        warnings_text = "; ".join(data.get("warnings", [])) or None
        if warnings_text:
            service.fetch_error = warnings_text
            if save:
                service.save(update_fields=['fetch_error', 'updated_at'])

    return data


# ---------------------------------------------------------------------------
# Headless-browser fallback glue
# ---------------------------------------------------------------------------

def _apply_browser_render(result: dict, url: str, user_agent: str) -> None:
    """
    Renders *url* in headless Chromium (via playwright_fallback) and
    backfills `result` in place with whatever the plain-HTTP pass
    missed. Only fills empty/thin fields — real server-rendered <head>
    metadata from the first pass is left alone rather than overwritten
    with rendered-DOM guesses, since it's often already correct even
    on pages whose visible body needs JS.

    No-ops silently (via the outer _safe call in scrape_url) if
    playwright isn't installed or the render fails for any reason —
    the plain-HTTP result is always a valid fallback on its own.
    """
    rendered = render_with_browser(url, user_agent)
    if not rendered:
        return
    rendered_html, rendered_final_url = rendered

    warnings = result["warnings"]
    rendered_soup = _safe(lambda: BeautifulSoup(rendered_html, "lxml"), None, warnings, "parse_rendered_lxml")
    if rendered_soup is None:
        rendered_soup = _safe(lambda: BeautifulSoup(rendered_html, "html.parser"), None, warnings, "parse_rendered_html_parser")
    if rendered_soup is None:
        warnings.append("playwright_render: rendered HTML failed to parse")
        return

    result["rendered_with_browser"] = True
    base_url = _safe(_base_url, rendered_final_url, warnings, "base_url_rendered", rendered_soup, rendered_final_url)

    structured_data = _safe(_extract_json_ld, [], warnings, "json_ld_rendered", rendered_soup)
    if structured_data and not result["structured_data"]:
        result["structured_data"] = structured_data
    else:
        structured_data = result["structured_data"] or structured_data

    og = _safe(_extract_og, {}, warnings, "og_rendered", rendered_soup, structured_data)
    _backfill(result["og"], og)

    twitter = _safe(_extract_twitter, {}, warnings, "twitter_rendered", rendered_soup)
    _backfill(result["twitter"], twitter)

    page_meta = _safe(_extract_page_meta, {}, warnings, "page_meta_rendered", rendered_soup)
    _backfill(result["meta"], page_meta)

    icons_feeds = _safe(_extract_icons_and_feeds, {}, warnings, "icons_feeds_rendered", rendered_soup, base_url)
    _backfill(result["icons_feeds"], icons_feeds)

    article = _safe(_extract_article_meta, {}, warnings, "article_rendered", rendered_soup, base_url)
    _backfill(result["article"], article)

    if not result["alternate_languages"]:
        result["alternate_languages"] = _safe(
            _extract_alternate_languages, [], warnings, "alt_languages_rendered", rendered_soup, base_url,
        )
    if not result["same_as"]:
        result["same_as"] = _safe(_extract_same_as, [], warnings, "same_as_rendered", structured_data)
    if not result["category_breadcrumb"]:
        result["category_breadcrumb"] = _safe(
            _extract_breadcrumb, [], warnings, "breadcrumb_rendered", structured_data, rendered_soup,
        )

    if not result["canonical_url"]:
        result["canonical_url"] = _safe(_extract_canonical, result["canonical_url"], warnings, "canonical_rendered", rendered_soup, base_url)
    if not result["favicon"]:
        result["favicon"] = _safe(_extract_favicon, "", warnings, "favicon_rendered", rendered_soup, base_url)
    if not result["author"]:
        result["author"] = _safe(_extract_author, "", warnings, "author_rendered", rendered_soup)
    if not result["published_time"]:
        result["published_time"] = _safe(_extract_published_time, "", warnings, "published_time_rendered", rendered_soup)
    if not any(result["headings"].values()):
        result["headings"] = _safe(_extract_headings, result["headings"], warnings, "headings_rendered", rendered_soup)
    if not result["product_info"]:
        result["product_info"] = _safe(_extract_product_info, {}, warnings, "product_info_rendered", structured_data, rendered_soup)

    rendered_images = _safe(_extract_images, [], warnings, "images_rendered", rendered_soup, base_url)
    if len(rendered_images) > len(result["images"]):
        result["images"] = rendered_images

    rendered_videos = _safe(_extract_videos, [], warnings, "videos_rendered", rendered_soup, base_url)
    if len(rendered_videos) > len(result["videos"]):
        result["videos"] = rendered_videos

    rendered_links = _safe(_extract_links, [], warnings, "links_rendered", rendered_soup, base_url)
    if len(rendered_links) > len(result["links"]):
        result["links"] = rendered_links

    rendered_text = _safe(_extract_text, "", warnings, "text_rendered", rendered_soup)
    if len(rendered_text) > len(result["text"]):
        result["text"] = rendered_text


# ---------------------------------------------------------------------------
# Extraction helpers
# ---------------------------------------------------------------------------

def _meta_tag_content(soup: BeautifulSoup, *, name: str = None, prop: str = None) -> str:
    attrs = {"name": name} if name else {"property": prop}
    tag = soup.find("meta", attrs=attrs)
    return tag.get("content", "").strip() if tag else ""


def _http_equiv_content(soup: BeautifulSoup, http_equiv_name: str) -> str:
    tag = soup.find("meta", attrs={"http-equiv": lambda x: x and x.lower() == http_equiv_name.lower()})
    return tag.get("content", "").strip() if tag else ""


def _extract_og(soup: BeautifulSoup, structured_data: list = None) -> dict:
    og = {}

    og["title"] = (_meta_tag_content(soup, prop="og:title")
                   or _meta_tag_content(soup, name="twitter:title")
                   or (soup.title.string.strip() if soup.title and soup.title.string else ""))
    og["description"] = (_meta_tag_content(soup, prop="og:description")
                          or _meta_tag_content(soup, name="description")
                          or _meta_tag_content(soup, name="twitter:description"))
    og["thumbnail"] = (_meta_tag_content(soup, prop="og:image")
                        or _meta_tag_content(soup, name="twitter:image"))
    og["site_name"] = _meta_tag_content(soup, prop="og:site_name")
    og["type"] = _meta_tag_content(soup, prop="og:type")
    og["locale"] = _meta_tag_content(soup, prop="og:locale")
    og["video"] = _meta_tag_content(soup, prop="og:video") or _meta_tag_content(soup, prop="og:video:url")
    og["audio"] = _meta_tag_content(soup, prop="og:audio")

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


def _extract_twitter(soup: BeautifulSoup) -> dict:
    return {
        "card": _meta_tag_content(soup, name="twitter:card"),
        "title": _meta_tag_content(soup, name="twitter:title"),
        "description": _meta_tag_content(soup, name="twitter:description"),
        "image": _meta_tag_content(soup, name="twitter:image") or _meta_tag_content(soup, name="twitter:image:src"),
        "site": _meta_tag_content(soup, name="twitter:site"),
        "creator": _meta_tag_content(soup, name="twitter:creator"),
    }


def _extract_page_meta(soup: BeautifulSoup) -> dict:
    charset = ""
    charset_tag = soup.find("meta", attrs={"charset": True})
    if charset_tag:
        charset = (charset_tag.get("charset") or "").strip()
    else:
        content_type_equiv = _http_equiv_content(soup, "content-type")
        if "charset=" in content_type_equiv:
            charset = content_type_equiv.split("charset=")[-1].split(";")[0].strip()

    return {
        "title": soup.title.string.strip() if soup.title and soup.title.string else "",
        "description": _meta_tag_content(soup, name="description"),
        "keywords": _meta_tag_content(soup, name="keywords"),
        "generator": _meta_tag_content(soup, name="generator"),
        "charset": charset,
        "language": (soup.html.get("lang", "").strip() if soup.html else "") or _http_equiv_content(soup, "content-language"),
        "theme_color": _meta_tag_content(soup, name="theme-color"),
        "viewport": _meta_tag_content(soup, name="viewport"),
    }


def _extract_icons_and_feeds(soup: BeautifulSoup, base_url: str) -> dict:
    result = {"apple_touch_icon": "", "manifest_url": "", "amp_url": "", "rss_feed_url": "", "atom_feed_url": ""}
    for tag in soup.find_all("link", href=True):
        tokens = _rel_tokens(tag.get("rel"))
        tag_type = (tag.get("type") or "").lower()
        href = tag["href"]

        if not result["apple_touch_icon"] and "apple-touch-icon" in tokens:
            result["apple_touch_icon"] = _abs(href, base_url)
        elif not result["manifest_url"] and "manifest" in tokens:
            result["manifest_url"] = _abs(href, base_url)
        elif not result["amp_url"] and "amphtml" in tokens:
            result["amp_url"] = _abs(href, base_url)
        elif not result["rss_feed_url"] and "alternate" in tokens and "rss" in tag_type:
            result["rss_feed_url"] = _abs(href, base_url)
        elif not result["atom_feed_url"] and "alternate" in tokens and "atom" in tag_type:
            result["atom_feed_url"] = _abs(href, base_url)
    return result


def _extract_robots_meta(soup: BeautifulSoup, resp_headers: dict) -> dict:
    tag = soup.find("meta", attrs={"name": lambda x: x and x.lower() == "robots"})
    robots_meta = tag.get("content", "").strip() if tag else ""
    x_robots_tag = (resp_headers or {}).get("X-Robots-Tag") or (resp_headers or {}).get("x-robots-tag") or ""
    combined = f"{robots_meta} {x_robots_tag}".lower()
    is_indexable = None if not (robots_meta or x_robots_tag) else "noindex" not in combined
    return {"robots_meta": robots_meta, "x_robots_tag": x_robots_tag, "is_indexable": is_indexable}


def _extract_article_meta(soup: BeautifulSoup, base_url: str) -> dict:
    tags = [
        t.get("content", "").strip()
        for t in soup.find_all("meta", attrs={"property": "article:tag"})
        if t.get("content", "").strip()
    ]

    author_url = ""
    author_link = soup.find("link", rel=lambda x: x and "author" in _rel_tokens(x), href=True)
    if author_link:
        author_url = _abs(author_link["href"], base_url)
    else:
        article_author = _meta_tag_content(soup, prop="article:author")
        if article_author.startswith("http"):
            author_url = article_author

    return {
        "section": _meta_tag_content(soup, prop="article:section"),
        "tags": tags,
        "modified_time": _meta_tag_content(soup, prop="article:modified_time") or _meta_tag_content(soup, prop="og:updated_time"),
        "author_url": author_url,
    }


def _extract_alternate_languages(soup: BeautifulSoup, base_url: str) -> list:
    out = []
    for tag in soup.find_all("link", rel=lambda x: x and "alternate" in _rel_tokens(x), hreflang=True, href=True):
        out.append({"hreflang": tag["hreflang"], "href": _abs(tag["href"], base_url)})
    return out


def _iter_json_ld_nodes(structured_data: list):
    """Yield every dict node in `structured_data`, flattening the common
    top-level {"@graph": [...]} wrapper pattern in addition to a bare
    object or a bare list of objects."""
    for block in structured_data or []:
        for node in (block if isinstance(block, list) else [block]):
            if not isinstance(node, dict):
                continue
            if isinstance(node.get("@graph"), list):
                for inner in node["@graph"]:
                    if isinstance(inner, dict):
                        yield inner
            else:
                yield node


def _extract_same_as(structured_data: list) -> list:
    same_as = []
    for node in _iter_json_ld_nodes(structured_data):
        value = node.get("sameAs")
        if isinstance(value, str):
            same_as.append(value)
        elif isinstance(value, list):
            same_as.extend(v for v in value if isinstance(v, str))

    seen = set()
    deduped = []
    for url in same_as:
        if url not in seen:
            seen.add(url)
            deduped.append(url)
    return deduped


def _extract_breadcrumb(structured_data: list, soup: BeautifulSoup) -> list:
    for node in _iter_json_ld_nodes(structured_data):
        types = node.get("@type")
        types = types if isinstance(types, list) else [types]
        if "BreadcrumbList" not in (types or []):
            continue
        items = node.get("itemListElement") or []
        crumbs = []
        for el in sorted((i for i in items if isinstance(i, dict)), key=lambda x: x.get("position", 0)):
            entity = el.get("item")
            name = el.get("name")
            url = None
            if isinstance(entity, dict):
                url = entity.get("@id") or entity.get("url")
                name = name or entity.get("name")
            elif isinstance(entity, str):
                url = entity
            if name:
                crumbs.append({"name": name, "url": url or ""})
        if crumbs:
            return crumbs

    breadcrumb_container = (
        soup.find(attrs={"aria-label": lambda x: x and "breadcrumb" in x.lower()})
        or soup.find(class_=lambda x: x and "breadcrumb" in x.lower())
    )
    if breadcrumb_container:
        crumbs = []
        for link in breadcrumb_container.find_all("a"):
            text = link.get_text(strip=True)
            if text:
                crumbs.append({"name": text, "url": link.get("href", "")})
        if crumbs:
            return crumbs
    return []


def _extract_json_ld(soup: BeautifulSoup) -> list:
    blocks = []
    for tag in soup.find_all("script", type="application/ld+json"):
        if not tag.string:
            continue
        try:
            blocks.append(json.loads(tag.string))
        except (ValueError, TypeError):
            continue
    return blocks


def _extract_canonical(soup: BeautifulSoup, fallback_url: str) -> str:
    """
    Best-guess canonical URL from <link rel="canonical">.

    FIX: matched via _rel_tokens() exact-token membership, not a raw
    substring check on the tag's `rel` attribute. BeautifulSoup
    normally returns `rel` as a list (["canonical"]), so
    `"canonical" in rel` behaves like a token check by accident — but
    in the parser/attribute combinations where `rel` comes back as a
    plain string instead (the same case _rel_tokens()/_extract_favicon
    already guard against for "icon" vs "apple-touch-icon"/"shortcut
    icon"), the same expression silently degrades into a substring
    check and can match an unrelated rel value that merely contains
    "canonical" as a substring — writing the wrong URL into
    canonical_url with no warning raised. Tokenizing first closes that
    gap and keeps this extractor consistent with _extract_favicon,
    _extract_alternate_languages, and _extract_article_meta, which
    already do this correctly.
    """
    tag = soup.find("link", rel=lambda x: x and "canonical" in _rel_tokens(x), href=True)
    href = tag.get("href") if tag else None
    return _abs(href, fallback_url) if href else fallback_url


def _rel_tokens(rel_attr) -> set:
    """
    Normalize a tag's `rel` attribute (BS4 gives a list for space-
    separated attrs, but falls back to a string in some parser/attr
    combinations) into a lowercase token set for exact matching.
    """
    if rel_attr is None:
        return set()
    if isinstance(rel_attr, list):
        return {tok.lower() for tok in rel_attr}
    return {rel_attr.lower()}


def _extract_favicon(soup: BeautifulSoup, base_url: str) -> str:
    """
    Best-guess favicon URL, honoring the icon > shortcut icon >
    apple-touch-icon preference order via exact rel-token matching.
    "icon" is a substring of both other values, so a naive substring
    check on the joined rel string always matches whichever tag
    happens to appear first in the document regardless of this
    preference order — matching on the tokenized rel set instead
    ("shortcut icon" -> {"shortcut", "icon"}) avoids that.
    """
    tags = soup.find_all("link", href=True)
    for rel_name in _FAVICON_RELS_IN_PRIORITY:
        wanted = set(rel_name.split())
        for tag in tags:
            if wanted <= _rel_tokens(tag.get("rel")):
                return _abs(tag["href"], base_url)
    return _abs("/favicon.ico", base_url)


def _extract_author(soup: BeautifulSoup) -> str:
    tag = (soup.find("meta", attrs={"name": "author"})
           or soup.find("meta", attrs={"property": "article:author"}))
    return tag.get("content", "").strip() if tag else ""


def _extract_published_time(soup: BeautifulSoup) -> str:
    tag = (soup.find("meta", attrs={"property": "article:published_time"})
           or soup.find("meta", attrs={"name": "date"}))
    if tag:
        return tag.get("content", "").strip()
    time_tag = soup.find("time", attrs={"datetime": True})
    return time_tag.get("datetime", "").strip() if time_tag else ""


def _extract_headings(soup: BeautifulSoup) -> dict:
    return {
        "h1": [h.get_text(strip=True) for h in soup.find_all("h1") if h.get_text(strip=True)],
        "h2": [h.get_text(strip=True) for h in soup.find_all("h2") if h.get_text(strip=True)],
        "h3": [h.get_text(strip=True) for h in soup.find_all("h3") if h.get_text(strip=True)],
    }


def _extract_product_info(structured_data: list, soup: BeautifulSoup) -> dict:
    """
    Pull name/brand/price/currency/availability/sku/gtin/condition/
    seller/rating for a schema.org Product block, plus a lightweight
    specs/features dict. JSON-LD is checked first (authoritative — sites
    populate it for SEO / rich search snippets) and CSS-selector
    heuristics only fill in whatever it didn't have.
    """
    info = {}
    for node in _iter_json_ld_nodes(structured_data):
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

        if node.get("sku"):
            info.setdefault("sku", str(node["sku"]))
        gtin = node.get("gtin13") or node.get("gtin") or node.get("gtin8") or node.get("gtin12") or node.get("gtin14") or node.get("mpn")
        if gtin:
            info.setdefault("gtin", str(gtin))

        offers = node.get("offers") or {}
        if isinstance(offers, list):
            offers = offers[0] if offers else {}
        if offers.get("price"):
            info.setdefault("price", str(offers["price"]))
        if offers.get("priceCurrency"):
            info.setdefault("currency", offers["priceCurrency"])
        if offers.get("availability"):
            info.setdefault("availability", str(offers["availability"]).rsplit("/", 1)[-1])
        if offers.get("itemCondition"):
            info.setdefault("condition", str(offers["itemCondition"]).rsplit("/", 1)[-1])
        seller = offers.get("seller")
        if isinstance(seller, dict):
            seller = seller.get("name")
        if seller:
            info.setdefault("seller", seller)

        rating = node.get("aggregateRating")
        if isinstance(rating, dict):
            if rating.get("ratingValue") is not None:
                info.setdefault("rating_value", str(rating["ratingValue"]))
            count = rating.get("reviewCount") or rating.get("ratingCount")
            if count is not None:
                info.setdefault("rating_count", str(count))

        if info:
            break

    if "price" not in info:
        price_selectors = [
            {"class": "price"},
            {"class": "product-price"},
            {"itemprop": "price"},
            {"class": lambda x: x and "price" in x.lower()},
        ]
        for selector in price_selectors:
            price_elem = soup.find(["span", "div", "p"], selector)
            if price_elem:
                info["price"] = price_elem.get_text(strip=True)
                break

    specifications = {}
    for keyword in _SPEC_KEYWORDS:
        spec_section = soup.find(
            ["div", "section", "table"],
            class_=lambda x: x and keyword in x.lower(),
        )
        if spec_section:
            for item in spec_section.find_all(["li", "tr", "div"]):
                text = item.get_text(strip=True)
                if ":" in text:
                    key, value = text.split(":", 1)
                    key, value = key.strip(), value.strip()
                    if key and value:
                        specifications[key] = value
            if specifications:
                break

    if specifications:
        info["specifications"] = specifications

    return info


def _extract_images(soup: BeautifulSoup, base_url: str) -> list:
    images = []
    seen = set()
    for tag in soup.find_all("img", src=True):
        src = _abs(tag["src"], base_url)
        if not src or src in seen or not _is_valid_url(src):
            continue
        if _safe(is_probably_tracking_pixel, False, [], "tracking_pixel_check", src, tag.get("width"), tag.get("height")):
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


def _extract_videos(soup: BeautifulSoup, base_url: str) -> list:
    videos = []
    seen = set()

    for tag in soup.find_all(["video", "source"]):
        src = tag.get("src") or tag.get("data-src")
        if src:
            src = _abs(src, base_url)
            if src and src not in seen and _is_valid_url(src):
                seen.add(src)
                videos.append({"url": src, "type": tag.get("type", "video/*")})

    for tag in soup.find_all("iframe", src=True):
        src = _abs(tag["src"], base_url)
        if not src or src in seen:
            continue
        info = _safe(get_video_info_cached, {}, [], "video_info", src)
        if info.get("platform") and info["platform"] != "unknown":
            seen.add(src)
            videos.append({"url": src, "type": "embed"})
        if len(videos) >= MAX_VIDEOS:
            break

    return videos[:MAX_VIDEOS]


def _extract_links(soup: BeautifulSoup, base_url: str) -> list:
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
    for tag in soup(["script", "style", "noscript", "head"]):
        tag.decompose()

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


def _guess_encoding(resp_headers: dict) -> Union[str, None]:
    content_type = resp_headers.get("Content-Type", "")
    if "charset=" in content_type:
        return content_type.split("charset=")[-1].split(";")[0].strip()
    return None


def _empty_result() -> dict:
    return {
        "og": {},
        "twitter": {},
        "meta": {},
        "icons_feeds": {},
        "robots": {"robots_meta": "", "x_robots_tag": "", "is_indexable": None},
        "article": {},
        "canonical_url": "",
        "favicon": "",
        "author": "",
        "published_time": "",
        "headings": {"h1": [], "h2": [], "h3": []},
        "structured_data": [],
        "product_info": {},
        "alternate_languages": [],
        "same_as": [],
        "category_breadcrumb": [],
        "images": [],
        "videos": [],
        "links": [],
        "text": "",
        "word_count": 0,
        "reading_time_minutes": 0,
        "rendered_with_browser": False,
        "raw": {},
        "etag": "",
        "last_modified_header": "",
        "response_headers": {},
        "content_type": "",
        "page_size_bytes": 0,
        "content_hash": "",
        "error": None,
        "warnings": [],
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }


# ---------------------------------------------------------------------------
# Notes
# ---------------------------------------------------------------------------
# - JS-only SPA pages: handled via the playwright_fallback.py browser
#   render, triggered automatically when the plain-HTTP pass comes
#   back thin (see should_attempt_render). Requires
#   `pip install playwright && playwright install --with-deps chromium`
#   at deploy time; if playwright isn't installed, this silently
#   no-ops back to the plain-HTTP result (one warning logged, not per
#   request — see playwright_fallback.is_playwright_installed).
# - Every field in the result dict now has a corresponding
#   ConnectedService column and is written by scrape_and_store; there
#   are no more "nowhere to put this" fields riding along only inside
#   last_fetched_data (raw). `raw` is still populated as a full
#   diagnostic snapshot (including which warnings fired), independent
#   of what made it onto the typed columns.
# - redirect_chain, dns_time_ms, ttfb_ms, ssl_valid, and proxy_used are
#   NOT populated here: resilient_get()'s current return signature
#   (raw_bytes, resp_headers, final_url, fetch_error) doesn't expose
#   any of that. Extend resilient_fetch.py to surface them, then wire
#   them into `data`/`scrape_and_store` the same way content_type and
#   page_size_bytes were added, rather than guessing at values this
#   module has no real way to know.
# - response_headers (and therefore every `resp_headers.get(...)` call
#   in this file, including Content-Type/ETag/Last-Modified/
#   X-Robots-Tag) is case-insensitive as of resilient_fetch.py's
#   CaseInsensitiveDict fix — see that module's docstring for why an
#   exact-case "Content-Type" lookup previously misfired against
#   HTTP/2 origins (which lowercase all header names on the wire) and
#   caused perfectly normal HTML pages to be misclassified as
#   non-HTML, skipping structured extraction entirely.