# apps/ponno/models/category.py

"""
Enhanced Category Model for International Business Standards
------------------------------------------------------------
Features:
- Hierarchical categories (parent/child)
- Category icons and images
- SEO optimization
- Category analytics
- Featured categories
- Multi-language support
- Display ordering
- Soft delete
- Category visibility control
"""

import uuid
from typing import Optional, List

from django.db import models
from django.conf import settings
from django.utils import timezone
from django.utils.text import slugify
from django.utils.translation import gettext_lazy as _
from django.core.exceptions import ValidationError
from django.db.models import Count, Q
from apps.ponno.models.brand import Brand

# ====================================================================
# CATEGORY MANAGER
# ====================================================================

class CategoryManager(models.Manager):
    """
    Custom manager for Category with optimized queries
    """
    
    def active_categories(self):
        """Get all active, non-deleted categories"""
        return self.filter(
            is_active=True,
            deleted_at__isnull=True
        )
    
    def root_categories(self):
        """Get all root level categories (no parent)"""
        return self.filter(
            parent__isnull=True,
            is_active=True,
            deleted_at__isnull=True
        )
    
    def featured_categories(self):
        """Get featured categories"""
        return self.filter(
            is_featured=True,
            is_active=True,
            deleted_at__isnull=True
        )
    
    def popular_categories(self, limit=10):
        """Get popular categories by product count"""
        return self.filter(
            is_active=True,
            deleted_at__isnull=True
        ).annotate(
            product_count=Count('products')
        ).filter(
            product_count__gt=0
        ).order_by('-product_count')[:limit]
    
    def search_categories(self, query):
        """Search categories by name or description"""
        return self.filter(
            Q(category_name__icontains=query) |
            Q(category_description__icontains=query) |
            Q(category_slug__icontains=query),
            is_active=True,
            deleted_at__isnull=True
        )
    
    def get_by_slug(self, slug):
        """Get category by slug"""
        return self.get(
            category_slug=slug,
            is_active=True,
            deleted_at__isnull=True
        )
    
    def get_category_tree(self):
        """Get hierarchical category tree"""
        root_categories = self.root_categories()
        return [cat.get_descendants_tree() for cat in root_categories]


# ====================================================================
# CATEGORY MODEL
# ====================================================================

class Category(models.Model):
    """
    Enhanced Category Model with Hierarchical Structure
    
    Features:
    - Parent/child relationships
    - Category images and icons
    - SEO optimization
    - Analytics tracking
    - Display ordering
    - Featured categories
    - Soft delete
    """
    
    # ================================================================
    # CHOICES
    # ================================================================
    
    class CategoryType(models.TextChoices):
        PRODUCT = 'product', _('Product Category')
        SERVICE = 'service', _('Service Category')
        DIGITAL = 'digital', _('Digital Goods')
        PHYSICAL = 'physical', _('Physical Goods')
        MIXED = 'mixed', _('Mixed')
    
    class DisplayStyle(models.TextChoices):
        GRID = 'grid', _('Grid View')
        LIST = 'list', _('List View')
        CAROUSEL = 'carousel', _('Carousel View')
        FEATURED = 'featured', _('Featured View')
    
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

    brand = models.ForeignKey(
    Brand,
    on_delete=models.SET_NULL,
    null=True,
    blank=True,
    related_name='categories',
    help_text=_("Brand associated with this category")
)
    
    category_name = models.CharField(
        _("Category Name"),
        max_length=150,
        unique=True,
        db_index=True,
        help_text=_("Category name")
    )
    
    category_slug = models.SlugField(
        _("Category Slug"),
        max_length=160,
        unique=True,
        blank=True,
        null=True,
        db_index=True,
        help_text=_("URL-friendly category identifier")
    )
    
    category_type = models.CharField(
        _("Category Type"),
        max_length=20,
        choices=CategoryType.choices,
        default=CategoryType.PRODUCT,
        help_text=_("Type of category")
    )

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='category_creator',
        help_text=_("User who created this category")
    )

    managed_by = models.ManyToManyField(
        settings.AUTH_USER_MODEL,
        related_name='managed_categories',
        blank=True,
        help_text=_("Users who can manage this Category")
    )
    
    # ================================================================
    # HIERARCHICAL STRUCTURE
    # ================================================================
    
    parent = models.ForeignKey(
        'self',
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name='children',
        help_text=_("Parent category for hierarchical structure")
    )
    
    level = models.PositiveIntegerField(
        _("Level"),
        default=0,
        help_text=_("Depth level in hierarchy (0 = root)")
    )
    
    path = models.CharField(
        _("Path"),
        max_length=500,
        blank=True,
        help_text=_("Category path for quick lookup")
    )
    
    # ================================================================
    # CATEGORY INFORMATION
    # ================================================================
    
    category_description = models.TextField(
        _("Category Description"),
        max_length=1000,
        blank=True,
        null=True,
        help_text=_("Detailed category description")
    )
    
    category_short_description = models.CharField(
        _("Short Description"),
        max_length=200,
        blank=True,
        null=True,
        help_text=_("Brief category description")
    )
    
    # ================================================================
    # MEDIA
    # ================================================================
    
    category_image = models.ImageField(
        _("Category Image"),
        upload_to="categories/images/%Y/%m/",
        blank=True,
        null=True,
        help_text=_("Category banner/cover image")
    )
    
    category_icon = models.ImageField(
        _("Category Icon"),
        upload_to="categories/icons/%Y/%m/",
        blank=True,
        null=True,
        help_text=_("Category icon image")
    )
    
    category_thumbnail = models.ImageField(
        _("Category Thumbnail"),
        upload_to="categories/thumbnails/%Y/%m/",
        blank=True,
        null=True,
        help_text=_("Category thumbnail image")
    )
    
    icon_class = models.CharField(
        _("Icon Class"),
        max_length=50,
        blank=True,
        null=True,
        help_text=_("CSS icon class (e.g., 'fa fa-laptop')")
    )
    
    color_code = models.CharField(
        _("Color Code"),
        max_length=7,
        blank=True,
        null=True,
        help_text=_("Hex color code for category theme (e.g., '#FF5733')")
    )
    
    # ================================================================
    # STATUS FLAGS
    # ================================================================
    
    is_active = models.BooleanField(
        _("Active"),
        default=True,
        db_index=True,
        help_text=_("Category is active and visible")
    )
    
    is_featured = models.BooleanField(
        _("Featured"),
        default=False,
        db_index=True,
        help_text=_("Category is featured on homepage")
    )
    
    is_trending = models.BooleanField(
        _("Trending"),
        default=False,
        help_text=_("Category is currently trending")
    )
    
    is_visible_in_menu = models.BooleanField(
        _("Visible in Menu"),
        default=True,
        help_text=_("Show category in navigation menu")
    )
    
    is_visible_on_homepage = models.BooleanField(
        _("Visible on Homepage"),
        default=False,
        help_text=_("Show category on homepage")
    )
    
    # ================================================================
    # DISPLAY SETTINGS
    # ================================================================
    
    display_style = models.CharField(
        _("Display Style"),
        max_length=20,
        choices=DisplayStyle.choices,
        default=DisplayStyle.GRID,
        help_text=_("How to display products in this category")
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
    
    show_subcategories = models.BooleanField(
        _("Show Subcategories"),
        default=True,
        help_text=_("Show subcategories on category page")
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
        help_text=_("Total category page views")
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
        help_text=_("Last time category page was viewed")
    )
    
    # ================================================================
    # COMMISSION & PRICING (for marketplace)
    # ================================================================
    
    commission_rate = models.DecimalField(
        _("Commission Rate"),
        max_digits=5,
        decimal_places=2,
        default=0.0,
        help_text=_("Platform commission percentage (0-100)")
    )
    
    min_price = models.DecimalField(
        _("Minimum Price"),
        max_digits=10,
        decimal_places=2,
        null=True,
        blank=True,
        help_text=_("Minimum allowed price for products in this category")
    )
    
    max_price = models.DecimalField(
        _("Maximum Price"),
        max_digits=10,
        decimal_places=2,
        null=True,
        blank=True,
        help_text=_("Maximum allowed price for products in this category")
    )
    
    # ================================================================
    # TIMESTAMPS
    # ================================================================
    
    category_created_at = models.DateTimeField(
        _("Created At"),
        auto_now_add=True,
        help_text=_("When category was created")
    )
    
    category_updated_at = models.DateTimeField(
        _("Updated At"),
        auto_now=True,
        help_text=_("Last time category was updated")
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
        related_name='deleted_categories',
        help_text=_("User who deleted this category")
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
        help_text=_("Additional category metadata in JSON format")
    )
    
    # ================================================================
    # MANAGER
    # ================================================================
    
    objects = CategoryManager()
    
    # ================================================================
    # META
    # ================================================================
    
    class Meta:
        verbose_name = _("Category")
        verbose_name_plural = _("Categories")
        db_table = 'categories'
        ordering = ['display_order', 'category_name']
        indexes = [
            models.Index(fields=['uuid']),
            models.Index(fields=['category_slug']),
            models.Index(fields=['category_name']),
            models.Index(fields=['parent']),
            models.Index(fields=['level']),
            models.Index(fields=['is_active', 'deleted_at']),
            models.Index(fields=['is_featured']),
            models.Index(fields=['display_order']),
            models.Index(fields=['popularity_score']),
        ]
        constraints = [
            models.CheckConstraint(
                check=models.Q(level__gte=0) & models.Q(level__lte=10),
                name='level_range'
            ),
            models.CheckConstraint(
                check=models.Q(commission_rate__gte=0) & models.Q(commission_rate__lte=100),
                name='commission_rate_range'
            )
        ]
    
    # ================================================================
    # STRING REPRESENTATION
    # ================================================================
    
    def __str__(self) -> str:
        """Return category name with level indication"""
        if self.parent:
            return f"{'  ' * self.level}↳ {self.category_name}"
        return self.category_name
    
    def __repr__(self) -> str:
        return f"<Category: {self.category_name} (Level {self.level})>"
    
    # ================================================================
    # PROPERTIES
    # ================================================================
    
    @property
    def image_url(self) -> str:
        """Get category image URL or default"""
        if self.category_image:
            return self.category_image.url
        return "/static/defaults/default-category-image.png"
    
    @property
    def icon_url(self) -> str:
        """Get category icon URL or default"""
        if self.category_icon:
            return self.category_icon.url
        return "/static/defaults/default-category-icon.png"
    
    @property
    def thumbnail_url(self) -> str:
        """Get category thumbnail URL or default"""
        if self.category_thumbnail:
            return self.category_thumbnail.url
        return "/static/defaults/default-category-thumbnail.png"
    
    @property
    def category_url(self) -> str:
        """Get category page URL"""
        if self.category_slug:
            return f"/categories/{self.category_slug}/"
        return f"/categories/{self.uuid}/"
    
    @property
    def full_path(self) -> str:
        """Get full category path (e.g., 'Electronics > Laptops > Gaming')"""
        if not self.parent:
            return self.category_name
        
        path_parts = []
        current = self
        while current:
            path_parts.insert(0, current.category_name)
            current = current.parent
        
        return ' > '.join(path_parts)
    
    @property
    def breadcrumb(self) -> List[dict]:
        """Get breadcrumb trail"""
        breadcrumbs = []
        current = self
        
        while current:
            breadcrumbs.insert(0, {
                'name': current.category_name,
                'url': current.category_url,
                'slug': current.category_slug
            })
            current = current.parent
        
        return breadcrumbs
    
    @property
    def is_root(self) -> bool:
        """Check if this is a root category"""
        return self.parent is None
    
    @property
    def is_leaf(self) -> bool:
        """Check if this is a leaf category (no children)"""
        return not self.children.exists()
    
    @property
    def has_children(self) -> bool:
        """Check if category has children"""
        return self.children.filter(is_active=True, deleted_at__isnull=True).exists()
    
    @property
    def child_count(self) -> int:
        """Get count of active children"""
        return self.children.filter(is_active=True, deleted_at__isnull=True).count()
    
    # ================================================================
    # SLUG METHODS
    # ================================================================
    
    def generate_slug(self, save: bool = False) -> str:
        """Generate unique slug from category name"""
        if not self.category_name:
            return None
        
        base_slug = slugify(self.category_name)
        slug = base_slug
        counter = 1
        
        # Ensure uniqueness
        while Category.objects.filter(
            category_slug=slug
        ).exclude(pk=self.pk).exists():
            slug = f"{base_slug}-{counter}"
            counter += 1
        
        self.category_slug = slug
        
        if save:
            self.save(update_fields=['category_slug'])
        
        return slug
    
    # ================================================================
    # HIERARCHY METHODS
    # ================================================================
    
    def get_ancestors(self, include_self=False) -> List['Category']:
        """Get all ancestor categories"""
        ancestors = []
        current = self.parent if not include_self else self
        
        while current:
            ancestors.insert(0, current)
            current = current.parent
        
        return ancestors
    
    def get_descendants(self, include_self=False) -> List['Category']:
        """Get all descendant categories (flat list)"""
        descendants = [self] if include_self else []
        
        for child in self.children.filter(is_active=True, deleted_at__isnull=True):
            descendants.append(child)
            descendants.extend(child.get_descendants())
        
        return descendants
    
    def get_descendants_tree(self) -> dict:
        """Get descendants as nested dictionary tree"""
        return {
            'category': self,
            'children': [
                child.get_descendants_tree()
                for child in self.children.filter(
                    is_active=True,
                    deleted_at__isnull=True
                ).order_by('display_order', 'category_name')
            ]
        }
    
    def get_siblings(self, include_self=False) -> models.QuerySet:
        """Get sibling categories (same parent)"""
        siblings = Category.objects.filter(
            parent=self.parent,
            is_active=True,
            deleted_at__isnull=True
        )
        
        if not include_self:
            siblings = siblings.exclude(pk=self.pk)
        
        return siblings
    
    def get_root(self) -> 'Category':
        """Get root category of this branch"""
        current = self
        while current.parent:
            current = current.parent
        return current
    
    def move_to(self, new_parent: Optional['Category'], save: bool = True) -> None:
        """Move category to a new parent"""
        # Prevent circular reference
        if new_parent and (new_parent == self or new_parent in self.get_descendants()):
            raise ValidationError(_("Cannot move category to itself or its descendants"))
        
        self.parent = new_parent
        self.update_level()
        
        if save:
            self.save()
            # Update all descendants' levels
            for descendant in self.get_descendants():
                descendant.update_level(save=True)
    
    def update_level(self, save: bool = False) -> None:
        """Update level based on parent"""
        if self.parent:
            self.level = self.parent.level + 1
        else:
            self.level = 0
        
        # Update path
        if self.parent:
            self.path = f"{self.parent.path}/{self.category_slug}"
        else:
            self.path = f"/{self.category_slug}"
        
        if save:
            self.save(update_fields=['level', 'path'])
    
    # ================================================================
    # STATUS METHODS
    # ================================================================
    
    def activate(self, save: bool = True) -> None:
        """Activate category"""
        self.is_active = True
        if save:
            self.save(update_fields=['is_active'])
    
    def deactivate(self, save: bool = True) -> None:
        """Deactivate category and all descendants"""
        self.is_active = False
        if save:
            self.save(update_fields=['is_active'])
        
        # Deactivate all descendants
        for descendant in self.get_descendants():
            descendant.deactivate(save=True)
    
    def feature(self, save: bool = True) -> None:
        """Mark category as featured"""
        self.is_featured = True
        if save:
            self.save(update_fields=['is_featured'])
    
    def unfeature(self, save: bool = True) -> None:
        """Remove featured status"""
        self.is_featured = False
        if save:
            self.save(update_fields=['is_featured'])
    
    def mark_trending(self, save: bool = True) -> None:
        """Mark category as trending"""
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
        cascade: bool = True,
        save: bool = True
    ) -> None:
        """Soft delete category"""
        self.deleted_at = timezone.now()
        self.deleted_by = deleted_by_user
        self.deletion_reason = reason
        self.is_active = False
        
        if save:
            self.save()
        
        # Optionally cascade to children
        if cascade:
            for child in self.children.all():
                child.soft_delete(
                    deleted_by_user=deleted_by_user,
                    reason=f"Parent category deleted: {reason}",
                    cascade=True
                )
    
    def restore(self, cascade: bool = True, save: bool = True) -> None:
        """Restore soft-deleted category"""
        self.deleted_at = None
        self.deleted_by = None
        self.deletion_reason = None
        self.is_active = True
        
        if save:
            self.save()
        
        # Optionally restore children
        if cascade:
            for child in self.children.filter(deleted_at__isnull=False):
                child.restore(cascade=True)
    
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
        """Increment category page view count"""
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
        Calculate popularity score
        Formula: (products * 10) + (views * 0.1) + (featured * 50)
        """
        score = 0.0
        
        # Product count weight
        score += self.product_count * 10
        
        # View count weight
        score += self.view_count * 0.1
        
        # Featured bonus
        if self.is_featured:
            score += 50
        
        # Trending bonus
        if self.is_trending:
            score += 25
        
        self.popularity_score = round(score, 2)
        
        if save:
            self.save(update_fields=['popularity_score'])
        
        return self.popularity_score
    
    def get_total_product_count(self) -> int:
        """Get total products including all descendants"""
        total = self.product_count
        for descendant in self.get_descendants():
            total += descendant.product_count
        return total
    
    # ================================================================
    # DATA EXPORT (GDPR)
    # ================================================================
    
    def export_data(self) -> dict:
        """Export category data"""
        data = {
            'uuid': str(self.uuid),
            'name': self.category_name,
            'slug': self.category_slug,
            'type': self.category_type,
            'description': self.category_description,
            'hierarchy': {
                'level': self.level,
                'path': self.full_path,
                'parent': self.parent.category_name if self.parent else None,
                'children_count': self.child_count,
            },
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
            'created_at': self.category_created_at.isoformat(),
            'updated_at': self.category_updated_at.isoformat(),
            'metadata': self.metadata,
        }
        
        return data
    
    # ================================================================
    # VALIDATION
    # ================================================================
    
    def clean(self) -> None:
        """Validate model fields"""
        super().clean()
        
        # Prevent circular reference
        if self.parent:
            if self.parent == self:
                raise ValidationError(_("Category cannot be its own parent"))
            
            if self in self.parent.get_ancestors(include_self=True):
                raise ValidationError(_("Circular reference detected in category hierarchy"))
        
        # Validate level depth (max 10 levels)
        if self.level > 10:
            raise ValidationError(_("Category hierarchy cannot exceed 10 levels"))
        
        # Validate commission rate
        if self.commission_rate < 0 or self.commission_rate > 100:
            raise ValidationError(_("Commission rate must be between 0 and 100"))
        
        # Validate price range
        if self.min_price and self.max_price:
            if self.min_price > self.max_price:
                raise ValidationError(_("Minimum price cannot be greater than maximum price"))
    
    def save(self, *args, **kwargs):
        """Override save to generate slug, update level, and run validation"""
        # Generate slug if name exists but slug doesn't
        if self.category_name and not self.category_slug:
            self.generate_slug()
        
        # Update level and path
        self.update_level()
        
        # Run validation
        self.full_clean()
        
        super().save(*args, **kwargs)