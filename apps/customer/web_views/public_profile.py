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
from apps.ponno.models.product import Product, Wishlist
from apps.ponno.views.home import _serialize_service as _serialize_engine_post
from megamind.models.connected_service import ConnectedService
from megamind.utils.video_info import get_video_info_cached

# ────────────────────────────────────────────────────────────────────
# HELPERS
# ────────────────────────────────────────────────────────────────────

PRODUCTS_PER_PAGE = 12
SERVICES_PER_PAGE = 12

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


# ────────────────────────────────────────────────────────────────────
# PRODUCTS — VIDEO WIRING
# ────────────────────────────────────────────────────────────────────
#
# Product only stores a single `video_url` (unlike
# ConnectedService.extracted_videos, a list of scraped entries), and
# has no external "source" URL worth falling back to — `product_url`
# is this app's own /products/<slug>/ page, not a video platform link.
# So products do NOT use resolve_post_video()'s guaranteed-non-empty
# placeholder fallback the way engines do: a themed stock-footage
# clip is fine for a social feed post, but would be misleading glued
# onto a real product listing. Only products with a genuine video_url
# get a video_info dict; the rest simply omit the key, and the front
# end skips rendering a video block for that card.

def _product_video_info(product: Product) -> dict:
    if not product.video_url:
        return None
    info = get_video_info_cached(product.video_url)
    if info and info.get("embed_url"):
        return info
    return None


# ────────────────────────────────────────────────────────────────────
# PRODUCTS — JSON SERIALIZATION (used by both the initial page load's
# JS hydration path and the infinite-scroll "load more" endpoint, so
# every product card — first batch or Nth — is built from the exact
# same shape of data by the same client-side <template> binder in
# public_profile.html.)
# ────────────────────────────────────────────────────────────────────

def _serialize_product_card(product: Product, profile_info, profile_user) -> dict:
    return {
        "uuid":                 str(product.product_id),
        "slug":                 product.slug,
        "product_name":         product.product_name,
        "short_description":    product.short_description or "",
        "image_url":            product.image.url if product.image else "",

        "brand_name":           product.brand.brand_name if product.brand_id else "No Brand",
        "is_verified":          bool(getattr(product, "is_verified", False)),

        "stock":                product.stock,
        "is_low_stock":         product.is_low_stock,
        "discount_percentage":  float(product.discount_percentage or 0),
        "free_shipping":        product.free_shipping,
        "is_featured":          product.is_featured,

        "wishlist_count":       product.wishlist_count,
        "view_count":           product.view_count,
        "rating_average":       float(product.rating_average or 0),

        "price_hidden":         bool(getattr(product, "price_hidden", False)),
        "currency":             product.currency,
        "selling_price":        float(product.selling_price) if product.selling_price is not None else None,
        "final_price":          float(product.final_price) if product.final_price is not None else None,

        "video_info": _product_video_info(product),

        "dealer_name":          profile_info.profile_name or profile_user.email_or_phone,
        "dealer_avatar_url":    profile_info.profile_photo.url if profile_info.profile_photo else "",
        "dealer_phone":         profile_info.profile_phone if profile_info.show_phone else "",
    }


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
# ENGINES (CONNECTED SERVICES) — PUBLIC CARD SERIALIZATION
# ────────────────────────────────────────────────────────────────────
#
# Full parity with the home/engine feed: reuses the EXACT SAME
# serializer as apps.ponno.views.home._serialize_service, so a card
# here carries the same guaranteed video_info, platform-normalized
# `videos`, `links`, `extracted_text`, and counts — and therefore
# renders with the SAME rich post-card UI (media mosaic, per-platform
# video embeds, links preview, text preview, details modal) as the
# home feed. There is exactly one place that decides what a
# ConnectedService "post" looks like; this view just re-scopes it to
# a single profile_user and adds viewer-relative flags.
#
# `is_following` / `is_own_post` are NOT recomputed per row here —
# every card on this page belongs to the same profile_user, so the
# relationship between the viewer and the poster is identical for
# every row. The caller computes it once (see PublicProfileView /
# load_more_services) and passes it straight through, avoiding N
# redundant follow-lookups per page the way a truly multi-poster feed
# (home.py's _personalize_feed) would need.

def _serialize_public_service(svc: ConnectedService, profile_info, *, is_following: bool, is_own_post: bool) -> dict:
    post = _serialize_engine_post(svc, profile_info)
    post["is_following"] = is_following
    post["is_own_post"]  = is_own_post
    return post


def _get_public_services(profile_user, request):
    """
    Public-facing connected services (engines) for this profile.

    Eligibility mirrors the same rules used everywhere else an engine
    is surfaced to someone other than its owner (see
    apps.ponno.views.home._get_public_feed_queryset), plus two rules
    the newer ConnectedService model introduced that this page must
    also respect since it's a direct downstream consumer of crawled
    data:

        - status == 'public'          → respects the per-engine privacy toggle
        - is_connected == True        → the owner hasn't disconnected it
        - fetch_status == 'success'   → only show engines with real scraped data
        - is_active == True           → excludes soft-deleted engines
                                         (ConnectedService.soft_delete() does
                                         not touch status/is_connected/
                                         fetch_status, so without this an
                                         engine the owner deleted would keep
                                         showing up here)
        - content_sensitivity != PII_DETECTED → never expose a row the
          PII-scan flagged after the fact, even if it was already public
          (see ContentSensitivity's help_text: this field gates what's
          "persisted/exposed downstream", and this view IS that downstream
          exposure point — both the server-rendered first batch and the
          load_more_services JSON endpoint)

    Private, disconnected, still-erroring, soft-deleted, or PII-flagged
    engines never leave the owner's own dashboard, even when the owner
    is viewing their own public profile page — status='public' alone
    isn't enough, since an engine can be marked public but still be
    broken, unfetched, deleted, or sensitive.

    Note: rows with content_sensitivity == 'unknown' (the PII scan
    hasn't run/completed yet) are NOT excluded here — only a confirmed
    PII_DETECTED result hides a row. If the scan is meant to be a gate
    (nothing public until cleared) rather than a filter, change this to
    `filter(content_sensitivity=ContentSensitivity.CLEAN)` instead.
    """
    qs = (
        ConnectedService.objects
        .filter(
            user=profile_user,
            status=ConnectedService.Status.PUBLIC,
            is_connected=True,
            fetch_status=ConnectedService.FetchStatus.SUCCESS,
            is_active=True,
        )
        .exclude(
            content_sensitivity=ConnectedService.ContentSensitivity.PII_DETECTED,
        )
        .order_by("-created_at")
    )

    service_type = request.GET.get("service_type", "").strip()
    if service_type in ConnectedService.ServiceType.values:
        qs = qs.filter(service_type=service_type)

    total_count = qs.count()

    paginator = Paginator(qs, SERVICES_PER_PAGE)
    page_num  = request.GET.get("services_page", 1)

    try:
        page_obj = paginator.page(page_num)
    except PageNotAnInteger:
        page_obj = paginator.page(1)
    except EmptyPage:
        page_obj = paginator.page(paginator.num_pages)

    return page_obj, total_count, service_type


# ────────────────────────────────────────────────────────────────────
# PUBLIC PROFILE VIEW
# ────────────────────────────────────────────────────────────────────

@login_required(login_url='/customer/signin/')
def PublicProfileView(request, username):
    # .active_users() (is_active=True, deleted_at__isnull=True) so a
    # soft-deleted account's profile isn't still fully browsable at its
    # old URL just because email/phone are intentionally kept on the
    # row for referential integrity -- see User.soft_delete()'s
    # docstring in account.py. Same reasoning applies to every other
    # User-by-username lookup in this file below.
    profile_user = get_object_or_404(
        User.objects.active_users().select_related("profileinfo"),
        email_or_phone=username,
    )
    profile_info         = get_object_or_404(ProfileInfo, user=profile_user)
    current_user_profile = get_object_or_404(ProfileInfo, user=request.user)

    if request.user != profile_user:
        _log_profile_view(request, profile_user)
        profile_info.increment_view_count()

    is_following = current_user_profile.is_following(profile_user)
    is_own_profile = request.user == profile_user

    recent_viewers = (
        ProfileViewLog.objects
        .filter(profile_user=profile_user, viewer__isnull=False)
        .select_related("viewer", "viewer__profileinfo")
        .order_by("-viewed_at")[:20]
    )

    # ── Products (dealers only) — first batch only; JS takes over
    #    from here via load_more_products for every batch after. ──
    products_first_batch = []
    total_products    = 0
    products_has_next  = False
    products_next_page = None
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

        products_first_batch = [
            _serialize_product_card(p, profile_info, profile_user)
            for p in products_page.object_list
        ]
        products_has_next  = products_page.has_next()
        products_next_page = products_page.next_page_number() if products_has_next else None

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

    # ── Engines (public connected services, any role) — first batch,
    #    same infinite-scroll handoff pattern as products, and the
    #    SAME rich post-card shape/serializer as the home feed. ────
    services_page, total_services, active_service_type = _get_public_services(
        profile_user, request
    )
    services_first_batch = [
        _serialize_public_service(s, profile_info, is_following=is_following, is_own_post=is_own_profile)
        for s in services_page.object_list
    ]
    services_has_next    = services_page.has_next()
    services_next_page   = services_page.next_page_number() if services_has_next else None

    context = {
        "profile_user":         profile_user,
        "profile_info":         profile_info,
        "current_user":         request.user,
        "current_user_profile": current_user_profile,
        "is_following":         is_following,
        "follower_count":       profile_info.follower_count,
        "following_count":      profile_info.following_count,
        "recent_viewers":       recent_viewers,

        "total_products":       total_products,
        "search_query":         search_query,
        "active_sort":          active_sort,
        "active_category":      active_category,
        "active_brand":         active_brand,
        "active_stock":         active_stock,
        "dealer_categories":    dealer_categories,
        "dealer_brands":        dealer_brands,
        "sort_options":         list(_SORT_MAP.keys()),

        "total_services":       total_services,
        "active_service_type":  active_service_type,
        "service_type_choices": ConnectedService.ServiceType.choices,

        # JSON payloads consumed by the inline <script> in
        # public_profile.html to hydrate the first batch of cards via
        # the same renderer infinite scroll uses for every batch
        # after — so there is exactly one code path that turns
        # "a list of card dicts" into DOM, used on load AND on scroll.
        "products_initial_json": {
            "items": products_first_batch,
            "has_next": products_has_next,
            "next_page": products_next_page,
        },
        "services_initial_json": {
            "items": services_first_batch,
            "has_next": services_has_next,
            "next_page": services_next_page,
        },
    }

    return render(request, "personal/public_profile.html", context)


# ────────────────────────────────────────────────────────────────────
# INFINITE SCROLL — JSON "LOAD MORE" ENDPOINTS
# ────────────────────────────────────────────────────────────────────
#
# Return plain JSON (not rendered HTML) so public_profile.html stays
# the ONLY template involved — the front end's renderer builds card
# markup from these same dicts client-side, identical to how it
# hydrates the server-rendered first batch above.

@login_required(login_url='/customer/signin/')
def load_more_products(request, username):
    # Scoping the role check into the lookup itself (rather than
    # fetching the user first and branching after) means a direct
    # hit on a non-dealer's load-more URL — which the UI never
    # generates, since the Products tab + its sentinel only render
    # when profile_user.role == 'dealer' — gets a standard 404 like
    # any other "this resource doesn't exist for this profile" case,
    # instead of a bespoke JSON 400 error path nothing consumes.
    profile_user = get_object_or_404(User.objects.active_users(), email_or_phone=username, role="dealer")

    profile_info = get_object_or_404(ProfileInfo, user=profile_user)
    page_obj, total_products, *_ = _get_dealer_products(profile_user, request)

    items = [
        _serialize_product_card(p, profile_info, profile_user)
        for p in page_obj.object_list
    ]

    return JsonResponse({
        "items":      items,
        "has_next":   page_obj.has_next(),
        "next_page":  page_obj.next_page_number() if page_obj.has_next() else None,
        "page":       page_obj.number,
        "num_pages":  page_obj.paginator.num_pages,
        "total":      total_products,
    })

@login_required(login_url='/customer/signin/')
def load_more_services(request, username):
    profile_user = get_object_or_404(User.objects.active_users(), email_or_phone=username)
    profile_info = get_object_or_404(ProfileInfo, user=profile_user)

    current_user_profile = get_object_or_404(ProfileInfo, user=request.user)
    is_following = current_user_profile.is_following(profile_user)
    is_own_post  = request.user == profile_user

    page_obj, total_services, _ = _get_public_services(profile_user, request)
    items = [
        _serialize_public_service(s, profile_info, is_following=is_following, is_own_post=is_own_post)
        for s in page_obj.object_list
    ]

    return JsonResponse({
        "items":      items,
        "has_next":   page_obj.has_next(),
        "next_page":  page_obj.next_page_number() if page_obj.has_next() else None,
        "page":       page_obj.number,
        "num_pages":  page_obj.paginator.num_pages,
        "total":      total_services,
    })


# ────────────────────────────────────────────────────────────────────
# FOLLOW / UNFOLLOW
# ────────────────────────────────────────────────────────────────────
from apps.ponno.views.discovery_engine_views import invalidate_user_feed  # already exists
from apps.notify.services import notice
from apps.notify.models.ring_bell import Notification
from django.urls import reverse


@require_POST
@login_required(login_url='/customer/signin/')
def follow(request, username):
    user_to_follow = get_object_or_404(User.objects.active_users(), email_or_phone=username)

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
    user_to_unfollow = get_object_or_404(User.objects.active_users(), email_or_phone=username)

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


@require_POST
@login_required(login_url='/customer/signin/')
def WishlistToggleView(request, product_id):
    product = get_object_or_404(Product, product_id=product_id, is_active=True, deleted_at__isnull=True)
    _, added = Wishlist.toggle(request.user, product)

    product.refresh_from_db(fields=['wishlist_count'])

    return JsonResponse({
        "wishlisted":     added,
        "wishlist_count": product.wishlist_count,
    })