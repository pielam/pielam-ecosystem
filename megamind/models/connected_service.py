"""
Enterprise crawling models.

Design goals over the original single-table version:
  - Politeness: crawl-delay is enforced via a token-bucket on
    DomainCrawlPolicy, shared per-domain rather than per-row.
  - Safety: outbound URLs are validated against SSRF (private/link-local/
    loopback/metadata-endpoint IPs) before any fetch is attempted.
  - Reliability: exponential backoff with jitter, a per-service circuit
    breaker, and distributed-worker locking so two workers can't crawl the
    same row concurrently.
  - Auditability: every fetch attempt is logged to CrawlAttempt instead of
    being clobbered by the next save() on ConnectedService.
  - Freshness without re-downloading: ETag / Last-Modified are stored so
    conditional GETs (304 Not Modified) are possible.
  - Secrets: Fernet key rotation via MultiFernet, with the encrypting key
    id stored alongside the ciphertext so old rows keep decrypting after
    rotation, and column sizing that accounts for ciphertext expansion.

None of this file makes network calls itself — it defines the schema and
the small amount of pure-Python policy logic (backoff math, SSRF checks,
locking) that a crawl worker (Celery task, management command, etc.) is
expected to call into.

NOTE: robots.txt support has been intentionally removed from this schema
(no robots_txt cache, no disallow_all flag, no respect_robots_txt toggle,
no ROBOTS_BLOCKED error category). Crawl politeness is now expressed only
through DomainCrawlPolicy's own crawl_delay_seconds / rate-limit window
and the operator-level is_blocked kill switch.
"""

import ipaddress
import random
import socket
import uuid
from urllib.parse import urlparse

from cryptography.fernet import Fernet, InvalidToken, MultiFernet
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured, ValidationError
from django.db import models, transaction
from django.utils import timezone

from apps.customer.models.account import User
from apps.customer.models.profile_info import ProfileInfo


# ---------------------------------------------------------------------------
# Encrypted field support (key-rotation capable)
#
# Setup:
#   1. Generate a key:
#        python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
#   2. Put it FIRST in a list in settings:
#        FIELD_ENCRYPTION_KEYS = [env("FIELD_ENCRYPTION_KEY_2"), env("FIELD_ENCRYPTION_KEY_1")]
#      MultiFernet encrypts with the first key and can decrypt with any key
#      in the list, so rotation is: generate a new key, prepend it, deploy,
#      then (optionally, later) run a re-encryption pass and drop the old
#      key. Never remove a key that any existing row was encrypted with
#      until every row has been re-encrypted.
# ---------------------------------------------------------------------------

def _get_multi_fernet() -> MultiFernet:
    keys = getattr(settings, "FIELD_ENCRYPTION_KEYS", None)
    if not keys:
        # Back-compat with a single-key setting name.
        single = getattr(settings, "FIELD_ENCRYPTION_KEY", None)
        keys = [single] if single else None
    if not keys:
        raise ImproperlyConfigured(
            "FIELD_ENCRYPTION_KEYS is not set. Generate one with "
            "`Fernet.generate_key()` and add it to your settings/env."
        )
    return MultiFernet([Fernet(k.encode() if isinstance(k, str) else k) for k in keys])


class EncryptedTextFieldMixin:
    """
    Encrypts on write, decrypts on read. Ciphertext is ~1.35x the plaintext
    length plus a fixed ~60-byte overhead (base64 of IV + HMAC + timestamp),
    so callers MUST NOT size max_length to the plaintext length — use
    `encrypted_max_length()` below, or just use EncryptedTextField (no cap).
    """

    def get_prep_value(self, value):
        if value is None or value == "":
            return value
        f = _get_multi_fernet()
        return f.encrypt(str(value).encode()).decode()

    def from_db_value(self, value, expression, connection):
        if value is None or value == "":
            return value
        f = _get_multi_fernet()
        try:
            return f.decrypt(value.encode()).decode()
        except InvalidToken:
            raise ValueError(
                "Could not decrypt field value — none of FIELD_ENCRYPTION_KEYS "
                "matches, or the stored value is corrupted/legacy plaintext."
            )

    def to_python(self, value):
        return value


def encrypted_max_length(plaintext_max_length: int) -> int:
    """Column size needed to store an encrypted value for a given plaintext cap."""
    return int(plaintext_max_length * 1.4) + 100


class EncryptedCharField(EncryptedTextFieldMixin, models.CharField):
    """
    Encrypted CharField.

    max_length represents the PLAINTEXT maximum length.
    The database column is automatically enlarged to accommodate
    Fernet ciphertext expansion.
    """

    def __init__(self, *args, max_length=255, **kwargs):
        self._plaintext_max_length = max_length

        super().__init__(
            *args,
            max_length=encrypted_max_length(max_length),
            **kwargs,
        )

    def deconstruct(self):
        name, path, args, kwargs = super().deconstruct()

        # Django must serialize the plaintext limit, not the
        # internally expanded database column size.
        kwargs["max_length"] = self._plaintext_max_length

        return name, path, args, kwargs

    def clean(self, value, model_instance):
        if value and len(str(value)) > self._plaintext_max_length:
            raise ValidationError(
                f"Ensure this value has at most "
                f"{self._plaintext_max_length} characters "
                f"(it has {len(str(value))})."
            )

        return super().clean(value, model_instance)

class EncryptedTextField(EncryptedTextFieldMixin, models.TextField):
    """Encrypted, unbounded — for OAuth tokens, refresh tokens, long secrets."""
    pass


# ---------------------------------------------------------------------------
# SSRF-safe URL validation
#
# A crawler that fetches arbitrary user-submitted URLs is a classic SSRF
# vector (attacker submits http://169.254.169.254/... or http://localhost:6379
# and your worker fetches it from inside your VPC). Validate both the
# hostname *and* its resolved IP before any request is issued.
# ---------------------------------------------------------------------------

_BLOCKED_NETWORKS = [
    ipaddress.ip_network("127.0.0.0/8"),      # loopback
    ipaddress.ip_network("10.0.0.0/8"),       # RFC1918
    ipaddress.ip_network("172.16.0.0/12"),    # RFC1918
    ipaddress.ip_network("192.168.0.0/16"),   # RFC1918
    ipaddress.ip_network("169.254.0.0/16"),   # link-local / cloud metadata
    ipaddress.ip_network("::1/128"),          # loopback v6
    ipaddress.ip_network("fc00::/7"),         # unique local v6
    ipaddress.ip_network("fe80::/10"),        # link-local v6
]


class UnsafeCrawlURLError(ValueError):
    pass


def validate_crawl_url(url: str) -> None:
    """Raise UnsafeCrawlURLError if `url` should never be fetched by a worker.

    Checks scheme, then resolves the hostname and rejects it if any
    resolved address falls in a private/loopback/link-local range. Callers
    should re-validate at fetch time too (DNS can change between save and
    crawl — "TOCTOU" — so the actual HTTP client should also disable
    redirects-to-private-IPs or re-check on every redirect hop).
    """
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise UnsafeCrawlURLError(f"Unsupported scheme: {parsed.scheme!r}")
    if not parsed.hostname:
        raise UnsafeCrawlURLError("URL has no hostname")
    if parsed.hostname.lower() in ("localhost", "0.0.0.0"):
        raise UnsafeCrawlURLError("Refusing to crawl localhost")

    try:
        infos = socket.getaddrinfo(parsed.hostname, None)
    except socket.gaierror as exc:
        raise UnsafeCrawlURLError(f"DNS resolution failed: {exc}") from exc

    for family, _, _, _, sockaddr in infos:
        ip = ipaddress.ip_address(sockaddr[0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
            raise UnsafeCrawlURLError(f"Resolved to non-routable address {ip}")
        for network in _BLOCKED_NETWORKS:
            if ip in network:
                raise UnsafeCrawlURLError(f"Resolved to blocked network {network}")


def extract_domain(url: str) -> str:
    """Normalize a URL down to a bare, lowercased hostname (no userinfo, no
    port) suitable for grouping under one DomainCrawlPolicy row. The single
    source of truth for this — callers must not re-derive it with their own
    urlparse(...).netloc, since `user:pass@host:8080` and `host` would
    otherwise land in two different policy rows for what's really one site.
    """
    netloc = urlparse(url).netloc
    return netloc.split('@')[-1].split(':')[0].lower() if netloc else ""


# ---------------------------------------------------------------------------
# Per-domain crawl policy (politeness + rate limiting)
#
# One row per domain, shared across every ConnectedService on that domain,
# so crawl-delay / rate limiting is enforced globally rather than per-row
# (which would let N rows on the same domain hammer it in parallel).
# ---------------------------------------------------------------------------

class DomainCrawlPolicy(models.Model):
    domain = models.CharField(max_length=255, unique=True, db_index=True)

    # politeness
    crawl_delay_seconds = models.FloatField(default=1.0)
    max_concurrent_requests = models.PositiveIntegerField(default=2)
    user_agent_override = models.CharField(max_length=255, blank=True, null=True)

    # simple token-bucket state for rate limiting
    requests_in_current_window = models.PositiveIntegerField(default=0)
    window_started_at = models.DateTimeField(null=True, blank=True)
    window_seconds = models.PositiveIntegerField(default=60)
    max_requests_per_window = models.PositiveIntegerField(default=30)

    # operator-level kill switch
    is_blocked = models.BooleanField(
        default=False,
        help_text="Manual block, e.g. domain asked to be excluded or is abusive to scrape."
    )
    blocked_reason = models.CharField(max_length=255, blank=True, null=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [models.Index(fields=['domain'])]

    def __str__(self):
        return self.domain

    def is_request_allowed_now(self) -> bool:
        """Cheap in-process token-bucket check. Not a substitute for a real
        distributed rate limiter (e.g. Redis) under high worker concurrency,
        but fine for a single-scheduler setup."""
        if self.is_blocked:
            return False
        now = timezone.now()
        if not self.window_started_at or (now - self.window_started_at).total_seconds() > self.window_seconds:
            return True  # window has rolled over
        return self.requests_in_current_window < self.max_requests_per_window

    def record_request(self, *, save=True):
        now = timezone.now()
        window_expired = (
            not self.window_started_at
            or (now - self.window_started_at).total_seconds() > self.window_seconds
        )
        if window_expired:
            self.window_started_at = now
            self.requests_in_current_window = 1
        else:
            self.requests_in_current_window = models.F('requests_in_current_window') + 1
        if save:
            self.save(update_fields=['requests_in_current_window', 'window_started_at', 'updated_at'])
            if not window_expired:
                # requests_in_current_window is an F() expression until refreshed;
                # without this, a second is_request_allowed_now() call on the same
                # in-memory instance would compare against a stale/wrapped value.
                self.refresh_from_db(fields=['requests_in_current_window'])


# ---------------------------------------------------------------------------
# Crawl attempt — append-only audit log
#
# ConnectedService keeps a rollup of "current" state (for fast filtering)
# but every individual fetch, success or failure, is logged here so you
# can answer "why did this start failing three days ago" without having
# thrown that history away.
# ---------------------------------------------------------------------------

class CrawlAttempt(models.Model):

    class ErrorCategory(models.TextChoices):
        NONE = 'none', 'None'
        TIMEOUT = 'timeout', 'Timeout'
        DNS_ERROR = 'dns_error', 'DNS Error'
        SSL_ERROR = 'ssl_error', 'SSL Error'
        CONNECTION_ERROR = 'connection_error', 'Connection Error'
        HTTP_ERROR = 'http_error', 'HTTP Error'
        RATE_LIMITED = 'rate_limited', 'Rate Limited'
        SSRF_BLOCKED = 'ssrf_blocked', 'Blocked (unsafe URL)'
        CONTENT_TOO_LARGE = 'content_too_large', 'Content Too Large'
        PARSE_ERROR = 'parse_error', 'Parse Error'
        UNKNOWN = 'unknown', 'Unknown'

    service = models.ForeignKey(
        'ConnectedService', on_delete=models.CASCADE, related_name='crawl_attempts'
    )
    started_at = models.DateTimeField()
    finished_at = models.DateTimeField(null=True, blank=True)
    success = models.BooleanField(default=False)
    http_status_code = models.PositiveSmallIntegerField(null=True, blank=True)
    error_category = models.CharField(
        max_length=30, choices=ErrorCategory.choices, default=ErrorCategory.NONE
    )
    error_message = models.TextField(blank=True, null=True)
    duration_ms = models.PositiveIntegerField(null=True, blank=True)
    bytes_downloaded = models.PositiveIntegerField(null=True, blank=True)
    content_hash = models.CharField(max_length=64, blank=True, null=True)
    content_changed = models.BooleanField(
        null=True, blank=True,
        help_text="Whether content_hash differed from the previous attempt's."
    )
    used_conditional_get = models.BooleanField(default=False)
    returned_not_modified = models.BooleanField(default=False)
    worker_id = models.CharField(
        max_length=100, blank=True, null=True,
        help_text="Hostname/PID/task-id of the worker that made this attempt."
    )
    proxy_used = models.CharField(max_length=255, blank=True, null=True)

    class Meta:
        ordering = ['-started_at']
        indexes = [
            models.Index(fields=['service', '-started_at']),
            models.Index(fields=['error_category']),
        ]


# ---------------------------------------------------------------------------
# ConnectedService — the main model
#
# Soft-delete only: rows are never physically removed through the normal
# ORM delete path (neither `instance.delete()` nor `queryset.delete()`).
# `soft_delete()` / bulk `.soft_delete()` are the only supported ways to
# remove a row from active use; both just flip is_active/deleted_at.
# ---------------------------------------------------------------------------

class HardDeleteNotAllowed(Exception):
    """Raised when code attempts a real SQL DELETE on ConnectedService rows."""
    pass


class ConnectedServiceQuerySet(models.QuerySet):
    def delete(self, *args, **kwargs):
        raise HardDeleteNotAllowed(
            "ConnectedService rows cannot be hard-deleted via queryset.delete(). "
            "Use queryset.soft_delete() (or instance.soft_delete()) instead."
        )

    def soft_delete(self):
        """Bulk soft-delete: mark every row in this queryset inactive."""
        return self.filter(is_active=True).update(is_active=False, deleted_at=timezone.now())

    def active(self):
        return self.filter(is_active=True)

    def deleted(self):
        return self.filter(is_active=False)

    def due_for_crawl(self):
        now = timezone.now()
        return self.filter(
            is_active=True,
        ).filter(
            models.Q(circuit_breaker_open=False)
            | models.Q(circuit_breaker_open=True, circuit_breaker_until__lte=now)
        ).filter(
            models.Q(next_crawl_at__lte=now) | models.Q(next_crawl_at__isnull=True)
        ).exclude(
            fetch_status=ConnectedService.FetchStatus.LOCKED
        )

    def needs_retry(self):
        now = timezone.now()
        return self.filter(
            fetch_status=ConnectedService.FetchStatus.ERROR,
            is_active=True,
        ).filter(
            models.Q(circuit_breaker_open=False)
            | models.Q(circuit_breaker_open=True, circuit_breaker_until__lte=now)
        ).filter(models.Q(next_retry_at__lte=now) | models.Q(next_retry_at__isnull=True))

    def stuck_locks(self, older_than_seconds=900):
        cutoff = timezone.now() - timezone.timedelta(seconds=older_than_seconds)
        return self.filter(fetch_status=ConnectedService.FetchStatus.LOCKED, locked_at__lt=cutoff)

    def circuit_open(self):
        return self.filter(circuit_breaker_open=True)


class ConnectedService(models.Model):

    class ServiceType(models.TextChoices):
        GENERAL = 'general', 'General'
        EDUCATION = 'education', 'Education'
        PRODUCT = 'product', 'Product'
        BRAND = 'brand', 'Brand'
        NEWS = 'news', 'News'
        LOCATION = 'location', 'Location'
        PERSON = 'person', 'Person'
        BUSINESS = 'business', 'Business'
        ENTERTAINMENT = 'entertainment', 'Entertainment'
        SOCIAL = 'social', 'Social'
        GOVERNMENT = 'government', 'Government'

    class Status(models.TextChoices):
        PRIVATE = 'private', 'Private'
        PUBLIC = 'public', 'Public'

    class FetchStatus(models.TextChoices):
        PENDING = 'pending', 'Pending'
        LOCKED = 'locked', 'Locked (crawl in progress)'
        SUCCESS = 'success', 'Success'
        ERROR = 'error', 'Error'
        NOT_MODIFIED = 'not_modified', 'Not Modified (conditional GET)'

    class CrawlSource(models.TextChoices):
        MANUAL = 'manual', 'Manual'
        SCHEDULED = 'scheduled', 'Scheduled'
        WEBHOOK = 'webhook', 'Webhook Triggered'
        BACKFILL = 'backfill', 'Backfill'

    class ContentSensitivity(models.TextChoices):
        UNKNOWN = 'unknown', 'Unknown'
        CLEAN = 'clean', 'Clean'
        PII_DETECTED = 'pii_detected', 'PII Detected'

    objects = ConnectedServiceQuerySet.as_manager()

    # --- Ownership / identity ------------------------------------------------
    user = models.ForeignKey(
        User, on_delete=models.CASCADE, related_name='connected_services_user'
    )
    profile = models.ForeignKey(
        ProfileInfo, blank=True, null=True, on_delete=models.CASCADE,
        related_name='connected_services_profile'
    )
    organization_id = models.CharField(
        max_length=100, blank=True, null=True, db_index=True,
        help_text="Tenant identifier for multi-tenant deployments. Kept as a plain "
                   "indexed column rather than a FK so this app doesn't hard-depend "
                   "on an Organization model existing."
    )
    service_name = models.CharField(max_length=200)
    service_url = models.URLField(max_length=500)
    domain = models.CharField(max_length=255, blank=True, null=True, db_index=True)
    domain_policy = models.ForeignKey(
        DomainCrawlPolicy, null=True, blank=True, on_delete=models.SET_NULL,
        related_name='services',
        help_text="Auto-linked to the DomainCrawlPolicy matching `domain` on save()."
    )
    service_type = models.CharField(
        max_length=50, choices=ServiceType.choices, default=ServiceType.GENERAL, db_index=True
    )
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PRIVATE)
    is_connected = models.BooleanField(default=False)
    is_active = models.BooleanField(
        default=True,
        help_text="Soft-delete flag. Excluded rows are never picked up by due_for_crawl()."
    )
    deleted_at = models.DateTimeField(null=True, blank=True)

    # --- API authentication (encrypted at rest, key-rotation aware) ----------
    api_key = EncryptedCharField(max_length=1000, blank=True, null=True)
    auth_token = EncryptedTextField(blank=True, null=True)
    webhook_url = models.URLField(max_length=500, blank=True, null=True)
    webhook_secret = EncryptedCharField(max_length=255, blank=True, null=True)
    notify_on_change = models.BooleanField(default=False)

    # --- Crawl scheduling & politeness ---------------------------------------
    crawl_priority = models.PositiveSmallIntegerField(
        default=5, help_text="1 = highest priority, 10 = lowest. Used to order the crawl queue."
    )
    crawl_frequency_minutes = models.PositiveIntegerField(
        default=1440, help_text="How often this URL should be recrawled, in minutes."
    )
    next_crawl_at = models.DateTimeField(null=True, blank=True, db_index=True)
    crawl_source = models.CharField(
        max_length=20, choices=CrawlSource.choices, default=CrawlSource.SCHEDULED
    )
    max_crawl_depth = models.PositiveSmallIntegerField(
        default=0, help_text="0 = only this URL. >0 = follow links up to N hops (if a link-following worker is used)."
    )
    user_agent = models.CharField(max_length=255, blank=True, null=True)
    render_javascript = models.BooleanField(
        default=False, help_text="Whether this page requires a headless browser to render before extraction."
    )

    # --- Distributed worker locking ------------------------------------------
    lock_id = models.UUIDField(null=True, blank=True, db_index=True)
    locked_at = models.DateTimeField(null=True, blank=True)
    locked_by = models.CharField(max_length=100, blank=True, null=True)

    # --- Retry / circuit breaker ----------------------------------------------
    retry_count = models.PositiveIntegerField(default=0)
    max_retries = models.PositiveIntegerField(default=5)
    next_retry_at = models.DateTimeField(null=True, blank=True)
    consecutive_failures = models.PositiveIntegerField(default=0)
    circuit_breaker_open = models.BooleanField(
        default=False,
        help_text="Trips after too many consecutive failures. due_for_crawl()/needs_retry() "
                   "skip this row until circuit_breaker_until passes or it's manually reset."
    )
    circuit_breaker_until = models.DateTimeField(null=True, blank=True)
    circuit_breaker_threshold = models.PositiveIntegerField(default=10)

    # --- Conditional fetching --------------------------------------------------
    etag = models.CharField(max_length=255, blank=True, null=True)
    last_modified_header = models.CharField(max_length=255, blank=True, null=True)

    # --- Open Graph ------------------------------------------------------------
    og_title = models.CharField(max_length=500, blank=True, null=True)
    og_description = models.TextField(blank=True, null=True)
    og_thumbnail = models.URLField(max_length=500, blank=True, null=True)
    og_site_name = models.CharField(max_length=200, blank=True, null=True)
    og_type = models.CharField(max_length=100, blank=True, null=True)
    og_locale = models.CharField(max_length=20, blank=True, null=True)
    og_video = models.URLField(max_length=500, blank=True, null=True)
    og_audio = models.URLField(max_length=500, blank=True, null=True)

    # --- Twitter / X Card -------------------------------------------------------
    twitter_card = models.CharField(max_length=50, blank=True, null=True)
    twitter_title = models.CharField(max_length=500, blank=True, null=True)
    twitter_description = models.TextField(blank=True, null=True)
    twitter_image = models.URLField(max_length=500, blank=True, null=True)
    twitter_site = models.CharField(max_length=100, blank=True, null=True)
    twitter_creator = models.CharField(max_length=100, blank=True, null=True)

    # --- Raw page identity -------------------------------------------------------
    page_title = models.CharField(max_length=500, blank=True, null=True)
    meta_description = models.TextField(blank=True, null=True)
    meta_keywords = models.CharField(max_length=1000, blank=True, null=True)
    meta_generator = models.CharField(max_length=200, blank=True, null=True)
    charset = models.CharField(max_length=50, blank=True, null=True)
    content_language = models.CharField(max_length=20, blank=True, null=True)
    theme_color = models.CharField(max_length=20, blank=True, null=True)
    viewport = models.CharField(max_length=200, blank=True, null=True)

    # --- Extended page metadata -------------------------------------------------
    canonical_url = models.URLField(max_length=500, blank=True, null=True)
    favicon = models.URLField(max_length=500, blank=True, null=True)
    apple_touch_icon = models.URLField(max_length=500, blank=True, null=True)
    manifest_url = models.URLField(max_length=500, blank=True, null=True)
    amp_url = models.URLField(max_length=500, blank=True, null=True)
    rss_feed_url = models.URLField(max_length=500, blank=True, null=True)
    atom_feed_url = models.URLField(max_length=500, blank=True, null=True)
    author = models.CharField(max_length=255, blank=True, null=True)
    author_url = models.URLField(max_length=500, blank=True, null=True)
    published_time = models.CharField(max_length=100, blank=True, null=True)
    modified_time = models.CharField(max_length=100, blank=True, null=True)
    article_section = models.CharField(max_length=200, blank=True, null=True)
    article_tags = models.JSONField(default=list, blank=True)
    category_breadcrumb = models.JSONField(default=list, blank=True)
    structured_data = models.JSONField(default=list, blank=True)
    alternate_languages = models.JSONField(default=list, blank=True)
    same_as_links = models.JSONField(default=list, blank=True)

    # --- Commerce ---------------------------------------------------------------
    price_amount = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    price_currency = models.CharField(max_length=3, blank=True, null=True)
    price_original = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    availability = models.CharField(max_length=50, blank=True, null=True)
    product_sku = models.CharField(max_length=100, blank=True, null=True)
    product_gtin = models.CharField(max_length=50, blank=True, null=True)
    product_condition = models.CharField(max_length=50, blank=True, null=True)
    brand_name = models.CharField(max_length=200, blank=True, null=True)
    seller_name = models.CharField(max_length=200, blank=True, null=True)
    rating_value = models.DecimalField(
        max_digits=4, decimal_places=2, null=True, blank=True,
        help_text="Allows up to 99.99 since some source schemas rate out of 10 or 100, not just 5."
    )
    rating_count = models.PositiveIntegerField(null=True, blank=True)

    # --- Robots / indexability -------------------------------------------------
    robots_meta = models.CharField(max_length=200, blank=True, null=True)
    x_robots_tag = models.CharField(max_length=200, blank=True, null=True)
    is_indexable = models.BooleanField(null=True, blank=True)

    # --- Extracted media & content -----------------------------------------------
    extracted_images = models.JSONField(default=list, blank=True)
    extracted_videos = models.JSONField(default=list, blank=True)
    extracted_links = models.JSONField(default=list, blank=True)
    extracted_headings = models.JSONField(default=list, blank=True)
    extracted_text = models.TextField(blank=True, null=True)
    word_count = models.PositiveIntegerField(null=True, blank=True)
    reading_time_minutes = models.PositiveIntegerField(null=True, blank=True)
    content_hash = models.CharField(max_length=64, blank=True, null=True, db_index=True)
    content_version = models.PositiveIntegerField(
        default=0, help_text="Incremented each time content_hash changes from the previous fetch."
    )
    content_sensitivity = models.CharField(
        max_length=20, choices=ContentSensitivity.choices, default=ContentSensitivity.UNKNOWN,
        help_text="Set by a PII-scan step before extracted_text is persisted/exposed downstream."
    )

    # --- Fetch / response diagnostics --------------------------------------------
    http_status_code = models.PositiveSmallIntegerField(null=True, blank=True)
    content_type = models.CharField(max_length=200, blank=True, null=True)
    response_headers = models.JSONField(null=True, blank=True)
    redirect_chain = models.JSONField(default=list, blank=True)
    page_size_bytes = models.PositiveIntegerField(null=True, blank=True)
    load_time_ms = models.PositiveIntegerField(null=True, blank=True)
    dns_time_ms = models.PositiveIntegerField(null=True, blank=True)
    ttfb_ms = models.PositiveIntegerField(null=True, blank=True)
    ssl_valid = models.BooleanField(null=True, blank=True)
    screenshot_url = models.URLField(max_length=500, blank=True, null=True)
    proxy_used = models.CharField(max_length=255, blank=True, null=True)

    # --- Raw cache ------------------------------------------------------------
    last_fetched_data = models.JSONField(null=True, blank=True)
    last_fetch_time = models.DateTimeField(null=True, blank=True)
    fetch_status = models.CharField(
        max_length=20, choices=FetchStatus.choices, default=FetchStatus.PENDING, db_index=True
    )
    fetch_error = models.TextField(blank=True, null=True)
    fetch_error_category = models.CharField(
        max_length=30, choices=CrawlAttempt.ErrorCategory.choices,
        default=CrawlAttempt.ErrorCategory.NONE,
    )
    fetch_attempts = models.PositiveIntegerField(default=0)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    created_by = models.CharField(max_length=150, blank=True, null=True)

    cached_feed_payload = models.JSONField(null=True, blank=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['user', 'service_type']),
            models.Index(fields=['user', 'status']),
            models.Index(fields=['fetch_status', 'last_fetch_time']),
            models.Index(fields=['status', 'is_connected']),
            models.Index(fields=['domain']),
            models.Index(fields=['organization_id']),
            models.Index(fields=['crawl_priority', 'next_crawl_at']),
            models.Index(fields=['circuit_breaker_open']),
            models.Index(fields=['is_active', 'next_crawl_at']),
            models.Index(fields=['created_at']),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=['user', 'service_url'], name='uniq_user_service_url'
            ),
        ]

    def __str__(self):
        return f"{self.service_name} - {self.user.email_or_phone}"

    def clean(self):
        super().clean()
        if self.fetch_status == self.FetchStatus.SUCCESS and self.last_fetch_time is None:
            raise ValidationError(
                {'fetch_status': "fetch_status is 'success' but last_fetch_time is not set."}
            )
        if self.fetch_status == self.FetchStatus.ERROR and not self.fetch_error:
            raise ValidationError(
                {'fetch_error': "fetch_status is 'error' but fetch_error is empty."}
            )
        if self.service_url:
            try:
                validate_crawl_url(self.service_url)
            except UnsafeCrawlURLError as exc:
                raise ValidationError({'service_url': str(exc)})

    def save(self, *args, **kwargs):
        run_full_clean = kwargs.pop('run_full_clean', False)
        if self.service_url:
            self.domain = extract_domain(self.service_url) or self.domain
            if self.domain:
                policy, _ = DomainCrawlPolicy.objects.get_or_create(domain=self.domain)
                self.domain_policy = policy
        if run_full_clean:
            self.full_clean()
        super().save(*args, **kwargs)

    # --- crawl queue / locking -------------------------------------------------

    @classmethod
    @transaction.atomic
    def claim_next_for_crawl(cls, worker_id: str, lock_timeout_seconds: int = 600, lookahead: int = 50):
        """Atomically claim one due row for this worker, respecting domain
        rate limits, and return it (or None if nothing is claimable).

        Scans up to `lookahead` candidates in priority order rather than only
        the single highest-priority row: if that row's domain is currently
        rate-limited, the whole queue must not stall behind it while other
        domains sit idle.
        """
        now = timezone.now()
        candidates = (
            cls.objects.select_for_update(skip_locked=True)
            .due_for_crawl()
            .order_by('crawl_priority', 'next_crawl_at')
            .select_related('domain_policy')[:lookahead]
        )
        for candidate in candidates:
            if candidate.domain_policy and not candidate.domain_policy.is_request_allowed_now():
                continue
            if candidate.circuit_breaker_open and candidate.circuit_breaker_until and candidate.circuit_breaker_until <= now:
                candidate.reset_circuit_breaker(save=False)
            candidate.lock_id = uuid.uuid4()
            candidate.locked_at = now
            candidate.locked_by = worker_id
            candidate.fetch_status = cls.FetchStatus.LOCKED
            candidate.save(update_fields=[
                'lock_id', 'locked_at', 'locked_by', 'fetch_status',
                'circuit_breaker_open', 'circuit_breaker_until', 'consecutive_failures',
                'updated_at',
            ])
            if candidate.domain_policy:
                candidate.domain_policy.record_request()
            return candidate
        return None

    def release_lock(self, *, save=True):
        self.lock_id = None
        self.locked_at = None
        self.locked_by = None
        if save:
            self.save(update_fields=['lock_id', 'locked_at', 'locked_by', 'updated_at'])

    # --- retry / circuit breaker -------------------------------------------------

    def compute_next_retry_at(self):
        """Exponential backoff with jitter, capped at 24h."""
        base_seconds = min(60 * (2 ** self.retry_count), 60 * 60 * 24)
        jitter = random.uniform(0, base_seconds * 0.2)
        return timezone.now() + timezone.timedelta(seconds=base_seconds + jitter)

    def _maybe_trip_circuit_breaker(self):
        if self.consecutive_failures >= self.circuit_breaker_threshold:
            self.circuit_breaker_open = True
            self.circuit_breaker_until = timezone.now() + timezone.timedelta(hours=6)

    def reset_circuit_breaker(self, *, save=True):
        self.circuit_breaker_open = False
        self.circuit_breaker_until = None
        self.consecutive_failures = 0
        if save:
            self.save(update_fields=[
                'circuit_breaker_open', 'circuit_breaker_until', 'consecutive_failures', 'updated_at',
            ])

    # --- convenience helpers -------------------------------------------------

    @property
    def needs_retry(self) -> bool:
        return self.fetch_status in (self.FetchStatus.PENDING, self.FetchStatus.ERROR)

    def _log_attempt(self, **kwargs):
        CrawlAttempt.objects.create(service=self, **kwargs)

    def mark_fetch_success(self, *, content_hash=None, http_status_code=200,
                            duration_ms=None, bytes_downloaded=None,
                            not_modified=False, worker_id=None, save=True):
        now = timezone.now()
        content_changed = bool(content_hash) and content_hash != self.content_hash
        if content_changed:
            self.content_version = models.F('content_version') + 1

        self.fetch_status = self.FetchStatus.NOT_MODIFIED if not_modified else self.FetchStatus.SUCCESS
        self.fetch_error = None
        self.fetch_error_category = CrawlAttempt.ErrorCategory.NONE
        self.last_fetch_time = now
        self.http_status_code = http_status_code
        self.consecutive_failures = 0
        self.circuit_breaker_open = False
        self.circuit_breaker_until = None
        self.retry_count = 0
        self.next_retry_at = None
        self.next_crawl_at = now + timezone.timedelta(minutes=self.crawl_frequency_minutes)
        if content_hash:
            self.content_hash = content_hash
        self.release_lock(save=False)

        self._log_attempt(
            started_at=now, finished_at=now, success=True,
            http_status_code=http_status_code, duration_ms=duration_ms,
            bytes_downloaded=bytes_downloaded, content_hash=content_hash,
            content_changed=content_changed, used_conditional_get=bool(self.etag or self.last_modified_header),
            returned_not_modified=not_modified, worker_id=worker_id,
        )

        if save:
            # Deliberately a full save(), not save(update_fields=[...]).
            # Callers (scrape_and_store, connect_url.py, etc.) set a whole
            # page's worth of content fields — og_title, extracted_text,
            # structured_data, and so on — directly on the instance before
            # calling this method and rely on it to be "the save". An
            # update_fields list here that only covers this method's own
            # bookkeeping columns would silently drop every one of those
            # content fields: Django only writes the columns named in
            # update_fields, so anything else dirty on the instance never
            # reaches the database even though it looks saved in memory.
            self.save()
            self.refresh_from_db(fields=['content_version'])

    def mark_fetch_error(self, error_message: str, *, error_category=CrawlAttempt.ErrorCategory.UNKNOWN,
                          http_status_code=None, duration_ms=None, worker_id=None, save=True):
        now = timezone.now()
        self.fetch_status = self.FetchStatus.ERROR
        self.fetch_error = error_message[:5000]
        self.fetch_error_category = error_category
        self.last_fetch_time = now
        self.http_status_code = http_status_code
        self.fetch_attempts = models.F('fetch_attempts') + 1
        self.retry_count = models.F('retry_count') + 1
        self.consecutive_failures = models.F('consecutive_failures') + 1
        self.release_lock(save=False)

        self._log_attempt(
            started_at=now, finished_at=now, success=False,
            http_status_code=http_status_code, error_category=error_category,
            error_message=error_message[:5000], duration_ms=duration_ms, worker_id=worker_id,
        )

        if save:
            # Full save() for the same reason as mark_fetch_success() above —
            # callers set content fields on the instance before calling this
            # and expect them persisted, not just this method's own columns.
            self.save()
            self.refresh_from_db(fields=['fetch_attempts', 'retry_count', 'consecutive_failures'])
            self._maybe_trip_circuit_breaker()
            self.next_retry_at = (
                None if self.retry_count > self.max_retries else self.compute_next_retry_at()
            )
            self.save()

    def delete(self, *args, **kwargs):
        raise HardDeleteNotAllowed(
            "ConnectedService rows cannot be hard-deleted via instance.delete(). "
            "Use instance.soft_delete() instead."
        )

    def soft_delete(self, *, save=True):
        self.is_active = False
        self.deleted_at = timezone.now()
        if save:
            self.save(update_fields=['is_active', 'deleted_at', 'updated_at'])

    def _hard_delete(self, *args, **kwargs):
        """Genuine, irreversible SQL DELETE — bypasses the soft-delete guard.

        Not exposed as `.delete()` on purpose so a stray `obj.delete()` or
        `queryset.delete()` can never destroy data. Reserve this for
        deliberate, audited compliance erasure (e.g. a GDPR data-removal
        request), called explicitly by name.
        """
        return models.Model.delete(self, *args, **kwargs)

