# apps/ponno/models/sub_category.py

"""
SubCategory Model for International Business Standards
------------------------------------------------------
Features:
- Belongs to a parent Category
- Optional nested sub-subcategory support (one level deep)
- Category icons and images
- SEO optimization
- Analytics tracking
- Featured subcategories
- Display ordering
- Soft delete
- Visibility control
- Commission & pricing overrides
"""

import uuid
from typing import Optional, List
from apps.ponno.models.brand import Brand
from django.db import models
from django.conf import settings
from django.utils import timezone
from django.utils.text import slugify
from django.utils.translation import gettext_lazy as _
from django.core.exceptions import ValidationError
from django.db.models import Count, Q

from apps.ponno.models.category import Category


# ====================================================================
# SUBCATEGORY MANAGER
# ====================================================================

class SubCategoryManager(models.Manager):
    """
    Custom manager for SubCategory with optimized queries
    """

    def active(self):
        """Get all active, non-deleted subcategories"""
        return self.filter(
            is_active=True,
            deleted_at__isnull=True
        )

    def for_category(self, category):
        """Get active subcategories belonging to a specific category"""
        return self.active().filter(category=category)

    def featured(self):
        """Get featured subcategories"""
        return self.filter(
            is_featured=True,
            is_active=True,
            deleted_at__isnull=True
        )

    def trending(self):
        """Get trending subcategories"""
        return self.filter(
            is_trending=True,
            is_active=True,
            deleted_at__isnull=True
        )

    def popular(self, limit=10):
        """Get popular subcategories by product count"""
        return self.active().annotate(
            product_count_annotated=Count('products')
        ).filter(
            product_count_annotated__gt=0
        ).order_by('-product_count_annotated')[:limit]

    def search(self, query):
        """Search subcategories by name or description"""
        return self.filter(
            Q(sub_category_name__icontains=query) |
            Q(sub_category_description__icontains=query) |
            Q(sub_category_slug__icontains=query),
            is_active=True,
            deleted_at__isnull=True
        ).select_related('category')

    def get_by_slug(self, slug):
        """Get subcategory by slug"""
        return self.select_related('category').get(
            sub_category_slug=slug,
            is_active=True,
            deleted_at__isnull=True
        )

    def visible_in_menu(self):
        """Get subcategories visible in the navigation menu"""
        return self.active().filter(is_visible_in_menu=True)

    def visible_on_homepage(self):
        """Get subcategories shown on the homepage"""
        return self.active().filter(is_visible_on_homepage=True)

    def recent(self, days=30):
        """Get subcategories created in the last N days"""
        threshold = timezone.now() - timezone.timedelta(days=days)
        return self.active().filter(sub_category_created_at__gte=threshold)


# ====================================================================
# SUBCATEGORY MODEL
# ====================================================================

class SubCategory(models.Model):
    """
    SubCategory Model

    Sits one level below Category. Each SubCategory belongs to exactly
    one parent Category. Products can be assigned at this level for
    finer-grained classification.

    Features:
    - Parent category relationship
    - Images, icons, and thumbnails
    - SEO fields
    - Display ordering and style
    - Featured / trending / visibility flags
    - Analytics (views, product count, popularity score)
    - Commission & price range overrides (marketplace)
    - Soft delete with cascade option
    - Full GDPR data export
    """

    # ================================================================
    # CHOICES
    # ================================================================

    class SubCategoryType(models.TextChoices):
        PRODUCT  = 'product',  _('Product SubCategory')
        SERVICE  = 'service',  _('Service SubCategory')
        DIGITAL  = 'digital',  _('Digital Goods')
        PHYSICAL = 'physical', _('Physical Goods')
        MIXED    = 'mixed',    _('Mixed')

    class DisplayStyle(models.TextChoices):
        GRID     = 'grid',     _('Grid View')
        LIST     = 'list',     _('List View')
        CAROUSEL = 'carousel', _('Carousel View')
        FEATURED = 'featured', _('Featured View')

    # ================================================================
    # PRIMARY FIELDS
    # ================================================================

    uuid = models.UUIDField(
        _("UUID"),
        default=uuid.uuid4,
        editable=False,
        unique=True,
        db_index=True,
        help_text=_("Unique identifier for external API references")
    )

    sub_category_name = models.CharField(
        _("SubCategory Name"),
        max_length=150,
        db_index=True,
        help_text=_("SubCategory name")
    )

    sub_category_slug = models.SlugField(
        _("SubCategory Slug"),
        max_length=160,
        unique=True,
        blank=True,
        null=True,
        db_index=True,
        help_text=_("URL-friendly subcategory identifier")
    )

    sub_category_type = models.CharField(
        _("SubCategory Type"),
        max_length=20,
        choices=SubCategoryType.choices,
        default=SubCategoryType.PRODUCT,
        help_text=_("Type of subcategory")
    )

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='subcategory_creator',
        help_text=_("User who created this subcategory")
    )

    managed_by = models.ManyToManyField(
        settings.AUTH_USER_MODEL,
        related_name='managed_subcategories',
        blank=True,

        help_text=_("Users who can manage this SubCategory")
    )

    # ================================================================
    # PARENT RELATIONSHIP
    # ================================================================
    brand = models.ForeignKey(
            Brand,
            on_delete=models.SET_NULL,
            null=True,
            blank=True,
            related_name='subcategories',
            help_text=_("Brand Category associated with this subcategory")
        )
    category = models.ForeignKey(
        Category,
        on_delete=models.CASCADE,
        related_name='sub_categories',
        help_text=_("Parent category this subcategory belongs to")
    )

    # ================================================================
    # SUBCATEGORY INFORMATION
    # ================================================================

    sub_category_description = models.TextField(
        _("SubCategory Description"),
        max_length=1000,
        blank=True,
        null=True,
        help_text=_("Detailed subcategory description")
    )

    sub_category_short_description = models.CharField(
        _("Short Description"),
        max_length=200,
        blank=True,
        null=True,
        help_text=_("Brief subcategory description")
    )

    # ================================================================
    # MEDIA
    # ================================================================

    sub_category_image = models.ImageField(
        _("SubCategory Image"),
        upload_to="sub_categories/images/%Y/%m/",
        blank=True,
        null=True,
        help_text=_("SubCategory banner/cover image")
    )

    sub_category_icon = models.ImageField(
        _("SubCategory Icon"),
        upload_to="sub_categories/icons/%Y/%m/",
        blank=True,
        null=True,
        help_text=_("SubCategory icon image")
    )

    sub_category_thumbnail = models.ImageField(
        _("SubCategory Thumbnail"),
        upload_to="sub_categories/thumbnails/%Y/%m/",
        blank=True,
        null=True,
        help_text=_("SubCategory thumbnail image")
    )

    icon_class = models.CharField(
        _("Icon Class"),
        max_length=50,
        blank=True,
        null=True,
        help_text=_("CSS icon class (e.g. 'fa fa-laptop')")
    )

    color_code = models.CharField(
        _("Color Code"),
        max_length=7,
        blank=True,
        null=True,
        help_text=_("Hex color code for subcategory theme (e.g. '#FF5733')")
    )

    # ================================================================
    # STATUS FLAGS
    # ================================================================

    is_active = models.BooleanField(
        _("Active"),
        default=True,
        db_index=True,
        help_text=_("SubCategory is active and visible")
    )

    is_featured = models.BooleanField(
        _("Featured"),
        default=False,
        db_index=True,
        help_text=_("SubCategory is featured on its parent category page")
    )

    is_trending = models.BooleanField(
        _("Trending"),
        default=False,
        help_text=_("SubCategory is currently trending")
    )

    is_visible_in_menu = models.BooleanField(
        _("Visible in Menu"),
        default=True,
        help_text=_("Show subcategory in navigation menu")
    )

    is_visible_on_homepage = models.BooleanField(
        _("Visible on Homepage"),
        default=False,
        help_text=_("Show subcategory on homepage")
    )

    # ================================================================
    # DISPLAY SETTINGS
    # ================================================================

    display_style = models.CharField(
        _("Display Style"),
        max_length=20,
        choices=DisplayStyle.choices,
        default=DisplayStyle.GRID,
        help_text=_("How to display products in this subcategory")
    )

    display_order = models.PositiveIntegerField(
        _("Display Order"),
        default=0,
        help_text=_("Order for display (lower number = higher priority)")
    )

    products_per_page = models.PositiveIntegerField(
        _("Products Per Page"),
        default=24,
        help_text=_("Number of products to show per page")
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

    canonical_url = models.URLField(
        _("Canonical URL"),
        max_length=200,
        blank=True,
        null=True,
        help_text=_("Canonical URL for SEO")
    )

    # ================================================================
    # ANALYTICS
    # ================================================================

    view_count = models.PositiveIntegerField(
        _("View Count"),
        default=0,
        help_text=_("Total subcategory page views")
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
        help_text=_("Last time subcategory page was viewed")
    )

    # ================================================================
    # COMMISSION & PRICING (marketplace overrides)
    # ================================================================

    commission_rate = models.DecimalField(
        _("Commission Rate"),
        max_digits=5,
        decimal_places=2,
        default=0.0,
        help_text=_(
            "Platform commission percentage (0-100). "
            "Overrides parent category rate when set above 0."
        )
    )

    min_price = models.DecimalField(
        _("Minimum Price"),
        max_digits=10,
        decimal_places=2,
        null=True,
        blank=True,
        help_text=_("Minimum allowed price for products in this subcategory")
    )

    max_price = models.DecimalField(
        _("Maximum Price"),
        max_digits=10,
        decimal_places=2,
        null=True,
        blank=True,
        help_text=_("Maximum allowed price for products in this subcategory")
    )

    # ================================================================
    # TIMESTAMPS
    # ================================================================

    sub_category_created_at = models.DateTimeField(
        _("Created At"),
        auto_now_add=True,
        help_text=_("When subcategory was created")
    )

    sub_category_updated_at = models.DateTimeField(
        _("Updated At"),
        auto_now=True,
        help_text=_("Last time subcategory was updated")
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
        related_name='deleted_sub_categories',
        help_text=_("User who deleted this subcategory")
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
        help_text=_("Additional subcategory metadata in JSON format")
    )

    # ================================================================
    # MANAGER
    # ================================================================

    objects = SubCategoryManager()

    # ================================================================
    # META
    # ================================================================

    class Meta:
        verbose_name = _("SubCategory")
        verbose_name_plural = _("SubCategories")
        db_table = 'sub_categories'
        ordering = ['display_order', 'sub_category_name']

        # Enforce unique name per parent category
        unique_together = [('category', 'sub_category_name')]

        indexes = [
            models.Index(fields=['uuid']),
            models.Index(fields=['sub_category_slug']),
            models.Index(fields=['sub_category_name']),
            models.Index(fields=['category']),
            models.Index(fields=['is_active', 'deleted_at']),
            models.Index(fields=['is_featured']),
            models.Index(fields=['display_order']),
            models.Index(fields=['popularity_score']),
        ]
        constraints = [
            models.CheckConstraint(
                check=models.Q(commission_rate__gte=0) & models.Q(commission_rate__lte=100),
                name='sub_category_commission_rate_range'
            )
        ]

    # ================================================================
    # STRING REPRESENTATION
    # ================================================================

    def __str__(self) -> str:
        return f"{self.category.category_name} › {self.sub_category_name}"

    def __repr__(self) -> str:
        return f"<SubCategory: {self.sub_category_name} (under {self.category.category_name})>"

    # ================================================================
    # PROPERTIES
    # ================================================================

    @property
    def image_url(self) -> str:
        """Get subcategory image URL or default"""
        if self.sub_category_image:
            return self.sub_category_image.url
        return "/static/defaults/default-category-image.png"

    @property
    def icon_url(self) -> str:
        """Get subcategory icon URL or default"""
        if self.sub_category_icon:
            return self.sub_category_icon.url
        return "/static/defaults/default-category-icon.png"

    @property
    def thumbnail_url(self) -> str:
        """Get subcategory thumbnail URL or default"""
        if self.sub_category_thumbnail:
            return self.sub_category_thumbnail.url
        return "/static/defaults/default-category-thumbnail.png"

    @property
    def sub_category_url(self) -> str:
        """Get subcategory page URL"""
        if self.sub_category_slug:
            return f"/categories/{self.category.category_slug}/{self.sub_category_slug}/"
        return f"/sub-categories/{self.uuid}/"

    @property
    def full_path(self) -> str:
        """Get full path (e.g. 'Electronics > Laptops')"""
        return f"{self.category.category_name} > {self.sub_category_name}"

    @property
    def breadcrumb(self) -> List[dict]:
        """Get breadcrumb trail including parent category"""
        return [
            {
                'name': self.category.category_name,
                'url': self.category.category_url,
                'slug': self.category.category_slug,
            },
            {
                'name': self.sub_category_name,
                'url': self.sub_category_url,
                'slug': self.sub_category_slug,
            },
        ]

    @property
    def effective_commission_rate(self):
        """
        Return this subcategory's commission rate if set,
        otherwise fall back to the parent category's rate.
        """
        if self.commission_rate and self.commission_rate > 0:
            return self.commission_rate
        return self.category.commission_rate

    # ================================================================
    # SLUG METHODS
    # ================================================================

    def generate_slug(self, save: bool = False) -> str:
        """Generate unique slug from subcategory name"""
        if not self.sub_category_name:
            return None

        base_slug = slugify(f"{self.category.category_name}-{self.sub_category_name}")
        slug = base_slug
        counter = 1

        while SubCategory.objects.filter(
            sub_category_slug=slug
        ).exclude(pk=self.pk).exists():
            slug = f"{base_slug}-{counter}"
            counter += 1

        self.sub_category_slug = slug

        if save:
            self.save(update_fields=['sub_category_slug'])

        return slug

    # ================================================================
    # STATUS METHODS
    # ================================================================

    def activate(self, save: bool = True) -> None:
        """Activate subcategory"""
        self.is_active = True
        if save:
            self.save(update_fields=['is_active'])

    def deactivate(self, save: bool = True) -> None:
        """Deactivate subcategory"""
        self.is_active = False
        if save:
            self.save(update_fields=['is_active'])

    def feature(self, save: bool = True) -> None:
        """Mark subcategory as featured"""
        self.is_featured = True
        if save:
            self.save(update_fields=['is_featured'])

    def unfeature(self, save: bool = True) -> None:
        """Remove featured status"""
        self.is_featured = False
        if save:
            self.save(update_fields=['is_featured'])

    def mark_trending(self, save: bool = True) -> None:
        """Mark subcategory as trending"""
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
        """Soft delete subcategory"""
        self.deleted_at = timezone.now()
        self.deleted_by = deleted_by_user
        self.deletion_reason = reason
        self.is_active = False

        if save:
            self.save()

    def restore(self, save: bool = True) -> None:
        """Restore soft-deleted subcategory"""
        self.deleted_at = None
        self.deleted_by = None
        self.deletion_reason = None
        self.is_active = True 

        if save:
            self.save()

    def delete(self, *args, **kwargs):
        """Override delete to use soft delete by default"""
        if kwargs.pop('hard_delete', False):
            super().delete(*args, **kwargs)
        else:
            self.soft_delete()

    # ================================================================
    # ANALYTICS METHODS
    # ================================================================

    def increment_view_count(self, save: bool = True) -> None:
        """Increment subcategory page view count"""
        self.view_count += 1
        self.last_viewed_at = timezone.now()

        if save:
            self.save(update_fields=['view_count', 'last_viewed_at'])

    def update_product_count(self, save: bool = True) -> int:
        """
        Update cached product count from related products.

        Requires Product.sub_category ForeignKey to be defined with
        related_name='products'. Until that FK exists this is a no-op
        that returns the current cached value safely.
        """
        if not hasattr(self, 'products'):
            # FK not wired yet — return existing cached value unchanged
            return self.product_count

        count = self.products.filter(is_active=True).count()
        self.product_count = count

        if save:
            self.save(update_fields=['product_count'])

        return count

    def calculate_popularity_score(self, save: bool = True) -> float:
        """
        Calculate popularity score.
        Formula: (products × 10) + (views × 0.1) + (featured × 50) + (trending × 25)
        """
        score = 0.0
        score += self.product_count * 10
        score += self.view_count * 0.1

        if self.is_featured:
            score += 50

        if self.is_trending:
            score += 25

        self.popularity_score = round(score, 2)

        if save:
            self.save(update_fields=['popularity_score'])

        return self.popularity_score

    # ================================================================
    # DATA EXPORT (GDPR)
    # ================================================================

    def export_data(self) -> dict:
        """Export subcategory data"""
        return {
            'uuid': str(self.uuid),
            'name': self.sub_category_name,
            'slug': self.sub_category_slug,
            'type': self.sub_category_type,
            'description': self.sub_category_description,
            'parent_category': {
                'uuid': str(self.category.uuid),
                'name': self.category.category_name,
                'slug': self.category.category_slug,
            },
            'full_path': self.full_path,
            'display': {
                'order': self.display_order,
                'style': self.display_style,
                'color': self.color_code,
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
            },
            'commission': {
                'rate': float(self.commission_rate),
                'effective_rate': float(self.effective_commission_rate),
            },
            'created_at': self.sub_category_created_at.isoformat(),
            'updated_at': self.sub_category_updated_at.isoformat(),
            'metadata': self.metadata,
        }

    # ================================================================
    # VALIDATION
    # ================================================================

    def clean(self) -> None:
        """Validate model fields"""
        super().clean()

        # Ensure the parent category is active
        if self.category_id and not self.category.is_active:
            raise ValidationError(
                _("Cannot add a subcategory to an inactive category.")
            )

        # Validate commission rate
        if self.commission_rate < 0 or self.commission_rate > 100:
            raise ValidationError(
                _("Commission rate must be between 0 and 100.")
            )

        # Validate price range
        if self.min_price is not None and self.max_price is not None:
            if self.min_price > self.max_price:
                raise ValidationError(
                    _("Minimum price cannot be greater than maximum price.")
                )

        # Validate hex color code format
        if self.color_code:
            import re
            if not re.match(r'^#[0-9A-Fa-f]{6}$', self.color_code):
                raise ValidationError(
                    _("Color code must be a valid hex color (e.g. '#FF5733').")
                )

    def save(self, *args, **kwargs):
        """Override save to auto-generate slug and run validation"""
        if self.sub_category_name and not self.sub_category_slug:
            self.generate_slug()

        self.full_clean()
        super().save(*args, **kwargs)