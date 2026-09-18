# apps/ponno/views/campaign_list.py

from datetime import timedelta

from django.utils import timezone
from django.views.generic import ListView

from apps.ponno.models.campaign import Campaign


class _CampaignListView(ListView):
    """
    Storefront listing of currently-active campaigns (flash sales /
    promos), newest/highest-priority first. Mirrors BrandListView /
    CategoryListView's plain ListView pattern.

    Also feeds the header stat pills (`ending_soon_count`,
    `stackable_count`) shown above the grid — these are computed across
    *all* active campaigns, not just the current page, so pagination
    doesn't make the numbers wobble.
    """

    model = Campaign
    template_name = 'ponno/campaign/campaign_list.html'
    context_object_name = 'campaigns'
    paginate_by = 24

    #: window used to flag a campaign as "ending soon" for the header stat pill
    ending_soon_window = timedelta(hours=24)

    def get_queryset(self):
        return Campaign.objects.active().order_by('-priority', 'end_at')

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)

        active_qs = Campaign.objects.active()
        now = timezone.now()

        context['ending_soon_count'] = active_qs.filter(
            end_at__isnull=False,
            end_at__gt=now,
            end_at__lte=now + self.ending_soon_window,
        ).count()
        context['stackable_count'] = active_qs.filter(is_stackable=True).count()

        return context


CampaignListView = _CampaignListView.as_view()