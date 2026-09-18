# apps/customer/models/contact_info.py

"""
ContactInfo Model
------------------
Stores how users can be reached and contact one another through the
platform: phone, alternate email, messaging-app handles, and a
preferred-contact preference -- separate from ProfileInfo (which holds
display/social profile data).

Design mirrors apps/customer/models/profile_info.py:
- Soft-delete queryset/manager pair (bulk `.delete()` archives instead
  of hard-deleting, with `.hard_delete()` as the explicit escape hatch)
- Per-channel privacy toggles (show_email/show_phone/etc.), same shape
  as ProfileInfo's show_location/show_followers/etc.
- Verification tracked per channel. alt_email/phone_number keep their
  own is_X_verified/X_verified_at pair directly on this model (that
  predates ContactVerification and isn't worth migrating). Every other
  channel (whatsapp, telegram, signal, ...) is verified via the
  ContactVerification model below instead -- see that model's
  docstring for why those live separately rather than as 30 more
  columns here.
"""

import uuid
import re
from typing import Optional

from django.db import models
from django.conf import settings
from django.utils import timezone
from django.core.exceptions import ValidationError
from django.utils.translation import gettext_lazy as _


# ====================================================================
# CONTACT INFO QUERYSET
# ====================================================================

class ContactInfoQuerySet(models.QuerySet):
    """
    Soft-delete guarantee at the queryset level, same rationale as
    ProfileInfoQuerySet: a bulk `ContactInfo.objects.filter(...).delete()`
    should archive every row rather than bypassing soft_delete().
    """

    def delete(self):
        count = 0
        for obj in self:
            obj.soft_delete()
            count += 1
        return count, {self.model._meta.label: count}

    def hard_delete(self):
        """Escape hatch for an actual, irreversible bulk delete."""
        return super().delete()


# ====================================================================
# CONTACT INFO MANAGER
# ====================================================================

class ContactInfoManager(models.Manager.from_queryset(ContactInfoQuerySet)):
    """Custom manager for ContactInfo with common lookups."""

    def create_contact_info(self, user, **kwargs):
        """Create a new ContactInfo row for a user."""
        contact = self.model(user=user, **kwargs)
        contact.save(using=self._db)
        return contact

    def get_contact_by_user(self, user):
        """Get contact info by user instance."""
        return self.select_related('user').get(user=user)

    def get_publicly_contactable(self):
        """Users who allow being contacted and aren't archived."""
        return self.filter(
            allow_contact_requests=True,
            is_contact_archived=False,
            user__is_active=True,
        ).select_related('user')

    def get_verified_phone_contacts(self):
        """Contacts with a verified phone number."""
        return self.filter(
            is_phone_verified=True,
            is_contact_archived=False,
        ).select_related('user')

    def get_verified_email_contacts(self):
        """Contacts with a verified alternate email."""
        return self.filter(
            is_alt_email_verified=True,
            is_contact_archived=False,
        ).select_related('user')

    def get_archived_contacts(self):
        """Archived (soft-deleted) contact info rows."""
        return self.filter(is_contact_archived=True).select_related('user')


# ====================================================================
# CONTACT INFO MODEL
# ====================================================================

class ContactInfo(models.Model):
    """
    Stores how a user can be reached and contacted by other users.
    One row per user (OneToOne), same shape as ProfileInfo.
    """

    # ================================================================
    # CHOICES
    # ================================================================

    class PreferredMethod(models.TextChoices):
        EMAIL = 'email', _('Email')
        PHONE = 'phone', _('Phone Call')
        WHATSAPP = 'whatsapp', _('WhatsApp')
        TELEGRAM = 'telegram', _('Telegram')
        SIGNAL = 'signal', _('Signal')
        VIBER = 'viber', _('Viber')
        WECHAT = 'wechat', _('WeChat')
        LINE = 'line', _('LINE')
        IMO = 'imo', _('imo')
        SKYPE = 'skype', _('Skype')
        DISCORD = 'discord', _('Discord')
        FACEBOOK_MESSENGER = 'facebook_messenger', _('Facebook Messenger')
        INSTAGRAM = 'instagram', _('Instagram')
        SNAPCHAT = 'snapchat', _('Snapchat')
        TWITTER = 'twitter', _('X / Twitter')
        LINKEDIN = 'linkedin', _('LinkedIn')
        SLACK = 'slack', _('Slack')
        PLATFORM_MESSAGE = 'platform_message', _('In-App Message')

    # ================================================================
    # MESSAGING CHANNEL REGISTRY
    # ================================================================
    # Single source of truth for the 15 non-phone/non-email channels:
    # (short key, field name). Used by get_visible_contact_methods(),
    # ContactVerification.clean() (to reject typo'd channel names),
    # and the verify_channel()/unverify_channel() helpers below.
    # `show_<key>` is assumed to exist for every key here -- true for
    # all 15 today (show_whatsapp, show_facebook_messenger, etc.).
    MESSAGING_CHANNELS = (
        ('whatsapp', 'whatsapp_number'),
        ('telegram', 'telegram_username'),
        ('signal', 'signal_number'),
        ('viber', 'viber_number'),
        ('wechat', 'wechat_id'),
        ('line', 'line_id'),
        ('imo', 'imo_number'),
        ('skype', 'skype_id'),
        ('discord', 'discord_username'),
        ('facebook_messenger', 'facebook_messenger_username'),
        ('instagram', 'instagram_username'),
        ('snapchat', 'snapchat_username'),
        ('twitter', 'twitter_username'),
        ('linkedin', 'linkedin_url'),
        ('slack', 'slack_workspace_handle'),
    )
    CHANNEL_FIELD_NAMES = frozenset(field_name for _key, field_name in MESSAGING_CHANNELS)

    # ================================================================
    # PRIMARY FIELDS
    # ================================================================

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        primary_key=True,
        related_name='contactinfo',
        help_text=_("User account associated with this contact info")
    )

    uuid = models.UUIDField(
        default=uuid.uuid4,
        editable=False,
        unique=True,
        db_index=True,
        help_text=_("Unique identifier for external API references")
    )

    # ================================================================
    # EMAIL (alternate/contact email, distinct from login email)
    # ================================================================

    alt_email = models.EmailField(
        _("Alternate Email"),
        max_length=254,
        blank=True,
        null=True,
        help_text=_("Secondary email other users can reach you at")
    )

    is_alt_email_verified = models.BooleanField(
        _("Alternate Email Verified"),
        default=False
    )

    alt_email_verified_at = models.DateTimeField(
        _("Alternate Email Verified At"),
        blank=True,
        null=True
    )

    show_email = models.BooleanField(
        _("Show Email"),
        default=False,
        help_text=_("Display alternate email on public profile")
    )

    # ================================================================
    # PHONE
    # ================================================================

    phone_number = models.CharField(
        _("Phone Number"),
        max_length=20,
        blank=True,
        null=True,
        help_text=_("Contact phone number, E.164 format recommended (e.g. +8801XXXXXXXXX)")
    )

    is_phone_verified = models.BooleanField(
        _("Phone Verified"),
        default=False
    )

    phone_verified_at = models.DateTimeField(
        _("Phone Verified At"),
        blank=True,
        null=True
    )

    show_phone = models.BooleanField(
        _("Show Phone"),
        default=False,
        help_text=_("Display phone number on public profile")
    )

    # ================================================================
    # MESSAGING APP HANDLES
    # ================================================================

    whatsapp_number = models.CharField(
        _("WhatsApp Number"),
        max_length=20,
        blank=True,
        null=True
    )

    show_whatsapp = models.BooleanField(
        _("Show WhatsApp"),
        default=False
    )

    telegram_username = models.CharField(
        _("Telegram Username"),
        max_length=50,
        blank=True,
        null=True
    )

    show_telegram = models.BooleanField(
        _("Show Telegram"),
        default=False
    )

    signal_number = models.CharField(
        _("Signal Number"),
        max_length=20,
        blank=True,
        null=True
    )

    show_signal = models.BooleanField(
        _("Show Signal"),
        default=False
    )

    viber_number = models.CharField(
        _("Viber Number"),
        max_length=20,
        blank=True,
        null=True
    )

    show_viber = models.BooleanField(
        _("Show Viber"),
        default=False
    )

    wechat_id = models.CharField(
        _("WeChat ID"),
        max_length=50,
        blank=True,
        null=True
    )

    show_wechat = models.BooleanField(
        _("Show WeChat"),
        default=False
    )

    line_id = models.CharField(
        _("LINE ID"),
        max_length=50,
        blank=True,
        null=True
    )

    show_line = models.BooleanField(
        _("Show LINE"),
        default=False
    )

    imo_number = models.CharField(
        _("imo Number"),
        max_length=20,
        blank=True,
        null=True
    )

    show_imo = models.BooleanField(
        _("Show imo"),
        default=False
    )

    skype_id = models.CharField(
        _("Skype ID"),
        max_length=50,
        blank=True,
        null=True
    )

    show_skype = models.BooleanField(
        _("Show Skype"),
        default=False
    )

    discord_username = models.CharField(
        _("Discord Username"),
        max_length=50,
        blank=True,
        null=True,
        help_text=_("e.g. username or legacy username#1234")
    )

    show_discord = models.BooleanField(
        _("Show Discord"),
        default=False
    )

    facebook_messenger_username = models.CharField(
        _("Facebook Messenger Username"),
        max_length=100,
        blank=True,
        null=True,
        help_text=_("m.me/ username or profile handle")
    )

    show_facebook_messenger = models.BooleanField(
        _("Show Facebook Messenger"),
        default=False
    )

    instagram_username = models.CharField(
        _("Instagram Username"),
        max_length=50,
        blank=True,
        null=True
    )

    show_instagram = models.BooleanField(
        _("Show Instagram"),
        default=False
    )

    snapchat_username = models.CharField(
        _("Snapchat Username"),
        max_length=50,
        blank=True,
        null=True
    )

    show_snapchat = models.BooleanField(
        _("Show Snapchat"),
        default=False
    )

    twitter_username = models.CharField(
        _("X / Twitter Username"),
        max_length=50,
        blank=True,
        null=True
    )

    show_twitter = models.BooleanField(
        _("Show X / Twitter"),
        default=False
    )

    linkedin_url = models.URLField(
        _("LinkedIn Profile URL"),
        max_length=255,
        blank=True,
        null=True
    )

    show_linkedin = models.BooleanField(
        _("Show LinkedIn"),
        default=False
    )

    slack_workspace_handle = models.CharField(
        _("Slack Handle"),
        max_length=100,
        blank=True,
        null=True,
        help_text=_("workspace:@handle, for professional/community contexts")
    )

    show_slack = models.BooleanField(
        _("Show Slack"),
        default=False
    )

    # ================================================================
    # CONTACT PREFERENCES
    # ================================================================

    preferred_contact_method = models.CharField(
        _("Preferred Contact Method"),
        max_length=20,
        choices=PreferredMethod.choices,
        default=PreferredMethod.PLATFORM_MESSAGE,
        help_text=_("How this user prefers to be contacted")
    )

    allow_contact_requests = models.BooleanField(
        _("Allow Contact Requests"),
        default=True,
        help_text=_("Allow other users to initiate contact")
    )

    is_contact_public = models.BooleanField(
        _("Contact Info Public"),
        default=False,
        help_text=_("Master switch: if False, all show_* fields are ignored and nothing is shown")
    )

    # ================================================================
    # TIMESTAMPS
    # ================================================================

    contact_creation_time = models.DateTimeField(
        _("Created At"),
        auto_now_add=True
    )

    contact_updated_time = models.DateTimeField(
        _("Updated At"),
        auto_now=True
    )

    # ================================================================
    # SOFT DELETE
    # ================================================================

    is_contact_archived = models.BooleanField(
        _("Archived"),
        default=False,
        db_index=True
    )

    archived_at = models.DateTimeField(
        _("Archived At"),
        blank=True,
        null=True
    )

    # ================================================================
    # MANAGER / META
    # ================================================================

    objects = ContactInfoManager()

    class Meta:
        verbose_name = _("Contact Info")
        verbose_name_plural = _("Contact Infos")
        db_table = 'contact_info'
        ordering = ['-contact_creation_time']
        indexes = [
            models.Index(fields=['uuid']),
            models.Index(fields=['is_contact_public', 'is_contact_archived']),
            models.Index(fields=['phone_number']),
        ]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Snapshot of the 15 messaging-channel field values as loaded
        # from the DB (or as set at construction time for a new,
        # unsaved instance). Used by
        # _invalidate_changed_channel_verifications() on save() to
        # detect which channels actually changed since load, so we
        # only touch ContactVerification rows for those.
        self._original_channel_values = {
            field_name: getattr(self, field_name, None)
            for _key, field_name in self.MESSAGING_CHANNELS
        }

    def __str__(self) -> str:
        return f"ContactInfo({self.user})"

    def __repr__(self) -> str:
        return f"<ContactInfo: user={self.user}>"

    # ================================================================
    # VERIFICATION METHODS (alt_email / phone_number only -- see
    # ContactVerification below for every other channel)
    # ================================================================

    def verify_phone(self, save: bool = True) -> None:
        """Mark phone number as verified."""
        self.is_phone_verified = True
        self.phone_verified_at = timezone.now()
        if save:
            self.save(update_fields=['is_phone_verified', 'phone_verified_at'])

    def unverify_phone(self, save: bool = True) -> None:
        """Clear phone verification (e.g. after the number changes)."""
        self.is_phone_verified = False
        self.phone_verified_at = None
        if save:
            self.save(update_fields=['is_phone_verified', 'phone_verified_at'])

    def verify_alt_email(self, save: bool = True) -> None:
        """Mark alternate email as verified."""
        self.is_alt_email_verified = True
        self.alt_email_verified_at = timezone.now()
        if save:
            self.save(update_fields=['is_alt_email_verified', 'alt_email_verified_at'])

    def unverify_alt_email(self, save: bool = True) -> None:
        """Clear alternate-email verification."""
        self.is_alt_email_verified = False
        self.alt_email_verified_at = None
        if save:
            self.save(update_fields=['is_alt_email_verified', 'alt_email_verified_at'])

    # ================================================================
    # MESSAGING CHANNEL VERIFICATION (the other 15 channels -- backed
    # by ContactVerification rather than fields on this model)
    # ================================================================

    @classmethod
    def _resolve_channel_field(cls, channel: str) -> str:
        """
        Accept either the short key ('whatsapp') or the raw field name
        ('whatsapp_number') and return the field name. Raises
        ValueError for anything that isn't one of the 15 registered
        messaging channels.
        """
        key_to_field = dict(cls.MESSAGING_CHANNELS)
        if channel in key_to_field:
            return key_to_field[channel]
        if channel in cls.CHANNEL_FIELD_NAMES:
            return channel
        raise ValueError(f"'{channel}' is not a recognized messaging channel.")

    def verify_channel(self, channel: str, *, method: str = None, external_id: str = '') -> "ContactVerification":
        """
        Mark one of the 15 messaging channels verified. Creates or
        updates the matching ContactVerification row (unique_together
        on contact+channel means this must be update_or_create, not
        create) and snapshots the *current* field value into
        verified_value, so is_stale() has something correct to compare
        against later.

        Unlike verify_phone()/verify_alt_email(), this doesn't flip a
        boolean on ContactInfo itself -- verification state for these
        channels lives entirely on ContactVerification.

        Raises ValueError if the channel is unrecognized or currently
        has no value set (nothing to verify).
        """
        field_name = self._resolve_channel_field(channel)
        value = getattr(self, field_name)
        if not value:
            raise ValueError(f"Cannot verify '{channel}': {field_name} has no value set.")

        verification, _created = ContactVerification.objects.update_or_create(
            contact=self,
            channel=field_name,
            defaults={
                'status': ContactVerification.Status.VERIFIED,
                'method': method or ContactVerification.Method.MANUAL,
                'verified_value': value,
                'verified_at': timezone.now(),
                'external_id': external_id,
            },
        )
        return verification

    def unverify_channel(self, channel: str) -> None:
        """Revert a messaging channel to unverified without deleting its row/history."""
        field_name = self._resolve_channel_field(channel)
        self.verifications.filter(channel=field_name).update(
            status=ContactVerification.Status.UNVERIFIED,
            verified_at=None,
        )

    def get_verification_status(self, channel: str) -> str:
        """
        Live verification state for one messaging channel:
        - 'unverified' -- no ContactVerification row, or status != VERIFIED
        - 'verified'   -- row is VERIFIED and matches the current field value
        - 'stale'      -- row says VERIFIED but the field was edited since
        """
        field_name = self._resolve_channel_field(channel)
        verification = self.verifications.filter(channel=field_name).first()
        if verification is None or verification.status != ContactVerification.Status.VERIFIED:
            return 'unverified'
        return 'stale' if verification.is_stale() else 'verified'

    def _invalidate_changed_channel_verifications(self) -> None:
        """
        Called from save() on updates only (not on first insert, where
        there's nothing to compare against). If a messaging-channel
        field changed since this instance was loaded, reset any
        VERIFIED ContactVerification row for that channel back to
        UNVERIFIED.

        is_stale() already catches a changed-but-not-reset channel on
        read (e.g. inside get_visible_contact_methods()), so this
        isn't required for correctness there -- but without it, the
        ContactVerification row itself keeps reporting status=VERIFIED
        indefinitely to anything that reads it directly (admin list,
        exports, etc.) rather than going through is_stale(). Resetting
        here keeps that field honest.
        """
        changed_fields = [
            field_name for _key, field_name in self.MESSAGING_CHANNELS
            if getattr(self, field_name, None) != self._original_channel_values.get(field_name)
        ]
        if not changed_fields:
            return
        self.verifications.filter(
            channel__in=changed_fields,
            status=ContactVerification.Status.VERIFIED,
        ).update(status=ContactVerification.Status.UNVERIFIED, verified_at=None)

    # ================================================================
    # CROSS-USER CONTACT METHODS
    # ================================================================

    def get_visible_contact_methods(self, viewer=None) -> dict:
        """
        Return only the contact channels this user has actually opted
        to show, respecting the `is_contact_public` master switch and
        each per-channel show_* toggle. `viewer` is accepted (and
        currently unused beyond an owner short-circuit) so it's easy
        to layer in follower-only/blocked-user rules later without
        changing every call site.

        Every returned channel includes a `verified` key:
        - email/phone read straight off is_alt_email_verified /
          is_phone_verified, same as before.
        - every other channel is looked up against ContactVerification:
          verified only if a row exists with status=VERIFIED *and*
          the live field value still matches what was verified (i.e.
          not stale -- see ContactVerification.is_stale()). A channel
          the user edited after verifying reverts to unverified here
          without any extra write, since staleness is computed live.
        """
        if self.is_contact_archived:
            return {}

        # Owner always sees their own full contact info.
        is_owner = viewer is not None and viewer == self.user
        if not is_owner and not self.is_contact_public:
            return {}

        methods = {}

        if is_owner or self.show_email:
            if self.alt_email:
                methods['email'] = {
                    'value': self.alt_email,
                    'verified': self.is_alt_email_verified,
                }

        if is_owner or self.show_phone:
            if self.phone_number:
                methods['phone'] = {
                    'value': self.phone_number,
                    'verified': self.is_phone_verified,
                }

        # Channels below have no separate verified/verified_at pair on
        # this model -- their verification state lives on
        # ContactVerification instead. Built from MESSAGING_CHANNELS
        # (the registry) rather than a hardcoded tuple, so adding a
        # 16th channel only means editing that one registry.
        simple_channels = tuple(
            (key, field_name, getattr(self, f'show_{key}'), getattr(self, field_name))
            for key, field_name in self.MESSAGING_CHANNELS
        )

        # Which (field_name -> key) channels are actually going to be
        # returned, so we only need to resolve verification for those
        # -- not all 15 regardless of visibility.
        candidate_field_names = {
            field_name
            for _key, field_name, show_flag, value in simple_channels
            if (is_owner or show_flag) and value
        }

        verified_field_names = set()
        if candidate_field_names:
            # One query for every candidate channel at once, rather
            # than one query per channel -- self.verifications is the
            # related_name from ContactVerification.contact.
            verifications = self.verifications.filter(
                channel__in=candidate_field_names,
                status=ContactVerification.Status.VERIFIED,
            )
            verified_field_names = {
                v.channel for v in verifications if not v.is_stale()
            }

        for key, field_name, show_flag, value in simple_channels:
            if (is_owner or show_flag) and value:
                methods[key] = {
                    'value': value,
                    'verified': field_name in verified_field_names,
                }

        return methods

    def can_be_contacted_by(self, viewer) -> bool:
        """
        Whether `viewer` is currently allowed to initiate contact with
        this user at all -- checks archive state, the contact-requests
        toggle, and (if a ProfileInfo/blocking system is present)
        whether the viewer has been blocked.
        """
        if self.is_contact_archived or not self.allow_contact_requests:
            return False

        if viewer == self.user:
            return False  # can't contact yourself

        profile = getattr(self.user, 'profileinfo', None)
        if profile is not None and hasattr(profile, 'is_blocked'):
            if profile.is_blocked(viewer):
                return False

        return True

    def get_preferred_contact_value(self) -> Optional[str]:
        """
        Resolve the actual contact value (email/phone/handle) for this
        user's `preferred_contact_method`. Returns None if that
        channel has no value set, so callers should fall back to
        platform messaging in that case.
        """
        mapping = {
            self.PreferredMethod.EMAIL: self.alt_email,
            self.PreferredMethod.PHONE: self.phone_number,
            self.PreferredMethod.WHATSAPP: self.whatsapp_number,
            self.PreferredMethod.TELEGRAM: self.telegram_username,
            self.PreferredMethod.SIGNAL: self.signal_number,
            self.PreferredMethod.VIBER: self.viber_number,
            self.PreferredMethod.WECHAT: self.wechat_id,
            self.PreferredMethod.LINE: self.line_id,
            self.PreferredMethod.IMO: self.imo_number,
            self.PreferredMethod.SKYPE: self.skype_id,
            self.PreferredMethod.DISCORD: self.discord_username,
            self.PreferredMethod.FACEBOOK_MESSENGER: self.facebook_messenger_username,
            self.PreferredMethod.INSTAGRAM: self.instagram_username,
            self.PreferredMethod.SNAPCHAT: self.snapchat_username,
            self.PreferredMethod.TWITTER: self.twitter_username,
            self.PreferredMethod.LINKEDIN: self.linkedin_url,
            self.PreferredMethod.SLACK: self.slack_workspace_handle,
        }
        return mapping.get(self.preferred_contact_method)

    # ================================================================
    # SOFT DELETE
    # ================================================================

    def soft_delete(self, save: bool = True) -> None:
        """Archive this contact info instead of hard-deleting it."""
        self.is_contact_archived = True
        self.archived_at = timezone.now()
        if save:
            self.save()

    def restore(self, save: bool = True) -> None:
        """Un-archive this contact info."""
        self.is_contact_archived = False
        self.archived_at = None
        if save:
            self.save()

    def delete(self, *args, **kwargs):
        """
        Instance-level delete soft-deletes (archives). Bulk queryset
        deletes are covered separately by ContactInfoQuerySet.delete().
        """
        self.soft_delete()

    # ================================================================
    # GDPR EXPORT
    # ================================================================

    def export_data(self) -> dict:
        """Export this user's own contact data (GDPR data portability)."""
        return {
            'uuid': str(self.uuid),
            'alt_email': self.alt_email,
            'phone_number': self.phone_number,
            'whatsapp_number': self.whatsapp_number,
            'telegram_username': self.telegram_username,
            'signal_number': self.signal_number,
            'viber_number': self.viber_number,
            'wechat_id': self.wechat_id,
            'line_id': self.line_id,
            'imo_number': self.imo_number,
            'skype_id': self.skype_id,
            'discord_username': self.discord_username,
            'facebook_messenger_username': self.facebook_messenger_username,
            'instagram_username': self.instagram_username,
            'snapchat_username': self.snapchat_username,
            'twitter_username': self.twitter_username,
            'linkedin_url': self.linkedin_url,
            'slack_workspace_handle': self.slack_workspace_handle,
            'preferred_contact_method': self.preferred_contact_method,
            'created_at': self.contact_creation_time.isoformat(),
            'updated_at': self.contact_updated_time.isoformat(),
        }

    # ================================================================
    # VALIDATION
    # ================================================================

    PHONE_REGEX = re.compile(r'^\+?[0-9\s\-()]{7,20}$')

    def clean(self) -> None:
        """Validate phone-shaped fields before save."""
        super().clean()

        phone_shaped_fields = {
            'phone_number': self.phone_number,
            'whatsapp_number': self.whatsapp_number,
            'signal_number': self.signal_number,
            'viber_number': self.viber_number,
            'imo_number': self.imo_number,
        }

        errors = {}
        for field_name, value in phone_shaped_fields.items():
            if value and not self.PHONE_REGEX.match(value):
                errors[field_name] = _("Enter a valid phone number.")

        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        """
        Run validation, invalidate any ContactVerification rows whose
        underlying field just changed (see
        _invalidate_changed_channel_verifications), then persist and
        refresh the change-detection snapshot.
        """
        self.full_clean()
        if not self._state.adding:
            self._invalidate_changed_channel_verifications()
        super().save(*args, **kwargs)
        self._original_channel_values = {
            field_name: getattr(self, field_name, None)
            for _key, field_name in self.MESSAGING_CHANNELS
        }


# ====================================================================
# CONTACT VERIFICATION
# ====================================================================
# Per-channel verification state for ContactInfo's messaging/social
# handles. Deliberately a separate model rather than a
# is_X_verified/X_verified_at pair per channel on ContactInfo itself
# (which is how alt_email/phone_number already work) -- with 15
# channels that pairing would mean 30 extra columns on ContactInfo,
# most of them null for most users, and no natural place to store
# per-channel verification metadata (method, external_id, OTP state)
# that doesn't apply uniformly across channels.
#
# `channel` matches a ContactInfo field name (e.g. "whatsapp_number",
# "telegram_username") -- this used to be enforced only by convention
# ("kept in sync by application code"); clean() below now enforces it
# against ContactInfo.CHANNEL_FIELD_NAMES so a typo'd or renamed
# channel raises at save() time instead of silently creating a row
# that get_visible_contact_methods() will never match against.
#
# `verified_value` snapshots the ContactInfo field's value at the
# moment verification succeeded. If the user later edits that field
# to something else, ContactInfo.save() now proactively resets this
# row's status back to UNVERIFIED (see
# _invalidate_changed_channel_verifications) -- and even if that
# hadn't run yet for some reason, is_stale() (below) lets read paths
# like get_visible_contact_methods() catch the mismatch live.
class ContactVerification(models.Model):

    class Status(models.TextChoices):
        UNVERIFIED = "unverified", _("Unverified")
        PENDING = "pending", _("Pending")
        VERIFIED = "verified", _("Verified")
        FAILED = "failed", _("Failed")

    class Method(models.TextChoices):
        OTP = "otp", _("One-Time Code")
        BOT = "bot", _("Bot/Deep Link")
        OAUTH = "oauth", _("OAuth Connect")
        PHONE_MATCH = "phone_match", _("Matches Verified Phone")
        MANUAL = "manual", _("Manual Review")

    contact = models.ForeignKey(
        "customer.ContactInfo",
        on_delete=models.CASCADE,
        related_name="verifications",
        help_text=_("The ContactInfo row this verification belongs to."),
    )

    channel = models.CharField(
        _("Channel"),
        max_length=30,
        help_text=_("Matches a ContactInfo field name, e.g. 'whatsapp_number'."),
    )

    status = models.CharField(
        _("Status"), max_length=15, choices=Status.choices,
        default=Status.UNVERIFIED, db_index=True,
    )

    method = models.CharField(
        _("Method"), max_length=15, choices=Method.choices,
    )

    verified_value = models.CharField(
        _("Verified Value"), max_length=255, blank=True,
        help_text=_("Snapshot of the field's value at the moment it was verified."),
    )

    verified_at = models.DateTimeField(_("Verified At"), null=True, blank=True)

    external_id = models.CharField(
        _("External ID"), max_length=255, blank=True,
        help_text=_("OAuth provider user ID, Telegram chat_id, etc., if applicable."),
    )

    otp_code_hash = models.CharField(_("OTP Code Hash"), max_length=128, blank=True)
    otp_expires_at = models.DateTimeField(_("OTP Expires At"), null=True, blank=True)
    attempts = models.PositiveSmallIntegerField(_("Attempts"), default=0)

    created_at = models.DateTimeField(_("Created At"), auto_now_add=True)
    updated_at = models.DateTimeField(_("Updated At"), auto_now=True)

    class Meta:
        verbose_name = _("Contact Verification")
        verbose_name_plural = _("Contact Verifications")
        db_table = "contact_verification"
        unique_together = [("contact", "channel")]
        indexes = [
            models.Index(fields=["contact", "channel"], name="contact_verif_lookup_idx"),
            models.Index(fields=["status"], name="contact_verif_status_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.channel} ({self.get_status_display()}) — {self.contact.user.email_or_phone}"

    def clean(self) -> None:
        """
        Reject any `channel` value that isn't one of ContactInfo's
        registered messaging-channel field names. Avoids silently
        creating a verification row that no lookup will ever match
        (e.g. channel='whatsapp' instead of 'whatsapp_number').
        """
        super().clean()
        if self.channel not in ContactInfo.CHANNEL_FIELD_NAMES:
            raise ValidationError({
                'channel': _(
                    "'%(channel)s' is not one of ContactInfo's messaging "
                    "channel field names."
                ) % {'channel': self.channel}
            })

    def save(self, *args, **kwargs):
        """Run validation, then persist -- same pattern as ContactInfo.save()."""
        self.full_clean()
        super().save(*args, **kwargs)

    def is_stale(self) -> bool:
        """True if the live ContactInfo value no longer matches what was verified."""
        current_value = getattr(self.contact, self.channel, None)
        return self.status == self.Status.VERIFIED and current_value != self.verified_value