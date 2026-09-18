# apps/ponno/views/sub_category_create.py

import re
import logging
from decimal import Decimal, InvalidOperation

from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.db import transaction
from django.core.exceptions import ValidationError

from apps.customer.models.profile_info import ProfileInfo
from apps.ponno.models.category import Category
from apps.ponno.models.brand import Brand
from apps.ponno.models.sub_category import SubCategory

logger = logging.getLogger(__name__)


def _to_decimal(value: str, field_label: str, errors: list):
    """Safely convert a string to Decimal, appending to errors on failure."""
    if not value:
        return None
    try:
        return Decimal(value)
    except InvalidOperation:
        errors.append(f"{field_label} must be a valid number.")
        return None


@login_required
def SubCategoryCreateView(request):
    """
    Dealer-only subcategory creation view.
    """
    user = request.user
    profile_info = get_object_or_404(ProfileInfo, user=user)

    if user.role != 'dealer':
        messages.error(request, "❌ Only dealers can create subcategories.")
        return redirect('customer:profile')

    active_categories = Category.objects.filter(is_active=True, deleted_at__isnull=True)
    active_brands = Brand.objects.filter(is_active=True)

    context_defaults = {
        'user': user,
        'profile_info': profile_info,
        'categories': active_categories,
        'brands': active_brands,
        'sub_category_types': SubCategory.SubCategoryType.choices,
        'display_styles': SubCategory.DisplayStyle.choices,
    }

    if request.method == 'GET':
        return render(request, 'ponno/sub_category_create.html', context_defaults)

    if request.method != 'POST':
        return redirect('ponno:home')

    # ==================== POST REQUEST HANDLING ====================
    try:
        with transaction.atomic():

            # ===== 1. EXTRACT RAW DATA =====
            sub_category_name              = request.POST.get('sub_category_name', '').strip()
            sub_category_type              = request.POST.get('sub_category_type', SubCategory.SubCategoryType.PRODUCT)
            sub_category_description       = request.POST.get('sub_category_description', '').strip()
            sub_category_short_description = request.POST.get('sub_category_short_description', '').strip()
            category_id                    = request.POST.get('category', '').strip()
            brand_id                       = request.POST.get('brand', '').strip()
            display_style                  = request.POST.get('display_style', SubCategory.DisplayStyle.GRID)
            display_order_str              = request.POST.get('display_order', '0').strip()
            products_per_page_str          = request.POST.get('products_per_page', '24').strip()
            icon_class                     = request.POST.get('icon_class', '').strip()
            color_code                     = request.POST.get('color_code', '').strip()
            commission_rate_str            = request.POST.get('commission_rate', '0').strip()
            min_price_str                  = request.POST.get('min_price', '').strip()
            max_price_str                  = request.POST.get('max_price', '').strip()
            meta_title                     = request.POST.get('meta_title', '').strip()
            meta_description_seo          = request.POST.get('meta_description', '').strip()
            meta_keywords                  = request.POST.get('meta_keywords', '').strip()
            canonical_url                  = request.POST.get('canonical_url', '').strip()
            is_featured                    = request.POST.get('is_featured') == 'on'
            is_trending                    = request.POST.get('is_trending') == 'on'
            is_visible_in_menu             = request.POST.get('is_visible_in_menu') == 'on'
            is_visible_on_homepage         = request.POST.get('is_visible_on_homepage') == 'on'
            sub_category_image             = request.FILES.get('sub_category_image')
            sub_category_icon              = request.FILES.get('sub_category_icon')
            sub_category_thumbnail         = request.FILES.get('sub_category_thumbnail')
            managed_by_ids                 = request.POST.getlist('managed_by')  # optional multi-select

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

            # Unique name per category
            if category and sub_category_name:
                if SubCategory.objects.filter(
                    category=category,
                    sub_category_name__iexact=sub_category_name
                ).exists():
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
            display_order = 0
            if display_order_str:
                try:
                    display_order = int(display_order_str)
                    if display_order < 0:
                        errors.append("Display order must be 0 or greater.")
                except ValueError:
                    errors.append("Display order must be a valid number.")

            # Products per page
            products_per_page = 24
            if products_per_page_str:
                try:
                    products_per_page = int(products_per_page_str)
                    if products_per_page < 1:
                        errors.append("Products per page must be at least 1.")
                except ValueError:
                    errors.append("Products per page must be a valid number.")

            # Commission rate (model: max_digits=5, decimal_places=2, 0-100)
            commission_rate = _to_decimal(commission_rate_str, "Commission rate", errors) or Decimal('0.0')
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

            # Resolve optional managers (must be dealer's own team, adjust as needed)
            managed_users = []
            if managed_by_ids:
                from django.contrib.auth import get_user_model
                User = get_user_model()
                managed_users = list(User.objects.filter(pk__in=managed_by_ids))

            if errors:
                for err in errors:
                    messages.error(request, f"❌ {err}")
                return render(request, 'ponno/sub_category_create.html', {
                    **context_defaults,
                    'form_data': request.POST,
                })

            # ===== 3. CREATE SUBCATEGORY =====
            sub_category = SubCategory(
                sub_category_name              = sub_category_name,
                sub_category_type              = sub_category_type,
                sub_category_description       = sub_category_description or None,
                sub_category_short_description = sub_category_short_description or None,
                category                       = category,
                brand                          = brand,
                created_by                     = user,
                display_style                  = display_style,
                display_order                  = display_order,
                products_per_page              = products_per_page,
                icon_class                     = icon_class or None,
                color_code                     = color_code or None,
                commission_rate                = commission_rate,
                min_price                      = min_price,
                max_price                      = max_price,
                meta_title                     = meta_title or None,
                meta_description               = meta_description_seo or None,
                meta_keywords                  = meta_keywords or None,
                canonical_url                  = canonical_url or None,
                is_featured                    = is_featured,
                is_trending                    = is_trending,
                is_visible_in_menu             = is_visible_in_menu,
                is_visible_on_homepage         = is_visible_on_homepage,
                is_active                      = True,
            )

            if sub_category_image:
                sub_category.sub_category_image = sub_category_image
            if sub_category_icon:
                sub_category.sub_category_icon = sub_category_icon
            if sub_category_thumbnail:
                sub_category.sub_category_thumbnail = sub_category_thumbnail

            # ===== 4. SAVE (triggers slug gen + full_clean via model.save) =====
            sub_category.save()

            # ===== 5. M2M — set after the instance has a PK =====
            if managed_users:
                sub_category.managed_by.set(managed_users)

            # ===== 6. SUCCESS =====
            messages.success(
                request,
                f"✅ SubCategory '{sub_category.sub_category_name}' created successfully!"
            )
            return redirect('ponno:subcategory_products', category_slug=sub_category.sub_category_slug)

    except ValidationError as e:
        # Catches anything the model's own clean()/full_clean() rejects
        # that our manual checks above didn't already cover.
        if hasattr(e, 'message_dict'):
            for field, msgs in e.message_dict.items():
                for msg in msgs:
                    messages.error(request, f"❌ {field}: {msg}")
        else:
            for msg in e.messages:
                messages.error(request, f"❌ {msg}")
        return render(request, 'ponno/sub_category_create.html', {
            **context_defaults,
            'form_data': request.POST,
        })

    except Exception as e:
        logger.exception("SubCategory creation failed")
        messages.error(request, f"❌ Creation failed: {str(e)}")
        return render(request, 'ponno/sub_category_create.html', {
            **context_defaults,
            'form_data': request.POST,
        })