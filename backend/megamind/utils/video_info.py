# megamind/utils/video_info.py
"""
Shared video-URL normalizer.

Turns any raw video URL — scraped or user-entered, doesn't matter — into
a platform-normalized dict the frontend can render as either an <iframe>
embed or a native <video> tag:

    { platform, embed_url, watch_url, thumbnail, type, mime_type? }

Used by both discovery_engine.py (product cards / video_info field) and
personal_engine.py (public Engine feed) so a video looks and plays
identically everywhere in the app, regardless of where the URL came from.

Supported platforms: YouTube, Vimeo, Dailymotion, Rumble, Streamable,
Twitch (channel + clip), Facebook, TikTok, Twitter/X, and any direct
video file (.mp4, .webm, .ogg, .mov, .m4v, .mkv, .avi).

get_video_info() always returns a dict (never None / raises).
Empty URL → {}.
"""

import hashlib
import re
import requests
from urllib.parse import parse_qs, urlparse, quote

from django.conf import settings
from django.core.cache import cache

from typing import Optional

_FB_SHARE_RESOLVE_TIMEOUT = 5
_FB_RESOLVE_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
}


def _resolve_facebook_share_link(url: str) -> str:
    """
    Facebook's newer share-link format (facebook.com/share/v/<id>/,
    /share/p/, /share/r/) is a redirector, not a canonical video URL —
    the video.php embed plugin can't resolve it directly and falls
    back to rendering a plain link card instead of a player.

    Follow the redirect chain server-side and return the canonical
    URL Facebook lands on, stripped of redirect-tracking query params
    (rdid, share_url, etc.) that add noise without adding embeddability.

    Fails open: any network error, timeout, or non-redirect just
    returns the original URL unchanged.
    """
    try:
        resp = requests.get(
            url,
            headers=_FB_RESOLVE_HEADERS,
            allow_redirects=True,
            timeout=_FB_SHARE_RESOLVE_TIMEOUT,
            stream=True,
        )
        resolved = resp.url
        resp.close()
        if resolved and resolved != url:
            parsed_resolved = urlparse(resolved)
            return f'{parsed_resolved.scheme}://{parsed_resolved.netloc}{parsed_resolved.path}'
    except requests.RequestException:
        pass
    return url

# How long to cache a parsed video_info dict per URL.
# URLs are stable (they come from the DB), so a long TTL is fine.
_VIDEO_INFO_TTL = getattr(settings, 'DE_VIDEO_INFO_TTL', 3600)


def get_video_info(url: str) -> dict:
    """
    Parse a raw video URL and return a platform-normalised dict that
    the template can use to render an <iframe> or <video> element.

    Returned keys
    -------------
    platform   str   youtube | vimeo | dailymotion | rumble | streamable |
                     twitch | twitch_clip | facebook | tiktok | twitter |
                     direct | unknown
    embed_url  str   URL suitable for an <iframe src="...">
    watch_url  str   Human-facing watch/share link
    thumbnail  str   Preview image URL (empty string when unavailable)
    type       str   "iframe" | "video"  — tells the template which tag to use
    mime_type  str   Only present when type == "video"
    """
    if not url:
        return {}

    url      = url.strip()
    parsed   = urlparse(url)
    hostname = parsed.netloc.lower().replace('www.', '')

    # ── YouTube ───────────────────────────────────────────────────
    if hostname in ('youtube.com', 'youtu.be', 'm.youtube.com', 'music.youtube.com'):
        vid_id = None
        if hostname == 'youtu.be':
            vid_id = parsed.path.lstrip('/').split('/')[0]
        elif '/shorts/' in parsed.path:
            vid_id = parsed.path.split('/shorts/')[1].split('/')[0]
        elif '/live/' in parsed.path:
            vid_id = parsed.path.split('/live/')[1].split('/')[0]
        elif '/embed/' in parsed.path:
            vid_id = parsed.path.split('/embed/')[1].split('/')[0]
        else:
            vid_id = parse_qs(parsed.query).get('v', [None])[0]
        if vid_id:
            vid_id = re.sub(r'[^a-zA-Z0-9_-]', '', vid_id)
            return {
                'platform':  'youtube',
                'embed_url': f'https://www.youtube.com/embed/{vid_id}?rel=0&modestbranding=1',
                'watch_url': f'https://www.youtube.com/watch?v={vid_id}',
                'thumbnail': f'https://img.youtube.com/vi/{vid_id}/hqdefault.jpg',
                'type':      'iframe',
            }

    # ── Vimeo ─────────────────────────────────────────────────────
    if hostname in ('vimeo.com', 'player.vimeo.com'):
        vid_id = (
            parsed.path.split('/video/')[1].split('/')[0]
            if '/video/' in parsed.path
            else parsed.path.lstrip('/').split('/')[0]
        )
        vid_id = re.sub(r'[^0-9]', '', vid_id)
        if vid_id:
            return {
                'platform':  'vimeo',
                'embed_url': f'https://player.vimeo.com/video/{vid_id}?badge=0&autopause=0',
                'watch_url': f'https://vimeo.com/{vid_id}',
                'thumbnail': '',
                'type':      'iframe',
            }

    # ── Dailymotion ───────────────────────────────────────────────
    if hostname in ('dailymotion.com', 'dai.ly'):
        if hostname == 'dai.ly':
            vid_id = parsed.path.lstrip('/').split('/')[0]
        elif '/video/' in parsed.path:
            vid_id = parsed.path.split('/video/')[1].split('_')[0].split('/')[0]
        else:
            vid_id = parsed.path.lstrip('/').split('/')[0]
        vid_id = re.sub(r'[^a-zA-Z0-9]', '', vid_id)
        if vid_id:
            return {
                'platform':  'dailymotion',
                'embed_url': f'https://www.dailymotion.com/embed/video/{vid_id}',
                'watch_url': f'https://www.dailymotion.com/video/{vid_id}',
                'thumbnail': f'https://www.dailymotion.com/thumbnail/video/{vid_id}',
                'type':      'iframe',
            }

    # ── Rumble ────────────────────────────────────────────────────
    if hostname == 'rumble.com':
        m = re.search(r'rumble\.com/embed/([^/?&]+)', url)
        vid_id = m.group(1) if m else None
        if not vid_id:
            m = re.search(r'rumble\.com/([^/?&]+)', url)
            vid_id = m.group(1) if m else None
        if vid_id:
            return {
                'platform':  'rumble',
                'embed_url': f'https://rumble.com/embed/{vid_id}/',
                'watch_url': url,
                'thumbnail': '',
                'type':      'iframe',
            }

    # ── Streamable ────────────────────────────────────────────────
    if hostname == 'streamable.com':
        vid_id = parsed.path.lstrip('/').split('/')[0]
        if vid_id:
            return {
                'platform':  'streamable',
                'embed_url': f'https://streamable.com/e/{vid_id}',
                'watch_url': url,
                'thumbnail': '',
                'type':      'iframe',
            }

    # ── Twitch ────────────────────────────────────────────────────
    if hostname in ('twitch.tv', 'clips.twitch.tv'):
        if '/clip/' in parsed.path or hostname == 'clips.twitch.tv':
            clip_id = parsed.path.lstrip('/').split('/')[-1]
            return {
                'platform':  'twitch_clip',
                'embed_url': f'https://clips.twitch.tv/embed?clip={clip_id}&parent={parsed.hostname}',
                'watch_url': url,
                'thumbnail': '',
                'type':      'iframe',
            }
        channel = parsed.path.lstrip('/').split('/')[0]
        return {
            'platform':  'twitch',
            'embed_url': f'https://player.twitch.tv/?channel={channel}&parent=yourdomain.com',
            'watch_url': url,
            'thumbnail': '',
            'type':      'iframe',
        }

    # ── Facebook ──────────────────────────────────────────────────
    if hostname in ('facebook.com', 'fb.watch', 'fb.com'):
        resolved_url = (
            _resolve_facebook_share_link(url)
            if '/share/' in parsed.path
            else url
        )
        return {
            'platform':  'facebook',
            'embed_url': f'https://www.facebook.com/plugins/video.php?href={quote(resolved_url, safe="")}&show_text=false&width=560',
            'watch_url': resolved_url,
            'thumbnail': '',
            'type':      'iframe',
        }

    # ── TikTok ────────────────────────────────────────────────────
    if hostname in ('tiktok.com', 'vm.tiktok.com'):
        m = re.search(r'/video/(\d+)', parsed.path)
        if m:
            return {
                'platform':  'tiktok',
                'embed_url': f'https://www.tiktok.com/embed/v2/{m.group(1)}',
                'watch_url': url,
                'thumbnail': '',
                'type':      'iframe',
            }

    # ── Twitter / X ───────────────────────────────────────────────
    if hostname in ('twitter.com', 'x.com', 't.co'):
        return {
            'platform':  'twitter',
            'embed_url': f'https://platform.twitter.com/embed/Tweet.html?id={parsed.path.split("/")[-1]}',
            'watch_url': url,
            'thumbnail': '',
            'type':      'iframe',
        }

    # ── Direct video file ─────────────────────────────────────────
    _VIDEO_EXTS = ('.mp4', '.webm', '.ogg', '.mov', '.m4v', '.mkv', '.avi')
    if any(parsed.path.lower().endswith(ext) for ext in _VIDEO_EXTS):
        ext = parsed.path.lower().rsplit('.', 1)[-1]
        mime_map = {
            'mp4': 'video/mp4', 'webm': 'video/webm', 'ogg': 'video/ogg',
            'mov': 'video/mp4', 'm4v': 'video/mp4',
            'mkv': 'video/x-matroska', 'avi': 'video/x-msvideo',
        }
        return {
            'platform':  'direct',
            'embed_url': url,
            'watch_url': url,
            'thumbnail': '',
            'type':      'video',
            'mime_type': mime_map.get(ext, 'video/mp4'),
        }

    # ── Unknown / fallback ────────────────────────────────────────
    return {
        'platform':  'unknown',
        'embed_url': url,
        'watch_url': url,
        'thumbnail': '',
        'type':      'iframe',
    }



def get_video_info_cached(url: Optional[str]) -> dict:
    """
    Thin cache wrapper around get_video_info().
    Cache key: de:vi:<blake2b of url>  TTL: _VIDEO_INFO_TTL (default 1 h).
    Returns {} immediately for falsy URLs — no cache I/O.
    Fails open (falls back to uncached parse) if the cache backend errors.
    """
    if not url:
        return {}
    key = 'de:vi:' + hashlib.blake2b(url.encode(), digest_size=10).hexdigest()
    try:
        cached = cache.get(key)
    except Exception:
        cached = None
    if cached is not None:
        return cached
    info = get_video_info(url)
    if info:
        try:
            cache.set(key, info, _VIDEO_INFO_TTL)
        except Exception:
            pass
    return info