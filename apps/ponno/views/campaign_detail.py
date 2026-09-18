from django.http import Http404
from django.shortcuts import get_object_or_404
from django.views.generic import DetailView

from apps.ponno.models.campaign import Campaign
from apps.ponno.models.product import Product
from apps.ponno.services.campaign_pricing import CampaignPricingService


class _CampaignDetailView(DetailView):
    """
    Campaign landing page: the campaign itself plus the products it
    currently discounts, each annotated with its resolved campaign
    price via CampaignPricingService.get_effective_prices_bulk() (one
    bulk resolution for the whole page rather than one query per
    product card).

    Accessible even after a campaign has ended (status shows ENDED in
    that case) rather than 404ing, so shared links / SEO don't break
    the moment a flash sale closes — mirrors ProductDetailView not
    hiding soft-deleted-adjacent state, just reflecting it.

    Archived campaigns are the one exception and DO 404: is_archived is
    Campaign's soft-delete flag (see Campaign.archive() / the
    CampaignManager.archived() admin-only manager method) — the model's
    own docstring says archived rows are "hidden from normal admin
    lists", and every other soft-deletable model in this app (Product,
    Category, Brand) is likewise excluded from its public detail view
    via an active_*() queryset. get_queryset() below does the same for
    Campaign rather than falling through to Campaign.objects.all().

    The product listing itself is only populated while the campaign is
    BOTH actually ACTIVE per Campaign.status (i.e. enabled, approved,
    inside its start/end window, and not exhausted by usage or budget —
    see Campaign.is_currently_active / Campaign.active()) AND allowed on
    this surface per Campaign.channel. This template renders the public
    web storefront, so a campaign scoped to channel=POS or channel=APP
    must never show its discounted products here even while otherwise
    ACTIVE — POS/APP campaigns are deliberately not meant to be
    web-visible (see the staff-only-POS restriction in
    CampaignCreateView). A SCHEDULED, PAUSED, ENDED, CANCELLED, or
    wrong-channel campaign still renders its own page (name, banner,
    terms, status badge), but with an empty product list rather than a
    stale or misleading set of "discounted" products. This also avoids
    a needless bulk pricing query in either case.

    Product scope resolution (which products belong to this campaign at
    all, per its applies_to) is delegated to
    CampaignPricingService.products_for_campaign() rather than
    reimplemented here — see that method's docstring; this used to be a
    local staticmethod on this class, factored out so the owner-branch
    detail view in apps/customer/views/campaign_profile.py resolves
    scope the exact same way instead of drifting (which it previously
    had — see that file's history).

    Products are further restricted to Product.objects.active_products()
    (is_active=True, not soft-deleted) regardless of campaign status,
    since a campaign scope can reference a product that's since been
    deactivated or deleted by its dealer. This is handled inside
    products_for_campaign() itself.

    Access control: intentionally none. No LoginRequiredMixin, no
    UserPassesTestMixin, no role/ownership check anywhere in this view
    — anonymous visitors, customers, dealers, and staff all get the
    exact same page and the exact same product-listing logic (gated
    only by campaign status/channel above, never by who's asking). A
    campaign's landing page is public marketing surface, same as a
    product detail page; do not add a role check here without deciding
    that on purpose, since it would silently change who can open a
    shared campaign link.
    """

    model = Campaign
    template_name = 'ponno/campaign/campaign_detail.html'
    context_object_name = 'campaign'
    slug_url_kwarg = 'slug'

    def get_queryset(self):
        # Excludes archived (soft-deleted) campaigns — see class
        # docstring. Staff-facing tooling that needs to reach an
        # archived campaign (audit trail, restore flow, etc.) should
        # query Campaign.objects.archived() directly rather than
        # through this public view.
        return Campaign.objects.filter(is_archived=False)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        campaign = self.object

        is_currently_active = campaign.is_currently_active
        context['campaign_is_active'] = is_currently_active

        # A campaign can be genuinely ACTIVE (schedule/usage/budget/
        # approval all satisfied) while still being off-limits for this
        # surface — e.g. a POS-only flash sale is fully live at the
        # register but has no business appearing on the public website.
        # campaign_is_active above stays a true reflection of
        # Campaign.status for any "Live now" badge etc.; this second,
        # narrower flag is what actually gates the product listing.
        is_visible_on_web = is_currently_active and campaign.channel in (
            Campaign.Channel.ALL,
            Campaign.Channel.WEB,
        )
        context['campaign_visible_on_web'] = is_visible_on_web

        if is_visible_on_web:
            products = CampaignPricingService.products_for_campaign(campaign)
            price_results = CampaignPricingService.get_effective_prices_bulk(products)
            context['products_with_prices'] = [
                {'product': p, 'price': price_results[p.pk]} for p in products
            ]
        else:
            context['products_with_prices'] = []

        return context


CampaignDetailView = _CampaignDetailView.as_view()