# megamind/models/visit_log.py

from __future__ import annotations

import uuid

from django.conf import settings
from django.db import models

__all__ = ['DiscoveryVisitLog']


class DiscoveryVisitLog(models.Model):
    """
    One row per hit to DiscoveryEngineView.

    Enterprise-grade visitor telemetry: identity, network/geo, device
    fingerprint, acquisition/attribution, session behavior, security
    signals, experimentation context, observability correlation ids,
    and data-governance metadata — all captured at request time.

    IMMUTABLE BY DESIGN: every field is editable=False. This is an
    append-only audit trail — rows are created once by
    record_discovery_visit_task and never modified. `editable=False`
    hides all fields from ModelForms and the Django admin's add/change
    forms (list/detail display in admin still works, and code can
    still `.create()` / bulk-write directly via the ORM — this only
    blocks form-based / admin-based editing).

    DATA SOURCE TRUST: ip_address, user_agent, referrer,
    accept_language, screen/viewport dimensions, and any client-JS
    reported field are client-supplied. Treat them as hints for
    analytics/UX, never as verified identity or security truth. Server
    side signals (geo/ASN lookup, bot score, TLS fingerprint from the
    TLS layer, WAF verdict) are comparatively higher trust but still
    probabilistic.

    DATA GOVERNANCE / PRIVACY: this table stores IP address, precise
    device fingerprint components, coarse geolocation, and campaign
    attribution against a possibly-identified user. This is regulated
    personal data in most jurisdictions (GDPR/CCPA/etc). Before
    enabling fingerprinting or geo capture in production:
      - Define and enforce `retention_expires_at` via a scheduled purge
        job using `queryset.delete()` (per-row delete is blocked below).
      - Populate `consent_given` / `consent_categories` from your CMP
        and gate fingerprinting fields on consent where required.
      - Set `data_classification` per your internal data taxonomy so
        downstream systems (warehouse, BI tools) inherit handling rules.
      - Have a documented erasure procedure for regulator/user requests
        that respects the immutability guarantee (e.g. anonymization
        pass instead of deletion, run via a dedicated maintenance task,
        not `.save()`/`.delete()` on this model).
    """

    class DeviceType(models.TextChoices):
        DESKTOP = 'desktop', 'Desktop'
        MOBILE  = 'mobile',  'Mobile'
        TABLET  = 'tablet',  'Tablet'
        BOT     = 'bot',     'Bot / Crawler'
        UNKNOWN = 'unknown', 'Unknown'

    class ConnectionType(models.TextChoices):
        WIFI     = 'wifi',     'WiFi'
        CELLULAR = 'cellular', 'Cellular'
        ETHERNET = 'ethernet', 'Ethernet'
        UNKNOWN  = 'unknown',  'Unknown'

    class DataClassification(models.TextChoices):
        PUBLIC       = 'public',       'Public'
        INTERNAL     = 'internal',     'Internal'
        CONFIDENTIAL = 'confidential', 'Confidential'
        RESTRICTED   = 'restricted',   'Restricted (PII)'

    # ── Identity / correlation ──────────────────────────────────
    id = models.UUIDField(
        primary_key=True, default=uuid.uuid4, editable=False,
        help_text='UUID pk avoids sequential-id enumeration and simplifies multi-region writes.',
    )
    request_id = models.CharField(
        max_length=64, blank=True, default='', editable=False, db_index=True,
        help_text='Per-request id, e.g. from X-Request-ID, for log/trace correlation.',
    )
    trace_id = models.CharField(
        max_length=64, blank=True, default='', editable=False, db_index=True,
        help_text='Distributed tracing id (e.g. W3C traceparent), for APM correlation.',
    )
    correlation_id = models.CharField(
        max_length=64, blank=True, default='', editable=False, db_index=True,
        help_text='Cross-service correlation id, if propagated separately from trace_id.',
    )

    # ── Who ──────────────────────────────────────────────────────
    user = models.ForeignKey(
            settings.AUTH_USER_MODEL,
            null=True, blank=True,
            on_delete=models.SET_NULL,
            related_name='megamind_discovery_visits',
            related_query_name='megamind_discovery_visit',
            editable=False,
            help_text='Null for anonymous visitors.',
        )
    role_at_visit = models.CharField(
        max_length=32, null=True, blank=True, db_index=True,
        editable=False,
        help_text="Snapshot of user.role at visit time. Null for anonymous.",
    )
    is_authenticated = models.BooleanField(
        default=False, db_index=True, editable=False,
    )
    account_tier_at_visit = models.CharField(
        max_length=32, blank=True, default='', editable=False,
        help_text='Snapshot of subscription/plan tier at visit time, if applicable.',
    )

    session_key = models.CharField(max_length=40, blank=True, default='', editable=False)
    device_fingerprint = models.CharField(
        max_length=64, blank=True, default='', editable=False, db_index=True,
        help_text='Stable hashed client fingerprint (e.g. FingerprintJS visitorId). Requires consent.',
    )
    is_first_visit = models.BooleanField(
        default=False, editable=False,
        help_text='True if this session_key had no prior row at write time.',
    )
    is_returning_visitor = models.BooleanField(
        default=False, editable=False,
        help_text='True if device_fingerprint or user has any prior row, regardless of session.',
    )
    visit_sequence_number = models.PositiveIntegerField(
        default=1, editable=False,
        help_text='Ordinal of this visit for the session (1 = landing hit).',
    )

    # ── Network / geo ────────────────────────────────────────────
    ip_address = models.GenericIPAddressField(null=True, blank=True, editable=False)
    country = models.CharField(
        max_length=2, blank=True, default='', editable=False, db_index=True,
        help_text='ISO 3166-1 alpha-2 country code, from GeoIP lookup.',
    )
    region = models.CharField(max_length=100, blank=True, default='', editable=False)
    city = models.CharField(max_length=100, blank=True, default='', editable=False)
    postal_code = models.CharField(max_length=20, blank=True, default='', editable=False)
    latitude = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True, editable=False)
    longitude = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True, editable=False)
    isp = models.CharField(max_length=255, blank=True, default='', editable=False)
    asn = models.CharField(max_length=20, blank=True, default='', editable=False)
    is_proxy_or_vpn = models.BooleanField(
        default=False, db_index=True, editable=False,
        help_text='Best-effort flag from IP intelligence. Not authoritative.',
    )
    is_tor_exit_node = models.BooleanField(default=False, db_index=True, editable=False)
    is_datacenter_ip = models.BooleanField(
        default=False, db_index=True, editable=False,
        help_text='True if IP belongs to a known cloud/hosting ASN rather than a residential/mobile ISP.',
    )
    connection_type = models.CharField(
        max_length=10, choices=ConnectionType.choices,
        default=ConnectionType.UNKNOWN, editable=False,
    )

    # ── Client / device / fingerprint ───────────────────────────
    user_agent = models.CharField(max_length=512, blank=True, default='', editable=False)
    device_type = models.CharField(
        max_length=10, choices=DeviceType.choices,
        default=DeviceType.UNKNOWN, db_index=True, editable=False,
    )
    browser_name = models.CharField(max_length=50, blank=True, default='', editable=False)
    browser_version = models.CharField(max_length=20, blank=True, default='', editable=False)
    os_name = models.CharField(max_length=50, blank=True, default='', editable=False)
    os_version = models.CharField(max_length=20, blank=True, default='', editable=False)
    accept_language = models.CharField(max_length=255, blank=True, default='', editable=False)
    timezone = models.CharField(
        max_length=64, blank=True, default='', editable=False,
        help_text='Client-reported IANA timezone (e.g. Asia/Dhaka), from JS Intl API if available.',
    )
    screen_width = models.PositiveIntegerField(null=True, blank=True, editable=False)
    screen_height = models.PositiveIntegerField(null=True, blank=True, editable=False)
    viewport_width = models.PositiveIntegerField(null=True, blank=True, editable=False)
    viewport_height = models.PositiveIntegerField(null=True, blank=True, editable=False)
    pixel_ratio = models.DecimalField(max_digits=4, decimal_places=2, null=True, blank=True, editable=False)
    color_depth = models.PositiveSmallIntegerField(null=True, blank=True, editable=False)

    # ── Security / trust signals ────────────────────────────────
    tls_version = models.CharField(max_length=16, blank=True, default='', editable=False)
    ja3_fingerprint = models.CharField(
        max_length=64, blank=True, default='', editable=False, db_index=True,
        help_text='TLS client fingerprint hash, useful for bot/tooling detection independent of UA string.',
    )
    bot_score = models.PositiveSmallIntegerField(
        null=True, blank=True, editable=False,
        help_text='0-100 bot likelihood score from upstream WAF/CDN (e.g. Cloudflare), if available.',
    )
    is_likely_bot = models.BooleanField(
        default=False, db_index=True, editable=False,
        help_text='UA/heuristic-based bot flag. Distinct from bot_score, which comes from an external service.',
    )
    waf_action = models.CharField(
        max_length=20, blank=True, default='', editable=False,
        help_text='Action taken by upstream WAF/CDN for this request (allow, challenge, block-logged, etc).',
    )
    threat_score = models.PositiveSmallIntegerField(
        null=True, blank=True, editable=False,
        help_text='General abuse/threat score from IP reputation service, if integrated.',
    )

    # ── Acquisition / attribution ───────────────────────────────
    referrer = models.CharField(max_length=1000, blank=True, default='', editable=False)
    referrer_domain = models.CharField(max_length=255, blank=True, default='', editable=False, db_index=True)
    landing_page_path = models.CharField(
        max_length=500, blank=True, default='', editable=False,
        help_text='First page path of the session, captured once per session at landing.',
    )
    utm_source = models.CharField(max_length=100, blank=True, default='', editable=False)
    utm_medium = models.CharField(max_length=100, blank=True, default='', editable=False)
    utm_campaign = models.CharField(max_length=100, blank=True, default='', editable=False)
    utm_term = models.CharField(max_length=100, blank=True, default='', editable=False)
    utm_content = models.CharField(max_length=100, blank=True, default='', editable=False)
    click_id = models.CharField(
        max_length=255, blank=True, default='', editable=False,
        help_text='Ad-network click id if present (gclid, fbclid, msclkid, etc).',
    )

    # ── Experimentation / product context ───────────────────────
    ab_test_variant = models.CharField(
        max_length=100, blank=True, default='', editable=False,
        help_text='Assigned experiment variant(s) for this request, e.g. "discovery_layout:v2".',
    )
    feature_flags = models.JSONField(
        default=dict, blank=True, editable=False,
        help_text='Snapshot of active feature-flag states relevant to this view at request time.',
    )
    client_app_version = models.CharField(
        max_length=32, blank=True, default='', editable=False,
        help_text='Frontend/app build version, for correlating behavior with releases.',
    )
    environment = models.CharField(
        max_length=20, blank=True, default='', editable=False,
        help_text='Serving environment (production, staging, etc), useful when logs are aggregated across envs.',
    )

    # ── What they were looking at ───────────────────────────────
    query_string  = models.CharField(max_length=1000, blank=True, default='', editable=False)
    search_query  = models.CharField(max_length=255, blank=True, default='', editable=False)
    filter_slug   = models.CharField(max_length=255, blank=True, default='', editable=False)
    sort_by       = models.CharField(max_length=50, blank=True, default='', editable=False)
    page_number   = models.PositiveIntegerField(default=1, editable=False)
    results_count = models.PositiveIntegerField(
        null=True, blank=True, editable=False,
        help_text='Number of results returned for this query/filter combination, for zero-result tracking.',
    )

    # ── Performance ──────────────────────────────────────────────
    server_response_time_ms = models.PositiveIntegerField(null=True, blank=True, editable=False)

    # ── Consent / data governance ───────────────────────────────
    consent_given = models.BooleanField(
        null=True, blank=True, editable=False,
        help_text='Null = no consent signal received. Gate fingerprinting/geo capture on this where required.',
    )
    consent_categories = models.JSONField(
        default=list, blank=True, editable=False,
        help_text='Consent categories granted at visit time, e.g. ["analytics", "marketing"].',
    )
    gdpr_applicable = models.BooleanField(default=False, editable=False)
    ccpa_applicable = models.BooleanField(default=False, editable=False)
    data_classification = models.CharField(
        max_length=20, choices=DataClassification.choices,
        default=DataClassification.RESTRICTED, editable=False,
        help_text='Contains IP/device data by default, so classified RESTRICTED unless proven otherwise.',
    )
    retention_expires_at = models.DateTimeField(
        null=True, blank=True, editable=False, db_index=True,
        help_text='Set at write time (e.g. now + 13 months) and consumed by a scheduled purge task.',
    )

    visited_at = models.DateTimeField(auto_now_add=True, db_index=True, editable=False)

    product = models.ForeignKey(
        'ponno.Product',
        null=True, blank=True,
        on_delete=models.SET_NULL,
        related_name='megamind_discovery_visits',
        related_query_name='megamind_discovery_visit',
        editable=False,
        db_index=True,
        help_text='Product viewed at this visit, if this row logs a product detail hit.',
    )

    class Meta:
        db_table = 'megamind_visit_log'
        indexes = [
            models.Index(fields=['user', 'visited_at']),
            models.Index(fields=['role_at_visit', 'visited_at']),
            models.Index(fields=['ip_address', 'visited_at']),
            models.Index(fields=['session_key', 'visited_at']),
            models.Index(fields=['country', 'visited_at']),
            models.Index(fields=['utm_source', 'utm_medium', 'utm_campaign']),
            models.Index(fields=['retention_expires_at']),
            models.Index(fields=['product', 'visited_at']),
        ]
        ordering = ['-visited_at']
        verbose_name = 'Discovery visit log'
        verbose_name_plural = 'Discovery visit logs'

    def __str__(self) -> str:
        who = self.user_id or self.ip_address or 'unknown'
        return f'{who} @ {self.visited_at:%Y-%m-%d %H:%M:%S}'

    def save(self, *args, **kwargs):
        """
        Enforce append-only at the ORM level too: once a row has a pk,
        refuse to save over it. `editable=False` only stops form/admin
        edits — this stops a stray `.save()` on an already-fetched
        instance from mutating history.
        """
        if self._state.adding is False:
            raise ValueError(
                'DiscoveryVisitLog rows are immutable and cannot be updated '
                'after creation.'
            )
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValueError(
            'DiscoveryVisitLog rows are immutable and cannot be deleted '
            'individually. Use a retention/cleanup task with queryset.delete() '
            '(e.g. filtered on retention_expires_at) if bulk purging is required.'
        )