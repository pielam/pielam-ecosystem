# megamind/utils/video_info.py
"""
Shared video-URL normalizer.

Turns any raw video URL — scraped or user-entered, doesn't matter — into
a platform-normalized dict the frontend can render as either an <iframe>
embed or a native <video> tag:

    { platform, embed_url, watch_url, thumbnail, type, mime_type?, embeddable? }

Used by both discovery_engine.py (product cards / video_info field) and
personal_engine.py (public Engine feed) so a video looks and plays
identically everywhere in the app, regardless of where the URL came from.

Also exposes resolve_post_video() — the single place that guarantees
every feed post has a playable video slot, real or placeholder. Both
feed pipelines (megamind.utils.feed_cache.refresh_feed_cache for the
public/engine feed, and apps.ponno.views.home._serialize_service for
the global feed) call this instead of each rolling their own fallback
logic.

Supported platforms: YouTube, Vimeo, Dailymotion, Rumble, Streamable,
Twitch (channel + clip), Facebook, TikTok, Twitter/X, and any direct
video file (.mp4, .webm, .ogg, .mov, .m4v, .mkv, .avi).

get_video_info() always returns a dict (never None / raises).
Empty URL → {}.

resolve_post_video() always returns a non-empty dict — it never
returns {}, so callers can render a player unconditionally.

IMPORTANT — why the 'unknown' fallback is type='video', not 'iframe':
Every caller that feeds a URL through here from `extracted_videos`
(apps.ponno.views.home._normalize_videos,
megamind.utils.feed_cache._normalize_videos) only ever got that URL
because the scraper found it inside a raw <video>/<source> tag (see
megamind.utils.service_fetcher._handle_html) — structurally, that
means it's ALWAYS a direct media resource, never a webpage, even when
the URL itself doesn't have a recognizable file extension (signed
CDN/S3 links, tokenized storage URLs, etc. often don't). Defaulting
such URLs to type='iframe' means the frontend wraps a raw binary/video
response in an <iframe src="...">; the browser can't render that
inline and instead silently triggers a file download the instant the
card renders — with zero click, on every page load. Defaulting to
type='video' is safe either way: a real video file plays normally,
and on the rare case this URL genuinely is an unembeddable page, the
<video> tag just fails gracefully and shows the existing "Watch it
here" link instead of forcing an unwanted download.

IMPORTANT — Facebook oEmbed confirmation was REMOVED (see history below):
An earlier version of this module verified Facebook embeddability via
Facebook's oEmbed Video API (graph.facebook.com/.../oembed_video)
before returning an iframe, because Facebook's plugins/video.php embed
gives ZERO JS-visible signal on failure (private post, embedding
disabled by the poster, region lock — the iframe just renders blank,
no error event, nothing the frontend can react to).

That approach was abandoned because Facebook's oEmbed Read feature
requires App Review approval before it works for content posted by
anyone other than the app's own admins/developers/testers:

    (#10) To use 'Meta oEmbed Read', your use of this endpoint must
    be reviewed and approved by Facebook.

Since arbitrary users' videos on this platform aren't posted by the
app's own registered testers, every real-world call failed with this
error regardless of valid FACEBOOK_APP_ID/FACEBOOK_APP_SECRET
credentials — making the check useless without a completed (slow,
non-guaranteed) Facebook App Review submission.

Current behavior: Facebook now behaves like every other platform here
— we always attempt the iframe embed and never call out to Facebook's
API at parse time. The accepted tradeoff is that a private/region-
locked/embedding-disabled video will render a blank iframe with no
JS-visible error, instead of being caught up front and swapped for a
"Watch on Facebook" card. `embeddable` is intentionally never set on
the returned dict anymore — callers should continue to treat a
missing `embeddable` key as "assume True" (this was already the
contract for every non-Facebook platform).

As a partial mitigation, home.html keeps 'facebook' in its
SILENT_FAIL_PLATFORMS set, so a small "Watch on Facebook ↗" link is
still rendered underneath the iframe as a manual fallback the viewer
can use if the embed itself renders blank.

If you later complete Facebook's App Review process for oEmbed Read,
the removed verification logic can be reinstated — see git history for
this file, or the `_facebook_oembed()` / `_resolve_facebook_share_link()`
implementations from before this change.

── PRODUCTION FIX (short/share-link resolution) ────────────────────
Three platforms here use *short/share links* that aren't themselves
parseable into a video ID — they're redirectors that must be
followed server-side first to reach the canonical URL:

  - Facebook: facebook.com/share/v/<id>/, /share/p/, /share/r/
  - TikTok:   vm.tiktok.com/<code>/, vt.tiktok.com/<code>/
  - Twitter/X: t.co/<code>

All three are resolved through the same shared `_resolve_redirect()`
helper, which makes a *server-side* outbound HTTP request to follow
the redirect chain. This works locally (unrestricted dev-machine
egress) but commonly fails in production for two environment-specific
reasons that have nothing to do with the parsing logic itself:

  1. Outbound firewall / egress allowlist. Many production hosts
     (k8s clusters, PaaS, corporate networks) only permit outbound
     traffic to an explicit domain allowlist. If the target domain
     (facebook.com, tiktok.com, t.co, ...) isn't on it, every request
     times out or is refused at the network layer.
  2. These platforms actively rate-limit / block requests coming from
     known datacenter/cloud IP ranges (AWS, GCP, Azure, DigitalOcean,
     etc.) as an anti-scraping measure, while a residential dev IP
     is unaffected.

Previously (Facebook only) this failure mode was completely silent:
`except requests.RequestException: pass` swallowed the real error and
fell back to the *original, unresolved* short URL, which none of
these platforms' embed mechanisms can use directly — so production
rendered a broken/blank embed with nothing in the logs to explain
why, while localhost worked fine. TikTok/Twitter short links weren't
resolved at all before this fix — they silently fell through to a
broken or generic-fallback embed.

This version, for all three platforms:
  - Logs every resolution failure (with exception detail and which
    platform/URL it was) instead of silently passing, so the actual
    cause (timeout vs. connection refused vs. blocked) is visible in
    production logs.
  - Retries transient failures with backoff before giving up.
  - Supports routing resolution requests through a proxy so
    datacenter-IP blocking can be worked around with a residential/
    rotating exit IP, without proxying any other outbound traffic in
    the app.
  - Still fails open (returns the original URL / skips enrichment) if
    every retry is exhausted, so an outage on any one platform never
    breaks the surrounding fetch/sync pipeline — it's just no longer
    silent about it.

Config (all optional, generic ones apply to every platform, and can
be overridden per-platform):
    settings.VIDEO_RESOLVE_TIMEOUT / _MAX_RETRIES / _BACKOFF_FACTOR / _PROXY
    settings.FACEBOOK_RESOLVE_TIMEOUT / _MAX_RETRIES / _BACKOFF_FACTOR / _PROXY
    settings.TIKTOK_RESOLVE_TIMEOUT   / _MAX_RETRIES / _BACKOFF_FACTOR / _PROXY
    settings.TWITTER_RESOLVE_TIMEOUT  / _MAX_RETRIES / _BACKOFF_FACTOR / _PROXY
"""

import hashlib
import logging
import re
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from urllib.parse import parse_qs, urlparse, quote
from typing import Optional

from django.conf import settings
from django.core.cache import cache

logger = logging.getLogger(__name__)

_RESOLVE_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
}

# ── Generic defaults, applied to every redirect-resolving platform
# unless a platform-specific override is set. ──────────────────────
_GENERIC_RESOLVE_TIMEOUT        = getattr(settings, 'VIDEO_RESOLVE_TIMEOUT', 8)
_GENERIC_RESOLVE_MAX_RETRIES    = getattr(settings, 'VIDEO_RESOLVE_MAX_RETRIES', 2)
_GENERIC_RESOLVE_BACKOFF_FACTOR = getattr(settings, 'VIDEO_RESOLVE_BACKOFF_FACTOR', 0.5)
_GENERIC_RESOLVE_PROXY_SETTING  = getattr(settings, 'VIDEO_RESOLVE_PROXY', None)


def _normalize_proxy_setting(value) -> Optional[dict]:
    """
    Accepts a plain proxy URL string, a requests-style {"http":...,
    "https":...} dict, or None. Returns a dict suitable for
    requests' `proxies=` kwarg, or None.
    """
    if isinstance(value, str):
        return {'http': value, 'https': value}
    if isinstance(value, dict):
        return value
    return None


def _platform_resolve_config(platform: str, default_timeout, default_retries, default_backoff, default_proxy):
    """
    Builds (timeout, max_retries, backoff_factor, proxies) for one
    platform, letting settings.<PLATFORM>_RESOLVE_* override the
    generic settings.VIDEO_RESOLVE_* defaults passed in.
    """
    prefix = platform.upper()
    timeout   = getattr(settings, f'{prefix}_RESOLVE_TIMEOUT', default_timeout)
    retries   = getattr(settings, f'{prefix}_RESOLVE_MAX_RETRIES', default_retries)
    backoff   = getattr(settings, f'{prefix}_RESOLVE_BACKOFF_FACTOR', default_backoff)
    proxy_raw = getattr(settings, f'{prefix}_RESOLVE_PROXY', None)
    proxies   = _normalize_proxy_setting(proxy_raw) or default_proxy
    return timeout, retries, backoff, proxies


# Optional: route resolution requests through a proxy. Set
# settings.VIDEO_RESOLVE_PROXY (applies to all platforms) and/or
# settings.FACEBOOK_RESOLVE_PROXY / TIKTOK_RESOLVE_PROXY /
# TWITTER_RESOLVE_PROXY (per-platform override) to
# "http://user:pass@host:port" (or a {"http":..., "https":...} dict)
# if production requests to that platform are being blocked/
# rate-limited by IP (common for datacenter/cloud hosting ranges).
# Left unset by default — most deployments only need the egress
# allowlist fix, not a proxy.
_GENERIC_RESOLVE_PROXIES = _normalize_proxy_setting(_GENERIC_RESOLVE_PROXY_SETTING)

_FB_SHARE_RESOLVE_TIMEOUT, _FB_RESOLVE_MAX_RETRIES, _FB_RESOLVE_BACKOFF_FACTOR, _FB_RESOLVE_PROXIES = (
    _platform_resolve_config('facebook', _GENERIC_RESOLVE_TIMEOUT, _GENERIC_RESOLVE_MAX_RETRIES,
                              _GENERIC_RESOLVE_BACKOFF_FACTOR, _GENERIC_RESOLVE_PROXIES)
)
_TIKTOK_RESOLVE_TIMEOUT, _TIKTOK_RESOLVE_MAX_RETRIES, _TIKTOK_RESOLVE_BACKOFF_FACTOR, _TIKTOK_RESOLVE_PROXIES = (
    _platform_resolve_config('tiktok', _GENERIC_RESOLVE_TIMEOUT, _GENERIC_RESOLVE_MAX_RETRIES,
                              _GENERIC_RESOLVE_BACKOFF_FACTOR, _GENERIC_RESOLVE_PROXIES)
)
_TWITTER_RESOLVE_TIMEOUT, _TWITTER_RESOLVE_MAX_RETRIES, _TWITTER_RESOLVE_BACKOFF_FACTOR, _TWITTER_RESOLVE_PROXIES = (
    _platform_resolve_config('twitter', _GENERIC_RESOLVE_TIMEOUT, _GENERIC_RESOLVE_MAX_RETRIES,
                              _GENERIC_RESOLVE_BACKOFF_FACTOR, _GENERIC_RESOLVE_PROXIES)
)


def _build_resolve_session(max_retries: int, backoff_factor: float) -> requests.Session:
    """
    Session with retry/backoff for transient network failures
    (timeouts, connection resets, 5xx from the target platform's
    edge). Retrying at the transport layer catches the common "prod
    egress is flaky, not fully blocked" case without slowing down the
    happy path. Shared by every platform that needs short-link
    resolution (Facebook, TikTok, Twitter/X).
    """
    session = requests.Session()
    retry = Retry(
        total=max_retries,
        backoff_factor=backoff_factor,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset(['GET']),
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount('https://', adapter)
    session.mount('http://', adapter)
    return session


def _resolve_redirect(
    url: str,
    *,
    platform: str,
    timeout: float,
    max_retries: int,
    backoff_factor: float,
    proxies: Optional[dict],
) -> str:
    """
    Generic short-link → canonical-URL resolver, shared by Facebook
    share links, TikTok vm.tiktok.com/vt.tiktok.com short links, and
    Twitter/X t.co short links. Follows the redirect chain server-side
    and returns the URL the platform ultimately lands on, stripped of
    the query string (redirect-tracking params add noise without
    adding embeddability/ID-extractability).

    Fails open: any network error, timeout, or non-redirect just
    returns the original URL unchanged — but logs why, with the
    platform name included, and retries transient failures first.
    See the module docstring's "PRODUCTION FIX" section: this is the
    function responsible for "works on localhost, fails in
    production" symptoms on any of these three platforms.
    """
    session = _build_resolve_session(max_retries, backoff_factor)
    try:
        resp = session.get(
            url,
            headers=_RESOLVE_HEADERS,
            allow_redirects=True,
            timeout=timeout,
            stream=True,
            proxies=proxies,
        )
        resolved = resp.url
        resp.close()
        if resolved and resolved != url:
            parsed_resolved = urlparse(resolved)
            return f'{parsed_resolved.scheme}://{parsed_resolved.netloc}{parsed_resolved.path}'
        logger.warning(
            "%s short-link resolution returned no redirect for %s "
            "(response landed on the same URL) — embed will likely fail",
            platform, url,
        )
    except requests.Timeout:
        logger.warning(
            "%s short-link resolution timed out after %ss for %s. "
            "This is the most common production-only failure mode: the "
            "outbound request is being dropped/blocked by a firewall or "
            "egress allowlist that doesn't exist on localhost. Check "
            "that this platform's domain is allowlisted for outbound "
            "traffic from this environment.",
            platform, timeout, url,
        )
    except requests.ConnectionError as exc:
        logger.warning(
            "%s short-link resolution failed to connect for %s: %s. "
            "Likely causes: egress firewall blocking this domain, DNS "
            "resolution failure in this environment, or the platform "
            "rate-limiting/blocking this server's IP range (common for "
            "datacenter/cloud IPs). Consider setting a *_RESOLVE_PROXY "
            "setting for this platform if IP-blocking is confirmed.",
            platform, url, exc,
        )
    except requests.RequestException as exc:
        logger.warning(
            "%s short-link resolution failed for %s: %s", platform, url, exc,
        )
    finally:
        session.close()
    return url


# How long to cache a parsed video_info dict per URL.
# URLs are stable (they come from the DB), so a long TTL is fine.
_VIDEO_INFO_TTL = getattr(settings, 'DE_VIDEO_INFO_TTL', 3600)

# Domain to send as Twitch's `parent` embed param. Twitch requires this
# to be the domain the player is actually embedded on (not twitch.tv
# itself, and not the streamer's channel name) or the embed silently
# refuses to load. Configure via settings.SITE_DOMAIN in production;
# falls back to a placeholder that at least won't be twitch.tv's own host.
_EMBED_PARENT_DOMAIN = getattr(settings, 'SITE_DOMAIN', None) or 'localhost'


def _resolve_facebook_share_link(url: str) -> str:
    """
    Facebook's newer share-link format (facebook.com/share/v/<id>/,
    /share/p/, /share/r/) is a redirector, not a canonical video URL —
    the video.php embed plugin can't resolve it directly and falls
    back to rendering a plain link card instead of a player.

    Thin wrapper around the shared `_resolve_redirect()` — see that
    function and the module docstring's "PRODUCTION FIX" section for
    the full rationale/config options.
    """
    return _resolve_redirect(
        url,
        platform='facebook',
        timeout=_FB_SHARE_RESOLVE_TIMEOUT,
        max_retries=_FB_RESOLVE_MAX_RETRIES,
        backoff_factor=_FB_RESOLVE_BACKOFF_FACTOR,
        proxies=_FB_RESOLVE_PROXIES,
    )


def _resolve_tiktok_short_link(url: str) -> str:
    """
    TikTok's short-link domains (vm.tiktok.com/<code>/,
    vt.tiktok.com/<code>/) are redirectors to the canonical
    tiktok.com/@user/video/<id> URL that the embed API actually needs
    — the short code itself isn't parseable into a video ID.

    Thin wrapper around the shared `_resolve_redirect()`.
    """
    return _resolve_redirect(
        url,
        platform='tiktok',
        timeout=_TIKTOK_RESOLVE_TIMEOUT,
        max_retries=_TIKTOK_RESOLVE_MAX_RETRIES,
        backoff_factor=_TIKTOK_RESOLVE_BACKOFF_FACTOR,
        proxies=_TIKTOK_RESOLVE_PROXIES,
    )


def _resolve_twitter_short_link(url: str) -> str:
    """
    Twitter/X's t.co link shortener wraps every tweet URL regardless
    of where it's posted — t.co/<code> is a redirector, not a
    canonical status URL, so the tweet ID can't be parsed from it
    directly.

    Thin wrapper around the shared `_resolve_redirect()`.
    """
    return _resolve_redirect(
        url,
        platform='twitter',
        timeout=_TWITTER_RESOLVE_TIMEOUT,
        max_retries=_TWITTER_RESOLVE_MAX_RETRIES,
        backoff_factor=_TWITTER_RESOLVE_BACKOFF_FACTOR,
        proxies=_TWITTER_RESOLVE_PROXIES,
    )


def get_video_info(url: str) -> dict:
    """
    Parse a raw video URL and return a platform-normalised dict that
    the template can use to render an <iframe> or <video> element.

    Returned keys
    -------------
    platform    str   youtube | vimeo | dailymotion | rumble | streamable |
                      twitch | twitch_clip | facebook | tiktok | twitter |
                      direct | unknown
    embed_url   str   URL suitable for an <iframe src="...">
    watch_url   str   Human-facing watch/share link
    thumbnail   str   Preview image URL (empty string when unavailable)
    type        str   "iframe" | "video"  — tells the template which tag to use
    mime_type   str   Only present when type == "video"
    embeddable  bool  No longer set for any platform (Facebook's oEmbed
                      confirmation was removed — see module docstring).
                      Kept documented here because callers/templates
                      still check for it defensively; a missing key
                      should always be treated as "assume True".
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
                'embed_url': f'https://clips.twitch.tv/embed?clip={clip_id}&parent={_EMBED_PARENT_DOMAIN}',
                'watch_url': url,
                'thumbnail': '',
                'type':      'iframe',
            }
        channel = parsed.path.lstrip('/').split('/')[0]
        return {
            'platform':  'twitch',
            'embed_url': f'https://player.twitch.tv/?channel={channel}&parent={_EMBED_PARENT_DOMAIN}',
            'watch_url': url,
            'thumbnail': '',
            'type':      'iframe',
        }

    # ── Facebook ──────────────────────────────────────────────────
    # NOTE: oEmbed confirmation was removed. Facebook's oEmbed Read
    # feature requires App Review approval before it works for content
    # posted by anyone other than the app's own admins/developers/
    # testers (error code #10: "your use of this endpoint must be
    # reviewed and approved by Facebook") — unusable for arbitrary
    # user-submitted videos without going through that process.
    #
    # We now always attempt the iframe embed, same as every other
    # platform. Known tradeoff: a private/region-locked/embedding-
    # disabled video will render a blank iframe with no JS-visible
    # error (see module docstring) instead of falling back to a
    # "Watch on Facebook" card. `embeddable` is left unset (frontend
    # treats a missing key as "assume True"), so home.html's
    # videoTagHTML() renders the iframe directly. Facebook stays in
    # home.html's SILENT_FAIL_PLATFORMS set, so a "Watch on Facebook ↗"
    # link still renders underneath the iframe as a manual fallback.
    if hostname in ('facebook.com', 'fb.watch', 'fb.com'):
        # Unwrap plugin/embed-wrapper URLs FIRST. A video captured via
        # its <iframe src="..."> — the normal way sites embed Facebook
        # video, per megamind.utils.service_fetcher._handle_html — is
        # already a facebook.com/plugins/video.php?href=<real_url>
        # wrapper, not a watch URL. Every other platform's iframe src
        # is directly parseable (e.g. YouTube's '/embed/VIDEO_ID'
        # check above), but Facebook's plugin format wraps the real
        # URL in a query param instead. Without unwrapping here first,
        # the code below re-wraps an already-wrapped URL, producing a
        # broken double-nested href=href=... link that the video.php
        # plugin can't resolve.
        if '/plugins/video.php' in parsed.path or '/plugins/post.php' in parsed.path:
            inner_href = parse_qs(parsed.query).get('href', [None])[0]
            if inner_href:
                url = inner_href
                parsed = urlparse(url)

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
    # vm.tiktok.com / vt.tiktok.com are short-link redirectors — the
    # code in the path (e.g. vm.tiktok.com/ZM8xxxxxx/) doesn't encode
    # a video ID and must be resolved server-side to the canonical
    # tiktok.com/@user/video/<id> URL first. Without this, these short
    # links previously fell straight through to the generic 'unknown'
    # fallback below (no /video/(\d+) to match), so the huge share of
    # TikTok links copied from the app's native "Share" button — which
    # always produces a vm.tiktok.com short link, not a full URL —
    # never embedded at all. See module docstring's "PRODUCTION FIX"
    # section: resolution can fail in production specifically due to
    # egress/IP-blocking, same as Facebook.
    if hostname in ('tiktok.com', 'vm.tiktok.com', 'vt.tiktok.com'):
        resolve_url = url
        if hostname in ('vm.tiktok.com', 'vt.tiktok.com'):
            resolve_url = _resolve_tiktok_short_link(url)
        m = re.search(r'/video/(\d+)', urlparse(resolve_url).path)
        if m:
            return {
                'platform':  'tiktok',
                'embed_url': f'https://www.tiktok.com/embed/v2/{m.group(1)}',
                'watch_url': resolve_url,
                'thumbnail': '',
                'type':      'iframe',
            }
        # Resolution failed or landed somewhere unexpected (deleted
        # video, region lock, etc.) — fall through to 'unknown' below
        # rather than returning a broken embed with no video ID.

    # ── Twitter / X ───────────────────────────────────────────────
    # t.co is Twitter/X's own link shortener and wraps EVERY tweet URL
    # regardless of where it was copied from — the short code alone
    # doesn't contain a status/tweet ID, so it must be resolved
    # server-side to the canonical twitter.com|x.com/<user>/status/<id>
    # URL first (same redirector problem as Facebook share links and
    # TikTok short links — see module docstring's "PRODUCTION FIX").
    #
    # Previously this branch always returned an embed built from
    # `parsed.path.split("/")[-1]` regardless of whether that segment
    # was actually a numeric status ID — for a bare profile URL
    # (twitter.com/someuser) or an unresolved t.co link, that produced
    # a Tweet.html embed with a garbage/non-numeric id that can't
    # render. Now the status ID is extracted with a regex and the
    # embed is only returned when a real ID was found; otherwise this
    # falls through to the generic fallback instead of returning a
    # guaranteed-broken embed.
    if hostname in ('twitter.com', 'x.com', 't.co'):
        resolve_url = url
        if hostname == 't.co':
            resolve_url = _resolve_twitter_short_link(url)
        m = re.search(r'/status(?:es)?/(\d+)', urlparse(resolve_url).path)
        if m:
            return {
                'platform':  'twitter',
                'embed_url': f'https://platform.twitter.com/embed/Tweet.html?id={m.group(1)}',
                'watch_url': resolve_url,
                'thumbnail': '',
                'type':      'iframe',
            }
        # No status ID found (profile URL, resolution failed, etc.) —
        # fall through to 'unknown' below rather than returning a
        # broken embed.

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
    # type='video', NOT 'iframe' — see module docstring. Every caller
    # that reaches this branch via extracted_videos already knows
    # (structurally, from where the scraper found the URL) that this
    # is a direct media resource, just one without a recognizable file
    # extension (signed/tokenized CDN links, etc). Treating it as an
    # <iframe> instead causes the browser to auto-download the raw
    # response the instant the card renders, with no user click and
    # no error shown — that's worse than a <video> tag failing
    # gracefully in the rare case this URL really is an unembeddable
    # page.
    return {
        'platform':  'unknown',
        'embed_url': url,
        'watch_url': url,
        'thumbnail': '',
        'type':      'video',
        'mime_type': 'video/mp4',
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


_OEMBED_TTL = getattr(settings, 'YT_OEMBED_TTL', 86400)


def get_youtube_oembed_info(url: str) -> dict:
    """
    Fetch real title/channel-name/thumbnail for a YouTube URL via
    YouTube's public oEmbed endpoint (no API key needed). Returns
    {'title', 'author_name', 'author_url', 'thumbnail_url'}.

    IMPORTANT: oEmbed does NOT include a channel avatar/profile photo
    — only the channel's display name and URL. Callers must render a
    text/initials placeholder for the avatar, not assume a real photo
    is available. A real avatar would require YouTube Data API v3
    (API key + channels.list by channel ID) — not implemented here.

    Fails open: returns {} on any error (private/deleted video,
    network failure, non-JSON response, etc), same convention as the
    rest of this module.
    """
    if not url:
        return {}

    cache_key = 'de:yt_oembed:' + hashlib.blake2b(url.encode(), digest_size=10).hexdigest()
    try:
        cached = cache.get(cache_key)
        if cached is not None:
            return cached
    except Exception:
        pass

    try:
        resp = requests.get(
            'https://www.youtube.com/oembed',
            params={'url': url, 'format': 'json'},
            headers=_RESOLVE_HEADERS,
            timeout=_GENERIC_RESOLVE_TIMEOUT,
        )
        if resp.status_code != 200:
            logger.warning("YouTube oEmbed returned %s for %s", resp.status_code, url)
            return {}
        data = resp.json()
    except requests.RequestException as exc:
        logger.warning("YouTube oEmbed request failed for %s: %s", url, exc)
        return {}
    except ValueError:
        logger.warning("YouTube oEmbed returned non-JSON for %s", url)
        return {}

    info = {
        'title':         data.get('title', ''),
        'author_name':   data.get('author_name', ''),
        'author_url':    data.get('author_url', ''),
        'thumbnail_url': data.get('thumbnail_url', ''),
    }
    try:
        cache.set(cache_key, info, _OEMBED_TTL)
    except Exception:
        pass
    return info
# ════════════════════════════════════════════════════════════════════
# GUARANTEED PER-POST VIDEO RESOLUTION
# ════════════════════════════════════════════════════════════════════
#
# Feed posts must always have a playable video slot — either a real
# extracted one, or a themed placeholder. resolve_post_video() is the
# single place that decides which; both feed pipelines
# (feed_cache.refresh_feed_cache for the public/engine feed, and
# home._serialize_service for the global feed) call this instead of
# each rolling their own fallback logic.

# Swap these for your own hosted clips (short, muted-friendly, looping)
# rather than depending on a third-party CDN for fallback content across
# the whole platform. Keyed by ConnectedService.service_type; 'default'
# is used for any type without its own pool. Each entry carries an
# explicit thumbnail because direct video files don't yield one on
# their own (get_video_info returns thumbnail='' for type=='direct'),
# and the feed card's cover image needs something to show before play.
# Override entirely via settings.DE_FALLBACK_VIDEOS if you want to
# swap in self-hosted assets without touching this file.
_DEFAULT_FALLBACK_VIDEOS = {
    'product': [
        {
            'url': 'https://assets.mixkit.co/videos/preview/mixkit-hands-of-a-woman-wrapping-a-gift-box-4842-large.mp4',
            'thumbnail': 'https://assets.mixkit.co/videos/4842/4842-0.jpg',
        },
        {
            'url': 'https://assets.mixkit.co/videos/preview/mixkit-set-of-plates-with-cutlery-on-a-wooden-table-42678-large.mp4',
            'thumbnail': 'https://assets.mixkit.co/videos/42678/42678-0.jpg',
        },
    ],
    'brand': [
        {
            'url': 'https://assets.mixkit.co/videos/preview/mixkit-going-through-a-tunnel-of-trees-41537-large.mp4',
            'thumbnail': 'https://assets.mixkit.co/videos/41537/41537-0.jpg',
        },
    ],
    'business': [
        {
            'url': 'https://assets.mixkit.co/videos/preview/mixkit-people-working-in-modern-office-4830-large.mp4',
            'thumbnail': 'https://assets.mixkit.co/videos/4830/4830-0.jpg',
        },
    ],
    'news': [
        {
            'url': 'https://assets.mixkit.co/videos/preview/mixkit-city-traffic-at-night-34575-large.mp4',
            'thumbnail': 'https://assets.mixkit.co/videos/34575/34575-0.jpg',
        },
    ],
    'entertainment': [
        {
            'url': 'https://assets.mixkit.co/videos/preview/mixkit-crowd-at-a-concert-1230-large.mp4',
            'thumbnail': 'https://assets.mixkit.co/videos/1230/1230-0.jpg',
        },
    ],
    'default': [
        {
            'url': 'https://assets.mixkit.co/videos/preview/mixkit-forest-stream-in-the-sunlight-529-large.mp4',
            'thumbnail': 'https://assets.mixkit.co/videos/529/529-0.jpg',
        },
    ],
}

FALLBACK_VIDEOS = getattr(settings, 'DE_FALLBACK_VIDEOS', _DEFAULT_FALLBACK_VIDEOS)


def _pick_fallback_entry(seed: str, service_type: Optional[str]) -> dict:
    pool = FALLBACK_VIDEOS.get(service_type) or FALLBACK_VIDEOS.get('default') or []
    if not pool:
        return {}
    idx = int(hashlib.blake2b((seed or 'default').encode(), digest_size=4).hexdigest(), 16) % len(pool)
    return pool[idx]


def get_fallback_video_info(seed: str, service_type: Optional[str] = None) -> dict:
    """
    Deterministically pick a placeholder clip for `seed` (pass the
    post's stable id, e.g. str(svc.pk)) — same post always gets the
    same placeholder, so it doesn't change on every re-fetch/cache
    miss. Themed by service_type when a pool exists for it.

    Always returns a dict with is_fallback=True. If FALLBACK_VIDEOS
    is somehow empty/misconfigured, returns a minimal valid dict
    rather than {} — resolve_post_video()'s "never empty" guarantee
    depends on this never bottoming out.
    """
    entry = _pick_fallback_entry(seed, service_type)
    url = entry.get('url') if entry else None

    if not url:
        return {
            'platform': 'unknown', 'embed_url': '', 'watch_url': '',
            'thumbnail': '', 'type': 'video', 'is_fallback': True,
        }

    info = dict(get_video_info_cached(url) or {})
    if entry.get('thumbnail'):
        info['thumbnail'] = entry['thumbnail']
    info['is_fallback'] = True
    return info


def resolve_post_video(
    extracted_videos: Optional[list] = None,
    source_url: Optional[str] = None,
    seed: Optional[str] = None,
    service_type: Optional[str] = None,
) -> dict:
    """
    Returns a normalized, GUARANTEED non-empty video_info dict for a
    feed post. Never returns {} — callers can always render a player.

    Priority:
      1. First usable extracted video (scraped media).
      2. The post's own source/service URL, if it happens to resolve
         to a known video platform (not just 'unknown'/generic page).
      3. A deterministic, service-type-themed placeholder.

    `is_fallback` is set False for (1)/(2), True for (3), so the
    frontend can badge placeholder content differently if desired.
    """
    for v in extracted_videos or []:
        url = v.get('url') if isinstance(v, dict) else v
        if not url:
            continue
        info = get_video_info_cached(url)
        if info and info.get('embed_url'):
            info = dict(info)
            info['is_fallback'] = False
            return info

    if source_url:
        info = get_video_info_cached(source_url)
        if info and info.get('platform') != 'unknown' and info.get('embed_url'):
            info = dict(info)
            info['is_fallback'] = False
            return info

    return get_fallback_video_info(seed or source_url or 'default', service_type)