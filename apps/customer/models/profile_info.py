# apps/customer/models/profile_info.py

"""
Enhanced ProfileInfo Model for International Business Standards
---------------------------------------------------------------
Features:
- Enhanced profile management
- Social features (followers/following)
- Privacy controls
- Verification badges
- Professional profiles for dealers
- Social media links
- Address management (international)
- Profile analytics
- Notification preferences
- GDPR compliance

CHANGELOG (bug-fix pass)
-------------------------
1. block_user() only updated the caller's own followers/following M2M.
   followers/following are two independent M2M tables (not a symmetric
   self-relation), so every other method that touches them
   (follow_profile/unfollow_profile) carefully updates both sides. This
   one didn't: after A blocks B, A stops following B and B disappears
   from A's followers, but B's own ProfileInfo still lists A as a
   follower -> the follow graph goes out of sync. Fixed by also
   removing `self.user` from the blocked user's own followers/following.

2. delete() was overridden to soft-delete, but only instance-level
   `profile.delete()` goes through that override -- a queryset bulk
   call like `ProfileInfo.objects.filter(...).delete()` bypasses it
   completely and hard-deletes rows. Fixed by giving the manager a
   custom queryset whose `.delete()` soft-deletes every row instead,
   with an explicit `.hard_delete()` escape hatch for when a real
   delete is genuinely needed.

3. clean()'s under-13 check reused the "must be at least 13" message
   for a DOB in the future too (a negative age is always < 13), which
   is a confusing error for that case. Fixed to report "date of birth
   cannot be in the future" separately.

4. Removed the ProfileType enum/field entirely (PERSONAL/BUSINESS/
   PROFESSIONAL/PUBLIC_FIGURE) per product decision -- no more explicit
   profile-category flag. is_business is now inferred from whether
   business_name is filled in rather than a declared type, and the
   "business_name required for business profiles" validation in
   clean() was dropped since it would otherwise be circular (business
   status is now defined BY business_name being set). Downstream
   consumers of the old field -- profile.py and dashboard.py both read
   profile_info.profile_type / get_profile_type_display() -- will need
   updating separately; they are not touched by this file.

5. Removed the VerificationLevel enum/field entirely (NONE/EMAIL/
   PHONE/ID/BUSINESS) per product decision -- verification is now a
   plain boolean (is_profile_verified) with no granularity. verify()
   dropped its now-meaningless `level` parameter accordingly. Same
   downstream caveat as #4: profile.py and dashboard.py both read
   profile_info.verification_level directly and will need separate
   updates.

6. Removed the profile_tagline field per product decision. Nothing
   else inside this file referenced it, but it's still read directly
   by dashboard.py's build_user_profile_snapshot() ("tagline":
   profile.profile_tagline) and posted to by profile_edit.py /
   profile_managers.py's PROFILE_TEXT_FIELDS whitelist -- none of
   which are touched by this file.

7. Removed the profile_phone field per product decision. Nothing else
   inside this file referenced it, but it's still read by
   public_profile.py's dealer card serializer ("dealer_phone":
   profile_info.profile_phone if profile_info.show_phone else ""),
   and posted to by profile_edit.py / profile_managers.py's
   PROFILE_TEXT_FIELDS whitelist -- none of which are touched by this
   file. Note User (account.py) has its own separate `phone` field --
   unaffected, this was ProfileInfo's own copy.

8. Removed the entire LOCATION/ADDRESS block (profile_country,
   profile_state, profile_city, profile_address, profile_postal_code,
   profile_latitude, profile_longitude), the entire PROFESSIONAL
   INFORMATION block (business_name, business_type,
   business_registration, business_tax_id, business_website,
   business_email, business_phone, business_description), and the
   entire SOCIAL MEDIA LINKS block (social_facebook, social_twitter,
   social_instagram, social_linkedin, social_youtube, social_tiktok,
   social_whatsapp) per product decision. This forced two more
   internal removals: `is_business` (had nothing left to check now
   that business_name is gone) and `location_display` (had nothing
   left to format now that city/state/country are gone) -- both
   properties were deleted outright rather than patched, since neither
   has a meaningful definition anymore. completion_percentage and
   export_data() were trimmed to drop every reference to all of the
   above.

   `show_location` (the privacy toggle) was deliberately left in place
   even though it's now a no-op -- removing a field that wasn't named
   in this request felt like overreach; flagging it as dead weight
   rather than acting on it unilaterally.

   Downstream breakage from this one is large and mostly NOT limited
   to reads (which fail loudly) -- some are silent:
     - dashboard.py's build_user_profile_snapshot() reads
       profile.is_business, .business_name, .social_facebook (etc.),
       and .location_display directly -> AttributeError, will crash
       the dashboard page outright.
     - profile.py's _load_profile_context() reads the same business_*/
       social_*/location_display/profile_address/profile_postal_code
       fields into its cached context dict -> same crash.
     - profile_edit.py and profile_managers.py's PROFILE_TEXT_FIELDS/
       BUSINESS_FIELDS/SOCIAL_FIELDS whitelists still setattr() these
       names from POST data onto a ProfileInfo instance. Since Django
       model instances allow arbitrary attribute assignment, this does
       NOT raise -- it silently creates a throwaway Python attribute
       that full_clean()/save() ignore, so any business/social/address
       form submission would appear to succeed while quietly saving
       nothing. This is worse than the read-side crashes precisely
       because it fails silently.
   None of the above are addressed by this file.

9. Removed notify_on_comment and notify_on_mention per product
   decision. Nothing else inside this file referenced either (unlike
   notify_on_follow, which add_follower() actually checks before
   calling send_notification() -- these two were never wired to any
   logic in this file to begin with). NOTIFICATION_BOOL_FIELDS in
   profile_managers.py still lists both names for its update_fields
   setattr() loop -- same silent-no-op pattern as #8's business/social
   fields: the "update_notifications" action will keep accepting and
   discarding these two toggles from POST data without error.

10. profile_name now defaults to "Anonymous" instead of being left
    blank/null, enforced at two points: the field's `default` (covers
    brand-new rows, e.g. create_user_profile's signal-created
    ProfileInfo with no name supplied) and save() (covers a user
    later explicitly clearing it back to empty via profile_edit.py's
    `.strip() or None` pattern). This mirrors User.display_name's
    existing "Anonymous" fallback in account.py, except that fallback
    was only ever computed at read time -- this makes the same
    fallback an actual persisted value on ProfileInfo itself.
"""

import uuid
from datetime import datetime, date
from typing import Optional, List

from django.db import models
from django.conf import settings
from django.utils import timezone
from django.utils.text import slugify
from django.core.exceptions import ValidationError
from django.utils.translation import gettext_lazy as _
from django.db.models import Q, Count


# ====================================================================
# PROFILE INFO QUERYSET
# ====================================================================

class ProfileInfoQuerySet(models.QuerySet):
    """
    Custom queryset so the soft-delete (archive) guarantee also holds
    for queryset-level bulk operations, not just instance.delete()
    calls -- mirrors the same fix applied to User/UserQuerySet.
    """

    def delete(self):
        """
        Redirect bulk `.delete()` to soft_delete() (archive) for every
        row in the queryset, so `ProfileInfo.objects.filter(...).delete()`
        can't silently bypass archiving the way a bare queryset delete
        otherwise would. Returns a Django-delete-style
        (count, {label: count}) tuple for API compatibility.
        """
        count = 0
        for obj in self:
            obj.soft_delete()
            count += 1
        return count, {self.model._meta.label: count}

    def hard_delete(self):
        """Escape hatch for an actual, irreversible bulk delete."""
        return super().delete()


# ====================================================================
# PROFILE INFO MANAGER
# ====================================================================

class ProfileInfoManager(models.Manager.from_queryset(ProfileInfoQuerySet)):
    """
    Custom manager for ProfileInfo with optimized queries
    """

    def create_profile(self, user, **kwargs):
        """Create a new profile for a user"""
        profile = self.model(user=user, **kwargs)
        
        # Auto-generate slug if name provided
        if kwargs.get('profile_name'):
            profile.generate_slug()
        
        profile.save(using=self._db)
        return profile

    def get_public_profiles(self):
        """Get all public, non-archived profiles"""
        return self.filter(
            is_profile_public=True,
            is_profile_archived=False,
            user__is_active=True,
            user__deleted_at__isnull=True
        ).select_related('user')

    def get_verified_profiles(self):
        """Get all verified profiles"""
        return self.filter(
            is_profile_verified=True,
            is_profile_archived=False,
            user__is_active=True
        ).select_related('user')

    def get_featured_profiles(self):
        """Get featured profiles"""
        return self.filter(
            is_profile_featured=True,
            is_profile_archived=False,
            user__is_active=True
        ).select_related('user')

    def get_archived_profiles(self):
        """Get archived profiles"""
        return self.filter(is_profile_archived=True).select_related('user')

    def get_suspended_profiles(self):
        """Get suspended profiles"""
        return self.filter(is_profile_suspended=True).select_related('user')

    def update_profile(self, profile_id, **kwargs):
        """Update profile with given fields"""
        profile = self.get(pk=profile_id)
        
        # Handle slug regeneration if name changed
        if 'profile_name' in kwargs:
            profile.profile_name = kwargs.pop('profile_name')
            profile.generate_slug()
        
        for key, value in kwargs.items():
            setattr(profile, key, value)
        
        profile.save(using=self._db)
        return profile

    def delete_profile(self, profile_id):
        """Soft delete a profile"""
        profile = self.get(pk=profile_id)
        profile.soft_delete()
        return profile

    def get_profile_by_user(self, user):
        """Get profile by user instance"""
        return self.select_related('user').get(user=user)

    def get_profiles_by_role(self, role):
        """Get profiles by user role"""
        return self.filter(
            user__role=role,
            is_profile_archived=False,
            user__is_active=True
        ).select_related('user')

    def search_profiles(self, search_term):
        """Search profiles by name, bio, or username"""
        return self.filter(
            Q(profile_name__icontains=search_term) |
            Q(profile_bio__icontains=search_term) |
            Q(profile_name_slug__icontains=search_term),
            is_profile_archived=False,
            user__is_active=True
        ).select_related('user')

    def get_recent_profiles(self, days=30):
        """Get profiles created in last N days"""
        date_threshold = timezone.now() - timezone.timedelta(days=days)
        return self.filter(
            profile_creation_time__gte=date_threshold,
            is_profile_archived=False,
            user__is_active=True
        ).select_related('user')

    def get_popular_profiles(self, limit=10):
        """Get most popular profiles by follower count"""
        return self.filter(
            is_profile_archived=False,
            user__is_active=True
        ).annotate(
            follower_count=Count('followers')
        ).order_by('-follower_count')[:limit]

    def set_profile_status(self, profile_id, **kwargs):
        """Set profile status flags"""
        profile = self.get(pk=profile_id)
        allowed = [
            "is_profile_archived",
            "is_profile_public",
            "is_profile_featured",
            "is_profile_suspended",
            "is_profile_verified",
        ]
        for key, value in kwargs.items():
            if key in allowed:
                setattr(profile, key, value)
        profile.save(using=self._db)
        return profile


# ====================================================================
# PROFILE INFO MODEL
# ====================================================================

class ProfileInfo(models.Model):
    """
    Enhanced User Profile Model
    
    Features:
    - Complete profile information
    - Social features (followers/following)
    - Privacy controls
    - Professional information for dealers
    - Address management (international)
    - Social media links
    - Notification preferences
    - Profile analytics
    """
    
    # ================================================================
    # CHOICES
    # ================================================================
    
    class Gender(models.TextChoices):
        MALE = 'male', _('Male')
        FEMALE = 'female', _('Female')
        NON_BINARY = 'non_binary', _('Non-Binary')
        OTHER = 'other', _('Other')
        PREFER_NOT_TO_SAY = 'prefer_not_to_say', _('Prefer not to say')
    
    # ================================================================
    # PRIMARY FIELDS
    # ================================================================
    
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        primary_key=True,
        related_name='profileinfo',
        help_text=_("User account associated with this profile")
    )
    
    # UUID for external references
    uuid = models.UUIDField(
        default=uuid.uuid4,
        editable=False,
        unique=True,
        db_index=True,
        help_text=_("Unique identifier for external API references")
    )
    
    # ================================================================
    # BASIC INFORMATION
    # ================================================================
    
    profile_name = models.CharField(
        _("Display Name"),
        max_length=100,
        default="Anonymous",
        blank=True,
        null=True,
        help_text=_("Public display name")
    )
    
    profile_name_slug = models.SlugField(
        _("Profile Slug"),
        max_length=120,
        unique=True,
        blank=True,
        null=True,
        db_index=True,
        help_text=_("URL-friendly version of profile name")
    )
    
    profile_bio = models.TextField(
        _("Bio"),
        max_length=500,
        blank=True,
        null=True,
        help_text=_("Short biography or description")
    )
    
    # ================================================================
    # IMAGES
    # ================================================================
    
    profile_photo = models.ImageField(
        _("Profile Photo"),
        upload_to="profile-pics/%Y/%m/",
        blank=True,
        null=True,
        help_text=_("Profile picture")
    )
    
    profile_cover_photo = models.ImageField(
        _("Cover Photo"),
        upload_to="cover-pics/%Y/%m/",
        blank=True,
        null=True,
        help_text=_("Cover or banner image")
    )
    
    # ================================================================
    # PERSONAL INFORMATION
    # ================================================================
    
    profile_gender = models.CharField(
        _("Gender"),
        max_length=20,
        choices=Gender.choices,
        blank=True,
        null=True,
        help_text=_("Gender identity")
    )
    
    profile_dob = models.DateField(
        _("Date of Birth"),
        blank=True,
        null=True,
        help_text=_("Date of birth (kept private)")
    )
    
    profile_language = models.CharField(
        _("Preferred Language"),
        max_length=10,
        blank=True,
        null=True,
        help_text=_("Preferred language code (e.g., 'en', 'bn')")
    )
    
    # ================================================================
    # VERIFICATION & STATUS
    # ================================================================
    
    is_profile_verified = models.BooleanField(
        _("Verified"),
        default=False,
        help_text=_("Profile has been verified by administrators")
    )
    
    verified_at = models.DateTimeField(
        _("Verified At"),
        blank=True,
        null=True,
        help_text=_("When profile was verified")
    )
    
    verified_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='verified_profiles',
        help_text=_("Admin who verified this profile")
    )
    
    is_profile_public = models.BooleanField(
        _("Public Profile"),
        default=True,
        help_text=_("Profile is visible to public")
    )
    
    is_profile_featured = models.BooleanField(
        _("Featured"),
        default=False,
        db_index=True,
        help_text=_("Profile is featured on homepage")
    )
    
    is_profile_suspended = models.BooleanField(
        _("Suspended"),
        default=False,
        help_text=_("Profile is suspended")
    )
    
    suspended_at = models.DateTimeField(
        _("Suspended At"),
        blank=True,
        null=True
    )
    
    suspension_reason = models.TextField(
        _("Suspension Reason"),
        max_length=500,
        blank=True,
        null=True
    )
    
    # ================================================================
    # PRIVACY SETTINGS
    # ================================================================
    
    show_email = models.BooleanField(
        _("Show Email"),
        default=False,
        help_text=_("Display email on public profile")
    )
    
    show_phone = models.BooleanField(
        _("Show Phone"),
        default=False,
        help_text=_("Display phone on public profile")
    )
    
    show_dob = models.BooleanField(
        _("Show Date of Birth"),
        default=False,
        help_text=_("Display date of birth on profile")
    )
    
    show_age = models.BooleanField(
        _("Show Age"),
        default=False,
        help_text=_("Display age on profile")
    )
    
    show_location = models.BooleanField(
        _("Show Location"),
        default=True,
        help_text=_("Display city/country on profile")
    )
    
    show_followers = models.BooleanField(
        _("Show Followers"),
        default=True,
        help_text=_("Display follower count")
    )
    
    show_following = models.BooleanField(
        _("Show Following"),
        default=True,
        help_text=_("Display following count")
    )
    
    allow_messages = models.BooleanField(
        _("Allow Messages"),
        default=True,
        help_text=_("Allow other users to send messages")
    )
    
    allow_follow = models.BooleanField(
        _("Allow Follow"),
        default=True,
        help_text=_("Allow other users to follow")
    )
    
    # ================================================================
    # NOTIFICATION PREFERENCES
    # ================================================================
    
    notify_on_follow = models.BooleanField(
        _("Notify on Follow"),
        default=True,
        help_text=_("Get notified when someone follows")
    )
    
    notify_on_message = models.BooleanField(
        _("Notify on Message"),
        default=True,
        help_text=_("Get notified on new messages")
    )
    
    email_notifications = models.BooleanField(
        _("Email Notifications"),
        default=True,
        help_text=_("Receive email notifications")
    )
    
    sms_notifications = models.BooleanField(
        _("SMS Notifications"),
        default=False,
        help_text=_("Receive SMS notifications")
    )
    
    # ================================================================
    # SOCIAL FEATURES
    # ================================================================
    
    followers = models.ManyToManyField(
        settings.AUTH_USER_MODEL,
        related_name="following_profiles",
        blank=True,
        help_text=_("Users following this profile")
    )
    
    following = models.ManyToManyField(
        settings.AUTH_USER_MODEL,
        related_name="follower_profiles",
        blank=True,
        help_text=_("Profiles this user is following")
    )
    
    blocked_users = models.ManyToManyField(
        settings.AUTH_USER_MODEL,
        related_name="blocked_by_profiles",
        blank=True,
        help_text=_("Users blocked by this profile")
    )
    
    # ================================================================
    # ANALYTICS
    # ================================================================
    
    profile_views = models.PositiveIntegerField(
        _("Profile Views"),
        default=0,
        help_text=_("Total profile view count")
    )
    
    last_viewed_at = models.DateTimeField(
        _("Last Viewed At"),
        blank=True,
        null=True,
        help_text=_("Last time profile was viewed")
    )
    
    # ================================================================
    # TIMESTAMPS
    # ================================================================
    
    profile_creation_time = models.DateTimeField(
        _("Created At"),
        auto_now_add=True,
        help_text=_("When profile was created")
    )
    
    profile_updated_time = models.DateTimeField(
        _("Updated At"),
        auto_now=True,
        help_text=_("Last time profile was updated")
    )
    
    # ================================================================
    # SOFT DELETE
    # ================================================================
    
    is_profile_archived = models.BooleanField(
        _("Archived"),
        default=False,
        db_index=True,
        help_text=_("Profile is archived (soft deleted)")
    )
    
    archived_at = models.DateTimeField(
        _("Archived At"),
        blank=True,
        null=True
    )
    
    archived_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='archived_profiles',
        help_text=_("User who archived this profile")
    )
    
    # ================================================================
    # METADATA
    # ================================================================
    
    metadata = models.JSONField(
        _("Metadata"),
        default=dict,
        blank=True,
        help_text=_("Additional profile metadata in JSON format")
    )
    
    # ================================================================
    # MANAGER
    # ================================================================
    
    objects = ProfileInfoManager()
    
    # ================================================================
    # META
    # ================================================================
    
    class Meta:
        verbose_name = _("Profile Info")
        verbose_name_plural = _("Profile Infos")
        db_table = 'profile_info'
        ordering = ['-profile_creation_time']
        indexes = [
            models.Index(fields=['uuid']),
            models.Index(fields=['profile_name_slug']),
            models.Index(fields=['is_profile_public', 'is_profile_archived']),
            models.Index(fields=['is_profile_verified']),
            models.Index(fields=['is_profile_featured']),
            models.Index(fields=['profile_creation_time']),
        ]
    
    # ================================================================
    # STRING REPRESENTATION
    # ================================================================
    
    def __str__(self) -> str:
        """Return profile name or user identifier"""
        if self.profile_name:
            return self.profile_name
        return str(self.user)
    
    def __repr__(self) -> str:
        return f"<ProfileInfo: {self.profile_name or self.user}>"
    
    # ================================================================
    # PROPERTIES
    # ================================================================
    
    @property
    def age(self) -> Optional[int]:
        """Calculate age from date of birth"""
        if not self.profile_dob:
            return None
        today = date.today()
        return (
            today.year
            - self.profile_dob.year
            - ((today.month, today.day) < (self.profile_dob.month, self.profile_dob.day))
        )
    
    @property
    def follower_count(self) -> int:
        """Get follower count"""
        return self.followers.count()
    
    @property
    def following_count(self) -> int:
        """Get following count"""
        return self.following.count()
    
    @property
    def is_complete(self) -> bool:
        """Check if profile is complete"""
        required = [
            self.profile_name,
            self.profile_photo,
            self.profile_bio,
        ]
        return all(required)
    
    @property
    def is_verified(self) -> bool:
        """Check if profile is verified"""
        return self.is_profile_verified
    
    @property
    def completion_percentage(self) -> int:
        """Calculate profile completion percentage"""
        fields = [
            self.profile_name,
            self.profile_bio,
            self.profile_photo,
            self.profile_cover_photo,
            self.profile_gender,
            self.profile_dob,
        ]
        
        completed = sum(1 for field in fields if field)
        return int((completed / len(fields)) * 100)
    
    @property
    def profile_url(self) -> str:
        """Get profile URL slug"""
        if self.profile_name_slug:
            return f"/profile/{self.profile_name_slug}/"
        return f"/profile/{self.user.uuid}/"
    
    # ================================================================
    # IMAGE METHODS
    # ================================================================
    
    def get_profile_photo_url(self) -> str:
        """Get profile photo URL or default"""
        if self.profile_photo:
            return self.profile_photo.url
        return "/static/defaults/default-profile-picture.png"
    
    def get_profile_cover_photo_url(self) -> str:
        """Get cover photo URL or default"""
        if self.profile_cover_photo:
            return self.profile_cover_photo.url
        return "/static/defaults/default-cover-picture.png"
    
    def set_profile_photo(self, photo, save: bool = True) -> None:
        """Set profile photo"""
        self.profile_photo = photo
        if save:
            self.save(update_fields=['profile_photo'])
    
    def set_profile_cover_photo(self, photo, save: bool = True) -> None:
        """Set cover photo"""
        self.profile_cover_photo = photo
        if save:
            self.save(update_fields=['profile_cover_photo'])
    
    # ================================================================
    # SLUG METHODS
    # ================================================================
    
    def generate_slug(self, save: bool = False) -> str:
        """Generate unique slug from profile name"""
        if not self.profile_name:
            return None
        
        base_slug = slugify(self.profile_name)
        slug = base_slug
        counter = 1
        
        # Ensure uniqueness
        while ProfileInfo.objects.filter(
            profile_name_slug=slug
        ).exclude(user=self.user).exists():
            slug = f"{base_slug}-{counter}"
            counter += 1
        
        self.profile_name_slug = slug
        
        if save:
            self.save(update_fields=['profile_name_slug'])
        
        return slug
    
    # ================================================================
    # VERIFICATION METHODS
    # ================================================================
    
    def verify(self, verified_by_user=None, save: bool = True) -> None:
        """Mark profile as verified"""
        self.is_profile_verified = True
        self.verified_at = timezone.now()
        self.verified_by = verified_by_user
        
        if save:
            self.save(update_fields=[
                'is_profile_verified',
                'verified_at',
                'verified_by',
            ])
    
    def unverify(self, save: bool = True) -> None:
        """Remove verification"""
        self.is_profile_verified = False
        self.verified_at = None
        self.verified_by = None
        
        if save:
            self.save(update_fields=[
                'is_profile_verified',
                'verified_at',
                'verified_by',
            ])
    
    # ================================================================
    # STATUS METHODS
    # ================================================================
    
    def suspend(self, reason: str = None, save: bool = True) -> None:
        """Suspend profile"""
        self.is_profile_suspended = True
        self.suspended_at = timezone.now()
        self.suspension_reason = reason
        
        if save:
            self.save(update_fields=[
                'is_profile_suspended',
                'suspended_at',
                'suspension_reason'
            ])
    
    def unsuspend(self, save: bool = True) -> None:
        """Remove suspension"""
        self.is_profile_suspended = False
        self.suspended_at = None
        self.suspension_reason = None
        
        if save:
            self.save(update_fields=[
                'is_profile_suspended',
                'suspended_at',
                'suspension_reason'
            ])
    
    def make_public(self, save: bool = True) -> None:
        """Make profile public"""
        self.is_profile_public = True
        if save:
            self.save(update_fields=['is_profile_public'])
    
    def make_private(self, save: bool = True) -> None:
        """Make profile private"""
        self.is_profile_public = False
        if save:
            self.save(update_fields=['is_profile_public'])
    
    def feature(self, save: bool = True) -> None:
        """Mark profile as featured"""
        self.is_profile_featured = True
        if save:
            self.save(update_fields=['is_profile_featured'])
    
    def unfeature(self, save: bool = True) -> None:
        """Remove featured status"""
        self.is_profile_featured = False
        if save:
            self.save(update_fields=['is_profile_featured'])
    
    # ================================================================
    # SOFT DELETE METHODS
    # ================================================================
    
    def soft_delete(self, archived_by_user=None, save: bool = True) -> None:
        """Soft delete profile (archive)"""
        self.is_profile_archived = True
        self.archived_at = timezone.now()
        self.archived_by = archived_by_user
        
        if save:
            self.save()
    
    def restore(self, save: bool = True) -> None:
        """Restore archived profile"""
        self.is_profile_archived = False
        self.archived_at = None
        self.archived_by = None
        
        if save:
            self.save()
    
    def delete(self, *args, **kwargs):
        """
        Override instance delete to use soft delete (archive). Note this
        only covers instance-level `.delete()` calls -- queryset-level
        bulk deletes are handled separately by ProfileInfoQuerySet.delete()
        above, so `ProfileInfo.objects.filter(...).delete()` archives too
        instead of bypassing this override.
        """
        self.soft_delete()
    
    # ================================================================
    # SOCIAL METHODS
    # ================================================================
    
    def add_follower(self, user) -> bool:
        """Add a follower"""
        if not self.allow_follow:
            return False
        
        if user == self.user:
            return False  # Can't follow yourself
        
        if user not in self.followers.all():
            self.followers.add(user)
            
            # Send notification if enabled
            if self.notify_on_follow:
                self.send_notification('new_follower', user)
            
            return True
        return False
    
    def remove_follower(self, user) -> bool:
        """Remove a follower"""
        if user in self.followers.all():
            self.followers.remove(user)
            return True
        return False
    
    def follow_profile(self, profile) -> bool:
        """Follow another profile"""
        if profile.user == self.user:
            return False  # Can't follow yourself
        
        if profile.user not in self.following.all():
            self.following.add(profile.user)
            profile.add_follower(self.user)
            return True
        return False
    
    def unfollow_profile(self, profile) -> bool:
        """Unfollow a profile"""
        if profile.user in self.following.all():
            self.following.remove(profile.user)
            profile.remove_follower(self.user)
            return True
        return False
    
    def is_following(self, user_or_profile) -> bool:
        """Check if following a user or profile"""
        if isinstance(user_or_profile, ProfileInfo):
            user = user_or_profile.user
        else:
            user = user_or_profile
        
        return user in self.following.all()
    
    def is_followed_by(self, user) -> bool:
        """Check if followed by a user"""
        return user in self.followers.all()
    
    def block_user(self, user) -> None:
        """
        Block a user.

        Removes the follow relationship on BOTH sides, not just this
        profile's own followers/following. followers/following are two
        independent M2M tables (not a symmetric self-relation), so
        without mirroring the change onto the blocked user's own
        ProfileInfo, the follow graph goes out of sync: this profile
        would stop following/being-followed-by `user`, but `user`'s own
        profile would still list this profile as a follower, since
        nothing ever told it otherwise.
        """
        if user != self.user and user not in self.blocked_users.all():
            self.blocked_users.add(user)

            # This side of the relationship.
            self.followers.remove(user)
            self.following.remove(user)

            # Mirror onto the blocked user's own profile so the graph
            # stays consistent from their side too. Every user should
            # have a ProfileInfo (see signals.create_user_profile), but
            # this is defensive in case of a race or a not-yet-created
            # profile.
            other_profile = getattr(user, 'profileinfo', None)
            if other_profile is not None:
                other_profile.followers.remove(self.user)
                other_profile.following.remove(self.user)
    
    def unblock_user(self, user) -> None:
        """Unblock a user"""
        if user in self.blocked_users.all():
            self.blocked_users.remove(user)
    
    def is_blocked(self, user) -> bool:
        """Check if user is blocked"""
        return user in self.blocked_users.all()
    
    # ================================================================
    # ANALYTICS METHODS
    # ================================================================
    
    def increment_view_count(self, save: bool = True) -> None:
        """Increment profile view count"""
        self.profile_views += 1
        self.last_viewed_at = timezone.now()
        
        if save:
            self.save(update_fields=['profile_views', 'last_viewed_at'])
    
    def get_engagement_score(self) -> float:
        """Calculate engagement score based on followers and views"""
        # Simple engagement score calculation
        follower_score = self.follower_count * 10
        view_score = self.profile_views * 0.1
        
        return follower_score + view_score
    
    # ================================================================
    # NOTIFICATION HELPER
    # ================================================================
    
    def send_notification(self, notification_type: str, related_user=None) -> None:
        """
        Send notification to profile owner
        This is a placeholder - implement based on your notification system
        """
        # TODO: Implement notification sending
        pass
    
    # ================================================================
    # DATA EXPORT (GDPR)
    # ================================================================
    
    def export_data(self) -> dict:
        """Export profile data for GDPR compliance"""
        data = {
            'uuid': str(self.uuid),
            'profile_name': self.profile_name,
            'profile_bio': self.profile_bio,
            'gender': self.profile_gender,
            'date_of_birth': str(self.profile_dob) if self.profile_dob else None,
            'age': self.age,
            'stats': {
                'followers': self.follower_count,
                'following': self.following_count,
                'profile_views': self.profile_views,
            },
            'created_at': self.profile_creation_time.isoformat(),
            'updated_at': self.profile_updated_time.isoformat(),
            'metadata': self.metadata,
        }
        
        return data
    
    # ================================================================
    # VALIDATION
    # ================================================================
    
    def clean(self) -> None:
        """Validate model fields"""
        super().clean()
        
        # Validate age (must be 13+ for most platforms)
        if self.profile_dob:
            if self.profile_dob > date.today():
                raise ValidationError(
                    _("Date of birth cannot be in the future")
                )
            age = self.age
            if age is not None and age < 13:
                raise ValidationError(
                    _("You must be at least 13 years old to create a profile")
                )
    
    def save(self, *args, **kwargs):
        """Override save to generate slug and run validation"""
        # profile_name should never be persisted empty -- the field's
        # default covers brand-new rows (e.g. the create_user_profile
        # signal creating a ProfileInfo with no name at all), but
        # doesn't help if a form later explicitly clears it back to ""
        # / None (see profile_edit.py's `.strip() or None`), so enforce
        # it here too at the point of persistence.
        if not self.profile_name:
            self.profile_name = "Anonymous"

        # Generate slug if name exists but slug doesn't
        if self.profile_name and not self.profile_name_slug:
            self.generate_slug()
        
        self.full_clean()
        super().save(*args, **kwargs)