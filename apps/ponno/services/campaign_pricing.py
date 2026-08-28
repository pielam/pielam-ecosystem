"""
Campaign Pricing Service
-------------------------
All "which campaigns apply and how much do they save" logic lives here,
kept out of both Product (whose own pricing fields represent the
dealer's price, not a promo layer) and Campaign (a pure discount
definition). Three responsibilities:

  1. Scope resolution (products_for_campaign) — "which products does
     this campaign apply to", per its applies_to. Single source of
     truth: apps/ponno/views/campaign_detail.py (public landing page)
     and apps/customer/views/campaign_profile.py (owner detail view)
     both call this rather than each reimplementing the applies_to
     branch — that duplication is what let them drift apart before
     (the owner view only ever handled SPECIFIC_PRODUCTS and silently
     returned none() for the other three scopes).

  2. Read-time price resolution (get_effective_price / bulk variant) —
     cheap, no DB writes, safe to call on every product list/detail
     request.

  3. Redemption (redeem) — the write path, called once at checkout when
     an order line is actually being placed. Uses select_for_update to
     make the usage-limit check-and-increment atomic, so a flash-sale
     campaign with usage_limit_total=100 cannot be redeemed 105 times
     by concurrent checkouts racing past a naive check-then-increment.

PRICING BASE — read this before touching _base_price():
    A campaign discounts from Product.brand_price (MRP) when the
    product has one recorded, NOT from Product.selling_price /
    final_price. CampaignPriceResult.final_price is therefore
    "MRP minus the campaign discount", and that's the number that
    should be shown/used as the effective selling price wherever a
    campaign is active — it can end up *below* the dealer's own
    selling_price/final_price, exactly the same, or (if the campaign's
    discount is smaller than the dealer's own MRP->selling markdown)
    above it. This method makes no attempt to reconcile those two —
    callers displaying price should always prefer the
    CampaignPricingService result over Product.final_price directly
    once a campaign is applicable, since the service result is the
    "current true price" once a campaign is in play.
"""

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Iterable, List, Optional

from django.db import transaction
from django.core.exceptions import ValidationError
from django.utils.translation import gettext_lazy as _

from apps.ponno.models.campaign import Campaign, CampaignRedemption
from apps.ponno.models.product import Product


@dataclass
class CampaignPriceResult:
    """
    Result of resolving campaigns against a product's current price.
    `applied_campaigns` is ordered the same way the discounts were
    stacked (first = first applied), and is empty when nothing applied.

    original_price: the MRP (Product.brand_price) the campaign discount
        was computed against, when the product has one; otherwise the
        dealer's own final_price/selling_price (see _base_price()).
    final_price: original_price minus whatever campaigns applied. This
        is the number to actually charge/display as the selling price
        once a campaign is in play — it supersedes Product.final_price,
        it does not get averaged or reconciled with it.
    """

    product: Product
    original_price: Decimal
    final_price: Decimal
    applied_campaigns: List[Campaign] = field(default_factory=list)

    @property
    def total_discount(self) -> Decimal:
        return (self.original_price - self.final_price).quantize(Decimal('0.01'))

    @property
    def has_discount(self) -> bool:
        return self.total_discount > 0

    @property
    def campaign(self) -> Optional[Campaign]:
        """Convenience accessor for the common single-campaign case."""
        return self.applied_campaigns[0] if self.applied_campaigns else None

    @property
    def selling_price(self) -> Decimal:
        """
        Alias for final_price, for callers/templates that think in terms
        of "what's the selling price now" rather than "what's the final
        price after discount" — same value either way.
        """
        return self.final_price


class CampaignPricingService:

    # ----------------------------------------------------------------
    # SCOPE RESOLUTION — "which products does this campaign apply to"
    # ----------------------------------------------------------------

    @staticmethod
    def products_for_campaign(campaign: Campaign):
        """
        Resolve the queryset of active products a campaign is scoped to,
        per its `applies_to`. Single source of truth for scope
        resolution — see module docstring.

        Callers needing active/channel/status gating on top of this
        (e.g. the public landing page not showing products for a
        PAUSED or POS-only campaign) apply that themselves before
        calling in — this method only resolves *scope*, not whether
        the campaign is currently live or web-visible. Always filtered
        to Product.objects.active_products() regardless of campaign
        status, since a campaign's product scope can reference a
        product that's since been deactivated/deleted by its dealer,
        independent of the campaign's own state.
        """
        base_qs = Product.objects.active_products()
        if campaign.applies_to == Campaign.AppliesTo.ALL_PRODUCTS:
            return base_qs
        if campaign.applies_to == Campaign.AppliesTo.SPECIFIC_PRODUCTS:
            return base_qs.filter(campaigns=campaign)
        if campaign.applies_to == Campaign.AppliesTo.SPECIFIC_CATEGORIES:
            return base_qs.filter(category__in=campaign.categories.all())
        if campaign.applies_to == Campaign.AppliesTo.SPECIFIC_BRANDS:
            return base_qs.filter(brand__in=campaign.brands.all())
        return base_qs.none()

    # ----------------------------------------------------------------
    # READ PATH — pure computation, no DB writes
    # ----------------------------------------------------------------

    @staticmethod
    def _base_price(product: Product) -> Decimal:
        """
        The price a campaign discounts from.

        Priority order:
          1. Product.brand_price (MRP) — when set, a campaign discount
             is computed off the MRP, not off the dealer's own
             selling_price/final_price. This is a deliberate policy
             choice: "20% off" on a flash-sale campaign means 20% off
             the manufacturer's listed price, matching how MRP-based
             discounts are normally advertised/understood by shoppers,
             rather than compounding on top of a markdown the dealer
             already applied.
          2. Product.final_price, then Product.selling_price — fallback
             for products with no brand_price recorded at all (blank/
             null MRP), so a campaign can still apply to them using
             whatever price the product actually has.

        NOTE: brand_price being present does NOT depend on
        is_brand_price_visible — that flag only controls whether the
        *customer-facing* MRP strike-through is shown on the product's
        own page. It does not gate whether campaign math uses it. If a
        product's MRP is meant to be entirely invisible/unused, leave
        Product.brand_price blank rather than relying on
        is_brand_price_visible=False to hide it from this calculation
        — a display flag was never meant to double as a pricing-logic
        switch, and campaigns would silently use the hidden MRP anyway
        as currently written.
        """
        if product.brand_price is not None:
            return product.brand_price
        return product.final_price if product.final_price is not None else product.selling_price

    @classmethod
    def get_effective_price(cls, product: Product) -> CampaignPriceResult:
        """
        Resolve the best applicable campaign(s) for a single product and
        return the resulting price. Resolution algorithm:

          1. Fetch active campaigns in scope for this product, ordered
             by -priority, -discount_value (Campaign.objects.for_product).
          2. If the top campaign is NOT stackable, it applies alone —
             done. This is also what happens when there's only one
             applicable campaign, stackable or not.
          3. If the top campaign IS stackable, keep applying campaigns
             in priority order, skipping any non-stackable ones, each
             computed against the running discounted price (so a 10%
             then a further 5% is 10% then 5% of what's left, not both
             off the original — avoids > 100% discount edge cases from
             naively summing percentages).

        The starting point for all of this is _base_price() — see that
        method's docstring: it's the MRP (brand_price) when the product
        has one, not selling_price/final_price.

        Never mutates `product` or writes to the DB — safe to call for
        every product on a listing page. For a full page of products,
        prefer get_effective_prices_bulk() to avoid N+1 queries.
        """
        base_price = cls._base_price(product)
        applicable = list(Campaign.objects.for_product(product))

        if not applicable:
            return CampaignPriceResult(product=product, original_price=base_price, final_price=base_price)

        top = applicable[0]
        if not top.is_stackable:
            final_price = top.discounted_price(base_price)
            return CampaignPriceResult(
                product=product,
                original_price=base_price,
                final_price=final_price,
                applied_campaigns=[top],
            )

        running_price = base_price
        applied: List[Campaign] = []
        for campaign in applicable:
            # Only campaigns that are themselves stackable get to
            # combine; a non-stackable campaign other than `top` never
            # participates once we're in stacking mode.
            if not campaign.is_stackable and campaign is not top:
                continue
            running_price = campaign.discounted_price(running_price)
            applied.append(campaign)

        return CampaignPriceResult(
            product=product,
            original_price=base_price,
            final_price=running_price,
            applied_campaigns=applied,
        )

    @classmethod
    def get_effective_prices_bulk(cls, products: Iterable[Product]) -> dict:
        """
        Same as get_effective_price() but resolves campaigns for many
        products with a bounded number of queries instead of one
        for_product() query per product. Returns {product.pk: CampaignPriceResult}.

        Strategy: pull all currently-active campaigns once (small table,
        campaigns are a handful to a few dozen rows even for a large
        catalog), then match them against each product in memory using
        the same scope rules as Campaign.objects.for_product().
        """
        products = list(products)
        if not products:
            return {}

        active_campaigns = list(
            Campaign.objects.active()
            .prefetch_related('products', 'categories', 'brands')
            .order_by('-priority', '-discount_value')
        )

        results = {}
        for product in products:
            applicable = [c for c in active_campaigns if cls._campaign_matches_product(c, product)]
            base_price = cls._base_price(product)

            if not applicable:
                results[product.pk] = CampaignPriceResult(
                    product=product, original_price=base_price, final_price=base_price
                )
                continue

            top = applicable[0]
            if not top.is_stackable:
                results[product.pk] = CampaignPriceResult(
                    product=product,
                    original_price=base_price,
                    final_price=top.discounted_price(base_price),
                    applied_campaigns=[top],
                )
                continue

            running_price = base_price
            applied = []
            for campaign in applicable:
                if not campaign.is_stackable and campaign is not top:
                    continue
                running_price = campaign.discounted_price(running_price)
                applied.append(campaign)

            results[product.pk] = CampaignPriceResult(
                product=product,
                original_price=base_price,
                final_price=running_price,
                applied_campaigns=applied,
            )

        return results

    @staticmethod
    def _campaign_matches_product(campaign: Campaign, product: Product) -> bool:
        if campaign.applies_to == Campaign.AppliesTo.ALL_PRODUCTS:
            return True
        if campaign.applies_to == Campaign.AppliesTo.SPECIFIC_PRODUCTS:
            return any(p.pk == product.pk for p in campaign.products.all())
        if campaign.applies_to == Campaign.AppliesTo.SPECIFIC_CATEGORIES:
            return product.category_id is not None and any(
                c.pk == product.category_id for c in campaign.categories.all()
            )
        if campaign.applies_to == Campaign.AppliesTo.SPECIFIC_BRANDS:
            return product.brand_id is not None and any(
                b.pk == product.brand_id for b in campaign.brands.all()
            )
        return False

    # ----------------------------------------------------------------
    # WRITE PATH — actually consuming a campaign at checkout
    # ----------------------------------------------------------------

    @classmethod
    @transaction.atomic
    def redeem(
        cls,
        campaign: Campaign,
        *,
        user,
        product: Optional[Product] = None,
        order=None,
        order_item=None,
        original_price: Decimal,
    ) -> CampaignRedemption:
        """
        Atomically consume one use of `campaign` for `user` and record a
        CampaignRedemption. Call this once per order line at the point
        an order is actually being placed — NOT during price preview
        (use get_effective_price for that), since every call here
        increments Campaign.times_used.

        `original_price` should be whatever get_effective_price()/
        get_effective_prices_bulk() reported as original_price for this
        product at preview time (i.e. the MRP it was computed against,
        per _base_price()) — this method doesn't re-derive it itself,
        since the caller is expected to pass through the exact price the
        customer was shown, not recompute it and risk a mismatch between
        preview and redemption if the product's MRP changed in between.

        Race-safety: locks the Campaign row (select_for_update) before
        re-checking usage_limit_total, so concurrent checkouts on a
        capped flash-sale campaign can't both pass a stale "is there
        room left" check and jointly overrun the cap.

        Raises ValidationError if the campaign is no longer applicable
        (expired/disabled/exhausted) or the user has hit
        usage_limit_per_user — callers should catch this and fall back
        to the non-discounted price rather than failing the whole order,
        since price previews can go stale between page load and checkout.
        """
        locked_campaign = Campaign.objects.select_for_update().get(pk=campaign.pk)

        if not locked_campaign.is_currently_active:
            raise ValidationError(_("This campaign is no longer active."))

        if locked_campaign.usage_limit_total and locked_campaign.times_used >= locked_campaign.usage_limit_total:
            raise ValidationError(_("This campaign has reached its usage limit."))

        if user is not None and locked_campaign.usage_limit_per_user:
            user_redemption_count = CampaignRedemption.objects.filter(
                campaign=locked_campaign, user=user
            ).count()
            if user_redemption_count >= locked_campaign.usage_limit_per_user:
                raise ValidationError(_("You have already used this campaign the maximum number of times."))

        discount_amount = locked_campaign.compute_discount(original_price)
        final_price = (original_price - discount_amount).quantize(Decimal('0.01'))

        Campaign.objects.filter(pk=locked_campaign.pk).update(
            times_used=locked_campaign.times_used + 1
        )

        return CampaignRedemption.objects.create(
            campaign=locked_campaign,
            user=user,
            product=product,
            order=order,
            order_item=order_item,
            original_price=original_price,
            discount_amount=discount_amount,
            final_price=final_price,
        )