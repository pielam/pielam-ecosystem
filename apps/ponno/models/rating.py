# apps/ponno/models/rating.py

from django.db import models
from django.conf import settings
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from django.core.validators import MinValueValidator, MaxValueValidator


class ProductRating(models.Model):
    """
    One rating per (user, product) pair.
    update_or_create pattern means a user can change
    their rating but never double-rate.
    """

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='product_ratings',
        help_text=_("User who rated the product"),
    )

    product = models.ForeignKey(
        'ponno.Product',
        on_delete=models.CASCADE,
        related_name='ratings',
        help_text=_("Rated product"),
    )

    rating = models.PositiveSmallIntegerField(
        _("Rating"),
        validators=[MinValueValidator(1), MaxValueValidator(5)],
        help_text=_("1–5 star rating"),
    )

    rated_at = models.DateTimeField(
        _("Rated At"),
        default=timezone.now,
        db_index=True,
    )

    class Meta:
        verbose_name        = _("Product Rating")
        verbose_name_plural = _("Product Ratings")
        db_table            = 'product_ratings'
        unique_together     = [('user', 'product')]
        ordering            = ['-rated_at']
        indexes             = [
            models.Index(fields=['product', 'rating']),
            models.Index(fields=['user', 'rated_at']),
        ]

    def __str__(self):
        return f"{self.user} rated {self.product.product_name} → {self.rating}★"