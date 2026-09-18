# apps/customer/models/contact_info_log.py

"""
Lightweight, automatic change history for the ContactInfo model
(contact_info.py) -- same pattern as apps/customer/models/account_log.py,
adapted to ContactInfo's field set.

HOW IT WORKS
------------
pre_save/post_save signals on ContactInfo diff a curated set of
"trackable" fields (TRACKED_FIELDS below) before vs. after every save(),
and write one ContactInfoLog row per save that actually changed
something. ContactInfo.save() always calls full_clean() (unlike
User.save(), which skips it for narrow update_fields saves), so every
verify_phone() / verify_alt_email() / soft_delete() / etc. call goes
through the same signal path -- there's no separate "partial save"
case to worry about here.

Deliberately NOT tracked: `uuid` (immutable after creation, nothing
useful to log), `contact_creation_time` / `contact_updated_time` (pure
bookkeeping -- the former never changes, the latter changes on every
save and would just be noise, and `created_at` on this log model
already tells you when the CREATED row happened). Everything else --
including the actual handle/number values -- IS tracked, the same way
account_log.py tracks `email`/`phone`, because a change to e.g.
`phone_number` or `alt_email` is exactly the kind of event GDPR/support
tooling wants a history of.

ATTACHING CONTEXT TO A SAVE (optional, no middleware required)
----------------------------------------------------------------
Same convention as account_log.py -- set these transient attributes on
the instance right before calling .save() if you want to record who
made the change:

    contact.preferred_contact_method = ContactInfo.PreferredMethod.WHATSAPP
    contact._contact_log_actor = request.user
    contact._contact_log_ip = request.META.get("REMOTE_ADDR")
    contact._contact_log_context = {"source": "settings_page"}
    contact.save()

Leave them unset for the common case (the user editing their own
contact info) -- performed_by simply stays None, which reads as
"self-service / system".

WIRING THIS UP (required)
---------------------------
Signal receivers only connect once this module is actually imported
somewhere at app-loading time. Add it next to account_log's import:

    class CustomerConfig(AppConfig):
        def ready(self):
            import apps.customer.signals
            import apps.customer.models.account_log
            import apps.customer.models.contact_info_log   # <-- add this line

MIGRATION
---------
This file adds a new model/table -- run makemigrations/migrate after
adding it, same as any other new model.

A NOTE ON CASCADE + hard_delete()
-----------------------------------
`ContactInfoLog.contact` below is on_delete=CASCADE, the same choice
account_log.py made for `AccountLog.user`. In the normal flow this is
moot: User.delete() and User.objects.filter(...).delete() both route
through soft_delete() now (see account.py's changelog, fix #4), so the
User row -- and therefore, via ContactInfo.user's own CASCADE -- the
ContactInfo row is never actually removed from the DB, and this table
just accumulates history rows indefinitely. The one case where that
isn't true is the explicit escape hatch, User.objects.filter(...)
.hard_delete(): that performs a real row delete, Django's collector
cascades through ContactInfo.user (CASCADE) and then through
ContactInfoLog.contact (CASCADE), and the log history for that contact
info is deleted right along with it. That's consistent with what
already happens to AccountLog in the identical scenario, so no extra
handling is added here -- flagged only so it isn't a surprise later if
someone reaches for hard_delete() expecting logs to survive.
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
# Scalar ContactInfo fields worth a history entry when they change.
# Keep this list curated -- see the module docstring for what's
# deliberately excluded and why.
TRACKED_FIELDS = [
    "alt_email",
    "is_alt_email_verified",
    "alt_email_verified_at",
    "show_email",
    "phone_number",
    "is_phone_verified",
    "phone_verified_at",
    "show_phone",
    "whatsapp_number",
    "show_whatsapp",
    "telegram_username",
    "show_telegram",
    "signal_number",
    "show_signal",
    "viber_number",
    "show_viber",
    "wechat_id",
    "show_wechat",
    "line_id",
    "show_line",
    "imo_number",
    "show_imo",
    "skype_id",
    "show_skype",
    "discord_username",
    "show_discord",
    "facebook_messenger_username",
    "show_facebook_messenger",
    "instagram_username",
    "show_instagram",
    "snapchat_username",
    "show_snapchat",
    "twitter_username",
    "show_twitter",
    "linkedin_url",
    "show_linkedin",
    "slack_workspace_handle",
    "show_slack",
    "preferred_contact_method",
    "allow_contact_requests",
    "is_contact_public",
    "is_contact_archived",
    "archived_at",
]

# Fields whose change alone should be classified as a visibility change
# (a show_* toggle flipping), rather than falling through to generic
# UPDATED. Built from TRACKED_FIELDS so it can't drift out of sync as
# new show_* fields are added above.
_VISIBILITY_FIELDS = {f for f in TRACKED_FIELDS if f.startswith("show_")}


# ====================================================================
# MANAGER
# ====================================================================

class ContactInfoLogManager(models.Manager):

    def for_contact(self, contact):
        """All log entries about a given ContactInfo, most recent first."""
        return self.filter(contact=contact)

    def for_user(self, user):
        """All log entries about a given user's contact info, most recent first."""
        return self.filter(contact__user=user)

    def recent(self, limit: int = 50):
        return self.order_by("-created_at")[:limit]

    def by_action(self, action: str):
        return self.filter(action=action)

    def record(
        self,
        *,
        contact,
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
        that don't go through ContactInfo.save() at all (e.g. a bulk
        admin action, or something you want logged with a hand-written
        message rather than a field diff).
        """
        return self.create(
            contact=contact,
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

class ContactInfoLog(models.Model):
    """One row per ContactInfo save() that changed at least one tracked field."""

    class Action(models.TextChoices):
        CREATED = 'created', _('Contact Info Created')
        UPDATED = 'updated', _('Contact Info Updated')
        ARCHIVED = 'archived', _('Contact Info Archived')
        RESTORED = 'restored', _('Contact Info Restored')
        PHONE_VERIFIED = 'phone_verified', _('Phone Verified')
        PHONE_UNVERIFIED = 'phone_unverified', _('Phone Unverified')
        EMAIL_VERIFIED = 'email_verified', _('Alternate Email Verified')
        EMAIL_UNVERIFIED = 'email_unverified', _('Alternate Email Unverified')
        PREFERRED_METHOD_CHANGED = 'preferred_method_changed', _('Preferred Contact Method Changed')
        CONTACT_REQUESTS_ENABLED = 'contact_requests_enabled', _('Contact Requests Enabled')
        CONTACT_REQUESTS_DISABLED = 'contact_requests_disabled', _('Contact Requests Disabled')
        MADE_PUBLIC = 'made_public', _('Contact Info Made Public')
        MADE_PRIVATE = 'made_private', _('Contact Info Made Private')
        VISIBILITY_CHANGED = 'visibility_changed', _('Channel Visibility Changed')

    # NOTE: FK target assumes app_label "customer" (matching
    # apps/customer/models/contact_info.py's location). Adjust the
    # string below if the app is registered under a different label.
    contact = models.ForeignKey(
        'customer.ContactInfo',
        on_delete=models.CASCADE,
        related_name="contact_logs",
        help_text=_("The ContactInfo row this log entry is about"),
    )

    action = models.CharField(
        _("Action"),
        max_length=30,
        choices=Action.choices,
        default=Action.UPDATED,
        db_index=True,
    )

    # {field_name: {"old": ..., "new": ...}} for every tracked field that
    # actually changed. On CREATED, "old" is always None. DjangoJSONEncoder
    # handles datetime/date/UUID/Decimal values without any manual
    # serialization on our end.
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
        related_name="contact_info_logs_performed",
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

    objects = ContactInfoLogManager()

    class Meta:
        verbose_name = _("Contact Info Log")
        verbose_name_plural = _("Contact Info Logs")
        db_table = "contact_info_log"
        ordering = ["-created_at"]
        indexes = [
            # Matches the "history for this one contact, newest first"
            # read pattern, same reasoning as AccountLog's composite index.
            models.Index(fields=["contact", "-created_at"], name="contact_log_contact_idx"),
            models.Index(fields=["action", "-created_at"], name="contact_log_action_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.get_action_display()} â€” {self.contact.user.email_or_phone} @ {self.created_at:%Y-%m-%d %H:%M}"


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
    """
    if created:
        return ContactInfoLog.Action.CREATED

    if "is_contact_archived" in diff:
        return (
            ContactInfoLog.Action.ARCHIVED
            if diff["is_contact_archived"]["new"]
            else ContactInfoLog.Action.RESTORED
        )

    if "is_phone_verified" in diff:
        return (
            ContactInfoLog.Action.PHONE_VERIFIED
            if diff["is_phone_verified"]["new"]
            else ContactInfoLog.Action.PHONE_UNVERIFIED
        )

    if "is_alt_email_verified" in diff:
        return (
            ContactInfoLog.Action.EMAIL_VERIFIED
            if diff["is_alt_email_verified"]["new"]
            else ContactInfoLog.Action.EMAIL_UNVERIFIED
        )

    if "preferred_contact_method" in diff:
        return ContactInfoLog.Action.PREFERRED_METHOD_CHANGED

    if "allow_contact_requests" in diff:
        return (
            ContactInfoLog.Action.CONTACT_REQUESTS_ENABLED
            if diff["allow_contact_requests"]["new"]
            else ContactInfoLog.Action.CONTACT_REQUESTS_DISABLED
        )

    if "is_contact_public" in diff:
        return (
            ContactInfoLog.Action.MADE_PUBLIC
            if diff["is_contact_public"]["new"]
            else ContactInfoLog.Action.MADE_PRIVATE
        )

    if _VISIBILITY_FIELDS & diff.keys():
        return ContactInfoLog.Action.VISIBILITY_CHANGED

    return ContactInfoLog.Action.UPDATED


# ====================================================================
# SIGNALS
# ====================================================================

# String sender ('customer.ContactInfo') matches the lazy-resolution
# pattern account_log.py uses for settings.AUTH_USER_MODEL, so this
# connects correctly regardless of module import order at app-loading
# time. Update the app_label in this string if ContactInfo ever moves
# to a different app.
_CONTACT_INFO_SENDER = 'customer.ContactInfo'


@receiver(pre_save, sender=_CONTACT_INFO_SENDER)
def _capture_before_state(sender, instance, **kwargs):
    """
    Stash the row's current (pre-update) tracked-field values on the
    instance so post_save can diff against them -- by the time
    post_save fires, the UPDATE has already happened and the old
    values are gone from the DB.

    Skipped entirely for not-yet-created instances. ContactInfo.user
    is the primary key (not an auto-incrementing id), so `instance.pk
    is None` is the correct "does a row exist yet" check here too --
    it's None until a `user` has actually been assigned, same as any
    other unset pk.
    """
    if instance.pk is None:
        instance._contact_log_before = None
        return

    try:
        instance._contact_log_before = (
            sender._default_manager
            .filter(pk=instance.pk)
            .values(*TRACKED_FIELDS)
            .first()
        )
    except Exception:
        # Never let change-logging infrastructure break an actual save.
        logger.exception(
            "contact_info_log: failed to capture before-state for contact pk=%s",
            instance.pk,
        )
        instance._contact_log_before = None


@receiver(post_save, sender=_CONTACT_INFO_SENDER)
def _write_contact_info_log(sender, instance, created, update_fields=None, **kwargs):
    """
    Diff before vs. after and write one ContactInfoLog row if anything
    tracked actually changed. Wrapped defensively end-to-end: a bug or
    outage in the logging path must never surface as a failed save()
    for the user-facing action that triggered it.
    """
    try:
        after = _snapshot(instance)

        if created:
            diff = {field: {"old": None, "new": value} for field, value in after.items()}
        else:
            before = getattr(instance, "_contact_log_before", None)
            if before is None:
                # No reliable before-state (pre_save capture failed, or
                # this is an edge case like an explicit pk pre-assigned
                # before first insert) -- nothing trustworthy to log.
                return
            diff = _diff(before, after)

        if not diff:
            return  # save() happened, but nothing tracked changed

        action = _classify_action(diff, created)

        context = getattr(instance, "_contact_log_context", None) or {}
        metadata = {
            "update_fields": sorted(update_fields) if update_fields else None,
            **context,
        }

        ContactInfoLog.objects.record(
            contact=instance,
            action=action,
            changes=diff,
            performed_by=getattr(instance, "_contact_log_actor", None),
            ip_address=getattr(instance, "_contact_log_ip", None),
            user_agent=getattr(instance, "_contact_log_user_agent", None),
            metadata=metadata,
        )
    except Exception:
        logger.exception(
            "contact_info_log: failed to write ContactInfoLog for contact pk=%s",
            instance.pk,
        )
    finally:
        # Strip transient context so it can never leak into a later,
        # unrelated save() on the same in-memory instance.
        for attr in (
            "_contact_log_before",
            "_contact_log_actor",
            "_contact_log_ip",
            "_contact_log_user_agent",
            "_contact_log_context",
        ):
            if hasattr(instance, attr):
                delattr(instance, attr)
