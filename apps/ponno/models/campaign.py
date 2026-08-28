# apps/ponno/models/campaign.py

"""
Discount Campaign Models
-------------------------
Promo / flash-sale campaigns that apply a discount on top of a product's
existing price, without touching Product.selling_price / final_price
directly. Product's own pricing stays "what the dealer set"; a campaign
is a separate, time-boxed, possibly-limited-use layer resolved at read
time (product listing / detail / cart) by
apps.ponno.services.campaign_pricing.CampaignPricingService.

Two models:
  - Campaign            the promo definition (type, value, scope, window,
                         usage/budget limits, approval workflow,
                         priority/stackability rules).
  - CampaignRedemption  an audit row created every time a campaign is
                         actually applied to an order, used both for
                         reporting and for enforcing usage_limit_total /
                         usage_limit_per_user race-safely. Never deleted
                         via cascade — see its Meta for why.

Enterprise-lifecycle notes:
  - Campaigns go through an approval workflow (approval_status) before
    they can go live, independent of the is_enabled on/off switch.
  - Campaigns are soft-deleted (is_archived), never hard-deleted, so
    historical redemptions always resolve back to a real Campaign row.
  - Campaign.register_redemption() is the one and only correct way to
    consume a usage/budget slot; it locks the row and re-checks limits
    against live data rather than trusting whatever the caller last
    read, which is what actually makes the "race-safe" claim in
    CampaignRedemption's docstring true.
"""

import uuid
from decimal import Decimal
from typing import Optional

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator
from django.db import IntegrityError, models, transaction
from django.db.models import Q
from django.utils import timezone
from django.utils.text import slugify
from django.utils.translation import gettext_lazy as _

from apps.ponno.models.brand import Brand
from apps.ponno.models.category import Category
from apps.ponno.models.product import Product

# ====================================================================
# CAMPAIGN MANAGER
# ====================================================================

class CampaignManager(models.Manager):
    """Custom manager for Campaign."""

    def active(self):
        """
        Campaigns that are currently eligible to be applied: enabled,
        approved (or approval not required), not cancelled, not
        archived, inside their start/end window, and not exhausted by
        either the total usage cap or the budget cap.

        Does NOT check per-user limits — that's necessarily per-request
        and is enforced by CampaignPricingService at apply time, not at
        the queryset level.
        """
        now = timezone.now()
        return self.filter(
            is_enabled=True,
            is_cancelled=False,
            is_archived=False,
            approval_status__in=[
                Campaign.ApprovalStatus.APPROVED,
                Campaign.ApprovalStatus.NOT_REQUIRED,
            ],
            start_at__lte=now,
        ).filter(
            Q(end_at__isnull=True) | Q(end_at__gte=now)
        ).exclude(
            # usage_limit_total=0 is treated as "unlimited" (see field
            # help_text), so only exclude when a real cap has been hit.
            Q(usage_limit_total__gt=0) & Q(times_used__gte=models.F('usage_limit_total'))
        ).exclude(
            # budget_limit=None means "no budget cap" — only exclude
            # when a real cap has been set and reached.
            Q(budget_limit__isnull=False) & Q(budget_spent__gte=models.F('budget_limit'))
        )

    def for_product(self, product: "Product"):
        """
        Active campaigns whose scope covers `product`, ordered by
        priority (highest first) then by discount value (largest first)
        as a tiebreaker — callers that only want the single best
        campaign can just take .first().
        """
        scope_q = Q(applies_to=Campaign.AppliesTo.ALL_PRODUCTS)

        scope_q |= Q(applies_to=Campaign.AppliesTo.SPECIFIC_PRODUCTS, products=product)

        if product.category_id:
            scope_q |= Q(
                applies_to=Campaign.AppliesTo.SPECIFIC_CATEGORIES,
                categories=product.category_id,
            )

        if product.brand_id:
            scope_q |= Q(
                applies_to=Campaign.AppliesTo.SPECIFIC_BRANDS,
                brands=product.brand_id,
            )

        return (
            self.active()
            .filter(scope_q)
            .distinct()
            .order_by('-priority', '-discount_value')
        )

    def pending_approval(self):
        """Campaigns sitting in the admin approval queue."""
        return self.filter(
            approval_status=Campaign.ApprovalStatus.PENDING,
            is_archived=False,
        ).order_by('submitted_for_approval_at')

    def archived(self):
        """Soft-deleted campaigns, for admin/audit views only."""
        return self.filter(is_archived=True)

    # ================================================================
    # SCOPE LOCKING (prevent double-booking a product, category, or
    # brand across a dealer's own SPECIFIC_* campaigns)
    # ----------------------------------------------------------------
    # Three parallel families of methods below — one each for
    # products, categories, and brands — all following the same shape:
    #
    #   locked_<x>_ids(created_by)   -> set of ids (for query exclusion)
    #   locked_<x>s_map(created_by)  -> {id: {'campaign_name',
    #                                          'campaign_slug',
    #                                          'unlocks_at'}}
    #                                    (for display / UI labels)
    #
    # "Locked" means: reserved by one of `created_by`'s own campaigns
    # of the matching applies_to scope that hasn't ended yet — not
    # archived, not cancelled, not rejected, and (if it has an end_at)
    # that end_at hasn't passed. Deliberately does NOT check
    # is_enabled/approval_status=PENDING separately: a draft or
    # pending-approval campaign still reserves its scope, since the
    # creator clearly intends to use it there once the campaign goes
    # live. Only a genuinely dead campaign (ended/cancelled/archived/
    # rejected) releases its lock.
    #
    # Scoped to `created_by` because locking is meant to stop the SAME
    # creator double-booking a product/category/brand across two of
    # their own campaigns — it does not lock against other creators'
    # campaigns.
    # ================================================================

    # ---- Products ---------------------------------------------------

    def locked_product_ids(self, created_by):
        """Product-id set version of locked_products_map() — see there."""
        return set(self.locked_products_map(created_by).keys())

    def locked_products_map(self, created_by):
        """
        {product_id: {'campaign_name', 'campaign_slug', 'unlocks_at'}}
        for every product reserved by one of `created_by`'s own
        still-live SPECIFIC_PRODUCTS campaigns.

        'unlocks_at' is the campaign's end_at, or None if the campaign
        has no end date (i.e. it never auto-unlocks — only
        cancel()/archive()/reject() will free the product). Template
        can render that as "locked indefinitely" when None.

        If a product were somehow reserved by more than one live
        campaign (shouldn't happen going forward since the view checks
        this map before allowing a save, but could exist from data
        created before this rule), the campaign with the soonest
        unlocks_at wins the display slot — not important which, since
        it's just informational.
        """
        now = timezone.now()

        live_qs = (
            self.filter(
                applies_to=Campaign.AppliesTo.SPECIFIC_PRODUCTS,
                created_by=created_by,
                is_archived=False,
                is_cancelled=False,
            )
            .exclude(approval_status=Campaign.ApprovalStatus.REJECTED)
            .filter(Q(end_at__isnull=True) | Q(end_at__gte=now))
            .order_by(models.F('end_at').asc(nulls_last=True))
        )

        locked = {}
        for campaign in live_qs.prefetch_related('products'):
            for product_id in campaign.products.values_list('id', flat=True):
                if product_id not in locked:
                    locked[product_id] = {
                        'campaign_name': campaign.name,
                        'campaign_slug': campaign.slug,
                        'unlocks_at': campaign.end_at,
                    }

        return locked

    # ---- Categories ---------------------------------------------------

    def locked_category_ids(self, created_by):
        """Category-id set version of locked_categories_map() — see there."""
        return set(self.locked_categories_map(created_by).keys())

    def locked_categories_map(self, created_by):
        """
        {category_id: {'campaign_name', 'campaign_slug', 'unlocks_at'}}
        for every category reserved by one of `created_by`'s own
        still-live SPECIFIC_CATEGORIES campaigns. Same semantics as
        locked_products_map() above, just walking the `categories` m2m
        instead of `products`.
        """
        now = timezone.now()

        live_qs = (
            self.filter(
                applies_to=Campaign.AppliesTo.SPECIFIC_CATEGORIES,
                created_by=created_by,
                is_archived=False,
                is_cancelled=False,
            )
            .exclude(approval_status=Campaign.ApprovalStatus.REJECTED)
            .filter(Q(end_at__isnull=True) | Q(end_at__gte=now))
            .order_by(models.F('end_at').asc(nulls_last=True))
        )

        locked = {}
        for campaign in live_qs.prefetch_related('categories'):
            for category_id in campaign.categories.values_list('id', flat=True):
                if category_id not in locked:
                    locked[category_id] = {
                        'campaign_name': campaign.name,
                        'campaign_slug': campaign.slug,
                        'unlocks_at': campaign.end_at,
                    }

        return locked

    # ---- Brands ---------------------------------------------------

    def locked_brand_ids(self, created_by):
        """Brand-id set version of locked_brands_map() — see there."""
        return set(self.locked_brands_map(created_by).keys())

    def locked_brands_map(self, created_by):
        """
        {brand_id: {'campaign_name', 'campaign_slug', 'unlocks_at'}}
        for every brand reserved by one of `created_by`'s own
        still-live SPECIFIC_BRANDS campaigns. Same semantics as
        locked_products_map() above, just walking the `brands` m2m
        instead of `products`.
        """
        now = timezone.now()

        live_qs = (
            self.filter(
                applies_to=Campaign.AppliesTo.SPECIFIC_BRANDS,
                created_by=created_by,
                is_archived=False,
                is_cancelled=False,
            )
            .exclude(approval_status=Campaign.ApprovalStatus.REJECTED)
            .filter(Q(end_at__isnull=True) | Q(end_at__gte=now))
            .order_by(models.F('end_at').asc(nulls_last=True))
        )

        locked = {}
        for campaign in live_qs.prefetch_related('brands'):
            for brand_id in campaign.brands.values_list('id', flat=True):
                if brand_id not in locked:
                    locked[brand_id] = {
                        'campaign_name': campaign.name,
                        'campaign_slug': campaign.slug,
                        'unlocks_at': campaign.end_at,
                    }

        return locked

# ====================================================================
# CAMPAIGN MODEL
# ====================================================================

class Campaign(models.Model):
    """
    A discount / promo campaign (percentage-off, fixed-amount-off, or
    flash sale) scoped to all products, specific products, specific
    categories, or specific brands, active within a time window and
    optionally capped by total/per-user usage and/or a discount budget.
    """

    # ================================================================
    # CHOICES
    # ================================================================

    class CampaignType(models.TextChoices):
        PERCENTAGE = 'percentage', _('Percentage Off')
        FIXED_AMOUNT = 'fixed_amount', _('Fixed Amount Off')

    class AppliesTo(models.TextChoices):
        ALL_PRODUCTS = 'all_products', _('All Products')
        SPECIFIC_PRODUCTS = 'specific_products', _('Specific Products')
        SPECIFIC_CATEGORIES = 'specific_categories', _('Specific Categories')
        SPECIFIC_BRANDS = 'specific_brands', _('Specific Brands')

    class Channel(models.TextChoices):
        ALL = 'all', _('All Channels')
        WEB = 'web', _('Web Storefront')
        APP = 'app', _('Mobile App')
        POS = 'pos', _('Point of Sale')

    class ApprovalStatus(models.TextChoices):
        NOT_REQUIRED = 'not_required', _('Approval Not Required')
        PENDING = 'pending', _('Pending Approval')
        APPROVED = 'approved', _('Approved')
        REJECTED = 'rejected', _('Rejected')

    class Status(models.TextChoices):
        DRAFT = 'draft', _('Draft')
        PENDING_APPROVAL = 'pending_approval', _('Pending Approval')
        REJECTED = 'rejected', _('Rejected')
        SCHEDULED = 'scheduled', _('Scheduled')
        ACTIVE = 'active', _('Active')
        PAUSED = 'paused', _('Paused')
        ENDED = 'ended', _('Ended')
        CANCELLED = 'cancelled', _('Cancelled')
        ARCHIVED = 'archived', _('Archived')

    # ================================================================
    # IDENTIFICATION
    # ================================================================

    campaign_id = models.UUIDField(
        _("Campaign UUID"),
        default=uuid.uuid4,
        editable=False,
        unique=True,
    )

    campaign_code = models.CharField(
        _("Campaign Code"),
        max_length=40,
        unique=True,
        blank=True,
        null=True,
        help_text=_(
            "Optional external reference code (e.g. for POS lookups or "
            "coupon-style entry), distinct from the customer-facing "
            "URL slug. Leave blank if this campaign is only ever "
            "resolved automatically by scope/schedule."
        ),
    )

    name = models.CharField(
        _("Campaign Name"),
        max_length=150,
        help_text=_("Internal / customer-facing name, e.g. 'Eid Flash Sale'")
    )

    slug = models.SlugField(
        _("Slug"),
        max_length=170,
        unique=True,
        blank=True,
        help_text=_("URL-friendly identifier, auto-generated from name")
    )

    description = models.TextField(
        _("Description"),
        max_length=2000,
        blank=True,
        null=True,
    )

    banner_image = models.ImageField(
        _("Banner Image"),
        upload_to='campaigns/%Y/%m/',
        blank=True,
        null=True,
    )

    terms_conditions = models.TextField(
        _("Terms & Conditions"),
        blank=True,
        null=True,
    )

    # ================================================================
    # DISCOUNT DEFINITION
    # ================================================================

    campaign_type = models.CharField(
        _("Campaign Type"),
        max_length=20,
        choices=CampaignType.choices,
        default=CampaignType.PERCENTAGE,
    )

    discount_value = models.DecimalField(
        _("Discount Value"),
        max_digits=12,
        decimal_places=2,
        validators=[MinValueValidator(Decimal('0.01'))],
        help_text=_(
            "Percentage (0-100) when type=percentage, or a currency "
            "amount when type=fixed_amount"
        ),
    )

    max_discount_amount = models.DecimalField(
        _("Max Discount Amount"),
        max_digits=12,
        decimal_places=2,
        null=True,
        blank=True,
        validators=[MinValueValidator(Decimal('0.01'))],
        help_text=_(
            "Optional cap on the discount actually applied. Mainly useful "
            "for percentage campaigns, e.g. '30% off, up to ৳500'. "
            "Leave blank for no cap — 0 is intentionally rejected "
            "(that would zero out every discount, which is never what "
            "someone filling in this field actually means)."
        ),
    )

    min_order_amount = models.DecimalField(
        _("Minimum Order Amount"),
        max_digits=12,
        decimal_places=2,
        null=True,
        blank=True,
        validators=[MinValueValidator(Decimal('0.00'))],
        help_text=_(
            "If set, the campaign only applies when the order/cart "
            "subtotal is at least this amount. Checked by the pricing "
            "service at cart/checkout resolution, not at the per-product "
            "level."
        ),
    )

    currency = models.CharField(
        _("Currency"),
        max_length=3,
        default='BDT',
        help_text=_(
            "ISO 4217 currency code that discount_value / "
            "max_discount_amount / min_order_amount / budget_limit are "
            "denominated in when campaign_type=fixed_amount. Purely "
            "informational for percentage campaigns."
        ),
    )

    # ================================================================
    # SCOPE
    # ================================================================

    applies_to = models.CharField(
        _("Applies To"),
        max_length=25,
        choices=AppliesTo.choices,
        default=AppliesTo.ALL_PRODUCTS,
    )

    products = models.ManyToManyField(
        Product,
        related_name='campaigns',
        blank=True,
        help_text=_("Used when applies_to=specific_products"),
    )

    categories = models.ManyToManyField(
        Category,
        related_name='campaigns',
        blank=True,
        help_text=_("Used when applies_to=specific_categories"),
    )

    brands = models.ManyToManyField(
        Brand,
        related_name='campaigns',
        blank=True,
        help_text=_("Used when applies_to=specific_brands"),
    )

    channel = models.CharField(
        _("Channel"),
        max_length=10,
        choices=Channel.choices,
        default=Channel.ALL,
        help_text=_("Which storefront surface this campaign is allowed to be applied on."),
    )

    # ================================================================
    # SCHEDULE
    # ================================================================

    start_at = models.DateTimeField(
        _("Start At"),
        db_index=True,
        help_text=_("When the campaign becomes active"),
    )

    end_at = models.DateTimeField(
        _("End At"),
        null=True,
        blank=True,
        db_index=True,
        help_text=_("When the campaign stops being active. Blank = no end date."),
    )

    # ================================================================
    # USAGE & BUDGET LIMITS
    # ================================================================

    usage_limit_total = models.PositiveIntegerField(
        _("Total Usage Limit"),
        default=0,
        help_text=_("Max number of times this campaign can be redeemed across all users. 0 = unlimited."),
    )

    usage_limit_per_user = models.PositiveIntegerField(
        _("Per-User Usage Limit"),
        default=0,
        help_text=_("Max number of times a single user can redeem this campaign. 0 = unlimited."),
    )

    times_used = models.PositiveIntegerField(
        _("Times Used"),
        default=0,
        editable=False,
        help_text=_("Running redemption count. Only ever modified by Campaign.register_redemption()."),
    )

    budget_limit = models.DecimalField(
        _("Budget Limit"),
        max_digits=14,
        decimal_places=2,
        null=True,
        blank=True,
        validators=[MinValueValidator(Decimal('0.01'))],
        help_text=_(
            "Optional cap on total discount amount (in `currency`) this "
            "campaign may give away across all redemptions combined. "
            "Blank = no budget cap. Enforced alongside, not instead of, "
            "usage_limit_total."
        ),
    )

    budget_spent = models.DecimalField(
        _("Budget Spent"),
        max_digits=14,
        decimal_places=2,
        default=Decimal('0.00'),
        editable=False,
        validators=[MinValueValidator(Decimal('0.00'))],
        help_text=_("Running total of discount amount given away. Only ever modified by Campaign.register_redemption()."),
    )

    # ================================================================
    # STACKING / PRIORITY
    # ================================================================

    priority = models.IntegerField(
        _("Priority"),
        default=0,
        help_text=_(
            "When multiple campaigns are applicable to the same product "
            "and are NOT stackable with each other, the highest-priority "
            "one wins. Ties broken by largest discount_value."
        ),
    )

    is_stackable = models.BooleanField(
        _("Stackable"),
        default=False,
        help_text=_(
            "If True, this campaign can be combined with other stackable "
            "campaigns applicable to the same product. If False (default), "
            "it is applied exclusively — the single best-priority "
            "campaign wins and all others (stackable or not) are ignored "
            "for that product. See CampaignPricingService for the exact "
            "resolution algorithm."
        ),
    )

    # ================================================================
    # APPROVAL WORKFLOW
    # ================================================================

    approval_status = models.CharField(
        _("Approval Status"),
        max_length=15,
        choices=ApprovalStatus.choices,
        default=ApprovalStatus.NOT_REQUIRED,
        help_text=_(
            "Independent of is_enabled: a campaign only counts as "
            "effectively live once approval_status is APPROVED or "
            "NOT_REQUIRED *and* is_enabled=True. See Campaign.active()."
        ),
    )

    submitted_for_approval_at = models.DateTimeField(_("Submitted At"), null=True, blank=True, editable=False)

    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='campaigns_approved',
    )

    approved_at = models.DateTimeField(_("Approved At"), null=True, blank=True, editable=False)

    rejected_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='campaigns_rejected',
    )

    rejected_at = models.DateTimeField(_("Rejected At"), null=True, blank=True, editable=False)

    rejection_reason = models.TextField(_("Rejection Reason"), blank=True, null=True)

    # ================================================================
    # LIFECYCLE
    # ================================================================

    is_enabled = models.BooleanField(
        _("Enabled"),
        default=True,
        help_text=_(
            "Manual on/off switch, independent of the start_at/end_at "
            "window and of approval_status. A disabled campaign never "
            "appears in .active(), even mid-window and even if approved."
        ),
    )

    is_cancelled = models.BooleanField(
        _("Cancelled"),
        default=False,
        help_text=_(
            "Terminal state — a cancelled campaign is permanently done "
            "regardless of is_enabled/schedule/approval, and cannot be "
            "re-enabled. Use cancel() rather than setting this directly, "
            "since is_enabled=True + is_cancelled=True is rejected by "
            "clean()."
        ),
    )

    first_activated_at = models.DateTimeField(
        _("First Activated At"),
        null=True,
        blank=True,
        editable=False,
        help_text=_(
            "Set automatically the first time is_enabled transitions to "
            "True. Used only to distinguish DRAFT (never launched) from "
            "PAUSED (launched, then disabled) in the derived status "
            "property — has no effect on discount eligibility."
        ),
    )

    # ================================================================
    # SOFT DELETE
    # ================================================================

    is_archived = models.BooleanField(
        _("Archived"),
        default=False,
        help_text=_(
            "Soft-delete flag. Archived campaigns are excluded from "
            "Campaign.active() and hidden from normal admin lists, but "
            "the row (and its redemption history) is never hard-deleted "
            "— use archive() rather than .delete()."
        ),
    )

    archived_at = models.DateTimeField(_("Archived At"), null=True, blank=True, editable=False)

    archived_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='campaigns_archived',
    )

    # ================================================================
    # OWNERSHIP / AUDIT / TIMESTAMPS
    # ================================================================

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='campaigns_created',
    )

    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='campaigns_updated',
        help_text=_("Last admin/dealer to save this campaign. Set by callers, not inferred automatically."),
    )

    created_at = models.DateTimeField(_("Created"), auto_now_add=True)
    updated_at = models.DateTimeField(_("Updated"), auto_now=True)

    # ================================================================
    # MANAGER / META
    # ================================================================

    objects = CampaignManager()

    class Meta:
        verbose_name = _("Campaign")
        verbose_name_plural = _("Campaigns")
        db_table = 'campaigns'
        ordering = ['-priority', '-start_at']
        indexes = [
            # campaign_id, slug, and campaign_code already get an index
            # for free from unique=True — no separate Index() needed.
            models.Index(fields=['is_enabled', 'is_cancelled', 'is_archived', 'start_at', 'end_at']),
            models.Index(fields=['applies_to']),
            models.Index(fields=['priority']),
            models.Index(fields=['approval_status']),
        ]
        constraints = [
            models.CheckConstraint(
                check=Q(end_at__isnull=True) | Q(end_at__gt=models.F('start_at')),
                name='campaign_end_after_start',
            ),
            models.CheckConstraint(
                check=~(Q(is_cancelled=True) & Q(is_enabled=True)),
                name='campaign_cancelled_not_enabled',
            ),
        ]

    def __str__(self) -> str:
        return f"{self.name} ({self.get_campaign_type_display()})"

    def __repr__(self) -> str:
        return f"<Campaign: {self.name} ({self.campaign_type}={self.discount_value})>"

    # ================================================================
    # STATUS / VALIDITY HELPERS
    # ================================================================

    @property
    def status(self) -> str:
        """
        Derived display status. Not stored — computed from the
        lifecycle/approval/schedule/usage fields so it's always
        consistent (no risk of a stale stored status field drifting
        from the actual window).
        """
        if self.is_archived:
            return self.Status.ARCHIVED
        if self.is_cancelled:
            return self.Status.CANCELLED
        if self.approval_status == self.ApprovalStatus.PENDING:
            return self.Status.PENDING_APPROVAL
        if self.approval_status == self.ApprovalStatus.REJECTED:
            return self.Status.REJECTED
        if not self.is_enabled:
            return self.Status.PAUSED if self.first_activated_at else self.Status.DRAFT

        now = timezone.now()
        if self.end_at and now > self.end_at:
            return self.Status.ENDED
        if now < self.start_at:
            return self.Status.SCHEDULED
        if self.is_exhausted or self.is_budget_exhausted:
            return self.Status.ENDED
        return self.Status.ACTIVE

    @property
    def is_currently_active(self) -> bool:
        return self.status == self.Status.ACTIVE

    @property
    def is_exhausted(self) -> bool:
        return bool(self.usage_limit_total) and self.times_used >= self.usage_limit_total

    @property
    def is_budget_exhausted(self) -> bool:
        return self.budget_limit is not None and self.budget_spent >= self.budget_limit

    @property
    def remaining_uses(self) -> Optional[int]:
        if not self.usage_limit_total:
            return None
        return max(self.usage_limit_total - self.times_used, 0)

    @property
    def remaining_budget(self) -> Optional[Decimal]:
        if self.budget_limit is None:
            return None
        return max(self.budget_limit - self.budget_spent, Decimal('0.00'))

    # ================================================================
    # DISCOUNT CALCULATION (pure — no DB writes)
    # ================================================================

    def compute_discount(self, price: Decimal) -> Decimal:
        """
        Compute the discount amount (never negative, never more than
        `price` itself) this campaign would take off a given price,
        honoring max_discount_amount if set. Does not check scope,
        schedule, or usage/budget limits — callers should only invoke
        this on a campaign already confirmed applicable via
        for_product()/active().
        """
        if price is None or price <= 0:
            return Decimal('0.00')

        if self.campaign_type == self.CampaignType.PERCENTAGE:
            discount = (price * self.discount_value) / Decimal('100')
        else:
            discount = self.discount_value

        if self.max_discount_amount is not None:
            discount = min(discount, self.max_discount_amount)

        # Never discount past zero.
        discount = min(discount, price)
        return discount.quantize(Decimal('0.01'))

    def discounted_price(self, price: Decimal) -> Decimal:
        return (price - self.compute_discount(price)).quantize(Decimal('0.01'))

    # ================================================================
    # REDEMPTION (the only correct way to consume a usage/budget slot)
    # ================================================================

    def register_redemption(self, discount_amount: Optional[Decimal] = None) -> bool:
        """
        Atomically claim one redemption slot for this campaign.

        Locks this campaign's row (SELECT ... FOR UPDATE), re-checks
        usage_limit_total and budget_limit against the *live* row
        (never against self, which may be stale by the time this is
        called), and only if there's room, increments times_used (and
        budget_spent, if discount_amount is given). Returns True if the
        slot was claimed, False if the campaign was already exhausted
        by the time the lock was acquired — callers must treat False as
        "do not apply this discount", not just a logging concern.

        MUST be called from inside the same transaction.atomic() block
        that creates the corresponding CampaignRedemption row, so the
        counter increment and the audit row commit or roll back
        together. This method is what makes the "race-safe" claim in
        CampaignRedemption's docstring actually true — a plain
        `F('times_used') + 1` update without the row lock and live
        re-check would let two concurrent requests both squeeze past a
        usage_limit_total=1 cap.

        Per-user limits are NOT checked here — that requires counting
        CampaignRedemption rows for the specific user, which the caller
        (CampaignPricingService) is better positioned to do alongside
        creating that row, inside the same transaction.
        """
        with transaction.atomic():
            locked = Campaign.objects.select_for_update().get(pk=self.pk)

            if locked.usage_limit_total and locked.times_used >= locked.usage_limit_total:
                return False
            if locked.budget_limit is not None and discount_amount is not None:
                if locked.budget_spent + discount_amount > locked.budget_limit:
                    return False

            update_fields = ['times_used']
            locked.times_used = models.F('times_used') + 1
            if discount_amount is not None:
                locked.budget_spent = models.F('budget_spent') + discount_amount
                update_fields.append('budget_spent')

            locked.save(update_fields=update_fields)
            locked.refresh_from_db(fields=update_fields)

            # Mirror the post-update counters onto self so the caller's
            # in-memory instance reflects reality without a second query.
            self.times_used = locked.times_used
            self.budget_spent = locked.budget_spent

        return True

    # ================================================================
    # LIFECYCLE TRANSITIONS
    # ================================================================

    def submit_for_approval(self, save: bool = True) -> None:
        self.approval_status = self.ApprovalStatus.PENDING
        self.submitted_for_approval_at = timezone.now()
        if save:
            self.save(update_fields=['approval_status', 'submitted_for_approval_at'])

    def approve(self, approved_by_user=None, save: bool = True) -> None:
        self.approval_status = self.ApprovalStatus.APPROVED
        self.approved_by = approved_by_user
        self.approved_at = timezone.now()
        self.rejected_by = None
        self.rejected_at = None
        self.rejection_reason = None
        if save:
            self.save(update_fields=[
                'approval_status', 'approved_by', 'approved_at',
                'rejected_by', 'rejected_at', 'rejection_reason',
            ])

    def reject(self, rejected_by_user=None, reason: str = '', save: bool = True) -> None:
        self.approval_status = self.ApprovalStatus.REJECTED
        self.rejected_by = rejected_by_user
        self.rejected_at = timezone.now()
        self.rejection_reason = reason
        self.is_enabled = False
        if save:
            self.save(update_fields=[
                'approval_status', 'rejected_by', 'rejected_at',
                'rejection_reason', 'is_enabled',
            ])

    def enable(self, save: bool = True) -> None:
        """Turn the campaign on. Does not affect approval_status."""
        self.is_enabled = True
        if self.first_activated_at is None:
            self.first_activated_at = timezone.now()
        if save:
            self.save(update_fields=['is_enabled', 'first_activated_at'])

    def disable(self, save: bool = True) -> None:
        """Pause the campaign. Reversible, unlike cancel()."""
        self.is_enabled = False
        if save:
            self.save(update_fields=['is_enabled'])

    def cancel(self, save: bool = True) -> None:
        """Terminal state — see is_cancelled help_text."""
        self.is_cancelled = True
        self.is_enabled = False
        if save:
            self.save(update_fields=['is_cancelled', 'is_enabled'])

    def archive(self, archived_by_user=None, save: bool = True) -> None:
        """Soft delete. Use this instead of .delete()."""
        self.is_archived = True
        self.archived_at = timezone.now()
        self.archived_by = archived_by_user
        self.is_enabled = False
        if save:
            self.save(update_fields=['is_archived', 'archived_at', 'archived_by', 'is_enabled'])

    def restore(self, save: bool = True) -> None:
        """Reverse archive(). Does not automatically re-enable."""
        self.is_archived = False
        self.archived_at = None
        self.archived_by = None
        if save:
            self.save(update_fields=['is_archived', 'archived_at', 'archived_by'])

    def delete(self, *args, **kwargs):
        """
        Hard delete is deliberately not the default path — call
        archive() instead so redemption audit history stays resolvable.
        This override exists as a safety net for code that calls
        .delete() out of habit; genuine hard deletion (data-retention
        jobs, GDPR erasure, etc.) should go through
        Campaign.objects.filter(pk=...).delete() explicitly, which this
        does NOT intercept.
        """
        self.archive()

    # ================================================================
    # SLUG
    # ================================================================

    def generate_slug(self, save: bool = False) -> str:
        if not self.name:
            return None

        base_slug = slugify(self.name) or f"campaign-{uuid.uuid4().hex[:10]}"
        slug = base_slug
        counter = 1
        while Campaign.objects.filter(slug=slug).exclude(pk=self.pk).exists():
            slug = f"{base_slug}-{counter}"
            counter += 1

        self.slug = slug
        if save:
            self.save(update_fields=['slug'])
        return slug

    # ================================================================
    # VALIDATION
    # ================================================================

    def clean(self) -> None:
        super().clean()

        if self.campaign_type == self.CampaignType.PERCENTAGE:
            if self.discount_value > Decimal('100.00'):
                raise ValidationError(
                    _("Percentage discount cannot exceed 100.")
                )

        if self.end_at and self.start_at and self.end_at <= self.start_at:
            raise ValidationError(
                _("End date must be after start date.")
            )

        if self.is_cancelled and self.is_enabled:
            raise ValidationError(
                _("A cancelled campaign cannot also be enabled. Call cancel() rather than setting these fields directly.")
            )

        if self.budget_limit is not None and self.budget_spent > self.budget_limit:
            raise ValidationError(
                _("budget_spent cannot exceed budget_limit.")
            )

    def save(self, *args, **kwargs):
        update_fields = kwargs.get('update_fields')

        if self.name and not self.slug:
            self.generate_slug()

        # Track the first is_enabled=True transition so status can tell
        # DRAFT (never launched) apart from PAUSED (launched, then
        # turned off). Only worth the extra lookup when is_enabled is
        # actually part of what's being saved.
        if self.pk and (update_fields is None or 'is_enabled' in update_fields):
            previous_enabled = (
                Campaign.objects.filter(pk=self.pk)
                .values_list('is_enabled', flat=True)
                .first()
            )
            if self.is_enabled and not previous_enabled and not self.first_activated_at:
                self.first_activated_at = timezone.now()
                if update_fields is not None and 'first_activated_at' not in update_fields:
                    kwargs['update_fields'] = list(update_fields) + ['first_activated_at']
        elif not self.pk and self.is_enabled and not self.first_activated_at:
            self.first_activated_at = timezone.now()

        # Full validation (incl. uniqueness checks) on create and on
        # unrestricted saves. Targeted update_fields saves — e.g. the
        # internal counter bumps in register_redemption(), which write
        # F()-expression values that full_clean() cannot validate as
        # plain Decimals — intentionally skip it. Anything reaching
        # save(update_fields=...) from outside this file is expected to
        # have already gone through clean()/full_clean() once at
        # creation time.
        if update_fields is None:
            self.full_clean()

        try:
            super().save(*args, **kwargs)
        except IntegrityError:
            # Most likely cause: a concurrent save landed on the same
            # auto-generated slug between our uniqueness check in
            # generate_slug() and this INSERT/UPDATE. Regenerate once
            # with a random suffix and retry; a second collision is
            # treated as a genuine error rather than retried forever.
            if update_fields is None and self.name:
                self.slug = f"{slugify(self.name)}-{uuid.uuid4().hex[:6]}"
                super().save(*args, **kwargs)
            else:
                raise


# ====================================================================
# CAMPAIGN REDEMPTION (usage / audit trail)
# ====================================================================

class CampaignRedemption(models.Model):
    """
    One row per successful application of a campaign to an order line.
    Written by CampaignPricingService.redeem() inside the same
    transaction as a call to Campaign.register_redemption() — that
    method is the sole source of truth for whether a slot was actually
    available, and this table is both the audit trail and the source
    of truth for per-user limit checks (rather than trusting a
    client-supplied count).

    campaign uses on_delete=PROTECT rather than CASCADE: Campaign rows
    are soft-deleted via Campaign.archive() and should never be
    hard-deleted while redemption history referencing them exists. If
    you hit a ProtectedError trying to delete a Campaign, that's the
    system working as intended — archive it instead.
    """

    campaign = models.ForeignKey(
        Campaign,
        on_delete=models.PROTECT,
        related_name='redemptions',
    )

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='campaign_redemptions',
    )

    product = models.ForeignKey(
        Product,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='campaign_redemptions',
    )

    # Nullable + SET_NULL: a redemption can be provisionally recorded at
    # cart-price-preview time (order not yet placed) and linked to an
    # order later, or the order itself can later be deleted/archived
    # without losing the discount audit trail.
    order = models.ForeignKey(
        'ponno.Order',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='campaign_redemptions',
    )

    order_item = models.ForeignKey(
        'ponno.OrderItem',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='campaign_redemptions',
    )

    original_price = models.DecimalField(max_digits=12, decimal_places=2)
    discount_amount = models.DecimalField(max_digits=12, decimal_places=2)
    final_price = models.DecimalField(max_digits=12, decimal_places=2)

    redeemed_at = models.DateTimeField(_("Redeemed At"), default=timezone.now, db_index=True)

    class Meta:
        verbose_name = _("Campaign Redemption")
        verbose_name_plural = _("Campaign Redemptions")
        db_table = 'campaign_redemptions'
        ordering = ['-redeemed_at']
        indexes = [
            models.Index(fields=['campaign', 'user']),
            models.Index(fields=['campaign', 'redeemed_at']),
        ]

    def __str__(self):
        return f"{self.campaign.name} redeemed by {self.user} (-{self.discount_amount})"