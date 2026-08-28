# megamind/utils/feed_cache.py
#
# Builds and persists the precomputed engine-feed payload for a
# ConnectedService (see engine/business_engine/personal_engine.py for
# where this gets read).
#
# Lives in megamind rather than in the engine app on purpose: this is
# called from producer code (scraper / service_fetcher, both in
# megamind) right after extracted_images/extracted_videos/extracted_links
# are written. Putting it in engine/business_engine would mean megamind
# importing from the engine app — backwards, since engine already
# depends on megamind. personal_engine.py imports `refresh_feed_cache`
# from here instead of defining it locally.

from __future__ import annotations

import base64
import logging
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional

from apps.customer.models.profile_info import ProfileInfo
from megamind.models.connected_service import ConnectedService
from megamind.utils.media_info import normalize_images, normalize_links
from megamind.utils.video_info import get_video_info_cached, resolve_post_video

logger = logging.getLogger(__name__)


def _encode_cursor(created_at: datetime, pk: Any) -> str:
    raw = f"{created_at.isoformat()}|{pk}"
    return base64.urlsafe_b64encode(raw.encode()).decode()


def _normalize_videos(raw_videos: Optional[Iterable[Any]], limit: Optional[int] = None) -> List[dict]:
    """
    See engine/business_engine/personal_engine.py for the full
    rationale — kept identical, just relocated. Accepts scraper dicts
    like {"url": ..., "type": "video/*"|"embed"}, bare URL strings, or
    already-normalized video_info dicts.
    """
    normalized: List[dict] = []
    for v in raw_videos or []:
        if isinstance(v, dict):
            url = v.get("url") or v.get("embed_url") or v.get("src")
        else:
            url = v
        if not url:
            continue
        try:
            info = get_video_info_cached(url)
        except Exception:
            # One bad/unreachable video URL shouldn't blank out every
            # other video (or the whole feed cache refresh) for this post.
            logger.exception("_normalize_videos: get_video_info_cached failed for %s", url)
            continue
        if info:
            normalized.append(info)
        if limit is not None and len(normalized) >= limit:
            break
    return normalized


def _resolve_profile(svc: ConnectedService) -> Optional[ProfileInfo]:
    """
    Prefer the explicit ConnectedService.profile FK when it's set —
    that field exists precisely to record which of a user's profiles
    a given service belongs to (multi-profile / business accounts).
    Falling back unconditionally to the user's default profile
    (user.profileinfo) would misattribute the feed card's name/avatar/
    tagline/url to the wrong profile for any user with more than one,
    even though the service was explicitly tagged.

    `profile` is a plain forward FK (nullable), so reading svc.profile
    directly returns None when unset — no DoesNotExist handling
    needed there, unlike the reverse one-to-one fallback below.
    """
    if svc.profile_id:
        return svc.profile
    try:
        return svc.user.profileinfo
    except ProfileInfo.DoesNotExist:
        return None


def _build_attribution(svc: ConnectedService) -> Dict[str, Any]:
    user = svc.user
    profile = _resolve_profile(svc)

    return {
        "user_id": user.pk,
        "user_uuid": str(user.uuid),
        "name": profile.profile_name if profile else user.email_or_phone,
        "avatar": profile.get_profile_photo_url() if profile else "/static/defaults/default-profile-picture.png",
        "tagline": profile.profile_tagline if profile else "",
        "location": profile.location_display if profile else "",
        "verified": bool(profile.is_profile_verified) if profile else False,
        "type": profile.profile_type if profile else "personal",
        "url": profile.profile_url if profile else f"/profile/{user.uuid}/",
    }


def refresh_feed_cache(svc: ConnectedService) -> Dict[str, Any]:
    """
    Builds the full (unfiltered by media_type) serialized engine-feed
    payload for one ConnectedService and persists it to
    `svc.cached_feed_payload`.

    Call this once, right after extracted_images / extracted_videos /
    extracted_links are written on a service — i.e. from the producer
    save path (scraper / service_fetcher) — never from a request path.
    This is where the expensive work happens (image/video/link
    normalization); it should happen once per service update, not once
    per feed read per visitor.

    Only makes sense to call for services that are actually eligible
    for the public feed, but it's harmless to call unconditionally —
    the payload just won't be read if the service isn't public/connected/
    successful (see _get_public_feed_queryset's filters).

    Requires `cached_feed_payload = models.JSONField(null=True, blank=True)`
    on ConnectedService.

    Raises if the ConnectedService itself is unusable (e.g. no
    created_at) or if the DB write fails — those indicate a genuinely
    broken record/connection and the caller (producer save path) should
    know about it. Per-item normalization failures (one bad image/video/
    link) are swallowed and logged instead, so a single malformed entry
    doesn't blank out an otherwise-good feed card.
    """
    try:
        images = normalize_images(svc.extracted_images)
    except Exception:
        logger.exception("refresh_feed_cache: normalize_images failed for service %s", svc.pk)
        images = []

    try:
        videos = _normalize_videos(svc.extracted_videos)
    except Exception:
        logger.exception("refresh_feed_cache: _normalize_videos failed for service %s", svc.pk)
        videos = []

    try:
        links = normalize_links(svc.extracted_links)
    except Exception:
        logger.exception("refresh_feed_cache: normalize_links failed for service %s", svc.pk)
        links = []

    # Guaranteed single video for card display — real extracted video
    # if there is one, else the source URL if it resolves to a known
    # platform, else a deterministic themed placeholder. Never empty.
    try:
        video_info = resolve_post_video(
            extracted_videos=svc.extracted_videos,
            source_url=svc.service_url,
            seed=str(svc.pk),
            service_type=svc.service_type,
        )
    except Exception:
        logger.exception("refresh_feed_cache: resolve_post_video failed for service %s", svc.pk)
        video_info = None

    payload = {
        "id": svc.pk,
        "cursor": _encode_cursor(svc.created_at, svc.pk),
        "attribution": _build_attribution(svc),
        "source": {
            "service_id": svc.pk,
            "service_name": svc.service_name,
            "service_url": svc.service_url,
            "service_type": svc.service_type,
            "og_title": svc.og_title or svc.service_name,
            "og_description": svc.og_description or "",
            "og_site_name": svc.og_site_name or "",
            "og_thumbnail": svc.og_thumbnail or "",
        },
        "images": images,
        "videos": videos,
        "video_info": video_info,  # ← guaranteed single video for card display
        "links": links,
        "images_count": len(images),
        "videos_count": len(videos),
        "links_count": len(links),
        "posted_at": svc.created_at.isoformat(),
        "updated_at": svc.updated_at.isoformat(),
    }

    updated = ConnectedService.objects.filter(pk=svc.pk).update(cached_feed_payload=payload)
    if not updated:
        # pk vanished (deleted concurrently) — the caller building this
        # payload for a no-longer-existent row should know, since the
        # feed cache for it will never be read anyway.
        logger.warning("refresh_feed_cache: ConnectedService %s no longer exists; cache not persisted", svc.pk)

    return payload