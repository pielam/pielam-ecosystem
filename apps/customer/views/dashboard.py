"""
ProfessionalDashboardView
==========================
A dashboard endpoint engineered for extreme read-scale (the goal of
"millions of requests/second" is only realistic if the overwhelming
majority of hits NEVER reach Django or Postgres at all).

Architecture
------------

    Client
      |
      v
    CDN / Edge cache (Cloudflare, Fastly, Varnish)   <-- absorbs ~99.9% of hits
      |  (cache miss / revalidation only)
      v
    Django  (this view)
      |  (cache miss only, guarded by a distributed lock)
      v
    Redis   (shared app cache, cache-aside + stale-while-revalidate)
      |  (cache miss only; also refreshed proactively by Celery beat)
      v
    Postgres (read replica)

Design decisions baked into this file
--------------------------------------
1. The response is split into two parts:
     - a large, fully public/anonymous "shell" payload (trending
       products, featured brands, featured categories) that is
       IDENTICAL for every visitor -> safe to cache at the CDN edge
       for tens of seconds with `Cache-Control: public`.
     - a tiny per-user "fragment" (wishlist count, order count,
       unread-ish counters) that is cheap, indexed-only, and cached
       per-user with a short TTL. This is what actually varies, so
       we keep it small on purpose.

2. Redis cache-aside with jittered TTL + stale-while-revalidate:
   Django/Postgres only regenerate the shell payload occasionally,
   never per-request. Jitter avoids every key expiring in lockstep
   ("cache avalanche").

3. Cache-stampede protection: on a miss, only ONE process/thread
   recomputes (via an atomic Redis SETNX-style lock through
   `cache.add`); every other concurrent request gets the last-known
   good value (or a fast, bounded wait) instead of all of them
   hammering Postgres simultaneously.

4. A Celery beat task proactively refreshes the cache before it
   expires, so in steady state the "miss" path is rarely exercised
   at all in production.

5. Conditional GET support (`ETag` / `If-None-Match`) so CDNs and
   browsers can revalidate with a cheap 304 instead of re-downloading
   the payload.

6. Query cost on the rare miss path is minimized with
   `select_related` / `only()` and by leaning on the cached counter
   fields already denormalized onto the models (`view_count`,
   `product_count`, `popularity_score`, etc.) instead of aggregating
   live.

7. `orjson` for fast (de)serialization; plain `View` (not a heavier
   DRF `APIView`) to keep per-request Python overhead minimal; async
   `get()` so slow-path DB/cache calls don't block the event loop /
   worker.

Required settings
------------------
    CACHES = {
        "default": {
            "BACKEND": "django_redis.cache.RedisCache",
            "LOCATION": "redis://redis-cache:6379/1",
            "OPTIONS": {"CLIENT_CLASS": "django_redis.client.DefaultClient"},
        }
    }

Required packages: django-redis, celery
Recommended (optional, faster JSON): orjson
"""

from __future__ import annotations

import hashlib
import logging
import random
import time
from typing import Optional

from django.core.cache import cache
from django.http import HttpRequest, HttpResponse
from django.shortcuts import render
from django.views import View

from apps.ponno.models.brand import Brand
from apps.ponno.models.category import Category
from apps.ponno.models.product import Product, Wishlist, Order
from apps.customer.models.profile_info import ProfileInfo
from apps.customer.models.profile_view_log import ProfileViewLog  # adjust path if this model lives elsewhere
from megamind.models.connected_service import ConnectedService  # adjust path if this model lives elsewhere

logger = logging.getLogger("dashboard")

# orjson is a fast, optional accelerator — if it isn't installed (e.g. a
# fresh venv, a server where requirements.txt wasn't fully synced), fall
# back to stdlib json rather than crashing the whole app at import time.
try:
    import orjson

    def _dumps(obj) -> bytes:
        return orjson.dumps(obj)

except ImportError:  # pragma: no cover
    import json

    logger.warning(
        "orjson not installed — falling back to stdlib json for the "
        "dashboard view. Run `pip install orjson` for better throughput "
        "on the cache-miss path."
    )

    def _dumps(obj) -> bytes:
        return json.dumps(obj, default=str).encode("utf-8")

# ---------------------------------------------------------------------
# CACHE CONFIG
# ---------------------------------------------------------------------

CACHE_VERSION = "v1"  # bump to invalidate all dashboard keys at once

SHELL_CACHE_KEY = f"dashboard:shell:{CACHE_VERSION}"
SHELL_BASE_TTL = 30           # seconds — real freshness window
SHELL_TTL_JITTER = 10         # +/- seconds to avoid synchronized expiry
SHELL_STALE_GRACE = 60        # serve stale-but-cached data up to this long
                              # past expiry while a refresh is in flight

LOCK_KEY = f"dashboard:shell:{CACHE_VERSION}:lock"
LOCK_TTL = 5                  # seconds — max time one process holds the lock

USER_FRAGMENT_TTL = 15        # seconds — small, cheap, per-user data
USER_FRAGMENT_TTL_JITTER = 5


# ---------------------------------------------------------------------
# STAMPEDE-SAFE CACHE-ASIDE HELPER
# ---------------------------------------------------------------------

def _jittered_ttl(base: int, jitter: int) -> int:
    return base + random.randint(-jitter, jitter)


def get_or_refresh(
    key: str,
    build_fn,
    base_ttl: int,
    jitter: int,
    stale_grace: int = 0,
    lock_key: Optional[str] = None,
    lock_ttl: int = 5,
):
    """
    Cache-aside read with:
      - stale-while-revalidate (serves the old value past its "fresh"
        TTL for up to `stale_grace` seconds while one process rebuilds)
      - stampede protection (only one process rebuilds at a time, via
        an atomic `cache.add` acting as a distributed lock)

    Stored envelope: {"data": ..., "fresh_until": epoch_seconds}
    """
    envelope = cache.get(key)
    now = time.time()

    if envelope is not None and envelope["fresh_until"] > now:
        # Hot path: still fresh. No lock, no DB, nothing — just return.
        return envelope["data"]

    lock_key = lock_key or f"{key}:lock"
    got_lock = cache.add(lock_key, "1", timeout=lock_ttl)

    if envelope is not None and (
        got_lock is False or now < envelope["fresh_until"] + stale_grace
    ):
        # Either someone else already holds the lock and is rebuilding,
        # or we're still within the stale-serving grace window.
        # Serve what we have — never make the user pay for a rebuild.
        if not got_lock:
            return envelope["data"]

    if not got_lock:
        # No cached value at all and someone else is building it.
        # Bounded short wait rather than a thundering herd on Postgres.
        for _ in range(20):
            time.sleep(0.05)
            envelope = cache.get(key)
            if envelope is not None:
                return envelope["data"]
        # Still nothing after ~1s — build it ourselves rather than 500.
        got_lock = cache.add(lock_key, "1", timeout=lock_ttl)

    try:
        data = build_fn()
        cache.set(
            key,
            {"data": data, "fresh_until": now + _jittered_ttl(base_ttl, jitter)},
            timeout=base_ttl + jitter + stale_grace + 30,
        )
        return data
    except Exception:
        logger.exception("dashboard cache rebuild failed for key=%s", key)
        if envelope is not None:
            return envelope["data"]  # degrade gracefully on error
        raise
    finally:
        cache.delete(lock_key)


# ---------------------------------------------------------------------
# PAYLOAD BUILDERS  (only ever run on a cache miss)
# ---------------------------------------------------------------------

def build_public_shell() -> dict:
    """
    Expensive-ish, but runs rarely (once per TTL window, not per request).
    Leans on already-denormalized counters instead of live aggregation.

    NOTE: deliberately does NOT go through Product.objects.trending_products()
    / active_products() here. Those manager methods attach
    select_related('brand', 'category', 'dealer') for other call sites that
    actually render brand/category info. This payload only ever reads
    product-local fields, so joining those relations would either be wasted
    work or (if .only() is applied afterward without including the FK
    columns) raise `FieldError: Field cannot be both deferred and traversed
    using select_related`. Querying directly avoids the join entirely.
    """
    trending = (
        Product.objects
        .filter(is_active=True, deleted_at__isnull=True)
        .only(
            "product_id", "product_name", "slug", "image",
            "selling_price", "final_price", "discount_percentage",
            "view_count", "total_sales", "rating_average",
        )
        .order_by("-view_count", "-total_sales")[:20]
    )
    featured_brands = (
        Brand.objects.featured_brands()
        .only("uuid", "brand_name", "brand_slug", "brand_logo")[:12]
    )
    featured_categories = (
        Category.objects.featured_categories()
        .only("uuid", "category_name", "category_slug", "category_image")[:12]
    )

    return {
        "generated_at": time.time(),
        "trending_products": [
            {
                "id": str(p.product_id),
                "name": p.product_name,
                "slug": p.slug,
                "image": p.image_url,
                "price": str(p.final_price or p.selling_price),
                "discount_pct": str(p.discount_percentage),
                "views": p.view_count,
                "sales": p.total_sales,
                "rating": str(p.rating_average),
            }
            for p in trending
        ],
        "featured_brands": [
            {"id": str(b.uuid), "name": b.brand_name, "slug": b.brand_slug, "logo": b.logo_url}
            for b in featured_brands
        ],
        "featured_categories": [
            {"id": str(c.uuid), "name": c.category_name, "slug": c.category_slug, "image": c.image_url}
            for c in featured_categories
        ],
    }


def build_user_fragment(user) -> dict:
    """
    Cheap, indexed-only, per-user counters. Kept deliberately tiny so
    caching it per-user is affordable even at huge scale.
    """
    return {
        "wishlist_count": Wishlist.objects.filter(user=user).count(),
        "order_count": Order.objects.filter(user=user).count(),
        "role": user.role,
        "display_name": user.display_name,
    }


def build_user_engine(user) -> dict:
    """
    Aggregates a user's ConnectedService rows into one summary — mirrors
    the "engine" structure already used elsewhere in the app (see the
    PROFILE DEBUG log: engine_keys=['connected_services',
    'extracted_images', 'extracted_videos', 'extracted_links',
    'extracted_texts', 'images_count', ...]).

    Only ever called on a per-user cache miss (short TTL), never per
    request — ConnectedService rows carry JSON blobs, so this is not
    something to run live on every hit.
    """
    services = list(
        ConnectedService.objects.filter(user=user).only(
            "id", "service_name", "service_url", "service_type", "status",
            "is_connected", "og_title", "og_thumbnail",
            "extracted_images", "extracted_videos", "extracted_links",
            "extracted_text", "fetch_status",
        )
    )

    extracted_images, extracted_videos, extracted_links, extracted_texts = [], [], [], []
    for s in services:
        extracted_images.extend(s.extracted_images or [])
        extracted_videos.extend(s.extracted_videos or [])
        extracted_links.extend(s.extracted_links or [])
        if s.extracted_text:
            extracted_texts.append(s.extracted_text)

    return {
        "connected_services": [
            {
                "id": s.id,
                "name": s.service_name,
                "url": s.service_url,
                "type": s.service_type,
                "status": s.status,
                "is_connected": s.is_connected,
                "og_title": s.og_title,
                "og_thumbnail": s.og_thumbnail,
                "fetch_status": s.fetch_status,
            }
            for s in services
        ],
        "extracted_images": extracted_images,
        "extracted_videos": extracted_videos,
        "extracted_links": extracted_links,
        "extracted_texts": extracted_texts,
        "images_count": len(extracted_images),
        "videos_count": len(extracted_videos),
        "links_count": len(extracted_links),
        "text_count": len(extracted_texts),
    }


def build_user_profile_snapshot(user) -> Optional[dict]:
    """
    Template-friendly snapshot of the user's ProfileInfo — includes
    business_* fields when the profile is a business/professional profile,
    social links (only the ones actually filled in), and the handful of
    verification/completion fields an enterprise profile view needs, so
    dashboard.html can render everything without a second live query.
    """
    profile: Optional[ProfileInfo] = getattr(user, "profileinfo", None)
    if profile is None:
        return None

    business = None
    if profile.is_business:
        business = {
            "name": profile.business_name,
            "type": profile.business_type,
            "registration": profile.business_registration,
            "tax_id": profile.business_tax_id,
            "email": profile.business_email,
            "phone": profile.business_phone,
            "website": profile.business_website,
            "description": profile.business_description,
        }

    # Only pass along social links that are actually set, so the template
    # can loop over `social.items` instead of `{% if %}`-checking each one.
    social_raw = {
        "facebook": profile.social_facebook,
        "twitter": profile.social_twitter,
        "instagram": profile.social_instagram,
        "linkedin": profile.social_linkedin,
        "youtube": profile.social_youtube,
        "tiktok": profile.social_tiktok,
        "whatsapp": profile.social_whatsapp,
    }
    social = {k: v for k, v in social_raw.items() if v}

    return {
        "display_name": profile.full_name,
        "tagline": profile.profile_tagline,
        "bio": profile.profile_bio,
        "photo": profile.get_profile_photo_url(),
        "cover_photo": profile.get_profile_cover_photo_url(),
        "is_verified": profile.is_profile_verified,
        "verification_level": profile.verification_level,
        "verified_at": profile.verified_at,
        "profile_type": profile.profile_type,
        "is_business": profile.is_business,
        "is_public": profile.is_profile_public,
        "is_featured": profile.is_profile_featured,
        "business": business,
        "social": social,
        "location": profile.location_display,
        "follower_count": profile.follower_count,
        "following_count": profile.following_count,
        "profile_views": profile.profile_views,
        "completion_percentage": profile.completion_percentage,
        "profile_url": profile.profile_url,
    }


def build_account_security(user) -> dict:
    """
    Small snapshot of account-level (not profile-level) fields from the
    custom User model — verification/MFA/lockout status plus locale
    preferences. All plain scalar/boolean fields already on the User row,
    so this costs nothing beyond the user object already loaded by auth
    middleware.
    """
    return {
        "account_status": user.account_status,
        "email": user.email,
        "phone": user.phone,
        "email_verified": user.email_verified,
        "phone_verified": user.phone_verified,
        "is_fully_verified": user.is_fully_verified,
        "mfa_enabled": user.mfa_enabled,
        "mfa_method": user.mfa_method,
        "is_locked": user.is_locked,
        "needs_password_rotation": user.needs_password_rotation,
        "days_since_joined": user.days_since_joined,
        "date_joined": user.date_joined,
        "last_login": user.last_login,
        "language": user.language,

        "country": user.country,
        "currency": user.currency,
    }


def build_profile_viewers(user, limit: int = 6) -> dict:
    """
    Recent authenticated visitors to this profile, from ProfileViewLog.
    That model keeps ONE row per viewer (see its UniqueConstraint on
    profile_user+viewer) updated in place on revisit, so "recent" here
    means recently-active viewers, not a raw event log — cheap to read
    and cheap to cache alongside the rest of the per-user extras.
    """
    recent = (
        ProfileViewLog.objects
        .filter(profile_user=user, viewer__isnull=False)
        .select_related("viewer", "viewer__profileinfo")
        .order_by("-viewed_at")[:limit]
    )

    viewers = []
    for log in recent:
        viewer = log.viewer
        viewer_profile = getattr(viewer, "profileinfo", None)
        viewers.append({
            "name": viewer_profile.full_name if viewer_profile else viewer.display_name,
            "photo": viewer_profile.get_profile_photo_url() if viewer_profile else None,
            "is_business": viewer_profile.is_business if viewer_profile else False,
            "viewed_at": log.viewed_at,
        })

    return {
        "recent": viewers,
        "unique_viewer_count": ProfileViewLog.objects.filter(
            profile_user=user, viewer__isnull=False
        ).count(),
    }


def build_user_dashboard_extras(user) -> dict:
    """
    Everything the HTML dashboard needs beyond the JSON API's tiny
    activity counters — bundled into ONE cache entry so a cache miss
    costs one round trip through this function, not five separate
    cache lookups.
    """
    return {
        "activity": build_user_fragment(user),
        "profile": build_user_profile_snapshot(user),
        "engine": build_user_engine(user),
        "account": build_account_security(user),
        "viewers": build_profile_viewers(user),
    }


# ---------------------------------------------------------------------
# VIEW
# ---------------------------------------------------------------------

class ProfessionalDashboardView(View):
    """
    GET /api/dashboard/

    Public shell payload is cached and CDN-cacheable; the small
    per-user fragment is merged in on top, cached separately and
    briefly. Supports conditional GET via ETag.
    """

    http_method_names = ["get"]

    async def get(self, request: HttpRequest, *args, **kwargs) -> HttpResponse:
        from asgiref.sync import sync_to_async

        shell = await sync_to_async(get_or_refresh)(
            key=SHELL_CACHE_KEY,
            build_fn=build_public_shell,
            base_ttl=SHELL_BASE_TTL,
            jitter=SHELL_TTL_JITTER,
            stale_grace=SHELL_STALE_GRACE,
            lock_key=LOCK_KEY,
            lock_ttl=LOCK_TTL,
        )

        payload = dict(shell)  # shallow copy — don't mutate the cached object

        user = getattr(request, "user", None)
        if user is not None and user.is_authenticated:
            user_key = f"dashboard:user:{CACHE_VERSION}:{user.pk}"
            fragment = await sync_to_async(get_or_refresh)(
                key=user_key,
                build_fn=lambda: build_user_fragment(user),
                base_ttl=USER_FRAGMENT_TTL,
                jitter=USER_FRAGMENT_TTL_JITTER,
            )
            payload["user"] = fragment
        else:
            payload["user"] = None

        body = _dumps(payload)
        etag = hashlib.md5(body).hexdigest()  # noqa: S324 — not a security use

        if_none_match = request.headers.get("If-None-Match")
        if if_none_match and if_none_match.strip('"') == etag:
            response = HttpResponse(status=304)
        else:
            response = HttpResponse(body, content_type="application/json")

        response["ETag"] = f'"{etag}"'

        if payload["user"] is None:
            # Fully anonymous, fully shared response -> let the CDN cache it.
            response["Cache-Control"] = (
                f"public, max-age={SHELL_BASE_TTL}, "
                f"stale-while-revalidate={SHELL_STALE_GRACE}, "
                f"stale-if-error=300"
            )
        else:
            # Contains per-user data -> private, short-lived, no CDN sharing.
            response["Cache-Control"] = f"private, max-age={USER_FRAGMENT_TTL}"
            response["Vary"] = "Cookie, Authorization"

        return response


class ProfessionalDashboardTemplateView(View):
    """
    GET /dashboard/

    Same cache-aside data as ProfessionalDashboardView, rendered as an
    HTML page instead of JSON. Sync (not async) because template
    rendering is CPU-bound and short — no benefit from async here,
    and it keeps this view usable on WSGI deployments too.

    Reuses build_public_shell / get_or_refresh directly, so the HTML
    and JSON endpoints share the shell cache — a warm shell for one
    path warms the other automatically.

    Template context (logged-in users only get non-None values for
    the last five):
        user     — the real request.user instance (never cached)
        activity — {"wishlist_count", "order_count", "role", "display_name"}
        profile  — ProfileInfo snapshot: business/social/location/
                   verification/completion — see build_user_profile_snapshot
        engine   — connected-services summary (images/videos/links/text)
        account  — User-model security/locale snapshot — see
                   build_account_security
        viewers  — {"recent": [...], "unique_viewer_count": n} from
                   ProfileViewLog — see build_profile_viewers
    """

    http_method_names = ["get"]
    template_name = "customer/dashboard.html"

    def get(self, request: HttpRequest, *args, **kwargs) -> HttpResponse:
        shell = get_or_refresh(
            key=SHELL_CACHE_KEY,
            build_fn=build_public_shell,
            base_ttl=SHELL_BASE_TTL,
            jitter=SHELL_TTL_JITTER,
            stale_grace=SHELL_STALE_GRACE,
            lock_key=LOCK_KEY,
            lock_ttl=LOCK_TTL,
        )

        request_user = getattr(request, "user", None)
        is_logged_in = bool(request_user and request_user.is_authenticated)

        activity = profile = engine = account = viewers = None

        if is_logged_in:
            # One cache entry, one round trip on a miss — covers activity
            # counters, the profile snapshot (incl. business_* fields),
            # the connected-services "engine" summary, account security/
            # locale fields, and recent profile viewers together.
            extras_key = f"dashboard:user_extras:{CACHE_VERSION}:{request_user.pk}"
            extras = get_or_refresh(
                key=extras_key,
                build_fn=lambda: build_user_dashboard_extras(request_user),
                base_ttl=USER_FRAGMENT_TTL,
                jitter=USER_FRAGMENT_TTL_JITTER,
            )
            activity = extras["activity"]
            profile = extras["profile"]
            engine = extras["engine"]
            account = extras["account"]
            viewers = extras["viewers"]

        context = {
            "trending_products": shell["trending_products"],
            "featured_brands": shell["featured_brands"],
            "featured_categories": shell["featured_categories"],
            "generated_at": shell["generated_at"],
            "cache_age_seconds": int(time.time() - shell["generated_at"]),
            "shell_ttl": SHELL_BASE_TTL,
            "cache_version": CACHE_VERSION,
            # The REAL User instance (not a cached dict) — safe to use
            # directly since it's already loaded by auth middleware and
            # costs nothing extra. Template can call .is_authenticated,
            # .role, .is_dealer, etc. directly.
            "user": request_user,
            "activity": activity,   # {"wishlist_count", "order_count", ...} or None
            "profile": profile,     # ProfileInfo snapshot dict or None
            "engine": engine,       # connected-services summary dict or None
            "account": account,     # User security/locale snapshot dict or None
            "viewers": viewers,     # {"recent": [...], "unique_viewer_count": n} or None
        }

        response = render(request, self.template_name, context)

        if not is_logged_in:
            # Fully anonymous, fully shared response -> let the CDN cache it.
            response["Cache-Control"] = (
                f"public, max-age={SHELL_BASE_TTL}, "
                f"stale-while-revalidate={SHELL_STALE_GRACE}, "
                f"stale-if-error=300"
            )
        else:
            response["Cache-Control"] = f"private, max-age={USER_FRAGMENT_TTL}"
            response["Vary"] = "Cookie, Authorization"

        return response


# ---------------------------------------------------------------------
# PROACTIVE REFRESH (Celery beat) — keeps the "miss" path rare in prod
# ---------------------------------------------------------------------
#
# from celery import shared_task
#
# @shared_task
# def refresh_dashboard_shell():
#     get_or_refresh(
#         key=SHELL_CACHE_KEY,
#         build_fn=build_public_shell,
#         base_ttl=SHELL_BASE_TTL,
#         jitter=SHELL_TTL_JITTER,
#         stale_grace=SHELL_STALE_GRACE,
#         lock_key=LOCK_KEY,
#         lock_ttl=LOCK_TTL,
#     )
#
# CELERY_BEAT_SCHEDULE = {
#     "refresh-dashboard-shell": {
#         "task": "apps.customer.tasks.refresh_dashboard_shell",
#         "schedule": 20.0,  # slightly less than SHELL_BASE_TTL
#     },
# }
#
# ---------------------------------------------------------------------
# URL wiring
# ---------------------------------------------------------------------
#
# apps/customer/urls.py
#   from apps.customer.views.dashboard import (
#       ProfessionalDashboardView,
#       ProfessionalDashboardTemplateView,
#   )
#   urlpatterns = [
#       path("api/dashboard/", ProfessionalDashboardView.as_view(), name="dashboard-api"),
#       path("dashboard/", ProfessionalDashboardTemplateView.as_view(), name="dashboard"),
#   ]
#
# Template location: apps/customer/templates/customer/dashboard.html
# (make sure 'APP_DIRS': True is set in TEMPLATES, or add the app's
# templates/ dir to DIRS)