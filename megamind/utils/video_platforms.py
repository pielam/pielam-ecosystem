# megamind/utils/video_platforms.py

"""

Turns a raw video URL (found in a <video>/<source>/<iframe> tag, or a
bare link) into a normalized dict describing which platform it's from
and how to embed/play it:

    {platform, embed_url, watch_url, thumbnail, type, mime_type?}

Supports YouTube, Vimeo, Dailymotion, Rumble, Streamable, Twitch,
Facebook, TikTok, Twitter/X, and direct video files. Falls back to a
generic 'unknown'/'direct' entry for anything else rather than
dropping it.

Short-link platforms (TikTok's vm.tiktok.com, Twitter's t.co) are
redirectors — the short code alone doesn't encode a video/status ID —
so those are resolved via one extra guarded request through
http_fetcher before parsing. That resolution fails open: if it can't
resolve (network blocked, rate-limited, etc.) the function falls
through to the generic fallback instead of raising.
"""

from __future__ import annotations

import logging
import re
from urllib.parse import parse_qs, quote, urlparse

from http_fetcher import guarded_get, build_session, DEFAULT_HEADERS, UnsafeURLError

logger = logging.getLogger(__name__)

_VIDEO_EXTS = (".mp4", ".webm", ".ogg", ".mov", ".m4v", ".mkv", ".avi")
_VIDEO_MIME_MAP = {
    "mp4": "video/mp4", "webm": "video/webm", "ogg": "video/ogg",
    "mov": "video/mp4", "m4v": "video/mp4",
    "mkv": "video/x-matroska", "avi": "video/x-msvideo",
}
_RESOLVE_TIMEOUT = 8

# Hostname sets, precompiled regexes: get_video_info() runs once per
# scraped/extracted video URL, so anything built inline in the
# function body (tuples, regex pattern strings) gets rebuilt on every
# call. Hoisting these here avoids that per-call cost.
_YOUTUBE_HOSTS = frozenset({"youtube.com", "youtu.be", "m.youtube.com", "music.youtube.com"})
_VIMEO_HOSTS = frozenset({"vimeo.com", "player.vimeo.com"})
_DAILYMOTION_HOSTS = frozenset({"dailymotion.com", "dai.ly"})
_TWITCH_HOSTS = frozenset({"twitch.tv", "clips.twitch.tv"})
_FACEBOOK_HOSTS = frozenset({"facebook.com", "fb.watch", "fb.com"})
_TIKTOK_HOSTS = frozenset({"tiktok.com", "vm.tiktok.com", "vt.tiktok.com"})
_TIKTOK_SHORT_HOSTS = frozenset({"vm.tiktok.com", "vt.tiktok.com"})
_TWITTER_HOSTS = frozenset({"twitter.com", "x.com", "t.co"})

_RE_YT_ID_SANITIZE = re.compile(r"[^a-zA-Z0-9_-]")
_RE_VIMEO_DIGITS_ONLY = re.compile(r"[^0-9]")
_RE_DAILYMOTION_SANITIZE = re.compile(r"[^a-zA-Z0-9]")
_RE_RUMBLE_EMBED_ID = re.compile(r"rumble\.com/embed/([^/?&]+)")
_RE_RUMBLE_GENERIC_ID = re.compile(r"rumble\.com/([^/?&]+)")
_RE_TIKTOK_VIDEO_ID = re.compile(r"/video/(\d+)")
_RE_TWITTER_STATUS_ID = re.compile(r"/status(?:es)?/(\d+)")


def _iframe_result(platform: str, embed_url: str, watch_url: str, thumbnail: str = "") -> dict:
    """Every iframe-embed platform branch below returns this exact
    shape by hand; centralizing it means the dict shape only needs to
    be right in one place."""
    return {
        "platform": platform,
        "embed_url": embed_url,
        "watch_url": watch_url,
        "thumbnail": thumbnail,
        "type": "iframe",
    }


def _video_result(platform: str, url: str, mime_type: str = "video/mp4", thumbnail: str = "") -> dict:
    """Same as _iframe_result but for the native <video>-tag branches
    (direct file links and the 'unknown' fallback)."""
    return {
        "platform": platform,
        "embed_url": url,
        "watch_url": url,
        "thumbnail": thumbnail,
        "type": "video",
        "mime_type": mime_type,
    }


def _resolve_short_link(url: str) -> str:
    """Follows a short-link redirect chain (TikTok vm/vt, Twitter t.co)
    server-side and returns the landing URL, stripped of query string.
    Fails open — returns the original URL unchanged on any error, but
    logs why, so a resolution outage (egress blocked, rate-limited,
    etc.) is visible instead of silently producing a broken embed."""
    try:
        session = build_session(max_retries=1)
        try:
            resp = guarded_get(session, url, dict(DEFAULT_HEADERS), timeout=_RESOLVE_TIMEOUT)
            resolved = resp.url
            resp.close()
            if resolved and resolved != url:
                p = urlparse(resolved)
                return f"{p.scheme}://{p.netloc}{p.path}"
        finally:
            session.close()
    except UnsafeURLError as exc:
        logger.warning("Refused to resolve short link %s: %s", url, exc)
    except Exception as exc:
        logger.warning("Short-link resolution failed for %s: %s", url, exc)
    return url


def get_video_info(url: str, resolve_short_links: bool = True) -> dict:
    """Parse `url` and return a normalized video-info dict. Never
    raises; returns {} for an empty URL."""
    if not url:
        return {}

    url = url.strip()
    parsed = urlparse(url)
    hostname = parsed.netloc.lower().replace("www.", "")

    # YouTube
    if hostname in _YOUTUBE_HOSTS:
        vid_id = None
        if hostname == "youtu.be":
            vid_id = parsed.path.lstrip("/").split("/")[0]
        elif "/shorts/" in parsed.path:
            vid_id = parsed.path.split("/shorts/")[1].split("/")[0]
        elif "/embed/" in parsed.path:
            vid_id = parsed.path.split("/embed/")[1].split("/")[0]
        else:
            vid_id = parse_qs(parsed.query).get("v", [None])[0]
        if vid_id:
            vid_id = _RE_YT_ID_SANITIZE.sub("", vid_id)
            return _iframe_result(
                "youtube",
                f"https://www.youtube.com/embed/{vid_id}?rel=0&modestbranding=1",
                f"https://www.youtube.com/watch?v={vid_id}",
                f"https://img.youtube.com/vi/{vid_id}/hqdefault.jpg",
            )

    # Vimeo
    if hostname in _VIMEO_HOSTS:
        vid_id = (parsed.path.split("/video/")[1].split("/")[0]
                  if "/video/" in parsed.path else parsed.path.lstrip("/").split("/")[0])
        vid_id = _RE_VIMEO_DIGITS_ONLY.sub("", vid_id)
        if vid_id:
            return _iframe_result(
                "vimeo",
                f"https://player.vimeo.com/video/{vid_id}?badge=0&autopause=0",
                f"https://vimeo.com/{vid_id}",
            )

    # Dailymotion
    if hostname in _DAILYMOTION_HOSTS:
        if hostname == "dai.ly":
            vid_id = parsed.path.lstrip("/").split("/")[0]
        elif "/video/" in parsed.path:
            vid_id = parsed.path.split("/video/")[1].split("_")[0].split("/")[0]
        else:
            vid_id = parsed.path.lstrip("/").split("/")[0]
        vid_id = _RE_DAILYMOTION_SANITIZE.sub("", vid_id)
        if vid_id:
            return _iframe_result(
                "dailymotion",
                f"https://www.dailymotion.com/embed/video/{vid_id}",
                f"https://www.dailymotion.com/video/{vid_id}",
                f"https://www.dailymotion.com/thumbnail/video/{vid_id}",
            )

    # Rumble
    if hostname == "rumble.com":
        m = _RE_RUMBLE_EMBED_ID.search(url) or _RE_RUMBLE_GENERIC_ID.search(url)
        if m:
            return _iframe_result("rumble", f"https://rumble.com/embed/{m.group(1)}/", url)

    # Streamable
    if hostname == "streamable.com":
        vid_id = parsed.path.lstrip("/").split("/")[0]
        if vid_id:
            return _iframe_result("streamable", f"https://streamable.com/e/{vid_id}", url)

    # Twitch
    if hostname in _TWITCH_HOSTS:
        if "/clip/" in parsed.path or hostname == "clips.twitch.tv":
            clip_id = parsed.path.lstrip("/").split("/")[-1]
            return _iframe_result(
                "twitch_clip",
                f"https://clips.twitch.tv/embed?clip={clip_id}&parent=localhost",
                url,
            )
        channel = parsed.path.lstrip("/").split("/")[0]
        return _iframe_result(
            "twitch",
            f"https://player.twitch.tv/?channel={channel}&parent=localhost",
            url,
        )

    # Facebook
    if hostname in _FACEBOOK_HOSTS:
        if "/plugins/video.php" in parsed.path or "/plugins/post.php" in parsed.path:
            inner = parse_qs(parsed.query).get("href", [None])[0]
            if inner:
                url = inner
                parsed = urlparse(url)
        resolved = _resolve_short_link(url) if (resolve_short_links and "/share/" in parsed.path) else url
        return _iframe_result(
            "facebook",
            f"https://www.facebook.com/plugins/video.php?href={quote(resolved, safe='')}&show_text=false&width=560",
            resolved,
        )

    # TikTok (vm./vt. short links must be resolved server-side first)
    if hostname in _TIKTOK_HOSTS:
        resolve_url = url
        if hostname in _TIKTOK_SHORT_HOSTS and resolve_short_links:
            resolve_url = _resolve_short_link(url)
        m = _RE_TIKTOK_VIDEO_ID.search(urlparse(resolve_url).path)
        if m:
            return _iframe_result("tiktok", f"https://www.tiktok.com/embed/v2/{m.group(1)}", resolve_url)

    # Twitter / X (t.co is always a short link and must be resolved first)
    if hostname in _TWITTER_HOSTS:
        resolve_url = url
        if hostname == "t.co" and resolve_short_links:
            resolve_url = _resolve_short_link(url)
        m = _RE_TWITTER_STATUS_ID.search(urlparse(resolve_url).path)
        if m:
            return _iframe_result(
                "twitter",
                f"https://platform.twitter.com/embed/Tweet.html?id={m.group(1)}",
                resolve_url,
            )

    # Direct video file
    if any(parsed.path.lower().endswith(ext) for ext in _VIDEO_EXTS):
        ext = parsed.path.lower().rsplit(".", 1)[-1]
        return _video_result("direct", url, mime_type=_VIDEO_MIME_MAP.get(ext, "video/mp4"))

    # Unknown — type='video' (not 'iframe'). A URL reaching here that came
    # from a <video>/<source> tag IS direct media even without a
    # recognizable extension (signed CDN links, tokenized URLs). Treating
    # it as an iframe would make the browser silently download the raw
    # response with zero clicks; a <video> tag just fails gracefully if
    # this really is an unembeddable page.
    return _video_result("unknown", url)


def is_recognized_platform(info: dict) -> bool:
    return bool(info) and info.get("platform") not in (None, "unknown")