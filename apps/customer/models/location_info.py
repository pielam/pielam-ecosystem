# apps/customer/models/location_info.py

"""
LocationInfo -- holds the location/address data that used to live
directly on ProfileInfo (profile_country, profile_state, profile_city,
profile_address, profile_postal_code, profile_latitude,
profile_longitude), before those fields were removed from that model.

WHY A SEPARATE MODEL INSTEAD OF PUTTING IT BACK ON ProfileInfo
------------------------------------------------------------------
ProfileInfo was deliberately trimmed down to core identity/social data
(name, bio, photos, followers). Location is optional, sparse (most
users may never fill in an address), and privacy-sensitive on its own
axis -- it deserves its own table rather than bloating every
ProfileInfo row with mostly-null columns again.

WHY THIS IS NOT AUTO-CREATED LIKE ProfileInfo IS
------------------------------------------------------------------
ProfileInfo is foundational identity data, so signals.py creates one
for every new User automatically. Location data is different: many
users will never provide one, so a row is only created lazily, the
same way cover_photo.py / profile_photo.py lazily get_or_create() a
ProfileInfo on first use. Use LocationInfo.objects.get_or_create_for_user(user)
for that -- there is deliberately no post_save signal on User creating
one of these.

RELATIONSHIP TO ProfileInfo.show_location
------------------------------------------------------------------
ProfileInfo still has a `show_location` privacy toggle ("Display city/
country on profile"), which became a no-op once the location fields
were removed from that model. It was intentionally left alone at the
time (removing a field nobody asked to remove felt like overreach).
Now that the data has a home again here, that toggle is meaningful
again -- a view rendering `location.location_display` should still
gate it behind `profile_info.show_location`, since that flag was never
moved or duplicated onto this model.

NOT YET WIRED: GDPR / soft-delete cascade
------------------------------------------------------------------
User.soft_delete() (account.py) anonymizes email/phone and, via
signals.py's sync_profile_archive_state(), archives the user's
ProfileInfo too. It does NOT currently know about LocationInfo. An
address/coordinates pair is meaningfully sensitive PII, so if you want
"delete my account" to also scrub this, that needs an explicit signal
addition in signals.py (or a call from soft_delete() itself) -- not
included here since this file is scoped to the model itself.
"""

from decimal import Decimal
from typing import Optional

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
import uuid


# ====================================================================
# MANAGER
# ====================================================================

class LocationInfoManager(models.Manager):

    def for_user(self, user):
        """Get the LocationInfo row for a user, or None if none exists yet."""
        return self.filter(user=user).first()

    def get_or_create_for_user(self, user, **defaults):
        """
        Lazily create a LocationInfo for `user` on first use -- the
        intended entry point for views, mirroring the
        ProfileInfo.objects.get_or_create(user=...) pattern already
        used in cover_photo.py / profile_photo.py / campaign_profile.py.
        """
        obj, created = self.get_or_create(user=user, defaults=defaults)
        return obj, created

    def with_coordinates(self):
        """Rows that actually have a lat/long pair set -- useful for map/geo features."""
        return self.filter(latitude__isnull=False, longitude__isnull=False)


# ====================================================================
# MODEL
# ====================================================================

class LocationInfo(models.Model):
    """
    A single address/location record for a user. One-to-one with User
    for now (mirrors ProfileInfo's shape) -- if multiple saved
    addresses (home/work/shipping/billing) are ever needed, this is
    the model to convert to a ForeignKey + a `label`/`is_default` pair
    rather than bolting that onto ProfileInfo.
    """

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        primary_key=True,
        related_name="location_info",
        help_text=_("User this location belongs to"),
    )

    uuid = models.UUIDField(
        default=uuid.uuid4,
        editable=False,
        unique=True,
        db_index=True,
        help_text=_("Unique identifier for external API references"),
    )

    # Free-text full name (e.g. "Bangladesh", "Japan", "England"), not
    # an ISO alpha-2 code -- matching the same decision already made
    # for User.country in account.py, for consistency across the app.
    country = models.CharField(
        _("Country"),
        max_length=100,
        blank=True,
        null=True,
        help_text=_("Country name, e.g. 'Bangladesh', 'Japan', 'England'"),
    )

    state = models.CharField(
        _("State/Province"),
        max_length=100,
        blank=True,
        null=True,
        help_text=_("State, province, or region"),
    )

    city = models.CharField(
        _("City"),
        max_length=100,
        blank=True,
        null=True,
        help_text=_("City"),
    )

    address = models.TextField(
        _("Address"),
        max_length=255,
        blank=True,
        null=True,
        help_text=_("Full street address"),
    )

    postal_code = models.CharField(
        _("Postal Code"),
        max_length=20,
        blank=True,
        null=True,
        help_text=_("Postal or ZIP code"),
    )

    latitude = models.DecimalField(
        _("Latitude"),
        max_digits=9,
        decimal_places=6,
        blank=True,
        null=True,
        help_text=_("GPS latitude coordinate (-90 to 90)"),
    )

    longitude = models.DecimalField(
        _("Longitude"),
        max_digits=9,
        decimal_places=6,
        blank=True,
        null=True,
        help_text=_("GPS longitude coordinate (-180 to 180)"),
    )

    created_at = models.DateTimeField(_("Created At"), auto_now_add=True)
    updated_at = models.DateTimeField(_("Updated At"), auto_now=True)

    objects = LocationInfoManager()

    class Meta:
        verbose_name = _("Location Info")
        verbose_name_plural = _("Location Infos")
        db_table = "location_info"
        indexes = [
            models.Index(fields=["uuid"]),
            models.Index(fields=["country", "city"]),
        ]

    def __str__(self) -> str:
        return self.location_display or str(self.user)

    def __repr__(self) -> str:
        return f"<LocationInfo: {self.user} ({self.location_display or 'no location set'})>"

    # ================================================================
    # PROPERTIES
    # ================================================================

    @property
    def location_display(self) -> str:
        """Formatted 'City, State, Country' string, skipping empty parts."""
        parts = [p for p in (self.city, self.state, self.country) if p]
        return ", ".join(parts)

    @property
    def has_coordinates(self) -> bool:
        return self.latitude is not None and self.longitude is not None

    @property
    def coordinates(self) -> Optional[tuple]:
        """(latitude, longitude) tuple, or None if either is unset."""
        if self.has_coordinates:
            return (self.latitude, self.longitude)
        return None

    # ================================================================
    # DATA EXPORT (GDPR)
    # ================================================================

    def export_data(self) -> dict:
        """Export location data for GDPR compliance -- same shape/spirit as User.export_data() and ProfileInfo.export_data()."""
        return {
            "uuid": str(self.uuid),
            "country": self.country,
            "state": self.state,
            "city": self.city,
            "address": self.address,
            "postal_code": self.postal_code,
            "latitude": float(self.latitude) if self.latitude is not None else None,
            "longitude": float(self.longitude) if self.longitude is not None else None,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
        }

    # ================================================================
    # VALIDATION
    # ================================================================

    def clean(self) -> None:
        super().clean()

        if self.latitude is not None and not (Decimal("-90") <= self.latitude <= Decimal("90")):
            raise ValidationError(_("Latitude must be between -90 and 90"))

        if self.longitude is not None and not (Decimal("-180") <= self.longitude <= Decimal("180")):
            raise ValidationError(_("Longitude must be between -180 and 180"))

        # Coordinates are meaningful as a pair -- one without the other
        # is almost always a data-entry mistake rather than intentional.
        if (self.latitude is None) != (self.longitude is None):
            raise ValidationError(
                _("Latitude and longitude must both be set, or both left empty")
            )

    def save(self, *args, **kwargs):
        self.full_clean()
        super().save(*args, **kwargs)