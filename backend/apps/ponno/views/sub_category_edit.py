# apps/ponno/views/sub_category_edit.py

import re
import logging
from decimal import Decimal, InvalidOperation

from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.db import transaction

from apps.customer.models.profile_info import ProfileInfo
from apps.ponno.models.category import Category
from apps.ponno.models.brand import Brand
from apps.ponno.models.sub_category import SubCategory

logger = logging.getLogger(__name__)


def _to_decimal(value: str, field_label: str, errors: list, default=None):
    """Safely convert a string to Decimal, appending to errors on failure."""
    if not value:
        return default
    try:
        return Decimal(value)
    except InvalidOperation:
        errors.append(f"{field_label} must be a valid number.")
        return default


@login_required
def SubCategoryEditView(request, slug):
    """
    Dealer-only subcategory edit view.
    Fetches the existing SubCategory by slug, pre-populates the form,
    and updates only the fields that were submitted.

    - Slug is NOT changed on edit (preserves existing URLs).
    - Soft-deleted subcategories cannot be edited.
    - On success redirects to the subcategory's detail page.
    """
    user = request.user
    profile_info = get_object_or_404(ProfileInfo, user=user)

    if user.role != 'dealer':
        messages.error(request, "❌ Only dealers can edit subcategories.")
        return redirect('customer:profile')

    # Fetch the existing subcategory by slug — reject soft-deleted ones
    sub_category = get_object_or_404(
        SubCategory,
        sub_category_slug=slug,
        deleted_at__isnull=True
    )

    active_categories = Category.objects.filter(is_active=True, deleted_at__isnull=True)
    active_brands = Brand.objects.filter(is_active=True)

    context_defaults = {
        'user': user,
        'profile_info': profile_info,
        'sub_category': sub_category,
        'categories': active_categories,
        'brands': active_brands,
        'sub_category_types': SubCategory.SubCategoryType.choices,
        'display_styles': SubCategory.DisplayStyle.choices,
    }

    if request.method == 'GET':
        return render(request, 'ponno/sub_category_edit.html', context_defaults)

    if request.method != 'POST':
        return redirect('ponno:home')

    # ==================== POST REQUEST HANDLING ====================
    try:
        with transaction.atomic():

            # ===== 1. EXTRACT RAW DATA =====
            sub_category_name              = request.POST.get('sub_category_name', '').strip()
            sub_category_type              = request.POST.get('sub_category_type', sub_category.sub_category_type)
            sub_category_description       = request.POST.get('sub_category_description', '').strip()
            sub_category_short_description = request.POST.get('sub_category_short_description', '').strip()
            category_id                    = request.POST.get('category', '').strip()
            brand_id                       = request.POST.get('brand', '').strip()
            display_style                  = request.POST.get('display_style', sub_category.display_style)
            display_order_str              = request.POST.get('display_order', str(sub_category.display_order)).strip()
            products_per_page_str          = request.POST.get('products_per_page', str(sub_category.products_per_page)).strip()
            icon_class                     = request.POST.get('icon_class', '').strip()
            color_code                     = request.POST.get('color_code', '').strip()
            commission_rate_str            = request.POST.get('commission_rate', str(sub_category.commission_rate)).strip()
            min_price_str                  = request.POST.get('min_price', '').strip()
            max_price_str                  = request.POST.get('max_price', '').strip()
            meta_title                     = request.POST.get('meta_title', '').strip()
            meta_description_seo           = request.POST.get('meta_description', '').strip()
            meta_keywords                  = request.POST.get('meta_keywords', '').strip()
            canonical_url                  = request.POST.get('canonical_url', '').strip()
            is_featured                    = request.POST.get('is_featured') == 'on'
            is_trending                    = request.POST.get('is_trending') == 'on'
            is_visible_in_menu             = request.POST.get('is_visible_in_menu') == 'on'
            is_visible_on_homepage         = request.POST.get('is_visible_on_homepage') == 'on'
            is_active                      = request.POST.get('is_active') == 'on'
            sub_category_image             = request.FILES.get('sub_category_image')
            sub_category_icon_file         = request.FILES.get('sub_category_icon')
            sub_category_thumbnail         = request.FILES.get('sub_category_thumbnail')

            errors = []

            # ===== 2. VALIDATE =====

            # SubCategory name (model: max_length=150)
            if not sub_category_name:
                errors.append("SubCategory name is required.")
            elif len(sub_category_name) < 2:
                errors.append("SubCategory name must be at least 2 characters.")
            elif len(sub_category_name) > 150:
                errors.append("SubCategory name must be 150 characters or fewer.")

            # Parent category
            category = None
            if not category_id:
                errors.append("Parent category is required.")
            else:
                try:
                    category = Category.objects.get(pk=category_id, is_active=True, deleted_at__isnull=True)
                except Category.DoesNotExist:
                    errors.append("Selected category does not exist or is inactive.")

            # Unique name per category — exclude current instance
            if category and sub_category_name:
                duplicate_qs = SubCategory.objects.filter(
                    category=category,
                    sub_category_name__iexact=sub_category_name
                ).exclude(pk=sub_category.pk)

                if duplicate_qs.exists():
                    errors.append(
                        f'A subcategory named "{sub_category_name}" already exists under "{category.category_name}".'
                    )

            # SubCategory type
            valid_types = [choice[0] for choice in SubCategory.SubCategoryType.choices]
            if sub_category_type not in valid_types:
                errors.append("Invalid subcategory type selected.")

            # Display style
            valid_styles = [choice[0] for choice in SubCategory.DisplayStyle.choices]
            if display_style not in valid_styles:
                errors.append("Invalid display style selected.")

            # Display order
            display_order = sub_category.display_order
            if display_order_str:
                try:
                    display_order = int(display_order_str)
                    if display_order < 0:
                        errors.append("Display order must be 0 or greater.")
                except ValueError:
                    errors.append("Display order must be a valid number.")

            # Products per page
            products_per_page = sub_category.products_per_page
            if products_per_page_str:
                try:
                    products_per_page = int(products_per_page_str)
                    if products_per_page < 1:
                        errors.append("Products per page must be at least 1.")
                except ValueError:
                    errors.append("Products per page must be a valid number.")

            # Commission rate (model: max_digits=5, decimal_places=2, 0-100)
            commission_rate = _to_decimal(
                commission_rate_str, "Commission rate", errors, default=sub_category.commission_rate
            )
            if commission_rate < 0 or commission_rate > 100:
                errors.append("Commission rate must be between 0 and 100.")

            # Min / max price (model: max_digits=10, decimal_places=2)
            min_price = _to_decimal(min_price_str, "Minimum price", errors)
            max_price = _to_decimal(max_price_str, "Maximum price", errors)
            if min_price is not None and min_price < 0:
                errors.append("Minimum price cannot be negative.")
            if max_price is not None and max_price < 0:
                errors.append("Maximum price cannot be negative.")
            if min_price is not None and max_price is not None and min_price > max_price:
                errors.append("Minimum price cannot be greater than maximum price.")

            # Color code format (model: max_length=7)
            if color_code and not re.match(r'^#[0-9A-Fa-f]{6}$', color_code):
                errors.append("Color code must be a valid hex color (e.g. '#FF5733').")

            # Icon class length (model: max_length=50)
            if icon_class and len(icon_class) > 50:
                errors.append("Icon class must be 50 characters or fewer.")

            # SubCategory description length (model: max_length=1000)
            if sub_category_description and len(sub_category_description) > 1000:
                errors.append("Description must be 1000 characters or fewer.")

            # Short description length (model: max_length=200)
            if sub_category_short_description and len(sub_category_short_description) > 200:
                errors.append("Short description must be 200 characters or fewer.")

            # SEO field lengths (model: meta_title=60, meta_description=160, meta_keywords=255)
            if meta_title and len(meta_title) > 60:
                errors.append("Meta title must be 60 characters or fewer.")
            if meta_description_seo and len(meta_description_seo) > 160:
                errors.append("Meta description must be 160 characters or fewer.")
            if meta_keywords and len(meta_keywords) > 255:
                errors.append("Meta keywords must be 255 characters or fewer.")

            # Canonical URL (model: max_length=200)
            if canonical_url:
                if not (canonical_url.startswith('http://') or canonical_url.startswith('https://')):
                    errors.append("Canonical URL must start with http:// or https://")
                elif len(canonical_url) > 200:
                    errors.append("Canonical URL must be 200 characters or fewer.")

            # Resolve optional brand
            brand = None
            if brand_id:
                try:
                    brand = Brand.objects.get(pk=brand_id, is_active=True)
                except Brand.DoesNotExist:
                    errors.append("Selected brand does not exist or is inactive.")

            if errors:
                for err in errors:
                    messages.error(request, f"❌ {err}")
                return render(request, 'ponno/sub_category_edit.html', {
                    **context_defaults,
                    'form_data': request.POST,
                })

            # ===== 3. APPLY CHANGES TO EXISTING INSTANCE =====
            sub_category.sub_category_name              = sub_category_name
            sub_category.sub_category_type              = sub_category_type
            sub_category.sub_category_description       = sub_category_description or None
            sub_category.sub_category_short_description = sub_category_short_description or None
            sub_category.category                       = category
            sub_category.brand                          = brand
            sub_category.display_style                  = display_style
            sub_category.display_order                  = display_order
            sub_category.products_per_page              = products_per_page
            sub_category.icon_class                     = icon_class or None
            sub_category.color_code                     = color_code or None
            sub_category.commission_rate                = commission_rate
            sub_category.min_price                      = min_price
            sub_category.max_price                      = max_price
            sub_category.meta_title                     = meta_title or None
            sub_category.meta_description               = meta_description_seo or None
            sub_category.meta_keywords                  = meta_keywords or None
            sub_category.canonical_url                  = canonical_url or None
            sub_category.is_featured                    = is_featured
            sub_category.is_trending                    = is_trending
            sub_category.is_visible_in_menu             = is_visible_in_menu
            sub_category.is_visible_on_homepage         = is_visible_on_homepage
            sub_category.is_active                      = is_active

            # Only replace media if a new file was uploaded
            if sub_category_image:
                sub_category.sub_category_image = sub_category_image
            if sub_category_icon_file:
                sub_category.sub_category_icon = sub_category_icon_file
            if sub_category_thumbnail:
                sub_category.sub_category_thumbnail = sub_category_thumbnail

            # ===== 4. SAVE (triggers full_clean; slug is intentionally preserved) =====
            sub_category.save()

            # ===== 5. SUCCESS =====
            messages.success(
                request,
                f"✅ SubCategory '{sub_category.sub_category_name}' updated successfully!"
            )
            return redirect(
                'ponno:subcategory_products',
                category_slug=sub_category.category.category_slug,
                sub_category_slug=sub_category.sub_category_slug,
            )

    except Exception:
        logger.exception("SubCategory update failed")
        messages.error(request, "❌ Update failed. Please check your input and try again.")
        return render(request, 'ponno/sub_category_edit.html', {
            **context_defaults,
            'form_data': request.POST,
        })