# apps/customer/models/business_info_log.py

"""
Lightweight, automatic change history for the BusinessInfo model
(business_info.py) -- same pattern as account_log.py / contact_info_log.py /
location_info_log.py / profile_info_log.py, adapted to BusinessInfo's
field set.

HOW IT WORKS
------------
pre_save/post_save signals on BusinessInfo diff a curated set of
"trackable" fields (TRACKED_FIELDS below) before vs. after every
save(), and write one BusinessInfoLog row per save that actually
changed something. This covers both full saves AND the narrow
save(update_fields=[...]) calls business_info.py's helper methods
already use (verify(), unverify(), soft_delete(), restore()).

Deliberately NOT tracked:

- `uuid`, `created_at`, `updated_at`: same bookkeeping rationale as
  every other *_log.py in this app -- the first never changes, the
  second never changes after insert, and the third changes on every
  save (or is already implied by this log row's own `created_at`) and
  would just be noise.

- `metadata`: an arbitrary, potentially large/unbounded JSON blob,
  same reasoning as account_log.py excluding User.metadata and
  profile_info_log.py excluding ProfileInfo.metadata.

- `verified_by` / `archived_by`: FK fields. Same policy as every other
  *_log.py's TRACKED_FIELDS -- who performed a verify()/soft_delete()
  call is exactly what this log's own `performed_by` column is for
  (see "ATTACHING CONTEXT" below), so it isn't lost, just recorded in
  the right place instead of duplicated into `changes`.

Unlike ProfileInfo, BusinessInfo has no ManyToManyFields and no
FieldFile fields, so this module doesn't need the m2m_changed carve-out
or the _normalize()-to-path-string handling that profile_info_log.py
needs for profile_photo/profile_cover_photo -- every tracked field
here is a plain scalar (str/bool/int/Decimal/date/datetime), so the
diff can compare and serialize values as-is. DjangoJSONEncoder already
knows how to serialize Decimal (annual_revenue) and date (founded_date)
without any extra handling.

A NOTE ON SENSITIVE FIELDS
------------------------------
`registration_number` and `tax_id` are meaningful PII, and this log
will store their old/new values in plaintext in `changes` the same way
it does for every other tracked field -- there's no special masking
here, consistent with how account_log.py logs email/phone changes
in full rather than redacting them. If that's not acceptable for this
data, the fix is to remove those two field names from TRACKED_FIELDS
below (they'll then simply never appear in `changes`, and a save that
only touches those two fields alongside no other tracked field will
write no log row at all) -- not to be silently redacted here, since a
half-masked log entry is often more confusing than no entry.

ATTACHING CONTEXT TO A SAVE (optional, no middleware required)
----------------------------------------------------------------
Same convention as the other *_log.py files -- set these transient
attributes on the instance right before calling .save() if you want to
record who made the change (e.g. an admin verifying someone else's
business):

    business.verify(verified_by_user=None, save=False)
    business._business_log_actor = request.user
    business._business_log_ip = request.META.get("REMOTE_ADDR")
    business._business_log_context = {"source": "admin_panel"}
    business.save()

Leave them unset for the common case (a user editing their own
business record) -- performed_by simply stays None, which reads as
"self-service / system".

WIRING THIS UP (required)
---------------------------
Signal receivers only connect once this module is actually imported
somewhere at app-loading time. Add it next to the other *_log imports:

    class CustomerConfig(AppConfig):
        def ready(self):
            import apps.customer.signals
            import apps.customer.models.account_log
            import apps.customer.models.contact_info_log
            import apps.customer.models.location_info_log
            import apps.customer.models.profile_info_log
            import apps.customer.models.business_info_log   # <-- add this line

MIGRATION
---------
This file adds a new model/table -- run makemigrations/migrate after
adding it, same as any other new model.

A NOTE ON CASCADE + hard_delete()
-----------------------------------
`BusinessInfoLog.business` below is on_delete=CASCADE, matching the
choice made for AccountLog.user / ContactInfoLog.contact /
LocationInfoLog.location / ProfileInfoLog.profile (see
contact_info_log.py's docstring for the full reasoning). In short:
normal User deletion never actually removes rows anymore (it routes
through soft_delete()), so this is moot day-to-day. Only the explicit
escape hatch, User.objects.filter(...).hard_delete(), performs a real
delete that cascades through BusinessInfo.user and then through this
table, removing that business's history along with it. Note
business_info.py's own docstring already flags that User.soft_delete()
doesn't currently cascade into archiving BusinessInfo at all -- this
log table inherits that same gap by extension (a soft-deleted user's
business history simply sits untouched, same as the BusinessInfo row
itself).
"""

import logging

from django.conf import settings
from django.core.serializers.json import DjangoJSONEncoder
from django.db import models
from django.db.models.signals import post_save, pre_save
from django.dispatch import receiver
from django.utils.translation import gettext_lazy as _

logger = logging.getLogger(__name__)


# ====================================================================
# TRACKED FIELDS
# ====================================================================
# Scalar BusinessInfo fields worth a history entry when they change.
# Keep this list curated -- see the module docstring for what's
# deliberately excluded and why.
TRACKED_FIELDS = [
    "business_name",
    "legal_name",
    "business_type",
    "industry",
    "description",
    "founded_date",
    "registration_number",
    "tax_id",
    "business_email",
    "business_phone",
    "website",
    "employee_count",
    "annual_revenue",
    "currency",
    "is_business_verified",
    "verified_at",
    "is_business_public",
    "is_business_archived",
    "archived_at",
]

# Grouped subsets used by _classify_action below.
_IDENTITY_FIELDS = {"business_name", "legal_name", "business_type"}
_DESCRIPTIVE_FIELDS = {"industry", "description", "founded_date"}
_REGISTRATION_FIELDS = {"registration_number", "tax_id"}
_CONTACT_FIELDS = {"business_email", "business_phone", "website"}
_FINANCIAL_FIELDS = {"employee_count", "annual_revenue", "currency"}


# ====================================================================
# MANAGER
# ====================================================================

class BusinessInfoLogManager(models.Manager):

    def for_business(self, business):
        """All log entries about a given BusinessInfo, most recent first."""
        return self.filter(business=business)

    def for_user(self, user):
        """All log entries about a given user's business, most recent first."""
        return self.filter(business__user=user)

    def recent(self, limit: int = 50):
        return self.order_by("-created_at")[:limit]

    def by_action(self, action: str):
        return self.filter(action=action)

    def record(
        self,
        *,
        business,
        action: str,
        changes: dict = None,
        performed_by=None,
        ip_address: str = None,
        user_agent: str = None,
        metadata: dict = None,
    ):
        """
        Explicit logging entry point -- used internally by the post_save
        signal handler below, but also safe to call directly for events
        that don't go through BusinessInfo.save() at all (e.g. a bulk
        admin action, or something you want logged with a hand-written
        message rather than a field diff).
        """
        return self.create(
            business=business,
            action=action,
            changes=changes or {},
            performed_by=performed_by,
            ip_address=ip_address,
            user_agent=user_agent,
            metadata=metadata or {},
        )


# ====================================================================
# MODEL
# ====================================================================

class BusinessInfoLog(models.Model):
    """One row per BusinessInfo save() that changed at least one tracked field."""

    class Action(models.TextChoices):
        CREATED = 'created', _('Business Created')
        UPDATED = 'updated', _('Business Updated')
        ARCHIVED = 'archived', _('Business Archived')
        RESTORED = 'restored', _('Business Restored')
        VERIFIED = 'verified', _('Business Verified')
        UNVERIFIED = 'unverified', _('Business Unverified')
        MADE_PUBLIC = 'made_public', _('Business Made Public')
        MADE_PRIVATE = 'made_private', _('Business Made Private')
        IDENTITY_CHANGED = 'identity_changed', _('Name/Legal Name/Type Changed')
        DESCRIPTION_CHANGED = 'description_changed', _('Industry/Description/Founded Date Changed')
        REGISTRATION_CHANGED = 'registration_changed', _('Registration Number/Tax ID Changed')
        CONTACT_CHANGED = 'contact_changed', _('Business Email/Phone/Website Changed')
        FINANCIALS_CHANGED = 'financials_changed', _('Employee Count/Revenue/Currency Changed')

    # NOTE: FK target assumes app_label "customer" (matching
    # apps/customer/models/business_info.py's location). Adjust the
    # string below if the app is registered under a different label.
    business = models.ForeignKey(
        'customer.BusinessInfo',
        on_delete=models.CASCADE,
        related_name="business_logs",
        help_text=_("The BusinessInfo row this log entry is about"),
    )

    action = models.CharField(
        _("Action"),
        max_length=30,
        choices=Action.choices,
        default=Action.UPDATED,
        db_index=True,
    )

    # {field_name: {"old": ..., "new": ...}} for every tracked field that
    # actually changed. On CREATED, "old" is always None.
    changes = models.JSONField(
        _("Changes"),
        default=dict,
        blank=True,
        encoder=DjangoJSONEncoder,
        help_text=_("Field-level diff: {field: {'old': ..., 'new': ...}}"),
    )

    performed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="business_info_logs_performed",
        help_text=_("Who performed this change, if known. None = self-service or system."),
    )

    ip_address = models.GenericIPAddressField(
        _("IP Address"), null=True, blank=True,
    )

    user_agent = models.TextField(
        _("User Agent"), null=True, blank=True,
    )

    metadata = models.JSONField(
        _("Metadata"),
        default=dict,
        blank=True,
        encoder=DjangoJSONEncoder,
        help_text=_("Extra context, e.g. which update_fields triggered this save"),
    )

    created_at = models.DateTimeField(_("Created At"), auto_now_add=True, db_index=True)

    objects = BusinessInfoLogManager()

    class Meta:
        verbose_name = _("Business Info Log")
        verbose_name_plural = _("Business Info Logs")
        db_table = "business_info_log"
        ordering = ["-created_at"]
        indexes = [
            # Matches the "history for this one business, newest first"
            # read pattern, same reasoning as the other *_log.py models'
            # composite index.
            models.Index(fields=["business", "-created_at"], name="business_log_business_idx"),
            models.Index(fields=["action", "-created_at"], name="business_log_action_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.get_action_display()} — {self.business.business_name} @ {self.created_at:%Y-%m-%d %H:%M}"


# ====================================================================
# DIFFING HELPERS
# ====================================================================

def _snapshot(instance) -> dict:
    """Current in-memory values of every tracked field on `instance`."""
    return {field: getattr(instance, field) for field in TRACKED_FIELDS}


def _diff(before: dict, after: dict) -> dict:
    """Only the tracked fields whose value actually changed."""
    return {
        field: {"old": before.get(field), "new": after[field]}
        for field in TRACKED_FIELDS
        if before.get(field) != after[field]
    }


def _classify_action(diff: dict, created: bool) -> str:
    """
    Best-effort, human-readable action label for a diff. This is a
    heuristic over which fields changed, not a guarantee of intent --
    if a save happens to touch multiple tracked fields at once,
    whichever check matches first below wins. Falls back to the
    generic UPDATED when nothing more specific applies.

    Status-flag checks (archived/verified/public) come first since
    those are the highest-signal, most deliberate actions in this
    model (each has its own dedicated method in business_info.py),
    followed by identity/descriptive/registration/contact/financial
    field groups, roughly in order of how consequential a change to
    that group tends to be.
    """
    if created:
        return BusinessInfoLog.Action.CREATED

    if "is_business_archived" in diff:
        return (
            BusinessInfoLog.Action.ARCHIVED
            if diff["is_business_archived"]["new"]
            else BusinessInfoLog.Action.RESTORED
        )

    if "is_business_verified" in diff:
        return (
            BusinessInfoLog.Action.VERIFIED
            if diff["is_business_verified"]["new"]
            else BusinessInfoLog.Action.UNVERIFIED
        )

    if "is_business_public" in diff:
        return (
            BusinessInfoLog.Action.MADE_PUBLIC
            if diff["is_business_public"]["new"]
            else BusinessInfoLog.Action.MADE_PRIVATE
        )

    if _IDENTITY_FIELDS & diff.keys():
        return BusinessInfoLog.Action.IDENTITY_CHANGED

    if _REGISTRATION_FIELDS & diff.keys():
        return BusinessInfoLog.Action.REGISTRATION_CHANGED

    if _CONTACT_FIELDS & diff.keys():
        return BusinessInfoLog.Action.CONTACT_CHANGED

    if _FINANCIAL_FIELDS & diff.keys():
        return BusinessInfoLog.Action.FINANCIALS_CHANGED

    if _DESCRIPTIVE_FIELDS & diff.keys():
        return BusinessInfoLog.Action.DESCRIPTION_CHANGED

    return BusinessInfoLog.Action.UPDATED


# ====================================================================
# SIGNALS
# ====================================================================

# String sender ('customer.BusinessInfo') matches the lazy-resolution
# pattern account_log.py uses for settings.AUTH_USER_MODEL, so this
# connects correctly regardless of module import order at app-loading
# time. Update the app_label in this string if BusinessInfo ever moves
# to a different app.
_BUSINESS_INFO_SENDER = 'customer.BusinessInfo'


@receiver(pre_save, sender=_BUSINESS_INFO_SENDER)
def _capture_before_state(sender, instance, **kwargs):
    """
    Stash the row's current (pre-update) tracked-field values on the
    instance so post_save can diff against them -- by the time
    post_save fires, the UPDATE has already happened and the old
    values are gone from the DB.

    Skipped entirely for not-yet-created instances. BusinessInfo.user
    is the primary key (not an auto-incrementing id), so `instance.pk
    is None` is the correct "does a row exist yet" check here too --
    same as ContactInfo/LocationInfo/ProfileInfo.

    Uses .values(...) rather than a full queryset fetch, same as the
    other *_log.py modules.
    """
    if instance.pk is None:
        instance._business_log_before = None
        return

    try:
        row = (
            sender._default_manager
            .filter(pk=instance.pk)
            .values(*TRACKED_FIELDS)
            .first()
        )
        instance._business_log_before = row
    except Exception:
        # Never let change-logging infrastructure break an actual save.
        logger.exception(
            "business_info_log: failed to capture before-state for business pk=%s",
            instance.pk,
        )
        instance._business_log_before = None


@receiver(post_save, sender=_BUSINESS_INFO_SENDER)
def _write_business_info_log(sender, instance, created, update_fields=None, **kwargs):
    """
    Diff before vs. after and write one BusinessInfoLog row if anything
    tracked actually changed. Wrapped defensively end-to-end: a bug or
    outage in the logging path must never surface as a failed save()
    for the user-facing action that triggered it.
    """
    try:
        after = _snapshot(instance)

        if created:
            diff = {field: {"old": None, "new": value} for field, value in after.items()}
        else:
            before = getattr(instance, "_business_log_before", None)
            if before is None:
                # No reliable before-state (pre_save capture failed, or
                # this is an edge case like an explicit pk pre-assigned
                # before first insert) -- nothing trustworthy to log.
                return
            diff = _diff(before, after)

        if not diff:
            return  # save() happened, but nothing tracked changed

        action = _classify_action(diff, created)

        context = getattr(instance, "_business_log_context", None) or {}
        metadata = {
            "update_fields": sorted(update_fields) if update_fields else None,
            **context,
        }

        BusinessInfoLog.objects.record(
            business=instance,
            action=action,
            changes=diff,
            performed_by=getattr(instance, "_business_log_actor", None),
            ip_address=getattr(instance, "_business_log_ip", None),
            user_agent=getattr(instance, "_business_log_user_agent", None),
            metadata=metadata,
        )
    except Exception:
        logger.exception(
            "business_info_log: failed to write BusinessInfoLog for business pk=%s",
            instance.pk,
        )
    finally:
        # Strip transient context so it can never leak into a later,
        # unrelated save() on the same in-memory instance.
        for attr in (
            "_business_log_before",
            "_business_log_actor",
            "_business_log_ip",
            "_business_log_user_agent",
            "_business_log_context",
        ):
            if hasattr(instance, attr):
                delattr(instance, attr)