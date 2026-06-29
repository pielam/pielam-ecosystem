# apps/ponno/models/brand.py

"""
Enhanced Brand Model for International Business Standards
---------------------------------------------------------
Features:
- Brand information management
- Logo and media support
- SEO optimization
- Verification and trust badges
- Brand analytics
- Multi-language support
- Soft delete
- Brand relationships
- Popularity tracking
"""

import uuid
from typing import Optional

from django.db import models
from django.conf import settings
from django.utils import timezone
from django.utils.text import slugify
from django.utils.translation import gettext_lazy as _
from django.core.exceptions import ValidationError
from django.core.validators import URLValidator
from django.db.models import Count, Q


# ====================================================================
# BRAND MANAGER
# ====================================================================

class BrandManager(models.Manager):
    """
    Custom manager for Brand with optimized queries
    """
    
    def active_brands(self):
        """Get all active, non-deleted brands"""
        return self.filter(
            is_active=True,
            deleted_at__isnull=True
        )
    
    def verified_brands(self):
        """Get verified brands only"""
        return self.filter(
            is_verified=True,
            is_active=True,
            deleted_at__isnull=True
        )
    
    def featured_brands(self):
        """Get featured brands"""
        return self.filter(
            is_featured=True,
            is_active=True,
            deleted_at__isnull=True
        )
    
    def popular_brands(self, limit=10):
        """Get popular brands by product count"""
        return self.filter(
            is_active=True,
            deleted_at__isnull=True
        ).annotate(
            product_count=Count('products')
        ).filter(
            product_count__gt=0
        ).order_by('-product_count')[:limit]
    
    def search_brands(self, query):
        """Search brands by name or description"""
        return self.filter(
            Q(brand_name__icontains=query) |
            Q(brand_description__icontains=query) |
            Q(brand_slug__icontains=query),
            is_active=True,
            deleted_at__isnull=True
        )
    
    def get_by_slug(self, slug):
        """Get brand by slug"""
        return self.get(
            brand_slug=slug,
            is_active=True,
            deleted_at__isnull=True
        )


# ====================================================================
# BRAND MODEL
# ====================================================================

class Brand(models.Model):
    """
    Enhanced Brand Model
    
    Features:
    - Complete brand information
    - Logo and images
    - Verification system
    - SEO optimization
    - Analytics tracking
    - Multi-language support
    - Soft delete
    """
    
    # ================================================================
    # CHOICES
    # ================================================================
    
    class BrandType(models.TextChoices):
        MANUFACTURER = 'manufacturer', _('Manufacturer')
        DISTRIBUTOR = 'distributor', _('Distributor')
        RETAILER = 'retailer', _('Retailer')
        PRIVATE_LABEL = 'private_label', _('Private Label')
        OTHER = 'other', _('Other')
    
    class VerificationStatus(models.TextChoices):
        UNVERIFIED = 'unverified', _('Unverified')
        PENDING = 'pending', _('Pending Verification')
        VERIFIED = 'verified', _('Verified')
        REJECTED = 'rejected', _('Verification Rejected')
    
    # ================================================================
    # PRIMARY FIELDS
    # ================================================================
    
    # UUID for external references
    uuid = models.UUIDField(
        _("UUID"),
        default=uuid.uuid4,
        editable=False,
        unique=True,
        db_index=True,
        help_text=_("Unique identifier for external API references")
    )
    
    brand_name = models.CharField(
        _("Brand Name"),
        max_length=150,
        unique=True,
        db_index=True,
        help_text=_("Official brand name")
    )
    
    brand_slug = models.SlugField(
        _("Brand Slug"),
        max_length=160,
        unique=True,
        blank=True,
        null=True,
        db_index=True,
        help_text=_("URL-friendly brand identifier")
    )
    
    brand_type = models.CharField(
        _("Brand Type"),
        max_length=20,
        choices=BrandType.choices,
        default=BrandType.MANUFACTURER,
        help_text=_("Type of brand")
    )
    
    # ================================================================
    # BRAND INFORMATION
    # ================================================================
    
    brand_description = models.TextField(
        _("Brand Description"),
        max_length=1000,
        blank=True,
        null=True,
        help_text=_("Detailed brand description")
    )
    
    brand_tagline = models.CharField(
        _("Brand Tagline"),
        max_length=200,
        blank=True,
        null=True,
        help_text=_("Brand slogan or tagline")
    )
    
    brand_story = models.TextField(
        _("Brand Story"),
        max_length=5000,
        blank=True,
        null=True,
        help_text=_("Brand history and story")
    )
    
    # ================================================================
    # MEDIA
    # ================================================================
    
    brand_logo = models.ImageField(
        _("Brand Logo"),
        upload_to="brands/logos/%Y/%m/",
        blank=True,
        null=True,
        help_text=_("Brand logo image")
    )
    
    brand_banner = models.ImageField(
        _("Brand Banner"),
        upload_to="brands/banners/%Y/%m/",
        blank=True,
        null=True,
        help_text=_("Brand banner/cover image")
    )
    
    brand_icon = models.ImageField(
        _("Brand Icon"),
        upload_to="brands/icons/%Y/%m/",
        blank=True,
        null=True,
        help_text=_("Brand icon/favicon")
    )
    
    # ================================================================
    # COMPANY INFORMATION
    # ================================================================
    
    company_name = models.CharField(
        _("Company Name"),
        max_length=200,
        blank=True,
        null=True,
        help_text=_("Legal company name behind the brand")
    )
    
    founded_year = models.PositiveIntegerField(
        _("Founded Year"),
        blank=True,
        null=True,
        help_text=_("Year the brand was founded")
    )
    
    country_of_origin = models.CharField(
        _("Country of Origin"),
        max_length=2,
        blank=True,
        null=True,
        help_text=_("ISO 3166-1 alpha-2 country code")
    )
    
    headquarters = models.CharField(
        _("Headquarters"),
        max_length=200,
        blank=True,
        null=True,
        help_text=_("Company headquarters location")
    )
    
    # ================================================================
    # CONTACT INFORMATION
    # ================================================================
    
    brand_website = models.URLField(
        _("Brand Website"),
        max_length=200,
        blank=True,
        null=True,
        help_text=_("Official brand website")
    )
    
    brand_email = models.EmailField(
        _("Brand Email"),
        max_length=150,
        blank=True,
        null=True,
        help_text=_("Brand contact email")
    )
    
    brand_phone = models.CharField(
        _("Brand Phone"),
        max_length=20,
        blank=True,
        null=True,
        help_text=_("Brand contact phone")
    )
    
    support_email = models.EmailField(
        _("Support Email"),
        max_length=150,
        blank=True,
        null=True,
        help_text=_("Customer support email")
    )
    
    support_phone = models.CharField(
        _("Support Phone"),
        max_length=20,
        blank=True,
        null=True,
        help_text=_("Customer support phone")
    )
    
    # ================================================================
    # SOCIAL MEDIA
    # ================================================================
    
    social_facebook = models.URLField(
        _("Facebook"),
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
    
    social_twitter = models.URLField(
        _("Twitter/X"),
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
    
    # ================================================================
    # VERIFICATION & TRUST
    # ================================================================
    
    is_verified = models.BooleanField(
        _("Verified"),
        default=False,
        db_index=True,
        help_text=_("Brand has been verified by administrators")
    )
    
    verification_status = models.CharField(
        _("Verification Status"),
        max_length=20,
        choices=VerificationStatus.choices,
        default=VerificationStatus.UNVERIFIED,
        help_text=_("Current verification status")
    )
    
    verified_at = models.DateTimeField(
        _("Verified At"),
        blank=True,
        null=True,
        help_text=_("When brand was verified")
    )
    
    verified_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='verified_brands',
        help_text=_("Admin who verified this brand")
    )
    
    is_official = models.BooleanField(
        _("Official Brand Account"),
        default=False,
        help_text=_("This is an official brand account")
    )
    
    is_trusted = models.BooleanField(
        _("Trusted Brand"),
        default=False,
        help_text=_("Trusted brand badge")
    )
    
    # ================================================================
    # STATUS FLAGS
    # ================================================================
    
    is_active = models.BooleanField(
        _("Active"),
        default=True,
        db_index=True,
        help_text=_("Brand is active and visible")
    )
    
    is_featured = models.BooleanField(
        _("Featured"),
        default=False,
        db_index=True,
        help_text=_("Brand is featured on homepage")
    )
    
    is_trending = models.BooleanField(
        _("Trending"),
        default=False,
        help_text=_("Brand is currently trending")
    )
    
    is_exclusive = models.BooleanField(
        _("Exclusive"),
        default=False,
        help_text=_("Exclusive brand on platform")
    )
    
    # ================================================================
    # SEO FIELDS
    # ================================================================
    
    meta_title = models.CharField(
        _("Meta Title"),
        max_length=60,
        blank=True,
        null=True,
        help_text=_("SEO meta title (60 chars max)")
    )
    
    meta_description = models.TextField(
        _("Meta Description"),
        max_length=160,
        blank=True,
        null=True,
        help_text=_("SEO meta description (160 chars max)")
    )
    
    meta_keywords = models.CharField(
        _("Meta Keywords"),
        max_length=255,
        blank=True,
        null=True,
        help_text=_("SEO keywords, comma-separated")
    )
    
    # ================================================================
    # ANALYTICS
    # ================================================================
    
    view_count = models.PositiveIntegerField(
        _("View Count"),
        default=0,
        help_text=_("Total brand page views")
    )
    
    product_count = models.PositiveIntegerField(
        _("Product Count"),
        default=0,
        help_text=_("Cached product count")
    )
    
    popularity_score = models.DecimalField(
        _("Popularity Score"),
        max_digits=10,
        decimal_places=2,
        default=0.0,
        help_text=_("Calculated popularity score")
    )
    
    last_viewed_at = models.DateTimeField(
        _("Last Viewed At"),
        blank=True,
        null=True,
        help_text=_("Last time brand page was viewed")
    )
    
    # ================================================================
    # ORDERING & PRIORITY
    # ================================================================
    
    display_order = models.PositiveIntegerField(
        _("Display Order"),
        default=0,
        help_text=_("Order for display (lower number = higher priority)")
    )
    
    priority_level = models.PositiveSmallIntegerField(
        _("Priority Level"),
        default=0,
        help_text=_("Priority level (0-10, higher is more important)")
    )
    
    # ================================================================
    # OWNERSHIP & MANAGEMENT
    # ================================================================
    
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='brander',
        help_text=_("User who created this brand")
    )
    
    managed_by = models.ManyToManyField(
        settings.AUTH_USER_MODEL,
        related_name='managed_brands',
        blank=True,
        help_text=_("Users who can manage this brand")
    )
    
    # ================================================================
    # TIMESTAMPS
    # ================================================================
    
    brand_created_at = models.DateTimeField(
        _("Created At"),
        auto_now_add=True,
        help_text=_("When brand was created")
    )
    
    brand_updated_at = models.DateTimeField(
        _("Updated At"),
        auto_now=True,
        help_text=_("Last time brand was updated")
    )
    
    # ================================================================
    # SOFT DELETE
    # ================================================================
    
    deleted_at = models.DateTimeField(
        _("Deleted At"),
        blank=True,
        null=True,
        db_index=True,
        help_text=_("Soft delete timestamp")
    )
    
    deleted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='deleted_brands',
        help_text=_("User who deleted this brand")
    )
    
    deletion_reason = models.TextField(
        _("Deletion Reason"),
        max_length=500,
        blank=True,
        null=True,
        help_text=_("Reason for deletion")
    )
    
    # ================================================================
    # METADATA
    # ================================================================
    
    metadata = models.JSONField(
        _("Metadata"),
        default=dict,
        blank=True,
        help_text=_("Additional brand metadata in JSON format")
    )
    
    # ================================================================
    # MANAGER
    # ================================================================
    
    objects = BrandManager()
    
    # ================================================================
    # META
    # ================================================================
    
    class Meta:
        verbose_name = _("Brand")
        verbose_name_plural = _("Brands")
        db_table = 'brands'
        ordering = ['display_order', 'brand_name']
        indexes = [
            models.Index(fields=['uuid']),
            models.Index(fields=['brand_slug']),
            models.Index(fields=['brand_name']),
            models.Index(fields=['is_active', 'deleted_at']),
            models.Index(fields=['is_verified']),
            models.Index(fields=['is_featured']),
            models.Index(fields=['verification_status']),
            models.Index(fields=['brand_type']),
            models.Index(fields=['country_of_origin']),
            models.Index(fields=['display_order']),
            models.Index(fields=['popularity_score']),
        ]
        constraints = [
            models.CheckConstraint(
                check=models.Q(priority_level__gte=0) & models.Q(priority_level__lte=10),
                name='priority_level_range'
            )
        ]
    
    # ================================================================
    # STRING REPRESENTATION
    # ================================================================
    
    def __str__(self) -> str:
        """Return brand name"""
        return self.brand_name
    
    def __repr__(self) -> str:
        return f"<Brand: {self.brand_name} ({self.brand_type})>"
    
    # ================================================================
    # PROPERTIES
    # ================================================================
    
    @property
    def logo_url(self) -> str:
        """Get brand logo URL or default"""
        if self.brand_logo:
            return self.brand_logo.url
        return "/static/defaults/default-brand-logo.png"
    
    @property
    def banner_url(self) -> str:
        """Get brand banner URL or default"""
        if self.brand_banner:
            return self.brand_banner.url
        return "/static/defaults/default-brand-banner.png"
    
    @property
    def icon_url(self) -> str:
        """Get brand icon URL or default"""
        if self.brand_icon:
            return self.brand_icon.url
        return "/static/defaults/default-brand-icon.png"
    
    @property
    def brand_url(self) -> str:
        """Get brand page URL"""
        if self.brand_slug:
            return f"/brands/{self.brand_slug}/"
        return f"/brands/{self.uuid}/"
    
    @property
    def is_complete(self) -> bool:
        """Check if brand profile is complete"""
        required = [
            self.brand_name,
            self.brand_description,
            self.brand_logo,
        ]
        return all(required)
    
    @property
    def completion_percentage(self) -> int:
        """Calculate brand profile completion percentage"""
        fields = [
            self.brand_name,
            self.brand_description,
            self.brand_tagline,
            self.brand_logo,
            self.brand_banner,
            self.company_name,
            self.country_of_origin,
            self.brand_website,
            self.brand_email,
        ]
        
        completed = sum(1 for field in fields if field)
        return int((completed / len(fields)) * 100)
    
    @property
    def has_contact_info(self) -> bool:
        """Check if brand has contact information"""
        return bool(self.brand_email or self.brand_phone or self.brand_website)
    
    @property
    def has_social_media(self) -> bool:
        """Check if brand has social media links"""
        return any([
            self.social_facebook,
            self.social_instagram,
            self.social_twitter,
            self.social_linkedin,
            self.social_youtube,
        ])
    
    @property
    def age_in_years(self) -> Optional[int]:
        """Calculate brand age in years"""
        if self.founded_year:
            current_year = timezone.now().year
            return current_year - self.founded_year
        return None
    
    # ================================================================
    # SLUG METHODS
    # ================================================================
    
    def generate_slug(self, save: bool = False) -> str:
        """Generate unique slug from brand name"""
        if not self.brand_name:
            return None
        
        base_slug = slugify(self.brand_name)
        slug = base_slug
        counter = 1
        
        # Ensure uniqueness
        while Brand.objects.filter(
            brand_slug=slug
        ).exclude(pk=self.pk).exists():
            slug = f"{base_slug}-{counter}"
            counter += 1
        
        self.brand_slug = slug
        
        if save:
            self.save(update_fields=['brand_slug'])
        
        return slug
    
    # ================================================================
    # VERIFICATION METHODS
    # ================================================================
    
    def verify(self, verified_by_user=None, save: bool = True) -> None:
        """Mark brand as verified"""
        self.is_verified = True
        self.verification_status = self.VerificationStatus.VERIFIED
        self.verified_at = timezone.now()
        self.verified_by = verified_by_user
        
        if save:
            self.save(update_fields=[
                'is_verified',
                'verification_status',
                'verified_at',
                'verified_by'
            ])
    
    def unverify(self, save: bool = True) -> None:
        """Remove verification"""
        self.is_verified = False
        self.verification_status = self.VerificationStatus.UNVERIFIED
        self.verified_at = None
        self.verified_by = None
        
        if save:
            self.save(update_fields=[
                'is_verified',
                'verification_status',
                'verified_at',
                'verified_by'
            ])
    
    def request_verification(self, save: bool = True) -> None:
        """Request verification"""
        self.verification_status = self.VerificationStatus.PENDING
        
        if save:
            self.save(update_fields=['verification_status'])
    
    def reject_verification(self, save: bool = True) -> None:
        """Reject verification request"""
        self.verification_status = self.VerificationStatus.REJECTED
        self.is_verified = False
        
        if save:
            self.save(update_fields=['verification_status', 'is_verified'])
    
    # ================================================================
    # STATUS METHODS
    # ================================================================
    
    def activate(self, save: bool = True) -> None:
        """Activate brand"""
        self.is_active = True
        if save:
            self.save(update_fields=['is_active'])
    
    def deactivate(self, save: bool = True) -> None:
        """Deactivate brand"""
        self.is_active = False
        if save:
            self.save(update_fields=['is_active'])
    
    def feature(self, save: bool = True) -> None:
        """Mark brand as featured"""
        self.is_featured = True
        if save:
            self.save(update_fields=['is_featured'])
    
    def unfeature(self, save: bool = True) -> None:
        """Remove featured status"""
        self.is_featured = False
        if save:
            self.save(update_fields=['is_featured'])
    
    def mark_trending(self, save: bool = True) -> None:
        """Mark brand as trending"""
        self.is_trending = True
        if save:
            self.save(update_fields=['is_trending'])
    
    def unmark_trending(self, save: bool = True) -> None:
        """Remove trending status"""
        self.is_trending = False
        if save:
            self.save(update_fields=['is_trending'])
    
    # ================================================================
    # SOFT DELETE METHODS
    # ================================================================
    
    def soft_delete(
        self,
        deleted_by_user=None,
        reason: str = None,
        save: bool = True
    ) -> None:
        """Soft delete brand"""
        self.deleted_at = timezone.now()
        self.deleted_by = deleted_by_user
        self.deletion_reason = reason
        self.is_active = False
        
        if save:
            self.save()
    
    def restore(self, save: bool = True) -> None:
        """Restore soft-deleted brand"""
        self.deleted_at = None
        self.deleted_by = None
        self.deletion_reason = None
        self.is_active = True
        
        if save:
            self.save()
    
    def delete(self, *args, **kwargs):
        """Override delete to use soft delete"""
        if kwargs.pop('hard_delete', False):
            super().delete(*args, **kwargs)
        else:
            self.soft_delete()
    
    # ================================================================
    # ANALYTICS METHODS
    # ================================================================
    
    def increment_view_count(self, save: bool = True) -> None:
        """Increment brand page view count"""
        self.view_count += 1
        self.last_viewed_at = timezone.now()
        
        if save:
            self.save(update_fields=['view_count', 'last_viewed_at'])
    
    def update_product_count(self, save: bool = True) -> int:
        """Update cached product count"""
        count = self.products.filter(is_active=True).count()
        self.product_count = count
        
        if save:
            self.save(update_fields=['product_count'])
        
        return count
    
    def calculate_popularity_score(self, save: bool = True) -> float:
        """
        Calculate popularity score based on various metrics
        Formula: (products * 10) + (views * 0.1) + (verified * 100)
        """
        score = 0.0
        
        # Product count weight
        score += self.product_count * 10
        
        # View count weight
        score += self.view_count * 0.1
        
        # Verification bonus
        if self.is_verified:
            score += 100
        
        # Featured bonus
        if self.is_featured:
            score += 50
        
        # Trusted bonus
        if self.is_trusted:
            score += 50
        
        self.popularity_score = round(score, 2)
        
        if save:
            self.save(update_fields=['popularity_score'])
        
        return self.popularity_score
    
    # ================================================================
    # MANAGEMENT METHODS
    # ================================================================
    
    def add_manager(self, user) -> bool:
        """Add a user as brand manager"""
        if user not in self.managed_by.all():
            self.managed_by.add(user)
            return True
        return False
    
    def remove_manager(self, user) -> bool:
        """Remove a user from brand managers"""
        if user in self.managed_by.all():
            self.managed_by.remove(user)
            return True
        return False
    
    def is_manager(self, user) -> bool:
        """Check if user is a brand manager"""
        return user in self.managed_by.all() or user == self.created_by
    
    # ================================================================
    # DATA EXPORT (GDPR)
    # ================================================================
    
    def export_data(self) -> dict:
        """Export brand data"""
        data = {
            'uuid': str(self.uuid),
            'name': self.brand_name,
            'slug': self.brand_slug,
            'type': self.brand_type,
            'description': self.brand_description,
            'tagline': self.brand_tagline,
            'company': {
                'name': self.company_name,
                'founded_year': self.founded_year,
                'country': self.country_of_origin,
                'headquarters': self.headquarters,
            },
            'contact': {
                'website': self.brand_website,
                'email': self.brand_email,
                'phone': self.brand_phone,
                'support_email': self.support_email,
                'support_phone': self.support_phone,
            },
            'social_media': {
                'facebook': self.social_facebook,
                'instagram': self.social_instagram,
                'twitter': self.social_twitter,
                'linkedin': self.social_linkedin,
                'youtube': self.social_youtube,
            },
            'verification': {
                'is_verified': self.is_verified,
                'status': self.verification_status,
                'verified_at': self.verified_at.isoformat() if self.verified_at else None,
            },
            'stats': {
                'view_count': self.view_count,
                'product_count': self.product_count,
                'popularity_score': float(self.popularity_score),
            },
            'status': {
                'is_active': self.is_active,
                'is_featured': self.is_featured,
                'is_trending': self.is_trending,
                'is_official': self.is_official,
                'is_trusted': self.is_trusted,
            },
            'created_at': self.brand_created_at.isoformat(),
            'updated_at': self.brand_updated_at.isoformat(),
            'metadata': self.metadata,
        }
        
        return data
    
    # ================================================================
    # VALIDATION
    # ================================================================
    
    def clean(self) -> None:
        """Validate model fields"""
        super().clean()
        
        # Validate founded year
        if self.founded_year:
            current_year = timezone.now().year
            if self.founded_year > current_year:
                raise ValidationError(
                    _("Founded year cannot be in the future")
                )
            if self.founded_year < 1800:
                raise ValidationError(
                    _("Founded year seems too old")
                )
        
        # Validate URLs
        url_validator = URLValidator()
        url_fields = [
            ('brand_website', self.brand_website),
            ('social_facebook', self.social_facebook),
            ('social_instagram', self.social_instagram),
            ('social_twitter', self.social_twitter),
            ('social_linkedin', self.social_linkedin),
            ('social_youtube', self.social_youtube),
        ]
        
        for field_name, url in url_fields:
            if url:
                try:
                    url_validator(url)
                except ValidationError:
                    raise ValidationError({
                        field_name: _("Enter a valid URL")
                    })
        
        # Validate priority level
        if self.priority_level < 0 or self.priority_level > 10:
            raise ValidationError(
                _("Priority level must be between 0 and 10")
            )
    
    def save(self, *args, **kwargs):
        """Override save to generate slug and run validation"""
        # Generate slug if name exists but slug doesn't
        if self.brand_name and not self.brand_slug:
            self.generate_slug()
        
        # Run validation
        self.full_clean()
        
        super().save(*args, **kwargs)