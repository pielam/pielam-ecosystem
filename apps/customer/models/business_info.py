# apps/customer/models/business_info.py

"""
BusinessInfo -- holds the business/dealer data that used to live
directly on ProfileInfo (business_name, business_type,
business_registration, business_tax_id, business_website,
business_email, business_phone, business_description), before those
fields were removed from that model.

WHY A SEPARATE MODEL INSTEAD OF PUTTING IT BACK ON ProfileInfo
------------------------------------------------------------------
Same reasoning as LocationInfo and SocialInfo (see their docstrings):
ProfileInfo was deliberately trimmed to core identity/social-graph
data. Business data is only relevant to the subset of users who are
dealers/businesses (User.Role.BUSINESS in account.py), so it doesn't
belong on every ProfileInfo row -- it gets its own table instead.

WHY THIS IS NOT AUTO-CREATED LIKE ProfileInfo IS
------------------------------------------------------------------
Same lazy-creation pattern as LocationInfo/SocialInfo: most users are
never businesses, so there is deliberately no post_save signal on
User creating one of these. Use
BusinessInfo.objects.get_or_create_for_user(user) instead, the same
way LocationInfo/SocialInfo/cover_photo.py/profile_photo.py lazily
get_or_create() their respective rows on first use.

RELATIONSHIP TO User.role / User.is_dealer
------------------------------------------------------------------
account.py already has User.Role.BUSINESS and a User.is_dealer
property, but nothing currently keys off whether a BusinessInfo row
exists. This file does NOT attempt to auto-sync those two things (e.g.
setting role=BUSINESS whenever a BusinessInfo row is created, or vice
versa) -- that's a cross-cutting decision (probably a signal, mirroring
signals.py's sync_profile_archive_state()) that belongs in signals.py,
not silently bolted on here. Until that's wired up, it's possible for
a user to have role=BUSINESS with no BusinessInfo row, or a
BusinessInfo row while role=USER -- callers that care should check
both explicitly rather than assuming one implies the other.

RELATIONSHIP TO LocationInfo / ContactInfo
------------------------------------------------------------------
The original business_website/business_email/business_phone fields
overlapped conceptually with what ContactInfo now covers, and a
business address overlaps with LocationInfo. This model does NOT
duplicate those -- business_email/business_phone/business_website
are kept here as business-specific contact points (a dealer's
storefront email is conceptually different from the user's own
alt_email/phone_number in ContactInfo), but a business's physical
address should use LocationInfo rather than a new business_address
field here. If a business ever needs an address distinct from the
user's own LocationInfo row, that's a reason to give LocationInfo a
ForeignKey + label (see LocationInfo's own docstring on multiple
addresses) rather than adding a parallel address block here.

NOT YET WIRED: GDPR / soft-delete cascade
------------------------------------------------------------------
Same gap as LocationInfo/SocialInfo: User.soft_delete() (account.py)
and signals.py's sync_profile_archive_state() don't currently know
about BusinessInfo. Business registration/tax IDs are sensitive, so if
"delete my account" should also scrub this, that needs an explicit
addition in signals.py (or a call from soft_delete() itself) -- not
included here since this file is scoped to the model itself.
"""

import uuid
from typing import Optional
from urllib.parse import urlparse

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _


# ====================================================================
# BUSINESS INFO QUERYSET
# ====================================================================

class BusinessInfoQuerySet(models.QuerySet):
    """
    Soft-delete guarantee at the queryset level, same rationale as
    ProfileInfoQuerySet/ContactInfoQuerySet: a bulk
    `BusinessInfo.objects.filter(...).delete()` should archive every
    row rather than bypassing soft_delete().
    """

    def delete(self):
        count = 0
        for obj in self:
            obj.soft_delete()
            count += 1
        return count, {self.model._meta.label: count}

    def hard_delete(self):
        """Escape hatch for an actual, irreversible bulk delete."""
        return super().delete()

    def verified(self):
        return self.filter(is_business_verified=True, is_business_archived=False)

    def active(self):
        return self.filter(is_business_archived=False)


# ====================================================================
# BUSINESS INFO MANAGER
# ====================================================================

class BusinessInfoManager(models.Manager.from_queryset(BusinessInfoQuerySet)):
    """Custom manager for BusinessInfo with the same lazy-creation entry points as LocationInfo/SocialInfo."""

    def for_user(self, user):
        """Get the BusinessInfo row for a user, or None if none exists yet."""
        return self.filter(user=user).first()

    def get_or_create_for_user(self, user, **defaults):
        """
        Lazily create a BusinessInfo for `user` on first use --
        mirrors LocationInfo.objects.get_or_create_for_user() and
        SocialInfo.objects.get_or_create_for_user(). There is
        deliberately no signal creating this automatically, since most
        users will never need one.
        """
        obj, created = self.get_or_create(user=user, defaults=defaults)
        return obj, created

    def search(self, term):
        """Search businesses by name, registration number, or description."""
        return self.filter(
            models.Q(business_name__icontains=term)
            | models.Q(registration_number__icontains=term)
            | models.Q(description__icontains=term),
            is_business_archived=False,
        )


# ====================================================================
# BUSINESS INFO MODEL
# ====================================================================

class BusinessInfo(models.Model):
    """
    A single business/dealer profile for a user. One-to-one with User
    for now (mirrors LocationInfo's and SocialInfo's shape) -- if a
    single user ever needs to represent multiple businesses, this is
    the model to convert to a ForeignKey rather than bolting that onto
    ProfileInfo.
    """

    # ================================================================
    # CHOICES
    # ================================================================

    class BusinessType(models.TextChoices):
        SOLE_PROPRIETORSHIP = 'sole_proprietorship', _('Sole Proprietorship')
        PARTNERSHIP = 'partnership', _('Partnership')
        LLC = 'llc', _('Limited Liability Company')
        CORPORATION = 'corporation', _('Corporation')
        NONPROFIT = 'nonprofit', _('Nonprofit')
        OTHER = 'other', _('Other')

    # ================================================================
    # PRIMARY FIELDS
    # ================================================================

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        primary_key=True,
        related_name="business_info",
        help_text=_("User this business belongs to"),
    )

    uuid = models.UUIDField(
        default=uuid.uuid4,
        editable=False,
        unique=True,
        db_index=True,
        help_text=_("Unique identifier for external API references"),
    )

    # ================================================================
    # CORE BUSINESS DETAILS
    # ================================================================

    business_name = models.CharField(
        _("Business Name"),
        max_length=200,
        help_text=_("Trading/display name of the business"),
    )

    legal_name = models.CharField(
        _("Legal Name"),
        max_length=200,
        blank=True,
        null=True,
        help_text=_("Official registered name, if different from business_name"),
    )

    business_type = models.CharField(
        _("Business Type"),
        max_length=30,
        choices=BusinessType.choices,
        default=BusinessType.OTHER,
        help_text=_("Legal structure of the business"),
    )

    industry = models.CharField(
        _("Industry"),
        max_length=100,
        blank=True,
        null=True,
        help_text=_("Industry or sector, e.g. 'Robotics & Automation'"),
    )

    description = models.TextField(
        _("Business Description"),
        max_length=1000,
        blank=True,
        null=True,
        help_text=_("What the business does"),
    )

    founded_date = models.DateField(
        _("Founded Date"),
        blank=True,
        null=True,
        help_text=_("Date the business was founded"),
    )

    # ================================================================
    # REGISTRATION / TAX
    # ================================================================

    registration_number = models.CharField(
        _("Registration Number"),
        max_length=100,
        blank=True,
        null=True,
        help_text=_("Company/registration number issued by a government body"),
    )

    tax_id = models.CharField(
        _("Tax ID"),
        max_length=100,
        blank=True,
        null=True,
        help_text=_("EIN, VAT number, or equivalent"),
    )

    # ================================================================
    # BUSINESS-SPECIFIC CONTACT POINTS
    # (Distinct from the user's own ContactInfo -- see this file's
    # docstring for why these aren't merged into that model.)
    # ================================================================

    business_email = models.EmailField(
        _("Business Email"),
        max_length=254,
        blank=True,
        null=True,
        help_text=_("Public/storefront email, separate from the user's own login or alt email"),
    )

    business_phone = models.CharField(
        _("Business Phone"),
        max_length=20,
        blank=True,
        null=True,
        help_text=_("Public/storefront phone number"),
    )

    website = models.URLField(
        _("Business Website"),
        max_length=500,
        blank=True,
        null=True,
        help_text=_("Business website URL"),
    )

    # ================================================================
    # SCALE / FINANCIALS
    # ================================================================

    employee_count = models.PositiveIntegerField(
        _("Employee Count"),
        blank=True,
        null=True,
        help_text=_("Approximate number of employees"),
    )

    annual_revenue = models.DecimalField(
        _("Annual Revenue"),
        max_digits=14,
        decimal_places=2,
        blank=True,
        null=True,
        help_text=_("Approximate annual revenue, in `currency`"),
    )

    currency = models.CharField(
        _("Currency"),
        max_length=3,
        default="USD",
        help_text=_("ISO 4217 currency code, e.g. USD"),
    )

    # ================================================================
    # VERIFICATION & STATUS
    # (Mirrors ProfileInfo.is_profile_verified's shape rather than
    # ContactInfo's per-channel verification -- a business is verified
    # or it isn't, there's no meaningful "verify just the phone" here.)
    # ================================================================

    is_business_verified = models.BooleanField(
        _("Verified"),
        default=False,
        help_text=_("Business has been verified by administrators"),
    )

    verified_at = models.DateTimeField(
        _("Verified At"),
        blank=True,
        null=True,
    )

    verified_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="verified_businesses",
        help_text=_("Admin who verified this business"),
    )

    is_business_public = models.BooleanField(
        _("Public"),
        default=True,
        help_text=_("Business details are visible to other users"),
    )

    # ================================================================
    # TIMESTAMPS
    # ================================================================

    created_at = models.DateTimeField(_("Created At"), auto_now_add=True)
    updated_at = models.DateTimeField(_("Updated At"), auto_now=True)

    # ================================================================
    # SOFT DELETE
    # ================================================================

    is_business_archived = models.BooleanField(
        _("Archived"),
        default=False,
        db_index=True,
    )

    archived_at = models.DateTimeField(
        _("Archived At"),
        blank=True,
        null=True,
    )

    archived_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="archived_businesses",
        help_text=_("User who archived this business record"),
    )

    # ================================================================
    # METADATA
    # ================================================================

    metadata = models.JSONField(
        _("Metadata"),
        default=dict,
        blank=True,
        help_text=_("Additional business metadata in JSON format"),
    )

    # ================================================================
    # MANAGER / META
    # ================================================================

    objects = BusinessInfoManager()

    class Meta:
        verbose_name = _("Business Info")
        verbose_name_plural = _("Business Infos")
        db_table = "business_info"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["uuid"]),
            models.Index(fields=["business_name"]),
            models.Index(fields=["registration_number"]),
            models.Index(fields=["is_business_verified"]),
            models.Index(fields=["is_business_public", "is_business_archived"]),
        ]

    def __str__(self) -> str:
        return self.business_name or str(self.user)

    def __repr__(self) -> str:
        return f"<BusinessInfo: {self.business_name} ({self.user})>"

    # ================================================================
    # PROPERTIES
    # ================================================================

    @property
    def is_active(self) -> bool:
        """Not archived -- convenience mirror of ProfileInfo's is_profile_archived checks elsewhere."""
        return not self.is_business_archived

    @property
    def age_in_days(self) -> Optional[int]:
        """Days since founded_date, or None if unset."""
        if not self.founded_date:
            return None
        return (timezone.now().date() - self.founded_date).days

    @property
    def completion_percentage(self) -> int:
        """Rough completeness score, same spirit as ProfileInfo.completion_percentage."""
        fields = [
            self.business_name,
            self.description,
            self.industry,
            self.registration_number,
            self.business_email,
            self.website,
        ]
        completed = sum(1 for f in fields if f)
        return int((completed / len(fields)) * 100)

    # ================================================================
    # VERIFICATION METHODS
    # ================================================================

    def verify(self, verified_by_user=None, save: bool = True) -> None:
        """Mark business as verified."""
        self.is_business_verified = True
        self.verified_at = timezone.now()
        self.verified_by = verified_by_user
        if save:
            self.save(update_fields=["is_business_verified", "verified_at", "verified_by"])

    def unverify(self, save: bool = True) -> None:
        """Remove verification."""
        self.is_business_verified = False
        self.verified_at = None
        self.verified_by = None
        if save:
            self.save(update_fields=["is_business_verified", "verified_at", "verified_by"])

    # ================================================================
    # SOFT DELETE
    # ================================================================

    def soft_delete(self, archived_by_user=None, save: bool = True) -> None:
        """Archive this business record instead of hard-deleting it."""
        self.is_business_archived = True
        self.archived_at = timezone.now()
        self.archived_by = archived_by_user
        if save:
            self.save()

    def restore(self, save: bool = True) -> None:
        """Un-archive this business record."""
        self.is_business_archived = False
        self.archived_at = None
        self.archived_by = None
        if save:
            self.save()

    def delete(self, *args, **kwargs):
        """
        Instance-level delete soft-deletes (archives). Bulk queryset
        deletes are covered separately by BusinessInfoQuerySet.delete().
        """
        self.soft_delete()

    # ================================================================
    # DATA EXPORT (GDPR)
    # ================================================================

    def export_data(self) -> dict:
        """Export business data for GDPR compliance -- same shape/spirit as the other *Info.export_data() methods."""
        return {
            "uuid": str(self.uuid),
            "business_name": self.business_name,
            "legal_name": self.legal_name,
            "business_type": self.business_type,
            "industry": self.industry,
            "description": self.description,
            "founded_date": str(self.founded_date) if self.founded_date else None,
            "registration_number": self.registration_number,
            "tax_id": self.tax_id,
            "business_email": self.business_email,
            "business_phone": self.business_phone,
            "website": self.website,
            "employee_count": self.employee_count,
            "annual_revenue": float(self.annual_revenue) if self.annual_revenue is not None else None,
            "currency": self.currency,
            "is_business_verified": self.is_business_verified,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
            "metadata": self.metadata,
        }

    # ================================================================
    # VALIDATION
    # ================================================================

    def clean(self) -> None:
        super().clean()

        errors = {}

        if self.business_email:
            try:
                validate_email(self.business_email)
            except ValidationError:
                errors["business_email"] = _("Enter a valid business email address.")

        if self.website:
            hostname = (urlparse(self.website).hostname or "")
            if not hostname:
                errors["website"] = _("Enter a valid website URL.")

        if self.currency and (len(self.currency) != 3 or not self.currency.isalpha()):
            errors["currency"] = _("Currency must be a 3-letter ISO 4217 code, e.g. 'USD'.")
        elif self.currency:
            self.currency = self.currency.upper()

        if self.founded_date and self.founded_date > timezone.now().date():
            errors["founded_date"] = _("Founded date cannot be in the future.")

        if self.employee_count is not None and self.employee_count < 0:
            errors["employee_count"] = _("Employee count cannot be negative.")

        if self.annual_revenue is not None and self.annual_revenue < 0:
            errors["annual_revenue"] = _("Annual revenue cannot be negative.")

        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        """
        Run full validation only on full saves (no `update_fields`),
        matching the same rationale as User.save() in account.py:
        the narrow-field helper methods above (verify/unverify/
        soft_delete/restore) are only meant to touch a couple of
        fields at a time and shouldn't pay for full-instance
        validation on every call.
        """
        if kwargs.get("update_fields") is None:
            self.full_clean()
        super().save(*args, **kwargs)