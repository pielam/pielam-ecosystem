# apps/ponno/models/discovery_visit_log.py

from __future__ import annotations

from django.conf import settings
from django.db import models


class DiscoveryVisitLog(models.Model):
    """
    One row per hit to DiscoveryEngineView.
    Captures WHO visited (user + role snapshot, or anonymous + IP)
    and WHAT they were looking at (query/filter/sort/page).

    IMMUTABLE BY DESIGN: every field is editable=False. This is an
    append-only audit trail — rows are created once by
    record_discovery_visit_task and never modified. `editable=False`
    hides all fields from ModelForms and the Django admin's add/change
    forms (list/detail display in admin still works, and code can
    still `.create()` / bulk-write directly via the ORM — this only
    blocks form-based / admin-based editing).
    """

    class DeviceType(models.TextChoices):
        DESKTOP = 'desktop', 'Desktop'
        MOBILE  = 'mobile',  'Mobile'
        TABLET  = 'tablet',  'Tablet'
        BOT     = 'bot',     'Bot / Crawler'
        UNKNOWN = 'unknown', 'Unknown'

    # ── Who ──────────────────────────────────────────────────────
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True, blank=True,
        on_delete=models.SET_NULL,
        related_name='discovery_visits',
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

    session_key = models.CharField(max_length=40, blank=True, default='', editable=False)
    ip_address  = models.GenericIPAddressField(null=True, blank=True, editable=False)
    user_agent  = models.CharField(max_length=512, blank=True, default='', editable=False)
    device_type = models.CharField(
        max_length=10, choices=DeviceType.choices,
        default=DeviceType.UNKNOWN, db_index=True, editable=False,
    )
    referrer = models.CharField(max_length=1000, blank=True, default='', editable=False)

    # ── What they were looking at ───────────────────────────────
    query_string  = models.CharField(max_length=1000, blank=True, default='', editable=False)
    search_query  = models.CharField(max_length=255, blank=True, default='', editable=False)
    filter_slug   = models.CharField(max_length=255, blank=True, default='', editable=False)
    sort_by       = models.CharField(max_length=50, blank=True, default='', editable=False)
    page_number   = models.PositiveIntegerField(default=1, editable=False)

    visited_at = models.DateTimeField(auto_now_add=True, db_index=True, editable=False)

    class Meta:
        db_table = 'ponno_discovery_visit_log'
        indexes = [
            models.Index(fields=['user', 'visited_at']),
            models.Index(fields=['role_at_visit', 'visited_at']),
            models.Index(fields=['ip_address', 'visited_at']),
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
        if self.pk is not None:
            raise ValueError(
                'DiscoveryVisitLog rows are immutable and cannot be updated '
                'after creation.'
            )
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValueError(
            'DiscoveryVisitLog rows are immutable and cannot be deleted '
            'individually. Use a retention/cleanup task with queryset.delete() '
            'if bulk purging is required.'
        )