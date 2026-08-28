# apps/ponno/views/campaign_edit.py

"""
Edit an existing campaign. Same manual-parsing approach as
CampaignCreateView (no forms.py) — see that file's module docstring
for the error-handling shape and the '__all__' -> 'non_field' key
translation.

Fields this view intentionally does NOT let anyone touch, staff
included:
  - approval_status / submitted_for_approval_at / approved_by /
    approved_at / rejected_by / rejected_at / rejection_reason —
    these belong to the approve()/reject()/submit_for_approval()
    transitions on the model, which are dedicated staff-only actions
    (a separate view), not a side effect of a general-purpose edit
    form.
  - is_cancelled / is_archived / first_activated_at / times_used /
    budget_spent — all editable=False on the model (or terminal /
    counter fields with their own methods: cancel(), archive(),
    register_redemption()), so they're never part of this form to
    begin with.
"""

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin, UserPassesTestMixin
from django.core.exceptions import ValidationError
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views import View

from apps.ponno.models.brand import Brand
from apps.ponno.models.campaign import Campaign
from apps.ponno.models.category import Category
from apps.ponno.models.product import Product
from apps.ponno.views._campaign_input import (
    parse_bool,
    parse_datetime_local,
    parse_decimal,
    parse_id_list,
    parse_int,
)


class _CampaignEditView(LoginRequiredMixin, UserPassesTestMixin, View):
    """
    Edit an existing campaign. Same manual-parsing approach as
    CampaignCreateView (no forms.py) — see that file's module docstring
    for the error-handling shape. Only the campaign's creator may edit
    it; staff have no override here (unlike creation, where staff get
    broader scope options for applies_to).
    """

    template_name = 'ponno/campaign/campaign_form.html'

    def _get_campaign(self):
        return get_object_or_404(Campaign, slug=self.kwargs['slug'])

    def test_func(self):
        """
        Only the user who created the campaign may edit it. A logged-in
        user hitting this URL for someone else's campaign gets a 403,
        not just a hidden link in the template.
        """
        user = self.request.user
        if not user.is_authenticated:
            return False
        campaign = self._get_campaign()
        return campaign.created_by_id == user.id

    def get(self, request, slug):
        campaign = self._get_campaign()
        return render(request, self.template_name, self._context(campaign))

    def post(self, request, slug):
        campaign = self._get_campaign()
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
            # The model's clean() already rejects budget_spent > budget_limit,
            # but catching it here gives a field-specific message instead of
            # a generic non_field one from full_clean().
            errors.setdefault('budget_limit', []).append(
                f"Budget limit cannot be lower than the amount already spent ({campaign.budget_spent})."
            )

        currency = (data.get('currency') or campaign.currency or 'BDT').strip().upper()
        if len(currency) != 3:
            errors.setdefault('currency', []).append("Currency must be a 3-letter ISO 4217 code.")

        channel = data.get('channel') or campaign.channel
        if channel not in Campaign.Channel.values:
            errors.setdefault('channel', []).append("Invalid channel.")

        if not request.user.is_staff and channel == Campaign.Channel.POS:
            errors.setdefault('channel', []).append("Only staff can set campaigns to POS.")

        applies_to = data.get('applies_to') or campaign.applies_to
        if applies_to not in Campaign.AppliesTo.values:
            errors.setdefault('applies_to', []).append("Invalid scope.")

        if not request.user.is_staff and applies_to != Campaign.AppliesTo.SPECIFIC_PRODUCTS:
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
        is_enabled = parse_bool(data.get('is_enabled'))

        products_qs = Product.objects.all()
        if not request.user.is_staff:
            products_qs = products_qs.filter(dealer=request.user)
        products = list(products_qs.filter(pk__in=product_ids)) if product_ids else []
        if product_ids and len(products) != len(set(product_ids)):
            errors.setdefault('products', []).append(
                "One or more selected products are invalid or not yours."
            )

        categories = list(Category.objects.filter(pk__in=category_ids)) if category_ids else []
        brands = list(Brand.objects.filter(pk__in=brand_ids)) if brand_ids else []

        if errors:
            return render(request, self.template_name, self._context(campaign, data=data, errors=errors), status=400)

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
        campaign.is_enabled = is_enabled

        if request.FILES.get('banner_image'):
            campaign.banner_image = request.FILES['banner_image']

        try:
            campaign.save()
        except ValidationError as exc:
            errors = self._normalize_validation_error(exc)
            return render(request, self.template_name, self._context(campaign, data=data, errors=errors), status=400)

        # Always .set() (even to empty) so clearing a scope's selection
        # on edit actually clears the M2M rather than leaving stale rows.
        campaign.products.set(products)
        campaign.categories.set(categories)
        campaign.brands.set(brands)

        messages.success(request, f"Campaign '{campaign.name}' updated.")
        return redirect(reverse('ponno:campaign_detail', kwargs={'slug': campaign.slug}))

    def _context(self, campaign, *, data=None, errors=None):
        """
        Build the template context.

        `data` is None on a fresh GET, and the raw POST QueryDict on a
        validation-error redisplay. Either way, the template should be
        able to read `data.<field>` as a plain string/bool without caring
        which case it's in — so this normalizes both into one flat dict.

        It also resolves the three scope pickers (products/categories/
        brands) into plain sets of id-strings ahead of time. Reading a
        multi-value field like `data.products` directly off a QueryDict
        in a template only returns the *last* posted value, not the full
        list, so `pk in data.products` would silently degrade into a
        substring check instead of a real membership test. Doing the
        `.getlist()` / `.values_list()` here avoids that trap entirely.
        """
        products_qs = Product.objects.active_products()
        if not self.request.user.is_staff:
            products_qs = products_qs.filter(dealer=self.request.user)

        if data is not None:
            # Redisplaying after a validation error: echo back exactly
            # what was submitted so the user doesn't lose their edits.
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
        else:
            # First load: prefill from the campaign being edited.
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

        return {
            'campaign': campaign,
            'campaign_types': Campaign.CampaignType.choices,
            'applies_to_choices': Campaign.AppliesTo.choices,
            'channel_choices': Campaign.Channel.choices,
            'products': products_qs,
            'categories': Category.objects.all(),
            'brands': Brand.objects.all(),
            'is_staff': self.request.user.is_staff,
            'data': initial,
            'selected_products': selected_products,
            'selected_categories': selected_categories,
            'selected_brands': selected_brands,
            'banner_image_url': campaign.banner_image.url if campaign.banner_image else '',
            # Read-only display info the template can show but not edit —
            # matches the fields this view deliberately never writes to.
            'approval_status': campaign.approval_status,
            'times_used': campaign.times_used,
            'budget_spent': campaign.budget_spent,
            'errors': errors or {},
        }

    @staticmethod
    def _normalize_validation_error(exc: ValidationError) -> dict:
        """
        Normalize a django ValidationError into a plain {field: [messages]}
        dict for the template.

        exc.message_dict uses '__all__' as the key for non-field-specific
        errors (e.g. raised via `raise ValidationError("some message")`
        inside Campaign.clean()). Django's template engine forbids
        resolving any variable/attribute starting with an underscore, so
        `errors.__all__` cannot be looked up in a template at all — it
        raises TemplateSyntaxError, not just a silent miss. Rename that
        key to 'non_field' here, once, so the template only ever needs
        `{% if errors.non_field %}` and never touches the dunder key.
        See campaign_create.py's module docstring for the full story.
        """
        if hasattr(exc, 'error_dict'):
            return {
                ('non_field' if field == '__all__' else field): [str(m) for m in msgs]
                for field, msgs in exc.message_dict.items()
            }
        return {'non_field': exc.messages}


CampaignEditView = _CampaignEditView.as_view()