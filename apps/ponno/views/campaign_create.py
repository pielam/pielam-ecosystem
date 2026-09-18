# apps/ponno/views/campaign_create.py

"""
No forms.py — request.POST/request.FILES are parsed directly and the
Campaign instance is validated via its own .clean()/full_clean() (run
inside Campaign.save()). Errors collected here are a mix of:
  - parsing errors (bad decimal/int/datetime string), caught before
    ever touching the model, and
  - django.core.exceptions.ValidationError raised by full_clean(),
    normalized into the same {field: [messages]} shape so the template
    doesn't need to special-case where an error came from.

Note on the 'non_field' key: Django's ValidationError uses '__all__' as
the dict key for errors not tied to a specific field (e.g. raised as
ValidationError("some message") or ValidationError({'__all__': [...]})
inside Campaign.clean()). Django's template engine refuses to resolve
any variable/attribute starting with an underscore (see
django.template.base's FILTER_SEPARATOR / dunder guard), so
`errors.__all__` in the template raises a TemplateSyntaxError. To keep
the template simple, that key is renamed to 'non_field' here before it
ever reaches the template — this is the one place that translation
happens, so the template only ever needs to check `errors.non_field`.

Approval workflow note: approval_status is independent of is_enabled
(see Campaign.active() / Campaign.Meta constraints). A non-staff
dealer must never be able to self-approve a campaign, so this view
forces approval_status=PENDING (via the same fields
submit_for_approval() would set) for any dealer-created campaign,
regardless of what was posted. Staff-created campaigns default to
NOT_REQUIRED (skip the queue) unless staff explicitly submits for
approval, which this view does not currently expose — staff can flip
that from the edit/detail view instead.

Scope-integrity note: `applies_to` decides how Campaign.for_product()
resolves scope (ALL_PRODUCTS / SPECIFIC_PRODUCTS / SPECIFIC_CATEGORIES
/ SPECIFIC_BRANDS — see CampaignManager.for_product()'s scope_q). Only
the m2m matching the chosen `applies_to` is ever consulted there, so
this view only ever writes to the one relevant m2m (products /
categories / brands) instead of setting whichever lists happen to be
non-empty — leftover selections from switching the scope dropdown in
the form must not get persisted as orphaned, unused m2m rows.

Soft-delete note: Brand and Category are soft-deleted (deleted_at) and
independently have an is_active flag, same as Product. Scope selection
here always resolves through each model's `active_*()` manager method
(Brand.objects.active_brands(), Category.objects.active_categories(),
Product.objects.active_products()) so a campaign can never be scoped to
a deactivated/soft-deleted brand, category, or product just because its
primary key was still present in the submitted form data.

Scope-locking note: a product, category, or brand already attached to
one of this same creator's OTHER still-live SPECIFIC_* campaigns is
excluded from the selectable querysets entirely — both for rendering
the pickers AND for resolving a POST's submitted ids. This means a
locked item behaves exactly like an inactive/soft-deleted one from
this view's point of view: if its id is still present in a tampered
POST, it simply won't resolve, and the existing "invalid/inactive/not
yours" mismatch check catches it and rejects the submission. See
Campaign.objects.locked_product_ids() / locked_products_map() (and the
category/brand equivalents) for the exact "still-live" definition.
"Locked" is scoped to `created_by` (Campaign.created_by) rather than
to e.g. the product's dealer, so it follows whoever is actually
creating campaigns (dealer or staff on their behalf).
"""

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin, UserPassesTestMixin
from django.core.exceptions import ValidationError
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
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


class _CampaignCreateView(LoginRequiredMixin, UserPassesTestMixin, View):
    """
    Dealer or staff campaign creation.

    Any authenticated dealer can create a campaign; non-staff dealers
    are restricted to applies_to=SPECIFIC_PRODUCTS scoped to their own
    products (checked server-side below, not just left to the template
    to hide options) — ALL_PRODUCTS / category-wide / brand-wide scopes
    can affect other dealers' listings indirectly and are staff-only.

    Non-staff dealers are also always routed into the approval queue
    (approval_status=PENDING) — they can enable the campaign, but per
    Campaign.active() it won't actually go live until staff approves
    it. Staff-created campaigns skip the queue by default
    (approval_status=NOT_REQUIRED).

    A product, category, or brand already reserved by one of this
    creator's own other still-live SPECIFIC_* campaigns cannot be
    selected again here until that campaign ends/is cancelled/
    archived/rejected — see Campaign.objects.locked_product_ids() /
    locked_products_map() (and the category/brand equivalents) and the
    "Scope-locking note" above.
    """

    template_name = 'ponno/campaign/campaign_form.html'

    def test_func(self):
        # Campaign creation is dealer-only. is_staff is intentionally
        # NOT a bypass here — the staff-only carve-outs further down
        # (applies_to beyond SPECIFIC_PRODUCTS, the POS channel,
        # approval-queue skip) only ever fire for a user who is BOTH
        # role='dealer' AND is_staff=True; a pure staff/admin account
        # with role != 'dealer' cannot reach this view at all.
        user = self.request.user
        return user.is_authenticated and getattr(user, 'role', None) == 'dealer'

    def get(self, request):
        return render(request, self.template_name, self._context())

    def post(self, request):
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

        applies_to = data.get('applies_to') or Campaign.AppliesTo.SPECIFIC_PRODUCTS
        if applies_to not in Campaign.AppliesTo.values:
            errors.setdefault('applies_to', []).append("Invalid scope.")

        if not request.user.is_staff and applies_to != Campaign.AppliesTo.SPECIFIC_PRODUCTS:
            errors.setdefault('applies_to', []).append(
                "Only staff can create campaigns scoped beyond specific products."
            )

        # Non-staff dealers can only launch on channels that don't
        # require platform-wide coordination; staff-only channel is
        # POS for the same reason ALL_PRODUCTS/category/brand scope is
        # staff-only above.
        if not request.user.is_staff and channel == Campaign.Channel.POS:
            errors.setdefault('channel', []).append("Only staff can create POS campaigns.")

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

        # --- Scope resolution -----------------------------------------
        # Restrict a non-staff dealer's product selection to their own
        # catalog server-side — a tampered POST listing another
        # dealer's product id must not be honored just because the
        # client sent it. Base queryset is active_products() (mirrors
        # _context()) so a campaign can never be scoped to a
        # soft-deleted / deactivated product just because its id was
        # still present in the submitted form.
        #
        # Locked products/categories/brands (already reserved by one of
        # this creator's other still-live SPECIFIC_* campaigns) are
        # excluded here too, for the same reason — see the
        # "Scope-locking note" in the module docstring. A locked id
        # present in the POST simply fails to resolve and falls into
        # the same "invalid/inactive/not yours" mismatch error below,
        # rather than needing its own bespoke error path.
        locked_product_ids = Campaign.objects.locked_product_ids(created_by=request.user)
        locked_category_ids = Campaign.objects.locked_category_ids(created_by=request.user)
        locked_brand_ids = Campaign.objects.locked_brand_ids(created_by=request.user)

        products_qs = Product.objects.active_products()
        if not request.user.is_staff:
            products_qs = products_qs.filter(dealer=request.user)
        if locked_product_ids:
            products_qs = products_qs.exclude(pk__in=locked_product_ids)

        products = list(products_qs.filter(pk__in=product_ids)) if product_ids else []
        if product_ids and len(products) != len(set(product_ids)):
            errors.setdefault('products', []).append(
                "One or more selected products are invalid, inactive, not yours, "
                "or already locked by another one of your active campaigns."
            )

        # Categories/brands get the same active-only + locked-exclusion
        # resolution as products, and — like products — a mismatch
        # between the submitted ids and what actually resolves is a
        # hard error rather than a silent drop. Without this check, a
        # stale or tampered id could pass the "at least one selected"
        # check above yet resolve to an empty list, saving a campaign
        # whose applies_to=specific_categories/specific_brands scope is
        # empty and therefore matches nothing in
        # CampaignManager.for_product().
        categories_qs = Category.objects.active_categories()
        if locked_category_ids:
            categories_qs = categories_qs.exclude(pk__in=locked_category_ids)
        categories = list(categories_qs.filter(pk__in=category_ids)) if category_ids else []
        if category_ids and len(categories) != len(set(category_ids)):
            errors.setdefault('categories', []).append(
                "One or more selected categories are invalid, inactive, "
                "or already locked by another one of your active campaigns."
            )

        brands_qs = Brand.objects.active_brands()
        if locked_brand_ids:
            brands_qs = brands_qs.exclude(pk__in=locked_brand_ids)
        brands = list(brands_qs.filter(pk__in=brand_ids)) if brand_ids else []
        if brand_ids and len(brands) != len(set(brand_ids)):
            errors.setdefault('brands', []).append(
                "One or more selected brands are invalid, inactive, "
                "or already locked by another one of your active campaigns."
            )

        if errors:
            return render(request, self.template_name, self._context(data=data, errors=errors), status=400)

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
            created_by=request.user,
        )

        # Approval workflow: dealers can never self-approve. This is
        # enforced here rather than left to the template/JS, mirroring
        # the same server-side-only enforcement used for `applies_to`
        # and `channel` above. Staff-created campaigns keep the model
        # default (NOT_REQUIRED) and go live immediately if is_enabled.
        if not request.user.is_staff:
            campaign.approval_status = Campaign.ApprovalStatus.PENDING
            campaign.submitted_for_approval_at = timezone.now()

        if request.FILES.get('banner_image'):
            campaign.banner_image = request.FILES['banner_image']

        try:
            campaign.save()  # runs generate_slug() / full_clean() internally
        except ValidationError as exc:
            errors = self._normalize_validation_error(exc)
            return render(request, self.template_name, self._context(data=data, errors=errors), status=400)

        # Only persist the m2m that actually matches the chosen scope.
        # Campaign.for_product() branches strictly on applies_to (see
        # CampaignManager.for_product()'s scope_q) — leftover
        # products/categories/brands selections left over from
        # switching the scope dropdown in the form must not be saved
        # as orphaned, functionally-inert m2m rows.
        if applies_to == Campaign.AppliesTo.SPECIFIC_PRODUCTS and products:
            campaign.products.set(products)
        elif applies_to == Campaign.AppliesTo.SPECIFIC_CATEGORIES and categories:
            campaign.categories.set(categories)
        elif applies_to == Campaign.AppliesTo.SPECIFIC_BRANDS and brands:
            campaign.brands.set(brands)

        if campaign.approval_status == Campaign.ApprovalStatus.PENDING:
            messages.success(
                request,
                f"Campaign '{campaign.name}' created and submitted for approval. "
                "It won't go live until staff approves it."
            )
        else:
            messages.success(request, f"Campaign '{campaign.name}' created.")

        return redirect(reverse('ponno:campaign_detail', kwargs={'slug': campaign.slug}))

    def _context(self, *, data=None, errors=None):
        # Locked items stay in each queryset (so the template can still
        # render them, greyed out, with a "locked by ..." label) rather
        # than disappearing silently — but they're flagged via `.lock`
        # so the template can disable their checkbox and show why. The
        # actual enforcement happens server-side in post() by excluding
        # them from the resolvable querysets there, not here.
        products_qs = Product.objects.active_products()
        if not self.request.user.is_staff:
            products_qs = products_qs.filter(dealer=self.request.user)
        products = list(products_qs)

        categories = list(Category.objects.active_categories())
        brands = list(Brand.objects.active_brands())

        # Attach lock info directly onto each instance as `.lock` (None
        # if not locked) rather than handing the template separate
        # {id: ...} dicts — plain Django templates can't do a
        # dict[variable] lookup, and this avoids needing a custom
        # template filter. `.lock` reads as a normal attribute in the
        # template: `{{ product.lock }}`, `{% if category.lock %}`, etc.
        locked_products = Campaign.objects.locked_products_map(created_by=self.request.user)
        for product in products:
            product.lock = locked_products.get(product.pk)

        locked_categories = Campaign.objects.locked_categories_map(created_by=self.request.user)
        for category in categories:
            category.lock = locked_categories.get(category.pk)

        locked_brands = Campaign.objects.locked_brands_map(created_by=self.request.user)
        for brand in brands:
            brand.lock = locked_brands.get(brand.pk)

        data = data if data is not None else {}

        # The template re-checks each picker's checkboxes on a
        # validation-error redisplay via
        # `product.pk|stringformat:"s" in selected_products` — so these
        # need to be sets of the *string* pks the form actually posted.
        # `data` is a QueryDict on POST (supports getlist) and a plain
        # {} on GET, hence the hasattr guard.
        get_list = data.getlist if hasattr(data, 'getlist') else (lambda _key: [])

        return {
            'campaign_types': Campaign.CampaignType.choices,
            'applies_to_choices': Campaign.AppliesTo.choices,
            'channel_choices': Campaign.Channel.choices,
            'products': products,
            'categories': categories,
            'brands': brands,
            'is_staff': self.request.user.is_staff,
            'data': data,
            'errors': errors or {},
            'selected_products': set(get_list('products')),
            'selected_categories': set(get_list('categories')),
            'selected_brands': set(get_list('brands')),
        }

    @staticmethod
    def _normalize_validation_error(exc: ValidationError) -> dict:
        """
        Normalize a django ValidationError into a plain {field: [messages]}
        dict for the template.

        exc.message_dict (present when the error was raised as, or
        aggregates into, a dict of field -> errors) uses '__all__' as the
        key for non-field-specific errors — e.g. anything raised via
        `raise ValidationError("some message")` inside Campaign.clean()
        without tying it to a field. Django's template engine forbids
        resolving any variable/attribute that starts with an underscore,
        so `errors.__all__` cannot be looked up in a template at all
        (raises TemplateSyntaxError, not just a silent miss). Rename that
        key to 'non_field' here, once, so the template only ever needs
        `{% if errors.non_field %}` and never touches the dunder key.
        """
        if hasattr(exc, 'error_dict'):
            return {
                ('non_field' if field == '__all__' else field): [str(m) for m in msgs]
                for field, msgs in exc.message_dict.items()
            }
        return {'non_field': exc.messages}


CampaignCreateView = _CampaignCreateView.as_view()