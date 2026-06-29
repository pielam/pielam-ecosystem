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
# PROFILE INFO MANAGER
# ====================================================================

class ProfileInfoManager(models.Manager):
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
    
    class ProfileType(models.TextChoices):
        PERSONAL = 'personal', _('Personal')
        BUSINESS = 'business', _('Business')
        PROFESSIONAL = 'professional', _('Professional')
        PUBLIC_FIGURE = 'public_figure', _('Public Figure')
    
    class VerificationLevel(models.TextChoices):
        NONE = 'none', _('Not Verified')
        EMAIL = 'email', _('Email Verified')
        PHONE = 'phone', _('Phone Verified')
        ID = 'id', _('ID Verified')
        BUSINESS = 'business', _('Business Verified')
    
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
    
    profile_tagline = models.CharField(
        _("Tagline"),
        max_length=100,
        blank=True,
        null=True,
        help_text=_("Short tagline or headline")
    )
    
    profile_type = models.CharField(
        _("Profile Type"),
        max_length=20,
        choices=ProfileType.choices,
        default=ProfileType.PERSONAL,
        help_text=_("Type of profile")
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
    
    profile_phone = models.CharField(
        _("Phone Number"),
        max_length=20,
        blank=True,
        null=True,
        help_text=_("Contact phone number")
    )
    
    profile_language = models.CharField(
        _("Preferred Language"),
        max_length=10,
        blank=True,
        null=True,
        help_text=_("Preferred language code (e.g., 'en', 'bn')")
    )
    
    # ================================================================
    # LOCATION / ADDRESS
    # ================================================================
    
    profile_country = models.CharField(
        _("Country"),
        max_length=2,
        blank=True,
        null=True,
        help_text=_("ISO 3166-1 alpha-2 country code")
    )
    
    profile_state = models.CharField(
        _("State/Province"),
        max_length=100,
        blank=True,
        null=True,
        help_text=_("State, province, or region")
    )
    
    profile_city = models.CharField(
        _("City"),
        max_length=100,
        blank=True,
        null=True,
        help_text=_("City")
    )
    
    profile_address = models.TextField(
        _("Address"),
        max_length=255,
        blank=True,
        null=True,
        help_text=_("Full street address")
    )
    
    profile_postal_code = models.CharField(
        _("Postal Code"),
        max_length=20,
        blank=True,
        null=True,
        help_text=_("Postal or ZIP code")
    )
    
    # Coordinates for location-based features
    profile_latitude = models.DecimalField(
        _("Latitude"),
        max_digits=9,
        decimal_places=6,
        blank=True,
        null=True,
        help_text=_("GPS latitude coordinate")
    )
    
    profile_longitude = models.DecimalField(
        _("Longitude"),
        max_digits=9,
        decimal_places=6,
        blank=True,
        null=True,
        help_text=_("GPS longitude coordinate")
    )
    
    # ================================================================
    # PROFESSIONAL INFORMATION (FOR DEALERS/BUSINESSES)
    # ================================================================
    
    business_name = models.CharField(
        _("Business Name"),
        max_length=150,
        blank=True,
        null=True,
        help_text=_("Legal business name")
    )
    
    business_type = models.CharField(
        _("Business Type"),
        max_length=50,
        blank=True,
        null=True,
        help_text=_("Type of business (e.g., Retail, Wholesale)")
    )
    
    business_registration = models.CharField(
        _("Business Registration Number"),
        max_length=100,
        blank=True,
        null=True,
        help_text=_("Government registration or license number")
    )
    
    business_tax_id = models.CharField(
        _("Tax ID"),
        max_length=50,
        blank=True,
        null=True,
        help_text=_("Tax identification number")
    )
    
    business_website = models.URLField(
        _("Business Website"),
        max_length=200,
        blank=True,
        null=True,
        help_text=_("Company website URL")
    )
    
    business_email = models.EmailField(
        _("Business Email"),
        max_length=150,
        blank=True,
        null=True,
        help_text=_("Business contact email")
    )
    
    business_phone = models.CharField(
        _("Business Phone"),
        max_length=20,
        blank=True,
        null=True,
        help_text=_("Business contact phone")
    )
    
    business_description = models.TextField(
        _("Business Description"),
        max_length=1000,
        blank=True,
        null=True,
        help_text=_("Description of business and services")
    )
    
    # ================================================================
    # SOCIAL MEDIA LINKS
    # ================================================================
    
    social_facebook = models.URLField(
        _("Facebook"),
        max_length=200,
        blank=True,
        null=True
    )
    
    social_twitter = models.URLField(
        _("Twitter/X"),
        max_length=200,
        blank=True,
        null=True
    )
    
    social_instagram = models.URLField(
        _("Instagram"),
        max_length=200,
        blank=True,
        null=True
    )
    
    social_linkedin = models.URLField(
        _("LinkedIn"),
        max_length=200,
        blank=True,
        null=True
    )
    
    social_youtube = models.URLField(
        _("YouTube"),
        max_length=200,
        blank=True,
        null=True
    )
    
    social_tiktok = models.URLField(
        _("TikTok"),
        max_length=200,
        blank=True,
        null=True
    )
    
    social_whatsapp = models.CharField(
        _("WhatsApp"),
        max_length=20,
        blank=True,
        null=True,
        help_text=_("WhatsApp number with country code")
    )
    
    # ================================================================
    # VERIFICATION & STATUS
    # ================================================================
    
    verification_level = models.CharField(
        _("Verification Level"),
        max_length=20,
        choices=VerificationLevel.choices,
        default=VerificationLevel.NONE,
        help_text=_("Level of profile verification")
    )
    
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
    
    notify_on_comment = models.BooleanField(
        _("Notify on Comment"),
        default=True,
        help_text=_("Get notified on comments")
    )
    
    notify_on_mention = models.BooleanField(
        _("Notify on Mention"),
        default=True,
        help_text=_("Get notified when mentioned")
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
            models.Index(fields=['verification_level']),
            models.Index(fields=['profile_type']),
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
        return f"<ProfileInfo: {self.profile_name or self.user} ({self.profile_type})>"
    
    # ================================================================
    # PROPERTIES
    # ================================================================
    
    @property
    def full_name(self) -> str:
        """Get full display name"""
        if self.profile_name:
            return self.profile_name
        if hasattr(self.user, 'email') and self.user.email:
            return self.user.email.split('@')[0]
        return self.user.email_or_phone
    
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
    def is_business(self) -> bool:
        """Check if this is a business profile"""
        return self.profile_type in [
            self.ProfileType.BUSINESS,
            self.ProfileType.PROFESSIONAL
        ]
    
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
            self.profile_city,
            self.profile_country,
        ]
        
        if self.is_business:
            fields.extend([
                self.business_name,
                self.business_description,
                self.business_email,
            ])
        
        completed = sum(1 for field in fields if field)
        return int((completed / len(fields)) * 100)
    
    @property
    def location_display(self) -> str:
        """Get formatted location string"""
        parts = []
        if self.profile_city:
            parts.append(self.profile_city)
        if self.profile_state:
            parts.append(self.profile_state)
        if self.profile_country:
            parts.append(self.profile_country)
        return ', '.join(parts) if parts else ''
    
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
    
    def verify(self, verified_by_user=None, level=None, save: bool = True) -> None:
        """Mark profile as verified"""
        self.is_profile_verified = True
        self.verified_at = timezone.now()
        self.verified_by = verified_by_user
        
        if level:
            self.verification_level = level
        
        if save:
            self.save(update_fields=[
                'is_profile_verified',
                'verified_at',
                'verified_by',
                'verification_level'
            ])
    
    def unverify(self, save: bool = True) -> None:
        """Remove verification"""
        self.is_profile_verified = False
        self.verified_at = None
        self.verified_by = None
        self.verification_level = self.VerificationLevel.NONE
        
        if save:
            self.save(update_fields=[
                'is_profile_verified',
                'verified_at',
                'verified_by',
                'verification_level'
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
        """Override delete to use soft delete"""
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
        """Block a user"""
        if user != self.user and user not in self.blocked_users.all():
            self.blocked_users.add(user)
            
            # Remove from followers/following
            self.followers.remove(user)
            self.following.remove(user)
    
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
            'profile_type': self.profile_type,
            'gender': self.profile_gender,
            'date_of_birth': str(self.profile_dob) if self.profile_dob else None,
            'age': self.age,
            'location': {
                'country': self.profile_country,
                'state': self.profile_state,
                'city': self.profile_city,
                'address': self.profile_address,
                'postal_code': self.profile_postal_code,
            },
            'business_info': {
                'name': self.business_name,
                'type': self.business_type,
                'registration': self.business_registration,
                'website': self.business_website,
                'email': self.business_email,
                'phone': self.business_phone,
            } if self.is_business else None,
            'social_media': {
                'facebook': self.social_facebook,
                'twitter': self.social_twitter,
                'instagram': self.social_instagram,
                'linkedin': self.social_linkedin,
                'youtube': self.social_youtube,
            },
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
            age = self.age
            if age and age < 13:
                raise ValidationError(
                    _("You must be at least 13 years old to create a profile")
                )
        
        # Validate business fields for business profiles
        if self.profile_type in [self.ProfileType.BUSINESS, self.ProfileType.PROFESSIONAL]:
            if not self.business_name:
                raise ValidationError(
                    _("Business name is required for business profiles")
                )
    
    def save(self, *args, **kwargs):
        """Override save to generate slug and run validation"""
        # Generate slug if name exists but slug doesn't
        if self.profile_name and not self.profile_name_slug:
            self.generate_slug()
        
        self.full_clean()
        super().save(*args, **kwargs)