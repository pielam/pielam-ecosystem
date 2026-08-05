# apps/ponno/models/product_view_log.py

"""
ProductViewLog Model
--------------------
Tracks every product page view for analytics purposes.

Features:
- Authenticated and anonymous view tracking
- One row per (product, viewer) pair — updated on revisit
- IP address and user-agent capture
- Session-based anonymous tracking
- Source / referrer tracking
- Device type classification
- Soft delete
- Manager with common query helpers
"""

import uuid

from django.db import models
from django.conf import settings
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from django.db.models import Q, Count

from apps.ponno.models.product import Product


# ====================================================================
# PRODUCT VIEW LOG MANAGER
# ====================================================================

class ProductViewLogManager(models.Manager):
    """
    Custom manager for ProductViewLog with optimized queries
    """

    def for_product(self, product):
        """Get all view logs for a specific product"""
        return self.filter(
            product=product,
            deleted_at__isnull=True
        ).order_by('-viewed_at')

    def for_user(self, user):
        """Get all view logs for a specific authenticated user"""
        return self.filter(
            viewer=user,
            deleted_at__isnull=True
        ).order_by('-viewed_at').select_related('product')

    def anonymous_views(self):
        """Get view logs from anonymous (unauthenticated) visitors"""
        return self.filter(
            viewer__isnull=True,
            deleted_at__isnull=True
        )

    def authenticated_views(self):
        """Get view logs from authenticated users only"""
        return self.filter(
            viewer__isnull=False,
            deleted_at__isnull=True
        )

    def recent(self, days=30):
        """Get view logs from the last N days"""
        threshold = timezone.now() - timezone.timedelta(days=days)
        return self.filter(
            viewed_at__gte=threshold,
            deleted_at__isnull=True
        )

    def top_products(self, limit=10, days=None):
        """
        Get products ranked by unique viewer count.
        Optionally restrict to the last N days.
        """
        qs = self.filter(deleted_at__isnull=True)
        if days:
            threshold = timezone.now() - timezone.timedelta(days=days)
            qs = qs.filter(viewed_at__gte=threshold)
        return (
            qs.values('product')
              .annotate(unique_viewers=Count('id'))
              .order_by('-unique_viewers')[:limit]
        )

    def by_device(self, device_type):
        """Filter view logs by device type"""
        return self.filter(
            device_type=device_type,
            deleted_at__isnull=True
        )

    def by_source(self, source):
        """Filter view logs by traffic source"""
        return self.filter(
            source=source,
            deleted_at__isnull=True
        )

    def record(self, product, viewer=None, **kwargs):
        """
        Upsert a view log.

        - Authenticated users: one row per (product, viewer) — updated on revisit.
        - Anonymous visitors: always creates a new row (viewer=None bypasses
          the unique constraint, so every anonymous hit is its own record).

        Returns (instance, created).
        """
        if viewer is not None:
            obj, created = self.get_or_create(
                product=product,
                viewer=viewer,
                defaults={
                    'viewed_at': timezone.now(),
                    'view_count': 1,
                    **kwargs,
                }
            )
            if not created:
                obj.viewed_at = timezone.now()
                obj.view_count += 1
                # Update mutable fields passed in on revisit
                for field, value in kwargs.items():
                    setattr(obj, field, value)
                update_fields = ['viewed_at', 'view_count'] + list(kwargs.keys())
                obj.save(update_fields=update_fields)
            return obj, created
        else:
            # Anonymous hit — always a new row
            return self.create(
                product=product,
                viewer=None,
                viewed_at=timezone.now(),
                view_count=1,
                **kwargs,
            ), True


# ====================================================================
# PRODUCT VIEW LOG MODEL
# ====================================================================

class ProductViewLog(models.Model):
    """
    ProductViewLog Model

    Records every product page view. For authenticated users one row
    is kept per (product, viewer) pair and updated on each revisit.
    Anonymous views always create a new row.

    Features:
    - Authenticated and anonymous tracking
    - IP address and user-agent capture
    - Session key for anonymous visitor correlation
    - Referrer / UTM source tracking
    - Device type classification
    - View counter per (product, viewer) pair
    - Soft delete
    """

    # ================================================================
    # CHOICES
    # ================================================================

    class DeviceType(models.TextChoices):
        DESKTOP = 'desktop', _('Desktop')
        MOBILE  = 'mobile',  _('Mobile')
        TABLET  = 'tablet',  _('Tablet')
        BOT     = 'bot',     _('Bot / Crawler')
        UNKNOWN = 'unknown', _('Unknown')

    class Source(models.TextChoices):
        DIRECT     = 'direct',     _('Direct')
        SEARCH     = 'search',     _('Search Engine')
        SOCIAL     = 'social',     _('Social Media')
        EMAIL      = 'email',      _('Email')
        REFERRAL   = 'referral',   _('Referral')
        AD         = 'ad',         _('Advertisement')
        INTERNAL   = 'internal',   _('Internal Navigation')
        UNKNOWN    = 'unknown',    _('Unknown')

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

    # ================================================================
    # RELATIONSHIPS
    # ================================================================

    product = models.ForeignKey(
        Product,
        on_delete=models.CASCADE,
        related_name='view_logs',
        help_text=_("Product that was viewed")
    )

    viewer = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='product_view_logs',
        help_text=_("Authenticated user who viewed the product (null = anonymous)")
    )

    # ================================================================
    # SESSION / ANONYMOUS TRACKING
    # ================================================================

    session_key = models.CharField(
        _("Session Key"),
        max_length=40,
        null=True,
        blank=True,
        db_index=True,
        help_text=_("Django session key — used to correlate anonymous views")
    )

    # ================================================================
    # VIEW METADATA
    # ================================================================

    viewed_at = models.DateTimeField(
        _("Viewed At"),
        default=timezone.now,
        db_index=True,
        help_text=_("Timestamp of the most recent view")
    )

    view_count = models.PositiveIntegerField(
        _("View Count"),
        default=1,
        help_text=_("How many times this viewer has viewed this product")
    )

    # ================================================================
    # REQUEST METADATA
    # ================================================================

    ip_address = models.GenericIPAddressField(
        _("IP Address"),
        null=True,
        blank=True,
        help_text=_("Visitor IP address at time of view")
    )

    user_agent = models.TextField(
        _("User Agent"),
        null=True,
        blank=True,
        help_text=_("Browser / client user-agent string")
    )

    # ================================================================
    # DEVICE & SOURCE CLASSIFICATION
    # ================================================================

    device_type = models.CharField(
        _("Device Type"),
        max_length=10,
        choices=DeviceType.choices,
        default=DeviceType.UNKNOWN,
        db_index=True,
        help_text=_("Type of device used to view the product")
    )

    source = models.CharField(
        _("Traffic Source"),
        max_length=20,
        choices=Source.choices,
        default=Source.UNKNOWN,
        db_index=True,
        help_text=_("How the visitor arrived at the product page")
    )

    referrer_url = models.URLField(
        _("Referrer URL"),
        max_length=500,
        null=True,
        blank=True,
        help_text=_("Full URL of the page that linked to this product")
    )

    # ================================================================
    # UTM / CAMPAIGN TRACKING
    # ================================================================

    utm_source = models.CharField(
        _("UTM Source"),
        max_length=100,
        null=True,
        blank=True,
        help_text=_("UTM source parameter (e.g. 'google', 'newsletter')")
    )

    utm_medium = models.CharField(
        _("UTM Medium"),
        max_length=100,
        null=True,
        blank=True,
        help_text=_("UTM medium parameter (e.g. 'cpc', 'email')")
    )

    utm_campaign = models.CharField(
        _("UTM Campaign"),
        max_length=100,
        null=True,
        blank=True,
        help_text=_("UTM campaign parameter")
    )

    # ================================================================
    # SOFT DELETE
    # ================================================================

    deleted_at = models.DateTimeField(
        _("Deleted At"),
        null=True,
        blank=True,
        db_index=True,
        help_text=_("Soft delete timestamp")
    )

    deleted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='deleted_product_view_logs',
        help_text=_("User who deleted this log entry")
    )

    # ================================================================
    # METADATA
    # ================================================================

    metadata = models.JSONField(
        _("Metadata"),
        default=dict,
        blank=True,
        help_text=_("Additional view log metadata in JSON format")
    )

    # ================================================================
    # MANAGER
    # ================================================================

    objects = ProductViewLogManager()

    # ================================================================
    # META
    # ================================================================

    class Meta:
        verbose_name = _("Product View Log")
        verbose_name_plural = _("Product View Logs")
        db_table = 'product_view_logs'
        ordering = ['-viewed_at']

        # One row per authenticated viewer per product.
        # Anonymous rows (viewer=None) are excluded from this constraint.
        constraints = [
            models.UniqueConstraint(
                fields=['product', 'viewer'],
                condition=Q(viewer__isnull=False),
                name='unique_product_view_per_viewer',
            )
        ]

        indexes = [
            models.Index(fields=['uuid']),
            models.Index(fields=['product', 'viewed_at']),
            models.Index(fields=['viewer', 'viewed_at']),
            models.Index(fields=['session_key']),
            models.Index(fields=['ip_address']),
            models.Index(fields=['device_type']),
            models.Index(fields=['source']),
            models.Index(fields=['viewed_at']),
            models.Index(fields=['deleted_at']),
        ]

    # ================================================================
    # STRING REPRESENTATION
    # ================================================================

    def __str__(self) -> str:
        viewer = self.viewer.email_or_phone if self.viewer else f"anon({self.ip_address or self.session_key or '?'})"
        return f"{viewer} → {self.product.product_name} @ {self.viewed_at:%Y-%m-%d %H:%M}"

    def __repr__(self) -> str:
        return f"<ProductViewLog: product={self.product_id} viewer={self.viewer_id} at={self.viewed_at}>"

    # ================================================================
    # PROPERTIES
    # ================================================================

    @property
    def is_anonymous(self) -> bool:
        """Check if this is an anonymous (unauthenticated) view"""
        return self.viewer is None

    @property
    def is_authenticated_view(self) -> bool:
        """Check if this view was made by an authenticated user"""
        return self.viewer is not None

    @property
    def has_utm(self) -> bool:
        """Check if UTM tracking parameters are present"""
        return any([self.utm_source, self.utm_medium, self.utm_campaign])

    @property
    def is_deleted(self) -> bool:
        """Check if this log entry has been soft deleted"""
        return self.deleted_at is not None

    # ================================================================
    # SOFT DELETE METHODS
    # ================================================================

    def soft_delete(self, deleted_by_user=None, save: bool = True) -> None:
        """Soft delete this log entry"""
        self.deleted_at = timezone.now()
        self.deleted_by = deleted_by_user

        if save:
            self.save(update_fields=['deleted_at', 'deleted_by'])

    def restore(self, save: bool = True) -> None:
        """Restore a soft-deleted log entry"""
        self.deleted_at = None
        self.deleted_by = None

        if save:
            self.save(update_fields=['deleted_at', 'deleted_by'])

    def delete(self, *args, **kwargs):
        """Override delete to use soft delete by default"""
        if kwargs.pop('hard_delete', False):
            super().delete(*args, **kwargs)
        else:
            self.soft_delete()

    # ================================================================
    # DATA EXPORT (GDPR)
    # ================================================================

    def export_data(self) -> dict:
        """Export view log data for GDPR compliance"""
        return {
            'uuid': str(self.uuid),
            'product': {
                'uuid': str(self.product.product_id),
                'name': self.product.product_name,
                'slug': self.product.slug,
            },
            'viewer': self.viewer.email_or_phone if self.viewer else None,
            'viewed_at': self.viewed_at.isoformat(),
            'view_count': self.view_count,
            'device_type': self.device_type,
            'source': self.source,
            'referrer_url': self.referrer_url,
            'utm': {
                'source': self.utm_source,
                'medium': self.utm_medium,
                'campaign': self.utm_campaign,
            },
            'metadata': self.metadata,
        }