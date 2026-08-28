# apps/ponno/models/product.py

"""
Enhanced Product Model for International Business Standards
-----------------------------------------------------------
Complete e-commerce product management with all essential features
"""

import logging
import uuid
from decimal import Decimal
from typing import Optional
from django.urls import reverse
from django.db import models
from django.conf import settings
from django.utils import timezone
from django.utils.text import slugify
from django.utils.translation import gettext_lazy as _
from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator, MaxValueValidator
from django.db.models import Avg, Count, Q, Sum

from apps.ponno.models.brand import Brand
from apps.ponno.models.category import Category
from apps.ponno.models.sub_category import SubCategory


logger = logging.getLogger(__name__)


# ====================================================================
# PRODUCT MANAGER
# ====================================================================

class ProductManager(models.Manager):
    """Custom manager for Product with optimized queries"""

    def active_products(self):
        """Get all active, non-deleted products"""
        return self.filter(
            is_active=True,
            deleted_at__isnull=True
        ).select_related('brand', 'category', 'dealer')

    def in_stock(self):
        """Get products in stock"""
        return self.active_products().filter(stock__gt=0)

    def featured_products(self):
        """Get featured products"""
        return self.filter(
            is_featured=True,
            is_active=True,
            deleted_at__isnull=True
        ).select_related('brand', 'category')

    def on_sale(self):
        """Get products on sale (with discount)"""
        return self.active_products().filter(discount_percentage__gt=0)

    def trending_products(self, limit=20):
        """Get trending products"""
        return self.active_products().order_by('-view_count', '-total_sales')[:limit]

    def bestsellers(self, limit=20):
        """Get bestselling products"""
        return self.active_products().order_by('-total_sales')[:limit]

    def low_stock(self, threshold=10):
        """Get low stock products"""
        return self.active_products().filter(
            stock__lte=threshold,
            stock__gt=0
        )

    def search_products(self, query):
        """Search products"""
        return self.filter(
            Q(product_name__icontains=query) |
            Q(description__icontains=query) |
            Q(sku__icontains=query) |
            Q(brand__brand_name__icontains=query),
            is_active=True,
            deleted_at__isnull=True
        ).select_related('brand', 'category', 'dealer')

    def get_by_slug(self, slug):
        """Get product by slug"""
        return self.select_related('brand', 'category', 'dealer').get(
            slug=slug,
            is_active=True,
            deleted_at__isnull=True
        )


# ====================================================================
# PRODUCT MODEL
# ====================================================================

class Product(models.Model):
    """
    Enhanced Product Model
    
    Complete product management with pricing, inventory,
    analytics, and all essential e-commerce features.
    """
    
    # ================================================================
    # CHOICES
    # ================================================================
    
    class ProductCondition(models.TextChoices):
        NEW = 'new', _('New')
        REFURBISHED = 'refurbished', _('Refurbished')
        USED = 'used', _('Used')
    
    class StockStatus(models.TextChoices):
        IN_STOCK = 'in_stock', _('In Stock')
        LOW_STOCK = 'low_stock', _('Low Stock')
        OUT_OF_STOCK = 'out_of_stock', _('Out of Stock')
        PRE_ORDER = 'pre_order', _('Pre-Order')
    
    # ================================================================
    # IDENTIFICATION
    # ================================================================
    
    product_id = models.UUIDField(
        _("Product UUID"),
        default=uuid.uuid4,
        editable=False,
        unique=True,
        db_index=True,
        help_text=_("Unique identifier")
    )
    
    sku = models.CharField(
        _("SKU"),
        max_length=100,
        unique=True,
        db_index=True,
        blank=True,
        null=True,  
        help_text=_("Stock Keeping Unit")
    )
    
    barcode = models.CharField(
        _("Barcode"),
        max_length=100,
        blank=True,
        null=True,
        help_text=_("Product barcode (EAN/UPC)")
    )
    
    product_title = models.CharField(
        _("Product Title"),
        max_length=200,
        db_index=True,
        blank=True,
        null=True,
        help_text=_("Product title")
    )

    product_name = models.CharField(
        _("Product Name"),
        max_length=200,
        db_index=True,
        help_text=_("Product name")
    )
    
    slug = models.SlugField(
        _("Slug"),
        max_length=220,
        unique=True,
        blank=True,
        db_index=True,
        help_text=_("URL-friendly identifier")
    )
    
    # ================================================================
    # RELATIONSHIPS
    # ================================================================
    
    brand = models.ForeignKey(
        Brand,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='products',
        help_text=_("Product brand")
    )
    
    category = models.ForeignKey(
        Category,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='products',
        help_text=_("Product category")
    )

    sub_category = models.ForeignKey(
        SubCategory,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='products',
        help_text=_("Product sub-category")
    )
    
    dealer = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='products',
        limit_choices_to={'role': 'dealer'},
        help_text=_("Product seller")
    )
    
    # ================================================================
    # DESCRIPTION
    # ================================================================
    
    description = models.TextField(
        _("Description"),
        max_length=5000,
        blank=True,
        null=True,
        help_text=_("Full description")
    )
    
    short_description = models.TextField(
        _("Short Description"),
        max_length=500,
        blank=True,
        null=True,
        help_text=_("Brief description")
    )
    
    warrenty_info = models.TextField(
        _("Warranty"),
        max_length=2000,
        blank=True,
        null=True,
        help_text=_("Warranty information")
    )
    
    delivery_info = models.JSONField(
        _("Delivery Information"),
        default=dict,
        blank=True,
        null=True,
        help_text=_("Delivery details (JSON)")
    )
    
    product_condition = models.CharField(
        _("Condition"),
        max_length=20,
        choices=ProductCondition.choices,
        default=ProductCondition.NEW,
        help_text=_("Product condition")
    )
    
    # ================================================================
    # MEDIA
    # ================================================================
    
    image = models.ImageField(
        _("Main Image"),
        upload_to='products/%Y/%m/',
        blank=True,
        null=True,
        help_text=_("Primary image")
    )
    
    video_url = models.URLField(
        _("Video URL"),
        max_length=500,
        blank=True,
        null=True,
        help_text=_("Product video URL")
    )

    # ================================================================
    # ANALYTICS (share_count lives here alongside the rest below)
    # ================================================================

    share_count = models.PositiveIntegerField(
        _("Shares"),
        default=0,
        blank=True,
        null=True,
        help_text=_("Times shared"),
    )
    
    # ================================================================
    # PRICING
    # ================================================================
    
    brand_price = models.DecimalField(
        _("Brand Price (MRP)"),
        max_digits=12,
        decimal_places=2,
        null=True,
        blank=True,
        validators=[MinValueValidator(Decimal('0.00'))],
        help_text=_("Manufacturer's price")
    )
    is_brand_price_visible = models.BooleanField(
        _("Show Brand Price"),
        default=True,
        blank=True,
        null=True,
        help_text=_("Display manufacturer's price (MRP) to customers")
    )
    
    buying_price = models.DecimalField(
        _("Buying Price"),
        max_digits=12,
        decimal_places=2,
        null=True,
        blank=True,
        validators=[MinValueValidator(Decimal('0.01'))],
        help_text=_("Cost price")
    )
    is_buying_price_visible = models.BooleanField(
        _("Show Buying Price"),
        default=False,
        blank=True,
        null=True,
        help_text=_("Display cost price (usually internal-only, keep hidden from customers)")
    )
    
    selling_price = models.DecimalField(
        _("Selling Price"),
        max_digits=12,
        decimal_places=2,
        validators=[MinValueValidator(Decimal('0.00'))],
        help_text=_("Selling price")
    )    

    is_selling_price_visible = models.BooleanField(
        _("Show Selling Price"),
        default=True,
        blank=True,
        null=True,
        help_text=_("Display selling price to customers")
    )

    discount_percentage = models.DecimalField(
        _("Discount %"),
        max_digits=5,
        decimal_places=2,
        default=0,
        null=True,
        blank=True,
        validators=[
            MinValueValidator(Decimal('0.00')),
            MaxValueValidator(Decimal('100.00'))
        ],
        help_text=_("Discount percentage")
    )
    
    final_price = models.DecimalField(
        _("Final Price"),
        max_digits=12,
        decimal_places=2,
        null=True,
        blank=True,
        help_text=_("Price after discount")
    )
    
    tax_rate = models.DecimalField(
        _("Tax Rate %"),
        max_digits=5,
        decimal_places=2,
        default=0,
        validators=[
            MinValueValidator(Decimal('0.00')),
            MaxValueValidator(Decimal('100.00'))
        ],
        help_text=_("Tax percentage")
    )
    
    currency = models.CharField(
        _("Currency"),
        max_length=3,
        default='BDT',
        help_text=_("Currency code")
    )
    
    # ================================================================
    # INVENTORY
    # ================================================================
    
    stock = models.PositiveIntegerField(
        _("Stock"),
        default=0,
        help_text=_("Available quantity")
    )
    
    stock_status = models.CharField(
        _("Stock Status"),
        max_length=20,
        choices=StockStatus.choices,
        default=StockStatus.IN_STOCK,
        help_text=_("Current status")
    )
    
    low_stock_threshold = models.PositiveIntegerField(
        _("Low Stock Alert"),
        default=10,
        help_text=_("Alert threshold")
    )
    
    track_inventory = models.BooleanField(
        _("Track Inventory"),
        default=True,
        help_text=_("Enable tracking")
    )
    
    allow_backorder = models.BooleanField(
        _("Allow Backorder"),
        default=False,
        help_text=_("Allow when out of stock")
    )
    
    min_order_quantity = models.PositiveIntegerField(
        _("Min Order Qty"),
        default=1,
        help_text=_("Minimum per order")
    )
    
    max_order_quantity = models.PositiveIntegerField(
        _("Max Order Qty"),
        null=True,
        blank=True,
        help_text=_("Maximum per order")
    )
    
    # ================================================================
    # SHIPPING
    # ================================================================
    
    weight = models.DecimalField(
        _("Weight (kg)"),
        max_digits=10,
        decimal_places=2,
        null=True,
        blank=True,
        validators=[MinValueValidator(Decimal('0.00'))],
        help_text=_("Weight in kg")
    )
    
    length = models.DecimalField(
        _("Length (cm)"),
        max_digits=10,
        decimal_places=2,
        null=True,
        blank=True,
        help_text=_("Length in cm")
    )
    
    width = models.DecimalField(
        _("Width (cm)"),
        max_digits=10,
        decimal_places=2,
        null=True,
        blank=True,
        help_text=_("Width in cm")
    )
    
    height = models.DecimalField(
        _("Height (cm)"),
        max_digits=10,
        decimal_places=2,
        null=True,
        blank=True,
        help_text=_("Height in cm")
    )
    
    free_shipping = models.BooleanField(
        _("Free Shipping"),
        default=False,
        help_text=_("Free shipping")
    )
    
    shipping_cost = models.DecimalField(
        _("Shipping Cost"),
        max_digits=10,
        decimal_places=2,
        null=True,
        blank=True,
        help_text=_("Flat shipping cost")
    )
    
    # ================================================================
    # SEO
    # ================================================================
    
    meta_title = models.CharField(
        _("Meta Title"),
        max_length=60,
        blank=True,
        null=True,
        help_text=_("SEO title")
    )
    
    meta_description = models.TextField(
        _("Meta Description"),
        max_length=160,
        blank=True,
        null=True,
        help_text=_("SEO description")
    )
    
    meta_keywords = models.CharField(
        _("Meta Keywords"),
        max_length=255,
        blank=True,
        null=True,
        help_text=_("SEO keywords")
    )
    
    # ================================================================
    # STATUS
    # ================================================================
    
    is_active = models.BooleanField(
        _("Active"),
        default=True,
        db_index=True,
        help_text=_("Product is active")
    )
    
    is_featured = models.BooleanField(
        _("Featured"),
        default=False,
        db_index=True,
        help_text=_("Featured product")
    )
    
    is_trending = models.BooleanField(
        _("Trending"),
        default=False,
        help_text=_("Trending product")
    )
    
    is_verified = models.BooleanField(
        _("Verified"),
        default=False,
        help_text=_("Admin verified")
    )
    
    # ================================================================
    # ANALYTICS
    # ================================================================
    
    view_count = models.PositiveIntegerField(
        _("Views"),
        default=0,
        help_text=_("Total views")
    )
    
    wishlist_count = models.PositiveIntegerField(
        _("Wishlists"),
        default=0,
        help_text=_("Times wishlisted")
    )
    
    total_sales = models.PositiveIntegerField(
        _("Total Sales"),
        default=0,
        help_text=_("Units sold")
    )
    
    revenue_generated = models.DecimalField(
        _("Revenue"),
        max_digits=15,
        decimal_places=2,
        default=0,
        help_text=_("Total revenue")
    )
    
    rating_average = models.DecimalField(
        _("Rating"),
        max_digits=3,
        decimal_places=2,
        default=0,
        validators=[
            MinValueValidator(Decimal('0.00')),
            MaxValueValidator(Decimal('5.00'))
        ],
        help_text=_("Average rating")
    )
    
    review_count = models.PositiveIntegerField(
        _("Reviews"),
        default=0,
        help_text=_("Number of reviews")
    )
    
    last_viewed_at = models.DateTimeField(
        _("Last Viewed"),
        null=True,
        blank=True,
        help_text=_("Last view time")
    )
    
    # ================================================================
    # TIMESTAMPS
    # ================================================================
    
    created_at = models.DateTimeField(
        _("Created"),
        auto_now_add=True,
        help_text=_("Creation date")
    )
    
    updated_at = models.DateTimeField(
        _("Updated"),
        auto_now=True,
        help_text=_("Last update")
    )
    
    # ================================================================
    # SOFT DELETE
    # ================================================================
    
    deleted_at = models.DateTimeField(
        _("Deleted At"),
        null=True,
        blank=True,
        db_index=True,
        help_text=_("Deletion timestamp")
    )
    
    deleted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='deleted_products',
        help_text=_("Deleted by")
    )
    
    deletion_reason = models.TextField(
        _("Deletion Reason"),
        max_length=500,
        blank=True,
        null=True,
        help_text=_("Why deleted")
    )
    
    # ================================================================
    # METADATA
    # ================================================================
    
    metadata = models.JSONField(
        _("Metadata"),
        default=dict,
        blank=True,
        help_text=_("Additional data")
    )
    
    # ================================================================
    # MANAGER
    # ================================================================
    
    objects = ProductManager()
    
    # ================================================================
    # META
    # ================================================================
    
    class Meta:
        verbose_name = _("Product")
        verbose_name_plural = _("Products")
        db_table = 'products'
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['product_id']),
            models.Index(fields=['sku']),
            models.Index(fields=['slug']),
            models.Index(fields=['brand', 'category']),
            models.Index(fields=['is_active', 'deleted_at']),
            models.Index(fields=['is_featured']),
            models.Index(fields=['stock_status']),
            models.Index(fields=['selling_price']),
            models.Index(fields=['discount_percentage']),
            models.Index(fields=['view_count']),
            models.Index(fields=['total_sales']),
            models.Index(fields=['rating_average']),
        ]
    
    # ================================================================
    # STRING REPRESENTATION
    # ================================================================
    
    def __str__(self) -> str:
        return f"{self.product_name} ({self.brand})"
    
    def __repr__(self) -> str:
        return f"<Product: {self.product_name} - {self.sku}>"
    
    # ================================================================
    # PROPERTIES
    # ================================================================
    
    @property
    def is_in_stock(self) -> bool:
        """Check if in stock"""
        return self.stock > 0
    
    @property
    def is_low_stock(self) -> bool:
        """Check if low stock"""
        return 0 < self.stock <= self.low_stock_threshold
    
    @property
    def image_url(self) -> str:
        """Get image URL"""
        if self.image:
            return self.image.url
        return "/static/defaults/default-product.png"
    
    @property
    def product_url(self) -> str:
        """Get product URL"""
        if self.slug:
            return reverse('ponno:product_detail', kwargs={'slug': self.slug})
        return reverse('ponno:product_detail', kwargs={'slug': str(self.product_id)})
    
    @property
    def discount_amount(self) -> Decimal:
        """Calculate discount amount"""
        if self.discount_percentage > 0:
            return (self.selling_price * self.discount_percentage) / 100
        return Decimal('0.00')
    
    @property
    def profit_margin(self) -> Decimal:
        """Calculate profit margin"""
        if self.buying_price:
            profit = self.selling_price - self.buying_price
            return (profit / self.buying_price) * 100
        return Decimal('0.00')
    
    @property
    def is_on_sale(self) -> bool:
        """Check if on sale"""
        return self.discount_percentage > 0
    
    # ================================================================
    # SLUG GENERATION
    # ================================================================
    
    def generate_slug(self, save: bool = False) -> str:
        """Generate unique slug"""
        if not self.product_name:
            return None
        
        if self.brand:
            base_slug = slugify(f"{self.product_name}-{self.brand.brand_name}")
        else:
            base_slug = slugify(self.product_name)
        
        # slugify() strips non-ASCII characters by default, so a
        # non-Latin-script product_name (Bangla, Arabic, CJK, etc.) or
        # one made entirely of symbols/emoji can legitimately reduce to
        # an empty string. Falling through with base_slug='' would save
        # a Product with slug='' — valid per the field's blank=True, but
        # unreachable via any {% url %} using the slug converter
        # ([-a-zA-Z0-9_]+, requires 1+ chars) — hence NoReverseMatch on
        # any template that links to it.
        if not base_slug:
            base_slug = f"product-{uuid.uuid4().hex[:10]}"
        
        slug = base_slug
        counter = 1
        
        while Product.objects.filter(slug=slug).exclude(pk=self.pk).exists():
            slug = f"{base_slug}-{counter}"
            counter += 1
        
        self.slug = slug
        
        if save:
            self.save(update_fields=['slug'])
        
        return slug

    # ================================================================
    # PRICING METHODS
    # ================================================================
    
    def calculate_final_price(self, save: bool = False) -> Decimal:
        """Calculate final price after discount"""
        discount_amount = self.discount_amount
        self.final_price = self.selling_price - discount_amount
        
        if save:
            self.save(update_fields=['final_price'])
        
        return self.final_price
    
    def get_price_with_tax(self) -> Decimal:
        """Get price including tax"""
        price = self.final_price or self.selling_price
        tax_amount = (price * self.tax_rate) / 100
        return price + tax_amount
    
    def calculate_profit(self) -> Decimal:
        """Calculate profit per unit"""
        price = self.final_price or self.selling_price
        if not self.buying_price:
            return Decimal('0.00')
        return price - self.buying_price
    # ================================================================
    # INVENTORY METHODS
    # ================================================================
    
    def update_stock_status(self, save: bool = True) -> None:
        """Update stock status based on quantity"""
        if self.stock == 0:
            self.stock_status = self.StockStatus.OUT_OF_STOCK
        elif self.stock <= self.low_stock_threshold:
            self.stock_status = self.StockStatus.LOW_STOCK
        else:
            self.stock_status = self.StockStatus.IN_STOCK
        
        if save:
            self.save(update_fields=['stock_status'])
    
    def add_stock(self, quantity: int, save: bool = True) -> int:
        """Add stock"""
        self.stock += quantity
        self.update_stock_status(save=False)
        
        if save:
            self.save(update_fields=['stock', 'stock_status'])
        
        return self.stock
    
    def reduce_stock(self, quantity: int, save: bool = True) -> int:
        """Reduce stock"""
        if quantity > self.stock:
            raise ValidationError(_("Insufficient stock"))
        
        self.stock -= quantity
        self.update_stock_status(save=False)
        
        if save:
            self.save(update_fields=['stock', 'stock_status'])
        
        return self.stock
    
    def can_be_ordered(self, quantity: int = 1) -> bool:
        """Check if can be ordered"""
        if not self.is_active:
            return False
        
        if quantity < self.min_order_quantity:
            return False
        
        if self.max_order_quantity and quantity > self.max_order_quantity:
            return False
        
        if not self.track_inventory:
            return True
        
        if self.stock >= quantity:
            return True
        
        return self.allow_backorder
    
    # ================================================================
    # ANALYTICS METHODS
    # ================================================================
    
    def increment_view_count(self, save: bool = True) -> None:
        """Increment view count"""
        self.view_count += 1
        self.last_viewed_at = timezone.now()
        
        if save:
            self.save(update_fields=['view_count', 'last_viewed_at'])
    
    def record_sale(self, quantity: int, amount: Decimal, save: bool = True) -> None:
        """Record a sale"""
        self.total_sales += quantity
        self.revenue_generated += amount
        
        if save:
            self.save(update_fields=['total_sales', 'revenue_generated'])
    
    def update_rating(self, save: bool = True) -> None:
        """Recalculate average rating"""
        # This would typically use the Review model
        # For now, placeholder
        if save:
            self.save(update_fields=['rating_average', 'review_count'])
    
    # ================================================================
    # STATUS METHODS
    # ================================================================
    
    def activate(self, save: bool = True) -> None:
        """Activate product"""
        self.is_active = True
        if save:
            self.save(update_fields=['is_active'])
    
    def deactivate(self, save: bool = True) -> None:
        """Deactivate product"""
        self.is_active = False
        if save:
            self.save(update_fields=['is_active'])
    
    def feature(self, save: bool = True) -> None:
        """Mark as featured"""
        self.is_featured = True
        if save:
            self.save(update_fields=['is_featured'])
    
    def unfeature(self, save: bool = True) -> None:
        """Remove featured"""
        self.is_featured = False
        if save:
            self.save(update_fields=['is_featured'])
    
    def verify(self, save: bool = True) -> None:
        """Verify product"""
        self.is_verified = True
        if save:
            self.save(update_fields=['is_verified'])
    
    # ================================================================
    # SOFT DELETE
    # ================================================================
    
    def soft_delete(
        self,
        deleted_by_user=None,
        reason: str = None,
        save: bool = True
    ) -> None:
        """Soft delete"""
        self.deleted_at = timezone.now()
        self.deleted_by = deleted_by_user
        self.deletion_reason = reason
        self.is_active = False
        
        if save:
            self.save()
    
    def restore(self, save: bool = True) -> None:
        """Restore deleted"""
        self.deleted_at = None
        self.deleted_by = None
        self.deletion_reason = None
        self.is_active = True
        
        if save:
            self.save()
    
    def delete(self, *args, **kwargs):
        """Override delete"""
        if kwargs.pop('hard_delete', False):
            super().delete(*args, **kwargs)
        else:
            self.soft_delete()
    
    # ================================================================
    # VALIDATION
    # ================================================================
    
    def clean(self) -> None:
        """Validate fields"""
        super().clean()
        
        # Validate pricing
        if self.buying_price is not None and self.selling_price < self.buying_price:
            raise ValidationError(
                _("Selling price cannot be less than buying price")
            )
        
        # Validate min/max order
        if self.max_order_quantity:
            if self.max_order_quantity < self.min_order_quantity:
                raise ValidationError(
                    _("Max order quantity cannot be less than min")
                )
        
        # Auto-generate SKU if not provided
        if not self.sku:
            import random
            import string
            self.sku = ''.join(random.choices(string.ascii_uppercase + string.digits, k=10))
    
    def save(self, *args, **kwargs):
        """Override save"""
        # Generate slug if needed
        if self.product_name and not self.slug:
            self.generate_slug()
        
        # Calculate final price
        self.calculate_final_price()
        
        # Update stock status
        if self.track_inventory:
            self.update_stock_status(save=False)
        
        # Run validation
        self.full_clean()
        
        super().save(*args, **kwargs)


"""
User Activity Tracking Models
------------------------------
Tracks:
- Recently viewed products
- Wishlisted products
- Purchase / order history
- Search history
"""

import uuid

from django.db import models
from django.conf import settings
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from django.db.models import Q


# ====================================================================
# 1. PRODUCT VIEW (recently viewed)
# ====================================================================

class ProductView(models.Model):
    """
    Records every time a logged-in user views a product.
    Duplicate visits update `viewed_at` instead of creating new rows.
    """

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='product_views',
        help_text=_("User who viewed the product")
    )

    product = models.ForeignKey(
        'ponno.Product',
        on_delete=models.CASCADE,
        related_name='user_views',
        help_text=_("Product that was viewed")
    )

    viewed_at = models.DateTimeField(
        _("Viewed At"),
        default=timezone.now,
        db_index=True,
        help_text=_("Last time the user viewed this product")
    )

    view_count = models.PositiveIntegerField(
        _("View Count"),
        default=1,
        help_text=_("How many times this user viewed this product")
    )

    class Meta:
        verbose_name = _("Product View")
        verbose_name_plural = _("Product Views")
        db_table = 'user_product_views'
        unique_together = [('user', 'product')]
        ordering = ['-viewed_at']
        indexes = [
            models.Index(fields=['user', 'viewed_at']),
        ]

    def __str__(self):
        return f"{self.user} viewed {self.product.product_name}"

    @classmethod
    def record(cls, user, product):
        """
        Upsert a view record — call this from the product detail view.
        Returns the ProductView instance.
        """
        obj, created = cls.objects.get_or_create(
            user=user,
            product=product,
            defaults={'viewed_at': timezone.now(), 'view_count': 1}
        )
        if not created:
            obj.viewed_at = timezone.now()
            obj.view_count += 1
            obj.save(update_fields=['viewed_at', 'view_count'])
        return obj


# ====================================================================
# 2. WISHLIST
# ====================================================================

class Wishlist(models.Model):
    """
    Stores products a user has saved / wishlisted.
    One row per (user, product) pair.
    """

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='wishlist_items',
        help_text=_("User who wishlisted the product")
    )

    product = models.ForeignKey(
        'ponno.Product',
        on_delete=models.CASCADE,
        related_name='wishlisted_by',
        help_text=_("Wishlisted product")
    )

    added_at = models.DateTimeField(
        _("Added At"),
        auto_now_add=True,
        db_index=True,
        help_text=_("When the product was added to the wishlist")
    )

    note = models.CharField(
        _("Note"),
        max_length=255,
        blank=True,
        null=True,
        help_text=_("Optional personal note")
    )

    class Meta:
        verbose_name = _("Wishlist Item")
        verbose_name_plural = _("Wishlist Items")
        db_table = 'user_wishlist'
        unique_together = [('user', 'product')]
        ordering = ['-added_at']
        indexes = [
            models.Index(fields=['user', 'added_at']),
        ]

    def __str__(self):
        return f"{self.user} wishlisted {self.product.product_name}"

    @classmethod
    def toggle(cls, user, product):
        """
        Add if not wishlisted, remove if already wishlisted.
        Returns (instance_or_None, added: bool).
        """
        obj = cls.objects.filter(user=user, product=product).first()
        if obj:
            obj.delete()
            # Decrement cached count on Product
            product.__class__.objects.filter(pk=product.pk).update(
                wishlist_count=models.F('wishlist_count') - 1
            )
            return None, False
        else:
            instance = cls.objects.create(user=user, product=product)
            product.__class__.objects.filter(pk=product.pk).update(
                wishlist_count=models.F('wishlist_count') + 1
            )
            return instance, True


# ====================================================================
# 3. ORDER & ORDER ITEM (purchase history)
# ====================================================================

class Order(models.Model):
    """
    Represents a purchase made by a user.
    """

    class OrderStatus(models.TextChoices):
        PENDING    = 'pending',    _('Pending')
        CONFIRMED  = 'confirmed',  _('Confirmed')
        PROCESSING = 'processing', _('Processing')
        SHIPPED    = 'shipped',    _('Shipped')
        DELIVERED  = 'delivered',  _('Delivered')
        CANCELLED  = 'cancelled',  _('Cancelled')
        REFUNDED   = 'refunded',   _('Refunded')

    class PaymentStatus(models.TextChoices):
        UNPAID  = 'unpaid',  _('Unpaid')
        PAID    = 'paid',    _('Paid')
        PARTIAL = 'partial', _('Partial')
        REFUNDED = 'refunded', _('Refunded')

    # ── Identity ──────────────────────────────────────────────────
    uuid = models.UUIDField(
        default=uuid.uuid4,
        editable=False,
        unique=True,
        db_index=True,
        help_text=_("Public-facing order identifier")
    )

    order_number = models.CharField(
        _("Order Number"),
        max_length=30,
        unique=True,
        db_index=True,
        help_text=_("Human-readable order number, e.g. ORD-20240516-0001")
    )

    # ── Relationships ─────────────────────────────────────────────
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        related_name='orders',
        help_text=_("Customer who placed the order")
    )

    # ── Status ────────────────────────────────────────────────────
    status = models.CharField(
        _("Order Status"),
        max_length=20,
        choices=OrderStatus.choices,
        default=OrderStatus.PENDING,
        db_index=True,
    )

    payment_status = models.CharField(
        _("Payment Status"),
        max_length=20,
        choices=PaymentStatus.choices,
        default=PaymentStatus.UNPAID,
        db_index=True,
    )

    # ── Pricing ───────────────────────────────────────────────────
    subtotal = models.DecimalField(
        _("Subtotal"),
        max_digits=12, decimal_places=2, default=0
    )

    discount_total = models.DecimalField(
        _("Discount Total"),
        max_digits=12, decimal_places=2, default=0
    )

    shipping_total = models.DecimalField(
        _("Shipping Total"),
        max_digits=12, decimal_places=2, default=0
    )

    grand_total = models.DecimalField(
        _("Grand Total"),
        max_digits=12, decimal_places=2, default=0
    )

    currency = models.CharField(
        _("Currency"), max_length=3, default='BDT'
    )

    # ── Shipping address (snapshot) ───────────────────────────────
    shipping_name    = models.CharField(_("Name"),    max_length=150, blank=True)
    shipping_phone   = models.CharField(_("Phone"),   max_length=20,  blank=True)
    shipping_address = models.TextField(_("Address"),                 blank=True)
    shipping_city    = models.CharField(_("City"),    max_length=100, blank=True)
    shipping_country = models.CharField(_("Country"), max_length=2,   blank=True)

    # ── Timestamps ────────────────────────────────────────────────
    placed_at = models.DateTimeField(
        _("Placed At"), auto_now_add=True, db_index=True
    )
    updated_at = models.DateTimeField(_("Updated At"), auto_now=True)
    delivered_at = models.DateTimeField(
        _("Delivered At"), null=True, blank=True
    )

    notes = models.TextField(_("Notes"), blank=True, null=True)

    class Meta:
        verbose_name = _("Order")
        verbose_name_plural = _("Orders")
        db_table = 'orders'
        ordering = ['-placed_at']
        indexes = [
            models.Index(fields=['user', 'status']),
            models.Index(fields=['placed_at']),
        ]

    def __str__(self):
        return self.order_number

    def generate_order_number(self):
        from django.utils import timezone as tz
        import random
        today = tz.now().strftime('%Y%m%d')
        rand  = random.randint(1000, 9999)
        return f"ORD-{today}-{rand}"

    def save(self, *args, **kwargs):
        if not self.order_number:
            self.order_number = self.generate_order_number()
        super().save(*args, **kwargs)


class OrderItem(models.Model):
    """
    Line item inside an order — snapshot of price at purchase time.
    """

    order = models.ForeignKey(
        Order,
        on_delete=models.CASCADE,
        related_name='items',
        help_text=_("Parent order")
    )

    product = models.ForeignKey(
        'ponno.Product',
        on_delete=models.SET_NULL,
        null=True,
        related_name='order_items',
        help_text=_("Ordered product")
    )

    # Snapshot fields (so history survives product edits)
    product_name  = models.CharField(_("Product Name"),  max_length=200)
    product_sku   = models.CharField(_("SKU"),           max_length=100, blank=True)
    product_image = models.URLField( _("Image URL"),     max_length=500, blank=True)

    quantity      = models.PositiveIntegerField(_("Quantity"), default=1)
    unit_price    = models.DecimalField(_("Unit Price"), max_digits=12, decimal_places=2)
    discount_pct  = models.DecimalField(_("Discount %"), max_digits=5,  decimal_places=2, default=0)
    line_total    = models.DecimalField(_("Line Total"), max_digits=12, decimal_places=2)

    class Meta:
        verbose_name = _("Order Item")
        verbose_name_plural = _("Order Items")
        db_table = 'order_items'
        ordering = ['id']

    def __str__(self):
        return f"{self.quantity}× {self.product_name} in {self.order.order_number}"

    def save(self, *args, **kwargs):
        # Auto-fill snapshot fields from product on first save
        if self.product and not self.product_name:
            self.product_name  = self.product.product_name
            self.product_sku   = self.product.sku or ''
            self.product_image = self.product.image_url
        # Calculate line total
        from decimal import Decimal
        discount = (self.unit_price * self.discount_pct) / Decimal('100')
        self.line_total = (self.unit_price - discount) * self.quantity
        super().save(*args, **kwargs)


# ====================================================================
# 4. SEARCH HISTORY
# ====================================================================

class SearchHistory(models.Model):
    """
    Logs every search query made by a logged-in user.
    Duplicate queries within 5 minutes are collapsed into one record.
    """

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='search_history',
        help_text=_("User who performed the search")
    )

    query = models.CharField(
        _("Search Query"),
        max_length=200,
        db_index=True,
        help_text=_("The search term entered by the user")
    )

    result_count = models.PositiveIntegerField(
        _("Result Count"),
        default=0,
        help_text=_("Number of products returned for this query")
    )

    searched_at = models.DateTimeField(
        _("Searched At"),
        default=timezone.now,
        db_index=True,
    )

    search_count = models.PositiveIntegerField(
        _("Search Count"),
        default=1,
        help_text=_("How many times this query was repeated")
    )

    class Meta:
        verbose_name = _("Search History")
        verbose_name_plural = _("Search Histories")
        db_table = 'user_search_history'
        ordering = ['-searched_at']
        indexes = [
            models.Index(fields=['user', 'searched_at']),
            models.Index(fields=['user', 'query']),
        ]

    def __str__(self):
        return f"{self.user} searched '{self.query}'"

    @classmethod
    def record(cls, user, query, result_count=0):
        """
        Upsert a search record.
        Collapses repeated identical queries within 5 minutes.
        """
        from datetime import timedelta
        if not query or not query.strip():
            return None

        query = query.strip()[:200]
        five_min_ago = timezone.now() - timedelta(minutes=5)

        recent = cls.objects.filter(
            user=user,
            query__iexact=query,
            searched_at__gte=five_min_ago
        ).first()

        if recent:
            recent.searched_at  = timezone.now()
            recent.result_count = result_count
            recent.search_count += 1
            recent.save(update_fields=['searched_at', 'result_count', 'search_count'])
            return recent

        return cls.objects.create(
            user=user,
            query=query,
            result_count=result_count,
        )