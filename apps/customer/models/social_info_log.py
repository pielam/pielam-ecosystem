# apps/customer/models/social_info_log.py

"""
Lightweight, automatic change history for the SocialInfo model
(social_info.py) -- same pattern as account_log.py / contact_info_log.py /
location_info_log.py / profile_info_log.py, adapted to SocialInfo's
field set.

HOW IT WORKS
------------
pre_save/post_save signals on SocialInfo diff a curated set of
"trackable" fields (TRACKED_FIELDS below) before vs. after every
save(), and write one SocialInfoLog row per save that actually changed
something.

TRACKED_FIELDS IS DERIVED, NOT HAND-MAINTAINED
------------------------------------------------
Unlike the other *_log.py files (which list their tracked fields out
by hand), TRACKED_FIELDS here is built from
`SocialInfo.PLATFORM_DOMAINS.keys()` plus `website_url` -- the exact
same construction social_info.py's own `all_links` property and
`export_data()` already use. SocialInfo is a wide, flat, ~34-field
model that's explicitly expected to grow (see that file's "ON THE
SHEER NUMBER OF FIELDS" note), so hand-copying the field list here
would be one more place to forget to update every time a platform is
added. Deriving it means a new platform field is tracked automatically
the moment it's added to PLATFORM_DOMAINS -- no changes needed in this
file.

Deliberately NOT tracked: `uuid`, `created_at`, `updated_at` -- same
bookkeeping rationale as every other *_log.py in this app.

WHY ACTIONS ARE COARSE-GRAINED (LINK_ADDED / LINK_CHANGED / LINK_REMOVED
/ MULTIPLE_LINKS_CHANGED) RATHER THAN PER-PLATFORM
------------------------------------------------------------------------
The other *_log.py models have a handful of fields each, so a specific
Action per meaningful field (VERIFIED, ARCHIVED, REGION_CHANGED, ...)
is legible as a fixed enum. SocialInfo has ~34 near-identical URL
fields; a per-platform action (INSTAGRAM_CHANGED, GITHUB_CHANGED, ...)
would make the Action enum grow every time a platform is added -- the
exact churn problem social_info.py's own docstring warns about for the
model's field list. Instead, `action` here answers "what kind of
change was this" (a link appearing, disappearing, being swapped for a
different one, or several changing at once) and `changes` (the JSON
diff) answers "which platform(s), specifically" -- callers who need to
filter by platform should query/filter on `changes` rather than on
`action`.

ATTACHING CONTEXT TO A SAVE (optional, no middleware required)
----------------------------------------------------------------
Same convention as the other *_log.py files -- set these transient
attributes on the instance right before calling .save() if you want to
record who made the change:

    social.instagram_url = "https://instagram.com/newhandle"
    social._social_log_actor = request.user
    social._social_log_ip = request.META.get("REMOTE_ADDR")
    social._social_log_context = {"source": "settings_page"}
    social.save()

Leave them unset for the common case (the user editing their own
links) -- performed_by simply stays None, which reads as
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
            import apps.customer.models.social_info_log   # <-- add this line

MIGRATION
---------
This file adds a new model/table -- run makemigrations/migrate after
adding it, same as any other new model.

RELATIONSHIP TO THE "NOT YET WIRED" GDPR NOTE IN social_info.py
------------------------------------------------------------------
Same situation as location_info_log.py: social_info.py's docstring
flags that User.soft_delete() doesn't yet know about SocialInfo. This
log only records that a change happened; it doesn't change what
soft_delete() touches. If/when that wiring is added, it'll produce its
own SocialInfoLog row for free, same as everywhere else in this app.

A NOTE ON CASCADE + hard_delete()
-----------------------------------
`SocialInfoLog.social` below is on_delete=CASCADE, matching every
other *_log.py model in this app (see contact_info_log.py's docstring
for the full reasoning). In short: normal User deletion never actually
removes rows anymore (it routes through soft_delete()), so this is
moot day-to-day. Only the explicit escape hatch,
User.objects.filter(...).hard_delete(), performs a real delete that
cascades through SocialInfo.user and then through this table, removing
that user's link history along with it.
"""

import logging

from django.conf import settings
from django.core.serializers.json import DjangoJSONEncoder
from django.db import models
from django.db.models.signals import post_save, pre_save
from django.dispatch import receiver
from django.utils.translation import gettext_lazy as _

from apps.customer.models.social_info import SocialInfo

logger = logging.getLogger(__name__)


# ====================================================================
# TRACKED FIELDS
# ====================================================================
# Every platform URL field SocialInfo defines, plus the generic
# website field -- see the module docstring for why this is derived
# from PLATFORM_DOMAINS rather than hand-listed.
TRACKED_FIELDS = list(SocialInfo.PLATFORM_DOMAINS.keys()) + ["website_url"]


# ====================================================================
# MANAGER
# ====================================================================

class SocialInfoLogManager(models.Manager):

    def for_social(self, social):
        """All log entries about a given SocialInfo, most recent first."""
        return self.filter(social=social)

    def for_user(self, user):
        """All log entries about a given user's social links, most recent first."""
        return self.filter(social__user=user)

    def for_platform(self, field_name: str):
        """
        Log entries where the given platform field name (e.g.
        'instagram_url') appears in the JSON diff -- the per-platform
        equivalent of by_action(), since `action` itself is
        intentionally coarse-grained (see module docstring).
        """
        return self.filter(**{f"changes__{field_name}__isnull": False})

    def recent(self, limit: int = 50):
        return self.order_by("-created_at")[:limit]

    def by_action(self, action: str):
        return self.filter(action=action)

    def record(
        self,
        *,
        social,
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
        that don't go through SocialInfo.save() at all (e.g. a bulk
        admin action, or something you want logged with a hand-written
        message rather than a field diff).
        """
        return self.create(
            social=social,
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

class SocialInfoLog(models.Model):
    """One row per SocialInfo save() that changed at least one tracked field."""

    class Action(models.TextChoices):
        CREATED = 'created', _('Social Info Created')
        UPDATED = 'updated', _('Social Info Updated')
        LINK_ADDED = 'link_added', _('Link Added')
        LINK_REMOVED = 'link_removed', _('Link Removed')
        LINK_CHANGED = 'link_changed', _('Link Changed')
        MULTIPLE_LINKS_CHANGED = 'multiple_links_changed', _('Multiple Links Changed')

    # NOTE: FK target assumes app_label "customer" (matching
    # apps/customer/models/social_info.py's location). Adjust the
    # string below if the app is registered under a different label.
    social = models.ForeignKey(
        'customer.SocialInfo',
        on_delete=models.CASCADE,
        related_name="social_logs",
        help_text=_("The SocialInfo row this log entry is about"),
    )

    action = models.CharField(
        _("Action"),
        max_length=25,
        choices=Action.choices,
        default=Action.UPDATED,
        db_index=True,
    )

    # {field_name: {"old": ..., "new": ...}} for every tracked field that
    # actually changed. On CREATED, "old" is always None. This is the
    # place to look up WHICH platform(s) changed -- see for_platform()
    # above and the module docstring on why `action` doesn't encode that.
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
        related_name="social_info_logs_performed",
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

    objects = SocialInfoLogManager()

    class Meta:
        verbose_name = _("Social Info Log")
        verbose_name_plural = _("Social Info Logs")
        db_table = "social_info_log"
        ordering = ["-created_at"]
        indexes = [
            # Matches the "history for this one user's links, newest
            # first" read pattern, same reasoning as the other
            # *_log.py models' composite index.
            models.Index(fields=["social", "-created_at"], name="social_log_social_idx"),
            models.Index(fields=["action", "-created_at"], name="social_log_action_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.get_action_display()} — {self.social.user.email_or_phone} @ {self.created_at:%Y-%m-%d %H:%M}"


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
    Coarse-grained classification -- see the module docstring for why
    this doesn't name individual platforms. `changes` (the diff dict
    itself) is always the source of truth for *which* platform(s)
    were involved; `action` only describes the shape of the change.
    """
    if created:
        return SocialInfoLog.Action.CREATED

    if len(diff) > 1:
        return SocialInfoLog.Action.MULTIPLE_LINKS_CHANGED

    # Exactly one field changed -- classify by its old/new presence.
    (only_change,) = diff.values()
    was_set = bool(only_change["old"])
    is_set = bool(only_change["new"])

    if not was_set and is_set:
        return SocialInfoLog.Action.LINK_ADDED
    if was_set and not is_set:
        return SocialInfoLog.Action.LINK_REMOVED
    if was_set and is_set:
        return SocialInfoLog.Action.LINK_CHANGED

    return SocialInfoLog.Action.UPDATED


# ====================================================================
# SIGNALS
# ====================================================================

# String sender ('customer.SocialInfo') matches the lazy-resolution
# pattern account_log.py uses for settings.AUTH_USER_MODEL, so this
# connects correctly regardless of module import order at app-loading
# time. Update the app_label in this string if SocialInfo ever moves
# to a different app.
_SOCIAL_INFO_SENDER = 'customer.SocialInfo'


@receiver(pre_save, sender=_SOCIAL_INFO_SENDER)
def _capture_before_state(sender, instance, **kwargs):
    """
    Stash the row's current (pre-update) tracked-field values on the
    instance so post_save can diff against them -- by the time
    post_save fires, the UPDATE has already happened and the old
    values are gone from the DB.

    Skipped entirely for not-yet-created instances. SocialInfo.user is
    the primary key (not an auto-incrementing id), so `instance.pk is
    None` is the correct "does a row exist yet" check here too --
    same as ContactInfo/LocationInfo/ProfileInfo.
    """
    if instance.pk is None:
        instance._social_log_before = None
        return

    try:
        instance._social_log_before = (
            sender._default_manager
            .filter(pk=instance.pk)
            .values(*TRACKED_FIELDS)
            .first()
        )
    except Exception:
        # Never let change-logging infrastructure break an actual save.
        logger.exception(
            "social_info_log: failed to capture before-state for social pk=%s",
            instance.pk,
        )
        instance._social_log_before = None


@receiver(post_save, sender=_SOCIAL_INFO_SENDER)
def _write_social_info_log(sender, instance, created, update_fields=None, **kwargs):
    """
    Diff before vs. after and write one SocialInfoLog row if anything
    tracked actually changed. Wrapped defensively end-to-end: a bug or
    outage in the logging path must never surface as a failed save()
    for the user-facing action that triggered it.
    """
    try:
        after = _snapshot(instance)

        if created:
            diff = {field: {"old": None, "new": value} for field, value in after.items()}
        else:
            before = getattr(instance, "_social_log_before", None)
            if before is None:
                # No reliable before-state (pre_save capture failed, or
                # this is an edge case like an explicit pk pre-assigned
                # before first insert) -- nothing trustworthy to log.
                return
            diff = _diff(before, after)

        if not diff:
            return  # save() happened, but nothing tracked changed

        action = _classify_action(diff, created)

        context = getattr(instance, "_social_log_context", None) or {}
        metadata = {
            "update_fields": sorted(update_fields) if update_fields else None,
            **context,
        }

        SocialInfoLog.objects.record(
            social=instance,
            action=action,
            changes=diff,
            performed_by=getattr(instance, "_social_log_actor", None),
            ip_address=getattr(instance, "_social_log_ip", None),
            user_agent=getattr(instance, "_social_log_user_agent", None),
            metadata=metadata,
        )
    except Exception:
        logger.exception(
            "social_info_log: failed to write SocialInfoLog for social pk=%s",
            instance.pk,
        )
    finally:
        # Strip transient context so it can never leak into a later,
        # unrelated save() on the same in-memory instance.
        for attr in (
            "_social_log_before",
            "_social_log_actor",
            "_social_log_ip",
            "_social_log_user_agent",
            "_social_log_context",
        ):
            if hasattr(instance, attr):
                delattr(instance, attr)