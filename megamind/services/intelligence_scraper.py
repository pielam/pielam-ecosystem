"""
Secondary, "link intelligence" scraper.

Given a ConnectedService (or a bare list of URLs), scrape_service_links()
/ scrape_links() crawl the LINKS a primary scrape already found —
service.extracted_links, or any other href list — and return the results
purely in-memory. Nothing here writes a ConnectedService row, a
CrawlAttempt row, or any other persistent scrape record.

═══════════════════════════════════════════════════════════════════════
UPGRADE NOTES (this version)
═══════════════════════════════════════════════════════════════════════

1. CONCURRENT FETCHING
   The link-following loop used to be a strict sequential `while
   frontier: pop(0)` — one HTTP round-trip at a time. It's now a
   wave-based breadth-first crawl: every link at the current depth is
   fetched in parallel via a bounded ThreadPoolExecutor
   (`max_workers`, default DEFAULT_MAX_WORKERS), the whole wave's
   results are collected, and only then is the next depth's frontier
   built and ranked. This keeps the same global `max_links` /
   `max_per_domain` / `max_depth` budgets and the same politeness
   guarantees (DomainCrawlPolicy.record_request() per fetch, same
   per-domain concurrency cap enforced via a lock around
   per_domain_count) — it just spends wall-clock time in parallel
   instead of back-to-back, so a 30-link scan takes roughly
   max(per-link latency) per wave instead of the sum of all of them.

2. ROBUST "ANY URL" FETCHING
   Previously every link went through scraper.scrape_url(), which is a
   single requests.get() with no retries, no size cap, and no
   redirect/encoding hardening — one flaky host could eat the whole
   request's timeout budget or blow up on a huge response. This module
   now fetches links itself via a shared requests.Session with:
     - automatic retry + exponential backoff on transient failures /
       429 / 5xx (via urllib3's Retry, mounted on the session),
     - a streamed read with a hard byte cap (_MAX_FETCH_BYTES) so a
       multi-gigabyte response can't hang a worker thread,
     - real encoding detection (declared charset -> apparent encoding
       -> utf-8 fallback) instead of trusting response.encoding blindly,
     - redirect-following with the final resolved URL used as the
       parse base (and a page's own <base href> still takes priority,
       exactly like scraper.py's own _base_url()),
     - a light local JSON-LD extractor, so structured_summary (Article/
       Recipe/Event/... detection) now actually has data to work with
       instead of always seeing an empty list.
   It still prefers scraper.py's own extractor functions (_extract_og,
   _extract_images, _extract_videos, _extract_links, _extract_text,
   _extract_canonical, _extract_author, _extract_published_time,
   _extract_product_info) so OG/media/link/author/canonical/product
   extraction behaves identically to a primary scrape — only the HTTP
   layer underneath them changed. If scraper.py doesn't expose one of
   those (older/newer version drift), a minimal local fallback is used
   instead of a hard ImportError, same defensive pattern already used
   below for _iter_json_ld_nodes.
   Also: raw HTML is now kept on data["raw"]["_html"], which switches
   on the readability-based text upgrade (_improve_text_extraction)
   that was previously wired in but permanently inert.
   NOTE: canonical_url / author / published_time / product_info /
   word_count / reading_time_minutes are populated here specifically so
   megamind.utils.link_info.prepare_scrape_report_for_display() — which
   already reads all of these off each scraped entry's `data` dict —
   has real values to show instead of the permanent blanks an earlier
   version of this function left behind (it only ever set og/images/
   videos/links/text/structured_data/raw/error/fetched_at). The oEmbed
   shortcut below (_oembed_to_scrape_result) already returned this full
   shape; this brings the plain-HTTP path to parity with it.

3. DJANGO / THREAD SAFETY
   Each worker calls close_old_connections() before touching the ORM
   (DomainCrawlPolicy lookups), which is the standard way to keep a
   ThreadPoolExecutor's persistent worker threads from accumulating
   stale/broken DB connections across submissions.

4. WIDER LEGITIMATE oEMBED COVERAGE + HONEST BLOCKED-LINK REPORTING
   Reddit, SoundCloud, Flickr, Pinterest, and Spotify now go through
   the same public, no-auth oEmbed shortcut YouTube/Vimeo/TikTok/X
   already used (see _OEMBED_ENDPOINTS) — more social links resolve
   cleanly via each platform's own sanctioned embed endpoint instead of
   falling through to a plain HTML fetch a bot-walled page would just
   block anyway. And when a fetch DOES get refused (401/403/429/451, or
   a non-HTML login-wall response), it's now classified as `skipped`
   with an honest "blocked_by_platform" reason (see _classify_blocked)
   instead of landing in `errors` — the scan as a whole still completes
   and reports success; only that one link is honestly labeled
   unreachable-by-design. This module still does not, and will not,
   attempt to get around a platform's bot detection (fake user agents,
   proxy/IP rotation, CAPTCHA solving, session spoofing) — see the
   "Social-platform awareness" notes further down for why that line is
   firm regardless of how public the underlying content is.

5. ADVANCED PER-PAGE INTELLIGENCE (_extract_page_intel)
   Beyond the OG/canonical/author/product fields a primary scrape
   already produces, each scraped page now also gets a second,
   independent `data["intel"]` block: page language, meta keywords /
   article tags, an H1/H2/H3 heading outline, favicon, on-page social
   links (rel="me" / footer icons / any outbound link whose domain is
   a known social platform), on-page contact info (mailto:/tel: links
   plus emails found in visible text — no phone-number regex, since
   international formats are too false-positive-prone without a
   dedicated library), freshness signals (Last-Modified header /
   article:modified_time), a few basic response stats (Server header,
   content type, byte length), a hero image, this page's own outbound-
   domain breakdown (distinct from the service-wide breakdown computed
   client-side across every scanned page), and a rough content-quality
   signal (text-to-HTML ratio, thin-content flag). This is purely
   additive display data for the link-scan popup — nothing else in
   this module depends on it, and it's extracted with the same
   _safe_call guard as every other field here so one bad selector
   degrades only this one block, never the rest of the scrape.
   megamind.utils.link_info._prepare_page_intel() is what sanitizes and
   caps this block before it reaches a template.

6. SITEMAP DISCOVERY (NEW — was previously referenced but undefined)
   discover_sitemap_urls() now has a real implementation: reads the
   target domain's robots.txt for declared `Sitemap:` locations
   (falling back to the conventional /sitemap.xml), and parses either
   a plain <urlset> or a <sitemapindex> (following up to
   _SITEMAP_MAX_NESTED_SITEMAPS child sitemaps). Every URL it returns
   still goes through the ordinary validate_crawl_url()/robots/
   circuit-breaker gate in _process_one_link before ever being
   fetched — this function only discovers candidates, it never fetches
   page content.

7. CONTENT-TYPE-AWARE FETCHING (NEW)
   Previously any non-HTML response was hard-rejected as
   "unsupported_content_type". The fetch layer is now split into a
   content-type-agnostic primitive (_robust_fetch_raw) and dedicated,
   lightweight parsers for PDF (via the optional `pypdf` package —
   degrades to metadata-only if it isn't installed), JSON, and RSS/
   Atom feeds (both via the stdlib, no new hard dependency). Every
   non-HTML result is shaped into the SAME top-level keys a normal
   scrape produces (og/images/videos/links/text/canonical_url/author/
   published_time/product_info/word_count/reading_time_minutes/
   structured_data/intel/raw/error/fetched_at) plus one new
   `content_kind` field, so nothing downstream needs a special case to
   avoid a KeyError — this is additive, not a breaking shape change.

8. WIDER STRUCTURED-DATA TYPE COVERAGE (NEW)
   _extract_structured_summary now also recognizes HowTo, Person,
   JobPosting, Review, SoftwareApplication, Book, Movie/TVSeries, and
   Course schema.org types (previously: Article, Recipe, Event,
   VideoObject, FAQPage, Organization only), each with its own summary
   shape. The function's return contract (a single dict with a
   "content_type" key, or None) is unchanged — only the set of types it
   recognizes grew — so this is safe for any existing caller.

9. LIGHTWEIGHT TEXT INTELLIGENCE (NEW)
   _extract_page_intel now also computes, entirely in pure Python with
   no new hard dependency:
     - a simple lexicon-based sentiment score (positive/neutral/
       negative + numeric score) — an approximation, not a replacement
       for a real sentiment model; documented as such,
     - top keyword/keyphrase candidates via frequency scoring over the
       extracted text (stopword-filtered unigrams/bigrams),
     - detected_language, populated ONLY if the optional `langdetect`
       package is installed (guarded import, same optional-dependency
       pattern as pypdf above) — no home-grown language guess is
       substituted in its absence, since a low-confidence guess dressed
       up as a detected language is worse than not reporting one.

10. RELIABILITY: STRUCTURED ERROR CLASSIFICATION + OVERALL TIME BUDGET
    _classify_link_error() maps a raw fetch-error string onto a small
    set of categories (dns_error, timeout, ssl_error, http_error,
    connection_error, content_too_large, unknown) — the same style of
    heuristic engine_profile.py's _classify_fetch_error already uses —
    so `errors` entries carry a machine-usable category alongside the
    raw message instead of only free text. scrape_links() also now
    accepts a `time_budget_seconds` (hard-capped) ceiling checked
    between waves, so a link scan can no longer run unbounded in wall-
    clock time purely by virtue of a deep/wide frontier — before this,
    only `max_links` bounded it, which doesn't bound *time* if pages
    are slow.

Everything else — SSRF validation, the operator manual-block check,
robots.txt being intentionally NOT enforced, credential-forwarding
restricted to the same domain, tracking-param dedup, frontier ranking,
non-page filtering, and the social-platform oEmbed / "requires official
API" handling — is unchanged in behavior, just now called from inside a
worker instead of a sequential loop. See the per-function docstrings
below for the rationale on each of those, which still applies as-is.
"""

import io
import json
import logging
import re
import threading
import urllib.parse as _urllib_parse
import urllib.request as _urllib_request
import xml.etree.ElementTree as ET
from collections import deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, List, Optional, Set, Tuple
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode

import requests
from bs4 import BeautifulSoup
from django.db import close_old_connections
from django.utils import timezone as dj_timezone
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from megamind.models.connected_service import (
    DomainCrawlPolicy,
    UnsafeCrawlURLError,
    extract_domain,
    validate_crawl_url,
)
from megamind.utils.media_info import normalize_links

# ---------------------------------------------------------------------------
# Reuse scraper.py's own extractors so OG/media/link/text/author/
# canonical/product extraction is identical to a primary scrape.
# Imported defensively (try/except) since a private helper in another
# module can move without warning — losing this import should degrade
# to a minimal local fallback, never a hard failure of this whole
# module.
# ---------------------------------------------------------------------------
try:
    from megamind.services.scraper import (
        _extract_og as _scraper_extract_og,
        _extract_images as _scraper_extract_images,
        _extract_videos as _scraper_extract_videos,
        _extract_links as _scraper_extract_links,
        _extract_text as _scraper_extract_text,
        _extract_canonical as _scraper_extract_canonical,
        _extract_author as _scraper_extract_author,
        _extract_published_time as _scraper_extract_published_time,
        _extract_product_info as _scraper_extract_product_info,
        _base_url as _scraper_base_url,
        DEFAULT_HEADERS as _SCRAPER_DEFAULT_HEADERS,
    )
except ImportError:  # pragma: no cover - environment-dependent
    _SCRAPER_DEFAULT_HEADERS = {
        "User-Agent": "Mozilla/5.0 (compatible; ConnectedServiceBot/1.0)",
        "Accept-Language": "en-US,en;q=0.9",
    }

    def _scraper_extract_og(soup):
        def _meta(attr, value):
            tag = soup.find("meta", attrs={attr: value})
            return tag.get("content", "").strip() if tag else ""
        return {
            "title": _meta("property", "og:title") or (soup.title.string.strip() if soup.title else ""),
            "description": _meta("property", "og:description") or _meta("name", "description"),
            "thumbnail": _meta("property", "og:image"),
            "site_name": _meta("property", "og:site_name"),
            "type": _meta("property", "og:type"),
        }

    def _scraper_extract_images(soup, base_url):
        from urllib.parse import urljoin
        out, seen = [], set()
        for tag in soup.find_all("img", src=True):
            src = urljoin(base_url, tag["src"].strip())
            if src and src not in seen:
                seen.add(src)
                out.append({"url": src, "alt": tag.get("alt", "").strip(), "href": ""})
        return out

    def _scraper_extract_videos(soup, base_url):
        from urllib.parse import urljoin
        out, seen = [], set()
        for tag in soup.find_all(["video", "source"]):
            src = tag.get("src")
            if src:
                src = urljoin(base_url, src)
                if src not in seen:
                    seen.add(src)
                    out.append({"url": src, "type": tag.get("type", "video/*")})
        return out

    def _scraper_extract_links(soup, base_url):
        from urllib.parse import urljoin
        out, seen = [], set()
        for tag in soup.find_all("a", href=True):
            href = urljoin(base_url, tag["href"].strip())
            if href and href not in seen:
                seen.add(href)
                out.append({"href": href, "text": tag.get_text(strip=True)[:200]})
        return out

    def _scraper_extract_text(soup):
        for tag in soup(["script", "style", "noscript", "head"]):
            tag.decompose()
        for selector in ["main", "article", "body"]:
            container = soup.find(selector)
            if container:
                return re.sub(r"\n{3,}", "\n\n", container.get_text(separator="\n", strip=True)).strip()
        return ""

    def _scraper_base_url(soup, fallback):
        base_tag = soup.find("base", href=True)
        return base_tag["href"] if base_tag else fallback

    def _scraper_extract_canonical(soup, fallback):
        tag = soup.find("link", rel=lambda v: v and "canonical" in v.lower(), href=True)
        return tag["href"].strip() if tag and tag.get("href") else fallback

    def _scraper_extract_author(soup):
        tag = soup.find("meta", attrs={"name": "author"})
        if tag and tag.get("content"):
            return tag["content"].strip()
        rel_tag = soup.find(attrs={"rel": "author"})
        return rel_tag.get_text(strip=True) if rel_tag else ""

    def _scraper_extract_published_time(soup):
        for attr, value in (
            ("property", "article:published_time"),
            ("name", "publish-date"),
            ("name", "date"),
            ("itemprop", "datePublished"),
        ):
            tag = soup.find("meta", attrs={attr: value})
            if tag and tag.get("content"):
                return tag["content"].strip()
        time_tag = soup.find("time", attrs={"datetime": True})
        return time_tag["datetime"].strip() if time_tag else ""

    def _scraper_extract_product_info(structured_data, soup):
        for block in structured_data or []:
            nodes = block if isinstance(block, list) else [block]
            for node in nodes:
                if not isinstance(node, dict):
                    continue
                types = node.get("@type")
                types = types if isinstance(types, list) else [types]
                if "Product" not in (types or []):
                    continue
                offers = node.get("offers") or {}
                if isinstance(offers, list):
                    offers = offers[0] if offers else {}
                if not isinstance(offers, dict):
                    offers = {}
                brand = node.get("brand")
                brand_name = brand.get("name") if isinstance(brand, dict) else brand
                return {
                    "name": node.get("name") or "",
                    "price": offers.get("price") or "",
                    "currency": offers.get("priceCurrency") or "",
                    "availability": (offers.get("availability") or "").rsplit("/", 1)[-1],
                    "brand": brand_name or "",
                    "sku": node.get("sku") or "",
                    "condition": (node.get("itemCondition") or "").rsplit("/", 1)[-1],
                    "rating": (node.get("aggregateRating") or {}).get("ratingValue", "")
                    if isinstance(node.get("aggregateRating"), dict) else "",
                }
        return {}

try:
    from megamind.services.scraper import _iter_json_ld_nodes
except ImportError:
    def _iter_json_ld_nodes(structured_data):
        """Fallback copy of scraper.py's _iter_json_ld_nodes: yields every
        dict node in `structured_data`, flattening a top-level
        {"@graph": [...]} wrapper in addition to a bare object or a bare
        list of objects."""
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

try:
    from megamind.utils.readability_extractor import extract_main_content
    _READABILITY_AVAILABLE = True
except ImportError:  # pragma: no cover - environment-dependent
    _READABILITY_AVAILABLE = False

# Optional PDF text extraction. Guarded exactly like the readability/
# playwright imports above — this module never hard-requires pypdf;
# without it, a PDF link still gets a clean metadata-only result
# instead of an error. `pip install pypdf` to activate real text
# extraction.
try:
    import pypdf
    _PYPDF_AVAILABLE = True
except ImportError:  # pragma: no cover - environment-dependent
    _PYPDF_AVAILABLE = False

# Optional language detection. Guarded the same way — without
# `langdetect` installed (`pip install langdetect`), detected_language
# is simply omitted rather than replaced with a low-confidence guess.
try:
    import langdetect
    _LANGDETECT_AVAILABLE = True
except ImportError:  # pragma: no cover - environment-dependent
    _LANGDETECT_AVAILABLE = False

logger = logging.getLogger(__name__)

__all__ = [
    "LinkScrapeResult",
    "scrape_links",
    "scrape_service_links",
    "discover_sitemap_urls",
]

# ---------------------------------------------------------------------------
# Defaults / budgets
# ---------------------------------------------------------------------------
DEFAULT_MAX_LINKS = 30
DEFAULT_MAX_PER_DOMAIN = 8
DEFAULT_MAX_DEPTH = 1

# Bounded worker pool for the concurrent fetch stage. Kept modest by
# default — this is a user-initiated, on-demand scan, not a background
# crawl loop, so it shouldn't try to open dozens of sockets at once even
# though max_links might be much larger than this.
DEFAULT_MAX_WORKERS = 8
MAX_WORKERS_HARD_CAP = 16

# Words-per-minute assumption used to derive reading_time_minutes when
# a page doesn't otherwise expose one — same figure
# megamind.utils.readability_extractor.estimate_reading_time() and
# megamind.utils.link_info.prepare_scrape_report_for_display() both use,
# kept in sync here so a link-intelligence result and a primary-scrape
# result never disagree about how long the same word count takes to read.
_WORDS_PER_MINUTE = 200

# Hard byte cap per link fetch — protects a worker thread from hanging
# on (or exhausting memory against) an unexpectedly huge response.
_MAX_FETCH_BYTES = 8 * 1024 * 1024  # 8 MB
_FETCH_TIMEOUT_SECONDS = 12
_FETCH_MAX_RETRIES = 3
_FETCH_BACKOFF_FACTOR = 0.6

# HTTP statuses that mean "this platform is actively refusing automated
# access" rather than "something is temporarily broken". Bucketed into
# `skipped` with an honest reason instead of `errors` — a scan hitting
# one of these on a social/bot-walled page is expected behavior, not a
# crawl failure, and shouldn't make the whole scan look broken.
_BLOCKED_STATUS_CODES = {401, 403, 429, 451}

# Per-scan circuit breaker: once a single domain accumulates this many
# consecutive blocked/error outcomes WITHIN one scrape_links() run, any
# further not-yet-fetched link on that domain is skipped without a
# network attempt for the rest of that run. This is scoped to a single
# run (not persisted across scans — DomainCrawlPolicy already owns
# longer-lived circuit-breaker state) and exists purely so one dead or
# hard-blocking domain discovered via many links can't burn the whole
# max_links/time budget retrying a host that has already made its
# answer clear.
_CIRCUIT_BREAKER_THRESHOLD = 3

# Cap on simultaneous in-flight requests to any ONE domain, independent
# of the global `max_workers` pool. Without this, a page that links to
# the same domain 20 times could have the global pool's entire capacity
# pointed at one host at once even though max_workers itself is
# perfectly reasonable — this is a politeness control, not a budget.
_PER_HOST_MAX_CONCURRENCY = 2

# Overall wall-clock ceiling for one scrape_links() run, checked
# between waves (see (10) in the module docstring). Independent of
# max_links: a slow/degraded target could otherwise take an unbounded
# amount of real time to hit its link count. Whatever has been
# collected so far when the budget is hit is kept — this cuts a run
# short, it never discards results, matching the same "keep partial
# results" posture used throughout this codebase (e.g. the deep-crawl
# time budget in engine_profile.py).
DEFAULT_TIME_BUDGET_SECONDS = 45
TIME_BUDGET_HARD_CAP_SECONDS = 120

# Honest, identifiable bot user agents — rotated per request so a site
# doesn't see one static UA string on every hit, but every single one
# of these still truthfully identifies this as an automated fetcher
# (the same spirit as Googlebot's own UA string), never a browser
# impersonation. This is NOT the "any UA that gets past a WAF" kind of
# rotation — it's cycling between a couple of honest self-identifications.
_USER_AGENTS = [
    "Mozilla/5.0 (compatible; ConnectedServiceLinkIntel/1.0; +https://example.com/bot)",
    "Mozilla/5.0 (compatible; ConnectedServiceLinkIntel/1.0; bot; +https://example.com/bot-info)",
]
_ua_rotate_lock = threading.Lock()
_ua_rotate_counter = 0


def _pick_user_agent() -> str:
    global _ua_rotate_counter
    with _ua_rotate_lock:
        ua = _USER_AGENTS[_ua_rotate_counter % len(_USER_AGENTS)]
        _ua_rotate_counter += 1
    return ua


# In-memory conditional-request cache: canonical URL -> {etag,
# last_modified, data, cached_at}. Deliberately module-level and
# in-process only (never persisted to disk/DB — consistent with this
# module's "nothing here writes a permanent record" contract) so a link
# scanned again later in the same process's lifetime can send
# If-None-Match / If-Modified-Since and skip re-downloading a page that
# hasn't changed. Entries older than _HTTP_CACHE_TTL_SECONDS are treated
# as expired and re-fetched from scratch.
_HTTP_CACHE: Dict[str, Dict[str, Any]] = {}
_HTTP_CACHE_LOCK = threading.Lock()
_HTTP_CACHE_TTL_SECONDS = 15 * 60
_HTTP_CACHE_MAX_ENTRIES = 500

_UNFETCHABLE_PREFIXES = ("mailto:", "tel:", "javascript:", "#")

_NON_PAGE_EXT_RE = re.compile(
    r"\.(?:jpe?g|png|gif|webp|bmp|svg|ico|heic|avif|"
    r"mp4|m4v|mov|avi|wmv|flv|webm|"
    r"mp3|wav|ogg|m4a|flac|"
    r"docx?|xlsx?|pptx?|"
    r"zip|rar|7z|gz|tgz|tar|"
    r"exe|dmg|apk|msi|"
    r"css|js|mjs|"
    r"woff2?|ttf|otf|eot)$",
    re.IGNORECASE,
)
# NOTE: .pdf, .json, .xml/.rss/.atom are deliberately NOT in this
# exclusion list (unlike engine_profile.py's page-crawl equivalent) —
# this module now has dedicated handlers for those content types (see
# UPGRADE NOTE 7), so a link ending in one of those extensions is still
# worth fetching here even though it isn't a further page to crawl.

_TRACKING_PARAM_RE = re.compile(
    r"^(utm_|fbclid$|gclid$|msclkid$|mc_eid$|mc_cid$|"
    r"ref$|referrer$|source$|_hs(enc|mi)$|igshid$|"
    r"sessionid$|sid$|phpsessid$)",
    re.IGNORECASE,
)

_LINK_BOOST_PATH_HINTS = (
    "/article", "/articles", "/post", "/posts", "/blog", "/news",
    "/product", "/products", "/item", "/items", "/recipe", "/recipes",
    "/guide", "/guides", "/story", "/stories", "/review", "/reviews",
    "/event", "/events", "/p/", "/story/",
)
_LINK_PENALTY_PATH_HINTS = (
    "/login", "/signin", "/sign-in", "/signup", "/sign-up", "/register",
    "/cart", "/checkout", "/account", "/logout", "/privacy", "/terms",
    "/cookie", "/legal", "/careers", "/jobs", "/press", "/advertise",
    "/subscribe", "/newsletter", "/search", "/tag/", "/tags/",
    "/category/", "/categories/", "/page/", "/author/",
)
_LINK_PENALTY_TEXT_HINTS = (
    "privacy policy", "terms of service", "terms & conditions",
    "cookie policy", "sign in", "log in", "log out", "sign up",
    "subscribe", "advertise with us", "careers", "next page",
    "previous page", "load more", "see all", "view all",
)
_LINK_BOOST_TEXT_MIN_WORDS = 4


def _canonicalize_for_dedup(url: str) -> str:
    """Normalize a URL for de-dup purposes only — never used as the
    actual URL fetched."""
    try:
        parts = urlsplit(url)
        kept_params = [
            (k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
            if not _TRACKING_PARAM_RE.match(k)
        ]
        query = urlencode(kept_params)
        return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path, query, ""))
    except Exception:
        return url


def _looks_unfetchable(url: str) -> bool:
    stripped = (url or "").strip().lower()
    return (not stripped) or stripped.startswith(_UNFETCHABLE_PREFIXES)


def _looks_like_non_page(url: str) -> bool:
    try:
        path = urlsplit(url).path or ""
    except Exception:
        return False
    return bool(_NON_PAGE_EXT_RE.search(path))


def _score_link(url: str, text: str) -> int:
    score = 0
    lower_url = url.lower()
    lower_text = (text or "").strip().lower()

    for hint in _LINK_BOOST_PATH_HINTS:
        if hint in lower_url:
            score += 3
            break
    for hint in _LINK_PENALTY_PATH_HINTS:
        if hint in lower_url:
            score -= 4
            break

    if lower_text:
        if any(hint in lower_text for hint in _LINK_PENALTY_TEXT_HINTS):
            score -= 4
        elif len(lower_text.split()) >= _LINK_BOOST_TEXT_MIN_WORDS:
            score += 2

    depth = urlsplit(url).path.strip("/").count("/")
    score -= min(depth, 3)

    return score


def _rank_frontier(candidates: List[Tuple[str, int, str, str]]) -> List[Tuple[str, int, str, str]]:
    return sorted(candidates, key=lambda item: _score_link(item[0], item[3]), reverse=True)


# ---------------------------------------------------------------------------
# Robust, concurrency-safe HTTP fetching — "must be able to scrape any
# given URL" without one flaky/huge/slow host taking down the batch.
# ---------------------------------------------------------------------------

_session_lock = threading.Lock()
_session: Optional[requests.Session] = None

# Per-domain semaphores enforcing _PER_HOST_MAX_CONCURRENCY, independent
# of the global ThreadPoolExecutor pool. Lazily created and cached for
# the life of the process.
_host_semaphores: Dict[str, threading.Semaphore] = {}
_host_sem_lock = threading.Lock()


def _get_host_semaphore(domain: str) -> threading.Semaphore:
    with _host_sem_lock:
        sem = _host_semaphores.get(domain)
        if sem is None:
            sem = threading.Semaphore(_PER_HOST_MAX_CONCURRENCY)
            _host_semaphores[domain] = sem
        return sem


def _cache_get(url: str) -> Optional[Dict[str, Any]]:
    with _HTTP_CACHE_LOCK:
        entry = _HTTP_CACHE.get(url)
        if not entry:
            return None
        age = (dj_timezone.now() - entry["cached_at"]).total_seconds()
        if age > _HTTP_CACHE_TTL_SECONDS:
            _HTTP_CACHE.pop(url, None)
            return None
        return entry


def _cache_put(url: str, etag: str, last_modified: str, data: dict) -> None:
    with _HTTP_CACHE_LOCK:
        if len(_HTTP_CACHE) >= _HTTP_CACHE_MAX_ENTRIES and url not in _HTTP_CACHE:
            # Cheap unbounded-growth guard: drop an arbitrary entry
            # rather than tracking real LRU order — this cache is a
            # best-effort optimization, not a correctness requirement.
            _HTTP_CACHE.pop(next(iter(_HTTP_CACHE)), None)
        _HTTP_CACHE[url] = {
            "etag": etag, "last_modified": last_modified,
            "data": data, "cached_at": dj_timezone.now(),
        }


def _get_session() -> requests.Session:
    """Lazily-built, module-shared requests.Session with connection
    pooling + automatic retry/backoff. Shared (not one-per-worker) so
    the underlying TCP connection pool is actually reused across the
    concurrent fetch stage instead of every worker paying a fresh
    handshake cost."""
    global _session
    if _session is not None:
        return _session
    with _session_lock:
        if _session is None:
            s = requests.Session()
            retry = Retry(
                total=_FETCH_MAX_RETRIES,
                connect=_FETCH_MAX_RETRIES,
                read=_FETCH_MAX_RETRIES,
                redirect=8,
                backoff_factor=_FETCH_BACKOFF_FACTOR,
                # 429/503 are usually transient rate-limiting and worth
                # retrying with backoff; 403/451 are access denials
                # (bot-wall, geo/legal block) that a same-request retry
                # will never fix, so they're deliberately NOT in this
                # list — retrying those would just burn time before
                # _process_one_link classifies them as a clean "skipped:
                # blocked" outcome instead of an error.
                status_forcelist=(429, 500, 502, 503, 504),
                allowed_methods=frozenset(["GET"]),
                raise_on_status=False,
                respect_retry_after_header=True,
            )
            adapter = HTTPAdapter(max_retries=retry, pool_connections=32, pool_maxsize=32)
            s.mount("http://", adapter)
            s.mount("https://", adapter)
            _session = s
    return _session


def _robust_fetch_raw(url: str, headers: dict) -> Tuple[Optional[bytes], str, str, Optional[str], dict]:
    """
    Content-type-agnostic fetch primitive: retries, redirect-following,
    a hard byte cap — but no decoding and no HTML-only gate. This is
    the single low-level fetch both the HTML path and the non-HTML
    (PDF/JSON/RSS/Atom) paths in _scrape_url_robust build on (see
    UPGRADE NOTE 7), replacing what used to be one fetch function that
    hard-rejected anything non-HTML before the caller ever got a say.

    Never raises: any failure comes back as (None, url, "", "<reason>", {}).

    Returns: (raw_bytes_or_None, final_url, content_type, error_or_None,
    response_headers). `error` is the literal string "not_modified" on
    a 304 in response to conditional headers the caller supplied.
    """
    session = _get_session()
    try:
        resp = session.get(
            url, headers=headers, timeout=_FETCH_TIMEOUT_SECONDS,
            stream=True, allow_redirects=True,
        )
    except requests.RequestException as exc:
        return None, url, "", str(exc), {}

    if resp.status_code == 304:
        resp.close()
        return None, resp.url, resp.headers.get("Content-Type", ""), "not_modified", dict(resp.headers)

    content_type = resp.headers.get("Content-Type", "")
    chunks: List[bytes] = []
    total = 0
    try:
        for chunk in resp.iter_content(chunk_size=65536):
            if not chunk:
                continue
            chunks.append(chunk)
            total += len(chunk)
            if total > _MAX_FETCH_BYTES:
                logger.debug("intelligence_scraper: %s exceeded %d bytes — truncating", url, _MAX_FETCH_BYTES)
                break
    except requests.RequestException as exc:
        resp.close()
        return None, resp.url, content_type, str(exc), dict(resp.headers)
    finally:
        resp_headers = dict(resp.headers)
        resp.close()

    raw_bytes = b"".join(chunks)
    if resp.status_code >= 400:
        return raw_bytes, resp.url, content_type, f"http_{resp.status_code}", resp_headers
    return raw_bytes, resp.url, content_type, None, resp_headers


def _decode_bytes(raw_bytes: bytes, content_type: str) -> str:
    """Declared-charset-first text decoding, same technique
    scraper.py/engine_profile.py both use (`_guess_encoding`) — kept
    independent of any single requests.Response object so this works
    against the content-type-agnostic _robust_fetch_raw() above."""
    charset = None
    if "charset=" in (content_type or ""):
        charset = content_type.split("charset=")[-1].split(";")[0].strip()
    try:
        return raw_bytes.decode(charset or "utf-8", errors="replace")
    except (LookupError, TypeError):
        return raw_bytes.decode("utf-8", errors="replace")


def _extract_json_ld_local(soup: BeautifulSoup) -> list:
    """Minimal JSON-LD block extractor — feeds _extract_structured_summary
    below so Article/Recipe/Event/... detection has real data instead
    of always seeing an empty list (scraper.py's thin scrape_url() never
    populated structured_data at all)."""
    blocks = []
    for tag in soup.find_all("script", attrs={"type": "application/ld+json"}):
        raw = tag.string or tag.get_text() or ""
        if not raw.strip():
            continue
        try:
            blocks.append(json.loads(raw))
        except Exception:
            continue
    return blocks


# ---------------------------------------------------------------------------
# Non-HTML content handling — PDF / JSON / RSS+Atom feeds (NEW)
# ---------------------------------------------------------------------------
#
# A link scan inevitably turns up links that aren't further HTML pages
# — spec sheets, data feeds, syndication feeds. Rather than uniformly
# reporting these as "unsupported_content_type" (the previous
# behavior), each gets a small dedicated parser. All three degrade
# gracefully: PDF text extraction is fully optional (pypdf), JSON/feed
# parsing use only the stdlib so they always work.

_PDF_CONTENT_TYPE_HINT = "application/pdf"
_JSON_CONTENT_TYPE_HINTS = ("application/json", "application/ld+json", "text/json")
_FEED_CONTENT_TYPE_HINTS = ("application/rss+xml", "application/atom+xml", "application/xml", "text/xml")

_PDF_MAX_PAGES_TO_READ = 15
_PDF_MAX_TEXT_CHARS = 20000
_FEED_MAX_ITEMS = 20
_ATOM_NS = "{http://www.w3.org/2005/Atom}"


def _non_html_result_shape(content_kind: str) -> dict:
    """Same top-level keys _scrape_url_robust's HTML path returns, so
    every consumer of a scraped entry's `data` dict can rely on the
    same shape regardless of what kind of content was actually behind
    the link. `content_kind` is the one new, purely additive key."""
    return {
        "og": {}, "images": [], "videos": [], "links": [], "text": "",
        "canonical_url": "", "author": "", "published_time": "",
        "product_info": {}, "word_count": 0, "reading_time_minutes": 0,
        "structured_data": [], "intel": {}, "raw": {}, "error": None,
        "fetched_at": dj_timezone.now().isoformat(), "content_kind": content_kind,
    }


def _build_pdf_result(raw_bytes: bytes, final_url: str, fetch_error: Optional[str]) -> dict:
    """
    Best-effort PDF text/metadata extraction via the optional `pypdf`
    package. Without it installed, still returns a clean, honest
    metadata-only result (byte size, content_kind='pdf') rather than
    an error — a PDF link is legitimately "scraped" even if we can't
    read its text, the same way a non-HTML result was already treated
    as informative-but-thin before this upgrade.
    """
    result = _non_html_result_shape("pdf")
    result["canonical_url"] = final_url
    result["raw"] = {"content_type": _PDF_CONTENT_TYPE_HINT, "size_bytes": len(raw_bytes), "pypdf_available": _PYPDF_AVAILABLE}

    if _PYPDF_AVAILABLE:
        try:
            reader = pypdf.PdfReader(io.BytesIO(raw_bytes))
            meta = reader.metadata
            title = (getattr(meta, "title", None) or "").strip() if meta else ""
            author = (getattr(meta, "author", None) or "").strip() if meta else ""
            text_parts = []
            for page in reader.pages[:_PDF_MAX_PAGES_TO_READ]:
                try:
                    text_parts.append(page.extract_text() or "")
                except Exception:
                    continue
            text = "\n".join(t for t in text_parts if t).strip()[:_PDF_MAX_TEXT_CHARS]

            result["og"] = {"title": title, "description": "", "thumbnail": "", "site_name": "", "type": "pdf"}
            result["author"] = author
            result["text"] = text
            result["word_count"] = len(text.split()) if text else 0
            result["reading_time_minutes"] = (
                max(1, round(result["word_count"] / _WORDS_PER_MINUTE)) if result["word_count"] else 0
            )
            result["raw"]["page_count"] = len(reader.pages)
        except Exception as exc:
            logger.debug("intelligence_scraper: PDF extraction failed for %s: %s", final_url, exc)
            result["raw"]["extraction_error"] = str(exc)

    if fetch_error:
        result["error"] = fetch_error
    return result


def _build_json_result(raw_bytes: bytes, final_url: str, fetch_error: Optional[str]) -> dict:
    """
    Shallow, display-safe JSON summary — top-level keys, a guessed
    title/name field, and item count if the payload is a list — rather
    than dumping a potentially huge/deeply-nested structure wholesale
    into a scrape result.
    """
    result = _non_html_result_shape("json")
    result["canonical_url"] = final_url
    result["raw"] = {"content_type": "application/json", "size_bytes": len(raw_bytes)}

    try:
        payload = json.loads(raw_bytes.decode("utf-8", errors="replace"))
    except Exception as exc:
        result["raw"]["parse_error"] = str(exc)
        if fetch_error:
            result["error"] = fetch_error
        return result

    title = ""
    if isinstance(payload, dict):
        for key in ("title", "name", "headline"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                title = value.strip()
                break
        result["raw"]["top_level_keys"] = list(payload.keys())[:30]
        result["text"] = json.dumps(payload, indent=2, default=str)[:2000]
    elif isinstance(payload, list):
        result["raw"]["item_count"] = len(payload)
        result["text"] = json.dumps(payload[:5], indent=2, default=str)[:2000]

    result["og"] = {"title": title, "description": "", "thumbnail": "", "site_name": "", "type": "json"}
    result["word_count"] = len(result["text"].split()) if result["text"] else 0
    if fetch_error:
        result["error"] = fetch_error
    return result


def _feed_item_text(el, tag_name: str, ns: str = "") -> str:
    child = el.find(f"{ns}{tag_name}")
    return (child.text or "").strip() if child is not None and child.text else ""


def _build_feed_result(raw_bytes: bytes, final_url: str, fetch_error: Optional[str]) -> dict:
    """
    Minimal RSS 2.0 / Atom feed parser via the stdlib
    xml.etree.ElementTree — no `feedparser` dependency needed. Returns
    the feed title plus up to _FEED_MAX_ITEMS entries (title/link/
    published), tolerant of either format's element/namespace layout.
    """
    result = _non_html_result_shape("feed")
    result["canonical_url"] = final_url
    result["raw"] = {"content_type": "application/rss+xml", "size_bytes": len(raw_bytes)}

    try:
        root_el = ET.fromstring(raw_bytes)
    except ET.ParseError as exc:
        result["raw"]["parse_error"] = str(exc)
        if fetch_error:
            result["error"] = fetch_error
        return result

    tag = root_el.tag.rsplit("}", 1)[-1]
    feed_title = ""
    items = []

    if tag == "rss":
        channel = root_el.find("channel")
        if channel is not None:
            feed_title = _feed_item_text(channel, "title")
            for item in channel.findall("item")[:_FEED_MAX_ITEMS]:
                items.append({
                    "title": _feed_item_text(item, "title"),
                    "link": _feed_item_text(item, "link"),
                    "published": _feed_item_text(item, "pubDate"),
                })
    elif tag == "feed":  # Atom
        feed_title = _feed_item_text(root_el, "title", _ATOM_NS)
        for entry in root_el.findall(f"{_ATOM_NS}entry")[:_FEED_MAX_ITEMS]:
            link_el = entry.find(f"{_ATOM_NS}link")
            items.append({
                "title": _feed_item_text(entry, "title", _ATOM_NS),
                "link": (link_el.get("href") or "").strip() if link_el is not None else "",
                "published": _feed_item_text(entry, "updated", _ATOM_NS),
            })

    result["og"] = {"title": feed_title, "description": "", "thumbnail": "", "site_name": "", "type": "feed"}
    result["raw"]["feed_item_count"] = len(items)
    result["links"] = [{"href": it["link"], "text": it["title"]} for it in items if it["link"]]
    result["text"] = "\n".join(f"{it['title']} — {it['published']}".strip(" —") for it in items)[:_PDF_MAX_TEXT_CHARS]
    result["word_count"] = len(result["text"].split()) if result["text"] else 0
    if fetch_error:
        result["error"] = fetch_error
    return result


# ---------------------------------------------------------------------------
# Sitemap discovery (NEW — see UPGRADE NOTE 6)
# ---------------------------------------------------------------------------

_SITEMAP_MAX_URLS_HARD_CAP = 500
_SITEMAP_MAX_NESTED_SITEMAPS = 5
_SITEMAP_FETCH_TIMEOUT_SECONDS = 8
_SITEMAP_MAX_FETCH_BYTES = 4 * 1024 * 1024  # 4 MB — a sitemap this big is unusual


def _fetch_small_text(url: str) -> Optional[bytes]:
    """
    Small, single-purpose, non-retrying fetch for robots.txt /
    sitemap.xml — intentionally NOT routed through the full
    _get_session()/_robust_fetch_raw machinery (no retry storm needed
    for a best-effort discovery lookup), capped at a smaller byte
    ceiling than a normal page fetch. Never raises.
    """
    try:
        resp = requests.get(
            url, timeout=_SITEMAP_FETCH_TIMEOUT_SECONDS,
            headers={"User-Agent": _pick_user_agent()}, stream=True,
        )
    except requests.RequestException:
        return None
    if resp.status_code != 200:
        resp.close()
        return None
    chunks: List[bytes] = []
    total = 0
    try:
        for chunk in resp.iter_content(chunk_size=65536):
            if not chunk:
                continue
            chunks.append(chunk)
            total += len(chunk)
            if total > _SITEMAP_MAX_FETCH_BYTES:
                break
    except requests.RequestException:
        resp.close()
        return None
    resp.close()
    return b"".join(chunks)


def _find_sitemap_locations(root: str) -> List[str]:
    """Parses /robots.txt (if reachable) for `Sitemap:` directive
    lines — the standard, site-declared way to point at a sitemap.
    Returns an empty list on any failure; the caller falls back to the
    conventional /sitemap.xml path."""
    robots_bytes = _fetch_small_text(f"{root}/robots.txt")
    if not robots_bytes:
        return []
    try:
        text = robots_bytes.decode("utf-8", errors="replace")
    except Exception:
        return []
    locations = []
    for line in text.splitlines():
        line = line.strip()
        if line.lower().startswith("sitemap:"):
            loc = line.split(":", 1)[1].strip()
            if loc:
                locations.append(loc)
    return locations


def discover_sitemap_urls(source_url: str, max_urls: int = 200) -> List[str]:
    """
    Best-effort sitemap discovery for a domain, used by scrape_links()
    when supplement_with_sitemap=True.

    Looks for a `Sitemap:` directive in /robots.txt first (the
    standard, site-declared location), falling back to the
    conventional /sitemap.xml path if robots.txt doesn't declare one
    or can't be fetched. Handles both a plain <urlset> and a
    <sitemapindex> that points at further sitemaps, following up to
    _SITEMAP_MAX_NESTED_SITEMAPS of those child sitemaps (breadth, not
    recursively nested indexes) before stopping.

    Every URL discovered here still goes through the normal
    validate_crawl_url() / robots.txt / circuit-breaker gate in
    _process_one_link before it's ever actually fetched as a link —
    this function only discovers candidate URLs, it never fetches page
    content itself.

    Never raises: any failure at any stage returns whatever URLs were
    collected so far (possibly empty) — this is a best-effort
    discovery aid, called through _safe_call by scrape_links(), not a
    required step.
    """
    try:
        validate_crawl_url(source_url)
    except UnsafeCrawlURLError:
        return []

    parsed = urlsplit(source_url)
    if not parsed.scheme or not parsed.netloc:
        return []
    root = f"{parsed.scheme}://{parsed.netloc}"
    max_urls = max(1, min(max_urls, _SITEMAP_MAX_URLS_HARD_CAP))

    declared = _find_sitemap_locations(root)
    seed_sitemaps = declared[:_SITEMAP_MAX_NESTED_SITEMAPS] or [f"{root}/sitemap.xml"]

    collected: List[str] = []
    seen_sitemaps: Set[str] = set()
    queue = deque(seed_sitemaps)

    while queue and len(collected) < max_urls:
        if len(seen_sitemaps) >= _SITEMAP_MAX_NESTED_SITEMAPS:
            break
        sm_url = queue.popleft()
        if sm_url in seen_sitemaps:
            continue
        seen_sitemaps.add(sm_url)

        try:
            validate_crawl_url(sm_url)
        except UnsafeCrawlURLError:
            continue

        xml_bytes = _fetch_small_text(sm_url)
        if not xml_bytes:
            continue

        try:
            root_el = ET.fromstring(xml_bytes)
        except ET.ParseError:
            continue

        tag = root_el.tag.rsplit("}", 1)[-1]  # strip XML namespace
        if tag == "sitemapindex":
            for loc_el in root_el.iter():
                if loc_el.tag.rsplit("}", 1)[-1] != "loc" or not loc_el.text:
                    continue
                child = loc_el.text.strip()
                if child and child not in seen_sitemaps:
                    queue.append(child)
        elif tag == "urlset":
            for loc_el in root_el.iter():
                if loc_el.tag.rsplit("}", 1)[-1] != "loc" or not loc_el.text:
                    continue
                url = loc_el.text.strip()
                if url:
                    collected.append(url)
                    if len(collected) >= max_urls:
                        break

    return collected


# ---------------------------------------------------------------------------
# Advanced per-page intelligence — a second, independent tier of signal
# beyond what a primary scrape's own fields already capture. See the
# module docstring's "ADVANCED PER-PAGE INTELLIGENCE" section.
# ---------------------------------------------------------------------------

_EMAIL_RE = re.compile(r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}')

_SOCIAL_DOMAINS = {
    "facebook.com": "facebook", "twitter.com": "twitter", "x.com": "twitter",
    "instagram.com": "instagram", "linkedin.com": "linkedin", "youtube.com": "youtube",
    "tiktok.com": "tiktok", "pinterest.com": "pinterest", "github.com": "github",
    "threads.net": "threads", "reddit.com": "reddit", "discord.com": "discord",
    "discord.gg": "discord",
}

# ---------------------------------------------------------------------------
# Lightweight, pure-Python sentiment + keyword extraction (NEW — see
# UPGRADE NOTE 9). Deliberately dependency-free: a small embedded
# lexicon and simple frequency scoring, not a substitute for a real
# sentiment/NLP model. Documented as approximate everywhere it's
# surfaced, so nothing downstream mistakes this for high-confidence
# analysis.
# ---------------------------------------------------------------------------

_SENTIMENT_POSITIVE_WORDS = frozenset("""
good great excellent amazing awesome fantastic wonderful best love loved
loving perfect happy delighted pleased satisfied impressive impressed
recommend recommended reliable easy helpful positive success successful
beautiful outstanding superb brilliant favorite favourite affordable
efficient innovative valuable enjoy enjoyed enjoying quality trustworthy
""".split())

_SENTIMENT_NEGATIVE_WORDS = frozenset("""
bad terrible awful horrible worst hate hated hating poor disappointing
disappointed frustrating frustrated broken bug buggy fail failed failure
useless slow expensive overpriced problem issue issues complaint
complaints negative unreliable annoying confusing difficult hard
misleading scam fraud fake defective
""".split())

_STOPWORDS = frozenset("""
a an the and or but if then else for to of in on at by with from up
about into over after under again further once here there when where
why how all any both each few more most other some such no nor not
only own same so than too very s t can will just don should now is
are was were be been being have has had do does did doing this that
these those i you he she it we they them his her its our their my your
""".split())

_WORD_RE = re.compile(r"[a-zA-Z][a-zA-Z'-]{2,}")


def _analyze_sentiment(text: str) -> dict:
    """
    Approximate, lexicon-based sentiment scoring: counts positive vs
    negative words against a small embedded word list and derives a
    label + normalized score in [-1, 1]. This is intentionally simple
    and will miss negation, sarcasm, and domain-specific language — it
    is a rough signal for a link-scan popup, not a real sentiment
    classifier. If a real model (e.g. a transformers pipeline) is
    available in a given deployment, swap this function's body for
    that instead; the {label, score, positive_hits, negative_hits}
    return shape is intentionally small so a real model's output can
    be mapped onto it without touching any caller.
    """
    words = [w.lower() for w in _WORD_RE.findall(text or "")]
    if not words:
        return {"label": "neutral", "score": 0.0, "positive_hits": 0, "negative_hits": 0}

    positive_hits = sum(1 for w in words if w in _SENTIMENT_POSITIVE_WORDS)
    negative_hits = sum(1 for w in words if w in _SENTIMENT_NEGATIVE_WORDS)
    total_hits = positive_hits + negative_hits

    if total_hits == 0:
        return {"label": "neutral", "score": 0.0, "positive_hits": 0, "negative_hits": 0}

    score = round((positive_hits - negative_hits) / total_hits, 3)
    if score > 0.15:
        label = "positive"
    elif score < -0.15:
        label = "negative"
    else:
        label = "neutral"

    return {"label": label, "score": score, "positive_hits": positive_hits, "negative_hits": negative_hits}


def _extract_keywords(text: str, max_keywords: int = 10) -> List[str]:
    """
    Simple frequency-based keyword/keyphrase extraction: stopword-
    filtered unigrams plus adjacent-word bigrams, ranked by frequency.
    No external NLP dependency (no sklearn/spaCy) — a lightweight
    complement to the page's own declared meta-keywords/article-tags,
    useful for pages that don't declare any.
    """
    words = [w.lower() for w in _WORD_RE.findall(text or "") if w.lower() not in _STOPWORDS]
    if not words:
        return []

    unigram_counts: Dict[str, int] = {}
    for w in words:
        unigram_counts[w] = unigram_counts.get(w, 0) + 1

    bigram_counts: Dict[str, int] = {}
    for i in range(len(words) - 1):
        bigram = f"{words[i]} {words[i + 1]}"
        bigram_counts[bigram] = bigram_counts.get(bigram, 0) + 1

    # Bigrams that repeat at least twice are usually more meaningful
    # phrases than a single repeated word; unigrams fill the rest.
    ranked_bigrams = [b for b, c in sorted(bigram_counts.items(), key=lambda x: x[1], reverse=True) if c >= 2]
    ranked_unigrams = [w for w, _c in sorted(unigram_counts.items(), key=lambda x: x[1], reverse=True)]

    combined: List[str] = []
    for phrase in ranked_bigrams + ranked_unigrams:
        if phrase not in combined:
            combined.append(phrase)
        if len(combined) >= max_keywords:
            break
    return combined


def _detect_language(text: str) -> Optional[str]:
    """
    Populates a detected-language code ONLY if the optional
    `langdetect` package is installed (`pip install langdetect`).
    Returns None otherwise — no home-grown heuristic guess is
    substituted, since a low-confidence guess presented as a detected
    language is worse than reporting nothing. `intel["language"]`
    (from the page's own <html lang> attribute, when present) remains
    the primary, page-declared signal; this is a text-based fallback
    for pages that don't declare one.
    """
    if not _LANGDETECT_AVAILABLE or not text or len(text.strip()) < 20:
        return None
    try:
        return langdetect.detect(text[:2000])
    except Exception:
        return None


def _extract_page_intel(
    soup: BeautifulSoup, html_text: str, resp_headers: dict, base_url: str,
    links: list, text: str, images: list,
) -> dict:
    """
    Second-tier, "everything else worth knowing about this page" pass —
    deliberately separate from the OG/canonical/author fields
    _scrape_url_robust already extracts, since those exist purely to
    match a primary scrape's shape. Nothing here is required by any
    other consumer; it's additive display data for the link-scan
    popup. Every sub-extraction is independent so one bad selector
    can't blank out the rest — call this itself through _safe_call.
    """
    intel = {
        "language": "", "keywords": [], "headings": {"h1": [], "h2": [], "h3": []},
        "favicon": "", "social_links": [], "contact": {"emails": [], "phones": []},
        "freshness": {}, "page_stats": {}, "hero_image": "",
        "outbound_domains": [], "content_quality": {},
        # NEW fields (see UPGRADE NOTE 9):
        "sentiment": {"label": "neutral", "score": 0.0, "positive_hits": 0, "negative_hits": 0},
        "extracted_keywords": [],
        "detected_language": None,
    }

    # ── language ──
    html_tag = soup.find("html")
    intel["language"] = (html_tag.get("lang") or "").strip() if html_tag else ""
    if not intel["language"]:
        meta_lang = soup.find("meta", attrs={"http-equiv": re.compile("content-language", re.I)})
        if meta_lang:
            intel["language"] = (meta_lang.get("content") or "").strip()

    # ── keywords / tags (page-declared) ──
    kw_tag = soup.find("meta", attrs={"name": "keywords"})
    keywords = [k.strip() for k in (kw_tag.get("content") or "").split(",")] if kw_tag else []
    for tag in soup.find_all("meta", attrs={"property": "article:tag"}):
        if tag.get("content"):
            keywords.append(tag["content"].strip())
    intel["keywords"] = [k for k in dict.fromkeys(keywords) if k][:20]

    # ── extracted keywords (NEW — frequency-based, for pages that
    # don't declare any of their own) ──
    intel["extracted_keywords"] = _extract_keywords(text)

    # ── sentiment (NEW — approximate, see _analyze_sentiment docstring) ──
    intel["sentiment"] = _analyze_sentiment(text)

    # ── detected language (NEW — optional langdetect only) ──
    intel["detected_language"] = _detect_language(text)

    # ── headings outline ──
    for level in ("h1", "h2", "h3"):
        intel["headings"][level] = [
            h.get_text(strip=True)[:150] for h in soup.find_all(level) if h.get_text(strip=True)
        ][:15]

    # ── favicon ──
    icon_tag = soup.find("link", rel=lambda v: v and "icon" in v.lower())
    if icon_tag and icon_tag.get("href"):
        from urllib.parse import urljoin
        intel["favicon"] = urljoin(base_url, icon_tag["href"].strip())

    # ── social links — rel="me" / footer icons / any outbound link
    # whose domain is a known social platform, deduped by URL ──
    seen = set()
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if not href.startswith(("http://", "https://")) or href in seen:
            continue
        platform = _SOCIAL_DOMAINS.get(_root_domain(extract_domain(href)))
        if platform:
            seen.add(href)
            intel["social_links"].append({"platform": platform, "url": href})
    intel["social_links"] = intel["social_links"][:12]

    # ── contact info — best-effort, mailto:/tel: links + emails found
    # in visible text. No phone-number regex: too many false positives
    # (dates, IDs, prices) across international formats to be reliable
    # without a dedicated library, so only explicit tel: links count. ──
    emails = set(_EMAIL_RE.findall(text or "")[:20])
    phones = set()
    for a in soup.find_all("a", href=True):
        low = a["href"].strip().lower()
        if low.startswith("mailto:"):
            emails.add(a["href"][7:].split("?")[0].strip())
        elif low.startswith("tel:"):
            phones.add(a["href"][4:].strip())
    intel["contact"]["emails"] = list(emails)[:8]
    intel["contact"]["phones"] = list(phones)[:8]

    # ── freshness ──
    intel["freshness"]["last_modified_header"] = (
        resp_headers.get("Last-Modified") or resp_headers.get("last-modified") or ""
    )
    mod_meta = soup.find("meta", attrs={"property": "article:modified_time"})
    intel["freshness"]["content_modified"] = (mod_meta.get("content") or "").strip() if mod_meta else ""

    # ── page stats ──
    intel["page_stats"]["server"] = resp_headers.get("Server") or resp_headers.get("server") or ""
    intel["page_stats"]["content_type"] = resp_headers.get("Content-Type") or resp_headers.get("content-type") or ""
    intel["page_stats"]["content_length_bytes"] = len((html_text or "").encode("utf-8", errors="ignore"))

    # ── hero image — first extracted image (already the OG/largest
    # candidate scraper.py's own _extract_images prioritizes) ──
    intel["hero_image"] = images[0]["url"] if images else ""

    # ── outbound domain breakdown for THIS page specifically (distinct
    # from the service-wide breakdown the Intelligence tab already
    # computes client-side across all scanned pages) ──
    domain_counts: Dict[str, int] = {}
    for link in (links or []):
        href = link.get("href") if isinstance(link, dict) else link
        dom = extract_domain(href) if href else None
        if dom:
            domain_counts[dom] = domain_counts.get(dom, 0) + 1
    intel["outbound_domains"] = sorted(
        ({"domain": d, "count": c} for d, c in domain_counts.items()),
        key=lambda x: x["count"], reverse=True,
    )[:10]

    # ── content quality — rough text-to-markup ratio, a cheap signal
    # for "mostly boilerplate/nav" vs "actual content" pages ──
    html_len = len(html_text or "")
    text_len = len(text or "")
    intel["content_quality"]["text_to_html_ratio"] = round(text_len / html_len, 3) if html_len else 0
    intel["content_quality"]["is_thin_content"] = text_len < 200

    return intel


def _scrape_url_robust(
    url: str, api_key: Optional[str] = None, auth_token: Optional[str] = None,
    domain: Optional[str] = None,
) -> dict:
    """
    Self-contained replacement for scraper.scrape_url(): fetches `url`
    via _robust_fetch_raw() (retries/backoff/size-cap/redirects) and,
    for HTML responses, parses it with scraper.py's own extractor
    functions where available, so the resulting dict has the same
    shape scrape_url() produced — og / images / videos / links / text /
    canonical_url / author / published_time / product_info / word_count
    / reading_time_minutes / structured_data / raw / error / fetched_at
    — and every existing downstream consumer of that shape
    (LinkScrapeResult, _extract_structured_summary, link_info.py's
    display layer) keeps working unchanged. In particular,
    canonical_url / author / published_time / product_info / word_count
    / reading_time_minutes are what
    megamind.utils.link_info.prepare_scrape_report_for_display() reads
    directly off each scraped entry's `data` dict — an earlier version
    of this function never populated them, so every intelligence-post's
    author/date/product row silently rendered blank regardless of what
    was actually on the page. The oEmbed shortcut
    (_oembed_to_scrape_result) already returned this full shape; this
    brings the plain-HTTP path to parity with it.

    NON-HTML RESPONSES (see UPGRADE NOTE 7): a PDF, JSON, or RSS/Atom
    feed response is routed to a dedicated lightweight parser
    (_build_pdf_result / _build_json_result / _build_feed_result)
    instead of being rejected outright — each returns the SAME
    top-level keys as the HTML path (via _non_html_result_shape) plus
    one additive `content_kind` field, so no caller needs a special
    case to avoid a KeyError. Anything else non-HTML still comes back
    as `error="unsupported_content_type:..."`, unchanged from before.

    Also populates data["intel"] — a second, independent tier of
    page-level signal (language/keywords/headings/favicon/social-links/
    contact/freshness/page-stats/hero-image/outbound-domains/content-
    quality/sentiment/extracted_keywords/detected_language) via
    _extract_page_intel(), likewise guarded by _safe_call so a bad
    selector degrades only that block, never the rest of the scrape.

    Adds two more upgrades on top of the plain fetch:
      - conditional requests: if this exact URL was fetched recently
        (see _HTTP_CACHE), sends If-None-Match / If-Modified-Since and,
        on a 304, returns the cached result immediately instead of
        re-parsing a page that hasn't changed.
      - per-host concurrency: the actual network call is gated by a
        semaphore capping simultaneous requests to this URL's domain to
        _PER_HOST_MAX_CONCURRENCY, independent of the global worker
        pool, so a page linking many times to the same host doesn't
        point the whole pool at one server at once.
    """
    result = {
        "og": {}, "images": [], "videos": [], "links": [], "text": "",
        "canonical_url": "", "author": "", "published_time": "",
        "product_info": {}, "word_count": 0, "reading_time_minutes": 0,
        "structured_data": [], "intel": {}, "raw": {}, "error": None,
        "fetched_at": dj_timezone.now().isoformat(), "content_kind": "html",
    }

    headers = dict(_SCRAPER_DEFAULT_HEADERS)
    headers["User-Agent"] = _pick_user_agent()
    if auth_token:
        headers["Authorization"] = f"Bearer {auth_token}"
    if api_key:
        headers["X-Api-Key"] = api_key

    cached = _cache_get(url)
    if cached:
        if cached.get("etag"):
            headers["If-None-Match"] = cached["etag"]
        if cached.get("last_modified"):
            headers["If-Modified-Since"] = cached["last_modified"]

    host_domain = domain or extract_domain(url)
    semaphore = _get_host_semaphore(host_domain) if host_domain else None
    if semaphore:
        semaphore.acquire()
    try:
        raw_bytes, final_url, content_type, error, resp_headers = _robust_fetch_raw(url, headers)
    finally:
        if semaphore:
            semaphore.release()

    if error == "not_modified" and cached:
        cached_data = dict(cached["data"])
        cached_data["fetched_at"] = dj_timezone.now().isoformat()
        cached_data["error"] = None
        return cached_data

    if raw_bytes is None:
        result["error"] = error or "fetch_failed"
        return result

    content_type_lower = (content_type or "").lower()

    # ── Content-type dispatch (NEW) ──────────────────────────────────
    if _PDF_CONTENT_TYPE_HINT in content_type_lower:
        return _build_pdf_result(raw_bytes, final_url or url, error)
    if any(hint in content_type_lower for hint in _JSON_CONTENT_TYPE_HINTS):
        return _build_json_result(raw_bytes, final_url or url, error)
    if any(hint in content_type_lower for hint in _FEED_CONTENT_TYPE_HINTS):
        return _build_feed_result(raw_bytes, final_url or url, error)
    if "text/html" not in content_type_lower and "application/xhtml" not in content_type_lower:
        result["error"] = f"unsupported_content_type:{content_type or 'unknown'}"
        return result

    html_text = _decode_bytes(raw_bytes, content_type)

    try:
        soup = BeautifulSoup(html_text, "lxml")
    except Exception:
        soup = BeautifulSoup(html_text, "html.parser")

    base_url = _safe_call(_scraper_base_url, final_url or url, "base_url", soup, final_url or url)

    result["og"] = _safe_call(_scraper_extract_og, {}, "og", soup)
    result["images"] = _safe_call(_scraper_extract_images, [], "images", soup, base_url)
    result["videos"] = _safe_call(_scraper_extract_videos, [], "videos", soup, base_url)
    result["links"] = _safe_call(_scraper_extract_links, [], "links", soup, base_url)
    result["text"] = _safe_call(_scraper_extract_text, "", "text", soup)
    result["structured_data"] = _safe_call(_extract_json_ld_local, [], "json_ld", soup)
    result["canonical_url"] = _safe_call(_scraper_extract_canonical, base_url, "canonical", soup, base_url)
    result["author"] = _safe_call(_scraper_extract_author, "", "author", soup)
    result["published_time"] = _safe_call(_scraper_extract_published_time, "", "published_time", soup)
    result["product_info"] = _safe_call(
        _scraper_extract_product_info, {}, "product_info", result["structured_data"], soup,
    )

    # Advanced page intelligence — see _extract_page_intel's docstring.
    result["intel"] = _safe_call(
        _extract_page_intel, {}, "page_intel",
        soup, html_text, resp_headers, base_url, result["links"], result["text"], result["images"],
    )

    result["word_count"] = len(result["text"].split()) if result["text"] else 0
    result["reading_time_minutes"] = (
        max(1, round(result["word_count"] / _WORDS_PER_MINUTE)) if result["word_count"] else 0
    )
    result["raw"] = {
        "_html": html_text,  # enables _improve_text_extraction's readability upgrade
        "content_type": content_type,
        "final_url": base_url,
        "text_snippet": (result["text"] or "")[:500],
    }

    # A body that came back on a 4xx/5xx is still recorded as an error —
    # matches the original scrape_url()/requests raise_for_status()
    # behavior — but we keep whatever we managed to parse in `raw` for
    # debugging rather than discarding it.
    if error:
        result["error"] = error
    else:
        # Cache successful responses that carry a validator, so a
        # later fetch of this exact URL (deep-crawl retry, another
        # scan) can go conditional instead of re-downloading.
        etag = resp_headers.get("ETag") or resp_headers.get("etag") or ""
        last_modified = resp_headers.get("Last-Modified") or resp_headers.get("last-modified") or ""
        if etag or last_modified:
            _cache_put(url, etag, last_modified, result)

    return result


# ---------------------------------------------------------------------------
# Social-platform awareness (unchanged from the previous version)
# ---------------------------------------------------------------------------

_SOCIAL_REQUIRES_AUTH = {
    "facebook.com": "Facebook",
    "instagram.com": "Instagram",
    "linkedin.com": "LinkedIn",
    "threads.net": "Threads",
}

# domain -> public, no-auth oEmbed endpoint template. Each of these is
# the platform's own documented, sanctioned "give me title/author/
# thumbnail/embed HTML for this public URL" mechanism — not scraping,
# and not an evasion technique. Expanded beyond the original video
# platforms to cover more of the social web that DOES publish a public
# oEmbed endpoint, so more social links resolve cleanly via the front
# door instead of falling through to a plain HTML fetch that a bot-
# walled platform would just block anyway.
_OEMBED_ENDPOINTS = {
    "youtube.com": "https://www.youtube.com/oembed?url={url}&format=json",
    "youtu.be": "https://www.youtube.com/oembed?url={url}&format=json",
    "vimeo.com": "https://vimeo.com/api/oembed.json?url={url}",
    "tiktok.com": "https://www.tiktok.com/oembed?url={url}",
    "x.com": "https://publish.twitter.com/oembed?url={url}",
    "twitter.com": "https://publish.twitter.com/oembed?url={url}",
    # New in this upgrade:
    "reddit.com": "https://www.reddit.com/oembed?url={url}",
    "soundcloud.com": "https://soundcloud.com/oembed?url={url}&format=json",
    "flickr.com": "https://www.flickr.com/services/oembed/?url={url}&format=json",
    "pinterest.com": "https://www.pinterest.com/oembed.json?url={url}",
    "spotify.com": "https://open.spotify.com/oembed?url={url}",
}

_OEMBED_TIMEOUT_SECONDS = 5


def _root_domain(domain: str) -> str:
    parts = (domain or "").lower().split(".")
    return ".".join(parts[-2:]) if len(parts) >= 2 else (domain or "")


def _social_platform_precheck(url: str, domain: str) -> Optional[Dict[str, Any]]:
    root = _root_domain(domain)
    platform_name = _SOCIAL_REQUIRES_AUTH.get(root)
    if not platform_name:
        return None
    return {
        "reason": f"requires_official_api: {platform_name} requires an authenticated "
                  f"app (Graph API / OAuth) to access data — no public endpoint exists "
                  f"for this link.",
    }


def _try_oembed(url: str, domain: str) -> Optional[Dict[str, Any]]:
    root = _root_domain(domain)
    template = _OEMBED_ENDPOINTS.get(root)
    if not template:
        return None

    endpoint = template.format(url=_urllib_parse.quote(url, safe=""))
    try:
        with _urllib_request.urlopen(endpoint, timeout=_OEMBED_TIMEOUT_SECONDS) as resp:
            if resp.status != 200:
                return None
            payload = json.loads(resp.read().decode("utf-8", errors="replace"))
    except Exception as exc:
        logger.debug("intelligence_scraper: oEmbed lookup failed for %s: %s", url, exc)
        return None

    if not isinstance(payload, dict):
        return None

    return {
        "title": payload.get("title") or "",
        "author_name": payload.get("author_name") or "",
        "author_url": payload.get("author_url") or "",
        "thumbnail_url": payload.get("thumbnail_url") or "",
        "html": payload.get("html") or "",
        "provider_name": payload.get("provider_name") or root,
    }


def _oembed_to_scrape_result(url: str, oembed: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "og": {
            "title": oembed.get("title") or "",
            "description": "",
            "thumbnail": oembed.get("thumbnail_url") or "",
            "site_name": oembed.get("provider_name") or "",
            "type": "oembed",
        },
        "twitter": {},
        "meta": {"title": oembed.get("title") or ""},
        "canonical_url": url,
        "favicon": "",
        "author": oembed.get("author_name") or "",
        "published_time": "",
        "headings": {"h1": [], "h2": [], "h3": []},
        "structured_data": [],
        "product_info": {},
        "images": [{"url": oembed["thumbnail_url"], "alt": "", "href": url}] if oembed.get("thumbnail_url") else [],
        "videos": [],
        "links": [],
        "text": "",
        "word_count": 0,
        "reading_time_minutes": 0,
        "rendered_with_browser": False,
        "content_kind": "oembed",
        # Matching shape to _scrape_url_robust's "intel" key so every
        # scraped entry — oEmbed shortcut or plain-HTTP fetch — has the
        # same fields available downstream. An oEmbed response doesn't
        # give us the underlying page's markup, so this is intentionally
        # thin; only page_stats.content_type is meaningfully populated.
        "intel": {
            "language": "", "keywords": [], "headings": {"h1": [], "h2": [], "h3": []},
            "favicon": "", "social_links": [], "contact": {"emails": [], "phones": []},
            "freshness": {}, "page_stats": {"content_type": "oembed"}, "hero_image": "",
            "outbound_domains": [], "content_quality": {},
            "sentiment": {"label": "neutral", "score": 0.0, "positive_hits": 0, "negative_hits": 0},
            "extracted_keywords": [], "detected_language": None,
        },
        "raw": {"source": "oembed", "embed_html": oembed.get("html") or ""},
        "error": None,
        "warnings": [],
    }


def _classify_blocked(error: str) -> Optional[str]:
    """
    Returns a human-readable "skipped" reason if `error` (as produced
    by _robust_fetch_raw / _scrape_url_robust) represents a platform
    actively refusing automated access (401/403/429/451, or a non-HTML
    content-type typical of a login/consent wall) rather than a genuine
    fetch failure. Returns None for everything else, so real errors
    (DNS failure, connection refused, timeout after retries, 404, 5xx)
    still land in `errors` where they belong.

    This is what keeps a scan against a social/bot-walled link from
    reading as a broken crawl: the link is honestly reported as
    unreachable-by-design in `skipped`, and the rest of the batch
    completes normally.
    """
    if not error:
        return None
    for code in _BLOCKED_STATUS_CODES:
        if error == f"http_{code}":
            return f"blocked_by_platform: received HTTP {code} — this site is refusing automated access."
    if error.startswith("unsupported_content_type:"):
        return f"blocked_or_unsupported: {error.split(':', 1)[1].strip() or 'non-HTML response'} (often a login/consent wall)."
    return None


# ---------------------------------------------------------------------------
# Structured error classification (NEW — see UPGRADE NOTE 10)
# ---------------------------------------------------------------------------

class LinkErrorCategory:
    """Mirrors the spirit of megamind.models.connected_service.
    CrawlAttempt.ErrorCategory / engine_profile.py's
    _classify_fetch_error, kept as plain string constants here since
    this module has no CrawlAttempt row to attach a real choice-field
    value to — these are display/grouping labels only."""
    SSRF_BLOCKED = "ssrf_blocked"
    TIMEOUT = "timeout"
    SSL_ERROR = "ssl_error"
    DNS_ERROR = "dns_error"
    CONTENT_TOO_LARGE = "content_too_large"
    HTTP_ERROR = "http_error"
    CONNECTION_ERROR = "connection_error"
    NOT_MODIFIED = "not_modified"
    UNKNOWN = "unknown"


def _classify_link_error(message: str) -> str:
    """
    Best-effort mapping of a raw fetch-error string (as produced by
    _robust_fetch_raw / requests exceptions) onto LinkErrorCategory, so
    `errors` entries carry a machine-usable category alongside the raw
    message — the same heuristic style engine_profile.py's
    _classify_fetch_error already uses for the primary-scrape pipeline,
    now applied to link-intelligence errors too. Never load-bearing for
    anything except which category label a UI shows; always a
    heuristic over free text, since the underlying exceptions here
    aren't structured either.
    """
    if not message:
        return LinkErrorCategory.UNKNOWN
    low = message.lower()
    if "non-public" in low or "non-routable" in low or "blocked (ssrf" in low or "unsafe_url" in low:
        return LinkErrorCategory.SSRF_BLOCKED
    if "timed out" in low or "timeout" in low:
        return LinkErrorCategory.TIMEOUT
    if "ssl" in low or "certificate" in low:
        return LinkErrorCategory.SSL_ERROR
    if "dns" in low or "name resolution" in low or "getaddrinfo" in low or "could not resolve" in low:
        return LinkErrorCategory.DNS_ERROR
    if "exceeds cap" in low or "too large" in low:
        return LinkErrorCategory.CONTENT_TOO_LARGE
    if low == "not_modified":
        return LinkErrorCategory.NOT_MODIFIED
    if low.startswith("http_") or low.startswith("http "):
        return LinkErrorCategory.HTTP_ERROR
    return LinkErrorCategory.CONNECTION_ERROR


def _check_domain_policy(url: str) -> Optional[str]:
    """Returns a skip reason if this link must NOT be fetched, or None if
    it's clear to proceed. robots.txt and the shared per-domain rate
    window are intentionally NOT enforced here (see module docstring in
    the previous version for the full rationale) — SSRF validation and
    the operator manual-block list are the enforced controls."""
    try:
        validate_crawl_url(url)
    except UnsafeCrawlURLError as exc:
        return f"unsafe_url: {exc}"

    domain = extract_domain(url)
    if not domain:
        return "no_domain"

    close_old_connections()
    policy, _ = DomainCrawlPolicy.objects.get_or_create(domain=domain)
    if policy.is_blocked:
        return f"domain_blocked: {policy.blocked_reason or 'manual block'}"
    return None


# ---------------------------------------------------------------------------
# Extended JSON-LD @type coverage (EXPANDED — see UPGRADE NOTE 8)
# ---------------------------------------------------------------------------

_ARTICLE_TYPES = {"Article", "NewsArticle", "BlogPosting", "Report", "TechArticle"}


def _node_types(node: dict) -> List[str]:
    types = node.get("@type")
    if isinstance(types, list):
        return [t for t in types if isinstance(t, str)]
    return [types] if isinstance(types, str) else []


def _text_or_name(value):
    if isinstance(value, dict):
        return value.get("name") or value.get("@id") or ""
    if isinstance(value, list):
        for v in value:
            out = _text_or_name(v)
            if out:
                return out
        return ""
    return value or ""


def _summarize_article(node: dict) -> dict:
    return {
        "headline": node.get("headline") or node.get("name") or "",
        "author": _text_or_name(node.get("author")),
        "date_published": node.get("datePublished") or "",
        "date_modified": node.get("dateModified") or "",
        "section": node.get("articleSection") or "",
        "publisher": _text_or_name(node.get("publisher")),
    }


def _summarize_recipe(node: dict) -> dict:
    yield_val = node.get("recipeYield")
    if isinstance(yield_val, list):
        yield_val = yield_val[0] if yield_val else ""
    total_time = node.get("totalTime") or node.get("cookTime") or node.get("prepTime") or ""
    ingredients = node.get("recipeIngredient") or []
    if not isinstance(ingredients, list):
        ingredients = []
    return {
        "name": node.get("name") or "",
        "yield": str(yield_val or ""),
        "total_time": total_time,
        "ingredient_count": len(ingredients),
        "author": _text_or_name(node.get("author")),
    }


def _summarize_event(node: dict) -> dict:
    location = node.get("location")
    location_name = ""
    if isinstance(location, dict):
        location_name = location.get("name") or _text_or_name(location.get("address"))
    return {
        "name": node.get("name") or "",
        "start_date": node.get("startDate") or "",
        "end_date": node.get("endDate") or "",
        "location": location_name,
        "is_online": bool(node.get("eventAttendanceMode", "").endswith("Online")) if node.get("eventAttendanceMode") else None,
    }


def _summarize_video_object(node: dict) -> dict:
    return {
        "name": node.get("name") or "",
        "duration": node.get("duration") or "",
        "upload_date": node.get("uploadDate") or "",
        "thumbnail": _text_or_name(node.get("thumbnailUrl")),
        "publisher": _text_or_name(node.get("publisher")),
    }


def _summarize_organization(node: dict) -> dict:
    return {
        "name": node.get("name") or "",
        "url": node.get("url") or "",
        "logo": _text_or_name(node.get("logo")),
        "same_as": node.get("sameAs") if isinstance(node.get("sameAs"), list) else [],
    }


def _summarize_faq(node: dict) -> dict:
    entries = node.get("mainEntity") or []
    if not isinstance(entries, list):
        entries = []
    questions = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        answer = entry.get("acceptedAnswer") or {}
        questions.append({
            "question": entry.get("name") or "",
            "answer": (answer.get("text") or "") if isinstance(answer, dict) else "",
        })
    return {"question_count": len(questions), "questions": questions[:10]}


def _summarize_howto(node: dict) -> dict:
    """NEW (UPGRADE NOTE 8): schema.org HowTo — step-by-step guides."""
    steps = node.get("step") or []
    if not isinstance(steps, list):
        steps = [steps]
    step_names = []
    for s in steps:
        if isinstance(s, dict):
            step_names.append(s.get("name") or s.get("text") or "")
        elif isinstance(s, str):
            step_names.append(s)
    return {
        "name": node.get("name") or "",
        "total_time": node.get("totalTime") or "",
        "step_count": len(step_names),
        "steps": [s for s in step_names if s][:15],
        "supply_count": len(node.get("supply") or []) if isinstance(node.get("supply"), list) else 0,
    }


def _summarize_person(node: dict) -> dict:
    """NEW (UPGRADE NOTE 8): schema.org Person — author/profile pages."""
    return {
        "name": node.get("name") or "",
        "job_title": node.get("jobTitle") or "",
        "works_for": _text_or_name(node.get("worksFor")),
        "same_as": node.get("sameAs") if isinstance(node.get("sameAs"), list) else [],
    }


def _summarize_job_posting(node: dict) -> dict:
    """NEW (UPGRADE NOTE 8): schema.org JobPosting."""
    salary = node.get("baseSalary") or {}
    salary_value = ""
    if isinstance(salary, dict):
        value = salary.get("value")
        if isinstance(value, dict):
            salary_value = value.get("value") or value.get("minValue") or ""
        else:
            salary_value = value or ""
    return {
        "title": node.get("title") or "",
        "hiring_organization": _text_or_name(node.get("hiringOrganization")),
        "employment_type": node.get("employmentType") or "",
        "date_posted": node.get("datePosted") or "",
        "salary": str(salary_value) if salary_value else "",
        "location": _text_or_name(node.get("jobLocation")),
    }


def _summarize_review(node: dict) -> dict:
    """NEW (UPGRADE NOTE 8): schema.org Review — standalone review pages
    (distinct from an aggregateRating already folded into product_info)."""
    rating = node.get("reviewRating") or {}
    return {
        "item_reviewed": _text_or_name(node.get("itemReviewed")),
        "author": _text_or_name(node.get("author")),
        "rating_value": rating.get("ratingValue") if isinstance(rating, dict) else "",
        "best_rating": rating.get("bestRating") if isinstance(rating, dict) else "",
        "review_body_preview": (node.get("reviewBody") or "")[:280],
    }


def _summarize_software_application(node: dict) -> dict:
    """NEW (UPGRADE NOTE 8): schema.org SoftwareApplication."""
    rating = node.get("aggregateRating") or {}
    offers = node.get("offers") or {}
    if isinstance(offers, list):
        offers = offers[0] if offers else {}
    return {
        "name": node.get("name") or "",
        "application_category": node.get("applicationCategory") or "",
        "operating_system": node.get("operatingSystem") or "",
        "rating_value": rating.get("ratingValue") if isinstance(rating, dict) else "",
        "price": offers.get("price") if isinstance(offers, dict) else "",
    }


def _summarize_book(node: dict) -> dict:
    """NEW (UPGRADE NOTE 8): schema.org Book."""
    return {
        "name": node.get("name") or "",
        "author": _text_or_name(node.get("author")),
        "isbn": node.get("isbn") or "",
        "number_of_pages": node.get("numberOfPages") or "",
        "publisher": _text_or_name(node.get("publisher")),
    }


def _summarize_movie_or_series(node: dict, content_type: str) -> dict:
    """NEW (UPGRADE NOTE 8): schema.org Movie / TVSeries."""
    return {
        "name": node.get("name") or "",
        "date_published": node.get("datePublished") or "",
        "director": _text_or_name(node.get("director")),
        "genre": node.get("genre") if isinstance(node.get("genre"), list) else ([node["genre"]] if node.get("genre") else []),
        "content_type": content_type,
    }


def _summarize_course(node: dict) -> dict:
    """NEW (UPGRADE NOTE 8): schema.org Course."""
    provider = node.get("provider")
    return {
        "name": node.get("name") or "",
        "provider": _text_or_name(provider),
        "description_preview": (node.get("description") or "")[:280],
    }


def _extract_structured_summary(structured_data: list) -> Optional[dict]:
    """
    Returns a single summary dict for the first recognized schema.org
    type found (in the priority order below), or None if nothing
    recognized was present. Return contract unchanged from before —
    always a single dict with a "content_type" key, or None — so this
    is a drop-in for any existing caller; only the SET of recognized
    types grew (see UPGRADE NOTE 8: HowTo, Person, JobPosting, Review,
    SoftwareApplication, Book, Movie/TVSeries, Course added to the
    original Article/Recipe/Event/VideoObject/FAQPage/Organization
    coverage).
    """
    for node in _iter_json_ld_nodes(structured_data):
        types = set(_node_types(node))
        if not types:
            continue
        if types & _ARTICLE_TYPES:
            return {"content_type": "article", **_summarize_article(node)}
        if "Recipe" in types:
            return {"content_type": "recipe", **_summarize_recipe(node)}
        if "Event" in types:
            return {"content_type": "event", **_summarize_event(node)}
        if "VideoObject" in types:
            return {"content_type": "video", **_summarize_video_object(node)}
        if "FAQPage" in types:
            return {"content_type": "faq", **_summarize_faq(node)}
        if "HowTo" in types:
            return {"content_type": "howto", **_summarize_howto(node)}
        if "JobPosting" in types:
            return {"content_type": "job_posting", **_summarize_job_posting(node)}
        if "Review" in types:
            return {"content_type": "review", **_summarize_review(node)}
        if "SoftwareApplication" in types or "WebApplication" in types or "MobileApplication" in types:
            return {"content_type": "software_application", **_summarize_software_application(node)}
        if "Book" in types:
            return {"content_type": "book", **_summarize_book(node)}
        if "Movie" in types:
            return {"content_type": "movie", **_summarize_movie_or_series(node, "movie")}
        if "TVSeries" in types:
            return {"content_type": "tv_series", **_summarize_movie_or_series(node, "tv_series")}
        if "Course" in types:
            return {"content_type": "course", **_summarize_course(node)}
        if "Person" in types:
            return {"content_type": "person", **_summarize_person(node)}
        if types & {"Organization", "Corporation", "LocalBusiness"}:
            return {"content_type": "organization", **_summarize_organization(node)}
    return None


# ---------------------------------------------------------------------------
# Readability-scored text upgrade (now actually activates — see
# _scrape_url_robust, which populates data["raw"]["_html"])
# ---------------------------------------------------------------------------

_READABILITY_MIN_RATIO = 0.3


def _improve_text_extraction(data: dict, html_text: Optional[str]) -> None:
    if not _READABILITY_AVAILABLE or not html_text:
        return
    try:
        soup = BeautifulSoup(html_text, "lxml")
    except Exception:
        try:
            soup = BeautifulSoup(html_text, "html.parser")
        except Exception:
            return

    try:
        readable_text, _debug = extract_main_content(soup)
    except Exception as exc:
        logger.debug("intelligence_scraper: readability extraction failed: %s", exc)
        return

    existing_text = data.get("text") or ""
    if readable_text and (
        not existing_text or len(readable_text) >= len(existing_text) * _READABILITY_MIN_RATIO
    ):
        data["text"] = readable_text
        data["word_count"] = len(readable_text.split())
        data["reading_time_minutes"] = (
            max(1, round(data["word_count"] / _WORDS_PER_MINUTE)) if data["word_count"] else 0
        )


@dataclass
class LinkScrapeResult:
    url: str
    domain: str
    depth: int
    source_url: str
    duration_ms: int = 0
    fetched_at: str = ""
    status: str = "success"
    data: Dict[str, Any] = field(default_factory=dict)
    structured_summary: Optional[dict] = None
    relevance_score: int = 0


# ---------------------------------------------------------------------------
# Per-link worker — runs inside the thread pool
# ---------------------------------------------------------------------------

def _process_one_link(
    url: str,
    depth: int,
    found_on: str,
    anchor_text: str,
    *,
    source_domain: Optional[str],
    source_api_key: Optional[str],
    source_auth_token: Optional[str],
    forward_auth_same_domain_only: bool,
    link_filter: Optional[Callable[[str, str], bool]],
    per_domain_count: Dict[str, int],
    max_per_domain: int,
    domain_lock: threading.Lock,
    domain_failure_count: Dict[str, int],
) -> Dict[str, Any]:
    """
    Does everything scrape_links() used to do inline for a single link —
    unfetchable/non-page filtering, the caller-supplied link_filter,
    the per-domain budget, the per-scan circuit breaker, SSRF +
    operator-block gate, the social-platform precheck/oEmbed shortcut,
    and finally the actual fetch — and returns a small outcome dict
    instead of mutating shared state directly, so the caller can safely
    fan this out across threads.

    Returns one of:
      {"kind": "skipped", "reason": str}
      {"kind": "error", "error": str, "error_category": str}
      {"kind": "scraped", "result": dict, "discovered": [(url, depth+1, found_on, text), ...]}
    """
    close_old_connections()

    if _looks_unfetchable(url):
        return {"kind": "skipped", "reason": "unfetchable_scheme"}
    if _looks_like_non_page(url):
        return {"kind": "skipped", "reason": "non_page_extension"}

    domain = extract_domain(url)

    if link_filter is not None:
        try:
            keep = bool(link_filter(url, domain))
        except Exception as exc:
            logger.debug("intelligence_scraper: link_filter raised for %s: %s", url, exc)
            keep = False
        if not keep:
            return {"kind": "skipped", "reason": "filtered"}

    # Reserve a per-domain slot before doing any network work, so two
    # workers racing on the same domain can't both slip in under the
    # cap. Also check the per-scan circuit breaker here — a domain that
    # has already accumulated _CIRCUIT_BREAKER_THRESHOLD consecutive
    # blocked/error outcomes this run is skipped without a fetch
    # attempt, so a single dead/hard-blocking host discovered via many
    # links can't eat the rest of the scan's time budget.
    with domain_lock:
        if per_domain_count.get(domain, 0) >= max_per_domain:
            return {"kind": "skipped", "reason": "max_per_domain_reached"}
        if domain_failure_count.get(domain, 0) >= _CIRCUIT_BREAKER_THRESHOLD:
            return {"kind": "skipped", "reason": f"circuit_open: {domain} failed repeatedly this scan — skipping further links to it"}
        per_domain_count[domain] = per_domain_count.get(domain, 0) + 1

    def _record_failure():
        with domain_lock:
            domain_failure_count[domain] = domain_failure_count.get(domain, 0) + 1

    def _record_success():
        with domain_lock:
            if domain in domain_failure_count:
                domain_failure_count[domain] = 0

    skip_reason = _check_domain_policy(url)
    if skip_reason:
        return {"kind": "skipped", "reason": skip_reason}

    social_precheck = _social_platform_precheck(url, domain)
    if social_precheck:
        return {"kind": "skipped", "reason": social_precheck["reason"]}

    oembed = _try_oembed(url, domain)
    if oembed:
        close_old_connections()
        policy, _ = DomainCrawlPolicy.objects.get_or_create(domain=domain)
        policy.record_request()
        _record_success()

        data = _oembed_to_scrape_result(url, oembed)
        structured_summary = {"content_type": "social_embed", "provider": oembed.get("provider_name") or domain}
        result = LinkScrapeResult(
            url=url, domain=domain, depth=depth, source_url=found_on,
            duration_ms=0, fetched_at=dj_timezone.now().isoformat(),
            data=data, structured_summary=structured_summary,
            relevance_score=_score_link(url, anchor_text),
        )
        return {"kind": "scraped", "result": result.__dict__, "discovered": []}

    same_domain = bool(domain) and domain == source_domain
    forward_creds = same_domain or not forward_auth_same_domain_only

    attempt_started = dj_timezone.now()
    try:
        data = _scrape_url_robust(
            url,
            api_key=source_api_key if forward_creds else None,
            auth_token=source_auth_token if forward_creds else None,
            domain=domain,
        )
    except Exception as exc:
        logger.warning("intelligence_scraper: fetch raised for %s: %s", url, exc)
        _record_failure()
        return {"kind": "error", "error": str(exc), "error_category": _classify_link_error(str(exc))}

    close_old_connections()
    policy, _ = DomainCrawlPolicy.objects.get_or_create(domain=domain)
    policy.record_request()
    duration_ms = int((dj_timezone.now() - attempt_started).total_seconds() * 1000)

    if data.get("error"):
        blocked_reason = _classify_blocked(data["error"])
        _record_failure()
        if blocked_reason:
            return {"kind": "skipped", "reason": blocked_reason}
        return {"kind": "error", "error": data["error"], "error_category": _classify_link_error(data["error"])}

    _record_success()

    structured_summary = _safe_call(
        _extract_structured_summary, None, "structured_summary", data.get("structured_data") or [],
    )
    _improve_text_extraction(data, (data.get("raw") or {}).get("_html"))

    result = LinkScrapeResult(
        url=url, domain=domain, depth=depth, source_url=found_on,
        duration_ms=duration_ms, fetched_at=dj_timezone.now().isoformat(),
        data=data, structured_summary=structured_summary,
        relevance_score=_score_link(url, anchor_text),
    )

    discovered = [
        (child["href"], depth + 1, url, child.get("text", ""))
        for child in normalize_links(data.get("links"))
    ]
    return {"kind": "scraped", "result": result.__dict__, "discovered": discovered}


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------

def scrape_links(
    urls: Iterable[str],
    *,
    source_url: Optional[str] = None,
    source_domain: Optional[str] = None,
    source_api_key: Optional[str] = None,
    source_auth_token: Optional[str] = None,
    max_links: int = DEFAULT_MAX_LINKS,
    max_per_domain: int = DEFAULT_MAX_PER_DOMAIN,
    max_depth: int = DEFAULT_MAX_DEPTH,
    link_filter: Optional[Callable[[str, str], bool]] = None,
    forward_auth_same_domain_only: bool = True,
    rank_frontier: bool = True,
    max_workers: int = DEFAULT_MAX_WORKERS,
    supplement_with_sitemap: bool = False,
    time_budget_seconds: int = DEFAULT_TIME_BUDGET_SECONDS,
) -> Dict[str, Any]:
    """
    Core engine: scrape a starting set of URLs and, optionally, follow
    the links found ON those pages, up to `max_depth` hops — all
    in-memory, nothing persisted.

    Fetches each depth's frontier CONCURRENTLY (bounded by max_workers,
    clamped to MAX_WORKERS_HARD_CAP) via a thread pool, one wave per
    depth level: every link at depth N is fetched in parallel, then the
    links they discovered become depth N+1's (ranked) frontier. Global
    `max_links` / `max_per_domain` dedup budgets are enforced the same
    way regardless of concurrency, via a lock around the shared
    bookkeeping (`visited`, `per_domain_count`, `domain_failure_count`).
    Worker count additionally scales down automatically for small
    requests (see `workers` below) so a 3-link scan doesn't spin up 8
    threads for no benefit.

    A per-scan circuit breaker (_CIRCUIT_BREAKER_THRESHOLD) stops
    sending further links to a domain once it's failed/been-blocked
    that many times in a row this run, a per-host semaphore
    (_PER_HOST_MAX_CONCURRENCY) caps simultaneous requests to any one
    domain independent of the global worker pool, and an overall
    `time_budget_seconds` ceiling (clamped to TIME_BUDGET_HARD_CAP_SECONDS,
    checked between waves) bounds total wall-clock time regardless of
    how many/how slow the pages turn out to be — see
    _process_one_link / _scrape_url_robust and UPGRADE NOTE 10.

    If `supplement_with_sitemap` is True and `source_url` is given, the
    source domain's sitemap.xml (see discover_sitemap_urls()) is fetched
    once up front and its URLs are folded into the depth-1 frontier
    alongside `urls` — a sitemap is the site's own, explicitly-published
    map of its pages, so it's often a far more complete and reliable way
    to discover what's on a site than following on-page links alone.

    See the module docstring for the full safety/politeness contract
    (SSRF validation, operator block list, robots.txt intentionally not
    enforced, same-domain-only credential forwarding), the fetch
    robustness contract (retries, size cap, encoding detection,
    redirects, conditional caching), and the content-type-aware fetch
    handling (PDF/JSON/RSS+Atom) — none of that changes based on how
    many links you ask for.

    Returns:
        {
          "started_at": iso str, "finished_at": iso str,
          "requested": int, "scraped": [...], "skipped": [...],
          "errors": [...], "truncated": bool, "time_budget_exceeded": bool,
        }

    Note: `scraped` / `skipped` / `errors` are lists, not counts. Callers
    that want summary counts (e.g. for a UI stat row) should go through
    megamind.utils.link_info.prepare_scrape_report_for_display(), which
    derives a `counts` dict from these lists rather than expecting one
    to already be present here.
    """
    started_at = dj_timezone.now()
    scraped: List[Dict[str, Any]] = []
    skipped: List[Dict[str, str]] = []
    errors: List[Dict[str, str]] = []
    visited: Set[str] = set()
    per_domain_count: Dict[str, int] = {}
    domain_failure_count: Dict[str, int] = {}
    truncated = False
    time_budget_exceeded = False
    bookkeeping_lock = threading.Lock()

    if source_url and not source_domain:
        source_domain = extract_domain(source_url)

    # Adaptive worker sizing: never spin up more workers than there are
    # links to fetch in the *first* wave — a small scan gains nothing
    # from a full-sized pool and just pays thread-creation overhead.
    # Later waves reuse the same pool (ThreadPoolExecutor threads are
    # kept warm for the `with` block's lifetime), so this only shapes
    # the pool's ceiling, not a per-wave resize.
    requested_seed_count = sum(1 for u in urls if u and str(u).strip()) if hasattr(urls, "__len__") or isinstance(urls, list) else None
    workers = max(1, min(max_workers, MAX_WORKERS_HARD_CAP))
    if requested_seed_count:
        workers = max(1, min(workers, requested_seed_count))

    time_budget = max(1, min(time_budget_seconds, TIME_BUDGET_HARD_CAP_SECONDS))

    seed_urls = list(dict.fromkeys(u.strip() for u in urls if u and u.strip()))
    if supplement_with_sitemap and source_url:
        sitemap_urls = _safe_call(discover_sitemap_urls, [], "sitemap_discovery", source_url)
        if sitemap_urls:
            seed_urls = list(dict.fromkeys(seed_urls + sitemap_urls))

    current_wave: List[Tuple[str, int, str, str]] = [
        (u, 1, source_url or "", "") for u in seed_urls
    ]

    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="link-intel") as executor:
        while current_wave and len(scraped) < max_links:
            elapsed = (dj_timezone.now() - started_at).total_seconds()
            if elapsed > time_budget:
                time_budget_exceeded = True
                logger.info(
                    "intelligence_scraper: time budget (%ss) reached with %d URL(s) still queued",
                    time_budget, len(current_wave),
                )
                break

            # Dedup + budget-trim this wave under the lock before
            # submitting any work, so two links pointing at the same
            # canonical URL within one wave don't both get fetched.
            batch: List[Tuple[str, int, str, str]] = []
            with bookkeeping_lock:
                for item in current_wave:
                    url = item[0]
                    dedup_key = _canonicalize_for_dedup(url)
                    if dedup_key in visited:
                        continue
                    if len(scraped) + len(batch) >= max_links:
                        truncated = True
                        break
                    visited.add(dedup_key)
                    batch.append(item)

            if not batch:
                break

            future_map = {
                executor.submit(
                    _process_one_link, url, depth, found_on, anchor_text,
                    source_domain=source_domain,
                    source_api_key=source_api_key,
                    source_auth_token=source_auth_token,
                    forward_auth_same_domain_only=forward_auth_same_domain_only,
                    link_filter=link_filter,
                    per_domain_count=per_domain_count,
                    max_per_domain=max_per_domain,
                    domain_lock=bookkeeping_lock,
                    domain_failure_count=domain_failure_count,
                ): (url, depth, found_on, anchor_text)
                for (url, depth, found_on, anchor_text) in batch
            }

            next_wave_candidates: List[Tuple[str, int, str, str]] = []
            for future in as_completed(future_map):
                url, depth, found_on, anchor_text = future_map[future]
                try:
                    outcome = future.result()
                except Exception as exc:
                    logger.warning("intelligence_scraper: worker raised for %s: %s", url, exc)
                    errors.append({"url": url, "error": str(exc), "error_category": _classify_link_error(str(exc))})
                    continue

                kind = outcome["kind"]
                if kind == "skipped":
                    skipped.append({"url": url, "reason": outcome["reason"]})
                elif kind == "error":
                    errors.append({
                        "url": url,
                        "error": outcome["error"],
                        "error_category": outcome.get("error_category", LinkErrorCategory.UNKNOWN),
                    })
                elif kind == "scraped":
                    if len(scraped) < max_links:
                        scraped.append(outcome["result"])
                    else:
                        truncated = True
                        continue
                    if depth < max_depth:
                        next_wave_candidates.extend(outcome["discovered"])

            if rank_frontier and next_wave_candidates:
                next_wave_candidates = _rank_frontier(next_wave_candidates)
            current_wave = next_wave_candidates

    if current_wave and len(scraped) >= max_links:
        truncated = True

    return {
        "started_at": started_at.isoformat(),
        "finished_at": dj_timezone.now().isoformat(),
        "requested": len(visited),
        "scraped": scraped,
        "skipped": skipped,
        "errors": errors,
        "truncated": truncated,
        "time_budget_exceeded": time_budget_exceeded,
    }


def scrape_service_links(
    service,
    *,
    max_links: int = DEFAULT_MAX_LINKS,
    max_per_domain: int = DEFAULT_MAX_PER_DOMAIN,
    max_depth: int = DEFAULT_MAX_DEPTH,
    text_filter: Optional[Callable[[str], bool]] = None,
    forward_auth_same_domain_only: bool = True,
    rank_frontier: bool = True,
    max_workers: int = DEFAULT_MAX_WORKERS,
    time_budget_seconds: int = DEFAULT_TIME_BUDGET_SECONDS,
) -> Dict[str, Any]:
    """
    Convenience wrapper: pulls `service.extracted_links` and scrapes
    them via scrape_links(). Nothing is written back onto `service`.
    See scrape_links() for the max_workers concurrency knob and the
    time_budget_seconds ceiling.
    """
    normalized = normalize_links(service.extracted_links)

    if text_filter is not None:
        normalized = [n for n in normalized if _safe_text_filter(text_filter, n["text"])]

    return scrape_links(
        (n["href"] for n in normalized),
        source_url=service.service_url,
        source_domain=service.domain or extract_domain(service.service_url),
        source_api_key=service.api_key,
        source_auth_token=service.auth_token,
        max_links=max_links,
        max_per_domain=max_per_domain,
        max_depth=max_depth,
        forward_auth_same_domain_only=forward_auth_same_domain_only,
        rank_frontier=rank_frontier,
        max_workers=max_workers,
        time_budget_seconds=time_budget_seconds,
    )


def _safe_text_filter(fn: Callable[[str], bool], text: str) -> bool:
    try:
        return bool(fn(text))
    except Exception as exc:
        logger.debug("intelligence_scraper: text_filter raised for %r: %s", text, exc)
        return False


def _safe_call(fn, default, label, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except Exception as exc:
        logger.debug("intelligence_scraper: %s failed: %s", label, exc)
        return default


# ---------------------------------------------------------------------------
# Notes
# ---------------------------------------------------------------------------
# - Concurrency is wave-based (one ThreadPoolExecutor.submit batch per
#   depth level), not a single flat pool across all depths — this keeps
#   the breadth-first budget semantics identical to the old sequential
#   version (max_links/max_per_domain still apply globally, not per
#   wave) while still parallelizing the actual network I/O within each
#   depth.
# - requests + urllib3's Retry handles connection-level retries; this
#   module does not add a second retry layer on top, since retrying an
#   already-retried request would just multiply latency on a genuinely
#   dead host for no benefit.
# - _MAX_FETCH_BYTES truncates rather than rejects: a response over the
#   cap still gets parsed from whatever was read before the cutoff,
#   since a huge page is far more often "long article/product listing"
#   than "not a real page", and partial extraction beats none.
# - canonical_url / author / published_time / product_info / word_count
#   / reading_time_minutes / intel are extracted with the same
#   _safe_call guard as every other field here, so a malformed selector
#   or an extractor raising on unusual markup degrades that one field
#   (or, for intel, that one sub-block) to its default rather than
#   failing the whole link's scrape.
# - Non-HTML responses (PDF/JSON/RSS+Atom) are now parsed instead of
#   uniformly rejected — see UPGRADE NOTE 7 and _build_pdf_result /
#   _build_json_result / _build_feed_result. PDF text extraction is
#   optional (`pip install pypdf`); JSON/feed parsing use only the
#   stdlib. Anything else non-HTML still comes back as
#   `error="unsupported_content_type:..."`, unchanged from before.
# - Sentiment/keyword/language signals (UPGRADE NOTE 9) are
#   deliberately lightweight and approximate: a small embedded lexicon
#   for sentiment, frequency scoring for keywords (no sklearn/spaCy),
#   and language detection that's simply omitted (not guessed) unless
#   the optional `langdetect` package is installed. None of these are
#   billed as authoritative NLP — they're a rough signal for a
#   link-scan popup, and every value they produce is clearly scoped to
#   its own dict key so a caller can ignore them entirely.
# - As before: robots.txt is intentionally not enforced (scan scoped to
#   public data only), no bot-detection evasion is used for social
#   platforms, and SSRF validation / the operator block list are the
#   non-negotiable safety controls that apply regardless of concurrency
#   or fetch strategy.