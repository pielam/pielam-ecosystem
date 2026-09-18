# apps/ponno/campaign_forms.py

"""
Forms backing CampaignProfileView (apps/customer/views/campaign_profile.py).

CampaignForm    - create/edit form for a dealer's own campaign. Scope
                   choice fields (products/categories/brands) are
                   optionally narrowed to the requesting user's own
                   catalog via the `user` kwarg, so a dealer can't
                   attach a campaign to another dealer's product.
CampaignFilterForm - GET-bound filter/search/order form for the list view.
"""

from django import forms
from django.utils.translation import gettext_lazy as _

from apps.ponno.models.campaign import Campaign
from apps.ponno.models.product import Product
from apps.ponno.models.category import Category
from apps.ponno.models.brand import Brand


class CampaignForm(forms.ModelForm):
    """
    Create/edit form for Campaign. Fields that are internal bookkeeping
    (times_used, budget_spent, approval_status, is_enabled, is_cancelled,
    is_archived, etc.) are deliberately excluded — those are only ever
    changed through the model's own transition methods, called from the
    view's action handlers, never directly from user input.
    """

    class Meta:
        model = Campaign
        fields = [
            'name',
            'description',
            'banner_image',
            'terms_conditions',
            'campaign_type',
            'discount_value',
            'max_discount_amount',
            'min_order_amount',
            'currency',
            'applies_to',
            'products',
            'categories',
            'brands',
            'channel',
            'start_at',
            'end_at',
            'usage_limit_total',
            'usage_limit_per_user',
            'budget_limit',
            'priority',
            'is_stackable',
        ]
        widgets = {
            'name': forms.TextInput(attrs={'class': 'form-control', 'placeholder': _('e.g. Eid Flash Sale')}),
            'description': forms.Textarea(attrs={'class': 'form-control', 'rows': 3}),
            'terms_conditions': forms.Textarea(attrs={'class': 'form-control', 'rows': 3}),
            'campaign_type': forms.Select(attrs={'class': 'form-select'}),
            'discount_value': forms.NumberInput(attrs={'class': 'form-control', 'step': '0.01'}),
            'max_discount_amount': forms.NumberInput(attrs={'class': 'form-control', 'step': '0.01'}),
            'min_order_amount': forms.NumberInput(attrs={'class': 'form-control', 'step': '0.01'}),
            'currency': forms.TextInput(attrs={'class': 'form-control', 'maxlength': 3}),
            'applies_to': forms.Select(attrs={'class': 'form-select', 'id': 'id_applies_to'}),
            'products': forms.SelectMultiple(attrs={'class': 'form-select', 'size': 8}),
            'categories': forms.SelectMultiple(attrs={'class': 'form-select', 'size': 6}),
            'brands': forms.SelectMultiple(attrs={'class': 'form-select', 'size': 6}),
            'channel': forms.Select(attrs={'class': 'form-select'}),
            'start_at': forms.DateTimeInput(attrs={'class': 'form-control', 'type': 'datetime-local'}),
            'end_at': forms.DateTimeInput(attrs={'class': 'form-control', 'type': 'datetime-local'}),
            'usage_limit_total': forms.NumberInput(attrs={'class': 'form-control'}),
            'usage_limit_per_user': forms.NumberInput(attrs={'class': 'form-control'}),
            'budget_limit': forms.NumberInput(attrs={'class': 'form-control', 'step': '0.01'}),
            'priority': forms.NumberInput(attrs={'class': 'form-control'}),
            'is_stackable': forms.CheckboxInput(attrs={'class': 'form-check-input'}),
        }

    def __init__(self, *args, user=None, **kwargs):
        self.user = user
        super().__init__(*args, **kwargs)

        # Non-admin dealers may only scope campaigns to their own
        # products/categories/brands they actually sell — prevents
        # attaching a campaign to inventory that isn't theirs.
        if user is not None and not getattr(user, 'is_admin', False):
            owned_products = Product.objects.filter(created_by=user)
            self.fields['products'].queryset = owned_products

            owned_category_ids = owned_products.values_list('category_id', flat=True).distinct()
            self.fields['categories'].queryset = Category.objects.filter(pk__in=owned_category_ids)

            owned_brand_ids = owned_products.values_list('brand_id', flat=True).distinct()
            self.fields['brands'].queryset = Brand.objects.filter(pk__in=owned_brand_ids)
        else:
            self.fields['products'].queryset = Product.objects.all()
            self.fields['categories'].queryset = Category.objects.all()
            self.fields['brands'].queryset = Brand.objects.all()

        # Scope fields are only required contextually (based on
        # applies_to), so none of the three are marked required here —
        # enforced instead in clean() below.
        self.fields['products'].required = False
        self.fields['categories'].required = False
        self.fields['brands'].required = False
        self.fields['end_at'].required = False
        self.fields['max_discount_amount'].required = False
        self.fields['min_order_amount'].required = False
        self.fields['budget_limit'].required = False

    def clean(self):
        cleaned_data = super().clean()
        applies_to = cleaned_data.get('applies_to')

        scope_map = {
            Campaign.AppliesTo.SPECIFIC_PRODUCTS: 'products',
            Campaign.AppliesTo.SPECIFIC_CATEGORIES: 'categories',
            Campaign.AppliesTo.SPECIFIC_BRANDS: 'brands',
        }
        required_field = scope_map.get(applies_to)
        if required_field and not cleaned_data.get(required_field):
            self.add_error(
                required_field,
                _("Select at least one item — this campaign's scope requires it."),
            )

        campaign_type = cleaned_data.get('campaign_type')
        discount_value = cleaned_data.get('discount_value')
        if campaign_type == Campaign.CampaignType.PERCENTAGE and discount_value is not None:
            if discount_value > 100:
                self.add_error('discount_value', _("Percentage discount cannot exceed 100."))

        start_at = cleaned_data.get('start_at')
        end_at = cleaned_data.get('end_at')
        if start_at and end_at and end_at <= start_at:
            self.add_error('end_at', _("End date must be after start date."))

        return cleaned_data


class CampaignFilterForm(forms.Form):
    """GET-bound filter/search form for the campaign list view."""

    STATUS_CHOICES = [
        ('', _('All statuses')),
        ('draft', _('Draft')),
        ('pending_approval', _('Pending Approval')),
        ('rejected', _('Rejected')),
        ('scheduled', _('Scheduled')),
        ('active', _('Active')),
        ('paused', _('Paused')),
        ('ended', _('Ended')),
        ('cancelled', _('Cancelled')),
        ('archived', _('Archived')),
    ]

    ORDER_CHOICES = [
        ('-created_at', _('Newest first')),
        ('created_at', _('Oldest first')),
        ('-priority', _('Highest priority')),
        ('name', _('Name (A\u2013Z)')),
        ('-start_at', _('Start date (latest)')),
        ('start_at', _('Start date (earliest)')),
    ]

    status = forms.ChoiceField(choices=STATUS_CHOICES, required=False)
    q = forms.CharField(
        required=False,
        max_length=150,
        widget=forms.TextInput(attrs={'placeholder': _('Search by name or code\u2026'), 'class': 'form-control'}),
    )
    order = forms.ChoiceField(choices=ORDER_CHOICES, required=False)