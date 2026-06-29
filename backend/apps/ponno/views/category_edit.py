# apps/ponno/views/category_edit.py

from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.db import transaction
import logging

from apps.customer.models.profile_info import ProfileInfo
from apps.ponno.models.category import Category
from engine.business_engine.business_profile import (
    invalidate_brand_cache,
    invalidate_role_cache,
)

logger = logging.getLogger(__name__)


@login_required
def CategoryEditView(request, slug):
    """
    Dealer-only category edit view.
    Mirrors CategoryCreateView pattern — manual field extraction,
    validation, and atomic save against an existing Category.

    On success:
    - Redirects to category detail page
    """
    user = request.user
    profile_info = get_object_or_404(ProfileInfo, user=user)
    category = get_object_or_404(Category, category_slug=slug, deleted_at__isnull=True)

    if user.role != 'dealer':
        messages.error(request, "❌ Only dealers can edit categories.")
        return redirect('customer:profile')

    if request.method == 'GET':
        return render(request, 'ponno/category_edit.html', {
            'user':           user,
            'profile_info':   profile_info,
            'category':       category,
            'category_types': Category.CategoryType.choices,
            'display_styles': Category.DisplayStyle.choices,
            'parent_options': Category.objects.active_categories().exclude(pk=category.pk),
        })

    # ==================== POST REQUEST HANDLING ====================
    if request.method == 'POST':
        try:
            with transaction.atomic():

                # ===== 1. EXTRACT RAW DATA =====
                category_name              = request.POST.get('category_name', '').strip()
                category_type              = request.POST.get('category_type', category.category_type)
                category_description       = request.POST.get('category_description', '').strip()
                category_short_description = request.POST.get('category_short_description', '').strip()
                parent_id                  = request.POST.get('parent', '').strip()
                icon_class                 = request.POST.get('icon_class', '').strip()
                color_code                 = request.POST.get('color_code', '').strip()
                display_style               = request.POST.get('display_style', category.display_style)
                display_order_str          = request.POST.get('display_order', str(category.display_order)).strip()
                products_per_page_str       = request.POST.get('products_per_page', str(category.products_per_page)).strip()
                show_subcategories          = request.POST.get('show_subcategories') == 'on'
                is_visible_in_menu          = request.POST.get('is_visible_in_menu') == 'on'
                is_visible_on_homepage      = request.POST.get('is_visible_on_homepage') == 'on'
                commission_rate_str         = request.POST.get('commission_rate', str(category.commission_rate)).strip()
                min_price_str               = request.POST.get('min_price', '').strip()
                max_price_str               = request.POST.get('max_price', '').strip()
                meta_title                 = request.POST.get('meta_title', '').strip()
                meta_description           = request.POST.get('meta_description', '').strip()
                meta_keywords              = request.POST.get('meta_keywords', '').strip()
                category_image             = request.FILES.get('category_image')
                category_icon              = request.FILES.get('category_icon')
                category_thumbnail         = request.FILES.get('category_thumbnail')

                # ===== 2. VALIDATE =====
                errors = []

                # Category name
                if not category_name:
                    errors.append("Category name is required.")
                elif len(category_name) < 2:
                    errors.append("Category name must be at least 2 characters.")
                elif Category.objects.filter(
                    category_name__iexact=category_name
                ).exclude(pk=category.pk).exists():
                    errors.append(f'A category named "{category_name}" already exists.')

                # Category type
                valid_types = [choice[0] for choice in Category.CategoryType.choices]
                if category_type not in valid_types:
                    errors.append("Invalid category type selected.")

                # Display style
                valid_styles = [choice[0] for choice in Category.DisplayStyle.choices]
                if display_style not in valid_styles:
                    errors.append("Invalid display style selected.")

                # Parent category — prevent self/descendant assignment
                parent = None
                if parent_id:
                    try:
                        candidate = Category.objects.active_categories().get(pk=parent_id)
                        if candidate.pk == category.pk:
                            errors.append("A category cannot be its own parent.")
                        elif candidate in category.get_descendants():
                            errors.append("Cannot set a descendant category as the parent.")
                        else:
                            parent = candidate
                    except (Category.DoesNotExist, ValueError):
                        errors.append("Selected parent category does not exist.")

                # Color code — basic hex check
                if color_code and not (color_code.startswith('#') and len(color_code) == 7):
                    errors.append("Color code must be a 7-character hex value (e.g. #FF5733).")

                # Display order
                try:
                    display_order = int(display_order_str) if display_order_str else 0
                    if display_order < 0:
                        errors.append("Display order cannot be negative.")
                except ValueError:
                    errors.append("Display order must be a valid number.")
                    display_order = category.display_order

                # Products per page
                try:
                    products_per_page = int(products_per_page_str) if products_per_page_str else 24
                    if products_per_page < 1:
                        errors.append("Products per page must be at least 1.")
                except ValueError:
                    errors.append("Products per page must be a valid number.")
                    products_per_page = category.products_per_page

                # Commission rate
                try:
                    commission_rate = float(commission_rate_str) if commission_rate_str else 0.0
                    if commission_rate < 0 or commission_rate > 100:
                        errors.append("Commission rate must be between 0 and 100.")
                except ValueError:
                    errors.append("Commission rate must be a valid number.")
                    commission_rate = category.commission_rate

                # Min / max price
                min_price = None
                max_price = None
                try:
                    if min_price_str:
                        min_price = float(min_price_str)
                except ValueError:
                    errors.append("Minimum price must be a valid number.")
                try:
                    if max_price_str:
                        max_price = float(max_price_str)
                except ValueError:
                    errors.append("Maximum price must be a valid number.")
                if min_price is not None and max_price is not None and min_price > max_price:
                    errors.append("Minimum price cannot be greater than maximum price.")

                # SEO field lengths
                if meta_title and len(meta_title) > 60:
                    errors.append("Meta title must be 60 characters or fewer.")
                if meta_description and len(meta_description) > 160:
                    errors.append("Meta description must be 160 characters or fewer.")

                # Show errors if any
                if errors:
                    for err in errors:
                        messages.error(request, f"❌ {err}")
                    return render(request, 'ponno/category_edit.html', {
                        'user':           user,
                        'profile_info':   profile_info,
                        'category':       category,
                        'category_types': Category.CategoryType.choices,
                        'display_styles': Category.DisplayStyle.choices,
                        'parent_options': Category.objects.active_categories().exclude(pk=category.pk),
                        'form_data':      request.POST,
                    })

                # ===== 3. UPDATE CATEGORY =====
                category.category_name              = category_name
                category.category_type              = category_type
                category.category_description       = category_description       or None
                category.category_short_description = category_short_description or None
                category.parent                      = parent
                category.icon_class                  = icon_class or None
                category.color_code                  = color_code or None
                category.display_style               = display_style
                category.display_order               = display_order
                category.products_per_page           = products_per_page
                category.show_subcategories          = show_subcategories
                category.is_visible_in_menu          = is_visible_in_menu
                category.is_visible_on_homepage      = is_visible_on_homepage
                category.commission_rate             = commission_rate
                category.min_price                   = min_price
                category.max_price                   = max_price
                category.meta_title                  = meta_title       or None
                category.meta_description            = meta_description or None
                category.meta_keywords               = meta_keywords    or None

                if category_image:     category.category_image     = category_image
                if category_icon:      category.category_icon      = category_icon
                if category_thumbnail: category.category_thumbnail = category_thumbnail

                # ===== 4. SAVE (triggers level/path update + full_clean) =====
                category.save()

                # ===== 5. BUST CACHE =====
                invalidate_brand_cache(user.pk)
                invalidate_role_cache(user.pk, str(user.role))

                # ===== 6. SUCCESS =====
                messages.success(
                    request,
                    f"✅ Category '{category.category_name}' updated successfully!"
                )
                return redirect('ponno:category_detail', category_slug=category.category_slug)

        except Exception as e:
            logger.exception("Category update failed")
            messages.error(request, f"❌ Update failed: {str(e)}")
            return render(request, 'ponno/category_edit.html', {
                'user':           user,
                'profile_info':   profile_info,
                'category':       category,
                'category_types': Category.CategoryType.choices,
                'display_styles': Category.DisplayStyle.choices,
                'parent_options': Category.objects.active_categories().exclude(pk=category.pk),
                'form_data':      request.POST,
            })

    return redirect('ponno:home')