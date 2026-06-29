# engine/business_engine/personal_engine.py

# Standard Library
import json
import logging
import random
import threading
from typing import Any
from urllib.parse import urlparse

# Django
from django.contrib.auth.decorators import login_required
from django.core.cache import cache
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, render
from django.utils import timezone
from django.views.decorators.cache import cache_control
from django.views.decorators.http import require_http_methods
from django.views.decorators.vary import vary_on_cookie

# Local Apps
from apps.customer.models.profile_info import ProfileInfo
from megamind.models.connected_service import ConnectedService
from megamind.services.scraper import scrape_url
from megamind.utils.service_fetcher import fetch_service_data

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════
# TTLs
# ═══════════════════════════════════════════════════════════════════

ENGINE_CACHE_TTL  = 60 * 5    # 5 min  — full engine context
PROFILE_CACHE_TTL = 60 * 10   # 10 min — profile info (changes rarely)
ERROR_BACKOFF_TTL = 60 * 10   # 10 min — cooldown after a failed fetch
STALE_AFTER       = 3600      # 1 hour — refetch if older than this


# ═══════════════════════════════════════════════════════════════════
# CACHE KEY HELPERS
# ═══════════════════════════════════════════════════════════════════

def _key_engine(uid: int)  -> str: return f"engine:ctx:{uid}"
def _key_profile(uid: int) -> str: return f"engine:profile:{uid}"


def _jittered_ttl(ttl: int, spread: float = 0.10) -> int:
    delta = int(ttl * spread)
    return ttl + random.randint(-delta, delta) if delta else ttl


# ═══════════════════════════════════════════════════════════════════
# CACHE INVALIDATION
# ═══════════════════════════════════════════════════════════════════

def invalidate_engine_cache(user_id: int) -> None:
    """Call this from post_save signals on ConnectedService."""
    cache.delete_many([_key_engine(user_id), _key_profile(user_id)])


# ═══════════════════════════════════════════════════════════════════
# STALENESS CHECK
# ═══════════════════════════════════════════════════════════════════

def _should_fetch(service: ConnectedService) -> bool:
    """
    True  → service data is missing or older than STALE_AFTER.
    False → data is fresh, or in error cooldown (10 min backoff).
    Never blocks — caller is responsible for spawning a thread.
    """
    if service.last_fetch_time is None:
        return True
    age = (timezone.now() - service.last_fetch_time).total_seconds()
    if service.fetch_status == 'error':
        return age > ERROR_BACKOFF_TTL   # don't hammer failing hosts
    return age > STALE_AFTER


# ═══════════════════════════════════════════════════════════════════
# BACKGROUND REFRESH
# ═══════════════════════════════════════════════════════════════════

def _background_refresh(service_id: int, user_id: int) -> None:
    """
    Fetches one service in a daemon thread so the view never blocks.
    Busts the engine cache on success so the next request sees fresh data.

    Replace threading.Thread with a Celery task when available:
        refresh_connected_service.delay(service_id, user_id)
    """
    try:
        service = ConnectedService.objects.get(pk=service_id)
        fetch_service_data(service)
        # Bust engine cache so next request reloads from DB
        cache.delete(_key_engine(user_id))
        logger.info(
            "Background refresh complete service_id=%s uid=%s status=%s",
            service_id, user_id, service.fetch_status,
        )
    except Exception:
        logger.exception(
            "Background refresh failed service_id=%s uid=%s",
            service_id, user_id,
        )


def _spawn_refresh(service_id: int, user_id: int) -> None:
    t = threading.Thread(
        target=_background_refresh,
        args=(service_id, user_id),
        daemon=True,
    )
    t.start()


# ═══════════════════════════════════════════════════════════════════
# SERIALISER  (plain dicts — pickle-safe for Redis)
# ═══════════════════════════════════════════════════════════════════

def _serialize_service(service: ConnectedService) -> dict[str, Any]:
    """
    Flatten a ConnectedService into a plain dict.
    Falls back to last_fetched_data for services that were saved before
    the flat OG/extracted columns existed.
    """
    # ── OG fields ────────────────────────────────────────────────
    og_title       = service.og_title       or ""
    og_description = service.og_description or ""
    og_thumbnail   = service.og_thumbnail   or ""
    og_site_name   = service.og_site_name   or ""
    og_type        = service.og_type        or ""

    # ── Extracted media ───────────────────────────────────────────
    images = service.extracted_images or []
    videos = service.extracted_videos or []
    links  = service.extracted_links  or []
    text   = service.extracted_text   or ""

    # ── Fallback: pull from raw cache blob ────────────────────────
    raw = service.last_fetched_data or {}
    if raw and not og_title:
        og_title       = raw.get("title")       or raw.get("og_title")       or ""
        og_description = raw.get("description") or raw.get("og_description") or ""
        og_thumbnail   = (raw.get("og_image")   or raw.get("og_thumbnail")
                          or raw.get("thumbnail") or "")
        og_site_name   = raw.get("site_name")   or raw.get("og_site_name")   or ""
        og_type        = raw.get("og_type")     or raw.get("type")           or ""
    if raw and not images:
        images = [_coerce_image(i) for i in (raw.get("images") or []) if i]
    if raw and not videos:
        videos = [_coerce_video(v) for v in (raw.get("videos") or []) if v]
    if raw and not links:
        links  = [_coerce_link(l)  for l in (raw.get("links")  or []) if l]
    if raw and not text:
        text = (raw.get("full_content") or raw.get("text_content")
                or raw.get("content")   or raw.get("text") or "")

    domain = urlparse(service.service_url).netloc

    return {
        # Identity
        "id":            service.id,
        "service_name":  service.service_name,
        "service_url":   service.service_url,
        "service_type":  service.service_type,
        "service_domain": domain,
        "status":        service.status,
        "is_connected":  service.is_connected,

        # OG / meta
        "og_title":       og_title,
        "og_description": og_description,
        "og_thumbnail":   og_thumbnail,
        "og_site_name":   og_site_name or domain,
        "og_type":        og_type,

        # Extracted media
        "extracted_images": images,
        "extracted_videos": videos,
        "extracted_links":  links,
        "extracted_text":   text,

        # Counts (cheap to pre-compute here, avoids |length in template)
        "images_count": len(images),
        "videos_count": len(videos),
        "links_count":  len(links),
        "has_text":     bool(text.strip()),

        # Fetch metadata
        "fetch_status":    service.fetch_status,
        "fetch_error":     service.fetch_error or "",
        "last_fetch_time": (
            service.last_fetch_time.isoformat()
            if service.last_fetch_time else None
        ),
        "is_stale": _should_fetch(service),
    }


# ── Coercion helpers (handles both dict and bare-string payloads) ──

def _coerce_image(img) -> dict:
    if isinstance(img, dict):
        return {
            "url": img.get("url") or img.get("src") or "",
            "alt": img.get("alt") or "",
            "href": img.get("href") or "",
        }
    return {"url": str(img), "alt": "", "href": ""}


def _coerce_video(v) -> dict:
    if isinstance(v, dict):
        return {
            "url":  v.get("url") or v.get("src") or "",
            "type": v.get("type") or "",
        }
    return {"url": str(v), "type": ""}


def _coerce_link(lnk) -> dict:
    if isinstance(lnk, dict):
        return {
            "href": lnk.get("href") or lnk.get("url")   or "",
            "text": lnk.get("text") or lnk.get("title") or "",
        }
    return {"href": str(lnk), "text": ""}


# ═══════════════════════════════════════════════════════════════════
# DATA LOADERS
# ═══════════════════════════════════════════════════════════════════

def _load_profile(user) -> dict[str, Any]:
    """Serialise ProfileInfo to a plain dict. Critical — raises on miss."""
    p = get_object_or_404(ProfileInfo.objects.select_related('user'), user=user)
    return {
        "profile_name":          p.profile_name,
        "profile_name_slug":     p.profile_name_slug,
        "profile_photo":         p.get_profile_photo_url(),
        "profile_cover_photo":   p.get_profile_cover_photo_url(),
        "profile_tagline":       p.profile_tagline,
        "profile_type":          p.profile_type,
        "profile_type_display":  p.get_profile_type_display(),
        "profile_bio":           p.profile_bio,
        "location_display":      p.location_display,
        "is_profile_verified":   p.is_profile_verified,
        "verification_level":    p.verification_level,
        "completion_percentage": p.completion_percentage,
        "follower_count":        p.follower_count,
        "following_count":       p.following_count,
        "engagement_score":      round(p.get_engagement_score(), 1),
        # Business
        "business_name":         p.business_name,
        "business_type":         p.business_type,
        "business_website":      p.business_website,
        "business_email":        p.business_email,
        "business_phone":        p.business_phone,
        "business_description":  p.business_description,
        # Social
        "social_facebook":       p.social_facebook,
        "social_twitter":        p.social_twitter,
        "social_instagram":      p.social_instagram,
        "social_linkedin":       p.social_linkedin,
        "social_youtube":        p.social_youtube,
        "social_tiktok":         p.social_tiktok,
        "social_whatsapp":       p.social_whatsapp,
    }


def _load_engine(user) -> dict[str, Any]:
    """
    Load all connected services from DB and build the engine context.
    Never fetches — fetching is always done in a background thread.
    """
    services = list(
        ConnectedService.objects
        .filter(user=user)
        .only(
            'service_name', 'service_url', 'service_type', 'status',
            'is_connected', 'og_title', 'og_description', 'og_thumbnail',
            'og_site_name', 'og_type', 'extracted_images', 'extracted_videos',
            'extracted_links', 'extracted_text', 'last_fetched_data',
            'fetch_status', 'fetch_error', 'last_fetch_time',
        )
    )

    serialized      = [_serialize_service(s) for s in services]
    connected       = [s for s in serialized if s['is_connected']]
    all_images      = []
    all_videos      = []
    all_links       = []
    all_texts       = []

    for svc in connected:
        meta = {
            'service_name':   svc['service_name'],
            'service_url':    svc['service_url'],
            'service_type':   svc['service_type'],
            'service_domain': svc['service_domain'],
            'fetch_status':   svc['fetch_status'],
            'last_fetch_time':svc['last_fetch_time'],
            'og_thumbnail':   svc['og_thumbnail'],
            'og_description': svc['og_description'],
        }

        for img in svc['extracted_images']:
            all_images.append({**meta, **img, 'title': svc['og_title'] or svc['service_name']})

        for vid in svc['extracted_videos']:
            all_videos.append({**meta, **vid, 'title': svc['og_title'] or svc['service_name']})

        for lnk in svc['extracted_links']:
            all_links.append({
                **meta,
                'url':    lnk['href'],
                'title':  lnk['text'] or svc['og_title'] or svc['service_name'],
                'domain': urlparse(lnk['href']).netloc or svc['service_domain'],
            })

        if svc['has_text']:
            all_texts.append({
                **meta,
                'title':   svc['og_title'] or svc['service_name'],
                'content': svc['extracted_text'],
            })

    return {
        # Full service list (all, not just connected)
        'services':           serialized,
        'connected_services': connected,
        'total_services':     len(serialized),
        'connected_count':    len(connected),

        # Status summary for sidebar dots
        'success_count': sum(1 for s in serialized if s['fetch_status'] == 'success'),
        'error_count':   sum(1 for s in serialized if s['fetch_status'] == 'error'),
        'pending_count': sum(1 for s in serialized if s['fetch_status'] == 'pending'),

        # Aggregated media (all connected services merged)
        'extracted_images': all_images,
        'extracted_videos': all_videos,
        'extracted_links':  all_links,
        'extracted_texts':  all_texts,

        # Counts
        'images_count': len(all_images),
        'videos_count': len(all_videos),
        'links_count':  len(all_links),
        'text_count':   len(all_texts),

        # JSON blob for JS (lightweight — no images/videos/links/text)
        'services_json': json.dumps([
            {
                'id':           s['id'],
                'service_name': s['service_name'],
                'service_url':  s['service_url'],
                'service_type': s['service_type'],
                'is_connected': s['is_connected'],
                'og_thumbnail': s['og_thumbnail'],
                'og_title':     s['og_title'],
                'fetch_status': s['fetch_status'],
                'is_stale':     s['is_stale'],
            }
            for s in serialized
        ], ensure_ascii=False),
    }


# ═══════════════════════════════════════════════════════════════════
# VIEW
# ═══════════════════════════════════════════════════════════════════
# engine/business_engine/personal_engine.py

import logging
from django.contrib.auth.decorators import login_required
from django.db.models import Count, Prefetch
from django.shortcuts import render
from django.views.decorators.cache import cache_control
from django.views.decorators.vary import vary_on_cookie

from apps.customer.models.account import User
from apps.customer.models.profile_info import ProfileInfo
from megamind.models.connected_service import ConnectedService

logger = logging.getLogger(__name__)


@login_required(login_url='/customer/signin/')
@vary_on_cookie
@cache_control(private=True, max_age=0, must_revalidate=True)
def EngineView(request):
    """
    Global engine feed — shows ALL users with their profile info
    and every connected service's extracted data.
    Each 'post' = one ConnectedService attached to a user+profile.
    """

    # Fetch all connected services that have been successfully fetched,
    # prefetch their user and profile in one query trip.
    services = (
        ConnectedService.objects
        .filter(is_connected=True)
        .select_related(
            'user',
            'user__profileinfo',   # OneToOne reverse
        )
        .order_by('-created_at')
    )

    # Build feed posts — one entry per connected service
    feed = []
    for svc in services:
        user = svc.user
        try:
            profile = user.profileinfo
        except ProfileInfo.DoesNotExist:
            profile = None

        feed.append({
            # ── User info ──────────────────────────────────
            'user_id':          user.pk,
            'user_uuid':        str(user.uuid),
            'user_role':        user.role,
            'user_email':       user.email or user.email_or_phone,

            # ── Profile info ───────────────────────────────
            'profile_name':     profile.profile_name if profile else user.email_or_phone,
            'profile_photo':    profile.get_profile_photo_url() if profile else '/static/defaults/default-profile-picture.png',
            'profile_tagline':  profile.profile_tagline if profile else '',
            'profile_location': profile.location_display if profile else '',
            'profile_verified': profile.is_profile_verified if profile else False,
            'profile_type':     profile.profile_type if profile else 'personal',
            'profile_url':      profile.profile_url if profile else f'/profile/{user.uuid}/',
            'follower_count':   profile.follower_count if profile else 0,

            # ── Service info ───────────────────────────────
            'svc_id':           svc.pk,
            'svc_name':         svc.service_name,
            'svc_url':          svc.service_url,
            'svc_type':         svc.service_type,
            'svc_status':       svc.status,           # public / private
            'fetch_status':     svc.fetch_status,

            # ── OG / meta ──────────────────────────────────
            'og_title':         svc.og_title or svc.service_name,
            'og_description':   svc.og_description or '',
            'og_thumbnail':     svc.og_thumbnail or '',
            'og_site_name':     svc.og_site_name or '',
            'og_type':          svc.og_type or '',

            # ── Extracted content ──────────────────────────
            'images':           svc.extracted_images or [],
            'videos':           svc.extracted_videos or [],
            'links':            svc.extracted_links  or [],
            'text':             svc.extracted_text   or '',

            # ── Counts ─────────────────────────────────────
            'images_count':     len(svc.extracted_images or []),
            'links_count':      len(svc.extracted_links  or []),
            'has_text':         bool((svc.extracted_text or '').strip()),

            # ── Timestamps ─────────────────────────────────
            'posted_at':        svc.created_at,
            'last_fetch':       svc.last_fetch_time,
        })

    # Global stats for header strip
    all_users_count     = User.objects.filter(is_active=True, deleted_at__isnull=True).count()
    total_services      = ConnectedService.objects.filter(is_connected=True).count()
    total_images        = sum(p['images_count'] for p in feed)
    total_links         = sum(p['links_count']  for p in feed)

    context = {
        'current_user':   request.user,
        'feed':           feed,
        'total_users':    all_users_count,
        'total_services': total_services,
        'total_images':   total_images,
        'total_links':    total_links,
        'feed_count':     len(feed),
    }

    return render(request, 'business/engine.html', context)

@login_required(login_url='/customer/signin/')
def PersonalEngineView(request):

    context = {

    }
    return render(request, "business/personal_engine.html", context)

