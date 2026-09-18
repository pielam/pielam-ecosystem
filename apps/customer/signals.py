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

3. Same gap as #2, but for BusinessInfo (business_info.py) -- that
   file's own docstring already flagged this as "NOT YET WIRED:
   GDPR / soft-delete cascade". Added sync_business_archive_state(),
   the same transition-only pattern as sync_profile_archive_state(),
   so a soft-deleted user's business record (name, registration
   number, tax ID, revenue, ...) doesn't stay publicly readable
   either. BusinessInfo is lazily created (unlike ProfileInfo, there's
   no create_*_on_signup signal for it), so this is a no-op for users
   who never created a BusinessInfo row -- same defensive
   `getattr(instance, "business_info", None)` pattern as the profile
   version.

   This required a small change to how the two post_save handlers
   share the value _capture_deleted_at_before() stashes in pre_save:
   sync_profile_archive_state() used to delattr it immediately after
   reading it, which was fine when it was the only reader, but a
   second post_save receiver reading the same attribute would then see
   it already gone -- silently depending on Django's post_save
   connection order (registration order), which isn't something to
   build a correctness guarantee on. Fixed by no longer deleting the
   attribute after each read: pre_save unconditionally overwrites it
   before every subsequent save() anyway, so there was never a real
   leak risk from leaving it set between saves -- only an appearance
   of tidiness that isn't worth the ordering fragility.

4. Same gap as #2/#3, extended to ContactInfo (contact_info.py) --
   flagged in that file's own docstring the same way. Added
   sync_contact_archive_state(), same transition-only pattern, same
   defensive `getattr(instance, "contactinfo", None)` lookup (though
   in practice every user should already have a ContactInfo row --
   nothing in this codebase creates one lazily the way BusinessInfo/
   LocationInfo/SocialInfo are, so this is closer to a
   should-never-be-None safety check than an expected common case).

   One signature difference from #2/#3: ContactInfo.soft_delete()
   takes no `archived_by_user` parameter and ContactInfo has no
   `archived_by` FK field at all (only `is_contact_archived` /
   `archived_at`) -- so sync_contact_archive_state() calls
   `contact.soft_delete()` with no argument, unlike the profile/
   business versions. Who triggered the deletion is still on
   AccountLog (via User.deleted_by), just not duplicated onto
   ContactInfo itself.

   LocationInfo and SocialInfo were deliberately NOT given an
   equivalent sync function here, despite their own docstrings using
   the same "NOT YET WIRED: GDPR / soft-delete cascade" language.
   Unlike ProfileInfo/ContactInfo/BusinessInfo, neither model actually
   has an archive flag, a soft_delete(), or a restore() method to sync
   against -- there is no archive *state* on those two models for a
   signal to toggle. Their docstrings' GDPR concern is really about
   scrubbing/anonymizing stored PII (address, coordinates, social
   handles) on account deletion, which is a different mechanism than
   the archive-toggle pattern used here, and would require adding
   soft-delete support to those models first. That's a model-level
   decision for location_info.py/social_info.py to make, not something
   to bolt on from this file by inventing fields/methods that aren't
   actually there.

5. apps/ponno/views/product_detail.py caches the product-detail page
   (ProductDetailView) with the dealer's ContactInfo and BusinessInfo
   pulled in via select_related -- so the cached page holds a snapshot
   of both models. Editing either one (verifying a phone number,
   changing business_name, toggling show_phone, archiving/restoring)
   was invisible on every product page that dealer sells until
   PRODUCT_DETAIL_CACHE_TTL naturally expired (default 300s, +/-10%
   jitter) -- there was no signal telling that view's cache to drop
   the stale entry. Added invalidate_product_cache_on_contactinfo_change()
   and invalidate_product_cache_on_businessinfo_change(), which fire
   on every save of either model (not transition-gated like #2-#4
   above -- any field change here is display-relevant, not just an
   archive toggle) and invalidate the cached page for every active
   product that dealer sells. See those functions' docstrings for the
   scale caveat on dealers with very large catalogs.
"""

import logging

from django.conf import settings
from django.db.models.signals import post_save, pre_save
from django.dispatch import receiver

from apps.customer.models.business_info import BusinessInfo
from apps.customer.models.contact_info import ContactInfo
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


@receiver(post_save, sender=settings.AUTH_USER_MODEL)
def sync_business_archive_state(sender, instance, created, **kwargs):
    """
    Keep BusinessInfo.is_business_archived in lock-step with
    User.deleted_at -- same rationale and same transition-only guard
    as sync_profile_archive_state() above (see that function's
    docstring for the "why only a transition, not every save" reasoning,
    which applies identically here).

    Unlike ProfileInfo, BusinessInfo is lazily created -- there's no
    create_user_profile-style signal guaranteeing every user has one
    (see business_info.py's own docstring on this). So most users
    simply won't have a `business_info` row at all, and this is a
    no-op for them, same as it would be if they'd never touched
    business features.
    """
    if created:
        return  # nothing to sync -- BusinessInfo isn't auto-created on signup

    prev_deleted_at = getattr(instance, "_signals_prev_deleted_at", None)
    now_deleted_at = instance.deleted_at

    if prev_deleted_at == now_deleted_at:
        return  # no transition -- nothing to sync

    business = getattr(instance, "business_info", None)
    if business is None:
        return

    try:
        if now_deleted_at is not None and prev_deleted_at is None:
            # Account was just soft-deleted -> archive its business too.
            if not business.is_business_archived:
                business.soft_delete(archived_by_user=instance.deleted_by)
        elif now_deleted_at is None and prev_deleted_at is not None:
            # Account was just restored -> restore its business too.
            if business.is_business_archived:
                business.restore()
    except Exception:
        logger.exception(
            "signals: failed to sync BusinessInfo archive state for user pk=%s",
            instance.pk,
        )


@receiver(post_save, sender=settings.AUTH_USER_MODEL)
def sync_contact_archive_state(sender, instance, created, **kwargs):
    """
    Keep ContactInfo.is_contact_archived in lock-step with
    User.deleted_at -- same transition-only pattern as
    sync_profile_archive_state()/sync_business_archive_state() above.

    Note ContactInfo.soft_delete() takes no `archived_by_user`
    argument (and ContactInfo has no `archived_by` FK to set) -- see
    this module's changelog entry #4 for why that's fine: who
    triggered the deletion is already on AccountLog via
    User.deleted_by, not duplicated here.
    """
    if created:
        return  # nothing to sync on brand-new users yet

    prev_deleted_at = getattr(instance, "_signals_prev_deleted_at", None)
    now_deleted_at = instance.deleted_at

    if prev_deleted_at == now_deleted_at:
        return  # no transition -- nothing to sync

    contact = getattr(instance, "contactinfo", None)
    if contact is None:
        return

    try:
        if now_deleted_at is not None and prev_deleted_at is None:
            # Account was just soft-deleted -> archive its contact info too.
            if not contact.is_contact_archived:
                contact.soft_delete()
        elif now_deleted_at is None and prev_deleted_at is not None:
            # Account was just restored -> restore its contact info too.
            if contact.is_contact_archived:
                contact.restore()
    except Exception:
        logger.exception(
            "signals: failed to sync ContactInfo archive state for user pk=%s",
            instance.pk,
        )


# ======================================================================
# PRODUCT-DETAIL CACHE INVALIDATION
# ======================================================================
# See changelog entry #5. Unlike the three sync_*_archive_state()
# handlers above, these are NOT transition-gated -- they fire on every
# save of ContactInfo/BusinessInfo, because any field change on either
# model (not just an archive toggle) can change what the cached
# product-detail page should show (phone/email values, verification
# flags, show_* visibility toggles, business_name, website, ...).
# A stray extra cache invalidation is harmless; a missed one means
# stale seller info sits on a live product page for up to five minutes.

def _invalidate_dealer_product_pages(dealer) -> None:
    """
    Invalidate the cached product-detail page for every active product
    this dealer sells.

    Local imports to avoid a hard import-time dependency from
    apps.customer -> apps.ponno at module load (apps.customer.signals
    is imported early via AppConfig.ready()). Adjust the
    product_detail import path below if that view file ever moves.

    SCALE NOTE: this iterates every active product the dealer sells
    and calls invalidate_product_cache() (a handful of cache.delete
    calls) per product, synchronously, inline on the ContactInfo/
    BusinessInfo save. Fine for a typical dealer's catalog size. If a
    dealer with a very large number of active listings edits their
    contact info, consider dispatching this via Celery instead --
    mirroring the USE_CELERY-gated pattern already used elsewhere in
    product_detail.py (_increment_view_count,
    _record_product_view_async) -- rather than blocking the admin
    save on possibly thousands of cache deletes.
    """
    try:
        from apps.ponno.models.product import Product
        from apps.ponno.views.product_detail import invalidate_product_cache
    except ImportError:
        logger.exception(
            "signals: could not import Product/invalidate_product_cache "
            "for dealer pk=%s -- check the import path in "
            "_invalidate_dealer_product_pages()",
            getattr(dealer, "pk", None),
        )
        return

    products = Product.objects.filter(
        dealer_id=dealer.pk,
        is_active=True,
        deleted_at__isnull=True,
    ).only("pk", "slug", "dealer_id")

    for product in products:
        invalidate_product_cache(product)


@receiver(post_save, sender=ContactInfo)
def invalidate_product_cache_on_contactinfo_change(sender, instance, **kwargs):
    """
    Fires on every ContactInfo save -- including the narrow
    update_fields=[...] saves from verify_phone()/unverify_phone()/
    verify_alt_email()/unverify_alt_email() and the admin bulk actions
    that call those, plus soft_delete()/restore() and ordinary field
    edits (phone_number, show_phone, is_contact_public, ...).
    """
    try:
        _invalidate_dealer_product_pages(instance.user)
    except Exception:
        logger.exception(
            "signals: failed to invalidate product cache for ContactInfo "
            "change, user pk=%s",
            instance.pk,
        )


@receiver(post_save, sender=BusinessInfo)
def invalidate_product_cache_on_businessinfo_change(sender, instance, **kwargs):
    """Same rationale as the ContactInfo handler above, for BusinessInfo edits."""
    try:
        _invalidate_dealer_product_pages(instance.user)
    except Exception:
        logger.exception(
            "signals: failed to invalidate product cache for BusinessInfo "
            "change, user pk=%s",
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