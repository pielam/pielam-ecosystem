"""
apps/customer/services/connect_url.py

Ties service_fetcher.py (the scraper) to the ConnectedService model
(the storage). This is the function you actually call when a user submits
a URL and you want to scrape + persist everything about it.

Enterprise crawling guards added here, both BEFORE fetch_service_data()
is ever called (so an unsafe/blocked URL never reaches the network):
  1. SSRF validation — a user-submitted URL is untrusted input by
     definition, so this is the one call site in the codebase most in
     need of validate_crawl_url(). A bad URL is turned into a saved row
     with fetch_status='error'/SSRF_BLOCKED, matching the existing
     "never raises, always creates the row" contract — the person still
     sees why it failed instead of getting a 500.
  2. Domain policy — an operator-blocked domain is rejected the same
     way, without ever issuing a request to it.

Both retry_connected_service() and create_connected_service_from_url()
acquire the row's lock with a single atomic UPDATE ... WHERE fetch_status
!= 'locked' before touching the network, and every code path releases it
via mark_fetch_success()/mark_fetch_error() (which already null the lock
fields). This closes two races the previous version only implemented in
its docstring:
  - A brand-new row is immediately visible to due_for_crawl() (its
    next_crawl_at is NULL), so a background worker could grab and fetch
    it in parallel with create_connected_service_from_url()'s own
    synchronous fetch. We now lock it right after creation; if a worker
    won the race first, we back off and return the row as-is rather than
    double-fetching.
  - retry_connected_service() used to only *read* fetch_status before
    fetching — two concurrent retries (or a retry racing a scheduled
    crawl) could both pass that check and clobber each other's write.
    The lock is now acquired with a single conditional UPDATE, which is
    atomic regardless of backend, instead of a check-then-act read.
Any exception raised by fetch_service_data() itself (not just a
fetch_status != 'success' result) is now also caught and persisted as a
CONNECTION_ERROR row, matching the "never raises" contract for both
entry points.
"""

import hashlib
import uuid

from django.utils import timezone

from apps.customer.models.connected_service import (
    ConnectedService,
    CrawlAttempt,
    DomainCrawlPolicy,
    UnsafeCrawlURLError,
    extract_domain,
    validate_crawl_url,
)
from apps.customer.services.service_fetcher import fetch_service_data


class ConnectedServiceLockedError(Exception):
    """Raised by retry_connected_service() when a crawl worker already
    holds the row's lock. Distinct from a silent failure because this
    one IS actionable by the caller (a view can catch it and tell the
    user "a crawl is already running, try again in a moment" instead of
    double-fetching the URL)."""
    pass


def _hash_extracted_text(text: str) -> str:
    """Same algorithm as services.scraper._hash_content — keep
    content_hash comparable regardless of which code path populated it."""
    if not text:
        return ""
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()


def _get_domain_policy_for_url(url: str):
    domain = extract_domain(url)
    if not domain:
        return None
    policy, _ = DomainCrawlPolicy.objects.get_or_create(domain=domain)
    return policy


def _try_acquire_lock(service_id, worker_id: str) -> bool:
    """Atomically claim `service_id` for a synchronous fetch: a single
    conditional UPDATE, so two callers racing this can never both win.
    Returns False if the row is already locked by someone else."""
    updated = ConnectedService.objects.filter(pk=service_id).exclude(
        fetch_status=ConnectedService.FetchStatus.LOCKED
    ).update(
        fetch_status=ConnectedService.FetchStatus.LOCKED,
        lock_id=uuid.uuid4(),
        locked_at=timezone.now(),
        locked_by=worker_id,
    )
    return updated == 1


def _apply_scrape_result(service: ConnectedService, result: dict) -> None:
    service.og_title = result["og_title"]
    service.og_description = result["og_description"]
    service.og_thumbnail = result["og_thumbnail"]
    service.og_site_name = result["og_site_name"]
    service.og_type = result["og_type"]
    service.canonical_url = result["canonical_url"]
    service.favicon = result["favicon"]
    service.author = result["author"]
    service.published_time = result["published_time"]
    service.structured_data = result["structured_data"]
    service.category_breadcrumb = result["category_breadcrumb"]
    service.extracted_images = result["extracted_images"]
    service.extracted_videos = result["extracted_videos"]
    service.extracted_links = result["extracted_links"]
    service.extracted_text = result["extracted_text"]
    service.last_fetched_data = result["raw"]


def _fetch_and_persist(service: ConnectedService, url: str, policy, *, worker_id: str) -> ConnectedService:
    """Shared tail end of both entry points, once the row is locked and
    past the SSRF/policy checks: run the scrape, and no matter what
    happens (success, a fetch_status='error' result, or the scraper
    itself raising) leave the row in a saved, non-locked, inspectable
    state."""
    # Counted as an attempt against the domain's window even if the
    # request itself blows up, so a flaky domain still gets throttled.
    if policy:
        policy.record_request()

    try:
        result = fetch_service_data(url)
    except Exception as exc:
        service.mark_fetch_error(
            str(exc)[:5000],
            error_category=CrawlAttempt.ErrorCategory.CONNECTION_ERROR,
            worker_id=worker_id,
        )
        return service

    _apply_scrape_result(service, result)

    if result["fetch_status"] == "success":
        service.mark_fetch_success(
            content_hash=_hash_extracted_text(result["extracted_text"]),
            worker_id=worker_id,
        )
    else:
        service.mark_fetch_error(
            result["fetch_error"],
            error_category=CrawlAttempt.ErrorCategory.CONNECTION_ERROR,
            worker_id=worker_id,
        )

    return service


def create_connected_service_from_url(
    *, user, url: str, service_name: str = None,
    service_type: str = ConnectedService.ServiceType.GENERAL,
    status: str = ConnectedService.Status.PRIVATE,
    profile=None, worker_id: str = "manual-create",
) -> ConnectedService:
    """
    Scrapes `url` and creates+saves a ConnectedService row with everything
    extracted from the page. Never raises on scrape failure OR on an
    unsafe/blocked URL — the row is still created, just with
    fetch_status='error' and an appropriate fetch_error_category set,
    so the caller always gets back a persisted, inspectable row.
    """
    service = ConnectedService(
        user=user,
        profile=profile,
        service_name=service_name or url[:200],
        service_url=url,
        service_type=service_type,
        status=status,
        crawl_source=ConnectedService.CrawlSource.MANUAL,
    )
    # save() derives `domain` and links `domain_policy` from service_url;
    # do this before the SSRF/policy checks below so both have `domain`
    # available, and before scraping so a bad row is still persisted
    # rather than lost if something below raises unexpectedly.
    service.save()

    if not _try_acquire_lock(service.pk, worker_id):
        # The row is visible to due_for_crawl() the instant it's saved
        # (next_crawl_at is still NULL), so a background worker can beat
        # us to it. Don't race it — let that fetch stand and hand back
        # the row as it currently is; the caller can poll/refresh.
        service.refresh_from_db()
        return service
    service.fetch_status = ConnectedService.FetchStatus.LOCKED

    try:
        validate_crawl_url(url)
    except UnsafeCrawlURLError as exc:
        service.mark_fetch_error(
            str(exc), error_category=CrawlAttempt.ErrorCategory.SSRF_BLOCKED,
            worker_id=worker_id,
        )
        return service

    policy = service.domain_policy or _get_domain_policy_for_url(url)
    if policy and policy.is_blocked:
        service.mark_fetch_error(
            policy.blocked_reason or "This domain has been blocked from crawling.",
            error_category=CrawlAttempt.ErrorCategory.ROBOTS_BLOCKED,
            worker_id=worker_id,
        )
        return service

    result = _fetch_and_persist(service, url, policy, worker_id=worker_id)

    if not service_name and result.og_title:
        result.service_name = result.og_title[:200]
        result.save(update_fields=['service_name', 'updated_at'])

    return result


def retry_connected_service(service: ConnectedService, *, worker_id: str = "manual-retry") -> ConnectedService:
    """Re-scrapes an existing ConnectedService's URL and updates it in place.

    Raises ConnectedServiceLockedError instead of scraping if a crawl
    worker currently holds this row's lock — fetching in parallel with
    the scheduled worker would mean whichever write lands last silently
    wins, with no indication either fetch was wasted or lost. The lock
    itself is claimed via a single atomic UPDATE so this can't race
    another retry_connected_service() call either.
    """
    if not _try_acquire_lock(service.pk, worker_id):
        raise ConnectedServiceLockedError(
            f"Service {service.pk} is currently locked "
            f"({'by ' + service.locked_by if service.locked_by else 'by another worker'}) "
            f"— a crawl is already in progress."
        )
    service.fetch_status = ConnectedService.FetchStatus.LOCKED

    try:
        validate_crawl_url(service.service_url)
    except UnsafeCrawlURLError as exc:
        service.mark_fetch_error(str(exc), error_category=CrawlAttempt.ErrorCategory.SSRF_BLOCKED,
                                  worker_id=worker_id)
        return service

    policy = service.domain_policy or _get_domain_policy_for_url(service.service_url)
    if policy and policy.is_blocked:
        service.mark_fetch_error(
            policy.blocked_reason or "This domain has been blocked from crawling.",
            error_category=CrawlAttempt.ErrorCategory.ROBOTS_BLOCKED,
            worker_id=worker_id,
        )
        return service

    return _fetch_and_persist(service, service.service_url, policy, worker_id=worker_id)