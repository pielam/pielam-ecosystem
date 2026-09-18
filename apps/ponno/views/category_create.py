# apps/ponno/views/category_create.py

from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.db import transaction
import logging

from apps.customer.models.profile_info import ProfileInfo
from apps.ponno.models.brand import Brand
from apps.ponno.models.category import Category

from engine.business_engine.business_profile import (
    invalidate_category_cache,   # ← was invalidate_brand_cache
    invalidate_role_cache,
)

logger = logging.getLogger(__name__)


@login_required
def CategoryCreateView(request):
    """
    Dealer-only category creation view.
    Mirrors BrandCreateView pattern — manual field extraction,
    validation, and atomic save.

    On success:
    - Sets created_by to the logged-in user
    - Updates level and path via Category.save()
    - Busts biz:brands and biz:role cache slices
    - Redirects to category detail page
    """
    user = request.user
    profile_info = get_object_or_404(ProfileInfo, user=user)

    if user.role != 'dealer':
        messages.error(request, "❌ Only dealers can create categories.")
        return redirect('customer:profile')

    # Querysets needed by the form
    active_brands     = Brand.objects.active_brands()
    parent_categories = Category.objects.active_categories().order_by('level', 'category_name')

    if request.method == 'GET':
        return render(request, 'ponno/category_create.html', {
            'user':             user,
            'profile_info':     profile_info,
            'category_types':   Category.CategoryType.choices,
            'display_styles':   Category.DisplayStyle.choices,
            'active_brands':    active_brands,
            'parent_categories': parent_categories,
        })

    # ==================== POST REQUEST HANDLING ====================
    if request.method == 'POST':
        try:
            with transaction.atomic():

                # ===== 1. EXTRACT RAW DATA =====
                category_name             = request.POST.get('category_name',             '').strip()
                category_type             = request.POST.get('category_type',             Category.CategoryType.PRODUCT)
                category_description      = request.POST.get('category_description',      '').strip()
                category_short_description = request.POST.get('category_short_description', '').strip()
                parent_id                 = request.POST.get('parent',                    '').strip()
                brand_id                  = request.POST.get('brand',                     '').strip()

                # Display settings
                display_style             = request.POST.get('display_style',             Category.DisplayStyle.GRID)
                display_order_str         = request.POST.get('display_order',             '0').strip()
                products_per_page_str     = request.POST.get('products_per_page',         '24').strip()
                color_code                = request.POST.get('color_code',                '').strip()
                icon_class                = request.POST.get('icon_class',                '').strip()

                # Visibility flags
                is_featured               = request.POST.get('is_featured')               == 'on'
                is_trending               = request.POST.get('is_trending')               == 'on'
                is_visible_in_menu        = request.POST.get('is_visible_in_menu')        == 'on'
                is_visible_on_homepage    = request.POST.get('is_visible_on_homepage')    == 'on'
                show_subcategories        = request.POST.get('show_subcategories')        == 'on'

                # Commission & pricing
                commission_rate_str       = request.POST.get('commission_rate',           '0').strip()
                min_price_str             = request.POST.get('min_price',                 '').strip()
                max_price_str             = request.POST.get('max_price',                 '').strip()

                # SEO
                meta_title                = request.POST.get('meta_title',                '').strip()
                meta_description          = request.POST.get('meta_description',          '').strip()
                meta_keywords             = request.POST.get('meta_keywords',             '').strip()
                canonical_url             = request.POST.get('canonical_url',             '').strip()

                # Media
                category_image            = request.FILES.get('category_image')
                category_icon             = request.FILES.get('category_icon')
                category_thumbnail        = request.FILES.get('category_thumbnail')

                # ===== 2. VALIDATE =====
                errors = []

                # Category name
                if not category_name:
                    errors.append("Category name is required.")
                elif len(category_name) < 2:
                    errors.append("Category name must be at least 2 characters.")
                elif Category.objects.filter(category_name__iexact=category_name).exists():
                    errors.append(f'A category named "{category_name}" already exists.')

                # Category type
                valid_types = [choice[0] for choice in Category.CategoryType.choices]
                if category_type not in valid_types:
                    errors.append("Invalid category type selected.")

                # Display style
                valid_styles = [choice[0] for choice in Category.DisplayStyle.choices]
                if display_style not in valid_styles:
                    errors.append("Invalid display style selected.")

                # Parent category
                parent = None
                if parent_id:
                    try:
                        parent = Category.objects.get(
                            pk=int(parent_id),
                            is_active=True,
                            deleted_at__isnull=True
                        )
                    except (Category.DoesNotExist, ValueError):
                        errors.append("Selected parent category does not exist or is inactive.")

                # Brand (optional)
                brand = None
                if brand_id:
                    try:
                        brand = Brand.objects.get(
                            pk=int(brand_id),
                            is_active=True,
                            deleted_at__isnull=True
                        )
                    except (Brand.DoesNotExist, ValueError):
                        errors.append("Selected brand does not exist or is inactive.")

                # Display order
                display_order = 0
                try:
                    display_order = int(display_order_str)
                    if display_order < 0:
                        errors.append("Display order must be a non-negative number.")
                except ValueError:
                    errors.append("Display order must be a valid number.")

                # Products per page
                products_per_page = 24
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
                if min_price_str:
                    try:
                        min_price = float(min_price_str)
                        if min_price < 0:
                            errors.append("Minimum price cannot be negative.")
                    except ValueError:
                        errors.append("Minimum price must be a valid number.")

                max_price = None
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

                # Color code
                if color_code:
                    if not color_code.startswith('#') or len(color_code) != 7:
                        errors.append("Color code must be a valid hex value (e.g. #FF5733).")

                # Canonical URL
                if canonical_url and not (
                    canonical_url.startswith('http://') or canonical_url.startswith('https://')
                ):
                    errors.append("Canonical URL must start with http:// or https://")

                # SEO field lengths
                if meta_title and len(meta_title) > 60:
                    errors.append("Meta title must be 60 characters or fewer.")
                if meta_description and len(meta_description) > 160:
                    errors.append("Meta description must be 160 characters or fewer.")

                # Show errors if any
                if errors:
                    for err in errors:
                        messages.error(request, f"❌ {err}")
                    return render(request, 'ponno/category_create.html', {
                        'user':              user,
                        'profile_info':      profile_info,
                        'category_types':    Category.CategoryType.choices,
                        'display_styles':    Category.DisplayStyle.choices,
                        'active_brands':     active_brands,
                        'parent_categories': parent_categories,
                        'form_data':         request.POST,
                    })

                # ===== 3. CREATE CATEGORY =====
                category = Category(
                    category_name              = category_name,
                    category_type              = category_type,
                    category_description       = category_description       or None,
                    category_short_description = category_short_description or None,
                    parent                     = parent,
                    brand                      = brand,
                    display_style              = display_style,
                    display_order              = display_order,
                    products_per_page          = products_per_page,
                    color_code                 = color_code                 or None,
                    icon_class                 = icon_class                 or None,
                    is_featured                = is_featured,
                    is_trending                = is_trending,
                    is_visible_in_menu         = is_visible_in_menu,
                    is_visible_on_homepage     = is_visible_on_homepage,
                    show_subcategories         = show_subcategories,
                    commission_rate            = commission_rate,
                    min_price                  = min_price,
                    max_price                  = max_price,
                    meta_title                 = meta_title                 or None,
                    meta_description           = meta_description           or None,
                    meta_keywords              = meta_keywords              or None,
                    canonical_url              = canonical_url              or None,
                    created_by                 = user,
                    is_active                  = True,
                )

                if category_image:     category.category_image     = category_image
                if category_icon:      category.category_icon      = category_icon
                if category_thumbnail: category.category_thumbnail = category_thumbnail

                # ===== 4. SAVE (triggers slug gen + level/path update + full_clean) =====
                category.save()

                # ===== 5. BUST CACHE =====
                # invalidate_brand_cache(user.pk)
                # invalidate_role_cache(user.pk, str(user.role))
                
                invalidate_category_cache(user.pk)               # busts biz:cats:{uid}
                invalidate_role_cache(user.pk, str(user.role))   # busts biz:role:{role}:{uid}

                # ===== 6. SUCCESS =====
                messages.success(
                    request,
                    f"✅ Category '{category.category_name}' created successfully!"
                )
                return redirect('ponno:category_products', category_slug=category.category_slug)

        except Exception as e:
            logger.exception("Category creation failed")
            messages.error(request, f"❌ Creation failed: {str(e)}")
            return render(request, 'ponno/category_create.html', {
                'user':              user,
                'profile_info':      profile_info,
                'category_types':    Category.CategoryType.choices,
                'display_styles':    Category.DisplayStyle.choices,
                'active_brands':     active_brands,
                'parent_categories': parent_categories,
                'form_data':         request.POST,
            })

    return redirect('ponno:home')