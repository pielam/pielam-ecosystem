# apps/customer/views/public_profile.py

from django.http import JsonResponse
from django.shortcuts import render, get_object_or_404
from django.contrib.auth.decorators import login_required
from django.views.decorators.http import require_POST
from django.core.paginator import Paginator, EmptyPage, PageNotAnInteger
from django.utils import timezone

from apps.customer.models.account import User
from apps.customer.models.profile_info import ProfileInfo
from apps.customer.models.profile_view_log import ProfileViewLog
from apps.ponno.models.product import Product


# ────────────────────────────────────────────────────────────────────
# HELPERS
# ────────────────────────────────────────────────────────────────────

PRODUCTS_PER_PAGE = 12

_SORT_MAP = {
    "newest":     "-created_at",
    "oldest":     "created_at",
    "price_asc":  "final_price",
    "price_desc": "-final_price",
    "popular":    "-view_count",
    "top_rated":  "-rating_average",
    "best_sell":  "-total_sales",
}


def _get_client_ip(request):
    x_forwarded = request.META.get("HTTP_X_FORWARDED_FOR")
    if x_forwarded:
        return x_forwarded.split(",")[0].strip()
    return request.META.get("REMOTE_ADDR")


def _log_profile_view(request, profile_user):
    """
    For authenticated visitors: upsert so one row per viewer,
    timestamp refreshes on every revisit.
    For anonymous: always insert a new row (no viewer FK).
    """
    ip = _get_client_ip(request)

    if request.user.is_authenticated:
        ProfileViewLog.objects.update_or_create(
            profile_user=profile_user,
            viewer=request.user,
            defaults={
                "viewed_at": timezone.now(),
                "ip_address": ip,
            },
        )
    else:
        ProfileViewLog.objects.create(
            profile_user=profile_user,
            viewer=None,
            ip_address=ip,
        )


def _get_dealer_products(profile_user, request):
    qs = (
        Product.objects
        .filter(
            dealer=profile_user,
            is_active=True,
            deleted_at__isnull=True,
        )
        .select_related("brand", "category", "sub_category")
        .order_by("-created_at")
    )

    q = request.GET.get("q", "").strip()
    if q:
        from django.db.models import Q
        qs = qs.filter(
            Q(product_name__icontains=q) |
            Q(product_title__icontains=q) |
            Q(sku__icontains=q) |
            Q(short_description__icontains=q)
        )

    category_id = request.GET.get("category", "").strip()
    if category_id.isdigit():
        qs = qs.filter(category_id=int(category_id))

    brand_id = request.GET.get("brand", "").strip()
    if brand_id.isdigit():
        qs = qs.filter(brand_id=int(brand_id))

    stock = request.GET.get("stock", "").strip()
    if stock == "in_stock":
        qs = qs.filter(stock__gt=0)
    elif stock == "out_of_stock":
        qs = qs.filter(stock=0)

    sort_key = request.GET.get("sort", "newest")
    qs = qs.order_by(_SORT_MAP.get(sort_key, "-created_at"))

    paginator = Paginator(qs, PRODUCTS_PER_PAGE)
    page_num  = request.GET.get("page", 1)

    try:
        page_obj = paginator.page(page_num)
    except PageNotAnInteger:
        page_obj = paginator.page(1)
    except EmptyPage:
        page_obj = paginator.page(paginator.num_pages)

    return page_obj, qs.count(), q, sort_key, category_id, brand_id, stock


# ────────────────────────────────────────────────────────────────────
# PUBLIC PROFILE VIEW
# ────────────────────────────────────────────────────────────────────

@login_required(login_url='/customer/signin/')
def PublicProfileView(request, username):
    profile_user = get_object_or_404(
        User.objects.select_related("profileinfo"),
        email_or_phone=username,
    )
    profile_info         = get_object_or_404(ProfileInfo, user=profile_user)
    current_user_profile = get_object_or_404(ProfileInfo, user=request.user)

    # Log view + increment counter — skip own profile
    if request.user != profile_user:
        _log_profile_view(request, profile_user)
        profile_info.increment_view_count()

    is_following = current_user_profile.is_following(profile_user)

    # Recent authenticated viewers — most recent first, max 20
    recent_viewers = (
        ProfileViewLog.objects
        .filter(profile_user=profile_user, viewer__isnull=False)
        .select_related("viewer", "viewer__profileinfo")
        .order_by("-viewed_at")[:20]
    )

    # ── Products (dealers only) ───────────────────────────────────────
    products_page     = None
    total_products    = 0
    search_query      = ""
    active_sort       = "newest"
    active_category   = ""
    active_brand      = ""
    active_stock      = ""
    dealer_categories = []
    dealer_brands     = []

    if profile_user.role == "dealer":
        (
            products_page,
            total_products,
            search_query,
            active_sort,
            active_category,
            active_brand,
            active_stock,
        ) = _get_dealer_products(profile_user, request)

        base_qs = Product.objects.filter(
            dealer=profile_user,
            is_active=True,
            deleted_at__isnull=True,
        )
        dealer_categories = (
            base_qs
            .exclude(category__isnull=True)
            .values_list("category__id", "category__category_name")
            .distinct()
            .order_by("category__category_name")
        )
        dealer_brands = (
            base_qs
            .exclude(brand__isnull=True)
            .values_list("brand__id", "brand__brand_name")
            .distinct()
            .order_by("brand__brand_name")
        )

    context = {
        "profile_user":         profile_user,
        "profile_info":         profile_info,
        "current_user":         request.user,
        "current_user_profile": current_user_profile,
        "is_following":         is_following,
        "follower_count":       profile_info.follower_count,
        "following_count":      profile_info.following_count,
      
        "products_page":        products_page,
        "total_products":       total_products,
        "search_query":         search_query,
        "active_sort":          active_sort,
        "active_category":      active_category,
        "active_brand":         active_brand,
        "active_stock":         active_stock,
        "dealer_categories":    dealer_categories,
        "dealer_brands":        dealer_brands,
        "sort_options":         list(_SORT_MAP.keys()),
    }

    return render(request, "personal/public_profile.html", context)


# ────────────────────────────────────────────────────────────────────
# FOLLOW / UNFOLLOW
# ────────────────────────────────────────────────────────────────────
from apps.ponno.views.discovery_engine_views import invalidate_user_feed  # already exists
from apps.notify.services import notice
from apps.notify.models.ring_bell import Notification

# apps/customer/views/public_profile.py
from django.urls import reverse
@require_POST
@login_required(login_url='/customer/signin/')
def follow(request, username):
    user_to_follow = get_object_or_404(User, email_or_phone=username)

    if request.user == user_to_follow:
        return JsonResponse({"error": "You cannot follow yourself."}, status=400)

    current_profile = get_object_or_404(ProfileInfo, user=request.user)
    target_profile  = get_object_or_404(ProfileInfo, user=user_to_follow)

    current_profile.following.add(user_to_follow)
    target_profile.followers.add(request.user)

    invalidate_user_feed(request.user.id)
    invalidate_user_feed(user_to_follow.id)

    notice(
        recipient=user_to_follow,
        actor=request.user,
        notification_type=Notification.NotificationType.FOLLOW,
        title=f"{request.user.email_or_phone} started following you",
        action_url=reverse('customer:profile_view', kwargs={'username': request.user.email_or_phone}),
        image=current_profile.get_profile_photo_url(),
        priority=Notification.Priority.NORMAL,
    )
    return JsonResponse({
        "following":      True,
        "follower_count": target_profile.follower_count,
    })

@require_POST
@login_required(login_url='/customer/signin/')
def unfollow(request, username):
    user_to_unfollow = get_object_or_404(User, email_or_phone=username)

    current_profile = get_object_or_404(ProfileInfo, user=request.user)
    target_profile  = get_object_or_404(ProfileInfo, user=user_to_unfollow)

    current_profile.following.remove(user_to_unfollow)
    target_profile.followers.remove(request.user)

    invalidate_user_feed(request.user.id)
    invalidate_user_feed(user_to_unfollow.id)

    return JsonResponse({
        "following":      False,
        "follower_count": target_profile.follower_count,
    })


from django.http import JsonResponse
from django.contrib.auth.decorators import login_required
from django.views.decorators.http import require_POST
from django.shortcuts import get_object_or_404

from apps.ponno.models.product import Product, Wishlist

@require_POST
@login_required(login_url='/customer/signin/')
def WishlistToggleView(request, product_id):
    product = get_object_or_404(Product, product_id=product_id, is_active=True, deleted_at__isnull=True)
    _, added = Wishlist.toggle(request.user, product)

    # Refresh from DB so wishlist_count reflects the just-made change
    product.refresh_from_db(fields=['wishlist_count'])

    return JsonResponse({
        "wishlisted":     added,
        "wishlist_count": product.wishlist_count,
    })