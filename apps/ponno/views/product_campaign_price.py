# apps/ponno/views/product_campaign_price.py

from django.http import JsonResponse
from django.shortcuts import get_object_or_404
from django.views.decorators.http import require_GET

from apps.ponno.models.product import Product
from apps.ponno.services.campaign_pricing import CampaignPricingService


@require_GET
def _product_campaign_price(request, slug):
    """
    GET /product/<slug>/campaign-price/

    JSON endpoint for the product detail page to fetch/refresh the
    resolved campaign price client-side (e.g. for a countdown-timer
    flash-sale banner that re-checks price near expiry), mirroring
    AjaxSearchView's plain JsonResponse pattern rather than DRF, since
    the rest of this app's user-facing surface is plain Django views.
    """
    product = get_object_or_404(Product.objects.active_products(), slug=slug)
    result = CampaignPricingService.get_effective_price(product)

    return JsonResponse({
        'product_id': str(product.product_id),
        'original_price': str(result.original_price),
        'final_price': str(result.final_price),
        'total_discount': str(result.total_discount),
        'has_discount': result.has_discount,
        'applied_campaigns': [
            {
                'campaign_id': str(c.campaign_id),
                'name': c.name,
                'slug': c.slug,
                'campaign_type': c.campaign_type,
                'discount_value': str(c.discount_value),
                'end_at': c.end_at.isoformat() if c.end_at else None,
            }
            for c in result.applied_campaigns
        ],
    })


ProductCampaignPriceView = _product_campaign_price