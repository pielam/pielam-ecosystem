# apps/customer/signals.py

"""
CHANGELOG (bug-fix pass)
-------------------------
1. create_user_profile() had no failure isolation: if
   ProfileInfo.objects.get_or_create() ever raised (a future required
   field with no default, a DB hiccup, etc.), the exception propagated
   straight out of User.save() -- meaning signup itself would fail with
   a confusing error that looks unrelated to ProfileInfo. Wrapped in a
   try/except that logs instead of raising: several views already
   defensively call ProfileInfo.objects.get_or_create(user=...) on
   first access anyway (cover_photo.py, profile_photo.py,
   campaign_profile.py), so a user ending up without a ProfileInfo row
   immediately after signup is already a case the app tolerates and
   self-heals from -- it shouldn't be allowed to break account
   creation.

2. User.soft_delete() (account.py) never cascaded to ProfileInfo, and
   PublicProfileView (public_profile.py) never filtered out deleted/
   inactive users -- meaning "delete my account" left the user's public
   profile (bio, photos, business info, social links) fully visible and
   reachable at the same URL. Added sync_profile_archive_state(), which
   watches for a transition on User.deleted_at (None -> set, or
   set -> None) and archives/restores the matching ProfileInfo to keep
   them in lock-step. See that function's docstring for why this only
   reacts to an actual *transition* rather than syncing on every save.
   (The public_profile.py queryset was also patched separately to
   exclude inactive/deleted users regardless, as defense in depth.)
"""

import logging

from django.conf import settings
from django.db.models.signals import post_save, pre_save
from django.dispatch import receiver

from apps.customer.models.profile_info import ProfileInfo

logger = logging.getLogger(__name__)


@receiver(post_save, sender=settings.AUTH_USER_MODEL)
def create_user_profile(sender, instance, created, **kwargs):
    """
    Create a ProfileInfo row automatically when a new User is created.

    Defensive: several views already tolerate a missing ProfileInfo by
    lazily get_or_create()-ing it on first access, so a failure here
    degrades to "profile gets created a little later than usual"
    instead of failing the signup transaction outright.
    """
    if not created:
        return
    try:
        ProfileInfo.objects.get_or_create(user=instance)
    except Exception:
        logger.exception(
            "signals: failed to auto-create ProfileInfo for new user pk=%s",
            instance.pk,
        )


@receiver(pre_save, sender=settings.AUTH_USER_MODEL)
def _capture_deleted_at_before(sender, instance, **kwargs):
    """
    Stash the user's current (pre-update) `deleted_at` value so the
    post_save handler below can tell whether this save is an actual
    delete/restore *transition*, as opposed to some unrelated save that
    merely happens to run while the account is already deleted (e.g. the
    admin editing a soft-deleted user's role) -- which should NOT
    re-trigger archive/restore logic on the profile every time.
    """
    if instance.pk is None:
        instance._signals_prev_deleted_at = None
        return
    instance._signals_prev_deleted_at = (
        sender._default_manager
        .filter(pk=instance.pk)
        .values_list("deleted_at", flat=True)
        .first()
    )


@receiver(post_save, sender=settings.AUTH_USER_MODEL)
def sync_profile_archive_state(sender, instance, created, **kwargs):
    """
    Keep ProfileInfo.is_profile_archived in lock-step with User.deleted_at.

    Only acts on an actual transition (captured via the pre_save handler
    above), not on every save of an already-deleted or already-active
    user -- so an admin editing some other field on a deleted account
    doesn't repeatedly call profile.soft_delete()/restore() and doesn't
    fight with a profile that was independently archived/suspended for
    an unrelated reason.
    """
    if created:
        return  # create_user_profile() handles the brand-new-user case

    prev_deleted_at = getattr(instance, "_signals_prev_deleted_at", None)
    now_deleted_at = instance.deleted_at

    if hasattr(instance, "_signals_prev_deleted_at"):
        delattr(instance, "_signals_prev_deleted_at")

    if prev_deleted_at == now_deleted_at:
        return  # no transition -- nothing to sync

    profile = getattr(instance, "profileinfo", None)
    if profile is None:
        return

    try:
        if now_deleted_at is not None and prev_deleted_at is None:
            # Account was just soft-deleted -> archive its profile too.
            if not profile.is_profile_archived:
                profile.soft_delete(archived_by_user=instance.deleted_by)
        elif now_deleted_at is None and prev_deleted_at is not None:
            # Account was just restored -> restore its profile too.
            if profile.is_profile_archived:
                profile.restore()
    except Exception:
        logger.exception(
            "signals: failed to sync ProfileInfo archive state for user pk=%s",
            instance.pk,
        )


# NOTE:
# The previous `sync_urls_to_connected_services` receiver (post_save on
# ProfileInfo, auto-creating/updating ConnectedService rows for social/
# business URL fields) has been intentionally removed. It fired on every
# ProfileInfo.save() — including saves from increment_view_count(),
# verify(), suspend(), make_public(), feature(), etc. — and its
# update_or_create() lookup mixed a stable key (service_label) with a
# mutated value (personalized display name) in the same field, which
# produced duplicate ConnectedService rows on nearly every save and
# silently failed to disconnect cleared URLs.
#
# If ConnectedService rows need to be created/updated from ProfileInfo
# social fields going forward, do it explicitly (e.g. in a form/serializer
# save step, or an explicit service method the caller invokes), not as an
# implicit signal side effect of every ProfileInfo.save().