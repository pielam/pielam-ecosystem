# apps/customer/models/location_info_log.py

"""
Lightweight, automatic change history for the LocationInfo model
(location_info.py) -- same pattern as account_log.py / contact_info_log.py,
adapted to LocationInfo's field set.

HOW IT WORKS
------------
pre_save/post_save signals on LocationInfo diff a curated set of
"trackable" fields (TRACKED_FIELDS below) before vs. after every
save(), and write one LocationInfoLog row per save that actually
changed something. LocationInfo.save() always calls full_clean() (like
ContactInfo, unlike User), so every get_or_create_for_user() /
attribute-set-then-save() call goes through the same signal path --
there's no separate "partial save" case to worry about here.

Deliberately NOT tracked: `uuid` (immutable after creation, nothing
useful to log) and `created_at` / `updated_at` (pure bookkeeping --
the former never changes and `created_at` on this log model already
tells you when the CREATED row happened; the latter changes on every
save and would just be noise). Everything else -- country, address
fields, and the lat/long pair -- IS tracked, since a change to any of
them is exactly the kind of PII-touching event this log exists to
surface (see location_info.py's own docstring on this data being
"meaningfully sensitive PII").

ATTACHING CONTEXT TO A SAVE (optional, no middleware required)
----------------------------------------------------------------
Same convention as account_log.py / contact_info_log.py -- set these
transient attributes on the instance right before calling .save() if
you want to record who made the change:

    location.city = "Dhaka"
    location._location_log_actor = request.user
    location._location_log_ip = request.META.get("REMOTE_ADDR")
    location._location_log_context = {"source": "settings_page"}
    location.save()

Leave them unset for the common case (the user editing their own
location) -- performed_by simply stays None, which reads as
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
            import apps.customer.models.location_info_log   # <-- add this line

MIGRATION
---------
This file adds a new model/table -- run makemigrations/migrate after
adding it, same as any other new model.

RELATIONSHIP TO THE "NOT YET WIRED" GDPR NOTE IN location_info.py
------------------------------------------------------------------
location_info.py's docstring flags that User.soft_delete() doesn't yet
know about LocationInfo -- that's still true after adding this log.
This model only records *that* a change happened; it doesn't change
what soft_delete() touches. If/when that wiring is added (an explicit
signal in signals.py, or a call from soft_delete() itself), whatever
it does to LocationInfo (archive it, null out fields, etc.) will
naturally produce its own LocationInfoLog row for free, the same way
User.soft_delete() already produces an AccountLog row today.

A NOTE ON CASCADE + hard_delete()
-----------------------------------
`LocationInfoLog.location` below is on_delete=CASCADE, matching the
choice made for ContactInfoLog.contact (see that file's docstring for
the full reasoning). In short: normal User deletion never actually
removes rows anymore (it routes through soft_delete()), so this is
moot day-to-day. Only the explicit escape hatch,
User.objects.filter(...).hard_delete(), performs a real delete that
cascades through LocationInfo.user and then through this table,
removing that location's history along with it.
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
# Scalar LocationInfo fields worth a history entry when they change.
# Keep this list curated -- see the module docstring for what's
# deliberately excluded and why.
TRACKED_FIELDS = [
    "country",
    "country_code",
    "state",
    "city",
    "address",
    "present_address",
    "permanent_address",
    "postal_code",
    "latitude",
    "longitude",
]

# Grouped subsets used by _classify_action below, built from
# TRACKED_FIELDS-adjacent names so a new field added to one of these
# groups is an explicit, visible decision rather than something that
# just silently falls into the generic UPDATED bucket.
_REGION_FIELDS = {"country", "country_code", "state", "city"}
_ADDRESS_FIELDS = {"address", "present_address", "permanent_address", "postal_code"}
_COORDINATE_FIELDS = {"latitude", "longitude"}


# ====================================================================
# MANAGER
# ====================================================================

class LocationInfoLogManager(models.Manager):

    def for_location(self, location):
        """All log entries about a given LocationInfo, most recent first."""
        return self.filter(location=location)

    def for_user(self, user):
        """All log entries about a given user's location, most recent first."""
        return self.filter(location__user=user)

    def recent(self, limit: int = 50):
        return self.order_by("-created_at")[:limit]

    def by_action(self, action: str):
        return self.filter(action=action)

    def record(
        self,
        *,
        location,
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
        that don't go through LocationInfo.save() at all (e.g. a bulk
        admin action, or something you want logged with a hand-written
        message rather than a field diff).
        """
        return self.create(
            location=location,
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

class LocationInfoLog(models.Model):
    """One row per LocationInfo save() that changed at least one tracked field."""

    class Action(models.TextChoices):
        CREATED = 'created', _('Location Info Created')
        UPDATED = 'updated', _('Location Info Updated')
        REGION_CHANGED = 'region_changed', _('Country/State/City Changed')
        ADDRESS_CHANGED = 'address_changed', _('Address Changed')
        COORDINATES_SET = 'coordinates_set', _('Coordinates Set')
        COORDINATES_CHANGED = 'coordinates_changed', _('Coordinates Changed')
        COORDINATES_CLEARED = 'coordinates_cleared', _('Coordinates Cleared')

    # NOTE: FK target assumes app_label "customer" (matching
    # apps/customer/models/location_info.py's location). Adjust the
    # string below if the app is registered under a different label.
    location = models.ForeignKey(
        'customer.LocationInfo',
        on_delete=models.CASCADE,
        related_name="location_logs",
        help_text=_("The LocationInfo row this log entry is about"),
    )

    action = models.CharField(
        _("Action"),
        max_length=25,
        choices=Action.choices,
        default=Action.UPDATED,
        db_index=True,
    )

    # {field_name: {"old": ..., "new": ...}} for every tracked field that
    # actually changed. On CREATED, "old" is always None. DjangoJSONEncoder
    # handles datetime/date/UUID/Decimal values (including the
    # latitude/longitude DecimalFields) without any manual serialization
    # on our end.
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
        related_name="location_info_logs_performed",
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

    objects = LocationInfoLogManager()

    class Meta:
        verbose_name = _("Location Info Log")
        verbose_name_plural = _("Location Info Logs")
        db_table = "location_info_log"
        ordering = ["-created_at"]
        indexes = [
            # Matches the "history for this one location, newest first"
            # read pattern, same reasoning as AccountLog/ContactInfoLog's
            # composite index.
            models.Index(fields=["location", "-created_at"], name="location_log_loc_idx"),
            models.Index(fields=["action", "-created_at"], name="location_log_action_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.get_action_display()} — {self.location.user.email_or_phone} @ {self.created_at:%Y-%m-%d %H:%M}"


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

    Coordinates are checked first (and given three distinct labels --
    set/changed/cleared) since latitude/longitude only ever move
    together as a pair (see LocationInfo.clean()'s "both set or both
    empty" invariant), which makes their transition unambiguous in a
    way plain field-presence checks for the other groups aren't.
    """
    if created:
        return LocationInfoLog.Action.CREATED

    if _COORDINATE_FIELDS & diff.keys():
        # Either field changing implies both were touched together
        # (enforced by clean()), so it's enough to inspect one.
        sample = diff.get("latitude") or diff.get("longitude")
        was_set = sample["old"] is not None
        is_set = sample["new"] is not None
        if not was_set and is_set:
            return LocationInfoLog.Action.COORDINATES_SET
        if was_set and not is_set:
            return LocationInfoLog.Action.COORDINATES_CLEARED
        return LocationInfoLog.Action.COORDINATES_CHANGED

    if _REGION_FIELDS & diff.keys():
        return LocationInfoLog.Action.REGION_CHANGED

    if _ADDRESS_FIELDS & diff.keys():
        return LocationInfoLog.Action.ADDRESS_CHANGED

    return LocationInfoLog.Action.UPDATED


# ====================================================================
# SIGNALS
# ====================================================================

# String sender ('customer.LocationInfo') matches the lazy-resolution
# pattern account_log.py uses for settings.AUTH_USER_MODEL, so this
# connects correctly regardless of module import order at app-loading
# time. Update the app_label in this string if LocationInfo ever moves
# to a different app.
_LOCATION_INFO_SENDER = 'customer.LocationInfo'


@receiver(pre_save, sender=_LOCATION_INFO_SENDER)
def _capture_before_state(sender, instance, **kwargs):
    """
    Stash the row's current (pre-update) tracked-field values on the
    instance so post_save can diff against them -- by the time
    post_save fires, the UPDATE has already happened and the old
    values are gone from the DB.

    Skipped entirely for not-yet-created instances. LocationInfo.user
    is the primary key (not an auto-incrementing id), so `instance.pk
    is None` is the correct "does a row exist yet" check here too --
    it's None until a `user` has actually been assigned, same as
    ContactInfo.
    """
    if instance.pk is None:
        instance._location_log_before = None
        return

    try:
        instance._location_log_before = (
            sender._default_manager
            .filter(pk=instance.pk)
            .values(*TRACKED_FIELDS)
            .first()
        )
    except Exception:
        # Never let change-logging infrastructure break an actual save.
        logger.exception(
            "location_info_log: failed to capture before-state for location pk=%s",
            instance.pk,
        )
        instance._location_log_before = None


@receiver(post_save, sender=_LOCATION_INFO_SENDER)
def _write_location_info_log(sender, instance, created, update_fields=None, **kwargs):
    """
    Diff before vs. after and write one LocationInfoLog row if anything
    tracked actually changed. Wrapped defensively end-to-end: a bug or
    outage in the logging path must never surface as a failed save()
    for the user-facing action that triggered it.
    """
    try:
        after = _snapshot(instance)

        if created:
            diff = {field: {"old": None, "new": value} for field, value in after.items()}
        else:
            before = getattr(instance, "_location_log_before", None)
            if before is None:
                # No reliable before-state (pre_save capture failed, or
                # this is an edge case like an explicit pk pre-assigned
                # before first insert) -- nothing trustworthy to log.
                return
            diff = _diff(before, after)

        if not diff:
            return  # save() happened, but nothing tracked changed

        action = _classify_action(diff, created)

        context = getattr(instance, "_location_log_context", None) or {}
        metadata = {
            "update_fields": sorted(update_fields) if update_fields else None,
            **context,
        }

        LocationInfoLog.objects.record(
            location=instance,
            action=action,
            changes=diff,
            performed_by=getattr(instance, "_location_log_actor", None),
            ip_address=getattr(instance, "_location_log_ip", None),
            user_agent=getattr(instance, "_location_log_user_agent", None),
            metadata=metadata,
        )
    except Exception:
        logger.exception(
            "location_info_log: failed to write LocationInfoLog for location pk=%s",
            instance.pk,
        )
    finally:
        # Strip transient context so it can never leak into a later,
        # unrelated save() on the same in-memory instance.
        for attr in (
            "_location_log_before",
            "_location_log_actor",
            "_location_log_ip",
            "_location_log_user_agent",
            "_location_log_context",
        ):
            if hasattr(instance, attr):
                delattr(instance, attr)