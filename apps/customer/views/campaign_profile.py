# apps/customer/views/campaign_profile.py

"""
Single-entry campaign profile view for the logged-in user. One URL,
one callable, branched by role:

  - Dealer / Staff / Admin ("owner" view): full campaign management —
    every action (list, create, edit, enable, disable, cancel,
    submit-for-approval, restore, archive, duplicate, redemption log,
    redemption CSV export) is dispatched internally off an `action`
    value, the same way ProfileManagerView consolidates related
    profile actions behind one route.
  - Everyone else ("customer" view): a read-only dashboard of their
    own redemption history plus a feed of currently-active, store-wide
    campaigns. No create/edit machinery — customers don't own
    campaigns, they redeem them.

Both branches render around the same ProfileInfo (avatar, bio,
verification badge, etc.), fetched once per request and merged into
every context via _with_profile(), so profile chrome stays identical
regardless of which branch a given user lands in.

URL wiring (add to apps/customer/web_urls.py):

    from apps.customer.views.campaign_profile import CampaignDetailView, CampaignProfileView

    urlpatterns = [
        ...
        path('campaign_profile/', CampaignProfileView, name='campaign_profile'),
        path('campaign/<slug:slug>/', CampaignDetailView, name='campaign_detail'),
    ]

Usage from templates / links (owner view):

    GET  /customer/campaign_profile/                              -> list
    GET  /customer/campaign_profile/?action=create                -> create form
    GET  /customer/campaign_profile/?action=edit&pk=3              -> edit form
    GET  /customer/campaign/eid-flash-sale/                        -> detail
    GET  /customer/campaign_profile/?action=redemptions&pk=3        -> audit log
    GET  /customer/campaign_profile/?action=redemptions_export&pk=3 -> CSV
    POST /customer/campaign_profile/  (action=create|edit|enable|disable|
         cancel|submit_for_approval|restore|archive|duplicate, pk=<id>)

Non-owner users hitting any of the above simply get the customer
dashboard instead (GET) or a permission error redirect (POST) — the
`action`/`pk` query params are only meaningful for the owner branch.

Note on identifiers: `detail` is its own URL, keyed by `slug` in the
path (`campaign/<slug:slug>/`, matching the public apps/ponno
campaign_detail.py convention and what the detail template's internal
links/breadcrumbs expect) and served by CampaignDetailView, a separate
view function rather than an `action=` branch of CampaignProfileView.
Every other action (`edit`, `redemptions`, `redemptions_export`, and
the mutating POST actions) stays on campaign_profile/ and keys off
`pk`. Use `_detail_url(slug)` — not `_self_url()` — to build a link to
the detail page.

Ownership: every campaign the owner branch can see, edit, or act on is
filtered to `created_by=request.user` (admins see everything via
_owner_queryset — note this currently checks `is_admin`, not
`is_staff`, so a plain STAFF-role user who isn't also a superuser only
ever sees their own campaigns, same as a dealer). That scoping is the
main defense against one dealer reaching another dealer's campaign by
guessing a pk/slug.

No forms.py — same convention as apps/ponno/views/campaign_create.py
and campaign_edit.py. request.POST/request.FILES are parsed directly
via the shared apps.ponno.views._campaign_input helpers, and the
Campaign instance is validated by its own .clean()/full_clean() (run
inside Campaign.save()). Errors collected here are a mix of parsing
errors (bad decimal/int/datetime string), caught before ever touching
the model, and django.core.exceptions.ValidationError raised by
full_clean(), normalized into the same {field: [messages]} shape via
_normalize_validation_error() so the template doesn't need to
special-case where an error came from. See campaign_create.py's module
docstring for the full story on why '__all__' is renamed to
'non_field' before it ever reaches the template.

Scope permission mirrors campaign_create.py / campaign_edit.py exactly:
non-staff dealers may only create/edit campaigns scoped to
applies_to=SPECIFIC_PRODUCTS, restricted server-side to products where
Product.dealer == request.user — never trust a client-submitted
applies_to or product id list past that check just because the form
hid the other options. The same "staff-only" treatment now also
applies to channel=POS, matching apps/ponno/views/campaign_create.py
and campaign_edit.py.

Product scope resolution for the detail view (which products a
campaign's applies_to actually covers) is delegated to
CampaignPricingService.products_for_campaign() rather than
reimplemented here — see that method's docstring in
apps/ponno/services/campaign_pricing.py. This used to be inlined in
_detail_context() below and only ever handled SPECIFIC_PRODUCTS scope,
silently returning no products for ALL_PRODUCTS/SPECIFIC_CATEGORIES/
SPECIFIC_BRANDS campaigns — fixed by sharing the same resolution logic
apps/ponno/views/campaign_detail.py (the public landing page) uses.

Templates:
    customer/campaign/campaign_form.html              (create/edit —
                                                          same template
                                                          apps/ponno's
                                                          own campaign
                                                          views use)
    customer/campaign/campaign_detail.html             (detail — same
                                                          template and
                                                          same context
                                                          key,
                                                          products_with_prices,
                                                          that
                                                          apps/ponno/views/campaign_detail.py
                                                          renders)
    customer/campaign/campaign_redemption_list.html    (redemption log)
    personal/campaign_profile.html                     (owner list AND
                                                          customer
                                                          dashboard —
                                                          distinguished
                                                          by `mode` in
                                                          context;
                                                          matches the
                                                          personal/
                                                          template dir
                                                          convention)
"""

import csv
import logging
from decimal import Decimal

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.core.paginator import EmptyPage, PageNotAnInteger, Paginator
from django.db import transaction
from django.db.models import F, Q, Sum
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.customer.models.account import User
from apps.customer.models.profile_info import ProfileInfo
from apps.ponno.models.brand import Brand
from apps.ponno.models.campaign import Campaign, CampaignRedemption
from apps.ponno.models.category import Category
from apps.ponno.models.product import Product
from apps.ponno.services.campaign_pricing import CampaignPricingService
from apps.ponno.views._campaign_input import (
    parse_bool,
    parse_datetime_local,
    parse_decimal,
    parse_id_list,
    parse_int,
)

logger = logging.getLogger(__name__)

ACTIVE_FEED_LIMIT = 10
CUSTOMER_REDEMPTIONS_PER_PAGE = 20


# ====================================================================
# STATUS FILTERS
# --------------------------------------------------------------------
# Campaign.status is a derived Python property, not a DB column, so it
# can't be filtered with .filter(status=...). These mirror the exact
# same precedence rules as the model's status property at the
# queryset level, so list filtering stays one indexed DB query instead
# of evaluating every row in Python before paginating.
# ====================================================================

def _filter_draft(qs):
    return qs.filter(is_archived=False, is_cancelled=False, is_enabled=False, first_activated_at__isnull=True)


def _filter_paused(qs):
    return qs.filter(is_archived=False, is_cancelled=False, is_enabled=False, first_activated_at__isnull=False)


def _filter_pending_approval(qs):
    return qs.filter(is_archived=False, is_cancelled=False, approval_status=Campaign.ApprovalStatus.PENDING)


def _filter_rejected(qs):
    return qs.filter(is_archived=False, is_cancelled=False, approval_status=Campaign.ApprovalStatus.REJECTED)


def _filter_scheduled(qs):
    now = timezone.now()
    return qs.filter(
        is_archived=False, is_cancelled=False, is_enabled=True,
        approval_status__in=[Campaign.ApprovalStatus.APPROVED, Campaign.ApprovalStatus.NOT_REQUIRED],
        start_at__gt=now,
    )


def _exhausted_q():
    return (
        (Q(usage_limit_total__gt=0) & Q(times_used__gte=F('usage_limit_total')))
        | (Q(budget_limit__isnull=False) & Q(budget_spent__gte=F('budget_limit')))
    )


def _filter_active(qs):
    now = timezone.now()
    return qs.filter(
        is_archived=False, is_cancelled=False, is_enabled=True,
        approval_status__in=[Campaign.ApprovalStatus.APPROVED, Campaign.ApprovalStatus.NOT_REQUIRED],
        start_at__lte=now,
    ).filter(Q(end_at__isnull=True) | Q(end_at__gte=now)).exclude(_exhausted_q())


def _filter_ended(qs):
    now = timezone.now()
    base = qs.filter(
        is_archived=False, is_cancelled=False, is_enabled=True,
        approval_status__in=[Campaign.ApprovalStatus.APPROVED, Campaign.ApprovalStatus.NOT_REQUIRED],
        start_at__lte=now,
    )
    return base.filter(Q(end_at__lt=now) | _exhausted_q())


def _filter_cancelled(qs):
    return qs.filter(is_cancelled=True, is_archived=False)


def _filter_archived(qs):
    return qs.filter(is_archived=True)


STATUS_FILTERS = {
    'draft': _filter_draft,
    'paused': _filter_paused,
    'pending_approval': _filter_pending_approval,
    'rejected': _filter_rejected,
    'scheduled': _filter_scheduled,
    'active': _filter_active,
    'ended': _filter_ended,
    'cancelled': _filter_cancelled,
    'archived': _filter_archived,
}

# Fields that change what the campaign actually does — editing any of
# these on a previously-approved campaign pulls it back into review.
# This set is the single source of truth for that rule; _handle_edit's
# `touched_sensitive_field` diff below is built to match it exactly
# (previously the diff was hand-rolled and had drifted from this set —
# channel/currency/budget_limit were declared sensitive here but never
# actually checked in the diff).
_SENSITIVE_FIELDS = {
    'campaign_type', 'discount_value', 'max_discount_amount', 'min_order_amount',
    'applies_to', 'products', 'categories', 'brands', 'channel', 'currency',
    'start_at', 'end_at', 'usage_limit_total', 'usage_limit_per_user', 'budget_limit',
}


# ====================================================================
# PROFILE (shared chrome for both the owner and customer branches)
# ====================================================================

def _get_or_create_profile(user):
    """
    Fetch (or lazily create) the caller's ProfileInfo. Called once per
    request from the top of CampaignProfileView and stashed on
    request._campaign_profile — see _with_profile() below — so neither
    branch re-queries it per render call.
    """
    profile, created = ProfileInfo.objects.select_related('user').get_or_create(user=user)
    if created:
        logger.info(
            "Auto-created ProfileInfo for user_id=%s on first campaign_profile visit",
            user.pk,
        )
    return profile


def _with_profile(request, context):
    """
    Merge the request-scoped ProfileInfo into a render context. Every
    template this view renders — the dealer management screens and
    the customer dashboard alike — can then show the same profile
    chrome (avatar, bio, verification badge) without each render call
    needing its own DB round trip.
    """
    return {
        **context,
        'profile': getattr(request, '_campaign_profile', None),
        'profile_user': request.user,
    }


# ====================================================================
# CUSTOMER DASHBOARD (non-owner branch — read only)
# ====================================================================

def _build_customer_context(request, user):
    """
    Non-dealer/staff/admin branch: the caller doesn't manage
    campaigns, so instead of the create/edit/detail/action machinery
    below, they get their own redemption history plus a feed of
    currently-active, store-wide (applies_to=ALL_PRODUCTS) campaigns.

    Scoped to ALL_PRODUCTS deliberately: a feed that also resolves
    per-product/category/brand scope against this user's order or
    browsing history belongs in a dedicated recommendation service,
    not this view.
    """
    redemptions_qs = (
        CampaignRedemption.objects.filter(user=user)
        .select_related('campaign', 'product', 'order')
        .order_by('-redeemed_at')
    )
    total_saved = redemptions_qs.aggregate(total=Sum('discount_amount'))['total'] or Decimal('0.00')

    active_campaigns = list(
        Campaign.objects.active()
        .filter(applies_to=Campaign.AppliesTo.ALL_PRODUCTS)
        .order_by('-priority', '-discount_value')[:ACTIVE_FEED_LIMIT]
    )

    return {
        'mode': 'customer',
        'redemptions': _paginate(request, redemptions_qs, per_page=CUSTOMER_REDEMPTIONS_PER_PAGE),
        'total_saved': total_saved,
        'active_campaigns': active_campaigns,
    }


# ====================================================================
# HELPERS (owner branch)
# ====================================================================

def _owner_queryset(user):
    if getattr(user, 'is_admin', False):
        return Campaign.objects.all()
    return Campaign.objects.filter(created_by=user)


def _self_url(action=None, pk=None):
    url = reverse('customer:campaign_profile')
    params = []
    if action:
        params.append(f"action={action}")
    if pk:
        params.append(f"pk={pk}")
    return f"{url}?{'&'.join(params)}" if params else url


def _detail_url(slug):
    """
    Build a link to the standalone detail route (campaign/<slug>/),
    served by CampaignDetailView — not an `action=` branch of
    CampaignProfileView, so it isn't built with _self_url().
    """
    return reverse('customer:campaign_detail', kwargs={'slug': slug})


def _detail_context(campaign):
    """
    Context for customer/campaign/campaign_detail.html — the exact
    same template and the exact same context key
    (products_with_prices) that apps/ponno/views/campaign_detail.py
    renders for the public landing page, so both views can share one
    template. Product scope is resolved via
    CampaignPricingService.products_for_campaign() rather than
    reimplemented here — see that method's docstring.

    Unlike the public view, this isn't gated on
    campaign.is_currently_active or campaign.channel — the owner
    should be able to preview scoped products for a draft/paused/
    POS-only campaign too, not just a live web-visible one.
    """
    sample_products = list(CampaignPricingService.products_for_campaign(campaign)[:12])
    priced_products = CampaignPricingService.get_effective_prices_bulk(sample_products) if sample_products else {}

    return {
        'mode': 'detail',
        'campaign': campaign,
        'products_with_prices': [
            {'product': p, 'price': priced_products[p.pk]} for p in sample_products
        ],
        'recent_redemptions': campaign.redemptions.select_related('user', 'product').order_by('-redeemed_at')[:10],
        'redemption_count': campaign.times_used,
        'remaining_uses': campaign.remaining_uses,
        'remaining_budget': campaign.remaining_budget,
        'can_edit': not campaign.is_archived and not campaign.is_cancelled,
        'can_submit_for_approval': (
            not campaign.is_archived and not campaign.is_cancelled
            and campaign.approval_status == Campaign.ApprovalStatus.REJECTED
        ),
        'can_enable': (
            not campaign.is_archived and not campaign.is_cancelled and not campaign.is_enabled
            and campaign.approval_status in (Campaign.ApprovalStatus.APPROVED, Campaign.ApprovalStatus.NOT_REQUIRED)
        ),
        'can_disable': not campaign.is_archived and not campaign.is_cancelled and campaign.is_enabled,
        'can_cancel': not campaign.is_archived and not campaign.is_cancelled,
        'can_restore': campaign.is_archived,
        'can_archive': not campaign.is_archived,
    }


def _paginate(request, queryset, per_page=20):
    paginator = Paginator(queryset, per_page)
    page_number = request.GET.get('page')
    try:
        return paginator.page(page_number)
    except PageNotAnInteger:
        return paginator.page(1)
    except EmptyPage:
        return paginator.page(paginator.num_pages)


def _normalize_validation_error(exc: ValidationError) -> dict:
    """
    Normalize a django ValidationError into a plain {field: [messages]}
    dict for the template. Identical to the helper of the same name in
    apps/ponno/views/campaign_create.py and campaign_edit.py — kept as
    its own copy here (rather than imported) since this view's error
    dict flows through messages.error()-free template rendering, same
    shape, same reason: '__all__' can't be resolved in a Django
    template (leading underscore is forbidden), so it's renamed to
    'non_field' once, here, before the template ever sees it.
    """
    if hasattr(exc, 'error_dict'):
        return {
            ('non_field' if field == '__all__' else field): [str(m) for m in msgs]
            for field, msgs in exc.message_dict.items()
        }
    return {'non_field': exc.messages}


def _scoped_products_queryset(user):
    """Non-staff dealers only ever see/select their own products."""
    qs = Product.objects.active_products()
    if not user.is_staff:
        qs = qs.filter(dealer=user)
    return qs


def _form_context(request, *, mode, campaign=None, data=None, errors=None):
    """
    Build context for customer/campaign/campaign_form.html — same shape
    as apps/ponno/views/campaign_create.py._context() /
    campaign_edit.py._context(), so the template can be shared as-is
    between the standalone ponno campaign views and this consolidated
    dealer-profile view.
    """
    user = request.user
    products_qs = _scoped_products_queryset(user)

    if data is not None:
        # Redisplaying after a validation error: echo back exactly what
        # was submitted so the user doesn't lose their edits.
        initial = {
            'campaign_code': data.get('campaign_code', ''),
            'name': data.get('name', ''),
            'description': data.get('description', ''),
            'terms_conditions': data.get('terms_conditions', ''),
            'campaign_type': data.get('campaign_type', ''),
            'discount_value': data.get('discount_value', ''),
            'max_discount_amount': data.get('max_discount_amount', ''),
            'min_order_amount': data.get('min_order_amount', ''),
            'budget_limit': data.get('budget_limit', ''),
            'currency': data.get('currency', ''),
            'priority': data.get('priority', ''),
            'applies_to': data.get('applies_to', ''),
            'channel': data.get('channel', ''),
            'start_at': data.get('start_at', ''),
            'end_at': data.get('end_at', ''),
            'usage_limit_total': data.get('usage_limit_total', ''),
            'usage_limit_per_user': data.get('usage_limit_per_user', ''),
            'is_stackable': parse_bool(data.get('is_stackable')),
            'is_enabled': parse_bool(data.get('is_enabled')),
        }
        selected_products = set(data.getlist('products'))
        selected_categories = set(data.getlist('categories'))
        selected_brands = set(data.getlist('brands'))
    elif campaign is not None:
        # First load of an edit form: prefill from the campaign.
        initial = {
            'campaign_code': campaign.campaign_code or '',
            'name': campaign.name,
            'description': campaign.description or '',
            'terms_conditions': campaign.terms_conditions or '',
            'campaign_type': campaign.campaign_type,
            'discount_value': campaign.discount_value if campaign.discount_value is not None else '',
            'max_discount_amount': campaign.max_discount_amount if campaign.max_discount_amount is not None else '',
            'min_order_amount': campaign.min_order_amount if campaign.min_order_amount is not None else '',
            'budget_limit': campaign.budget_limit if campaign.budget_limit is not None else '',
            'currency': campaign.currency,
            'priority': campaign.priority,
            'applies_to': campaign.applies_to,
            'channel': campaign.channel,
            'start_at': campaign.start_at.strftime('%Y-%m-%dT%H:%M') if campaign.start_at else '',
            'end_at': campaign.end_at.strftime('%Y-%m-%dT%H:%M') if campaign.end_at else '',
            'usage_limit_total': campaign.usage_limit_total,
            'usage_limit_per_user': campaign.usage_limit_per_user,
            'is_stackable': campaign.is_stackable,
            'is_enabled': campaign.is_enabled,
        }
        selected_products = set(str(pk) for pk in campaign.products.values_list('pk', flat=True))
        selected_categories = set(str(pk) for pk in campaign.categories.values_list('pk', flat=True))
        selected_brands = set(str(pk) for pk in campaign.brands.values_list('pk', flat=True))
    else:
        # Fresh create form.
        initial = {}
        selected_products = set()
        selected_categories = set()
        selected_brands = set()

    return {
        'mode': mode,
        'campaign': campaign,
        'campaign_types': Campaign.CampaignType.choices,
        'applies_to_choices': Campaign.AppliesTo.choices,
        'channel_choices': Campaign.Channel.choices,
        'products': products_qs,
        'categories': Category.objects.all(),
        'brands': Brand.objects.all(),
        'is_staff': user.is_staff,
        'data': initial,
        'selected_products': selected_products,
        'selected_categories': selected_categories,
        'selected_brands': selected_brands,
        'banner_image_url': campaign.banner_image.url if campaign and campaign.banner_image else '',
        # Read-only display fields — never written by this form. These
        # belong to dedicated transitions/counters on the model
        # (approve()/reject(), register_redemption()), not a general
        # edit form; see campaign_edit.py's module docstring for the
        # same rule applied to the standalone ponno edit view.
        'approval_status': campaign.approval_status if campaign else None,
        'times_used': campaign.times_used if campaign else None,
        'budget_spent': campaign.budget_spent if campaign else None,
        'errors': errors or {},
    }


# ====================================================================
# ACTION HANDLERS (POST) — each returns an HttpResponse
# ====================================================================

def _handle_create(request):
    """
    Manual-parse version of apps/ponno/views/campaign_create.py's post(),
    with two additions on top of that view's behavior: ownership is
    always set to request.user (this route only ever creates the
    caller's own campaign), and approval_status is decided by
    governance rules — staff self-approve, dealers always enter the
    review queue — rather than accepted from the client.
    """
    user = request.user
    data = request.POST
    errors = {}

    name = (data.get('name') or '').strip()
    if not name:
        errors.setdefault('name', []).append("Name is required.")

    campaign_code = (data.get('campaign_code') or '').strip() or None
    if campaign_code and Campaign.objects.filter(campaign_code=campaign_code).exists():
        errors.setdefault('campaign_code', []).append("This campaign code is already in use.")

    campaign_type = data.get('campaign_type') or Campaign.CampaignType.PERCENTAGE
    if campaign_type not in Campaign.CampaignType.values:
        errors.setdefault('campaign_type', []).append("Invalid campaign type.")

    discount_value, err = parse_decimal(data.get('discount_value'), required=True, field_label="Discount value")
    if err:
        errors.setdefault('discount_value', []).append(err)

    max_discount_amount, err = parse_decimal(data.get('max_discount_amount'), field_label="Max discount amount")
    if err:
        errors.setdefault('max_discount_amount', []).append(err)

    min_order_amount, err = parse_decimal(data.get('min_order_amount'), field_label="Minimum order amount")
    if err:
        errors.setdefault('min_order_amount', []).append(err)

    budget_limit, err = parse_decimal(data.get('budget_limit'), field_label="Budget limit")
    if err:
        errors.setdefault('budget_limit', []).append(err)

    currency = (data.get('currency') or 'BDT').strip().upper()
    if len(currency) != 3:
        errors.setdefault('currency', []).append("Currency must be a 3-letter ISO 4217 code.")

    channel = data.get('channel') or Campaign.Channel.ALL
    if channel not in Campaign.Channel.values:
        errors.setdefault('channel', []).append("Invalid channel.")

    if not user.is_staff and channel == Campaign.Channel.POS:
        errors.setdefault('channel', []).append("Only staff can create POS campaigns.")

    applies_to = data.get('applies_to') or Campaign.AppliesTo.SPECIFIC_PRODUCTS
    if applies_to not in Campaign.AppliesTo.values:
        errors.setdefault('applies_to', []).append("Invalid scope.")

    if not user.is_staff and applies_to != Campaign.AppliesTo.SPECIFIC_PRODUCTS:
        errors.setdefault('applies_to', []).append(
            "Only staff can create campaigns scoped beyond specific products."
        )

    product_ids = parse_id_list(data, 'products')
    category_ids = parse_id_list(data, 'categories')
    brand_ids = parse_id_list(data, 'brands')

    if applies_to == Campaign.AppliesTo.SPECIFIC_PRODUCTS and not product_ids:
        errors.setdefault('products', []).append("Select at least one product for this scope.")
    if applies_to == Campaign.AppliesTo.SPECIFIC_CATEGORIES and not category_ids:
        errors.setdefault('categories', []).append("Select at least one category for this scope.")
    if applies_to == Campaign.AppliesTo.SPECIFIC_BRANDS and not brand_ids:
        errors.setdefault('brands', []).append("Select at least one brand for this scope.")

    start_at, err = parse_datetime_local(data.get('start_at'), required=True, field_label="Start date")
    if err:
        errors.setdefault('start_at', []).append(err)

    end_at, err = parse_datetime_local(data.get('end_at'), field_label="End date")
    if err:
        errors.setdefault('end_at', []).append(err)

    usage_limit_total, err = parse_int(data.get('usage_limit_total'), default=0, field_label="Total usage limit")
    if err:
        errors.setdefault('usage_limit_total', []).append(err)

    usage_limit_per_user, err = parse_int(data.get('usage_limit_per_user'), default=0, field_label="Per-user usage limit")
    if err:
        errors.setdefault('usage_limit_per_user', []).append(err)

    priority, err = parse_int(data.get('priority'), default=0, field_label="Priority")
    if err:
        errors.setdefault('priority', []).append(err)

    is_stackable = parse_bool(data.get('is_stackable'))
    is_enabled = parse_bool(data.get('is_enabled'))

    # Restrict a non-staff dealer's product selection to their own
    # catalog server-side — a tampered POST listing another dealer's
    # product id must not be honored just because the client sent it.
    products_qs = Product.objects.all()
    if not user.is_staff:
        products_qs = products_qs.filter(dealer=user)
    products = list(products_qs.filter(pk__in=product_ids)) if product_ids else []
    if product_ids and len(products) != len(set(product_ids)):
        errors.setdefault('products', []).append(
            "One or more selected products are invalid or not yours."
        )

    categories = list(Category.objects.filter(pk__in=category_ids)) if category_ids else []
    brands = list(Brand.objects.filter(pk__in=brand_ids)) if brand_ids else []

    if errors:
        return render(
            request, 'ponno/campaign/campaign_form.html',
            _with_profile(request, _form_context(request, mode='create', data=data, errors=errors)),
            status=400,
        )

    campaign = Campaign(
        campaign_code=campaign_code,
        name=name,
        description=data.get('description') or None,
        terms_conditions=data.get('terms_conditions') or None,
        campaign_type=campaign_type,
        discount_value=discount_value,
        max_discount_amount=max_discount_amount,
        min_order_amount=min_order_amount,
        budget_limit=budget_limit,
        currency=currency,
        applies_to=applies_to,
        channel=channel,
        start_at=start_at,
        end_at=end_at,
        usage_limit_total=usage_limit_total,
        usage_limit_per_user=usage_limit_per_user,
        priority=priority,
        is_stackable=is_stackable,
        is_enabled=is_enabled,
        created_by=user,
        updated_by=user,
    )

    # Governance: only staff can self-approve. A dealer's own campaign
    # always enters the approval queue, regardless of is_enabled.
    if user.is_staff:
        campaign.approval_status = Campaign.ApprovalStatus.NOT_REQUIRED
    else:
        campaign.approval_status = Campaign.ApprovalStatus.PENDING
        campaign.submitted_for_approval_at = timezone.now()

    if request.FILES.get('banner_image'):
        campaign.banner_image = request.FILES['banner_image']

    try:
        with transaction.atomic():
            campaign.save()  # runs generate_slug() / full_clean() internally
            if products:
                campaign.products.set(products)
            if categories:
                campaign.categories.set(categories)
            if brands:
                campaign.brands.set(brands)
    except ValidationError as exc:
        errors = _normalize_validation_error(exc)
        return render(
            request, 'customer/campaign/campaign_form.html',
            _with_profile(request, _form_context(request, mode='create', data=data, errors=errors)),
            status=400,
        )

    if campaign.approval_status == Campaign.ApprovalStatus.PENDING:
        messages.success(
            request,
            _("Campaign \u201c%(name)s\u201d created and submitted for approval. "
              "It won't go live until staff approves it.") % {'name': campaign.name},
        )
    else:
        messages.success(request, _("Campaign \u201c%(name)s\u201d created.") % {'name': campaign.name})
    return redirect(_detail_url(campaign.slug))


def _handle_edit(request, campaign):
    """
    Manual-parse version of apps/ponno/views/campaign_edit.py's post(),
    with the same "sensitive edit on an approved campaign resets
    approval + pauses it" governance rule this view already had, now
    driven off a before/after field diff that matches _SENSITIVE_FIELDS
    exactly (channel/currency/budget_limit are now actually checked,
    not just declared).
    """
    if campaign.is_archived or campaign.is_cancelled:
        messages.error(request, _("This campaign can no longer be edited."))
        return redirect(_detail_url(campaign.slug))

    user = request.user
    data = request.POST
    errors = {}

    name = (data.get('name') or '').strip()
    if not name:
        errors.setdefault('name', []).append("Name is required.")

    campaign_code = data.get('campaign_code')
    campaign_code = (campaign_code.strip() or None) if campaign_code is not None else campaign.campaign_code
    if campaign_code and Campaign.objects.filter(campaign_code=campaign_code).exclude(pk=campaign.pk).exists():
        errors.setdefault('campaign_code', []).append("This campaign code is already in use.")

    campaign_type = data.get('campaign_type') or campaign.campaign_type
    if campaign_type not in Campaign.CampaignType.values:
        errors.setdefault('campaign_type', []).append("Invalid campaign type.")

    discount_value, err = parse_decimal(data.get('discount_value'), required=True, field_label="Discount value")
    if err:
        errors.setdefault('discount_value', []).append(err)

    max_discount_amount, err = parse_decimal(data.get('max_discount_amount'), field_label="Max discount amount")
    if err:
        errors.setdefault('max_discount_amount', []).append(err)

    min_order_amount, err = parse_decimal(data.get('min_order_amount'), field_label="Minimum order amount")
    if err:
        errors.setdefault('min_order_amount', []).append(err)

    budget_limit, err = parse_decimal(data.get('budget_limit'), field_label="Budget limit")
    if err:
        errors.setdefault('budget_limit', []).append(err)
    elif budget_limit is not None and budget_limit < campaign.budget_spent:
        errors.setdefault('budget_limit', []).append(
            f"Budget limit cannot be lower than the amount already spent ({campaign.budget_spent})."
        )

    currency = (data.get('currency') or campaign.currency or 'BDT').strip().upper()
    if len(currency) != 3:
        errors.setdefault('currency', []).append("Currency must be a 3-letter ISO 4217 code.")

    channel = data.get('channel') or campaign.channel
    if channel not in Campaign.Channel.values:
        errors.setdefault('channel', []).append("Invalid channel.")

    if not user.is_staff and channel == Campaign.Channel.POS:
        errors.setdefault('channel', []).append("Only staff can set campaigns to POS.")

    applies_to = data.get('applies_to') or campaign.applies_to
    if applies_to not in Campaign.AppliesTo.values:
        errors.setdefault('applies_to', []).append("Invalid scope.")

    if not user.is_staff and applies_to != Campaign.AppliesTo.SPECIFIC_PRODUCTS:
        errors.setdefault('applies_to', []).append(
            "Only staff can scope campaigns beyond specific products."
        )

    product_ids = parse_id_list(data, 'products')
    category_ids = parse_id_list(data, 'categories')
    brand_ids = parse_id_list(data, 'brands')

    if applies_to == Campaign.AppliesTo.SPECIFIC_PRODUCTS and not product_ids:
        errors.setdefault('products', []).append("Select at least one product for this scope.")
    if applies_to == Campaign.AppliesTo.SPECIFIC_CATEGORIES and not category_ids:
        errors.setdefault('categories', []).append("Select at least one category for this scope.")
    if applies_to == Campaign.AppliesTo.SPECIFIC_BRANDS and not brand_ids:
        errors.setdefault('brands', []).append("Select at least one brand for this scope.")

    start_at, err = parse_datetime_local(data.get('start_at'), required=True, field_label="Start date")
    if err:
        errors.setdefault('start_at', []).append(err)

    end_at, err = parse_datetime_local(data.get('end_at'), field_label="End date")
    if err:
        errors.setdefault('end_at', []).append(err)

    usage_limit_total, err = parse_int(data.get('usage_limit_total'), default=campaign.usage_limit_total, field_label="Total usage limit")
    if err:
        errors.setdefault('usage_limit_total', []).append(err)
    elif usage_limit_total and usage_limit_total < campaign.times_used:
        errors.setdefault('usage_limit_total', []).append(
            f"Total usage limit cannot be lower than the number of times already used ({campaign.times_used})."
        )

    usage_limit_per_user, err = parse_int(data.get('usage_limit_per_user'), default=campaign.usage_limit_per_user, field_label="Per-user usage limit")
    if err:
        errors.setdefault('usage_limit_per_user', []).append(err)

    priority, err = parse_int(data.get('priority'), default=campaign.priority, field_label="Priority")
    if err:
        errors.setdefault('priority', []).append(err)

    is_stackable = parse_bool(data.get('is_stackable'))
    is_enabled_requested = parse_bool(data.get('is_enabled'))

    products_qs = Product.objects.all()
    if not user.is_staff:
        products_qs = products_qs.filter(dealer=user)
    products = list(products_qs.filter(pk__in=product_ids)) if product_ids else []
    if product_ids and len(products) != len(set(product_ids)):
        errors.setdefault('products', []).append(
            "One or more selected products are invalid or not yours."
        )

    categories = list(Category.objects.filter(pk__in=category_ids)) if category_ids else []
    brands = list(Brand.objects.filter(pk__in=brand_ids)) if brand_ids else []

    if errors:
        return render(
            request, 'customer/campaign/campaign_form.html',
            _with_profile(request, _form_context(request, mode='edit', campaign=campaign, data=data, errors=errors)),
            status=400,
        )

    # Diff against the live row (not form.changed_data, since there's
    # no ModelForm) to decide whether this edit touches anything that
    # changes what the campaign actually does. Kept in sync field-by-
    # field with _SENSITIVE_FIELDS above.
    touched_sensitive_field = (
        campaign_type != campaign.campaign_type
        or discount_value != campaign.discount_value
        or max_discount_amount != campaign.max_discount_amount
        or min_order_amount != campaign.min_order_amount
        or budget_limit != campaign.budget_limit
        or currency != campaign.currency
        or applies_to != campaign.applies_to
        or channel != campaign.channel
        or start_at != campaign.start_at
        or end_at != campaign.end_at
        or usage_limit_total != campaign.usage_limit_total
        or usage_limit_per_user != campaign.usage_limit_per_user
        or set(str(pk) for pk in campaign.products.values_list('pk', flat=True)) != set(product_ids)
        or set(str(pk) for pk in campaign.categories.values_list('pk', flat=True)) != set(category_ids)
        or set(str(pk) for pk in campaign.brands.values_list('pk', flat=True)) != set(brand_ids)
    )
    was_approved = campaign.approval_status == Campaign.ApprovalStatus.APPROVED

    campaign.campaign_code = campaign_code
    campaign.name = name
    campaign.description = data.get('description') or None
    campaign.terms_conditions = data.get('terms_conditions') or None
    campaign.campaign_type = campaign_type
    campaign.discount_value = discount_value
    campaign.max_discount_amount = max_discount_amount
    campaign.min_order_amount = min_order_amount
    campaign.budget_limit = budget_limit
    campaign.currency = currency
    campaign.applies_to = applies_to
    campaign.channel = channel
    campaign.start_at = start_at
    campaign.end_at = end_at
    campaign.usage_limit_total = usage_limit_total
    campaign.usage_limit_per_user = usage_limit_per_user
    campaign.priority = priority
    campaign.is_stackable = is_stackable
    campaign.updated_by = user

    if was_approved and touched_sensitive_field and not user.is_staff:
        campaign.approval_status = Campaign.ApprovalStatus.PENDING
        campaign.submitted_for_approval_at = timezone.now()
        campaign.is_enabled = False
        messages.warning(
            request,
            _("Your changes affect pricing or eligibility, so this campaign was paused and "
              "sent back for admin approval before it can go live again."),
        )
    else:
        campaign.is_enabled = is_enabled_requested

    if request.FILES.get('banner_image'):
        campaign.banner_image = request.FILES['banner_image']

    try:
        with transaction.atomic():
            campaign.save()
            # Always .set() (even to empty) so clearing a scope's
            # selection on edit actually clears the M2M rather than
            # leaving stale rows.
            campaign.products.set(products)
            campaign.categories.set(categories)
            campaign.brands.set(brands)
    except ValidationError as exc:
        errors = _normalize_validation_error(exc)
        return render(
            request, 'customer/campaign/campaign_form.html',
            _with_profile(request, _form_context(request, mode='edit', campaign=campaign, data=data, errors=errors)),
            status=400,
        )

    messages.success(request, _("Campaign \u201c%(name)s\u201d updated.") % {'name': campaign.name})
    return redirect(_detail_url(campaign.slug))


def _handle_enable(request, campaign):
    if campaign.is_archived:
        messages.error(request, _("Restore this campaign before enabling it."))
    elif campaign.is_cancelled:
        messages.error(request, _("A cancelled campaign cannot be re-enabled."))
    elif campaign.approval_status == Campaign.ApprovalStatus.PENDING:
        messages.error(request, _("This campaign is awaiting admin approval and can't be enabled yet."))
    elif campaign.approval_status == Campaign.ApprovalStatus.REJECTED:
        messages.error(request, _("This campaign was rejected \u2014 edit and resubmit it for approval first."))
    else:
        campaign.enable()
        messages.success(request, _("Campaign enabled."))
    return redirect(_detail_url(campaign.slug))


def _handle_disable(request, campaign):
    if campaign.is_archived or campaign.is_cancelled:
        messages.error(request, _("This campaign is already terminal and can't be paused."))
    else:
        campaign.disable()
        messages.success(request, _("Campaign paused."))
    return redirect(_detail_url(campaign.slug))


def _handle_cancel(request, campaign):
    if campaign.is_archived:
        messages.error(request, _("Archived campaigns are already terminal."))
    elif campaign.is_cancelled:
        messages.error(request, _("This campaign is already cancelled."))
    else:
        campaign.cancel()
        messages.success(request, _("Campaign cancelled. This can't be undone \u2014 duplicate it if you need a similar campaign later."))
    return redirect(_detail_url(campaign.slug))


def _handle_submit_for_approval(request, campaign):
    if campaign.is_archived or campaign.is_cancelled:
        messages.error(request, _("This campaign can no longer be submitted for approval."))
    elif campaign.approval_status == Campaign.ApprovalStatus.PENDING:
        messages.error(request, _("This campaign is already awaiting approval."))
    else:
        campaign.submit_for_approval()
        messages.success(request, _("Campaign submitted for admin approval."))
    return redirect(_detail_url(campaign.slug))


def _handle_restore(request, campaign):
    if not campaign.is_archived:
        messages.error(request, _("This campaign isn't archived."))
    else:
        campaign.restore()
        messages.success(request, _("Campaign restored from the archive. It stays disabled \u2014 enable it when you're ready."))
    return redirect(_detail_url(campaign.slug))


def _handle_archive(request, campaign):
    if campaign.is_archived:
        messages.error(request, _("This campaign is already archived."))
        return redirect(_detail_url(campaign.slug))
    name = campaign.name
    campaign.archive(archived_by_user=request.user)
    messages.success(request, _("Campaign \u201c%(name)s\u201d has been archived.") % {'name': name})
    return redirect(_self_url())  # back to the list


def _handle_duplicate(request, campaign):
    user = request.user
    with transaction.atomic():
        clone = Campaign(
            name=_("%(name)s (Copy)") % {'name': campaign.name},
            description=campaign.description,
            terms_conditions=campaign.terms_conditions,
            campaign_type=campaign.campaign_type,
            discount_value=campaign.discount_value,
            max_discount_amount=campaign.max_discount_amount,
            min_order_amount=campaign.min_order_amount,
            currency=campaign.currency,
            applies_to=campaign.applies_to,
            channel=campaign.channel,
            start_at=timezone.now(),
            end_at=None,
            usage_limit_total=campaign.usage_limit_total,
            usage_limit_per_user=campaign.usage_limit_per_user,
            budget_limit=campaign.budget_limit,
            priority=campaign.priority,
            is_stackable=campaign.is_stackable,
            is_enabled=False,
            approval_status=(
                Campaign.ApprovalStatus.NOT_REQUIRED if getattr(user, 'is_admin', False)
                else Campaign.ApprovalStatus.PENDING
            ),
            created_by=user,
            updated_by=user,
        )
        # campaign_code and banner_image are intentionally not copied:
        # campaign_code is unique=True on the model, so carrying it over
        # verbatim would collide with the original on the very next
        # full_clean(); banner_image duplication belongs in a dedicated
        # media utility, not a save path.
        clone.save()
        clone.products.set(campaign.products.all())
        clone.categories.set(campaign.categories.all())
        clone.brands.set(campaign.brands.all())

    messages.success(request, _("Duplicated as a new draft campaign \u2014 review the details and enable it when ready."))
    return redirect(_self_url('edit', clone.pk))


_POST_ACTIONS_NO_CAMPAIGN = {'create'}
_POST_HANDLERS = {
    'edit': _handle_edit,
    'enable': _handle_enable,
    'disable': _handle_disable,
    'cancel': _handle_cancel,
    'submit_for_approval': _handle_submit_for_approval,
    'restore': _handle_restore,
    'archive': _handle_archive,
    'duplicate': _handle_duplicate,
}


# ====================================================================
# DETAIL VIEW (owner branch — separate URL, keyed by slug)
# ====================================================================

@login_required
def CampaignDetailView(request, slug):
    """
    Owner-branch campaign detail. Kept as its own view/URL — routed by
    campaign/<slug:slug>/, per the module docstring — rather than an
    `action=` branch of CampaignProfileView, so it can be keyed by
    slug in the path, matching the public
    apps/ponno/views/campaign_detail.py convention and what
    campaign_detail.html's own internal links/breadcrumbs expect.

    Scoped to the caller's own campaigns via _owner_queryset(), same
    as every other action on CampaignProfileView — this is the main
    defense against one dealer reaching another dealer's campaign by
    guessing a slug. Non-owners (plain customers) get a 404 here
    rather than falling back to the customer dashboard, since this is
    a standalone URL with no "other branch" to redirect into.
    """
    user = request.user
    request._campaign_profile = _get_or_create_profile(user)

    is_owner_view = (
        getattr(user, 'is_dealer', False)
        or getattr(user, 'is_admin', False)
        or user.role == User.Role.STAFF
    )
    if not is_owner_view:
        raise Http404("Unknown campaign: %r" % slug)

    base_qs = _owner_queryset(user)
    campaign = get_object_or_404(
        base_qs.select_related('created_by', 'approved_by').prefetch_related('products', 'categories', 'brands'),
        slug=slug,
    )
    return render(
        request, 'ponno/campaign/campaign_detail.html',
        _with_profile(request, _detail_context(campaign)),
    )


# ====================================================================
# MAIN VIEW
# ====================================================================

@login_required
def CampaignProfileView(request):
    user = request.user

    # Fetched once, reused by every render() in either branch via
    # _with_profile() — see that helper's docstring.
    request._campaign_profile = _get_or_create_profile(user)

    # STAFF is included here (in addition to the original is_dealer /
    # is_admin check) so plain staff accounts get the management UI
    # for their own campaigns too, rather than being redirected to the
    # customer dashboard. This does NOT change _owner_queryset(): a
    # staff user who isn't also a superuser (is_admin) still only ever
    # sees campaigns they personally created, same as a dealer.
    is_owner_view = (
        getattr(user, 'is_dealer', False)
        or getattr(user, 'is_admin', False)
        or user.role == User.Role.STAFF
    )

    if not is_owner_view:
        if request.method == 'POST':
            # The action/pk POST vocabulary below (create/edit/enable/...)
            # only makes sense for campaign owners. A non-owner POST here
            # means either a stale form or a tampered request.
            messages.error(request, _("You don't have permission to manage campaigns."))
            return redirect(_self_url())
        return render(
            request, 'personal/campaign_profile.html',
            _with_profile(request, _build_customer_context(request, user)),
        )

    action = (request.POST.get('action') or request.GET.get('action') or 'list').strip()
    pk = request.POST.get('pk') or request.GET.get('pk')
    base_qs = _owner_queryset(user)

    # ---- POST: mutating actions ----
    if request.method == 'POST':
        if action in _POST_ACTIONS_NO_CAMPAIGN:
            return _handle_create(request)

        handler = _POST_HANDLERS.get(action)
        if handler is None:
            raise Http404("Unknown campaign action: %r" % action)

        # `restore` must be reachable even though the campaign is
        # archived, so it looks up against the unrestricted owner
        # queryset rather than one that excludes archived rows.
        campaign = get_object_or_404(base_qs, pk=pk)
        return handler(request, campaign)

    # ---- GET: create form ----
    if action == 'create':
        return render(
            request, 'ponno/campaign/campaign_form.html',
            _with_profile(request, _form_context(request, mode='create')),
        )

    # ---- GET: edit form ----
    if action == 'edit':
        campaign = get_object_or_404(base_qs.exclude(Q(is_archived=True) | Q(is_cancelled=True)), pk=pk)
        return render(
            request, 'ponno/campaign/campaign_form.html',
            _with_profile(request, _form_context(request, mode='edit', campaign=campaign)),
        )

    # ---- GET: redemption audit log ----
    if action == 'redemptions':
        campaign = get_object_or_404(base_qs, pk=pk)
        redemptions_qs = (
            CampaignRedemption.objects.filter(campaign=campaign)
            .select_related('user', 'product', 'order')
            .order_by('-redeemed_at')
        )
        return render(
            request, 'ponno/campaign/campaign_redemption_list.html',
            _with_profile(request, {
                'mode': 'redemptions',
                'campaign': campaign,
                'redemptions': _paginate(request, redemptions_qs, per_page=50),
            }),
        )

    # ---- GET: redemption CSV export ----
    if action == 'redemptions_export':
        campaign = get_object_or_404(base_qs, pk=pk)
        response = HttpResponse(content_type='text/csv')
        filename = campaign.slug or f"campaign-{campaign.pk}"
        response['Content-Disposition'] = f'attachment; filename="{filename}-redemptions.csv"'
        writer = csv.writer(response)
        writer.writerow(['Redeemed At', 'User', 'Product', 'Order ID', 'Original Price', 'Discount', 'Final Price'])
        redemptions = (
            CampaignRedemption.objects.filter(campaign=campaign)
            .select_related('user', 'product', 'order')
            .order_by('-redeemed_at')
            .iterator()
        )
        for r in redemptions:
            writer.writerow([
                r.redeemed_at.isoformat(),
                str(r.user) if r.user else '\u2014',
                str(r.product) if r.product else '\u2014',
                r.order_id or '\u2014',
                r.original_price,
                r.discount_amount,
                r.final_price,
            ])
        return response

    # ---- GET: list (default) ----
    # No forms.py, same convention as the rest of this file — read
    # status/q/order straight off request.GET. ORDER_ALLOWED guards
    # against an arbitrary/unsafe field name being passed to
    # .order_by() via a tampered query string.
    ORDER_ALLOWED = {
        '-created_at', 'created_at', '-priority', 'name', '-start_at', 'start_at',
    }

    qs = base_qs.exclude(is_archived=True).select_related('created_by').prefetch_related('products', 'categories', 'brands')

    status = (request.GET.get('status') or '').strip()
    search = (request.GET.get('q') or '').strip()
    ordering = request.GET.get('order') or '-created_at'
    if ordering not in ORDER_ALLOWED:
        ordering = '-created_at'

    if status:
        qs = base_qs.filter(is_archived=True) if status == 'archived' else STATUS_FILTERS.get(status, lambda x: x)(qs)
    if search:
        qs = qs.filter(Q(name__icontains=search) | Q(campaign_code__icontains=search) | Q(slug__icontains=search))
    qs = qs.order_by(ordering)

    summary_base = base_qs.exclude(is_archived=True)
    context = {
        'mode': 'list',
        'filter_status': status,
        'filter_q': search,
        'filter_order': ordering,
        'campaigns': _paginate(request, qs.distinct()),
        'status_summary': {
            'draft': _filter_draft(summary_base).count(),
            'pending_approval': _filter_pending_approval(summary_base).count(),
            'active': _filter_active(summary_base).count(),
            'scheduled': _filter_scheduled(summary_base).count(),
            'paused': _filter_paused(summary_base).count(),
            'ended': _filter_ended(summary_base).count(),
        },
    }
    return render(request, 'personal/campaign_profile.html', _with_profile(request, context))