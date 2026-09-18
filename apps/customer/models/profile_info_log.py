# apps/customer/models/profile_info_log.py

"""
Lightweight, automatic change history for the ProfileInfo model
(profile_info.py) -- same pattern as account_log.py / contact_info_log.py /
location_info_log.py, adapted to ProfileInfo's field set.

HOW IT WORKS
------------
pre_save/post_save signals on ProfileInfo diff a curated set of
"trackable" fields (TRACKED_FIELDS below) before vs. after every
save(), and write one ProfileInfoLog row per save that actually
changed something. This covers both full saves AND the narrow
save(update_fields=[...]) calls most of profile_info.py's helper
methods already use (verify(), suspend(), make_public(), feature(),
soft_delete(), etc.).

Deliberately NOT tracked:

- `uuid`, `profile_creation_time`, `profile_updated_time`: same
  bookkeeping rationale as every other *_log.py in this app -- the
  first never changes, the rest change on every save (or are already
  implied by this log row's own `created_at`) and would just be noise.

- `metadata`: an arbitrary, potentially large/unbounded JSON blob,
  same reasoning as account_log.py excluding User.metadata.

- `profile_views` / `last_viewed_at`: unlike account.py's
  `failed_login_attempts` (tracked, because failed logins are a
  meaningful security signal worth a few log rows), `profile_views`
  increments on literally every profile page view via
  increment_view_count() -- tracking it would write one ProfileInfoLog
  row per view, which is exactly the "changes on every request, just
  noise" case account_log.py's docstring warns about.

- `verified_by` / `archived_by`: FK fields. Same policy as
  account_log.py's TRACKED_FIELDS ("FK fields ... are out of scope
  too, to keep the diff trivially comparable and JSON-safe") -- who
  performed a verify()/soft_delete() call is exactly what this log's
  own `performed_by` column is for (see the "ATTACHING CONTEXT" section
  below), so it isn't lost, just recorded in the right place instead
  of duplicated into `changes`.

- `followers` / `following` / `blocked_users`: these are
  ManyToManyFields, which don't participate in pre_save/post_save at
  all -- Django fires `m2m_changed` for them instead, on a different
  signal with a different payload shape (pk_set of added/removed
  related objects, not an old/new scalar pair). Wiring that up is a
  meaningfully different piece of work from the diff-based logging
  here, so it's out of scope for this file. If per-follow/block audit
  history is wanted, it needs its own m2m_changed receiver(s) --
  not shoehorned into TRACKED_FIELDS, which assumes scalar values.

`profile_photo` / `profile_cover_photo` ARE tracked, but as their
storage path (FieldFile.name) rather than the FieldFile object itself
-- see `_normalize()` below. FieldFile instances aren't meaningfully
comparable or JSON-serializable on their own, but the underlying path
string is both, and "the photo changed" is exactly the kind of event
worth a log row.

ATTACHING CONTEXT TO A SAVE (optional, no middleware required)
----------------------------------------------------------------
Same convention as the other *_log.py files -- set these transient
attributes on the instance right before calling .save() if you want to
record who made the change (e.g. an admin verifying or suspending
someone else's profile):

    profile.suspend(reason="policy violation", save=False)
    profile._profile_log_actor = request.user
    profile._profile_log_ip = request.META.get("REMOTE_ADDR")
    profile._profile_log_context = {"source": "admin_panel"}
    profile.save()

Leave them unset for the common case (a user editing their own
profile) -- performed_by simply stays None, which reads as
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
            import apps.customer.models.profile_info_log   # <-- add this line

MIGRATION
---------
This file adds a new model/table -- run makemigrations/migrate after
adding it, same as any other new model.

A NOTE ON CASCADE + hard_delete()
-----------------------------------
`ProfileInfoLog.profile` below is on_delete=CASCADE, matching the
choice made for AccountLog.user / ContactInfoLog.contact /
LocationInfoLog.location (see contact_info_log.py's docstring for the
full reasoning). In short: normal User deletion never actually removes
rows anymore (it routes through soft_delete()), so this is moot
day-to-day. Only the explicit escape hatch,
User.objects.filter(...).hard_delete(), performs a real delete that
cascades through ProfileInfo.user and then through this table,
removing that profile's history along with it.
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
# Scalar ProfileInfo fields worth a history entry when they change.
# Keep this list curated -- see the module docstring for what's
# deliberately excluded and why.
TRACKED_FIELDS = [
    "profile_name",
    "profile_name_slug",
    "profile_bio",
    "profile_photo",
    "profile_cover_photo",
    "profile_gender",
    "profile_dob",
    "profile_language",
    "is_profile_verified",
    "verified_at",
    "is_profile_public",
    "is_profile_featured",
    "is_profile_suspended",
    "suspended_at",
    "suspension_reason",
    "show_email",
    "show_phone",
    "show_dob",
    "show_age",
    "show_location",
    "show_followers",
    "show_following",
    "allow_messages",
    "allow_follow",
    "notify_on_follow",
    "notify_on_message",
    "email_notifications",
    "sms_notifications",
    "is_profile_archived",
    "archived_at",
]

# Grouped subsets used by _classify_action below.
_PHOTO_FIELDS = {"profile_photo", "profile_cover_photo"}
_IDENTITY_FIELDS = {"profile_name", "profile_name_slug"}
_PRIVACY_FIELDS = {
    "show_email", "show_phone", "show_dob", "show_age", "show_location",
    "show_followers", "show_following", "allow_messages", "allow_follow",
}
_NOTIFICATION_FIELDS = {
    "notify_on_follow", "notify_on_message",
    "email_notifications", "sms_notifications",
}

# FieldFile-bearing fields need their value normalized to a plain path
# string before diffing/serializing -- see _normalize() below.
_FILE_FIELDS = _PHOTO_FIELDS


# ====================================================================
# MANAGER
# ====================================================================

class ProfileInfoLogManager(models.Manager):

    def for_profile(self, profile):
        """All log entries about a given ProfileInfo, most recent first."""
        return self.filter(profile=profile)

    def for_user(self, user):
        """All log entries about a given user's profile, most recent first."""
        return self.filter(profile__user=user)

    def recent(self, limit: int = 50):
        return self.order_by("-created_at")[:limit]

    def by_action(self, action: str):
        return self.filter(action=action)

    def record(
        self,
        *,
        profile,
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
        that don't go through ProfileInfo.save() at all (e.g. a bulk
        admin action, an m2m_changed-based follow/block log if one is
        added later, or something you want logged with a hand-written
        message rather than a field diff).
        """
        return self.create(
            profile=profile,
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

class ProfileInfoLog(models.Model):
    """One row per ProfileInfo save() that changed at least one tracked field."""

    class Action(models.TextChoices):
        CREATED = 'created', _('Profile Created')
        UPDATED = 'updated', _('Profile Updated')
        ARCHIVED = 'archived', _('Profile Archived')
        RESTORED = 'restored', _('Profile Restored')
        SUSPENDED = 'suspended', _('Profile Suspended')
        UNSUSPENDED = 'unsuspended', _('Profile Unsuspended')
        VERIFIED = 'verified', _('Profile Verified')
        UNVERIFIED = 'unverified', _('Profile Unverified')
        MADE_PUBLIC = 'made_public', _('Profile Made Public')
        MADE_PRIVATE = 'made_private', _('Profile Made Private')
        FEATURED = 'featured', _('Profile Featured')
        UNFEATURED = 'unfeatured', _('Profile Unfeatured')
        PHOTO_CHANGED = 'photo_changed', _('Photo Changed')
        IDENTITY_CHANGED = 'identity_changed', _('Name/Slug Changed')
        BIO_CHANGED = 'bio_changed', _('Bio Changed')
        PRIVACY_SETTINGS_CHANGED = 'privacy_settings_changed', _('Privacy Settings Changed')
        NOTIFICATION_SETTINGS_CHANGED = 'notification_settings_changed', _('Notification Settings Changed')

    # NOTE: FK target assumes app_label "customer" (matching
    # apps/customer/models/profile_info.py's location). Adjust the
    # string below if the app is registered under a different label.
    profile = models.ForeignKey(
        'customer.ProfileInfo',
        on_delete=models.CASCADE,
        related_name="profile_logs",
        help_text=_("The ProfileInfo row this log entry is about"),
    )

    action = models.CharField(
        _("Action"),
        max_length=30,
        choices=Action.choices,
        default=Action.UPDATED,
        db_index=True,
    )

    # {field_name: {"old": ..., "new": ...}} for every tracked field that
    # actually changed. On CREATED, "old" is always None. File fields
    # are stored as their path string (see _normalize()), so
    # DjangoJSONEncoder can serialize the whole dict without any manual
    # handling on our end.
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
        related_name="profile_info_logs_performed",
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

    objects = ProfileInfoLogManager()

    class Meta:
        verbose_name = _("Profile Info Log")
        verbose_name_plural = _("Profile Info Logs")
        db_table = "profile_info_log"
        ordering = ["-created_at"]
        indexes = [
            # Matches the "history for this one profile, newest first"
            # read pattern, same reasoning as the other *_log.py models'
            # composite index.
            models.Index(fields=["profile", "-created_at"], name="profile_log_profile_idx"),
            models.Index(fields=["action", "-created_at"], name="profile_log_action_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.get_action_display()} — {self.profile.user.email_or_phone} @ {self.created_at:%Y-%m-%d %H:%M}"


# ====================================================================
# DIFFING HELPERS
# ====================================================================

def _normalize(field: str, value):
    """
    FieldFile values (profile_photo / profile_cover_photo) aren't
    directly comparable across before/after snapshots in a meaningful
    way (two FieldFile instances wrapping the same underlying path
    aren't guaranteed to compare equal) and aren't JSON-serializable at
    all. Reduce them to their storage path string -- or None if no
    file is set -- which is both comparable and safe for
    DjangoJSONEncoder. Every other tracked field passes through
    unchanged.

    Values can arrive here two different shapes depending on the
    caller: `_snapshot()` reads through the model descriptor
    (getattr(instance, field)), which always yields a FieldFile; but
    `_capture_before_state()` reads via .values(), which returns the
    raw DB column -- already a plain str (or None) for FileFields.
    Handle both.
    """
    if field in _FILE_FIELDS:
        if not value:
            return None
        return value.name if hasattr(value, "name") else value
    return value


def _snapshot(instance) -> dict:
    """Current in-memory values of every tracked field on `instance`."""
    return {
        field: _normalize(field, getattr(instance, field))
        for field in TRACKED_FIELDS
    }


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

    Status-flag checks (archived/suspended/verified/public/featured)
    come first since those are the highest-signal, most deliberate
    actions in this model (each has its own dedicated method in
    profile_info.py), followed by content changes (photo/name/bio),
    then the two settings-group toggles.
    """
    if created:
        return ProfileInfoLog.Action.CREATED

    if "is_profile_archived" in diff:
        return (
            ProfileInfoLog.Action.ARCHIVED
            if diff["is_profile_archived"]["new"]
            else ProfileInfoLog.Action.RESTORED
        )

    if "is_profile_suspended" in diff:
        return (
            ProfileInfoLog.Action.SUSPENDED
            if diff["is_profile_suspended"]["new"]
            else ProfileInfoLog.Action.UNSUSPENDED
        )

    if "is_profile_verified" in diff:
        return (
            ProfileInfoLog.Action.VERIFIED
            if diff["is_profile_verified"]["new"]
            else ProfileInfoLog.Action.UNVERIFIED
        )

    if "is_profile_public" in diff:
        return (
            ProfileInfoLog.Action.MADE_PUBLIC
            if diff["is_profile_public"]["new"]
            else ProfileInfoLog.Action.MADE_PRIVATE
        )

    if "is_profile_featured" in diff:
        return (
            ProfileInfoLog.Action.FEATURED
            if diff["is_profile_featured"]["new"]
            else ProfileInfoLog.Action.UNFEATURED
        )

    if _PHOTO_FIELDS & diff.keys():
        return ProfileInfoLog.Action.PHOTO_CHANGED

    if _IDENTITY_FIELDS & diff.keys():
        return ProfileInfoLog.Action.IDENTITY_CHANGED

    if "profile_bio" in diff:
        return ProfileInfoLog.Action.BIO_CHANGED

    if _PRIVACY_FIELDS & diff.keys():
        return ProfileInfoLog.Action.PRIVACY_SETTINGS_CHANGED

    if _NOTIFICATION_FIELDS & diff.keys():
        return ProfileInfoLog.Action.NOTIFICATION_SETTINGS_CHANGED

    return ProfileInfoLog.Action.UPDATED


# ====================================================================
# SIGNALS
# ====================================================================

# String sender ('customer.ProfileInfo') matches the lazy-resolution
# pattern account_log.py uses for settings.AUTH_USER_MODEL, so this
# connects correctly regardless of module import order at app-loading
# time. Update the app_label in this string if ProfileInfo ever moves
# to a different app.
_PROFILE_INFO_SENDER = 'customer.ProfileInfo'


@receiver(pre_save, sender=_PROFILE_INFO_SENDER)
def _capture_before_state(sender, instance, **kwargs):
    """
    Stash the row's current (pre-update) tracked-field values on the
    instance so post_save can diff against them -- by the time
    post_save fires, the UPDATE has already happened and the old
    values are gone from the DB.

    Skipped entirely for not-yet-created instances. ProfileInfo.user
    is the primary key (not an auto-incrementing id), so `instance.pk
    is None` is the correct "does a row exist yet" check here too --
    same as ContactInfo/LocationInfo.

    Uses .values(...) rather than a full queryset fetch, same as the
    other *_log.py modules -- except profile_photo/profile_cover_photo
    come back from .values() as plain path strings already (not
    FieldFile instances), so they're passed through _normalize() as-is
    for symmetry with the after-snapshot in post_save, which is
    already a no-op for a plain string.
    """
    if instance.pk is None:
        instance._profile_log_before = None
        return

    try:
        row = (
            sender._default_manager
            .filter(pk=instance.pk)
            .values(*TRACKED_FIELDS)
            .first()
        )
        if row is not None:
            row = {field: _normalize(field, value) for field, value in row.items()}
        instance._profile_log_before = row
    except Exception:
        # Never let change-logging infrastructure break an actual save.
        logger.exception(
            "profile_info_log: failed to capture before-state for profile pk=%s",
            instance.pk,
        )
        instance._profile_log_before = None


@receiver(post_save, sender=_PROFILE_INFO_SENDER)
def _write_profile_info_log(sender, instance, created, update_fields=None, **kwargs):
    """
    Diff before vs. after and write one ProfileInfoLog row if anything
    tracked actually changed. Wrapped defensively end-to-end: a bug or
    outage in the logging path must never surface as a failed save()
    for the user-facing action that triggered it.
    """
    try:
        after = _snapshot(instance)

        if created:
            diff = {field: {"old": None, "new": value} for field, value in after.items()}
        else:
            before = getattr(instance, "_profile_log_before", None)
            if before is None:
                # No reliable before-state (pre_save capture failed, or
                # this is an edge case like an explicit pk pre-assigned
                # before first insert) -- nothing trustworthy to log.
                return
            diff = _diff(before, after)

        if not diff:
            return  # save() happened, but nothing tracked changed

        action = _classify_action(diff, created)

        context = getattr(instance, "_profile_log_context", None) or {}
        metadata = {
            "update_fields": sorted(update_fields) if update_fields else None,
            **context,
        }

        ProfileInfoLog.objects.record(
            profile=instance,
            action=action,
            changes=diff,
            performed_by=getattr(instance, "_profile_log_actor", None),
            ip_address=getattr(instance, "_profile_log_ip", None),
            user_agent=getattr(instance, "_profile_log_user_agent", None),
            metadata=metadata,
        )
    except Exception:
        logger.exception(
            "profile_info_log: failed to write ProfileInfoLog for profile pk=%s",
            instance.pk,
        )
    finally:
        # Strip transient context so it can never leak into a later,
        # unrelated save() on the same in-memory instance.
        for attr in (
            "_profile_log_before",
            "_profile_log_actor",
            "_profile_log_ip",
            "_profile_log_user_agent",
            "_profile_log_context",
        ):
            if hasattr(instance, attr):
                delattr(instance, attr)