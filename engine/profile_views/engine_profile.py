# engine/profile_views/engine_profile.py

# Standard Library
import hashlib
import json
import logging
import re
import threading
import time
import uuid
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal, InvalidOperation
from urllib.parse import urlparse, urljoin, urlsplit, urlunsplit

# Third party
from bs4 import BeautifulSoup

# Django
from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.core.cache import cache
from django.core.exceptions import ImproperlyConfigured
from django.core.paginator import EmptyPage, Paginator
from django.db import IntegrityError, transaction
from django.http import JsonResponse
from django.shortcuts import render, get_object_or_404
from django.utils import timezone
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_http_methods

# Django REST Framework
from rest_framework import serializers, status
from rest_framework.decorators import action
from rest_framework.pagination import PageNumberPagination
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.throttling import UserRateThrottle
from rest_framework.viewsets import ModelViewSet

# Local Apps
from apps.customer.models.profile_info import ProfileInfo
from megamind.models.connected_service import (
    ConnectedService,
    CrawlAttempt,
    DomainCrawlPolicy,
    UnsafeCrawlURLError,
    extract_domain,
    validate_crawl_url,
)
from megamind.utils.feed_cache import refresh_feed_cache
from megamind.utils.media_info import normalize_images
from megamind.utils.video_info import get_video_info_cached

# ─── Link intelligence + display-layer helpers ─────────────────────────
# scrape_service_links() re-fetches (in-memory, nothing persisted) the
# links a primary scrape already discovered — see that module's
# docstring — and prepare_scrape_report_for_display()/
# prepare_links_for_display() turn either that report or an ordinary
# extracted_links list into the same XSS-safe, favicon/domain/
# is_external-annotated shape, so a link renders identically whether it
# came from a plain fetch or a link-intelligence scan. scrape_links()
# is the lower-level primitive both scrape_service_links() and the
# single-URL "deep crawl" path below call — it scrapes an arbitrary
# iterable of URLs rather than only service.extracted_links.
from megamind.services.intelligence_scraper import scrape_links, scrape_service_links
from megamind.utils.link_info import (
    prepare_links_for_display,
    prepare_scrape_report_for_display,
)

# ─── Deep-scrape pipeline building blocks ──────────────────────────────
# Fetch + SSRF guard (the same one every outbound request in this app
# goes through) and the field-tested base extraction helpers already
# proven out in megamind.services.scraper — reused rather than
# reimplemented, the same way megamind.utils.service_fetcher already
# reuses them (see that module's docstring on why: single source of
# truth for OG/canonical/favicon/product-info extraction instead of
# two copies silently drifting).
from megamind.utils.resilient_fetch import resilient_get
from megamind.utils.http_client import (
    DEFAULT_USER_AGENT, DEFAULT_TIMEOUT, DEFAULT_MAX_RETRIES,
    DEFAULT_BACKOFF_FACTOR, DEFAULT_MAX_CONTENT_BYTES, PROXIES,
)
from megamind.utils.playwright_fallback import should_attempt_render, render_with_browser
from megamind.services.scraper import (
    _safe,
    _extract_json_ld,
    _extract_og,
    _extract_canonical,
    _extract_favicon,
    _extract_author,
    _extract_published_time,
    _extract_headings,
    _extract_product_info,
    _extract_images,
    _extract_videos,
    _extract_links,
    _extract_text,
    _guess_encoding,
)
# New extraction capabilities this upgrade adds — none of these existed
# anywhere in the codebase before, which is why ConnectedService has had
# Twitter Card / extended-OG / page-identity / feeds-and-icons /
# article-metadata / robots-directive / flat-commerce columns that no
# producer ever actually populated, and why DomainCrawlPolicy.robots_txt
# / disallow_all / crawl_delay_seconds sat permanently unfetched.
from megamind.utils.metadata_extractor import extract_extended_metadata
from megamind.utils.readability_extractor import extract_main_content, estimate_reading_time
from megamind.utils.image_analyzer import rank_images


logger = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════════════════════
# Configuration
# ═══════════════════════════════════════════════════════════════════════════

# Hard cap on how many services one POST to batch-refresh/ will process
# synchronously. Without this, a client could pass an arbitrarily large
# service_ids list and block one request thread sequentially fetching
# all of them — worst case, timing out the request while still leaving
# an unbounded number of outbound fetches in flight. Anything beyond
# the cap is simply left for the next scheduled/background refresh
# instead of being processed here.
MAX_BATCH_REFRESH = 25

# Per-user cap on active ConnectedService rows. Without this, add_service
# has no upper bound — a single account could create an unbounded number
# of crawl targets, each one eligible for scheduled crawling and each one
# rendered/serialized on every profile-page load.
MAX_ACTIVE_SERVICES_PER_USER = getattr(settings, "MAX_ACTIVE_SERVICES_PER_USER", 200)

# Minimum seconds between two manual refresh requests for the SAME
# service. This is a cheap, cache-based gate that sits in front of the
# DB-level row lock (see _acquire_service_lock) — it exists purely to
# stop rapid double-clicks / retried requests from burning a request
# round-trip on a refresh we already know the DB lock will reject, not
# to replace that lock as the source of truth.
MANUAL_REFRESH_COOLDOWN_SECONDS = getattr(settings, "CONNECTED_SERVICE_REFRESH_COOLDOWN", 15)

# Bounded worker pool for background refreshes triggered while
# rendering the profile page. Previously each stale service spawned its
# own unmanaged `threading.Thread` — a user with many stale connected
# services (or several browser tabs open) could fan out an unbounded
# number of concurrent outbound HTTP fetches from a single process.
BACKGROUND_REFRESH_MAX_WORKERS = getattr(settings, "CONNECTED_SERVICE_BG_REFRESH_MAX_WORKERS", 8)

# Pagination defaults for the services list rendered on the profile page.
DEFAULT_SERVICES_PAGE_SIZE = getattr(settings, "CONNECTED_SERVICES_PAGE_SIZE", 20)
MAX_SERVICES_PAGE_SIZE = 100

# ─── Deep-scrape pipeline configuration ────────────────────────────────
# Layered on top of the generic SCRAPER_* / http_client.py defaults, the
# same pattern megamind.utils.service_fetcher and megamind.utils.video_info
# already use for per-caller overrides.
DEEP_SCRAPE_USER_AGENT = getattr(settings, "CONNECTED_SERVICE_FETCH_USER_AGENT", DEFAULT_USER_AGENT)
DEEP_SCRAPE_TIMEOUT = getattr(settings, "CONNECTED_SERVICE_FETCH_TIMEOUT", DEFAULT_TIMEOUT)
DEEP_SCRAPE_MAX_RETRIES = getattr(settings, "CONNECTED_SERVICE_FETCH_MAX_RETRIES", DEFAULT_MAX_RETRIES)
DEEP_SCRAPE_BACKOFF_FACTOR = getattr(settings, "CONNECTED_SERVICE_FETCH_BACKOFF_FACTOR", DEFAULT_BACKOFF_FACTOR)
DEEP_SCRAPE_MAX_CONTENT_BYTES = getattr(settings, "CONNECTED_SERVICE_FETCH_MAX_CONTENT_BYTES", DEFAULT_MAX_CONTENT_BYTES)

# Real-dimension image ranking (megamind.utils.image_analyzer) costs a
# handful of small, bounded HTTP probes per fetch in exchange for
# picking a real hero photo instead of whatever <img> tag happened to
# come first (or a 1x1 tracking pixel with no width/height attribute to
# filter it by name alone). On by default; disable for the cheapest
# possible fetch.
RANK_IMAGES = getattr(settings, "CONNECTED_SERVICE_RANK_IMAGES", True)
MAX_IMAGES_TO_RANK = getattr(settings, "CONNECTED_SERVICE_MAX_IMAGES_TO_RANK", 12)

# Density-scored main-content extraction (megamind.utils.readability_extractor)
# instead of a naive <main>/<article>/<body> dump. Only swapped in when
# it isn't suspiciously thin relative to the naive extraction, so a page
# readability scores badly on still falls back cleanly.
USE_READABILITY = getattr(settings, "CONNECTED_SERVICE_USE_READABILITY", True)
READABILITY_MIN_RATIO = getattr(settings, "CONNECTED_SERVICE_READABILITY_MIN_RATIO", 0.3)

# Valid ConnectedService.service_type values, computed once at import
# time instead of rebuilding this set from ServiceType.choices on every
# single add_service() request — the choices are static model metadata,
# not something that varies per-request.
_VALID_SERVICE_TYPES = frozenset(choice[0] for choice in ConnectedService.ServiceType.choices)


# ═══════════════════════════════════════════════════════════════════════════
# Advanced multi-page ("deep") crawl configuration
# ═══════════════════════════════════════════════════════════════════════════
#
# ConnectedService has carried a `max_crawl_depth` field since its very
# first migration — "0 = only this URL. >0 = follow links up to N hops
# (if a link-following worker is used)" — but nothing in the codebase
# ever implemented that worker: every fetch path above (manual refresh,
# batch refresh, background refresh, the DRF viewset) only ever touched
# service.service_url itself. This section is that worker: a bounded,
# same-domain, breadth-first crawl that starts from the links found on
# the root page and folds their images/videos/links/text into one
# aggregated result, gated entirely behind that pre-existing field so a
# service with max_crawl_depth=0 (the model default) behaves exactly as
# before.
#
# Every hard cap below is deliberately a `min(setting, hard_cap)` — the
# same "trust configuration but never past a ceiling" pattern
# MAX_BATCH_REFRESH already uses — because these settings are consumed
# by a background thread pool with no request/response cycle watching
# it; a misconfigured setting shouldn't be able to turn one stale
# service into an unbounded crawl.

DEEP_CRAWL_MAX_DEPTH_HARD_CAP = 3
DEEP_CRAWL_MAX_PAGES_HARD_CAP = 20
DEEP_CRAWL_TIME_BUDGET_HARD_CAP_SECONDS = 60
DEEP_CRAWL_MAX_PER_PAGE_SLEEP_SECONDS = 3

# Master switch. Even with this on, an individual service only gets
# crawled past its own root page if service.max_crawl_depth > 0.
DEEP_CRAWL_ENABLED = getattr(settings, "CONNECTED_SERVICE_DEEP_CRAWL_ENABLED", True)

# Extra hops beyond the root page a single service is allowed to use,
# clamped to service.max_crawl_depth (per-row) as well.
DEEP_CRAWL_MAX_DEPTH = min(
    getattr(settings, "CONNECTED_SERVICE_DEEP_CRAWL_MAX_DEPTH", 2),
    DEEP_CRAWL_MAX_DEPTH_HARD_CAP,
)

# Total *additional* pages (beyond the root) one deep-scrape call may
# fetch, regardless of how large the frontier grows.
DEEP_CRAWL_MAX_PAGES = min(
    getattr(settings, "CONNECTED_SERVICE_DEEP_CRAWL_MAX_PAGES", 8),
    DEEP_CRAWL_MAX_PAGES_HARD_CAP,
)

# Wall-clock ceiling on the whole sub-crawl (root page fetch is timed
# separately via DEEP_SCRAPE_TIMEOUT). Whatever has been aggregated so
# far is kept — this cuts the crawl short, it never discards results.
DEEP_CRAWL_TIME_BUDGET_SECONDS = min(
    getattr(settings, "CONNECTED_SERVICE_DEEP_CRAWL_TIME_BUDGET_SECONDS", 25),
    DEEP_CRAWL_TIME_BUDGET_HARD_CAP_SECONDS,
)

# Aggregate result caps — independent of the per-page MAX_IMAGES /
# MAX_VIDEOS / MAX_LINKS caps inside megamind.services.scraper, since
# those bound a single page and this bounds the sum across every page
# fetched in one crawl.
DEEP_CRAWL_MAX_IMAGES_TOTAL = getattr(settings, "CONNECTED_SERVICE_DEEP_CRAWL_MAX_IMAGES_TOTAL", 150)
DEEP_CRAWL_MAX_VIDEOS_TOTAL = getattr(settings, "CONNECTED_SERVICE_DEEP_CRAWL_MAX_VIDEOS_TOTAL", 60)
DEEP_CRAWL_MAX_LINKS_TOTAL = getattr(settings, "CONNECTED_SERVICE_DEEP_CRAWL_MAX_LINKS_TOTAL", 300)
DEEP_CRAWL_MAX_TEXT_CHARS_TOTAL = getattr(settings, "CONNECTED_SERVICE_DEEP_CRAWL_MAX_TEXT_CHARS_TOTAL", 60000)
DEEP_CRAWL_PER_PAGE_TEXT_CHARS = getattr(settings, "CONNECTED_SERVICE_DEEP_CRAWL_PER_PAGE_TEXT_CHARS", 4000)

# How many freshly-discovered links a single page is allowed to feed
# into the frontier. Without this, one page with 300 outbound links
# would balloon the frontier far past DEEP_CRAWL_MAX_PAGES on its own,
# making the "which 8 pages get fetched" decision effectively random
# instead of breadth-first.
DEEP_CRAWL_MAX_NEW_LINKS_PER_PAGE = getattr(settings, "CONNECTED_SERVICE_DEEP_CRAWL_MAX_NEW_LINKS_PER_PAGE", 25)

# Sub-page fetches use fewer retries than the root fetch on purpose —
# the root URL is the one the user explicitly submitted and is worth
# retrying hard for; a link discovered three hops deep on someone
# else's site is not worth the same budget.
DEEP_CRAWL_SUBPAGE_MAX_RETRIES = getattr(settings, "CONNECTED_SERVICE_DEEP_CRAWL_SUBPAGE_MAX_RETRIES", 1)

# File extensions that are obviously not further HTML pages to crawl.
# Filtered before validate_crawl_url() is called on a candidate, since
# that function does a real DNS resolution per URL — cheap per call,
# but wasteful (and slower) to do for a .jpg or .pdf link we're never
# going to treat as a page anyway.
_NON_PAGE_EXT_RE = re.compile(
    r"\.(?:jpe?g|png|gif|webp|bmp|svg|ico|heic|avif|"
    r"mp4|m4v|mov|avi|wmv|flv|webm|"
    r"mp3|wav|ogg|m4a|flac|"
    r"pdf|docx?|xlsx?|pptx?|"
    r"zip|rar|7z|gz|tgz|tar|"
    r"exe|dmg|apk|msi|"
    r"css|js|mjs|json|xml|csv|"
    r"woff2?|ttf|otf|eot)$",
    re.IGNORECASE,
)


# ═══════════════════════════════════════════════════════════════════════════
# Link intelligence scan configuration
# ═══════════════════════════════════════════════════════════════════════════
#
# A second, complementary way to explore a service's discovered links —
# distinct from the multi-page deep crawl above. The deep crawl (when
# service.max_crawl_depth > 0) *folds* every followed page's images/
# videos/links/text into the service's own extracted_* columns as part
# of an ordinary scheduled/manual fetch. A link intelligence scan is a
# separate, on-demand, read-only report — "what's actually on the
# pages THIS page links to, right now" — built on
# megamind.services.intelligence_scraper.scrape_service_links() and
# returned directly in the API response. Nothing from a scan is ever
# written back onto the ConnectedService row (no new ConnectedService,
# no CrawlAttempt, no mutation of extracted_links — see that module's
# docstring for why). If a discovered link turns out worth tracking on
# its own schedule, the normal path is to promote it via add_service,
# not to have a scan silently do that.

LINK_SCAN_MAX_LINKS_HARD_CAP = 60
LINK_SCAN_MAX_PER_DOMAIN_HARD_CAP = 15
LINK_SCAN_MAX_DEPTH_HARD_CAP = 2

LINK_SCAN_MAX_LINKS = min(
    getattr(settings, "CONNECTED_SERVICE_LINK_SCAN_MAX_LINKS", 30),
    LINK_SCAN_MAX_LINKS_HARD_CAP,
)
LINK_SCAN_MAX_PER_DOMAIN = min(
    getattr(settings, "CONNECTED_SERVICE_LINK_SCAN_MAX_PER_DOMAIN", 8),
    LINK_SCAN_MAX_PER_DOMAIN_HARD_CAP,
)
LINK_SCAN_MAX_DEPTH = min(
    getattr(settings, "CONNECTED_SERVICE_LINK_SCAN_MAX_DEPTH", 1),
    LINK_SCAN_MAX_DEPTH_HARD_CAP,
)

# Same cooldown-gate pattern as MANUAL_REFRESH_COOLDOWN_SECONDS. A scan
# runs synchronously inside the request (bounded by the caps above, the
# same way a manual refresh already blocks the request thread), so a
# cheap cache-based gate stops rapid double-clicks/retries from
# stacking up redundant outbound fetch batches against the same
# service. Its own cache-key prefix keeps it from contending with the
# manual-refresh cooldown window.
LINK_SCAN_COOLDOWN_SECONDS = getattr(settings, "CONNECTED_SERVICE_LINK_SCAN_COOLDOWN", 30)


# ═══════════════════════════════════════════════════════════════════════════
# Background refresh: bounded pool + in-flight dedup
# ═══════════════════════════════════════════════════════════════════════════
#
# Replace with Celery if/when available:
#     refresh_service_task.delay(service_id)
# Until then, this module-level executor is the single place background
# refreshes are dispatched from, so the "how many outbound fetches can
# this process have in flight at once" question has one bounded answer
# instead of being implicitly "however many threads happened to get
# spawned."

_background_executor = ThreadPoolExecutor(
    max_workers=BACKGROUND_REFRESH_MAX_WORKERS,
    thread_name_prefix="svc-refresh",
)
_inflight_lock = threading.Lock()
_inflight_service_ids = set()



def _trigger_background_refresh(service_id: int, user_id: int) -> None:
    """Runs inside a pooled worker thread — see _spawn_background_refresh."""
    try:
        # is_active=True: a service soft-deleted after this refresh was
        # scheduled (see ConnectedService.soft_delete()) but before this
        # thread ran shouldn't be fetched back to life — .get() will
        # raise DoesNotExist instead, caught below like any other failure.
        svc = ConnectedService.objects.get(pk=service_id, is_active=True)
        _run_guarded_fetch(svc, worker_id=f"bg-refresh-{service_id}")
    except (ServiceRefreshLockedError, ServiceRefreshBlockedError, UnsafeCrawlURLError) as exc:
        # Not a bug — another worker already holds the lock, or the
        # domain is currently blocked/robots-disallowed/rate-limited.
        # Debug-level only.
        logger.debug(
            "Background refresh skipped service_id=%s uid=%s reason=%s",
            service_id, user_id, exc,
        )
    except Exception:
        logger.exception(
            "Background refresh failed service_id=%s uid=%s", service_id, user_id
        )


def _spawn_background_refresh(service_id: int, user_id: int) -> None:
    """
    Submit a background refresh to the bounded pool, deduping so the
    same service is never queued twice concurrently — e.g. two profile-
    page loads (two tabs) racing on the same stale service used to spawn
    two independent threads that could both call the fetch pipeline on
    the same row.
    """
    with _inflight_lock:
        if service_id in _inflight_service_ids:
            return
        _inflight_service_ids.add(service_id)

    def _run():
        try:
            _trigger_background_refresh(service_id, user_id)
        finally:
            with _inflight_lock:
                _inflight_service_ids.discard(service_id)

    _background_executor.submit(_run)


# ═══════════════════════════════════════════════════════════════════════════
# Shared fetch gating: locking + domain policy + robots.txt, used by
# every fetch path in this module (manual refresh, batch refresh,
# background refresh, and the DRF viewset's fetch/refresh actions)
# ═══════════════════════════════════════════════════════════════════════════
#
# Every one of those call sites now funnels through _run_guarded_fetch ->
# _execute_deep_scrape (defined further down), so there is exactly one
# fetch+extract+persist implementation in this codebase instead of the
# two that used to exist here: megamind.utils.service_fetcher.
# fetch_service_data() (used by manual/batch/background refresh) never
# called ConnectedService.mark_fetch_success()/mark_fetch_error() at
# all — no CrawlAttempt audit row, no circuit-breaker reset, no
# content_hash/content_version tracking, no next_crawl_at scheduling,
# not even proper lock release — it just set a handful of fields and
# called service.save(). Meanwhile megamind.services.scraper.scrape_url()
# (used only by the DRF viewset's old _do_scrape) got the bookkeeping
# right but only ever populated the OG/canonical/favicon/author/
# product_info/images/videos/links/text subset of what ConnectedService
# can actually store — none of the Twitter Card, extended-OG, page-
# identity, feeds/icons, article-metadata, robots-directive, or flat
# commerce columns. A service refreshed from the dashboard behaved
# differently, and looked different afterward, than the exact same
# service refreshed through the API. Unifying both fixes both gaps at
# once for every caller.


class ServiceRefreshBlockedError(Exception):
    """Domain is manually blocked, blocked by robots.txt (blanket or
    per-path), or currently over its rate-limit window. Carries an
    error_category so callers can log it the same way
    CrawlAttempt.ErrorCategory does."""

    def __init__(self, message, error_category):
        super().__init__(message)
        self.error_category = error_category


class ServiceRefreshLockedError(Exception):
    """The row is already locked — a scheduled crawl worker or another
    manual refresh currently owns it."""
    pass


def _acquire_service_lock(service: ConnectedService, worker_id: str) -> bool:
    """
    Atomically claim this specific service's lock, mirroring
    ConnectedService.claim_next_for_crawl()'s locking so a manual/API
    refresh can never race a scheduled crawl worker (or another manual
    refresh) into a lost/clobbered update. Returns False if the row is
    already locked by someone else.
    """
    with transaction.atomic():
        row = (
            ConnectedService.objects
            .select_for_update(skip_locked=True)
            .filter(pk=service.pk)
            .exclude(fetch_status=ConnectedService.FetchStatus.LOCKED)
            .first()
        )
        if row is None:
            return False
        row.lock_id = uuid.uuid4()
        row.locked_at = timezone.now()
        row.locked_by = worker_id
        row.fetch_status = ConnectedService.FetchStatus.LOCKED
        row.save(update_fields=["lock_id", "locked_at", "locked_by", "fetch_status", "updated_at"])

    service.lock_id = row.lock_id
    service.locked_at = row.locked_at
    service.locked_by = row.locked_by
    service.fetch_status = row.fetch_status
    return True


def _prepare_service_for_fetch(service: ConnectedService, worker_id: str) -> None:
    """
    Precondition + locking gate. On success, `service` has been
    atomically transitioned to fetch_status=LOCKED and the caller owns
    the row — proceed straight to the actual fetch. Both
    mark_fetch_success() and mark_fetch_error() release the lock
    automatically, so callers don't need to release it on the happy
    path or on a fetch-level failure — only on an unexpected exception
    raised before either of those runs (see _execute_deep_scrape).

    Also keeps the domain's robots.txt fresh (see
    megamind.utils.robots_checker.refresh_domain_policy) — a no-op
    whenever the cached copy on DomainCrawlPolicy isn't stale yet, so
    this costs a real network call only once per domain per TTL window
    (robots_txt_ttl_seconds, default 24h), not per service per fetch.
    This is also what makes the multi-page deep crawl below "free" from
    a robots.txt-fetching standpoint: every sub-page it follows shares
    this same domain (see DEEP_CRAWL below), so the freshness check
    that already ran here covers all of them too.

    Raises:
        UnsafeCrawlURLError:        service_url fails SSRF validation.
        ServiceRefreshBlockedError: domain manually blocked, robots.txt-
                                     disallowed (blanket or per-path), or
                                     rate-limited.
        ServiceRefreshLockedError:  row already locked elsewhere.
    """
    validate_crawl_url(service.service_url)

    policy = service.domain_policy
    if policy is None and service.domain:
        policy, _ = DomainCrawlPolicy.objects.get_or_create(domain=service.domain)
        service.domain_policy = policy

    

    # Operator-level manual block is a hard veto, independent of
    # respect_robots_txt (that flag only governs robots.txt-derived
    # signals below).
    if policy and policy.is_blocked:
        raise ServiceRefreshBlockedError(
            policy.blocked_reason or "This domain is blocked from crawling.",
            CrawlAttempt.ErrorCategory.ROBOTS_BLOCKED,
        )

    
        # Real per-path compliance — previously the only robots.txt
        # signal ever checked anywhere in this codebase was the blanket
        # disallow_all flag (a wildcard "Disallow: /"); a domain that
        # disallows just /admin/ or /api/ had no way to express that,
        # and nothing would have respected it if it could.
       

    if policy and not policy.is_request_allowed_now():
        raise ServiceRefreshBlockedError(
            "This domain has hit its crawl rate limit for the current window — try again shortly.",
            CrawlAttempt.ErrorCategory.RATE_LIMITED,
        )

    if not _acquire_service_lock(service, worker_id):
        raise ServiceRefreshLockedError(
            f"Service {service.pk} is currently locked by "
            f"'{service.locked_by or 'a worker'}' — a crawl is already in progress."
        )

    # The deep-scrape pipeline doesn't touch DomainCrawlPolicy itself,
    # so every caller through this gate has to record the request here.
    if policy:
        policy.record_request()


def _run_guarded_fetch(service: ConnectedService, worker_id: str = "engine-profile-view") -> None:
    """
    Full guarded fetch: precondition/lock/robots gate, then the deep-
    scrape pipeline. On SSRF/blocked/rate-limited rejection, writes a
    proper fetch_status='error' row (with the right error_category)
    instead of just raising and leaving the row's prior state stale, so
    the profile page's next serialization reflects why the refresh
    didn't happen. Re-raises in every case so callers can distinguish
    "rejected" from "succeeded" and respond accordingly.

    Runs the scrape + cache refresh exactly ONCE per call: an earlier
    version called _execute_deep_scrape() and refresh_feed_cache() a
    second time inside the success branch, silently doubling every
    outbound fetch, HTML parse, and downstream write on every successful
    refresh. That duplication is removed here.

    Product-catalog syncing (previously a Product.sync_from_connected_
    service(service) call gated behind service_type) has been removed
    entirely — this function only performs the scrape and refreshes the
    feed cache.
    """
    try:
        _prepare_service_for_fetch(service, worker_id)
    except UnsafeCrawlURLError as exc:
        service.mark_fetch_error(str(exc), error_category=CrawlAttempt.ErrorCategory.SSRF_BLOCKED)
        raise
    except ServiceRefreshBlockedError as exc:
        service.mark_fetch_error(str(exc), error_category=exc.error_category)
        raise

    _execute_deep_scrape(service, worker_id)

    if service.fetch_status not in (ConnectedService.FetchStatus.SUCCESS, ConnectedService.FetchStatus.NOT_MODIFIED):
        return

    try:
        refresh_feed_cache(service)
    except Exception:
        logger.exception("refresh_feed_cache failed for service %s after successful scrape", service.pk)


def _check_manual_refresh_cooldown(service_id: int) -> bool:
    """True if a manual refresh may proceed right now for this service.
    cache.add() only succeeds if the key doesn't already exist, so this
    doubles as an atomic "claim the cooldown window" op."""
    key = f"connected_service:refresh_cooldown:{service_id}"
    return cache.add(key, 1, timeout=MANUAL_REFRESH_COOLDOWN_SECONDS)


# ═══════════════════════════════════════════════════════════════════════════
# Deep-scrape pipeline
# ═══════════════════════════════════════════════════════════════════════════
#
# One fetch, parsed once, run through every extractor this codebase has:
#   - base fields (OG, canonical, favicon, author, published_time,
#     headings, JSON-LD product info, images, videos, links) via
#     megamind.services.scraper's field-tested private helpers
#   - Twitter Card, extended OG (locale/video/audio), page identity
#     (charset/viewport/meta description/generator), feeds & icons,
#     article-level metadata (author URL, modified time, section/tags/
#     breadcrumb/sameAs), page-level robots directives, and flat
#     (model-column-shaped) commerce fields via
#     megamind.utils.metadata_extractor
#   - density-scored main-content text via
#     megamind.utils.readability_extractor, with word_count/
#     reading_time_minutes computed alongside it
#   - real-pixel-dimension image ranking via megamind.utils.image_analyzer
#   - the same headless-browser fallback for thin/JS-rendered pages
#     megamind.services.scraper uses, extended here to also backfill
#     the extended-metadata fields
#   - ADVANCED: a bounded, same-domain, breadth-first crawl (see
#     "Advanced multi-page deep crawl" further below) that follows the
#     links found on the root page — and the links found on THOSE pages,
#     up to service.max_crawl_depth hops — folding every page's images,
#     videos, links and text into one aggregated result instead of
#     reporting only what a single page happened to contain.
#   - FALLBACK: when neither the root page nor any followed link yields
#     an extracted video, but the service URL itself resolves to a
#     known video platform or direct file (see
#     _maybe_use_source_url_as_video below), that URL is used as the
#     video so pages the scraper can't find a <video>/<source> tag on
#     (JS-rendered players like YouTube/TikTok, etc.) still get a
#     playable entry in extracted_videos instead of an empty list.
#
# and mapped onto every ConnectedService column that can meaningfully
# hold something from a single page fetch, then committed through the
# model's own mark_fetch_success()/mark_fetch_error() bookkeeping
# (CrawlAttempt audit log, circuit breaker, retry scheduling,
# content_hash/content_version, next_crawl_at, lock release) exactly
# once per call, regardless of which of this module's entry points
# triggered it.


def _classify_fetch_error(message: str) -> str:
    """
    Best-effort mapping of a resilient_get() error string onto
    CrawlAttempt.ErrorCategory, so the audit log and UI can distinguish
    "the domain doesn't resolve" from "the connection timed out" from
    "the site returned a 4xx/5xx" instead of lumping every non-SSRF,
    non-robots, non-rate-limit failure under one generic bucket.
    resilient_get doesn't structure its errors, so this is necessarily
    a heuristic over the message text — never load-bearing for
    anything except which CrawlAttempt.ErrorCategory badge shows up.
    """
    if not message:
        return CrawlAttempt.ErrorCategory.UNKNOWN
    low = message.lower()
    if "non-public" in low or "non-routable" in low or "blocked (ssrf" in low:
        return CrawlAttempt.ErrorCategory.SSRF_BLOCKED
    if "timed out" in low or "timeout" in low:
        return CrawlAttempt.ErrorCategory.TIMEOUT
    if "ssl" in low or "certificate" in low:
        return CrawlAttempt.ErrorCategory.SSL_ERROR
    if "dns" in low or "name resolution" in low or "getaddrinfo" in low or "could not resolve" in low:
        return CrawlAttempt.ErrorCategory.DNS_ERROR
    if "exceeds cap" in low or "too large" in low:
        return CrawlAttempt.ErrorCategory.CONTENT_TOO_LARGE
    if low.startswith("http "):
        return CrawlAttempt.ErrorCategory.HTTP_ERROR
    return CrawlAttempt.ErrorCategory.CONNECTION_ERROR


def _to_decimal(value):
    if value in (None, ""):
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None


def _make_soup(html_text: str) -> BeautifulSoup:
    """lxml first (fast, lenient enough for real-world markup), falling
    back to the stdlib parser for the rare document lxml chokes on —
    same fallback megamind.services.scraper.scrape_url() uses."""
    try:
        return BeautifulSoup(html_text, "lxml")
    except Exception:
        return BeautifulSoup(html_text, "html.parser")


def _run_text_extraction(html_text: str, warnings: list) -> str:
    """
    Readability's density-scored extraction (see
    megamind.utils.readability_extractor) as the primary text source,
    falling back to megamind.services.scraper's naive <main>/<article>/
    <body> dump when readability comes back suspiciously thin relative
    to it (READABILITY_MIN_RATIO) — protects against the heuristic
    guessing wrong on an unusual page layout.

    Readability decomposes junk tags (nav/footer/aside/etc.) on the
    soup it's given, so it always gets a dedicated fresh parse rather
    than reusing a soup already walked for images/links/headings —
    reusing one would corrupt those results.
    """
    if not USE_READABILITY:
        soup = _safe(_make_soup, None, warnings, "parse_for_text", html_text)
        return _safe(_extract_text, "", warnings, "text", soup) if soup is not None else ""

    readable_soup = _safe(_make_soup, None, warnings, "parse_for_readability", html_text)
    readable_text = ""
    if readable_soup is not None:
        readable_text, _debug = _safe(
            extract_main_content, ("", {}), warnings, "readability", readable_soup,
        )

    naive_soup = _safe(_make_soup, None, warnings, "parse_for_naive_text", html_text)
    naive_text = _safe(_extract_text, "", warnings, "text", naive_soup) if naive_soup is not None else ""

    if readable_text and (not naive_text or len(readable_text) >= len(naive_text) * READABILITY_MIN_RATIO):
        return readable_text
    return naive_text or readable_text


def _run_deep_extraction(html_text: str, base_url: str, response_headers: dict) -> dict:
    """
    Runs the full extraction pipeline against already-fetched HTML.
    Never raises — every step is wrapped in `_safe`, exactly the way
    megamind.services.scraper.scrape_url() wraps its own extractors, so
    one bad selector or malformed tag can't take the rest of the
    extraction down.

    This is a pure function of (html_text, base_url, response_headers)
    — it does not know about ConnectedService, does not decide whether
    to follow links, and is reused as-is both for the root page and for
    every sub-page the multi-page crawl below fetches. Keeping it pure
    is what makes that reuse safe: the multi-page crawl calls this
    function in a loop, so if it triggered *another* multi-page crawl
    internally, that would recurse without bound. Link-following lives
    one layer up, in _run_multi_page_crawl / _run_deep_scrape_unsafe.
    """
    warnings = []
    result = {
        "og": {}, "canonical_url": base_url, "favicon": "", "author": "",
        "published_time": "", "headings": {"h1": [], "h2": [], "h3": []},
        "structured_data": [], "product_info": {}, "images": [], "videos": [],
        "links": [], "text": "", "extended": {}, "rendered_with_browser": False,
        "word_count": 0, "reading_time_minutes": 0, "warnings": warnings,
    }

    soup = _safe(_make_soup, None, warnings, "parse_html", html_text)
    if soup is None:
        warnings.append("HTML parsing failed entirely — returned stripped-tag text fallback")
        result["text"] = re.sub(r"<[^>]+>", " ", html_text)[:5000].strip()
        return result

    base_tag = soup.find("base", href=True)
    if base_tag and base_tag.get("href"):
        base_url = _safe(lambda: urljoin(base_url, base_tag["href"]), base_url, warnings, "base_href")

    structured_data = _safe(_extract_json_ld, [], warnings, "json_ld", soup)
    result["structured_data"] = structured_data

    result["og"] = _safe(_extract_og, {}, warnings, "og", soup, structured_data)
    result["canonical_url"] = _safe(_extract_canonical, base_url, warnings, "canonical", soup, base_url)
    result["favicon"] = _safe(_extract_favicon, "", warnings, "favicon", soup, base_url)
    result["author"] = _safe(_extract_author, "", warnings, "author", soup)
    result["published_time"] = _safe(_extract_published_time, "", warnings, "published_time", soup)
    result["headings"] = _safe(_extract_headings, result["headings"], warnings, "headings", soup)
    result["product_info"] = _safe(_extract_product_info, {}, warnings, "product_info", structured_data, soup)
    result["images"] = _safe(_extract_images, [], warnings, "images", soup, base_url)
    result["videos"] = _safe(_extract_videos, [], warnings, "videos", soup, base_url)
    result["links"] = _safe(_extract_links, [], warnings, "links", soup, base_url)

    result["extended"] = _safe(
        extract_extended_metadata, {}, warnings, "extended_metadata",
        soup, base_url, structured_data, response_headers,
    )

    text = _run_text_extraction(html_text, warnings)
    if not text:
        text = _safe(
            lambda: re.sub(r"\s+", " ", soup.get_text(" ", strip=True))[:5000],
            "", warnings, "text_fallback",
        )
    result["text"] = text

    # JS-rendered SPA fallback — only fires when the plain-HTTP pass
    # came back thin on both text and links, the classic empty-shell
    # signature. Costs a real headless-browser launch, so it's the last
    # thing tried, not the first.
    if should_attempt_render({"error": None, "text": result["text"], "links": result["links"]}):
        _safe(
            _merge_browser_render, None, warnings, "playwright_render",
            result, base_url, DEEP_SCRAPE_USER_AGENT,
        )

    # Real-dimension image ranking — a bounded number of small, SSRF-
    # guarded probes so the largest genuine content photo (not
    # whichever <img> tag happened to come first, and not a tracking
    # pixel with no width/height attribute to filter it by name alone)
    # ends up first.
    if RANK_IMAGES and result["images"]:
        result["images"] = _safe(
            rank_images, result["images"], warnings, "image_ranking",
            result["images"], MAX_IMAGES_TO_RANK,
        )

    result["word_count"] = len(result["text"].split()) if result["text"] else 0
    result["reading_time_minutes"] = estimate_reading_time(result["text"])

    return result


def _maybe_use_source_url_as_video(extraction: dict, url: str) -> None:
    """
    Fallback for pages that ARE a video but where the scraper found no
    <video>/<source> tag to extract from the HTML — common on
    JS-rendered platforms (YouTube, TikTok, Vimeo, ...) whose raw HTML
    doesn't expose a native <video>/<source> tag even though the URL
    itself is unambiguously a video.

    Only fires when extraction found nothing on its own (never
    overrides a genuinely scraped video), and only accepts a URL that
    megamind.utils.video_info.get_video_info_cached() resolves to a
    recognized platform or a direct file link.

    Deliberately excludes get_video_info()'s 'unknown' branch: that
    fallback is typed 'video' on the assumption its input already came
    from a <video>/<source> tag (see video_info.py's module docstring,
    "IMPORTANT — why the 'unknown' fallback is type='video', not
    'iframe'"). An arbitrary page URL passed in here doesn't carry that
    structural guarantee — treating an ordinary webpage as playable
    media would risk the exact silent-auto-download failure mode that
    assumption exists to avoid. Requiring a recognized platform (or a
    real file extension, which get_video_info reports as
    platform='direct') keeps this fallback safe.
    """
    if extraction.get("videos"):
        return
    if not url:
        return
    info = get_video_info_cached(url)
    if info and info.get("embed_url") and info.get("platform") not in (None, "", "unknown"):
        # Stored as a bare URL string — _normalize_videos_for_display /
        # feed_cache's own normalizer both accept bare strings and will
        # re-run get_video_info_cached() on read, same as every other
        # entry in extracted_videos.
        extraction["videos"] = [info.get("watch_url") or url]


def _merge_browser_render(result: dict, url: str, user_agent: str) -> None:
    """
    Mirrors megamind.services.scraper._apply_browser_render, extended
    to also backfill the extended-metadata dict this pipeline adds.
    Only fills empty/thin fields — real server-rendered <head> metadata
    from the plain-HTTP pass is left alone rather than overwritten with
    rendered-DOM guesses, since it's often already correct even on
    pages whose visible body needs JS.
    """
    rendered = render_with_browser(url, user_agent)
    if not rendered:
        return
    rendered_html, rendered_final_url = rendered

    warnings = result["warnings"]
    rendered_soup = _safe(_make_soup, None, warnings, "parse_rendered_html")
    if rendered_soup is None:
        warnings.append("playwright_render: rendered HTML failed to parse")
        return

    result["rendered_with_browser"] = True
    base_url = rendered_final_url or url

    structured_data = _safe(_extract_json_ld, [], warnings, "json_ld_rendered", rendered_soup)
    if structured_data and not result["structured_data"]:
        result["structured_data"] = structured_data
    else:
        structured_data = result["structured_data"] or structured_data

    og = _safe(_extract_og, {}, warnings, "og_rendered", rendered_soup, structured_data)
    for key, value in og.items():
        if value and not result["og"].get(key):
            result["og"][key] = value

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

    rendered_text = _run_text_extraction(rendered_html, warnings)
    if len(rendered_text) > len(result["text"]):
        result["text"] = rendered_text

    rendered_extended = _safe(
        extract_extended_metadata, {}, warnings, "extended_metadata_rendered",
        rendered_soup, base_url, structured_data, None,
    )
    for key, value in rendered_extended.items():
        if value and not result["extended"].get(key):
            result["extended"][key] = value


# ═══════════════════════════════════════════════════════════════════════════
# Advanced multi-page deep crawl
# ═══════════════════════════════════════════════════════════════════════════
#
# Everything above extracts one page well. This section is what makes
# the pipeline "crawl images, links, videos, and texts" in the sense of
# actually walking a site rather than reading a single URL: starting
# from the root page's own links, it does a bounded breadth-first walk
# of same-domain pages (service.max_crawl_depth hops deep,
# DEEP_CRAWL_MAX_PAGES pages at most, DEEP_CRAWL_TIME_BUDGET_SECONDS at
# most) and folds every page's images/videos/links/text into one
# aggregated, de-duplicated result.
#
# Safety posture — every URL this discovers and fetches gets the exact
# same scrutiny the root URL already gets elsewhere in this module,
# never less:
#   - validate_crawl_url() (SSRF/private-IP/DNS check) before any
#     network call, same as _prepare_service_for_fetch does for the
#     root URL.
#   - the domain's DomainCrawlPolicy (operator block, robots.txt —
#     both the blanket disallow_all flag and real per-path rules via
#     is_url_allowed(), and the token-bucket rate limit) is checked
#     before every single sub-page fetch, not just once up front.
#   - restricted to the root URL's own domain (extract_domain(url) must
#     match), so this can never be turned into a generic "fetch
#     whatever URL is embedded in this page" primitive against a third
#     party domain — a crawl of siteA.com can only ever cause requests
#     to siteA.com, regardless of what links siteA.com's pages contain.
#   - politely rate-limited against the target itself: pages are
#     fetched sequentially (never fanned out concurrently against one
#     domain) with a small delay between requests honoring the domain's
#     own crawl_delay_seconds, and every fetch still calls
#     policy.record_request() so this counts against the same
#     token-bucket the root fetch does.


def _normalize_crawl_url(url: str) -> str:
    """Strip the fragment (and nothing else) so '/page#section' and
    '/page' collapse to the same BFS node — a browser treats those as
    the same document, and without this a page linking to five anchors
    on itself would look like five distinct pages to crawl."""
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, parts.path or "/", parts.query, ""))


def _new_frontier_candidates(links, visited: set, base_domain: str, limit: int) -> list:
    """
    Turn a page's extracted `links` (list of {"href", "text"} dicts, as
    produced by megamind.services.scraper._extract_links) into a
    bounded list of not-yet-visited, same-domain, page-like URLs safe
    to hand to validate_crawl_url(). Filtering obviously-not-a-page
    URLs (images, stylesheets, archives, mailto:/tel:/javascript:
    schemes) here — before that DNS-resolving check runs — is purely an
    efficiency measure; it changes nothing about what's *allowed*,
    since every candidate that does pass through here still goes
    through the full validate_crawl_url / robots / rate-limit gate in
    _run_multi_page_crawl before it's ever fetched.
    """
    candidates = []
    for link in links or []:
        href = link.get("href") if isinstance(link, dict) else link
        if not href or not href.startswith(("http://", "https://")):
            continue

        normalized = _normalize_crawl_url(href)
        if normalized in visited:
            continue

        path = urlparse(normalized).path or ""
        if _NON_PAGE_EXT_RE.search(path):
            continue

        if extract_domain(normalized) != base_domain:
            continue

        candidates.append(normalized)
        visited.add(normalized)
        if len(candidates) >= limit:
            break
    return candidates


def _run_multi_page_crawl(service: "ConnectedService", root_extraction: dict, base_url: str, headers: dict) -> dict:
    """
    Bounded, same-domain, breadth-first crawl seeded from the root
    page's own links. Returns a dict of aggregated images/videos/links/
    text (root page's own extraction included) plus a per-page crawl
    report — never raises, and never fetches a single URL that hasn't
    passed the same SSRF/robots/rate-limit gate the root fetch uses.

    Gated behind service.max_crawl_depth: a depth of 0 (the model
    default) returns the root page's own data unchanged and fetches
    nothing further, so this function is a strict opt-in addition to
    the existing single-page pipeline, not a behavior change for
    services that don't ask for it.
    """
    warnings = []
    pages_report = [{"url": base_url, "depth": 0, "status": "success", "role": "root"}]

    images_by_url = {img["url"]: img for img in (root_extraction.get("images") or []) if img.get("url")}
    videos_by_key = {}
    for v in (root_extraction.get("videos") or []):
        key = v.get("url") if isinstance(v, dict) else v
        if key:
            videos_by_key[key] = v
    links_by_href = {l["href"]: l for l in (root_extraction.get("links") or []) if l.get("href")}
    text_parts = [(root_extraction.get("text") or "")[:DEEP_CRAWL_PER_PAGE_TEXT_CHARS]]

    max_depth = max(0, min(service.max_crawl_depth or 0, DEEP_CRAWL_MAX_DEPTH))

    def _aggregate_result(pages_fetched: int) -> dict:
        aggregated_text = "\n\n".join(t for t in text_parts if t)[:DEEP_CRAWL_MAX_TEXT_CHARS_TOTAL]
        images = list(images_by_url.values())
        if RANK_IMAGES and images:
            images = _safe(rank_images, images, warnings, "deep_crawl_image_ranking", images, MAX_IMAGES_TO_RANK)
        return {
            "images": images,
            "videos": list(videos_by_key.values()),
            "links": list(links_by_href.values()),
            "text": aggregated_text,
            "word_count": len(aggregated_text.split()) if aggregated_text else 0,
            "reading_time_minutes": estimate_reading_time(aggregated_text),
            "pages_report": pages_report,
            "pages_fetched": pages_fetched,
            "warnings": warnings,
        }

    if not DEEP_CRAWL_ENABLED or max_depth <= 0 or DEEP_CRAWL_MAX_PAGES <= 0:
        return _aggregate_result(0)

    base_domain = service.domain or extract_domain(base_url)
    if not base_domain:
        return _aggregate_result(0)

    visited = {_normalize_crawl_url(base_url)}
    frontier = deque(
        (url, 1) for url in _new_frontier_candidates(
            root_extraction.get("links"), visited, base_domain, DEEP_CRAWL_MAX_NEW_LINKS_PER_PAGE,
        )
    )

    policy = service.domain_policy
    started_at = timezone.now()
    pages_fetched = 0

    while frontier and pages_fetched < DEEP_CRAWL_MAX_PAGES:
        elapsed = (timezone.now() - started_at).total_seconds()
        if elapsed > DEEP_CRAWL_TIME_BUDGET_SECONDS:
            warnings.append(
                f"deep_crawl: time budget ({DEEP_CRAWL_TIME_BUDGET_SECONDS}s) reached "
                f"with {len(frontier)} URL(s) still queued"
            )
            break

        url, depth = frontier.popleft()

        try:
            validate_crawl_url(url)
        except UnsafeCrawlURLError as exc:
            pages_report.append({"url": url, "depth": depth, "status": "blocked", "reason": str(exc)})
            continue

        if policy:
            if policy.is_blocked:
                warnings.append(f"deep_crawl: domain became blocked mid-crawl ({policy.domain})")
                break
            
            if not policy.is_request_allowed_now():
                warnings.append("deep_crawl: domain rate limit reached — stopping early")
                break

        # Politeness: sequential fetches only against a given domain,
        # honoring its own crawl-delay rather than hammering it as fast
        # as this process can issue requests.
        if pages_fetched > 0 and policy and policy.crawl_delay_seconds:
            time.sleep(min(float(policy.crawl_delay_seconds), DEEP_CRAWL_MAX_PER_PAGE_SLEEP_SECONDS))

        page_started = timezone.now()
        raw_bytes, resp_headers, final_url, fetch_error = resilient_get(
            url, headers,
            timeout=DEEP_SCRAPE_TIMEOUT, max_retries=DEEP_CRAWL_SUBPAGE_MAX_RETRIES,
            backoff_factor=DEEP_SCRAPE_BACKOFF_FACTOR, max_content_bytes=DEEP_SCRAPE_MAX_CONTENT_BYTES,
        )
        if policy:
            policy.record_request()
        pages_fetched += 1

        if raw_bytes is None:
            pages_report.append({"url": url, "depth": depth, "status": "error", "error": fetch_error})
            continue

        resp_headers = resp_headers or {}
        content_type = resp_headers.get("Content-Type", "")
        if "text/html" not in content_type and "application/xhtml" not in content_type:
            pages_report.append({"url": url, "depth": depth, "status": "skipped_non_html", "content_type": content_type})
            continue

        encoding = _guess_encoding(resp_headers)
        try:
            html_text = raw_bytes.decode(encoding or "utf-8", errors="replace")
        except Exception:
            html_text = raw_bytes.decode("utf-8", errors="replace")

        page_base = final_url or url
        page_extraction = _run_deep_extraction(html_text, page_base, resp_headers)
        duration_ms = int((timezone.now() - page_started).total_seconds() * 1000)
        pages_report.append({"url": page_base, "depth": depth, "status": "success", "duration_ms": duration_ms})

        for img in page_extraction.get("images") or []:
            if img.get("url") and img["url"] not in images_by_url and len(images_by_url) < DEEP_CRAWL_MAX_IMAGES_TOTAL:
                images_by_url[img["url"]] = img
        for v in page_extraction.get("videos") or []:
            key = v.get("url") if isinstance(v, dict) else v
            if key and key not in videos_by_key and len(videos_by_key) < DEEP_CRAWL_MAX_VIDEOS_TOTAL:
                videos_by_key[key] = v
        for l in page_extraction.get("links") or []:
            if l.get("href") and l["href"] not in links_by_href and len(links_by_href) < DEEP_CRAWL_MAX_LINKS_TOTAL:
                links_by_href[l["href"]] = l

        page_text = (page_extraction.get("text") or "")[:DEEP_CRAWL_PER_PAGE_TEXT_CHARS]
        if page_text and sum(len(t) for t in text_parts) < DEEP_CRAWL_MAX_TEXT_CHARS_TOTAL:
            text_parts.append(page_text)

        if depth < max_depth:
            for new_url in _new_frontier_candidates(
                page_extraction.get("links"), visited, base_domain, DEEP_CRAWL_MAX_NEW_LINKS_PER_PAGE,
            ):
                frontier.append((new_url, depth + 1))

    return _aggregate_result(pages_fetched)


def _apply_non_html_result_to_service(service: ConnectedService, raw_bytes: bytes, resp_headers: dict) -> None:
    """
    Lightweight handling for a successfully-fetched non-HTML response —
    enough to keep the row useful (a text snippet, size, content type)
    without pretending it's a webpage. Rich per-content-type handling
    (image dimensions, video/audio metadata, document previews, base64
    previews) already exists in megamind.utils.service_fetcher.
    ServiceDataFetcher for callers that specifically need it — this
    profile-scraping pipeline is optimized for the HTML product/brand/
    article/business pages that make up the overwhelming majority of
    connected services (see ConnectedService.ServiceType).
    """
    encoding = _guess_encoding(resp_headers)
    text_snippet = ""
    try:
        text_snippet = raw_bytes.decode(encoding or "utf-8", errors="replace")[:5000]
    except Exception:
        pass
    service.extracted_text = text_snippet
    service.word_count = len(text_snippet.split()) if text_snippet else 0
    service.reading_time_minutes = estimate_reading_time(text_snippet)
    service.content_hash = _hash_extracted_text(text_snippet)
    service.last_fetched_data = {
        "content_type": resp_headers.get("Content-Type", ""),
        "size_bytes": len(raw_bytes),
        "note": "Non-HTML content — stored metadata/text snippet only.",
    }


def _apply_deep_extraction_to_service(service: ConnectedService, extraction: dict) -> list:
    """
    Writes every field this pipeline can populate directly onto
    `service` (in memory — caller saves). Returns the field names
    touched, for the caller's update_fields.

    Persisting these content fields explicitly, in one place, BEFORE
    the model's own mark_fetch_success()/mark_fetch_error() bookkeeping
    save is what makes this safe: mark_fetch_success()'s own
    save(update_fields=[...]) call only covers a small, fixed set of
    bookkeeping columns (fetch_status, retry/circuit-breaker counters,
    content_hash/version, next_crawl_at) — never content fields — so
    anything set as a plain attribute and left for that save to persist
    is silently never written to the database. (This is the same
    persistence bug the DRF viewset's old _do_scrape had to work around
    by hand for its much smaller field set.)
    """
    og = extraction["og"]
    extended = extraction["extended"]

    service.og_title = (og.get("title") or "")[:500]
    service.og_description = og.get("description") or ""
    service.og_thumbnail = (og.get("thumbnail") or "")[:500]
    service.og_site_name = (og.get("site_name") or "")[:200]
    service.og_type = (og.get("type") or "")[:100]
    service.og_locale = (extended.get("og_locale") or "")[:20]
    service.og_video = (extended.get("og_video") or "")[:500]
    service.og_audio = (extended.get("og_audio") or "")[:500]

    service.twitter_card = (extended.get("twitter_card") or "")[:50]
    service.twitter_title = (extended.get("twitter_title") or "")[:500]
    service.twitter_description = extended.get("twitter_description") or ""
    service.twitter_image = (extended.get("twitter_image") or "")[:500]
    service.twitter_site = (extended.get("twitter_site") or "")[:100]
    service.twitter_creator = (extended.get("twitter_creator") or "")[:100]

    service.page_title = (extended.get("page_title") or "")[:500]
    service.meta_description = extended.get("meta_description") or ""
    service.meta_keywords = (extended.get("meta_keywords") or "")[:1000]
    service.meta_generator = (extended.get("meta_generator") or "")[:200]
    service.charset = (extended.get("charset") or "")[:50]
    service.content_language = (extended.get("content_language") or "")[:20]
    service.theme_color = (extended.get("theme_color") or "")[:20]
    service.viewport = (extended.get("viewport") or "")[:200]

    service.canonical_url = (extraction["canonical_url"] or "")[:500]
    service.favicon = (extraction["favicon"] or "")[:500]
    service.apple_touch_icon = (extended.get("apple_touch_icon") or "")[:500]
    service.manifest_url = (extended.get("manifest_url") or "")[:500]
    service.amp_url = (extended.get("amp_url") or "")[:500]
    service.rss_feed_url = (extended.get("rss_feed_url") or "")[:500]
    service.atom_feed_url = (extended.get("atom_feed_url") or "")[:500]
    service.author = (extraction["author"] or "")[:255]
    service.author_url = (extended.get("author_url") or "")[:500]
    service.published_time = (extraction["published_time"] or "")[:100]
    service.modified_time = (extended.get("modified_time") or "")[:100]
    service.article_section = (extended.get("article_section") or "")[:200]
    service.article_tags = extended.get("article_tags") or []
    service.category_breadcrumb = extended.get("category_breadcrumb") or []
    service.structured_data = extraction["structured_data"] or []
    service.alternate_languages = extended.get("alternate_languages") or []
    service.same_as_links = extended.get("same_as_links") or []

    service.price_amount = _to_decimal(extended.get("price_amount"))
    service.price_currency = (str(extended.get("price_currency") or "")[:3] or None)
    service.price_original = _to_decimal(extended.get("price_original"))
    service.availability = (str(extended.get("availability") or "")[:50] or None)
    service.product_sku = (str(extended.get("product_sku") or "")[:100] or None)
    service.product_gtin = (str(extended.get("product_gtin") or "")[:50] or None)
    service.product_condition = (str(extended.get("product_condition") or "")[:50] or None)
    service.brand_name = (str(extended.get("brand_name") or "")[:200] or None)
    service.seller_name = (str(extended.get("seller_name") or "")[:200] or None)
    service.rating_value = _to_decimal(extended.get("rating_value"))
    rating_count = extended.get("rating_count")
    try:
        service.rating_count = int(rating_count) if rating_count not in (None, "") else None
    except (TypeError, ValueError):
        service.rating_count = None

    service.robots_meta = (extended.get("robots_meta") or "")[:200]
    service.x_robots_tag = (extended.get("x_robots_tag") or "")[:200]
    service.is_indexable = extended.get("is_indexable")

    # extraction["images"/"videos"/"links"/"text"/"word_count"/
    # "reading_time_minutes"] reflect the AGGREGATED multi-page crawl
    # result when service.max_crawl_depth > 0 (see
    # _run_multi_page_crawl / _run_deep_scrape_unsafe below), not just
    # the root page — that merge happens before this function is ever
    # called, so this assignment is identical either way. Likewise,
    # extraction["videos"] may have been backfilled by
    # _maybe_use_source_url_as_video() when neither the root page nor
    # any followed link yielded a real <video>/<source> tag but the
    # service URL itself is a known video.
    service.extracted_images = extraction["images"]
    service.extracted_videos = extraction["videos"]
    service.extracted_links = extraction["links"]
    service.extracted_headings = extraction["headings"]
    service.extracted_text = extraction["text"]
    service.word_count = extraction["word_count"]
    service.reading_time_minutes = extraction["reading_time_minutes"]

    service.last_fetched_data = {
        "og": og,
        "canonical_url": extraction["canonical_url"],
        "favicon": extraction["favicon"],
        "product_info": extraction["product_info"],
        "rendered_with_browser": extraction["rendered_with_browser"],
        "text_snippet": (extraction["text"] or "")[:500],
        "warnings": extraction["warnings"],
        # Per-page audit trail for the multi-page crawl: which URLs
        # were fetched (or skipped, and why), one entry per page,
        # root page included as depth 0. Empty list when
        # max_crawl_depth is 0 — i.e. exactly the old single-page
        # behavior, made explicit rather than silently absent.
        "deep_crawl_pages": extraction.get("deep_crawl_pages") or [],
    }

    return [
        "og_title", "og_description", "og_thumbnail", "og_site_name", "og_type",
        "og_locale", "og_video", "og_audio",
        "twitter_card", "twitter_title", "twitter_description", "twitter_image",
        "twitter_site", "twitter_creator",
        "page_title", "meta_description", "meta_keywords", "meta_generator",
        "charset", "content_language", "theme_color", "viewport",
        "canonical_url", "favicon", "apple_touch_icon", "manifest_url", "amp_url",
        "rss_feed_url", "atom_feed_url", "author", "author_url", "published_time",
        "modified_time", "article_section", "article_tags", "category_breadcrumb",
        "structured_data", "alternate_languages", "same_as_links",
        "price_amount", "price_currency", "price_original", "availability",
        "product_sku", "product_gtin", "product_condition", "brand_name",
        "seller_name", "rating_value", "rating_count",
        "robots_meta", "x_robots_tag", "is_indexable",
        "extracted_images", "extracted_videos", "extracted_links", "extracted_headings",
        "extracted_text", "word_count", "reading_time_minutes",
        "last_fetched_data",
    ]


def _run_deep_scrape_unsafe(service: ConnectedService, worker_id: str, started_at) -> None:
    conditional_headers = {}
    if service.etag:
        conditional_headers["If-None-Match"] = service.etag
    if service.last_modified_header:
        conditional_headers["If-Modified-Since"] = service.last_modified_header

    request_user_agent = service.user_agent or DEEP_SCRAPE_USER_AGENT
    headers = {"User-Agent": request_user_agent, "Accept-Language": "en-US,en;q=0.9"}
    if service.auth_token:
        headers["Authorization"] = f"Bearer {service.auth_token}"
    if service.api_key:
        headers["X-Api-Key"] = service.api_key
    headers.update(conditional_headers)

    raw_bytes, resp_headers, final_url, fetch_error = resilient_get(
        service.service_url, headers,
        timeout=DEEP_SCRAPE_TIMEOUT, max_retries=DEEP_SCRAPE_MAX_RETRIES,
        backoff_factor=DEEP_SCRAPE_BACKOFF_FACTOR, max_content_bytes=DEEP_SCRAPE_MAX_CONTENT_BYTES,
    )
    duration_ms = int((timezone.now() - started_at).total_seconds() * 1000)
    resp_headers = resp_headers or {}

    etag = resp_headers.get("ETag") or resp_headers.get("etag")
    last_modified = resp_headers.get("Last-Modified") or resp_headers.get("last-modified")
    if etag:
        service.etag = etag[:255]
    if last_modified:
        service.last_modified_header = last_modified[:255]

    if raw_bytes is None:
        service.save(update_fields=["etag", "last_modified_header", "updated_at"])
        service.mark_fetch_error(
            fetch_error or "Fetch failed for unknown reason",
            error_category=_classify_fetch_error(fetch_error),
            duration_ms=duration_ms, worker_id=worker_id,
        )
        return

    content_type = resp_headers.get("Content-Type", "")
    service.content_type = content_type[:200]
    service.page_size_bytes = len(raw_bytes)
    service.response_headers = dict(resp_headers)
    service.http_status_code = 200  # resilient_get doesn't surface the real status code yet (see its docstring)
    service.proxy_used = ((PROXIES or {}).get("https") or (PROXIES or {}).get("http") or None)

    # A 304 surfaces from resilient_get as an ordinary empty-but-
    # successful body (it doesn't expose status codes at all — see
    # resilient_fetch.py) — infer "not modified" from an empty body on
    # a conditional request rather than treating it as "the page is now
    # empty", which would otherwise wipe out everything already stored.
    not_modified = (raw_bytes == b"") and bool(conditional_headers)
    if not_modified:
        service.save(update_fields=[
            "etag", "last_modified_header", "content_type", "page_size_bytes",
            "response_headers", "http_status_code", "proxy_used", "updated_at",
        ])
        service.mark_fetch_success(
            content_hash=service.content_hash or None, http_status_code=304,
            duration_ms=duration_ms, not_modified=True, worker_id=worker_id,
        )
        return

    if "text/html" not in content_type and "application/xhtml" not in content_type:
        _apply_non_html_result_to_service(service, raw_bytes, resp_headers)
        service.save(update_fields=[
            "etag", "last_modified_header", "content_type", "page_size_bytes",
            "response_headers", "http_status_code", "proxy_used",
            "extracted_text", "word_count", "reading_time_minutes", "content_hash",
            "last_fetched_data", "updated_at",
        ])
        service.mark_fetch_success(
            content_hash=service.content_hash or None, http_status_code=200,
            duration_ms=duration_ms, worker_id=worker_id,
        )
        return

    encoding = _guess_encoding(resp_headers)
    try:
        html_text = raw_bytes.decode(encoding or "utf-8", errors="replace")
    except Exception:
        html_text = raw_bytes.decode("utf-8", errors="replace")

    base_url = final_url or service.service_url
    extraction = _run_deep_extraction(html_text, base_url, resp_headers)

    # ── Advanced multi-page crawl ───────────────────────────────────────
    # Root page is fully extracted above; if this service opted into
    # link-following (max_crawl_depth > 0), walk its same-domain links
    # now and fold their images/videos/links/text into `extraction`
    # before it's persisted. A failure here is logged and swallowed —
    # the root page's own successful extraction must never be lost just
    # because the *additional* crawl hit a problem.
    if service.max_crawl_depth and DEEP_CRAWL_ENABLED:
        subpage_headers = {"User-Agent": request_user_agent, "Accept-Language": "en-US,en;q=0.9"}
        if service.auth_token:
            subpage_headers["Authorization"] = f"Bearer {service.auth_token}"
        if service.api_key:
            subpage_headers["X-Api-Key"] = service.api_key
        # Deliberately NOT including conditional_headers here — those
        # ETag/If-Modified-Since values belong to service.service_url
        # specifically and would be meaningless (or wrong) applied to a
        # different URL on the same domain.
        try:
            crawl_result = _run_multi_page_crawl(service, extraction, base_url, subpage_headers)
        except Exception:
            logger.exception("deep_crawl failed for service_id=%s — keeping root-page-only result", service.pk)
            crawl_result = None

        if crawl_result:
            extraction["images"] = crawl_result["images"]
            extraction["videos"] = crawl_result["videos"]
            extraction["links"] = crawl_result["links"]
            if crawl_result["text"]:
                extraction["text"] = crawl_result["text"]
                extraction["word_count"] = crawl_result["word_count"]
                extraction["reading_time_minutes"] = crawl_result["reading_time_minutes"]
            extraction["deep_crawl_pages"] = crawl_result["pages_report"]
            if crawl_result.get("warnings"):
                extraction["warnings"] = list(extraction["warnings"]) + list(crawl_result["warnings"])
            logger.info(
                "deep_crawl: service_id=%s fetched %d additional page(s) (max_crawl_depth=%s)",
                service.pk, crawl_result.get("pages_fetched", 0), service.max_crawl_depth,
            )

    # ── Source-URL-as-video fallback ────────────────────────────────────
    # Runs last, after both the root page's own extraction and any
    # multi-page crawl have had a chance to supply a real scraped
    # video. Only fills extracted_videos if it's still empty at this
    # point — see _maybe_use_source_url_as_video's docstring for why
    # the service URL is safe to use here but get_video_info()'s
    # 'unknown' branch is deliberately excluded.
    _maybe_use_source_url_as_video(extraction, base_url)

    content_hash = _hash_extracted_text(extraction["text"])

    update_fields = _apply_deep_extraction_to_service(service, extraction)
    service.content_hash = content_hash
    update_fields = list(set(update_fields + [
        "content_hash", "content_type", "page_size_bytes", "response_headers",
        "http_status_code", "proxy_used", "etag", "last_modified_header", "updated_at",
    ]))
    service.save(update_fields=update_fields)

    if extraction["warnings"]:
        # mark_fetch_success() always clears fetch_error as part of its
        # own bookkeeping save (it's meant for hard failures, not
        # per-field notes), so non-fatal extraction warnings are logged
        # here and kept in last_fetched_data['warnings'] instead of
        # relying on a field mark_fetch_success is about to overwrite.
        logger.info(
            "deep_scrape: service_id=%s succeeded with %d non-fatal warning(s): %s",
            service.pk, len(extraction["warnings"]), "; ".join(extraction["warnings"])[:2000],
        )

    service.mark_fetch_success(
        content_hash=content_hash or None, http_status_code=200,
        duration_ms=duration_ms, worker_id=worker_id,
    )


def _execute_deep_scrape(service: ConnectedService, worker_id: str) -> None:
    """
    Precondition: caller has already run _prepare_service_for_fetch()
    successfully — the row is LOCKED and owned by this worker.

    Never raises for ordinary fetch/extraction failures — those are
    always resolved into a proper mark_fetch_error() call inside
    _run_deep_scrape_unsafe. Only an unexpected/programming-error
    exception escapes, and even then the lock is released first so the
    row can never get stuck at fetch_status=LOCKED — the same contract
    the old scrape_url()/fetch_service_data() functions documented for
    themselves.
    """
    started_at = timezone.now()
    try:
        _run_deep_scrape_unsafe(service, worker_id, started_at)
    except Exception:
        service.release_lock()
        raise


# ═══════════════════════════════════════════════════════════════════════════
# Serializers
# ═══════════════════════════════════════════════════════════════════════════

class ConnectedServiceSerializer(serializers.ModelSerializer):
    """Read serializer — all fields including scraped data."""

    class Meta:
        model = ConnectedService
        fields = [
            "id", "service_name", "service_url", "service_type",
            "status", "is_connected", "profile", "max_crawl_depth",
            "og_title", "og_description", "og_thumbnail", "og_site_name", "og_type",
            "og_locale", "og_video", "og_audio",
            "twitter_card", "twitter_title", "twitter_description", "twitter_image",
            "twitter_site", "twitter_creator",
            "page_title", "meta_description", "charset", "content_language", "viewport",
            "canonical_url", "favicon", "apple_touch_icon", "author", "author_url",
            "published_time", "modified_time", "article_section", "article_tags",
            "category_breadcrumb", "structured_data", "same_as_links",
            "price_amount", "price_currency", "availability", "product_sku",
            "brand_name", "seller_name", "rating_value", "rating_count",
            "robots_meta", "is_indexable",
            "extracted_images", "extracted_videos", "extracted_links",
            "extracted_headings", "extracted_text", "word_count", "reading_time_minutes",
            "fetch_status", "fetch_error", "circuit_breaker_open", "last_fetch_time",
            "created_at", "updated_at",
        ]
        # circuit_breaker_open is included so a card's "paused" badge
        # (engine_profile.html's buildEngineCard) survives a DRF
        # /fetch//refresh/ response, not just the initial server-rendered
        # page load (which uses _serialize_service, a different dict that
        # already carried this field). max_crawl_depth is included
        # read-only so the dashboard can show "crawled N pages deep"
        # without a second request — it's set at creation time via
        # ConnectedServiceWriteSerializer, not mutated by a fetch.
        read_only_fields = [
            "profile",
            "og_title", "og_description", "og_thumbnail", "og_site_name", "og_type",
            "og_locale", "og_video", "og_audio",
            "twitter_card", "twitter_title", "twitter_description", "twitter_image",
            "twitter_site", "twitter_creator",
            "page_title", "meta_description", "charset", "content_language", "viewport",
            "canonical_url", "favicon", "apple_touch_icon", "author", "author_url",
            "published_time", "modified_time", "article_section", "article_tags",
            "category_breadcrumb", "structured_data", "same_as_links",
            "price_amount", "price_currency", "availability", "product_sku",
            "brand_name", "seller_name", "rating_value", "rating_count",
            "robots_meta", "is_indexable",
            "extracted_images", "extracted_videos", "extracted_links",
            "extracted_headings", "extracted_text", "word_count", "reading_time_minutes",
            "fetch_status", "fetch_error", "circuit_breaker_open", "last_fetch_time",
            "created_at", "updated_at",
        ]


class ConnectedServiceWriteSerializer(serializers.ModelSerializer):
    """
    Write serializer — only user-editable fields.

    `profile` is deliberately NOT writable here. ProfileInfo is a
    strict one-to-one with User (OneToOneField(primary_key=True)), so
    there is never a legitimate case where a user should be able to
    set a ConnectedService's `profile` to anything other than their
    own — accepting it as client input would let any authenticated
    user attribute their posted content to someone else's profile
    (spoofed name/avatar/tagline on feed cards). The viewset sets
    `profile` itself from request.user in perform_create/perform_update.

    `max_crawl_depth` is writable and validated below — it directly
    controls how many additional pages a fetch is willing to visit
    (see _run_multi_page_crawl), so it gets the same clamp-to-a-hard-
    ceiling treatment every other user-facing crawl knob in this module
    gets (MAX_BATCH_REFRESH, MAX_ACTIVE_SERVICES_PER_USER, etc.) rather
    than trusting client input directly.
    """

    class Meta:
        model = ConnectedService
        fields = [
            "service_name", "service_url", "service_type",
            "status", "is_connected", "api_key", "auth_token",
            "max_crawl_depth",
        ]

    def validate_max_crawl_depth(self, value):
        if value is None:
            return value
        if value < 0:
            raise serializers.ValidationError("max_crawl_depth cannot be negative.")
        if value > DEEP_CRAWL_MAX_DEPTH_HARD_CAP:
            raise serializers.ValidationError(
                f"max_crawl_depth cannot exceed {DEEP_CRAWL_MAX_DEPTH_HARD_CAP}."
            )
        return value


class ConnectedServicePagination(PageNumberPagination):
    """Explicit, capped pagination for the DRF viewset — without this,
    ModelViewSet.list() returns every one of a user's (soft-delete-
    filtered) services in a single response."""
    page_size = 25
    page_size_query_param = "page_size"
    max_page_size = 100


# ═══════════════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════════════

def _get_request_profile(user):
    """
    Fetch user.profileinfo defensively. The reverse one-to-one
    accessor raises ProfileInfo.DoesNotExist (not AttributeError) when
    absent, so a plain getattr(..., default) won't catch it — this
    exists so every call site doesn't need its own try/except.
    """
    try:
        return user.profileinfo
    except ProfileInfo.DoesNotExist:
        return None


def _hash_extracted_text(text: str) -> str:
    """Same algorithm as every other producer of content_hash
    (scraper.py, connect_url.py) — keeps content_version/content_hash
    comparable regardless of which code path populated a row."""
    if not text:
        return ""
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()


def _normalize_videos_for_display(raw_videos, limit: int = None) -> list:
    """
    Mirrors megamind.utils.feed_cache._normalize_videos — kept in sync
    on purpose so a video looks and plays identically on the profile
    page as it does in the public engine feed, regardless of which
    producer wrote extracted_videos.

    Accepts scraper dicts like {"url": ..., "type": "video/*"|"embed"},
    bare URL strings, or already-normalized video_info dicts — anything
    get_video_info_cached() can resolve a URL out of.
    """
    normalized = []
    for v in raw_videos or []:
        if isinstance(v, dict):
            url = v.get('url') or v.get('embed_url') or v.get('src')
        else:
            url = v
        if not url:
            continue
        info = get_video_info_cached(url)
        if info:
            normalized.append(info)
        if limit and len(normalized) >= limit:
            break
    return normalized


def _serialize_service(service: ConnectedService) -> dict:
    """
    Serialize a service to a dict.
    Reads new flat fields first, falls back to last_fetched_data for
    backwards compatibility.

    Images/videos are run through the same shared normalizers used by
    the public engine feed (megamind.utils.media_info,
    megamind.utils.video_info) instead of ad-hoc per-view coercion, so
    this page's media renders identically to the feed regardless of
    which producer wrote the raw extracted_* fields, and videos come
    back embed-ready (platform/embed_url/watch_url/thumbnail/type)
    rather than the raw {"url", "type"} shape. Links go through
    megamind.utils.link_info.prepare_links_for_display instead —
    display-layer sanitization (HTML-escaped text, dangerous schemes
    dropped) plus domain/favicon/is_external, since these are hrefs
    scraped from an untrusted third-party page, not first-party data.
    """
    og_title       = service.og_title       or ""
    og_description = service.og_description or ""
    og_thumbnail   = service.og_thumbnail   or ""
    og_site_name   = service.og_site_name   or ""
    og_type        = service.og_type        or ""
    text           = service.extracted_text or ""

    # Page-metadata fields — flat fields first, same fallback-to-raw-dump
    # pattern as the OG fields above, so older rows saved before these
    # columns existed still render something on the profile page.
    canonical_url   = service.canonical_url   or ""
    favicon         = service.favicon         or ""
    author          = service.author          or ""
    published_time  = service.published_time  or ""
    structured_data = service.structured_data or []

    raw = service.last_fetched_data or {}
    if raw and not og_title:
        og_title       = raw.get("title")       or raw.get("og_title")       or ""
        og_description = raw.get("description") or raw.get("og_description") or ""
        og_thumbnail   = (raw.get("og_image")   or raw.get("og_thumbnail")
                          or raw.get("thumbnail") or "")
        og_site_name   = raw.get("site_name")   or raw.get("og_site_name")   or ""
        og_type        = raw.get("og_type")     or raw.get("type")           or ""

    if raw and not canonical_url:
        canonical_url = raw.get("canonical_url") or ""
    if raw and not favicon:
        favicon = raw.get("favicon") or ""
    if raw and not author:
        author = raw.get("author") or ""
    if raw and not published_time:
        published_time = raw.get("published_time") or ""
    if raw and not structured_data:
        structured_data = raw.get("structured_data") or []

    # Prefer the flat extracted_* fields; fall back to the raw scrape
    # dump only when the flat field is empty (older rows / partial writes).
    images_source = service.extracted_images or (raw.get("images") if raw else []) or []
    videos_source = service.extracted_videos or (raw.get("videos") if raw else []) or []
    links_source  = service.extracted_links  or (raw.get("links")  if raw else []) or []

    if not text and raw:
        text = (raw.get("full_content") or raw.get("text_content")
                or raw.get("content")   or raw.get("text") or "")

    images = normalize_images(images_source)
    videos = _normalize_videos_for_display(videos_source)
    # link_info.prepare_links_for_display, not media_info.normalize_links:
    # every one of these hrefs came from a scraped, untrusted third-party
    # page, so this also HTML-escapes the anchor text and drops anything
    # with a non-displayable scheme (javascript:, data:, vbscript:, ...)
    # outright rather than rendering it as a clickable link — see that
    # function's docstring. It also annotates each link with domain/
    # favicon/is_external/rel so the profile page can render them the
    # same way the link intelligence scan report does (see
    # scan_service_links / ConnectedServiceViewSet.link_scan below).
    links = prepare_links_for_display(links_source, base_domain=service.domain)

    return {
        "id":               service.id,
        "service_name":     service.service_name,
        "service_url":      service.service_url,
        "service_type":     service.service_type,
        "status":           service.status,
        "is_connected":     service.is_connected,
        "og_title":         og_title,
        "og_description":   og_description,
        "og_thumbnail":     og_thumbnail,
        "og_site_name":     og_site_name,
        "og_type":          og_type,
        "twitter": {
            "card":        service.twitter_card or "",
            "title":       service.twitter_title or "",
            "description": service.twitter_description or "",
            "image":       service.twitter_image or "",
        },
        "canonical_url":    canonical_url,
        "favicon":          favicon,
        "author":           author,
        "published_time":   published_time,
        "structured_data":  structured_data,
        "page_title":       service.page_title or "",
        "meta_description": service.meta_description or "",
        "commerce": {
            "price_amount":   str(service.price_amount) if service.price_amount is not None else None,
            "price_currency": service.price_currency,
            "availability":   service.availability,
            "brand_name":     service.brand_name,
            "rating_value":   str(service.rating_value) if service.rating_value is not None else None,
            "rating_count":   service.rating_count,
        },
        "is_indexable":      service.is_indexable,
        "extracted_images": images,
        "extracted_videos": videos,
        "extracted_links":  links,
        "extracted_headings": service.extracted_headings or {},
        "extracted_text":   text,
        "word_count":        service.word_count,
        "reading_time_minutes": service.reading_time_minutes,
        # Deep-crawl transparency: how many hops this service is
        # configured for, and — after at least one fetch — exactly
        # which pages that fetch visited (root page is depth 0).
        # Empty list for a service that has never been fetched yet, or
        # that has max_crawl_depth=0 (single-page only).
        "max_crawl_depth":  service.max_crawl_depth,
        "deep_crawl_pages": (raw.get("deep_crawl_pages") if raw else None) or [],
        "fetch_status":     service.fetch_status,
        "fetch_error":      service.fetch_error,
        "circuit_breaker_open": service.circuit_breaker_open,
        "last_fetch_time":  service.last_fetch_time.isoformat() if service.last_fetch_time else None,
        "created_at":       service.created_at.isoformat() if service.created_at else None,
        "updated_at":       service.updated_at.isoformat() if service.updated_at else None,
    }


def _should_fetch(service: ConnectedService, max_age_seconds: int = 3600) -> bool:
    """
    Stale if never fetched, or older than max_age.
    Back off for 10 min after an error so timeouts don't hammer the same host.

    Also skips scheduling entirely when the row is already LOCKED
    (someone else — scheduled worker or another request — already has
    it) or when its circuit breaker is open (repeated failures already
    told us this domain needs a longer cooldown than the normal error
    backoff gives it; retrying it from every profile-page load would
    just keep tripping the same breaker).
    """
    if service.fetch_status == ConnectedService.FetchStatus.LOCKED:
        return False
    if service.circuit_breaker_open:
        return False
    if service.last_fetch_time is None:
        return True
    age = (timezone.now() - service.last_fetch_time).total_seconds()
    if service.fetch_status == ConnectedService.FetchStatus.ERROR:
        return age > 600   # 10-minute cooldown after errors / timeouts
    return age > max_age_seconds


# ═══════════════════════════════════════════════════════════════════════════
# Link intelligence scan pipeline
# ═══════════════════════════════════════════════════════════════════════════

def _check_link_scan_cooldown(service_id: int) -> bool:
    """Same atomic claim-the-window pattern as
    _check_manual_refresh_cooldown, on its own cache key so a link scan
    and a manual refresh never contend for the same cooldown window."""
    key = f"connected_service:link_scan_cooldown:{service_id}"
    return cache.add(key, 1, timeout=LINK_SCAN_COOLDOWN_SECONDS)


def _clamp_scan_param(value, default: int, hard_cap: int) -> int:
    """Shared clamp used by both the plain-JSON endpoint and the DRF
    action: an absent/unparseable value falls back to the module
    default; anything present is coerced to an int and clamped into
    [1, hard_cap] regardless of what the caller asked for — the same
    'never trust a request past a ceiling' rule every other budget in
    this module follows (MAX_BATCH_REFRESH, MAX_ACTIVE_SERVICES_PER_USER,
    max_crawl_depth, ...)."""
    if value in (None, ""):
        return default
    try:
        value = int(value)
    except (TypeError, ValueError):
        return default
    return max(1, min(value, hard_cap))


def _run_link_scan(
    service: ConnectedService,
    *,
    href: str = None,
    max_links: int = None,
    max_per_domain: int = None,
    max_depth: int = None,
) -> dict:
    """
    Runs intelligence_scraper against this service's links and formats
    the result for the frontend via link_info.prepare_scrape_report_
    for_display().

    When `href` is given, scopes the scan to exactly that one URL — a
    single scrape_links([href], ...) call — instead of the batch scan
    over service.extracted_links. This is what powers the per-link /
    per-image "Deep crawl" button: it fetches and formats precisely the
    URL the user clicked (same page they'd land on via the title link),
    rather than re-running the full link-list scan and hoping the
    clicked URL happens to show up among the results.

    Read-only end to end: neither scrape_links() nor
    scrape_service_links() write anything onto `service` or create any
    new row — the only DB activity is the same DomainCrawlPolicy
    politeness bookkeeping (get_or_create + record_request) every other
    fetch path in this module already performs, so a scan counts
    against a domain's rate-limit window exactly like a primary crawl
    does (see intelligence_scraper.py's module docstring). This wrapper
    itself never calls service.save().

    Every param is re-clamped to its hard cap here too, not just by the
    call sites, so this function is safe to call from anywhere in the
    future without relying on every caller to have remembered to clamp
    first.
    """
    if href:
        domain = service.domain or extract_domain(service.service_url)
        report = scrape_links(
            [href],
            source_url=service.service_url,
            source_domain=domain,
            source_api_key=service.api_key,
            source_auth_token=service.auth_token,
            max_links=1,
            max_per_domain=1,
            max_depth=1,
        )
        return prepare_scrape_report_for_display(report, base_domain=service.domain)

    max_links = min(max_links, LINK_SCAN_MAX_LINKS_HARD_CAP) if max_links else LINK_SCAN_MAX_LINKS
    max_per_domain = (
        min(max_per_domain, LINK_SCAN_MAX_PER_DOMAIN_HARD_CAP) if max_per_domain else LINK_SCAN_MAX_PER_DOMAIN
    )
    max_depth = min(max_depth, LINK_SCAN_MAX_DEPTH_HARD_CAP) if max_depth else LINK_SCAN_MAX_DEPTH

    report = scrape_service_links(
        service,
        max_links=max_links,
        max_per_domain=max_per_domain,
        max_depth=max_depth,
    )
    return prepare_scrape_report_for_display(report, base_domain=service.domain)


# ═══════════════════════════════════════════════════════════════════════════
# Main View
# ═══════════════════════════════════════════════════════════════════════════

@login_required(login_url='/customer/signin/')
@ensure_csrf_cookie
def CrawlEngineView(request):
    """
    Enterprise-level identity view with data validation and comprehensive user data.
    """
    user = request.user
    profile_info = get_object_or_404(ProfileInfo, user=user)

    # ── Security Alerts ───────────────────────────────────────────────────
    security_alerts = []
    if user.failed_login_attempts > 0:
        if user.failed_login_attempts >= 5:
            alert_type = 'danger'
        elif user.failed_login_attempts >= 3:
            alert_type = 'warning'
        else:
            alert_type = 'info'
        security_alerts.append({
            'type': alert_type,
            'icon': 'exclamation-triangle',
            'message': f"{user.failed_login_attempts} failed login attempt(s) detected",
        })

    # ── Display Name & Initials ───────────────────────────────────────────
    if '@' in user.email_or_phone:
        display_name = user.email_or_phone.split('@')[0].replace('.', ' ').replace('_', ' ').title()
    else:
        display_name = user.email_or_phone
    initials = display_name[0].upper() if display_name else 'U'

    # ── Badges ────────────────────────────────────────────────────────────
    badges = []
    badges.sort(key=lambda x: x['priority'])

    # ── 2FA ──────────────────────────────────────────────────────────────
    is_2fa_enabled = getattr(user, 'is_2fa_enabled', False) or request.session.get('2fa_enabled', False)

    # ── User Data ─────────────────────────────────────────────────────────
    user_data = {
        'email_or_phone':   user.email_or_phone,
        'role':             user.get_role_display(),
        'role_raw':         user.role,
        'is_staff':         user.is_staff,
        'is_superuser':     user.is_superuser,
        'is_active':        user.is_active,
        'deleted_at':       user.deleted_at,
        'locked_until':     user.locked_until,
        'groups':           user.groups.all(),
        'user_permissions': user.user_permissions.all(),
    }

    stats = {
        'failed_attempts': user.failed_login_attempts,
        'is_2fa_enabled':  is_2fa_enabled,
    }

    # ── Services ──────────────────────────────────────────────────────────
    # Never fetch inside a view; serve stale (already-serialized) data
    # and trigger a background refresh only when stale.
    #
    # is_active=True: soft-deleted rows (see ConnectedService.soft_delete(),
    # used by delete_service()/perform_destroy() below instead of a hard
    # delete so CrawlAttempt's audit trail survives) must not resurface
    # here — a hard filter, not just an is_connected check, since a
    # deleted-but-still-connected row would otherwise still render.
    services_qs = ConnectedService.objects.filter(user=user, is_active=True).order_by('-created_at')
    connected_services_qs = services_qs.filter(is_connected=True)

    # Paginated instead of loaded/serialized in full — an account with
    # hundreds of connected services previously meant serializing every
    # single one (media normalization included) on every profile-page
    # render, and potentially spawning a background refresh thread per
    # stale row in the same request.
    try:
        page_size = int(request.GET.get('page_size', DEFAULT_SERVICES_PAGE_SIZE))
    except (TypeError, ValueError):
        page_size = DEFAULT_SERVICES_PAGE_SIZE
    page_size = max(1, min(page_size, MAX_SERVICES_PAGE_SIZE))

    paginator = Paginator(services_qs, page_size)
    try:
        page_number = int(request.GET.get('page', 1))
    except (TypeError, ValueError):
        page_number = 1

    try:
        services_page = paginator.page(page_number)
    except EmptyPage:
        services_page = paginator.page(paginator.num_pages) if paginator.num_pages else paginator.page(1)

    services_data = []
    for service in services_page.object_list:
        services_data.append(_serialize_service(service))

        # Schedule a background refresh only if stale — never block the view
        if service.is_connected and _should_fetch(service):
            _spawn_background_refresh(service.pk, user.pk)

    # ── Context ───────────────────────────────────────────────────────────
    context = {
        'user':               user,
        'profile_info':       profile_info,
        'user_data':          user_data,
        'stats':              stats,
        'badges':             badges,
        'security_alerts':    security_alerts,
        'page_title':         'Identity Profile',
        'notification_count': 0,
        'services':           services_page.object_list,
        'connected_services': services_data,
        'total_services':     connected_services_qs.count(),
        'services_page':      services_page.number,
        'services_num_pages': paginator.num_pages,
        'services_page_size': page_size,
        'services_total_count': paginator.count,
    }

    return render(request, "personal/engine_profile.html", context)


# ═══════════════════════════════════════════════════════════════════════════
# Service API Views
# ═══════════════════════════════════════════════════════════════════════════

@login_required(login_url='/customer/signin/')
@require_http_methods(["POST"])
def add_service(request):
    try:
        data = json.loads(request.body)
    except (TypeError, ValueError):
        return JsonResponse({'success': False, 'error': 'Invalid JSON body'}, status=400)

    try:
        service_name = data.get('service_name')
        service_url  = data.get('service_url')
        # 'other' isn't a valid ConnectedService.ServiceType choice —
        # fall back to the model's own default (ServiceType.PRODUCT)
        # instead of silently writing an invalid value that later
        # chokes anything keyed on service_type (e.g.
        # video_info.FALLBACK_VIDEOS pools, feed_cache attribution).
        service_type = data.get('service_type') or ConnectedService.ServiceType.PRODUCT

        if not service_name or not service_url:
            return JsonResponse({'success': False, 'error': 'Service name and URL are required'}, status=400)

        if len(service_name) > 200:
            return JsonResponse({'success': False, 'error': 'Service name is too long (max 200 characters)'}, status=400)

        # NOTE: ConnectedService.ServiceType is the source of truth for
        # valid service_type choices (not a `SERVICE_TYPES` attribute,
        # which doesn't exist on the model).
        valid_types = {choice[0] for choice in ConnectedService.ServiceType.choices}
        if service_type not in valid_types:
            return JsonResponse(
                {'success': False, 'error': f"Invalid service_type '{service_type}'"},
                status=400,
            )

        # Optional advanced-crawl opt-in at creation time — "0" (or
        # omitted) keeps the original single-page-only behavior.
        # Clamped the same way ConnectedServiceWriteSerializer clamps it
        # for the DRF path, so both entry points enforce the same
        # ceiling instead of the JSON API being stricter than this one.
        max_crawl_depth = data.get('max_crawl_depth', 0)
        try:
            max_crawl_depth = int(max_crawl_depth)
        except (TypeError, ValueError):
            return JsonResponse({'success': False, 'error': "'max_crawl_depth' must be an integer"}, status=400)
        if max_crawl_depth < 0 or max_crawl_depth > DEEP_CRAWL_MAX_DEPTH_HARD_CAP:
            return JsonResponse(
                {
                    'success': False,
                    'error': f"'max_crawl_depth' must be between 0 and {DEEP_CRAWL_MAX_DEPTH_HARD_CAP}",
                },
                status=400,
            )

        # Per-user quota — without this, a single account can create an
        # unbounded number of crawl targets, each eligible for scheduled
        # crawling and each serialized on every profile-page load.
        active_count = ConnectedService.objects.filter(user=request.user, is_active=True).count()
        if active_count >= MAX_ACTIVE_SERVICES_PER_USER:
            return JsonResponse(
                {
                    'success': False,
                    'error': (
                        f"You've reached the maximum of {MAX_ACTIVE_SERVICES_PER_USER} "
                        f"connected services. Remove one before adding another."
                    ),
                },
                status=403,
            )

        # Reject an unsafe URL up front with a clear, immediate error
        # instead of creating a row that's doomed to silently fail
        # later — the background/DRF fetch paths would also block it
        # (see _prepare_service_for_fetch), but only after the row
        # already exists and looks like a normal pending service.
        try:
            validate_crawl_url(service_url)
        except UnsafeCrawlURLError as exc:
            return JsonResponse(
                {'success': False, 'error': f"This URL can't be added: {exc}"},
                status=400,
            )

        

        try:
            service = ConnectedService.objects.create(
                user=request.user,
                profile=_get_request_profile(request.user),
                service_name=service_name,
                service_url=service_url,
                service_type=service_type,
                is_connected=False,
                status=ConnectedService.Status.PRIVATE,
                crawl_source=ConnectedService.CrawlSource.MANUAL,
                max_crawl_depth=max_crawl_depth,
            )
        except IntegrityError:
            # uniq_user_service_url — the model already enforces this,
            # surface it as a clear 409 instead of a generic 500.
            return JsonResponse(
                {'success': False, 'error': 'You have already added this URL.'},
                status=409,
            )

        return JsonResponse({
            'success': True,
            'service': {
                'id':              service.id,
                'service_name':    service.service_name,
                'service_url':     service.service_url,
                'service_type':    service.service_type,
                'is_connected':    service.is_connected,
                'status':          service.status,
                'max_crawl_depth': service.max_crawl_depth,
            },
        })
    except Exception:
        logger.exception("add_service failed for user_id=%s", request.user.pk)
        return JsonResponse({'success': False, 'error': 'Could not add this service.'}, status=500)


@login_required(login_url='/customer/signin/')
@require_http_methods(["POST"])
def toggle_service_connection(request, service_id):
    try:
        service = get_object_or_404(ConnectedService, id=service_id, user=request.user, is_active=True)
        service.is_connected = not service.is_connected
        service.save(update_fields=["is_connected", "updated_at"])

        # Never fetch inside a view (same rule CrawlEngineView follows) —
        # connecting a service used to call the fetch pipeline
        # synchronously here, which could block the request for as long
        # as the third-party URL takes to respond (including retries).
        # Hand it to the same background-refresh path instead; the
        # dashboard already polls/re-renders services asynchronously.
        if service.is_connected:
            _spawn_background_refresh(service.pk, request.user.pk)

        return JsonResponse({
            'success':      True,
            'is_connected': service.is_connected,
            'status':       service.status,
        })
    except Exception:
        logger.exception("toggle_service_connection failed for service_id=%s user_id=%s", service_id, request.user.pk)
        return JsonResponse({'success': False, 'error': 'Could not update this service.'}, status=500)


@login_required(login_url='/customer/signin/')
@require_http_methods(["DELETE"])
def delete_service(request, service_id):
    try:
        service = get_object_or_404(ConnectedService, id=service_id, user=request.user, is_active=True)
        # Soft delete, not .delete(): CrawlAttempt has on_delete=CASCADE,
        # so a hard delete here would silently wipe this service's
        # entire audit trail — exactly the history the model's
        # docstring says CrawlAttempt exists to preserve.
        service.soft_delete()
        return JsonResponse({'success': True})
    except Exception:
        logger.exception("delete_service failed for service_id=%s user_id=%s", service_id, request.user.pk)
        return JsonResponse({'success': False, 'error': 'Could not delete this service.'}, status=500)


@login_required(login_url='/customer/signin/')
@require_http_methods(["POST"])
def refresh_service_data(request, service_id):
    """POST engine/api/services/<id>/refresh/ — refresh one service."""
    try:
        service = get_object_or_404(ConnectedService, id=service_id, user=request.user, is_active=True)

        if not service.is_connected:
            return JsonResponse({'success': False, 'error': 'Service is not connected'}, status=400)

        if not _check_manual_refresh_cooldown(service.pk):
            return JsonResponse(
                {
                    'success': False,
                    'error': 'This service was just refreshed — please wait a few seconds before trying again.',
                },
                status=429,
            )

        try:
            _run_guarded_fetch(service, worker_id=f"manual-refresh-user-{request.user.pk}")
        except ServiceRefreshLockedError as exc:
            return JsonResponse({'success': False, 'error': str(exc)}, status=409)
        except (UnsafeCrawlURLError, ServiceRefreshBlockedError) as exc:
            service.refresh_from_db()
            return JsonResponse({'success': False, 'error': str(exc), **_serialize_service(service)}, status=200)

        service.refresh_from_db()
        return JsonResponse({'success': True, **_serialize_service(service)})

    except Exception:
        logger.exception("refresh_service_data failed for service_id=%s user_id=%s", service_id, request.user.pk)
        return JsonResponse({'success': False, 'error': 'Could not refresh this service.'}, status=500)


@login_required(login_url='/customer/signin/')
@require_http_methods(["POST"])
def batch_refresh_services(request):
    """POST engine/api/services/batch-refresh/ — refresh multiple services."""
    try:
        body = json.loads(request.body or '{}')
    except (TypeError, ValueError):
        return JsonResponse({'success': False, 'error': 'Invalid JSON body'}, status=400)

    try:
        service_ids = body.get('service_ids', [])
        if not isinstance(service_ids, list) or not all(isinstance(i, int) for i in service_ids):
            return JsonResponse({'success': False, 'error': "'service_ids' must be a list of integers"}, status=400)

        qs = ConnectedService.objects.filter(user=request.user, is_connected=True, is_active=True)
        if service_ids:
            qs = qs.filter(id__in=service_ids)

        total_matched = qs.count()
        # See MAX_BATCH_REFRESH above — ordering by crawl_priority first
        # means if a request has to be truncated, the services the user/
        # system considers most important are the ones actually refreshed.
        qs = qs.order_by('crawl_priority', '-updated_at')[:MAX_BATCH_REFRESH]

        results = []
        locked_or_blocked = 0
        for service in qs:
            try:
                _run_guarded_fetch(service, worker_id=f"batch-refresh-user-{request.user.pk}")
            except (ServiceRefreshLockedError, ServiceRefreshBlockedError, UnsafeCrawlURLError):
                locked_or_blocked += 1
                service.refresh_from_db()
                results.append(_serialize_service(service))
                continue
            service.refresh_from_db()
            results.append(_serialize_service(service))

        return JsonResponse({
            'success':          True,
            'results':          results,
            'total':            len(results),
            'total_matched':    total_matched,
            'skipped':          max(0, total_matched - len(results)),
            'locked_or_blocked': locked_or_blocked,
            'successful':       sum(1 for r in results if r['fetch_status'] == 'success'),
        })

    except Exception:
        logger.exception("batch_refresh_services failed for user_id=%s", request.user.pk)
        return JsonResponse({'success': False, 'error': 'Batch refresh failed.'}, status=500)


@login_required(login_url='/customer/signin/')
def get_service_media(request, service_id):
    """GET engine/api/services/<id>/media/ — return cached scraped media."""
    try:
        service = get_object_or_404(ConnectedService, id=service_id, user=request.user, is_active=True)

        if service.fetch_status != 'success':
            return JsonResponse({'success': False, 'error': 'No data available'}, status=404)

        return JsonResponse({'success': True, **_serialize_service(service)})

    except Exception:
        logger.exception("get_service_media failed for service_id=%s user_id=%s", service_id, request.user.pk)
        return JsonResponse({'success': False, 'error': 'Could not load media for this service.'}, status=500)


@login_required(login_url='/customer/signin/')
@require_http_methods(["POST"])
def scan_service_links(request, service_id):
    """
    POST engine/api/services/<id>/link-scan/ — on-demand "link
    intelligence" report.

    Scrapes the links this service's last fetch already discovered
    (service.extracted_links) via
    megamind.services.intelligence_scraper.scrape_service_links() and
    returns a display-ready report — title/description/favicon/domain/
    is_external per successfully-scraped link, plus human-readable
    skipped/error reasons — via
    megamind.utils.link_info.prepare_scrape_report_for_display().

    Nothing here is persisted onto the service; see _run_link_scan's
    docstring. Runs synchronously in-request (same as a manual refresh),
    bounded by LINK_SCAN_MAX_LINKS/_MAX_PER_DOMAIN/_MAX_DEPTH so the
    request can't balloon into an unbounded fetch batch, and gated by
    its own cooldown window so rapid double-clicks can't stack up
    redundant outbound fetch batches against the same service.

    Optional JSON body: {"max_links": int, "max_per_domain": int,
    "max_depth": int} — each still clamped to its hard cap regardless
    of what's requested (see _clamp_scan_param).
    """
    try:
        service = get_object_or_404(ConnectedService, id=service_id, user=request.user, is_active=True)

        if not service.extracted_links:
            return JsonResponse(
                {'success': False, 'error': 'No links to scan yet — refresh this service first.'},
                status=400,
            )

        if not _check_link_scan_cooldown(service.pk):
            return JsonResponse(
                {
                    'success': False,
                    'error': 'A link scan for this service was just run — please wait before trying again.',
                },
                status=429,
            )

        try:
            body = json.loads(request.body or '{}')
        except (TypeError, ValueError):
            body = {}

        href = (body.get('href') or '').strip() or None

        if not href and not service.extracted_links:
            return JsonResponse(
                {'success': False, 'error': 'No links to scan yet — refresh this service first.'},
                status=400,
            )

        if not _check_link_scan_cooldown(service.pk):
            return JsonResponse(
                {
                    'success': False,
                    'error': 'A link scan for this service was just run — please wait before trying again.',
                },
                status=429,
            )

        display_report = _run_link_scan(
            service,
            href=href,
            max_links=_clamp_scan_param(body.get('max_links'), LINK_SCAN_MAX_LINKS, LINK_SCAN_MAX_LINKS_HARD_CAP),
            max_per_domain=_clamp_scan_param(
                body.get('max_per_domain'), LINK_SCAN_MAX_PER_DOMAIN, LINK_SCAN_MAX_PER_DOMAIN_HARD_CAP,
            ),
            max_depth=_clamp_scan_param(body.get('max_depth'), LINK_SCAN_MAX_DEPTH, LINK_SCAN_MAX_DEPTH_HARD_CAP),
        )
        return JsonResponse({'success': True, 'service_id': service.id, **display_report})

    except Exception:
        logger.exception("scan_service_links failed for service_id=%s user_id=%s", service_id, request.user.pk)
        return JsonResponse({'success': False, 'error': 'Could not scan links for this service.'}, status=500)


# ═══════════════════════════════════════════════════════════════════════════
# DRF ViewSet  (mounted via megamind/urls.py → /api/connected-services/)
# ═══════════════════════════════════════════════════════════════════════════

class ConnectedServiceFetchThrottle(UserRateThrottle):
    """
    Scoped throttle for the expensive fetch/refresh actions specifically
    (list/retrieve/create/etc. use DRF's default throttle, if any).

    Reads its rate from
    settings.REST_FRAMEWORK['DEFAULT_THROTTLE_RATES']['connected_service_fetch']
    when configured (e.g. '20/min'). IMPORTANT: DRF does NOT silently
    skip throttling when a scope's rate is missing from that setting —
    SimpleRateThrottle.get_rate() raises ImproperlyConfigured, which is
    uncaught and turns into a 500 on every single request through this
    action. get_rate() is overridden below specifically to avoid that:
    it falls back to a safe built-in default so this class can never
    500 a deployment that hasn't added the setting yet. Add the setting
    to override the default; don't rely on the fallback long-term.
    """
    scope = "connected_service_fetch"
    DEFAULT_RATE_IF_UNCONFIGURED = "20/min"

    def get_rate(self):
        try:
            return super().get_rate()
        except ImproperlyConfigured:
            return self.DEFAULT_RATE_IF_UNCONFIGURED


class ConnectedServiceViewSet(ModelViewSet):
    """
    GET    /api/connected-services/              → list (paginated)
    POST   /api/connected-services/               → create
    GET    /api/connected-services/{id}/         → retrieve
    PUT    /api/connected-services/{id}/         → update
    PATCH  /api/connected-services/{id}/         → partial_update
    DELETE /api/connected-services/{id}/         → destroy
    POST   /api/connected-services/{id}/fetch/   → scrape & persist (throttled)
    POST   /api/connected-services/{id}/refresh/ → alias of fetch/ (throttled)
    GET    /api/connected-services/{id}/preview/ → cached data only
    """
    permission_classes = [IsAuthenticated]
    pagination_class = ConnectedServicePagination

    def get_queryset(self):
        # is_active=True: without this, a soft-deleted row (see
        # perform_destroy() below) stays reachable by id forever —
        # retrievable, re-fetchable, even re-activatable via PATCH.
        return ConnectedService.objects.filter(user=self.request.user, is_active=True).order_by("-created_at")

    def get_serializer_class(self):
        if self.action in ("create", "update", "partial_update"):
            return ConnectedServiceWriteSerializer
        return ConnectedServiceSerializer

    def perform_create(self, serializer):
        # `profile` is intentionally not part of ConnectedServiceWriteSerializer
        # (see its docstring) — set here from the authenticated user's own
        # profile instead of trusting client input, since ProfileInfo is a
        # strict one-to-one with User and there's no legitimate case where
        # a different profile should end up here.
        active_count = ConnectedService.objects.filter(user=self.request.user, is_active=True).count()
        if active_count >= MAX_ACTIVE_SERVICES_PER_USER:
            raise serializers.ValidationError(
                f"You've reached the maximum of {MAX_ACTIVE_SERVICES_PER_USER} connected services."
            )
        serializer.save(user=self.request.user, profile=_get_request_profile(self.request.user))

    def perform_update(self, serializer):
        # Re-pin `profile` to the current user's own profile on every
        # update too, in case a row was ever created before this
        # protection existed and somehow carries a stale/mismatched value.
        serializer.save(profile=_get_request_profile(self.request.user))

    def perform_destroy(self, instance):
        # Soft delete, not instance.delete() (ModelViewSet's default):
        # same CrawlAttempt-cascade concern as delete_service() above —
        # a hard delete here would silently erase this service's audit
        # trail. get_queryset()'s is_active=True filter is what makes
        # this row disappear from every future list/retrieve.
        instance.soft_delete()

    def _do_scrape(self, service: ConnectedService):
        """
        Scrapes service.service_url and persists the result through the
        same _run_guarded_fetch -> _execute_deep_scrape pipeline every
        other fetch entry point in this module uses — manual refresh,
        batch refresh, background refresh, and this DRF action all now
        share one implementation, one gate (SSRF/domain-policy/
        robots.txt/locking), one field-complete extraction, and (when
        service.max_crawl_depth > 0) the same bounded multi-page crawl,
        instead of this method hand-rolling its own scrape_url() call
        and field assignment (which is what caused the persistence bug
        described in _apply_deep_extraction_to_service's docstring in
        the first place: og_title/canonical_url/extracted_*/
        last_fetched_data set as plain attributes and left for
        mark_fetch_success()'s own narrow save(update_fields=[...]) to
        persist — a set of columns that update_fields list never
        actually included).
        """
        logger.info("Scraping service_id=%s url=%s", service.id, service.service_url)

        try:
            _run_guarded_fetch(service, worker_id=f"drf-fetch-user-{self.request.user.pk}")
        except UnsafeCrawlURLError as exc:
            service.refresh_from_db()
            return False, Response(
                {
                    "success": False,
                    "fetch_status": "error",
                    "fetch_error": str(exc),
                    "detail": "This URL is not safe to fetch and has been blocked.",
                },
                status=status.HTTP_200_OK,
            )
        except ServiceRefreshBlockedError as exc:
            service.refresh_from_db()
            return False, Response(
                {"success": False, "fetch_status": "error", "fetch_error": str(exc), "detail": str(exc)},
                status=status.HTTP_200_OK,
            )
        except ServiceRefreshLockedError as exc:
            return False, Response(
                {"success": False, "detail": str(exc)},
                status=status.HTTP_409_CONFLICT,
            )

        service.refresh_from_db()

        if service.fetch_status == ConnectedService.FetchStatus.ERROR:
            logger.warning(
                "Scrape failed for service_id=%s url=%s error=%s",
                service.id, service.service_url, service.fetch_error,
            )
            return False, Response(
                {
                    "success":      False,
                    "fetch_status": "error",
                    "fetch_error":  service.fetch_error,
                    "detail":       "Could not fetch data from this URL. The site may block automated access.",
                },
                status=status.HTTP_200_OK,
            )

        return True, Response(
            {"success": True, **ConnectedServiceSerializer(service).data},
            status=status.HTTP_200_OK,
        )

    @action(detail=True, methods=["post"], url_path="fetch", throttle_classes=[ConnectedServiceFetchThrottle])
    def fetch(self, request, pk=None):
        """Scrape service_url and persist all extracted fields."""
        _, response = self._do_scrape(self.get_object())
        return response

    @action(
        detail=True,
        methods=["post"],
        url_path="refresh",
        throttle_classes=[ConnectedServiceFetchThrottle],
    )
    def refresh(self, request, pk=None):
        """
        Dashboard/Home refresh.

        Uses the same /refresh/ URL for both engine_profile.html and
        home.html.

        Normal ViewSet operations remain user-scoped through get_queryset().
        For refresh only, allow a logged-in viewer to refresh a service that
        is publicly visible in the GLOBAL Home feed.

        The actual scrape still goes through _do_scrape(), so the existing
        guarded fetch pipeline, locking, throttling and bookkeeping remain
        unchanged.
        """
        try:
            service = self.get_queryset().get(pk=pk)
        except ConnectedService.DoesNotExist:
            service = get_object_or_404(
                ConnectedService,
                pk=pk,
                is_active=True,
                is_connected=True,
                status="public",
            )

        _, response = self._do_scrape(service)
        return response

    @action(detail=True, methods=["get"], url_path="preview")
    def preview(self, request, pk=None):
        """
        Return cached scraped content for the service.

        Owner services use the normal user-scoped queryset.
        Public GLOBAL-feed services can also be viewed by other
        authenticated users.
        """
        try:
            service = self.get_queryset().get(pk=pk)
        except ConnectedService.DoesNotExist:
            service = get_object_or_404(
                ConnectedService,
                pk=pk,
                is_active=True,
                is_connected=True,
                status="public",
            )

        if service.fetch_status != "success":
            return Response(
                {
                    "detail": "No data fetched yet. POST to /fetch/ first.",
                    "fetch_status": service.fetch_status,
                },
                status=status.HTTP_404_NOT_FOUND,
            )

        raw = service.last_fetched_data or {}

        return Response(
            {
                "id": service.id,
                "service_name": service.service_name,
                "service_url": service.service_url,
                "last_fetch_time": service.last_fetch_time,
                "og": {
                    "title": service.og_title,
                    "description": service.og_description,
                    "thumbnail": service.og_thumbnail,
                    "site_name": service.og_site_name,
                    "type": service.og_type,
                },
                "twitter": {
                    "card": service.twitter_card,
                    "title": service.twitter_title,
                    "description": service.twitter_description,
                    "image": service.twitter_image,
                },
                "canonical_url": service.canonical_url,
                "favicon": service.favicon,
                "author": service.author,
                "published_time": service.published_time,
                "structured_data": service.structured_data,
                "commerce": {
                    "price_amount": service.price_amount,
                    "price_currency": service.price_currency,
                    "availability": service.availability,
                    "brand_name": service.brand_name,
                    "rating_value": service.rating_value,
                    "rating_count": service.rating_count,
                },
                "robots_meta": service.robots_meta,
                "is_indexable": service.is_indexable,
                "images": service.extracted_images,
                "videos": service.extracted_videos,
                "links": prepare_links_for_display(
                    service.extracted_links,
                    base_domain=service.domain,
                ),
                "headings": service.extracted_headings,
                "text": service.extracted_text,
                "word_count": service.word_count,
                "reading_time_minutes": service.reading_time_minutes,
                "max_crawl_depth": service.max_crawl_depth,
                "deep_crawl_pages": raw.get("deep_crawl_pages") or [],
            })

    @action(detail=True, methods=["post"], url_path="link-scan", throttle_classes=[ConnectedServiceFetchThrottle])
    def link_scan(self, request, pk=None):
        try:
            service = self.get_queryset().get(pk=pk)
        except ConnectedService.DoesNotExist:
            service = get_object_or_404(ConnectedService, pk=pk, is_active=True, is_connected=True, status="public")

        href = (request.data.get("href") or "").strip() or None

        if not href and not service.extracted_links:
            return Response(
                {"success": False, "detail": "No links to scan yet — fetch this service first."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            display_report = _run_link_scan(
                service,
                href=href,
                max_links=_clamp_scan_param(
                    request.data.get("max_links"), LINK_SCAN_MAX_LINKS, LINK_SCAN_MAX_LINKS_HARD_CAP,
                ),
                max_per_domain=_clamp_scan_param(
                    request.data.get("max_per_domain"), LINK_SCAN_MAX_PER_DOMAIN, LINK_SCAN_MAX_PER_DOMAIN_HARD_CAP,
                ),
                max_depth=_clamp_scan_param(
                    request.data.get("max_depth"), LINK_SCAN_MAX_DEPTH, LINK_SCAN_MAX_DEPTH_HARD_CAP,
                ),
            )
        except Exception:
            logger.exception("link_scan action failed for service_id=%s", service.pk)
            return Response(
                {"success": False, "detail": "Could not scan links for this service."},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

        return Response({"success": True, **display_report}, status=status.HTTP_200_OK)


        