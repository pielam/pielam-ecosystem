# apps/ponno/views/brand_edit.py

import logging
from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.db import transaction
from django.utils import timezone

from apps.customer.models.profile_info import ProfileInfo
from apps.ponno.models.brand import Brand
from engine.business_engine.business_profile import (
    invalidate_brand_cache,
    invalidate_role_cache,
)

logger = logging.getLogger(__name__)


@login_required
def BrandEditView(request, slug):
    """
    Dealer-only brand edit view.

    - Fetches the existing Brand by slug and updates it in place.
    - Slug is NOT changed on edit (preserves existing URLs).
    - On success busts biz:brands and biz:role cache slices and
      redirects to the brand detail page.
    """
    user = request.user
    profile_info = get_object_or_404(ProfileInfo, user=user)

    if user.role != 'dealer':
        messages.error(request, "❌ Only dealers can edit brands.")
        return redirect('customer:profile')

    brand = get_object_or_404(Brand, brand_slug=slug)

    context_defaults = {
        'user': user,
        'profile_info': profile_info,
        'brand': brand,
        'brand_types': Brand.BrandType.choices,
    }

    if request.method == 'GET':
        return render(request, 'ponno/brand_edit.html', context_defaults)

    if request.method != 'POST':
        return redirect('ponno:home')

    # ==================== POST REQUEST HANDLING ====================
    try:
        with transaction.atomic():

            # ===== 1. EXTRACT RAW DATA =====
            brand_name        = request.POST.get('brand_name', '').strip()
            brand_type        = request.POST.get('brand_type', brand.brand_type)
            brand_tagline     = request.POST.get('brand_tagline', '').strip()
            brand_description = request.POST.get('brand_description', '').strip()
            company_name      = request.POST.get('company_name', '').strip()
            founded_year_str  = request.POST.get('founded_year', '').strip()
            country_of_origin = request.POST.get('country_of_origin', '').strip()
            headquarters      = request.POST.get('headquarters', '').strip()
            brand_website     = request.POST.get('brand_website', '').strip()
            brand_email        = request.POST.get('brand_email', '').strip()
            brand_phone        = request.POST.get('brand_phone', '').strip()
            support_email      = request.POST.get('support_email', '').strip()
            support_phone       = request.POST.get('support_phone', '').strip()
            social_facebook    = request.POST.get('social_facebook', '').strip()
            social_instagram   = request.POST.get('social_instagram', '').strip()
            social_twitter     = request.POST.get('social_twitter', '').strip()
            social_linkedin    = request.POST.get('social_linkedin', '').strip()
            social_youtube     = request.POST.get('social_youtube', '').strip()
            meta_title         = request.POST.get('meta_title', '').strip()
            meta_description   = request.POST.get('meta_description', '').strip()
            meta_keywords      = request.POST.get('meta_keywords', '').strip()
            is_active           = request.POST.get('is_active') == 'on'
            brand_logo         = request.FILES.get('brand_logo')
            brand_banner       = request.FILES.get('brand_banner')
            brand_icon         = request.FILES.get('brand_icon')

            errors = []

            # ===== 2. VALIDATE =====

            # Brand name — exclude current instance from the uniqueness check
            if not brand_name:
                errors.append("Brand name is required.")
            elif len(brand_name) < 2:
                errors.append("Brand name must be at least 2 characters.")
            elif Brand.objects.filter(brand_name__iexact=brand_name).exclude(pk=brand.pk).exists():
                errors.append(f'A brand named "{brand_name}" already exists.')

            # Brand type
            valid_types = [choice[0] for choice in Brand.BrandType.choices]
            if brand_type not in valid_types:
                errors.append("Invalid brand type selected.")

            # Founded year
            founded_year = brand.founded_year
            if founded_year_str:
                try:
                    founded_year = int(founded_year_str)
                    current_year = timezone.now().year
                    if founded_year < 1800 or founded_year > current_year:
                        errors.append(f"Founded year must be between 1800 and {current_year}.")
                except ValueError:
                    errors.append("Founded year must be a valid number.")

            # Country code length
            if country_of_origin and len(country_of_origin) > 2:
                errors.append("Country of origin must be a 2-letter ISO code (e.g. BD).")

            # URL fields — basic prefix check
            url_fields = {
                'Brand website':    brand_website,
                'Social Facebook':  social_facebook,
                'Social Instagram': social_instagram,
                'Social Twitter':   social_twitter,
                'Social LinkedIn':  social_linkedin,
                'Social YouTube':   social_youtube,
            }
            for label, url in url_fields.items():
                if url and not (url.startswith('http://') or url.startswith('https://')):
                    errors.append(f"{label} must start with http:// or https://")

            # SEO field lengths
            if meta_title and len(meta_title) > 60:
                errors.append("Meta title must be 60 characters or fewer.")
            if meta_description and len(meta_description) > 160:
                errors.append("Meta description must be 160 characters or fewer.")

            if errors:
                for err in errors:
                    messages.error(request, f"❌ {err}")
                return render(request, 'ponno/brand_edit.html', {
                    **context_defaults,
                    'form_data': request.POST,
                })

            # ===== 3. APPLY CHANGES TO EXISTING INSTANCE =====
            brand.brand_name        = brand_name
            brand.brand_type        = brand_type
            brand.brand_tagline     = brand_tagline or None
            brand.brand_description = brand_description or None
            brand.company_name      = company_name or None
            brand.founded_year      = founded_year
            brand.country_of_origin = country_of_origin or None
            brand.headquarters      = headquarters or None
            brand.brand_website     = brand_website or None
            brand.brand_email       = brand_email or None
            brand.brand_phone       = brand_phone or None
            brand.support_email     = support_email or None
            brand.support_phone     = support_phone or None
            brand.social_facebook   = social_facebook or None
            brand.social_instagram  = social_instagram or None
            brand.social_twitter    = social_twitter or None
            brand.social_linkedin   = social_linkedin or None
            brand.social_youtube    = social_youtube or None
            brand.meta_title        = meta_title or None
            brand.meta_description  = meta_description or None
            brand.meta_keywords     = meta_keywords or None
            brand.is_active         = is_active

            # Only replace media if a new file was uploaded
            if brand_logo:
                brand.brand_logo = brand_logo
            if brand_banner:
                brand.brand_banner = brand_banner
            if brand_icon:
                brand.brand_icon = brand_icon

            # ===== 4. SAVE (slug intentionally preserved) =====
            brand.save()

            # ===== 5. BUST CACHE =====
            invalidate_brand_cache(user.pk)
            invalidate_role_cache(user.pk, str(user.role))

            # ===== 6. SUCCESS =====
            messages.success(
                request,
                f"✅ Brand '{brand.brand_name}' updated successfully!"
            )
            return redirect('ponno:brand_products', brand_slug=brand.brand_slug)

    except Exception:
        logger.exception("Brand update failed")
        messages.error(request, "❌ Update failed. Please check your input and try again.")
        return render(request, 'ponno/brand_edit.html', {
            **context_defaults,
            'form_data': request.POST,
        })