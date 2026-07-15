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

import base64
import logging

from apps.customer.models.profile_info import ProfileInfo
from megamind.models.connected_service import ConnectedService
from megamind.utils.media_info import normalize_images, normalize_links
from megamind.utils.video_info import get_video_info_cached

logger = logging.getLogger(__name__)


def _encode_cursor(created_at, pk) -> str:
    raw = f"{created_at.isoformat()}|{pk}"
    return base64.urlsafe_b64encode(raw.encode()).decode()


def _normalize_videos(raw_videos, limit: int = None) -> list[dict]:
    """
    See engine/business_engine/personal_engine.py for the full
    rationale — kept identical, just relocated. Accepts scraper dicts
    like {"url": ..., "type": "video/*"|"embed"}, bare URL strings, or
    already-normalized video_info dicts.
    """
    normalized = []
    for v in raw_videos or []:
        if isinstance(v, dict):
            url = v.get('url') or v.get('embed_url') or v.get('src')
        else:
            url = v
        if not url:
            continue
        info = get_video_info_cached(url)
        if info:
            normalized.append(info)
        if limit and len(normalized) >= limit:
            break
    return normalized


def _build_attribution(svc: ConnectedService) -> dict:
    user = svc.user
    try:
        profile = user.profileinfo
    except ProfileInfo.DoesNotExist:
        profile = None

    return {
        'user_id':   user.pk,
        'user_uuid': str(user.uuid),
        'name':      profile.profile_name if profile else user.email_or_phone,
        'avatar':    profile.get_profile_photo_url() if profile else '/static/defaults/default-profile-picture.png',
        'tagline':   profile.profile_tagline if profile else '',
        'location':  profile.location_display if profile else '',
        'verified':  bool(profile.is_profile_verified) if profile else False,
        'type':      profile.profile_type if profile else 'personal',
        'url':       profile.profile_url if profile else f'/profile/{user.uuid}/',
    }


def refresh_feed_cache(svc: ConnectedService) -> dict:
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
    """
    images = normalize_images(svc.extracted_images)
    videos = _normalize_videos(svc.extracted_videos)
    links  = normalize_links(svc.extracted_links)

    payload = {
        'id':     svc.pk,
        'cursor': _encode_cursor(svc.created_at, svc.pk),
        'attribution': _build_attribution(svc),
        'source': {
            'service_id':     svc.pk,
            'service_name':   svc.service_name,
            'service_url':    svc.service_url,
            'service_type':   svc.service_type,
            'og_title':       svc.og_title or svc.service_name,
            'og_description': svc.og_description or '',
            'og_site_name':   svc.og_site_name or '',
            'og_thumbnail':   svc.og_thumbnail or '',
        },
        'images': images,
        'videos': videos,
        'links':  links,
        'images_count': len(images),
        'videos_count': len(videos),
        'links_count':  len(links),
        'posted_at':  svc.created_at.isoformat(),
        'updated_at': svc.updated_at.isoformat(),
    }

    ConnectedService.objects.filter(pk=svc.pk).update(cached_feed_payload=payload)
    return payload