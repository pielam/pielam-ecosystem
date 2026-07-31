# apps/customer/views/profile.py

"""
Profile view — engineered for high request volume with low DB pressure.

Architecture
────────────
  Layer 1 · Django-side response cache (per-user, short TTL)
  Layer 2 · Upstream HTTP cache headers (CDN / Varnish / Nginx)
  Layer 3 · Redis batch get_many (single round-trip on cache miss)
  Layer 4 · Parallel DB loading via ThreadPoolExecutor
  Layer 5 · Pickle-safe plain dicts only — no ORM objects cached
  Layer 6 · Conditional rendering (304 Not Modified via ETag)
  Layer 7 · Graceful degradation:
              - Full cache hit  → served entirely from Redis, 0 DB queries
              - Partial cache hit → only missing sections re-queried
              - Non-critical loader failure (activity/recent/engine) →
                page still renders with a safe empty fallback for that
                section instead of a 500; the failure is logged and the
                section is NOT cached, so the next request retries it
              - Critical loader failure (core profile/profile_info) →
                propagates normally (e.g. 404 if the profile doesn't
                exist), since the page cannot render without it

Cache topology
──────────────
  profile:ctx:{uid}          5 min   Core profile + product stats
  profile:activity:{uid}     3 min   Orders, wishlist, ratings
  profile:recent:{uid}       2 min   Recently viewed products
  profile:engine:{uid}       4 min   Connected services + content
  profile:etag:{uid}         2 min   Cached ETag fingerprint

  Cache TTLs are jittered by ±10% on write to avoid synchronized
  expiry / thundering-herd recompute across many users whose caches
  were populated at the same moment (e.g. after a deploy or flush).

Query budget
────────────
  Cold path  →  up to 4 loaders run in parallel (ThreadPoolExecutor)
                 _load_profile_context  → 6 queries
                 _load_activity_context → 5 queries
                 _load_recently_viewed  → 1 query
                 _load_engine_context   → 1 query
                 ─────────────────────────────────────────────────
                 Wall-clock time ≈ slowest single loader (not sum)
  Warm path  →  0 DB queries (single Redis get_many)

Security note
──────────────
  `following_profiles` deliberately selects only public-facing fields.
  Contact info (email/phone) must never be added to this projection —
  it is cached in Redis and rendered to anyone who can view this
  profile's "following" list, which is not a trusted audience for PII.
"""

import hashlib
import json
import logging
import random
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any
from urllib.parse import urlparse

from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.core.cache import cache
from django.db.models import (
    Avg, Count, DecimalField, ExpressionWrapper, F, Q, Sum,
)
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, render
from django.views.decorators.cache import cache_control
from django.views.decorators.vary import vary_on_cookie

from apps.customer.models.profile_info import ProfileInfo
from apps.ponno.models.brand import Brand
from apps.ponno.models.product import (
    Order, Product, ProductView, SearchHistory, Wishlist,
)
from apps.ponno.models.product_view_log import ProductViewLog
from apps.ponno.models.rating import ProductRating
from megamind.models.connected_service import ConnectedService

logger = logging.getLogger(__name__)

from megamind.utils.video_info import get_video_info_cached
from megamind.utils.media_info import normalize_images, normalize_links

# ═══════════════════════════════════════════════════════════════════
# TTLs  (seconds)
# ═══════════════════════════════════════════════════════════════════

PROFILE_TTL  = 60 * 5   # 5 min
ACTIVITY_TTL = 60 * 3   # 3 min
RECENT_TTL   = 60 * 2   # 2 min
ENGINE_TTL   = 60 * 4   # 4 min

# ETag cache: how long we store the hash for 304 short-circuit
ETAG_TTL     = 60 * 2   # 2 min (≤ shortest data TTL)

# Thread pool shared across requests — avoids per-request overhead.
# Tune via Django settings.PROFILE_VIEW_THREAD_POOL_WORKERS rather than
# editing this file; the right number depends on the WSGI/ASGI worker
# count and DB connection pool size of the actual deployment.
_MAX_WORKERS = getattr(settings, 'PROFILE_VIEW_THREAD_POOL_WORKERS', 4)
_EXECUTOR = ThreadPoolExecutor(max_workers=_MAX_WORKERS, thread_name_prefix="profile_loader")


# ═══════════════════════════════════════════════════════════════════
# CACHE KEY HELPERS
# ═══════════════════════════════════════════════════════════════════

def _key_ctx(uid: int)      -> str: return f"profile:ctx:{uid}"
def _key_activity(uid: int) -> str: return f"profile:activity:{uid}"
def _key_recent(uid: int)   -> str: return f"profile:recent:{uid}"
def _key_engine(uid: int)   -> str: return f"profile:engine:{uid}"
def _key_etag(uid: int)     -> str: return f"profile:etag:{uid}"


def _jittered_ttl(ttl: int, spread: float = 0.10) -> int:
    """
    Add up to ±`spread` randomness to a TTL so keys written at the same
    moment (deploy, cache flush, traffic spike) don't all expire at the
    same instant and cause a synchronized stampede of cache misses.
    """
    delta = int(ttl * spread)
    return ttl + random.randint(-delta, delta) if delta else ttl


# ═══════════════════════════════════════════════════════════════════
# CACHE INVALIDATION  (call from signals / post_save hooks)
# ═══════════════════════════════════════════════════════════════════

def invalidate_profile_cache(user_id: int) -> None:
    cache.delete_many([
        _key_ctx(user_id),
        _key_activity(user_id),
        _key_recent(user_id),
        _key_engine(user_id),
        _key_etag(user_id),
    ])

def invalidate_recent_views(user_id: int) -> None:
    cache.delete_many([_key_recent(user_id), _key_etag(user_id)])

def invalidate_activity(user_id: int) -> None:
    cache.delete_many([_key_activity(user_id), _key_etag(user_id)])

def invalidate_engine(user_id: int) -> None:
    cache.delete_many([_key_engine(user_id), _key_etag(user_id)])


# ═══════════════════════════════════════════════════════════════════
# SAFE FALLBACKS — used when a non-critical loader fails mid-request.
# Never written to cache; only used to let the page render this once.
# ═══════════════════════════════════════════════════════════════════

_EMPTY_ACTIVITY: dict[str, Any] = {
    "total_orders":        0,
    "total_spent":         0.0,
    "pending_orders":      0,
    "delivered_orders":    0,
    "cancelled_orders":    0,
    "wishlist_count":      0,
    "ratings_given_count": 0,
    "avg_rating_given":    0.0,
    "recent_searches":     [],
    "total_views_made":    0,
    "desktop_views":       0,
    "mobile_views":        0,
}

_EMPTY_RECENT: list[dict[str, Any]] = []

_EMPTY_ENGINE: dict[str, Any] = {
    'connected_services': [],
    'gallery_videos':     [],
    'gallery_media':      [],
    'extracted_links':    [],
    'extracted_texts':    [],
    'videos_count':       0,
    'images_count':       0,
    'links_count':        0,
    'text_count':         0,
}

_FALLBACKS: dict[str, Any] = {
    'activity': _EMPTY_ACTIVITY,
    'recent':   _EMPTY_RECENT,
    'engine':   _EMPTY_ENGINE,
}


# ═══════════════════════════════════════════════════════════════════
# DATA LOADERS — return plain dicts / lists of dicts (pickle-safe)
# ═══════════════════════════════════════════════════════════════════

def _load_profile_context(user) -> dict[str, Any]:
    """
    ~6 DB queries. Returns plain dict — safe to pickle & cache.

    CRITICAL loader: this is the only data the page cannot render
    without (it's also where get_object_or_404 can raise Http404 for
    a missing profile), so its exceptions are allowed to propagate.
    """

    profile_info = get_object_or_404(
        ProfileInfo.objects.select_related('user'),
        user=user,
    )

    product_stats = (
        Product.objects
        .filter(dealer=user)
        .aggregate(
            products_listed_count=Count('id'),
            active_products_count=Count('id', filter=Q(is_active=True)),
            out_of_stock_count=Count(
                'id', filter=Q(is_active=True, stock_status='out_of_stock'),
            ),
            low_stock_count=Count(
                'id', filter=Q(is_active=True, stock_status='low_stock'),
            ),
            featured_products_count=Count(
                'id', filter=Q(is_active=True, is_featured=True),
            ),
            total_product_views=Sum('view_count'),
            total_revenue=Sum(
                ExpressionWrapper(F('revenue_generated'), output_field=DecimalField()),
                filter=Q(is_active=True),
            ),
            avg_product_rating=Avg('rating_average', filter=Q(is_active=True)),
        )
    )

    brands_count = (
        Brand.objects
        .filter(created_by=user, deleted_at__isnull=True)
        .count()
    )

    categories_count = (
        Product.objects
        .filter(dealer=user, category__isnull=False, deleted_at__isnull=True)
        .values('category')
        .distinct()
        .count()
    )

    followers_count  = profile_info.followers.count()
    followings_count = profile_info.following.count()

    # NOTE: This view only ever loads request.user's OWN profile and
    # is only ever rendered back to that same logged-in user (uid is
    # request.user.pk; the Redis cache key and the response are both
    # scoped to that one user). It is NOT used to render anyone else's
    # profile to a third party. Because of that, it's safe to include
    # user__email_or_phone here — it's needed to build working
    # "view profile" / "follow" links — since the only person who
    # ever sees this cached payload is the user who owns the Following
    # list. If this loader is ever reused for a *public-facing*
    # profile view (i.e. rendered to visitors other than the owner),
    # remove email_or_phone from this projection immediately, since it
    # would then leak every followed user's contact identifier to
    # whoever is viewing that page.
    following_profiles = list(
        ProfileInfo.objects
        .filter(
            user__in=profile_info.following.all(),
            is_profile_archived=False,
        )
        .values(
            'user_id',
            'user__email_or_phone',
            'profile_name',
            'profile_name_slug',
            'profile_photo',
            'is_profile_verified',
            'profile_type',
        )
    )

    # Resolve photo URLs in Python, not in the template
    default_photo = '/static/defaults/default-profile-picture.png'
    for fp in following_profiles:
        raw = fp.get('profile_photo') or ''
        fp['profile_photo_url'] = (
            f"/media/{raw}" if raw else default_photo
        )

    ratings_received = (
        ProductRating.objects
        .filter(product__dealer=user)
        .aggregate(total_ratings=Count('id'), avg_rating=Avg('rating'))
    )

    return {
        # Serialize profile_info to a plain dict — never cache ORM instances
        "profile_info": {
            'profile_name':               profile_info.profile_name,
            'profile_name_slug':          profile_info.profile_name_slug,
            'profile_photo':              profile_info.get_profile_photo_url(),
            'profile_cover_photo':        profile_info.get_profile_cover_photo_url(),
            'profile_tagline':            profile_info.profile_tagline,
            'profile_type':               profile_info.profile_type,
            'get_profile_type_display':   profile_info.get_profile_type_display(),
            'profile_gender':             profile_info.profile_gender,
            'get_profile_gender_display': (
                profile_info.get_profile_gender_display()
                if profile_info.profile_gender else ''
            ),
            'profile_dob':                profile_info.profile_dob,
            'show_dob':                   profile_info.show_dob,
            'show_location':              profile_info.show_location,
            'location_display':           profile_info.location_display,
            'profile_address':            profile_info.profile_address,
            'profile_postal_code':        profile_info.profile_postal_code,
            'profile_language':           profile_info.profile_language,
            'is_profile_verified':        profile_info.is_profile_verified,
            'verification_level':         profile_info.verification_level,
            'verified_at':                profile_info.verified_at,
            'profile_creation_time':      profile_info.profile_creation_time,
            'profile_updated_time':       profile_info.profile_updated_time,

            # Business info (only meaningful when profile_type is
            # business/professional, but always included — the
            # template itself gates display via profile_type)
            'business_name':              profile_info.business_name,
            'business_type':              profile_info.business_type,
            'business_registration':      profile_info.business_registration,
            'business_tax_id':            profile_info.business_tax_id,
            'business_website':           profile_info.business_website,
            'business_email':             profile_info.business_email,
            'business_phone':             profile_info.business_phone,
            'business_description':       profile_info.business_description,

            # Social links
            'social_facebook':            profile_info.social_facebook,
            'social_twitter':             profile_info.social_twitter,
            'social_instagram':           profile_info.social_instagram,
            'social_linkedin':            profile_info.social_linkedin,
            'social_youtube':             profile_info.social_youtube,
            'social_tiktok':              profile_info.social_tiktok,
            'social_whatsapp':            profile_info.social_whatsapp,
        },
        "profile_completion":      profile_info.completion_percentage,
        "engagement_score":        round(profile_info.get_engagement_score(), 1),
        "followers_count":         followers_count,
        "followings_count":        followings_count,
        "following_profiles":      following_profiles,
        "products_listed_count":   product_stats["products_listed_count"] or 0,
        "active_products_count":   product_stats["active_products_count"] or 0,
        "out_of_stock_count":      product_stats["out_of_stock_count"] or 0,
        "low_stock_count":         product_stats["low_stock_count"] or 0,
        "featured_products_count": product_stats["featured_products_count"] or 0,
        "total_product_views":     product_stats["total_product_views"] or 0,
        "total_revenue":           float(product_stats["total_revenue"] or 0),
        "avg_product_rating":      round(product_stats["avg_product_rating"] or 0, 2),
        "brands_count":            brands_count,
        "categories_count":        categories_count,
        "ratings_received_count":  ratings_received["total_ratings"] or 0,
        "avg_rating_received":     round(ratings_received["avg_rating"] or 0, 2),
    }


def _load_activity_context(user) -> dict[str, Any]:
    """~5 DB queries. Returns plain dict — safe to pickle & cache. Non-critical."""

    order_stats = (
        Order.objects
        .filter(user=user)
        .aggregate(
            total_orders=Count('id'),
            total_spent=Sum('grand_total'),
            pending_orders=Count('id', filter=Q(status='pending')),
            delivered_orders=Count('id', filter=Q(status='delivered')),
            cancelled_orders=Count('id', filter=Q(status='cancelled')),
        )
    )

    wishlist_count = Wishlist.objects.filter(user=user).count()

    ratings_given = (
        ProductRating.objects
        .filter(user=user)
        .aggregate(total_given=Count('id'), avg_given=Avg('rating'))
    )

    recent_searches = list(
        SearchHistory.objects
        .filter(user=user)
        .order_by('-searched_at')
        .values('query', 'result_count', 'searched_at', 'search_count')
        [:5]
    )

    view_log_stats = (
        ProductViewLog.objects
        .filter(viewer=user, deleted_at__isnull=True)
        .aggregate(
            total_views_made=Count('id'),
            desktop_views=Count('id', filter=Q(device_type='desktop')),
            mobile_views=Count('id', filter=Q(device_type='mobile')),
        )
    )

    return {
        "total_orders":        order_stats["total_orders"] or 0,
        "total_spent":         float(order_stats["total_spent"] or 0),
        "pending_orders":      order_stats["pending_orders"] or 0,
        "delivered_orders":    order_stats["delivered_orders"] or 0,
        "cancelled_orders":    order_stats["cancelled_orders"] or 0,
        "wishlist_count":      wishlist_count,
        "ratings_given_count": ratings_given["total_given"] or 0,
        "avg_rating_given":    round(ratings_given["avg_given"] or 0, 2),
        "recent_searches":     recent_searches,
        "total_views_made":    view_log_stats["total_views_made"] or 0,
        "desktop_views":       view_log_stats["desktop_views"] or 0,
        "mobile_views":        view_log_stats["mobile_views"] or 0,
    }


def _load_recently_viewed(user) -> list[dict[str, Any]]:
    """1 DB query. Returns list of plain dicts — safe to pickle & cache. Non-critical."""

    rows = (
        ProductView.objects
        .filter(
            user=user,
            product__is_active=True,
            product__deleted_at__isnull=True,
        )
        .select_related(
            'product',
            'product__brand',
            'product__category',
            'product__sub_category',
        )
        .only(
            'viewed_at',
            'view_count',
            'product__product_title',
            'product__product_name',
            'product__slug',
            'product__image',
            'product__selling_price',
            'product__final_price',
            'product__brand_price',
            'product__discount_percentage',  # used to derive is_on_sale
            'product__rating_average',
            'product__review_count',
            'product__stock_status',
            'product__wishlist_count',
            'product__currency',
            # 'product__is_on_sale' intentionally excluded — it's a
            # @property, not a DB field; .only() would error on it.
            'product__brand__brand_name',
            'product__category__category_name',
            'product__sub_category__sub_category_name',
        )
        .order_by('-viewed_at')[:20]
    )

    result = []
    for pv in rows:
        p = pv.product
        result.append({
            'viewed_at':  pv.viewed_at,
            'view_count': pv.view_count,
            'product': {
                'product_title':       p.product_title,
                'product_name':        p.product_name,
                'slug':                p.slug,
                'image_url':           p.image_url,
                'selling_price':       float(p.selling_price or 0),
                'final_price':         float(p.final_price or 0),
                'brand_price':         float(p.brand_price or 0),
                'discount_percentage': float(p.discount_percentage or 0),
                'rating_average':      float(p.rating_average or 0),
                'review_count':        p.review_count,
                'stock_status':        p.stock_status,
                'wishlist_count':      p.wishlist_count,
                'currency':            p.currency,
                'is_on_sale':          p.discount_percentage > 0,  # derived, not fetched
                'brand_name':          p.brand.brand_name if p.brand else '',
                'category_name':       p.category.category_name if p.category else '',
                'sub_category_name':   p.sub_category.sub_category_name if p.sub_category else '',
            }
        })
    return result

from urllib.parse import urlparse
from typing import Any

from megamind.models.connected_service import ConnectedService
from megamind.utils.media_info import normalize_images, normalize_links


def _find_href_for_alt(alt: str, alt_to_href: dict, fallback: str) -> str:
    """Fallback only — used when an image has no direct href of its own."""
    if not alt:
        return fallback
    alt_lower = alt.strip().lower()
    if alt_lower in alt_to_href:
        return alt_to_href[alt_lower]
    for text, href in alt_to_href.items():
        if text.startswith(alt_lower):
            return href
    return fallback




def _load_engine_context(user) -> dict[str, Any]:
    """1 DB query (+ in-Python normalization). Returns plain dict — safe to pickle & cache."""

    services = list(
        ConnectedService.objects
        .filter(user=user, is_connected=True)
        .only(
            'service_name', 'service_url', 'service_type',
            'og_title', 'og_description', 'og_thumbnail', 'og_site_name',
            'extracted_images', 'extracted_videos',
            'extracted_links', 'extracted_text',
            'fetch_status', 'last_fetch_time',
        )
    )

    gallery_videos = []   # ← powers the new "Videos" tab (was Engine)
    gallery_media  = []   # ← powers the new "Media" tab (was Following)
    extracted_links = []
    extracted_texts = []

    for svc in services:
        domain = urlparse(svc.service_url).netloc
        title  = svc.og_title or svc.service_name
        site   = svc.og_site_name or domain

        meta = {
            'source_url':      svc.service_url,
            'source_domain':   site,
            'service_name':    svc.service_name,
            'service_type':    svc.service_type,
            'fetch_status':    svc.fetch_status,
            'last_fetch_time': svc.last_fetch_time,
            'og_description':  svc.og_description,
            'og_thumbnail':    svc.og_thumbnail,
        }

        # ── Media (images) — via media_info.py ──────────────────
        for img in normalize_images(svc.extracted_images):
            img_link = img['href'] or img['url']   # ← per-image fallback, not svc.service_url
            gallery_media.append({
                **meta,
                'file_url':      img['url'],
                'alt':           img['alt'],
                'title':         title,
                'source_url':    img_link,
                'source_domain': urlparse(img_link).netloc or site,
            })
        # ── Videos — via video_info.py (playable, normalized) ───
        for vid in (svc.extracted_videos or []):
            raw_url = vid.get('url') if isinstance(vid, dict) else vid
            if not raw_url:
                continue
            info = get_video_info_cached(raw_url)
            if not info or not info.get('embed_url'):
                continue
            gallery_videos.append({
                **meta,
                **info,          # platform, embed_url, watch_url, thumbnail, type, mime_type?
                'title': title,
            })

        for lnk in normalize_links(svc.extracted_links):
            extracted_links.append({
                **meta,
                'url':    lnk['href'],
                'title':  lnk['text'] or title,
                'domain': urlparse(lnk['href']).netloc or domain,
            })

        if svc.extracted_text and svc.extracted_text.strip():
            extracted_texts.append({**meta, 'title': title, 'content': svc.extracted_text})

    return {
        'connected_services': [
            {
                'service_name': s.service_name,
                'service_url':  s.service_url,
                'service_type': s.service_type,
                'og_thumbnail': s.og_thumbnail,
                'og_site_name': s.og_site_name,
                'fetch_status': s.fetch_status,
            }
            for s in services
        ],
        'gallery_videos':   gallery_videos,
        'gallery_media':    gallery_media,
        'extracted_links':  extracted_links,
        'extracted_texts':  extracted_texts,
        'videos_count':     len(gallery_videos),
        'images_count':     len(gallery_media),
        'links_count':      len(extracted_links),
        'text_count':       len(extracted_texts),
    }

# ═══════════════════════════════════════════════════════════════════
# ETag HELPERS
# ═══════════════════════════════════════════════════════════════════

def _build_etag(
    uid: int,
    ctx: dict[str, Any],
    activity: dict[str, Any],
    recently_viewed: list[dict[str, Any]],
    engine: dict[str, Any],
) -> str:
    """
    Deterministic ETag from cache content.
    JSON-serialise only lightweight fingerprint fields, not full payloads.
    """
    # Defensive: a section can legitimately be None if it was cached as
    # None, or if something upstream skipped assigning a fallback.
    # Never let that crash ETag generation — fall back to empty.
    ctx             = ctx or {}
    activity        = activity or {}
    recently_viewed = recently_viewed or []
    engine          = engine or {}

    fingerprint = {
        'uid':      uid,
        'pc':       ctx.get('profile_completion'),
        'fc':       ctx.get('followers_count'),
        'to':       activity.get('total_orders'),
        'rv_count': len(recently_viewed),
        'svc':      engine.get('images_count'),
    }
    raw = json.dumps(fingerprint, sort_keys=True, default=str)
    return hashlib.md5(raw.encode(), usedforsecurity=False).hexdigest()

# ═══════════════════════════════════════════════════════════════════
# VIEW
# ═══════════════════════════════════════════════════════════════════

@login_required(login_url='/customer/signin/')
@vary_on_cookie                          # CDN must vary by session cookie
@cache_control(private=True, max_age=0, must_revalidate=True)
def ProfileView(request) -> HttpResponse:
    uid = request.user.pk

    ck_ctx      = _key_ctx(uid)
    ck_activity = _key_activity(uid)
    ck_recent   = _key_recent(uid)
    ck_engine   = _key_engine(uid)
    ck_etag     = _key_etag(uid)

    # ── Layer 1: single Redis round-trip ─────────────────────────
    cached = cache.get_many([ck_ctx, ck_activity, ck_recent, ck_engine, ck_etag])

    ctx             = cached.get(ck_ctx)
    activity        = cached.get(ck_activity)
    recently_viewed = cached.get(ck_recent)
    engine          = cached.get(ck_engine)
    cached_etag     = cached.get(ck_etag)

    all_warm = all(v is not None for v in [ctx, activity, recently_viewed, engine])

    # ── Layer 2: 304 short-circuit (full cache hit only) ─────────
    if all_warm and cached_etag:
        client_etag = request.META.get('HTTP_IF_NONE_MATCH', '')
        if client_etag and client_etag.strip('"') == cached_etag:
            return HttpResponse(status=304)

    # ── Layer 3: parallel DB load for cache misses ────────────────
    loaders_needed: dict[str, tuple] = {}

    if ctx is None:
        loaders_needed['ctx'] = (_load_profile_context, request.user)
    if activity is None:
        loaders_needed['activity'] = (_load_activity_context, request.user)
    if recently_viewed is None:
        loaders_needed['recent'] = (_load_recently_viewed, request.user)
    if engine is None:
        loaders_needed['engine'] = (_load_engine_context, request.user)

    if loaders_needed:
        futures = {
            _EXECUTOR.submit(fn, arg): key
            for key, (fn, arg) in loaders_needed.items()
        }

        to_cache: dict[str, tuple] = {}
        for future in as_completed(futures):
            key = futures[future]

            try:
                result = future.result()
            except Exception:
                if key == 'ctx':
                    # Critical loader — no safe fallback exists for the
                    # core profile data. Let it propagate (e.g. Http404
                    # for a missing profile, or a 500 for anything else).
                    logger.exception(
                        "profile loader 'ctx' failed (critical) uid=%s", uid,
                    )
                    raise

                # Non-critical loader — degrade gracefully. Log it, use
                # an empty fallback so the page still renders, and skip
                # caching so the next request retries this section.
                logger.exception(
                    "profile loader '%s' failed (non-critical) uid=%s — "
                    "serving empty fallback for this section", key, uid,
                )
                result = _FALLBACKS[key]

                if key == 'activity':
                    activity = result
                elif key == 'recent':
                    recently_viewed = result
                elif key == 'engine':
                    engine = result
                continue

            if key == 'ctx':
                ctx = result
                to_cache[ck_ctx] = (result, PROFILE_TTL)
            elif key == 'activity':
                activity = result
                to_cache[ck_activity] = (result, ACTIVITY_TTL)
            elif key == 'recent':
                recently_viewed = result
                to_cache[ck_recent] = (result, RECENT_TTL)
            elif key == 'engine':
                engine = result
                to_cache[ck_engine] = (result, ENGINE_TTL)

        # Write all successfully-loaded misses back to Redis in one pass.
        # Sections that failed (and used a fallback) are deliberately
        # excluded so they're retried on the next request rather than
        # caching an empty placeholder.
        for cache_key, (value, ttl) in to_cache.items():
            cache.set(cache_key, value, _jittered_ttl(ttl))

    # ── Layer 4: compute + store ETag ────────────────────────────
    # Only cache a freshly computed ETag if every section came from a
    # real load/cache hit (not a fallback) — otherwise we'd lock in an
    # ETag that doesn't reflect the degraded response.
    degraded = (
        activity is _EMPTY_ACTIVITY
        or recently_viewed is _EMPTY_RECENT
        or engine is _EMPTY_ENGINE
    )

# ── Layer 4: compute + store ETag ────────────────────────────
    
    etag = cached_etag or _build_etag(uid, ctx, activity, recently_viewed, engine)
    if not cached_etag and not degraded:
        cache.set(ck_etag, etag, _jittered_ttl(ETAG_TTL))


    # TEMPORARY DEBUG — remove after diagnosing
    logger.warning(
        "PROFILE DEBUG uid=%s engine_is_none=%s engine_keys=%s images_count=%s degraded=%s",
        uid, engine is None,
        list(engine.keys()) if isinstance(engine, dict) else None,
        engine.get('images_count') if isinstance(engine, dict) else None,
        degraded,
    )
    # Defensive guard: never let a None section reach the template/
    # unpack. ctx should be impossible to be None here (critical
    # loader raises instead of returning None) — if it IS None,
    # something upstream is silently swallowing an exception, so we
    # still guard it rather than crash the whole page.
    ctx             = ctx or {}
    activity        = activity or _EMPTY_ACTIVITY
    recently_viewed = recently_viewed or _EMPTY_RECENT
    engine          = engine or _EMPTY_ENGINE

    # ── Layer 5: render (zero DB queries below this line) ─────────
    context = {
        'user':            request.user,
        'recently_viewed': recently_viewed,
        **ctx,
        **activity,
        **engine,
    }
    response = render(request, 'customer/profile.html', context)
    response['ETag'] = f'"{etag}"'
    return response