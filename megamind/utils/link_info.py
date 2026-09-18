# megamind/utils/link_info.py

"""
Display-layer helpers for rendering link/URL data as HTML.

Complements media_info.py (which normalizes RAW scraped link/image dicts
into a stable {href, text} / {url, alt, href} shape) and
intelligence_scraper.py (which produces a "deep scan" report of scraped/
skipped/error links). Neither of those modules concerns itself with how
a link ends up in the DOM — this one does: HTML-escaping text, rejecting
dangerous hrefs (javascript:, data:, vbscript:, etc.) before they can
become a clickable <a href>, truncating long anchor text, deriving a
favicon and a "same site vs external" flag, and turning intelligence_
scraper's skip/error reason codes into short human-readable labels.

prepare_scrape_report_for_display() additionally turns each scraped
entry from intelligence_scraper.py into a full "intelligence post":
title/description/thumbnail (OG -> Twitter Card -> first extracted
image), author/published date, word count + reading time, a capped and
sanitized image strip, a capped video list, an outbound-link count,
product info when present, a structured_summary block (article/recipe/
event/video/FAQ/organization — whatever intelligence_scraper.py's
JSON-LD detection found on that page), and an `intel` block (language,
keywords, heading outline, favicon, on-page social links, on-page
contact info, freshness signals, basic response stats, a hero image,
this page's own outbound-domain breakdown, and a content-quality
signal) — everything scrape_url() already extracted for that page,
normalized and capped the same way a primary scrape's own fields are
before reaching a template. This is a display-layer richness upgrade
only: it does not change what intelligence_scraper.py is allowed to
fetch (SSRF validation, domain blocks, robots.txt disallow_all, and
rate limiting are all enforced upstream, unchanged).

No Django import on purpose (same convention as media_info.py) — this
only depends on the stdlib, so it's safe to use from a template context
processor, a Celery task building a JSON payload for a frontend, a DRF
serializer, or a plain script. Every function here is a pure transform:
nothing is fetched, nothing is written to a model, nothing raises on bad
input (worst case a link/field is silently dropped or comes back "").
"""

from __future__ import annotations

import html
from typing import Any, Dict, Iterable, List, Optional, Tuple, TypedDict
from urllib.parse import urlparse

from megamind.utils.media_info import normalize_link, normalize_images

__all__ = [
    "LinkDisplay",
    "escape_text",
    "is_displayable_href",
    "extract_domain_for_display",
    "favicon_url_for_domain",
    "external_rel_attribute",
    "truncate_text",
    "humanize_duration_ms",
    "humanize_skip_reason",
    "prepare_link_for_display",
    "prepare_links_for_display",
    "group_links_by_domain",
    "prepare_scrape_report_for_display",
]

# Schemes that are ever safe to render as a clickable href. Anything not
# in this set — javascript:, data:, vbscript:, file:, etc. — is a known
# XSS / local-file-access vector when it ends up in an <a href="..."> on
# a page whose CONTENT we don't control (it came from a scraped, third-
# party page), so links with any other scheme are never rendered as
# clickable at all; prepare_link_for_display() drops them outright,
# same as it drops links with no href.
_DISPLAYABLE_SCHEMES = {"http", "https", "mailto", "tel"}

# Stricter than _DISPLAYABLE_SCHEMES: <img src> only ever gets http/https
# here, deliberately excluding data:/blob: — an intelligence post embeds
# images pulled from a scraped, untrusted third-party page, and a
# data:-URI image can carry an arbitrarily large inline payload straight
# into the report JSON instead of a normal, boundable image request.
_DISPLAYABLE_IMAGE_SCHEMES = {"http", "https"}

# Per-post caps for the "intelligence post" view. Independent of
# max_links / max_per_domain in intelligence_scraper.py — those bound
# how many PAGES get fetched; these bound how much of any one page's
# own scraped content gets rendered, so one image-heavy or text-heavy
# page can't blow up a single post's payload size.

_MAX_POST_IMAGES = 50
_MAX_POST_VIDEOS = 50
_MAX_POST_LINKS = 150

# _MAX_SNIPPET_LENGTH = 600

# Per-post caps for the `intel` block specifically — a second, unrelated
# tier of per-page data (see _prepare_page_intel), capped independently
# of the caps above so a page with e.g. 40 keywords or 30 social links
# can't blow up a single post's payload any more than a page with 40
# images can.
_MAX_POST_KEYWORDS = 12
_MAX_POST_SOCIAL_LINKS = 8
_MAX_POST_CONTACTS = 6
_MAX_POST_OUTBOUND_DOMAINS = 8
_MAX_POST_HEADINGS_PER_LEVEL = 8

# intelligence_scraper.py skip-reason codes -> short, user-facing label.
# Matched on the part before the first ':' so e.g. "domain_blocked: xyz"
# and "unsafe_url: ..." still map without leaking the internal detail
# after the colon (raw exception text / policy row internals) to an
# end user.
_SKIP_REASON_LABELS = {
    "unfetchable_scheme": "Not a fetchable link",
    "filtered": "Excluded",
    "max_per_domain_reached": "Domain link limit reached",
    "no_domain": "No domain found",
    "rate_limited": "Domain rate-limited — try again later",
    "robots_disallow_all": "Blocked by robots.txt",
    "domain_blocked": "Domain is blocked",
    "unsafe_url": "Link address isn't safe to fetch",
    "filter_error": "Excluded",
    "non_page_extension": "Not a page link",
    # intelligence_scraper.py also emits these (via _classify_blocked /
    # _social_platform_precheck / the per-scan circuit breaker) — added
    # so they map to a real label instead of falling through to the
    # generic "Skipped" default.
    "blocked_by_platform": "Blocked by the site",
    "blocked_or_unsupported": "Blocked or unsupported response",
    "requires_official_api": "Requires the platform's own app",
    "circuit_open": "Domain skipped after repeated failures",
}

# scrape_url()'s product_info dict -> (label shown in an intelligence
# post's product chip row). Keys not present (or empty/None) on a given
# page's product_info are simply omitted, so a non-commerce page's post
# has no product row at all.
_PRODUCT_INFO_LABELS = {
    "name": "Name",
    "price": "Price",
    "currency": "Currency",
    "availability": "Availability",
    "brand": "Brand",
    "sku": "SKU",
    "condition": "Condition",
    "rating": "Rating",
}

# intelligence_scraper.py's _extract_structured_summary() content_type ->
# (label, ordered list of (field, display label) pairs to surface).
# Mirrors the shape each _summarize_*() helper in intelligence_scraper.py
# actually returns, so a structured_summary block renders as a compact,
# labeled fact list without needing per-content-type template logic.
_STRUCTURED_SUMMARY_FIELDS = {
    "article": ("Article", [
        ("headline", "Headline"), ("author", "Author"),
        ("date_published", "Published"), ("date_modified", "Updated"),
        ("section", "Section"), ("publisher", "Publisher"),
    ]),
    "recipe": ("Recipe", [
        ("name", "Name"), ("yield", "Yield"), ("total_time", "Total time"),
        ("ingredient_count", "Ingredients"), ("author", "Author"),
    ]),
    "event": ("Event", [
        ("name", "Name"), ("start_date", "Starts"), ("end_date", "Ends"),
        ("location", "Location"), ("is_online", "Online"),
    ]),
    "video": ("Video", [
        ("name", "Name"), ("duration", "Duration"),
        ("upload_date", "Uploaded"), ("publisher", "Publisher"),
    ]),
    "organization": ("Organization", [
        ("name", "Name"), ("url", "URL"),
    ]),
    "faq": ("FAQ", [
        ("question_count", "Questions"),
    ]),
    "social_embed": ("Social embed", [
        ("provider", "Provider"),
    ]),
}


class LinkDisplay(TypedDict, total=False):
    href: str
    text: str
    domain: str
    favicon: str
    is_external: bool
    rel: str


# ---------------------------------------------------------------------------
# Low-level primitives
# ---------------------------------------------------------------------------

def escape_text(value: Optional[str]) -> str:
    """HTML-escape arbitrary scraped text (anchor text, titles,
    descriptions) before it's interpolated into a template. Always
    escapes quotes too (quote=True, the stdlib default), so the same
    escaped value is safe inside both text nodes and attribute values."""
    return html.escape(str(value or ""), quote=True)


def is_displayable_href(href: Optional[str]) -> bool:
    """True only for schemes safe to ever render as a clickable link. A
    scraped page is untrusted input — an <a href="javascript:..."> or
    href="data:text/html,..." found in its markup must never be
    reproduced as a real, clickable link in our own UI."""
    if not href:
        return False
    try:
        scheme = urlparse(href.strip()).scheme.lower()
    except Exception:
        return False
    return scheme in _DISPLAYABLE_SCHEMES


def _is_displayable_image_src(url: Optional[str]) -> bool:
    """Same idea as is_displayable_href but for an <img src> specifically
    — restricted to http/https only (see _DISPLAYABLE_IMAGE_SCHEMES)."""
    if not url:
        return False
    try:
        scheme = urlparse(url.strip()).scheme.lower()
    except Exception:
        return False
    return scheme in _DISPLAYABLE_IMAGE_SCHEMES


def extract_domain_for_display(url: str) -> str:
    """Bare, lowercased hostname for display next to a link (no
    userinfo, no port). Deliberately re-implemented here rather than
    imported from megamind.models.connected_service.extract_domain —
    this is a display-only utility and must not pull in Django models."""
    try:
        netloc = urlparse(url).netloc
    except Exception:
        return ""
    return netloc.split("@")[-1].split(":")[0].lower() if netloc else ""


def favicon_url_for_domain(domain: str, size: int = 32) -> str:
    """A small favicon to show next to a link, without this codebase
    fetching or storing favicons itself. Uses Google's public favicon
    service as a best-effort visual only — treat it as decoration, not
    authoritative site metadata; it can come back blank or unavailable."""
    if not domain:
        return ""
    return f"https://www.google.com/s2/favicons?sz={int(size)}&domain={domain}"


def external_rel_attribute(is_external: bool) -> str:
    """rel value for an <a> tag pointing at an externally-domained link,
    to prevent 'reverse tabnabbing' (a page opened with target="_blank"
    gaining window.opener access back into this tab). Empty string for
    same-site links, where it isn't needed."""
    return "noopener noreferrer" if is_external else ""


def truncate_text(text: Optional[str], max_length: int = 80) -> str:
    """Truncate at a word boundary where possible, appending an
    ellipsis. Falls back to a hard cut only if there's no nearby space."""
    text = (text or "").strip()
    if len(text) <= max_length:
        return text
    cut = text[:max_length].rsplit(" ", 1)[0]
    cut = cut or text[:max_length]
    return cut.rstrip(".,;:!? ") + "…"


def humanize_duration_ms(duration_ms: Optional[int]) -> str:
    if not duration_ms:
        return ""
    if duration_ms < 1000:
        return f"{duration_ms}ms"
    return f"{duration_ms / 1000:.1f}s"


def humanize_skip_reason(reason: Optional[str]) -> str:
    """Turns an intelligence_scraper skip/error reason code into a short
    label fit for display, without leaking internal detail to an end
    user (exact exception text, DomainCrawlPolicy row internals, etc.)."""
    if not reason:
        return "Skipped"
    prefix = reason.split(":", 1)[0].strip()
    return _SKIP_REASON_LABELS.get(prefix, "Skipped")


def _humanize_structured_summary(summary: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """
    Turns intelligence_scraper.py's _extract_structured_summary() output
    — {"content_type": "article"|"recipe"|"event"|"video"|"faq"|
    "organization"|"social_embed", ...type-specific fields} — into a
    display-ready {"label": str, "fields": [(label, escaped_value), ...]}
    block, or None if there's nothing to show.

    Deliberately re-derives the field list from
    _STRUCTURED_SUMMARY_FIELDS rather than dumping the dict as-is:
    fields with no value on this particular page (empty string, None,
    0 for a count) are dropped, values are HTML-escaped the same as
    every other scraped string reaching a template, and booleans (e.g.
    Event's is_online) are rendered as "Yes"/"No" rather than a raw
    Python bool repr.

    FAQ's own nested question/answer list is intentionally NOT expanded
    here — content_type="faq" only surfaces the question count as a
    fact, keeping this block a compact summary rather than reproducing
    the page's full FAQ content inline.
    """
    if not summary or not isinstance(summary, dict):
        return None
    content_type = summary.get("content_type")
    spec = _STRUCTURED_SUMMARY_FIELDS.get(content_type)
    if not spec:
        return None
    label, field_defs = spec

    fields: List[Tuple[str, str]] = []
    for key, field_label in field_defs:
        value = summary.get(key)
        if value in (None, "", 0):
            continue
        if isinstance(value, bool):
            display_value = "Yes" if value else "No"
        else:
            display_value = escape_text(str(value))
        fields.append((field_label, display_value))

    if not fields:
        return None
    return {"content_type": content_type, "label": label, "fields": fields}


def _prepare_page_intel(intel: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """
    Sanitizes intelligence_scraper.py's per-page `intel` block (see
    _extract_page_intel there) for display: escapes every string that
    came off scraped, untrusted markup, drops any social/contact/
    favicon/hero-image URL whose scheme isn't safe to render (same rule
    is_displayable_href / _is_displayable_image_src already apply
    elsewhere in this file), and caps every list independently of the
    main image/video/link caps above — this is a *second*, unrelated
    tier of per-page data, not more of the same one.

    Every sub-block is built defensively (missing/malformed input just
    means that sub-block is omitted) so one odd page's markup can't
    take down the rest of the post's display data.

    Returns {} (falsy) when `intel` is missing or empty, so a template
    can gate an entire "Page intelligence" section behind
    `if post.intel`.
    """
    if not intel or not isinstance(intel, dict):
        return {}

    out: Dict[str, Any] = {}

    language = (intel.get("language") or "").strip()
    if language:
        out["language"] = escape_text(language[:35])

    keywords = [
        escape_text(str(k))[:60] for k in (intel.get("keywords") or []) if k
    ][:_MAX_POST_KEYWORDS]
    if keywords:
        out["keywords"] = keywords

    headings_raw = intel.get("headings") or {}
    headings: Dict[str, List[str]] = {}
    for level in ("h1", "h2", "h3"):
        items = [
            escape_text(str(h))[:150] for h in (headings_raw.get(level) or []) if h
        ][:_MAX_POST_HEADINGS_PER_LEVEL]
        if items:
            headings[level] = items
    if headings:
        out["headings"] = headings

    favicon = (intel.get("favicon") or "").strip()
    if _is_displayable_image_src(favicon):
        out["favicon"] = favicon

    social_links = []
    for entry in (intel.get("social_links") or [])[:_MAX_POST_SOCIAL_LINKS]:
        url = (entry.get("url") or "").strip() if isinstance(entry, dict) else ""
        if not is_displayable_href(url):
            continue
        social_links.append({
            "platform": escape_text(str(entry.get("platform") or "")[:30]),
            "url": url,
        })
    if social_links:
        out["social_links"] = social_links

    contact_raw = intel.get("contact") or {}
    emails = [
        escape_text(str(e))[:120] for e in (contact_raw.get("emails") or []) if e
    ][:_MAX_POST_CONTACTS]
    phones = [
        escape_text(str(p))[:40] for p in (contact_raw.get("phones") or []) if p
    ][:_MAX_POST_CONTACTS]
    if emails or phones:
        out["contact"] = {"emails": emails, "phones": phones}

    freshness_raw = intel.get("freshness") or {}
    freshness = {
        k: escape_text(str(v))
        for k, v in freshness_raw.items()
        if v not in (None, "")
    }
    if freshness:
        out["freshness"] = freshness

    page_stats_raw = intel.get("page_stats") or {}
    page_stats: Dict[str, Any] = {}
    if page_stats_raw.get("server"):
        page_stats["server"] = escape_text(str(page_stats_raw["server"])[:80])
    if page_stats_raw.get("content_type"):
        page_stats["content_type"] = escape_text(str(page_stats_raw["content_type"])[:80])
    if page_stats_raw.get("content_length_bytes"):
        page_stats["content_length_bytes"] = page_stats_raw["content_length_bytes"]
    if page_stats:
        out["page_stats"] = page_stats

    hero_image = (intel.get("hero_image") or "").strip()
    if _is_displayable_image_src(hero_image):
        out["hero_image"] = hero_image

    outbound_domains = [
        {"domain": escape_text(str(d.get("domain") or "")), "count": int(d.get("count") or 0)}
        for d in (intel.get("outbound_domains") or [])[:_MAX_POST_OUTBOUND_DOMAINS]
        if isinstance(d, dict) and d.get("domain")
    ]
    if outbound_domains:
        out["outbound_domains"] = outbound_domains

    content_quality = intel.get("content_quality") or {}
    if content_quality:
        out["content_quality"] = {
            "text_to_html_ratio": content_quality.get("text_to_html_ratio", 0),
            "is_thin_content": bool(content_quality.get("is_thin_content")),
        }

    return out


# ---------------------------------------------------------------------------
# Link display preparation
# ---------------------------------------------------------------------------

def prepare_link_for_display(
    link: Any,
    *,
    base_domain: Optional[str] = None,
    max_text_length: int = 80,
    include_favicon: bool = True,
) -> Optional[LinkDisplay]:
    """
    Normalize + sanitize a single scraped link (any shape
    media_info.normalize_link accepts — dict with 'href'/'url', or a
    bare URL string) into something a template can render directly.

    Returns None — meaning "don't render this link at all" — if there's
    no href, or the href's scheme is never safe to render as clickable
    (see is_displayable_href). This mirrors the "no link, no display"
    rule already applied to images in media_info.normalize_image.
    """
    normalized = normalize_link(link)
    if not normalized:
        return None

    href = normalized["href"]
    if not is_displayable_href(href):
        return None

    domain = extract_domain_for_display(href)
    is_external = bool(base_domain) and domain != base_domain
    text = normalized["text"] or href

    return LinkDisplay(
        href=href,
        text=truncate_text(escape_text(text), max_text_length),
        domain=domain,
        favicon=favicon_url_for_domain(domain) if include_favicon else "",
        is_external=is_external,
        rel=external_rel_attribute(is_external),
    )


def prepare_links_for_display(
    links: Optional[Iterable[Any]],
    *,
    base_domain: Optional[str] = None,
    max_text_length: int = 80,
    include_favicon: bool = True,
    limit: Optional[int] = None,
) -> List[LinkDisplay]:
    """Batch version of prepare_link_for_display — silently drops any
    entry with no usable or no safely-displayable href, same as
    media_info.normalize_links() drops entries with no usable URL."""
    if not links:
        return []

    out: List[LinkDisplay] = []
    for link in links:
        prepared = prepare_link_for_display(
            link, base_domain=base_domain, max_text_length=max_text_length,
            include_favicon=include_favicon,
        )
        if prepared:
            out.append(prepared)
            if limit is not None and len(out) >= limit:
                break
    return out


def group_links_by_domain(links: List[LinkDisplay]) -> Dict[str, List[LinkDisplay]]:
    """Groups already-prepared links by domain, preserving first-seen
    domain order — handy for a template that wants one heading per site
    rather than a flat list."""
    grouped: Dict[str, List[LinkDisplay]] = {}
    for link in links:
        grouped.setdefault(link["domain"], []).append(link)
    return grouped


# ---------------------------------------------------------------------------
# Intelligence-post helpers
# ---------------------------------------------------------------------------

def _prepare_post_images(raw_images: Any, limit: int) -> Tuple[List[Dict[str, str]], int]:
    """Normalizes + sanitizes a scanned page's own extracted_images for
    display inside its intelligence post. Returns (capped list, total
    displayable count) so the UI can show e.g. 'showing 10 of 34'."""
    normalized = normalize_images(raw_images) if raw_images else []
    displayable = [
        {
            "url": img.get("url", ""),
            "alt": escape_text(img.get("alt") or ""),
            "href": img.get("href") or img.get("url", ""),
        }
        for img in normalized
        if _is_displayable_image_src(img.get("url"))
    ]
    return displayable[:limit], len(displayable)


def _prepare_post_videos(raw_videos: Any, limit: int) -> Tuple[List[Any], int]:
    """Videos are passed through largely as-is — the frontend's own
    normalizeVideoForDisplay() already knows how to turn a bare URL, a
    {"url","type"} dict, or a full video_info dict into an embeddable
    entry, so duplicating that logic here would just be a second place
    for the two to drift apart. Only the count is capped/returned; the
    frontend does its own per-platform normalization at render time."""
    videos = list(raw_videos or [])
    
    return videos[:limit], len(videos)


def _prepare_product_info(product_info: Optional[Dict[str, Any]]) -> Dict[str, str]:
    """Flattens scrape_url()'s product_info dict into an ordered
    {label: escaped_value} mapping for display, dropping any field
    that's missing/empty on this particular page."""
    if not product_info:
        return {}
    out: Dict[str, str] = {}
    for key, label in _PRODUCT_INFO_LABELS.items():
        value = product_info.get(key)
        if value not in (None, ""):
            out[label] = escape_text(str(value))
    return out


def _prepare_text_snippet(text: Optional[str]) -> str:
    """HTML-escaped full extracted text of a scanned page, for the
    expandable "full text" section of its intelligence post. Sent
    untruncated — the frontend's .intel-post__snippet.expanded rule
    caps the *visible* height (scrollable) rather than the data, so
    "Show more"/"Show less" actually reveals more content instead of
    uncollapsing a fixed-length stub that was already the entire
    payload."""
    if not text:
        return ""
    return escape_text(text.strip())


# ---------------------------------------------------------------------------
# intelligence_scraper.py report -> display shape
# ---------------------------------------------------------------------------

def prepare_scrape_report_for_display(
    report: Dict[str, Any],
    *,
    base_domain: Optional[str] = None,
    max_text_length: int = 80,
    max_images_per_post: int = _MAX_POST_IMAGES,
    max_videos_per_post: int = _MAX_POST_VIDEOS,
    max_links_per_post: int = _MAX_POST_LINKS,
) -> Dict[str, Any]:
    """
    Takes the dict returned by intelligence_scraper.scrape_links() /
    scrape_service_links() and turns it into a template-ready shape.

    Each scraped entry is a full "intelligence post":
      - title/description/thumbnail, preferring OG, then Twitter Card,
        then page meta for title/description, and OG image -> Twitter
        image -> first extracted image (in that order) for the
        thumbnail — same fallback chain a primary scrape's own display
        already uses.
      - author / published_time, when the page exposed them.
      - word_count / reading_time_minutes, using scrape_url()'s own
        figures when present, otherwise derived from the extracted text
        (~200 words/minute) so older or partial scrape_url() outputs
        still get a usable estimate.
      - a capped, sanitized image strip (images / image_count) and a
        capped video list (videos / video_count) — see
        _prepare_post_images / _prepare_post_videos.
      - outbound_link_count: how many links THIS scanned page itself
        contains (not followed — just counted).
      - product_info: a flattened, labeled dict when the page carries
        commerce structured data; omitted entirely otherwise.
      - structured_summary: a compact, labeled fact block (article/
        recipe/event/video/FAQ/organization/social-embed) built from
        intelligence_scraper.py's own JSON-LD detection
        (LinkScrapeResult.structured_summary), when that page's markup
        matched one of the recognized @type patterns; omitted entirely
        otherwise. See _humanize_structured_summary.
      - intel: page-level signal beyond the primary-scrape fields above
        — language, keywords, H1/H2/H3 outline, favicon, on-page social
        links, on-page contact info (mailto:/tel: links + emails found
        in text), freshness headers, basic response stats, a hero
        image, this page's own outbound-domain breakdown, and a rough
        content-quality signal (text-to-HTML ratio / thin-content
        flag). See intelligence_scraper.py's _extract_page_intel and
        _prepare_page_intel above; omitted (empty dict) when the page
        yielded nothing worth showing.
      - a text_snippet (truncated, escaped excerpt of the page's
        extracted text) for an expandable "read more" section.
      - favicon / is_external / rel / duration / depth, same as before.

    Skipped/error entries get a short human-readable reason instead of
    the raw internal code, unchanged from the prior behavior.

    A scraped entry whose own URL somehow isn't safely displayable
    (shouldn't happen — intelligence_scraper already runs everything
    through validate_crawl_url — but this function does its own
    independent check rather than trusting an upstream invariant) is
    moved into `skipped` instead of silently disappearing.

    Does not mutate `report` — returns a new dict.
    """
    scraped_out: List[Dict[str, Any]] = []
    demoted: List[Dict[str, str]] = []

    for entry in report.get("scraped", []):
        href = entry.get("url", "")
        if not is_displayable_href(href):
            demoted.append({"href": href, "reason": "Link address isn't safe to display"})
            continue

        data = entry.get("data") or {}
        og = data.get("og") or {}
        twitter = data.get("twitter") or {}
        meta = data.get("meta") or {}
        domain = entry.get("domain") or extract_domain_for_display(href)
        is_external = bool(base_domain) and domain != base_domain

        title = og.get("title") or twitter.get("title") or meta.get("title") or href
        description = og.get("description") or twitter.get("description") or meta.get("description") or ""
        thumbnail = og.get("thumbnail") or twitter.get("image") or ""

        raw_images = data.get("images") or []
        images, image_count = _prepare_post_images(raw_images, max_images_per_post)
        if not thumbnail and images:
            thumbnail = images[0]["url"]
        if not _is_displayable_image_src(thumbnail):
            thumbnail = ""

        raw_videos = data.get("videos") or []
        videos, video_count = _prepare_post_videos(raw_videos, max_videos_per_post)

        raw_links = data.get("links") or []
        links = prepare_links_for_display(
            raw_links, base_domain=domain, max_text_length=max_text_length,
            limit=max_links_per_post,
        )

        text = data.get("text") or ""
        word_count = data.get("word_count") or (len(text.split()) if text else 0)
        reading_time = data.get("reading_time_minutes") or (
            max(1, round(word_count / 200)) if word_count else 0
        )

        structured_summary = _humanize_structured_summary(entry.get("structured_summary"))
        intel = _prepare_page_intel(data.get("intel"))

        scraped_out.append({
            "href": href,
            "title": truncate_text(escape_text(title), max_text_length),
            "description": truncate_text(escape_text(description), 220),
            "thumbnail": thumbnail,
            "domain": domain,
            "favicon": favicon_url_for_domain(domain),
            "is_external": is_external,
            "rel": external_rel_attribute(is_external),
            "duration": humanize_duration_ms(entry.get("duration_ms")),
            "depth": entry.get("depth", 1),
            "author": escape_text(data.get("author") or ""),
            "published_time": escape_text(data.get("published_time") or ""),
            "canonical_url": data.get("canonical_url") or href,
            "word_count": word_count,
            "reading_time_minutes": reading_time,
            "text_snippet": _prepare_text_snippet(text),
            "images": images,
            "image_count": image_count,
            "videos": videos,
            "video_count": video_count,
            "outbound_link_count": len(data.get("links") or []),
            "product_info": _prepare_product_info(data.get("product_info")),
            "structured_summary": structured_summary,
            "intel": intel,
            "status": "success",

            "outbound_link_count": len(data.get("links") or []),
            "links": links,


        })

    skipped_out = [
        {"href": item.get("url", ""), "reason": humanize_skip_reason(item.get("reason"))}
        for item in report.get("skipped", [])
    ] + demoted

    errors_out = [
        {"href": item.get("url", ""), "reason": "Couldn't be reached"}
        for item in report.get("errors", [])
    ]

    return {
        "scraped": scraped_out,
        "skipped": skipped_out,
        "errors": errors_out,
        "counts": {
            "scraped": len(scraped_out),
            "skipped": len(skipped_out),
            "errors": len(errors_out),
            "requested": report.get("requested", 0),
        },
        "truncated": bool(report.get("truncated")),
    }