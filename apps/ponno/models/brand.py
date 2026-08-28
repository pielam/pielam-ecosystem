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
- Category association
- Business registration / compliance
- Customer ratings aggregate
- Brand theming
"""

import uuid
from typing import Optional

from django.db import models
from django.conf import settings
from django.urls import reverse
from django.utils import timezone
from django.utils.text import slugify
from django.utils.translation import gettext_lazy as _
from django.core.exceptions import ValidationError
from django.core.validators import URLValidator, RegexValidator
from django.db.models import Avg, Count, Q


# ====================================================================
# COUNTRY CHOICES (full country names)
# ====================================================================

COUNTRY_CHOICES = [
    ("Afghanistan", _("Afghanistan")),
    ("Albania", _("Albania")),
    ("Algeria", _("Algeria")),
    ("Andorra", _("Andorra")),
    ("Angola", _("Angola")),
    ("Antigua and Barbuda", _("Antigua and Barbuda")),
    ("Argentina", _("Argentina")),
    ("Armenia", _("Armenia")),
    ("Australia", _("Australia")),
    ("Austria", _("Austria")),
    ("Azerbaijan", _("Azerbaijan")),
    ("Bahamas", _("Bahamas")),
    ("Bahrain", _("Bahrain")),
    ("Bangladesh", _("Bangladesh")),
    ("Barbados", _("Barbados")),
    ("Belarus", _("Belarus")),
    ("Belgium", _("Belgium")),
    ("Belize", _("Belize")),
    ("Benin", _("Benin")),
    ("Bhutan", _("Bhutan")),
    ("Bolivia", _("Bolivia")),
    ("Bosnia and Herzegovina", _("Bosnia and Herzegovina")),
    ("Botswana", _("Botswana")),
    ("Brazil", _("Brazil")),
    ("Brunei", _("Brunei")),
    ("Bulgaria", _("Bulgaria")),
    ("Burkina Faso", _("Burkina Faso")),
    ("Burundi", _("Burundi")),
    ("Cabo Verde", _("Cabo Verde")),
    ("Cambodia", _("Cambodia")),
    ("Cameroon", _("Cameroon")),
    ("Canada", _("Canada")),
    ("Central African Republic", _("Central African Republic")),
    ("Chad", _("Chad")),
    ("Chile", _("Chile")),
    ("China", _("China")),
    ("Colombia", _("Colombia")),
    ("Comoros", _("Comoros")),
    ("Congo", _("Congo")),
    ("Costa Rica", _("Costa Rica")),
    ("Croatia", _("Croatia")),
    ("Cuba", _("Cuba")),
    ("Cyprus", _("Cyprus")),
    ("Czechia", _("Czechia")),
    ("Denmark", _("Denmark")),
    ("Djibouti", _("Djibouti")),
    ("Dominica", _("Dominica")),
    ("Dominican Republic", _("Dominican Republic")),
    ("DR Congo", _("DR Congo")),
    ("Ecuador", _("Ecuador")),
    ("Egypt", _("Egypt")),
    ("El Salvador", _("El Salvador")),
    ("Equatorial Guinea", _("Equatorial Guinea")),
    ("Eritrea", _("Eritrea")),
    ("Estonia", _("Estonia")),
    ("Eswatini", _("Eswatini")),
    ("Ethiopia", _("Ethiopia")),
    ("Fiji", _("Fiji")),
    ("Finland", _("Finland")),
    ("France", _("France")),
    ("Gabon", _("Gabon")),
    ("Gambia", _("Gambia")),
    ("Georgia", _("Georgia")),
    ("Germany", _("Germany")),
    ("Ghana", _("Ghana")),
    ("Greece", _("Greece")),
    ("Grenada", _("Grenada")),
    ("Guatemala", _("Guatemala")),
    ("Guinea", _("Guinea")),
    ("Guinea-Bissau", _("Guinea-Bissau")),
    ("Guyana", _("Guyana")),
    ("Haiti", _("Haiti")),
    ("Honduras", _("Honduras")),
    ("Hungary", _("Hungary")),
    ("Iceland", _("Iceland")),
    ("India", _("India")),
    ("Indonesia", _("Indonesia")),
    ("Iran", _("Iran")),
    ("Iraq", _("Iraq")),
    ("Ireland", _("Ireland")),
    ("Israel", _("Israel")),
    ("Italy", _("Italy")),
    ("Jamaica", _("Jamaica")),
    ("Japan", _("Japan")),
    ("Jordan", _("Jordan")),
    ("Kazakhstan", _("Kazakhstan")),
    ("Kenya", _("Kenya")),
    ("Kiribati", _("Kiribati")),
    ("Kuwait", _("Kuwait")),
    ("Kyrgyzstan", _("Kyrgyzstan")),
    ("Laos", _("Laos")),
    ("Latvia", _("Latvia")),
    ("Lebanon", _("Lebanon")),
    ("Lesotho", _("Lesotho")),
    ("Liberia", _("Liberia")),
    ("Libya", _("Libya")),
    ("Liechtenstein", _("Liechtenstein")),
    ("Lithuania", _("Lithuania")),
    ("Luxembourg", _("Luxembourg")),
    ("Madagascar", _("Madagascar")),
    ("Malawi", _("Malawi")),
    ("Malaysia", _("Malaysia")),
    ("Maldives", _("Maldives")),
    ("Mali", _("Mali")),
    ("Malta", _("Malta")),
    ("Marshall Islands", _("Marshall Islands")),
    ("Mauritania", _("Mauritania")),
    ("Mauritius", _("Mauritius")),
    ("Mexico", _("Mexico")),
    ("Micronesia", _("Micronesia")),
    ("Moldova", _("Moldova")),
    ("Monaco", _("Monaco")),
    ("Mongolia", _("Mongolia")),
    ("Montenegro", _("Montenegro")),
    ("Morocco", _("Morocco")),
    ("Mozambique", _("Mozambique")),
    ("Myanmar", _("Myanmar")),
    ("Namibia", _("Namibia")),
    ("Nauru", _("Nauru")),
    ("Nepal", _("Nepal")),
    ("Netherlands", _("Netherlands")),
    ("New Zealand", _("New Zealand")),
    ("Nicaragua", _("Nicaragua")),
    ("Niger", _("Niger")),
    ("Nigeria", _("Nigeria")),
    ("North Korea", _("North Korea")),
    ("North Macedonia", _("North Macedonia")),
    ("Norway", _("Norway")),
    ("Oman", _("Oman")),
    ("Pakistan", _("Pakistan")),
    ("Palau", _("Palau")),
    ("Palestine", _("Palestine")),
    ("Panama", _("Panama")),
    ("Papua New Guinea", _("Papua New Guinea")),
    ("Paraguay", _("Paraguay")),
    ("Peru", _("Peru")),
    ("Philippines", _("Philippines")),
    ("Poland", _("Poland")),
    ("Portugal", _("Portugal")),
    ("Qatar", _("Qatar")),
    ("Romania", _("Romania")),
    ("Russia", _("Russia")),
    ("Rwanda", _("Rwanda")),
    ("Saint Kitts and Nevis", _("Saint Kitts and Nevis")),
    ("Saint Lucia", _("Saint Lucia")),
    ("Saint Vincent and the Grenadines", _("Saint Vincent and the Grenadines")),
    ("Samoa", _("Samoa")),
    ("San Marino", _("San Marino")),
    ("Sao Tome and Principe", _("Sao Tome and Principe")),
    ("Saudi Arabia", _("Saudi Arabia")),
    ("Senegal", _("Senegal")),
    ("Serbia", _("Serbia")),
    ("Seychelles", _("Seychelles")),
    ("Sierra Leone", _("Sierra Leone")),
    ("Singapore", _("Singapore")),
    ("Slovakia", _("Slovakia")),
    ("Slovenia", _("Slovenia")),
    ("Solomon Islands", _("Solomon Islands")),
    ("Somalia", _("Somalia")),
    ("South Africa", _("South Africa")),
    ("South Korea", _("South Korea")),
    ("South Sudan", _("South Sudan")),
    ("Spain", _("Spain")),
    ("Sri Lanka", _("Sri Lanka")),
    ("Sudan", _("Sudan")),
    ("Suriname", _("Suriname")),
    ("Sweden", _("Sweden")),
    ("Switzerland", _("Switzerland")),
    ("Syria", _("Syria")),
    ("Taiwan", _("Taiwan")),
    ("Tajikistan", _("Tajikistan")),
    ("Tanzania", _("Tanzania")),
    ("Thailand", _("Thailand")),
    ("Timor-Leste", _("Timor-Leste")),
    ("Togo", _("Togo")),
    ("Tonga", _("Tonga")),
    ("Trinidad and Tobago", _("Trinidad and Tobago")),
    ("Tunisia", _("Tunisia")),
    ("Turkey", _("Turkey")),
    ("Turkmenistan", _("Turkmenistan")),
    ("Tuvalu", _("Tuvalu")),
    ("Uganda", _("Uganda")),
    ("Ukraine", _("Ukraine")),
    ("United Arab Emirates", _("United Arab Emirates")),
    ("United Kingdom", _("United Kingdom")),
    ("United States", _("United States")),
    ("Uruguay", _("Uruguay")),
    ("Uzbekistan", _("Uzbekistan")),
    ("Vanuatu", _("Vanuatu")),
    ("Vatican City", _("Vatican City")),
    ("Venezuela", _("Venezuela")),
    ("Vietnam", _("Vietnam")),
    ("Yemen", _("Yemen")),
    ("Zambia", _("Zambia")),
    ("Zimbabwe", _("Zimbabwe")),
]


# ====================================================================
# VALIDATORS
# ====================================================================

hex_color_validator = RegexValidator(
    regex=r'^#(?:[0-9a-fA-F]{3}){1,2}$',
    message=_("Enter a valid hex color code, e.g. #FF5733")
)

phone_validator = RegexValidator(
    regex=r'^\+?[1-9]\d{6,14}$',
    message=_("Enter a valid phone number, e.g. +8801XXXXXXXXX")
)


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
            product_count_agg=Count('products')
        ).filter(
            product_count_agg__gt=0
        ).order_by('-product_count_agg')[:limit]

    def top_rated_brands(self, limit=10, min_reviews=1):
        """Get top rated brands with a minimum number of reviews"""
        return self.filter(
            is_active=True,
            deleted_at__isnull=True,
            review_count__gte=min_reviews
        ).order_by('-average_rating')[:limit]

    def by_category(self, category):
        """Get brands that own the given category"""
        return self.filter(
            categories=category,  # reverse FK from Category.brand
            is_active=True,
            deleted_at__isnull=True
        )

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
    - Category association
    - Business registration / compliance
    - Customer ratings aggregate
    - Brand theming
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
    # BRAND RELATIONSHIPS
    # ================================================================
    #
    # NOTE: Categories are related via Category.brand (a ForeignKey
    # defined on the Category model with related_name='categories').
    # That means brand_instance.categories.all() already works without
    # any field needed here — do not add a duplicate M2M/FK for this,
    # it will clash with that reverse accessor.

    parent_brand = models.ForeignKey(
        'self',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='sub_brands',
        help_text=_("Parent brand, if this is a sub-brand")
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

    brand_intro_video_url = models.URLField(
        _("Intro Video URL"),
        max_length=300,
        blank=True,
        null=True,
        help_text=_("Link to brand introduction video (YouTube/Vimeo, etc.)")
    )

    # ================================================================
    # BRAND THEMING
    # ================================================================

    brand_primary_color = models.CharField(
        _("Primary Color"),
        max_length=7,
        blank=True,
        null=True,
        validators=[hex_color_validator],
        help_text=_("Hex color code, e.g. #FF5733")
    )

    brand_secondary_color = models.CharField(
        _("Secondary Color"),
        max_length=7,
        blank=True,
        null=True,
        validators=[hex_color_validator],
        help_text=_("Hex color code, e.g. #1A1A1A")
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
        max_length=100,
        choices=COUNTRY_CHOICES,
        blank=True,
        null=True,
        db_index=True,
        help_text=_("Country of origin")
    )

    headquarters = models.CharField(
        _("Headquarters"),
        max_length=200,
        blank=True,
        null=True,
        help_text=_("Company headquarters location")
    )

    # ================================================================
    # BUSINESS REGISTRATION / COMPLIANCE
    # ================================================================

    business_registration_number = models.CharField(
        _("Business Registration Number"),
        max_length=100,
        blank=True,
        null=True,
        help_text=_("Official business/company registration number")
    )

    tax_identification_number = models.CharField(
        _("Tax Identification Number"),
        max_length=100,
        blank=True,
        null=True,
        help_text=_("TIN / BIN or equivalent tax ID")
    )

    trade_license_number = models.CharField(
        _("Trade License Number"),
        max_length=100,
        blank=True,
        null=True,
        help_text=_("Trade license number, if applicable")
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
        validators=[phone_validator],
        help_text=_("Brand contact phone")
    )

    whatsapp_number = models.CharField(
        _("WhatsApp Number"),
        max_length=20,
        blank=True,
        null=True,
        validators=[phone_validator],
        help_text=_("WhatsApp contact number, including country code")
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
        validators=[phone_validator],
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
        help_text=_("Cached count of active products under this brand")
    )

    category_count = models.PositiveIntegerField(
        _("Category Count"),
        default=0,
        help_text=_("Cached count of active categories owned by this brand")
    )

    subcategory_count = models.PositiveIntegerField(
        _("SubCategory Count"),
        default=0,
        help_text=_("Cached count of active subcategories owned by this brand")
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
    # CUSTOMER RATINGS (aggregate)
    # ================================================================

    average_rating = models.DecimalField(
        _("Average Rating"),
        max_digits=3,
        decimal_places=2,
        default=0.0,
        help_text=_("Cached average customer rating (0.00 - 5.00)")
    )

    review_count = models.PositiveIntegerField(
        _("Review Count"),
        default=0,
        help_text=_("Cached total number of customer reviews")
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
            models.Index(fields=['average_rating']),
            models.Index(fields=['category_count']),
            models.Index(fields=['subcategory_count']),
        ]
        constraints = [
            models.CheckConstraint(
                check=models.Q(priority_level__gte=0) & models.Q(priority_level__lte=10),
                name='priority_level_range'
            ),
            models.CheckConstraint(
                check=models.Q(average_rating__gte=0) & models.Q(average_rating__lte=5),
                name='average_rating_range'
            ),
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
            return reverse("ponno:brand_products", kwargs={"brand_slug": self.brand_slug})
        return reverse("ponno:brand_products", kwargs={"brand_slug": str(self.uuid)})

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
    def has_business_documents(self) -> bool:
        """Check if brand has provided business/compliance documents"""
        return any([
            self.business_registration_number,
            self.tax_identification_number,
            self.trade_license_number,
        ])

    @property
    def age_in_years(self) -> Optional[int]:
        """Calculate brand age in years"""
        if self.founded_year:
            current_year = timezone.now().year
            return current_year - self.founded_year
        return None

    @property
    def is_sub_brand(self) -> bool:
        """Check whether this brand belongs to a parent brand"""
        return self.parent_brand_id is not None

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
        """Update cached count of active products under this brand"""
        count = self.products.filter(
            is_active=True,
            deleted_at__isnull=True
        ).count()
        self.product_count = count

        if save:
            self.save(update_fields=['product_count'])

        return count

    def update_category_count(self, save: bool = True) -> int:
        """
        Update cached count of active categories owned by this brand.
        Relies on Category.brand (FK, related_name='categories').
        """
        count = self.categories.filter(
            is_active=True,
            deleted_at__isnull=True
        ).count()
        self.category_count = count

        if save:
            self.save(update_fields=['category_count'])

        return count

    def update_subcategory_count(self, save: bool = True) -> int:
        """
        Update cached count of active subcategories owned by this brand.
        Relies on SubCategory.brand (FK, related_name='subcategories').
        """
        count = self.subcategories.filter(
            is_active=True,
            deleted_at__isnull=True
        ).count()
        self.subcategory_count = count

        if save:
            self.save(update_fields=['subcategory_count'])

        return count

    def refresh_rating_stats(self, save: bool = True) -> dict:
        """
        Recompute average_rating and review_count by aggregating
        ProductRating rows across all of this brand's active products.
        Use this instead of update_rating_stats() when you want the
        brand to compute its own numbers rather than being handed
        precomputed values (e.g. from a signal).
        """
        # Local import avoids a module-level dependency between
        # brand.py and rating.py at Django app-loading time.
        from apps.ponno.models.rating import ProductRating

        agg = ProductRating.objects.filter(
            product__brand=self,
            product__is_active=True,
            product__deleted_at__isnull=True
        ).aggregate(
            avg_rating=Avg('rating'),
            total_ratings=Count('id')
        )

        self.average_rating = round(agg['avg_rating'] or 0.0, 2)
        self.review_count = agg['total_ratings'] or 0

        if save:
            self.save(update_fields=['average_rating', 'review_count'])

        return {
            'average_rating': float(self.average_rating),
            'review_count': self.review_count,
        }

    def refresh_all_stats(self, save: bool = True) -> dict:
        """
        Recalculate every cached count/aggregate on this brand in one
        call: products, categories, subcategories, ratings, and the
        resulting popularity score. Handy for a scheduled task, an
        admin action, or right after a bulk import.
        """
        product_count = self.update_product_count(save=False)
        category_count = self.update_category_count(save=False)
        subcategory_count = self.update_subcategory_count(save=False)
        rating_stats = self.refresh_rating_stats(save=False)

        if save:
            self.save(update_fields=[
                'product_count',
                'category_count',
                'subcategory_count',
                'average_rating',
                'review_count',
            ])

        popularity_score = self.calculate_popularity_score(save=save)

        return {
            'product_count': product_count,
            'category_count': category_count,
            'subcategory_count': subcategory_count,
            'average_rating': rating_stats['average_rating'],
            'review_count': rating_stats['review_count'],
            'popularity_score': float(popularity_score),
        }

    def get_stats(self) -> dict:
        """
        Return the currently cached stats without hitting the database
        again — use this for display (serializers, templates). Call
        refresh_all_stats() first if you need fresh numbers.
        """
        return {
            'product_count': self.product_count,
            'category_count': self.category_count,
            'subcategory_count': self.subcategory_count,
            'average_rating': float(self.average_rating),
            'review_count': self.review_count,
            'popularity_score': float(self.popularity_score),
        }

    def update_rating_stats(self, average: float, count: int, save: bool = True) -> None:
        """
        Update cached rating aggregate.
        Intended to be called by a signal/task whenever a related
        review model is created, updated, or deleted.
        """
        self.average_rating = round(average, 2)
        self.review_count = count

        if save:
            self.save(update_fields=['average_rating', 'review_count'])

    def calculate_popularity_score(self, save: bool = True) -> float:
        """
        Calculate popularity score based on various metrics
        Formula: (products * 10) + (views * 0.1) + (verified * 100)
                 + (rating * review_count weight) + featured/trusted bonuses
        """
        score = 0.0

        # Product count weight
        score += self.product_count * 10

        # View count weight
        score += self.view_count * 0.1

        # Rating weight (rewards both high rating and review volume)
        if self.review_count > 0:
            score += float(self.average_rating) * min(self.review_count, 100) * 0.5

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
            'categories': list(self.categories.values_list('category_name', flat=True)),
            'company': {
                'name': self.company_name,
                'founded_year': self.founded_year,
                'country': self.country_of_origin,
                'headquarters': self.headquarters,
            },
            'compliance': {
                'business_registration_number': self.business_registration_number,
                'tax_identification_number': self.tax_identification_number,
                'trade_license_number': self.trade_license_number,
            },
            'contact': {
                'website': self.brand_website,
                'email': self.brand_email,
                'phone': self.brand_phone,
                'whatsapp': self.whatsapp_number,
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
                'category_count': self.category_count,
                'subcategory_count': self.subcategory_count,
                'popularity_score': float(self.popularity_score),
                'average_rating': float(self.average_rating),
                'review_count': self.review_count,
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
            ('brand_intro_video_url', self.brand_intro_video_url),
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

        # Validate average rating range
        if self.average_rating < 0 or self.average_rating > 5:
            raise ValidationError(
                _("Average rating must be between 0 and 5")
            )

        # Prevent a brand from being its own parent
        if self.parent_brand_id and self.pk and self.parent_brand_id == self.pk:
            raise ValidationError(
                _("A brand cannot be its own parent brand")
            )

    def save(self, *args, **kwargs):
        """Override save to generate slug and run validation"""
        # Generate slug if name exists but slug doesn't
        if self.brand_name and not self.brand_slug:
            self.generate_slug()

        # Run validation (skip full_clean on partial/update_fields saves
        # to avoid re-validating unrelated, possibly stale fields)
        if not kwargs.get('update_fields'):
            self.full_clean()

        super().save(*args, **kwargs)