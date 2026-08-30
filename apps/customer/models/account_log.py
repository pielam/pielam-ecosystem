# apps/customer/models/account_log.py

"""
Lightweight, automatic change history for the User model (account.py).

HOW IT WORKS
------------
pre_save/post_save signals on the User model diff a curated set of
"trackable" fields (TRACKED_FIELDS below) before vs. after every save(),
and write one AccountLog row per save that actually changed something.
This covers both full saves AND the narrow save(update_fields=[...])
calls most of account.py's helper methods already use
(record_successful_login, lock_account, verify_email, soft_delete,
change_role, etc.) -- which is exactly the traffic this model exists to
surface, since account.py's save() override intentionally skips
full_clean() (and therefore nothing else) for those partial saves.

Deliberately NOT tracked: `password` (a hash -- nothing useful to log
raw), `mfa_secret` (a secret; never log), `metadata` (an arbitrary,
potentially large/unbounded JSON blob), and pure bookkeeping fields
(`uuid`, `date_joined`, `updated_at`, `last_login`) that either never
change after creation or change on literally every request and would
just be noise. FK fields (`deleted_by`, `verified_by`, etc. -- none of
which live on User) are out of scope too; TRACKED_FIELDS is scalar
fields only, to keep the diff trivially comparable and JSON-safe.

ATTACHING CONTEXT TO A SAVE (optional, no middleware required)
----------------------------------------------------------------
If a caller wants to record *who* made a change (e.g. an admin editing
someone else's account, vs. the account editing itself) or the request
IP, set these transient attributes on the instance right before calling
.save(). They're read once by the post_save handler and then stripped
off the instance, so they never leak into a later, unrelated save() on
the same in-memory object:

    target_user.change_role(new_role, save=False)
    target_user._account_log_actor = request.user            # who did it
    target_user._account_log_ip = request.META.get("REMOTE_ADDR")
    target_user._account_log_context = {"source": "admin_panel"}
    target_user.save()

Leave them unset for the common case (a user's own action, e.g.
record_successful_login()) -- performed_by simply stays None, which
reads as "self-service / system".

WIRING THIS UP (required)
---------------------------
Signal receivers only connect once this module is actually imported
somewhere at app-loading time. Add it next to the existing signals
import in apps.py:

    class CustomerConfig(AppConfig):
        def ready(self):
            import apps.customer.signals
            import apps.customer.models.account_log   # <-- add this line

MIGRATION
---------
This file adds a new model/table -- run makemigrations/migrate after
adding it, same as any other new model.
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
# Scalar User fields worth a history entry when they change. Keep this
# list curated -- adding a field here means every change to it gets a
# permanent AccountLog row, so avoid anything high-churn/low-signal
# (see the module docstring for what's deliberately excluded and why).
TRACKED_FIELDS = [
    "email_or_phone",
    "email",
    "phone",
    "role",
    "account_status",
    "is_active",
    "is_staff",
    "is_superuser",
    "email_verified",
    "phone_verified",
    "mfa_enabled",
    "mfa_method",
    "failed_login_attempts",
    "locked_until",
    "last_login_ip",
    "last_login_user_agent",
    "password_changed_at",
    "require_password_change",
    "timezone",
    "language",
    "country",
    "currency",
    "marketing_consent",
    "terms_accepted",
    "privacy_accepted",
    "deleted_at",
]


# ====================================================================
# MANAGER
# ====================================================================

class AccountLogManager(models.Manager):

    def for_user(self, user):
        """All log entries about a given user, most recent first."""
        return self.filter(user=user)

    def recent(self, limit: int = 50):
        return self.order_by("-created_at")[:limit]

    def by_action(self, action: str):
        return self.filter(action=action)

    def record(
        self,
        *,
        user,
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
        that don't go through User.save() at all (e.g. a bulk admin
        action, or something you want logged with a hand-written message
        rather than a field diff).
        """
        return self.create(
            user=user,
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

class AccountLog(models.Model):
    """One row per User save() that changed at least one tracked field."""

    class Action(models.TextChoices):
        CREATED = 'created', _('Account Created')
        UPDATED = 'updated', _('Account Updated')
        DELETED = 'deleted', _('Account Soft-Deleted')
        RESTORED = 'restored', _('Account Restored')
        ROLE_CHANGED = 'role_changed', _('Role Changed')
        LOGIN_SUCCESS = 'login_success', _('Successful Login')
        LOGIN_FAILED = 'login_failed', _('Failed Login Attempt')
        LOCKED = 'locked', _('Account Locked')
        UNLOCKED = 'unlocked', _('Account Unlocked')
        PASSWORD_CHANGED = 'password_changed', _('Password Changed')
        MFA_ENABLED = 'mfa_enabled', _('MFA Enabled')
        MFA_DISABLED = 'mfa_disabled', _('MFA Disabled')
        EMAIL_VERIFIED = 'email_verified', _('Email Verified')
        PHONE_VERIFIED = 'phone_verified', _('Phone Verified')

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="account_logs",
        help_text=_("The account this log entry is about"),
    )

    action = models.CharField(
        _("Action"),
        max_length=20,
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
        related_name="account_logs_performed",
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

    objects = AccountLogManager()

    class Meta:
        verbose_name = _("Account Log")
        verbose_name_plural = _("Account Logs")
        db_table = "account_log"
        ordering = ["-created_at"]
        indexes = [
            # Matches the "history for this one user, newest first" read
            # pattern, same reasoning as ProfileViewLog's composite index.
            models.Index(fields=["user", "-created_at"], name="account_log_user_created_idx"),
            models.Index(fields=["action", "-created_at"], name="account_log_action_created_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.get_action_display()} — {self.user.email_or_phone} @ {self.created_at:%Y-%m-%d %H:%M}"


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
    if a save happens to touch multiple tracked fields at once, whichever
    check matches first below wins. Falls back to the generic UPDATED
    when nothing more specific applies.
    """
    if created:
        return AccountLog.Action.CREATED

    if "deleted_at" in diff:
        return AccountLog.Action.DELETED if diff["deleted_at"]["new"] else AccountLog.Action.RESTORED

    if "role" in diff:
        return AccountLog.Action.ROLE_CHANGED

    if "password_changed_at" in diff:
        return AccountLog.Action.PASSWORD_CHANGED

    if "mfa_enabled" in diff:
        return AccountLog.Action.MFA_ENABLED if diff["mfa_enabled"]["new"] else AccountLog.Action.MFA_DISABLED

    if "email_verified" in diff and diff["email_verified"]["new"]:
        return AccountLog.Action.EMAIL_VERIFIED

    if "phone_verified" in diff and diff["phone_verified"]["new"]:
        return AccountLog.Action.PHONE_VERIFIED

    if "locked_until" in diff:
        return AccountLog.Action.LOCKED if diff["locked_until"]["new"] else AccountLog.Action.UNLOCKED

    if "failed_login_attempts" in diff:
        old = diff["failed_login_attempts"]["old"] or 0
        new = diff["failed_login_attempts"]["new"] or 0
        if new == 0 and "last_login_ip" in diff:
            return AccountLog.Action.LOGIN_SUCCESS
        if new > old:
            return AccountLog.Action.LOGIN_FAILED

    return AccountLog.Action.UPDATED


# ====================================================================
# SIGNALS
# ====================================================================

# String sender (rather than importing User directly) matches the
# pattern already used in signals.py, and lets Django resolve it lazily
# regardless of module import order at app-loading time.

@receiver(pre_save, sender=settings.AUTH_USER_MODEL)
def _capture_before_state(sender, instance, **kwargs):
    """
    Stash the row's current (pre-update) tracked-field values on the
    instance so post_save can diff against them -- by the time post_save
    fires, the UPDATE has already happened and the old values are gone
    from the DB.

    Skipped entirely for not-yet-created instances (instance.pk is None):
    there's nothing in the DB to diff against, and post_save already
    knows `created=True` on its own.
    """
    if instance.pk is None:
        instance._account_log_before = None
        return

    try:
        instance._account_log_before = (
            sender._default_manager
            .filter(pk=instance.pk)
            .values(*TRACKED_FIELDS)
            .first()
        )
    except Exception:
        # Never let change-logging infrastructure break an actual save.
        logger.exception(
            "account_log: failed to capture before-state for user pk=%s",
            instance.pk,
        )
        instance._account_log_before = None


@receiver(post_save, sender=settings.AUTH_USER_MODEL)
def _write_account_log(sender, instance, created, update_fields=None, **kwargs):
    """
    Diff before vs. after and write one AccountLog row if anything
    tracked actually changed. Wrapped defensively end-to-end: a bug or
    outage in the logging path must never surface as a failed save()
    for the user-facing action that triggered it.
    """
    try:
        after = _snapshot(instance)

        if created:
            diff = {field: {"old": None, "new": value} for field, value in after.items()}
        else:
            before = getattr(instance, "_account_log_before", None)
            if before is None:
                # No reliable before-state (pre_save capture failed, or
                # this is an edge case like an explicit pk pre-assigned
                # before first insert) -- nothing trustworthy to log.
                return
            diff = _diff(before, after)

        if not diff:
            return  # save() happened, but nothing tracked changed

        action = _classify_action(diff, created)

        context = getattr(instance, "_account_log_context", None) or {}
        metadata = {
            "update_fields": sorted(update_fields) if update_fields else None,
            **context,
        }

        AccountLog.objects.record(
            user=instance,
            action=action,
            changes=diff,
            performed_by=getattr(instance, "_account_log_actor", None),
            ip_address=getattr(instance, "_account_log_ip", None),
            user_agent=getattr(instance, "_account_log_user_agent", None),
            metadata=metadata,
        )
    except Exception:
        logger.exception(
            "account_log: failed to write AccountLog for user pk=%s",
            instance.pk,
        )
    finally:
        # Strip transient context so it can never leak into a later,
        # unrelated save() on the same in-memory instance.
        for attr in (
            "_account_log_before",
            "_account_log_actor",
            "_account_log_ip",
            "_account_log_user_agent",
            "_account_log_context",
        ):
            if hasattr(instance, attr):
                delattr(instance, attr)