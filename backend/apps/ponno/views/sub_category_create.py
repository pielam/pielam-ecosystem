# apps/ponno/views/sub_category_create.py

from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.db import transaction
import logging

from apps.customer.models.profile_info import ProfileInfo
from apps.ponno.models.category import Category
from apps.ponno.models.brand import Brand
from apps.ponno.models.sub_category import SubCategory

logger = logging.getLogger(__name__)


@login_required
def SubCategoryCreateView(request):
    """
    Dealer-only subcategory creation view.
    Mirrors BrandCreateView pattern — manual field extraction,
    validation, and atomic save.

    On success:
    - Auto-generates slug from category_name + sub_category_name
    - Redirects to the parent category's subcategory list (or home)
    """
    user = request.user
    profile_info = get_object_or_404(ProfileInfo, user=user)

    if user.role != 'dealer':
        messages.error(request, "❌ Only dealers can create subcategories.")
        return redirect('customer:profile')

    active_categories = Category.objects.filter(is_active=True, deleted_at__isnull=True)
    active_brands     = Brand.objects.filter(is_active=True)

    if request.method == 'GET':
        return render(request, 'ponno/sub_category_create.html', {
            'user':             user,
            'profile_info':     profile_info,
            'categories':       active_categories,
            'brands':           active_brands,
            'sub_category_types': SubCategory.SubCategoryType.choices,
            'display_styles':   SubCategory.DisplayStyle.choices,
        })

    # ==================== POST REQUEST HANDLING ====================
    if request.method == 'POST':
        try:
            with transaction.atomic():

                # ===== 1. EXTRACT RAW DATA =====
                sub_category_name             = request.POST.get('sub_category_name',             '').strip()
                sub_category_type             = request.POST.get('sub_category_type',             SubCategory.SubCategoryType.PRODUCT)
                sub_category_description      = request.POST.get('sub_category_description',      '').strip()
                sub_category_short_description= request.POST.get('sub_category_short_description','').strip()
                category_id                   = request.POST.get('category',                      '').strip()
                brand_id                      = request.POST.get('brand',                         '').strip()
                display_style                 = request.POST.get('display_style',                 SubCategory.DisplayStyle.GRID)
                display_order_str             = request.POST.get('display_order',                 '0').strip()
                products_per_page_str         = request.POST.get('products_per_page',             '24').strip()
                icon_class                    = request.POST.get('icon_class',                    '').strip()
                color_code                    = request.POST.get('color_code',                    '').strip()
                commission_rate_str           = request.POST.get('commission_rate',               '0').strip()
                min_price_str                 = request.POST.get('min_price',                     '').strip()
                max_price_str                 = request.POST.get('max_price',                     '').strip()
                meta_title                    = request.POST.get('meta_title',                    '').strip()
                meta_description_seo          = request.POST.get('meta_description',              '').strip()
                meta_keywords                 = request.POST.get('meta_keywords',                 '').strip()
                canonical_url                 = request.POST.get('canonical_url',                 '').strip()
                is_featured                   = request.POST.get('is_featured')    == 'on'
                is_trending                   = request.POST.get('is_trending')    == 'on'
                is_visible_in_menu            = request.POST.get('is_visible_in_menu')    == 'on'
                is_visible_on_homepage        = request.POST.get('is_visible_on_homepage') == 'on'
                sub_category_image            = request.FILES.get('sub_category_image')
                sub_category_icon             = request.FILES.get('sub_category_icon')
                sub_category_thumbnail        = request.FILES.get('sub_category_thumbnail')

                # ===== 2. VALIDATE =====
                errors = []

                # SubCategory name
                if not sub_category_name:
                    errors.append("SubCategory name is required.")
                elif len(sub_category_name) < 2:
                    errors.append("SubCategory name must be at least 2 characters.")

                # Parent category
                category = None
                if not category_id:
                    errors.append("Parent category is required.")
                else:
                    try:
                        category = Category.objects.get(pk=category_id, is_active=True, deleted_at__isnull=True)
                    except Category.DoesNotExist:
                        errors.append("Selected category does not exist or is inactive.")

                # Unique name per category (only check if both are present)
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

                # Commission rate
                commission_rate = 0.0
                if commission_rate_str:
                    try:
                        commission_rate = float(commission_rate_str)
                        if commission_rate < 0 or commission_rate > 100:
                            errors.append("Commission rate must be between 0 and 100.")
                    except ValueError:
                        errors.append("Commission rate must be a valid number.")

                # Min / max price
                min_price = None
                max_price = None
                if min_price_str:
                    try:
                        min_price = float(min_price_str)
                        if min_price < 0:
                            errors.append("Minimum price cannot be negative.")
                    except ValueError:
                        errors.append("Minimum price must be a valid number.")
                if max_price_str:
                    try:
                        max_price = float(max_price_str)
                        if max_price < 0:
                            errors.append("Maximum price cannot be negative.")
                    except ValueError:
                        errors.append("Maximum price must be a valid number.")
                if min_price is not None and max_price is not None:
                    if min_price > max_price:
                        errors.append("Minimum price cannot be greater than maximum price.")

                # Color code format
                if color_code:
                    import re
                    if not re.match(r'^#[0-9A-Fa-f]{6}$', color_code):
                        errors.append("Color code must be a valid hex color (e.g. '#FF5733').")

                # SEO field lengths
                if meta_title and len(meta_title) > 60:
                    errors.append("Meta title must be 60 characters or fewer.")
                if meta_description_seo and len(meta_description_seo) > 160:
                    errors.append("Meta description must be 160 characters or fewer.")

                # Canonical URL prefix check
                if canonical_url and not (canonical_url.startswith('http://') or canonical_url.startswith('https://')):
                    errors.append("Canonical URL must start with http:// or https://")

                # Short description length
                if sub_category_short_description and len(sub_category_short_description) > 200:
                    errors.append("Short description must be 200 characters or fewer.")

                # Show errors if any
                if errors:
                    for err in errors:
                        messages.error(request, f"❌ {err}")
                    return render(request, 'ponno/sub_category_create.html', {
                        'user':               user,
                        'profile_info':       profile_info,
                        'categories':         active_categories,
                        'brands':             active_brands,
                        'sub_category_types': SubCategory.SubCategoryType.choices,
                        'display_styles':     SubCategory.DisplayStyle.choices,
                        'form_data':          request.POST,
                    })

                # ===== 3. RESOLVE OPTIONAL BRAND =====
                brand = None
                if brand_id:
                    try:
                        brand = Brand.objects.get(pk=brand_id, is_active=True)
                    except Brand.DoesNotExist:
                        pass  # brand is optional — silently ignore invalid id

                # ===== 4. CREATE SUBCATEGORY =====
                sub_category = SubCategory(
                    sub_category_name              = sub_category_name,
                    sub_category_type              = sub_category_type,
                    sub_category_description       = sub_category_description       or None,
                    sub_category_short_description = sub_category_short_description or None,
                    category                       = category,
                    brand                          = brand,
                    display_style                  = display_style,
                    display_order                  = display_order,
                    products_per_page              = products_per_page,
                    icon_class                     = icon_class                     or None,
                    color_code                     = color_code                     or None,
                    commission_rate                = commission_rate,
                    min_price                      = min_price,
                    max_price                      = max_price,
                    meta_title                     = meta_title                     or None,
                    meta_description               = meta_description_seo           or None,
                    meta_keywords                  = meta_keywords                  or None,
                    canonical_url                  = canonical_url                  or None,
                    is_featured                    = is_featured,
                    is_trending                    = is_trending,
                    is_visible_in_menu             = is_visible_in_menu,
                    is_visible_on_homepage         = is_visible_on_homepage,
                    is_active                      = True,
                )

                if sub_category_image:     sub_category.sub_category_image     = sub_category_image
                if sub_category_icon:      sub_category.sub_category_icon      = sub_category_icon
                if sub_category_thumbnail: sub_category.sub_category_thumbnail = sub_category_thumbnail

                # ===== 5. SAVE (triggers slug gen + full_clean) =====
                sub_category.save()

                # ===== 6. SUCCESS =====
                messages.success(
                    request,
                    f"✅ SubCategory '{sub_category.sub_category_name}' created successfully!"
                )
                return redirect('ponno:home')

        except Exception as e:
            logger.exception("SubCategory creation failed")
            messages.error(request, f"❌ Creation failed: {str(e)}")
            return render(request, 'ponno/sub_category_create.html', {
                'user':               user,
                'profile_info':       profile_info,
                'categories':         active_categories,
                'brands':             active_brands,
                'sub_category_types': SubCategory.SubCategoryType.choices,
                'display_styles':     SubCategory.DisplayStyle.choices,
                'form_data':          request.POST,
            })

    return redirect('ponno:home')