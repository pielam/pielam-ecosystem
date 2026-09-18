"""
megamind/home_feed_algorithm.py
================================
Feed-scoring and ranking engine for the GLOBAL ConnectedService feed
(apps/ponno/views/home.py's HomeEngineView / feed_load_more).

This is a sibling to apps/ponno/feed_algorithm.py, not a wrapper around
it. That module scores Product ORM instances / product dicts — sales,
wishlist counts, discount percentage, brand/category affinity, search
history. None of that has an honest equivalent on ConnectedService or
ProfileInfo, so reusing it would mean every affinity/popularity term
silently evaluating to 0 (dict path) or raising AttributeError (ORM
path). This module scores the fields that actually exist on a
connected-service post instead:

    - recency            (created_at)
    - content richness   (extracted_images / extracted_videos /
                           extracted_links / word_count)
    - poster reach        (ProfileInfo follower count)
    - poster verification (ProfileInfo.is_profile_verified)
    - viewer's follow graph (ProfileInfo.followers / .following)

IMPORTANT — ProfileInfo field names used below (profile_name,
get_profile_photo_url(), profile_tagline, location_display,
is_profile_verified, profile_type, is_profile_public, followers,
following) are inferred from how apps/ponno/views/home.py and
engine/profile_views/engine_profile.py already use ProfileInfo — this
module was written without direct sight of
apps/customer/models/profile_info.py itself. If any of those names are
wrong, the fix is confined to _poster_signals_orm() /
_poster_signals_dict() below; nothing else in this file assumes a
specific ProfileInfo shape.

No search-history / query-affinity term is included: unlike products,
there is no SearchHistory-equivalent model for connected-service posts
in evidence anywhere in this codebase, and fabricating that dependency
would be worse than omitting the term.

Usage (apps/ponno/views/home.py)
─────────────────────────────────
    from megamind.home_feed_algorithm import HomeFeedEngine

    def _personalize_feed(items, viewer_id, viewer=None):
        ...
        if viewer is not None:
            engine = HomeFeedEngine(viewer)
            personalized = engine.rank_dicts(personalized)
        return personalized

All weights are overridable via settings.py (see TUNEABLE WEIGHTS).
"""

from __future__ import annotations

import logging
import math
from collections import defaultdict
from datetime import timedelta
from typing import Any, Dict, List, Optional, Set

from django.conf import settings
from django.core.cache import cache
from django.db.models import Count, Q
from django.utils import timezone

from megamind.models.connected_service import ConnectedService

logger = logging.getLogger(__name__)


# ═════════════════════════════════════════════════════════════════════
# ██  TUNEABLE WEIGHTS  (override in settings.py)
# ═════════════════════════════════════════════════════════════════════

# Content richness — log-scaled so one service with 200 scraped images
# doesn't drown out everything else in the feed.
W_IMAGES  = getattr(settings, 'HOME_FEED_W_IMAGES',  2.0)
W_VIDEOS  = getattr(settings, 'HOME_FEED_W_VIDEOS',  4.0)
W_LINKS   = getattr(settings, 'HOME_FEED_W_LINKS',   0.5)
W_TEXT    = getattr(settings, 'HOME_FEED_W_TEXT',    1.5)  # bonus if has_text

# Poster reach/trust
W_FOLLOWER_REACH = getattr(settings, 'HOME_FEED_W_FOLLOWER_REACH', 3.0)
W_VERIFIED_POSTER = getattr(settings, 'HOME_FEED_W_VERIFIED_POSTER', 6.0)

# Recency
W_RECENCY      = getattr(settings, 'HOME_FEED_W_RECENCY', 30.0)
HALF_LIFE_DAYS = getattr(settings, 'HOME_FEED_HALF_LIFE', 2)  # posts feel "old" fast

# Viewer's own follow graph
W_FOLLOWED_POSTER = getattr(settings, 'HOME_FEED_W_FOLLOWED_POSTER', 25.0)

# Freshness of the underlying scrape — a post whose last crawl errored
# or is stale shouldn't be pushed as hard as one with live, current data.
W_FETCH_SUCCESS_BONUS = getattr(settings, 'HOME_FEED_W_FETCH_SUCCESS_BONUS', 4.0)
W_STALE_PENALTY        = getattr(settings, 'HOME_FEED_W_STALE_PENALTY', -3.0)

DIVERSITY_WINDOW = getattr(settings, 'HOME_FEED_DIVERSITY_WINDOW', 4)
TTL_AFFINITY     = getattr(settings, 'HOME_FEED_TTL_AFFINITY', 300)  # 5 min

_NEW_ARRIVAL_DAYS: int = getattr(settings, 'HOME_FEED_NEW_ARRIVAL_DAYS', 3)


# ═════════════════════════════════════════════════════════════════════
# ██  TAB FILTER MAP  (ConnectedService-native — no product fields)
# ═════════════════════════════════════════════════════════════════════

def get_tab_filter_map() -> Dict[str, Q]:
    """
    Returns a fresh dict each call so timezone.now() is always current.
    Do NOT cache at module level — 'new' uses a live timestamp.

    Built only from fields that exist on ConnectedService — no
    is_trending/is_featured/discount_percentage equivalents exist on
    this model, so tabs are limited to what the schema can actually
    answer.
    """
    return {
        'new':      Q(created_at__gte=timezone.now() - timedelta(days=_NEW_ARRIVAL_DAYS)),
        'video':    Q(extracted_videos__len__gt=0) if _supports_len_lookup() else Q(),
        'verified': Q(user__profileinfo__is_profile_verified=True),
        'public':   Q(status=ConnectedService.Status.PUBLIC),
    }


def _supports_len_lookup() -> bool:
    """
    JSONField's __len lookup needs Postgres + Django's jsonb support.
    Guarded rather than assumed, since get_tab_filter_map() is called
    from arbitrary views/management commands that may run against a
    different backend (e.g. sqlite in tests).
    """
    return getattr(settings, 'DATABASES', {}).get('default', {}).get('ENGINE', '').endswith('postgresql')


TAB_FILTER_NAMES: Dict[str, str] = {
    'new':      'New',
    'video':    'Video',
    'verified': 'Verified Creators',
    'public':   'Public',
}


# ═════════════════════════════════════════════════════════════════════
# ██  VIEWER AFFINITY  (follow graph only — see module docstring)
# ═════════════════════════════════════════════════════════════════════

class ViewerAffinity:
    """
    Typed container for what this module can honestly know about a
    viewer: who they follow. Everything else in feed_algorithm.py's
    AffinityProfile (brand/category scores, search words) has no
    ConnectedService equivalent and is deliberately not reproduced
    here — see module docstring.
    """

    __slots__ = ('followed_user_ids', 'is_anonymous')

    def __init__(self, followed_user_ids: Set[int], is_anonymous: bool = False) -> None:
        self.followed_user_ids = followed_user_ids
        self.is_anonymous = is_anonymous

    @classmethod
    def empty(cls) -> 'ViewerAffinity':
        return cls(set(), is_anonymous=True)

    def follows(self, user_id: Optional[int]) -> bool:
        return user_id in self.followed_user_ids if user_id else False

    def __repr__(self) -> str:
        return f"<ViewerAffinity following={len(self.followed_user_ids)} anon={self.is_anonymous}>"


def load_viewer_affinity(user) -> ViewerAffinity:
    """
    Build (or return cached) follow-graph affinity for a viewer.
    Cached per user PK at TTL_AFFINITY seconds — same pattern as
    apps.ponno.feed_algorithm.load_user_affinity, but there's only one
    signal here so there's no dict-vs-object legacy migration path to
    handle.

    ProfileInfo.followers and ProfileInfo.following (in
    apps/customer/models/profile_info.py) are two INDEPENDENT
    ManyToManyField(User, ...) relations, each with its own
    auto-generated through table — not one relation viewed from two
    directions. follow_profile() keeps both sides in sync (adds to the
    follower's own `following` AND the target's `followers`), but the
    direct, unambiguous way to read "who does this viewer follow" is
    the viewer's own `following` field — the same field
    apps.ponno.feed_algorithm.load_user_affinity already reads
    (`user.profileinfo.following.values_list('pk', flat=True)`).
    """
    if not user or not getattr(user, 'is_authenticated', False):
        return ViewerAffinity.empty()

    cache_key = f'home_feed_aff:{user.pk}'
    cached = cache.get(cache_key)
    if cached is not None:
        return cached

    followed_ids: Set[int] = set()
    try:
        # getattr(..., None) rather than a bare accessor: a user with
        # no ProfileInfo row yet (mid-signup, pre-onboarding) raises
        # ProfileInfo.DoesNotExist on the reverse one-to-one accessor,
        # not AttributeError — degrade to "no follow boost" instead of
        # breaking the whole feed for that viewer.
        profile = getattr(user, 'profileinfo', None)
        if profile is not None:
            followed_ids = set(profile.following.values_list('pk', flat=True))
    except Exception:
        logger.debug('load_viewer_affinity: follow graph lookup failed', exc_info=True)

    affinity = ViewerAffinity(followed_user_ids=followed_ids, is_anonymous=False)
    cache.set(cache_key, affinity, TTL_AFFINITY)
    return affinity


def invalidate_viewer_affinity(user_id: int) -> None:
    """Call after a follow/unfollow action."""
    cache.delete(f'home_feed_aff:{user_id}')


# ═════════════════════════════════════════════════════════════════════
# ██  SCORING PRIMITIVES
# ═════════════════════════════════════════════════════════════════════

def _recency_boost(created_at) -> float:
    if not created_at:
        return 0.0
    age_days = max((timezone.now() - created_at).total_seconds() / 86_400, 0.001)
    return W_RECENCY / (1.0 + age_days / HALF_LIFE_DAYS)


def _content_richness_score(images_count: int, videos_count: int,
                             links_count: int, has_text: bool) -> float:
    return (
        math.log1p(images_count or 0) * W_IMAGES +
        math.log1p(videos_count or 0) * W_VIDEOS +
        math.log1p(links_count  or 0) * W_LINKS +
        (W_TEXT if has_text else 0.0)
    )


def _fetch_health_score(fetch_status: str, is_stale: bool) -> float:
    score = 0.0
    if fetch_status == ConnectedService.FetchStatus.SUCCESS:
        score += W_FETCH_SUCCESS_BONUS
    if is_stale:
        score += W_STALE_PENALTY
    return score


# ── ORM path (List[ConnectedService]) ──────────────────────────────

def _poster_signals_orm(svc: ConnectedService) -> tuple[int, bool]:
    """
    Returns (follower_count, is_verified) for the post's owner.

    Prefers an annotated `follower_count` (see
    apps.ponno.views.home._feed_base_queryset's
    Count('user__profileinfo__followers', distinct=True) annotation)
    over touching svc.user.profileinfo directly, since the latter is
    an N+1 trap when this runs across a whole page of services.
    Falls back to a live profileinfo lookup only if the caller didn't
    annotate — logged so that gap is visible rather than silently slow.
    """
    follower_count = getattr(svc, 'follower_count', None)
    is_verified = False
    profile = getattr(svc.user, 'profileinfo', None)
    if profile is not None:
        is_verified = bool(getattr(profile, 'is_profile_verified', False))
        if follower_count is None:
            logger.debug(
                'home_feed_algorithm: svc %s has no annotated follower_count — '
                'falling back to a live count. Annotate the queryset to avoid N+1.',
                svc.pk,
            )
            follower_count = profile.followers.count()
    return follower_count or 0, is_verified


def _popularity_score_orm(svc: ConnectedService) -> float:
    images = svc.extracted_images or []
    videos = svc.extracted_videos or []
    links = svc.extracted_links or []
    follower_count, _ = _poster_signals_orm(svc)
    return (
        _content_richness_score(len(images), len(videos), len(links), bool(svc.extracted_text))
        + math.log1p(follower_count) * W_FOLLOWER_REACH
    )


def _badge_bonus_orm(svc: ConnectedService) -> float:
    _, is_verified = _poster_signals_orm(svc)
    return W_VERIFIED_POSTER if is_verified else 0.0


def _affinity_boost_orm(svc: ConnectedService, affinity: ViewerAffinity) -> float:
    return W_FOLLOWED_POSTER if affinity.follows(svc.user_id) else 0.0


def _is_stale_orm(svc: ConnectedService, stale_after_seconds: int = 3600) -> bool:
    if svc.last_fetch_time is None:
        return True
    age = (timezone.now() - svc.last_fetch_time).total_seconds()
    return age > stale_after_seconds


def score_services_orm(
    services: List[ConnectedService],
    affinity: Optional[ViewerAffinity] = None,
    *,
    user=None,
) -> List[ConnectedService]:
    """
    Score and sort a list of ConnectedService ORM instances.
    Attaches .feed_score to each instance. Mirrors
    apps.ponno.feed_algorithm.score_products_orm's contract, but for
    ConnectedService fields only.
    """
    if affinity is None:
        affinity = load_viewer_affinity(user) if user else ViewerAffinity.empty()

    for svc in services:
        pop = _popularity_score_orm(svc)
        recency = _recency_boost(svc.created_at)
        aff = _affinity_boost_orm(svc, affinity)
        badge = _badge_bonus_orm(svc)
        health = _fetch_health_score(svc.fetch_status, _is_stale_orm(svc))
        svc.feed_score = round(pop + recency + aff + badge + health, 2)

    services.sort(key=lambda s: s.feed_score, reverse=True)
    return services


# ── Dict path (List[dict] — output of apps.ponno.views.home._serialize_service) ──

def _poster_signals_dict(item: Dict[str, Any]) -> tuple[int, bool]:
    return int(item.get('follower_count') or 0), bool(item.get('profile_verified'))


def _popularity_score_dict(item: Dict[str, Any]) -> float:
    follower_count, _ = _poster_signals_dict(item)
    return (
        _content_richness_score(
            item.get('images_count', 0),
            item.get('videos_count', 0),
            item.get('links_count', 0),
            bool(item.get('has_text')),
        )
        + math.log1p(follower_count) * W_FOLLOWER_REACH
    )


def _badge_bonus_dict(item: Dict[str, Any]) -> float:
    _, is_verified = _poster_signals_dict(item)
    return W_VERIFIED_POSTER if is_verified else 0.0


def _affinity_boost_dict(item: Dict[str, Any], affinity: ViewerAffinity) -> float:
    return W_FOLLOWED_POSTER if affinity.follows(item.get('user_id')) else 0.0


def rank_service_dicts(
    items: List[Dict[str, Any]],
    user=None,
    affinity: Optional[ViewerAffinity] = None,
    apply_diversity: bool = True,
) -> List[Dict[str, Any]]:
    """
    Score and optionally diversity-reorder a list of serialized
    ConnectedService dicts — the exact shape
    apps.ponno.views.home._serialize_service() produces. Requires
    'created_at' (a real datetime, not the isoformat string already on
    the dict as 'posted_at') for recency scoring — the caller should
    stash the raw datetime under 'created_at' before calling this, or
    pass it through unchanged if already present.

    Returns the same dicts, reordered — no new keys are added to the
    dicts themselves (matches apps.ponno.feed_algorithm.rank_and_diversify's
    contract).
    """
    if not items:
        return items

    if affinity is None:
        affinity = load_viewer_affinity(user)

    scored = []
    for item in items:
        score = (
            _popularity_score_dict(item)
            + _recency_boost(item.get('created_at'))
            + _affinity_boost_dict(item, affinity)
            + _badge_bonus_dict(item)
            + _fetch_health_score(item.get('fetch_status'), bool(item.get('is_stale')))
        )
        scored.append((score, item))

    scored.sort(key=lambda t: t[0], reverse=True)
    ranked = [item for _, item in scored]

    if not apply_diversity:
        return ranked
    return _diversity_reorder(ranked)


# ═════════════════════════════════════════════════════════════════════
# ██  DIVERSITY REORDER
# ═════════════════════════════════════════════════════════════════════

def _diversity_reorder(items: List[Any], *, is_orm: bool = False) -> List[Any]:
    """
    Greedy window reorder so the feed doesn't stack several posts from
    the same poster, or several posts of the same service_type, back
    to back. Same algorithm as
    apps.ponno.feed_algorithm._diversity_reorder, keyed on
    (user_id, service_type) instead of (brand, category) since those
    are the fields that actually distinguish one ConnectedService post
    from another.
    """
    result: List[Any] = []
    remaining = list(items)
    recent_posters: List[Any] = []
    recent_types: List[Any] = []

    def _poster(p):
        return p.user_id if is_orm else p.get('user_id')

    def _svc_type(p):
        return p.service_type if is_orm else p.get('svc_type')

    while remaining:
        placed = False
        for idx, p in enumerate(remaining):
            poster = _poster(p)
            svc_type = _svc_type(p)
            if (
                poster not in recent_posters[-DIVERSITY_WINDOW:]
                and svc_type not in recent_types[-DIVERSITY_WINDOW:]
            ):
                result.append(p)
                remaining.pop(idx)
                recent_posters.append(poster)
                recent_types.append(svc_type)
                placed = True
                break
        if not placed:
            # No candidate satisfies the window — same fallback as
            # feed_algorithm.py: append what's left in score order
            # rather than looping forever.
            result.extend(remaining)
            break

    return result


def diversify_service_dicts(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return _diversity_reorder(items, is_orm=False)


def diversify_services_orm(services: List[ConnectedService]) -> List[ConnectedService]:
    return _diversity_reorder(services, is_orm=True)


# ═════════════════════════════════════════════════════════════════════
# ██  FEED ENGINE  (unified builder — mirrors apps.ponno.feed_algorithm.FeedEngine)
# ═════════════════════════════════════════════════════════════════════

class HomeFeedEngine:
    """
    High-level, reusable scoring API for the ConnectedService home
    feed. Build once per request (affinity loaded once from cache),
    then call score_orm()/rank_dicts() as needed.

    Example — apps/ponno/views/home.py::

        from megamind.home_feed_algorithm import HomeFeedEngine

        engine = HomeFeedEngine(request.user)
        ranked = engine.rank_dicts(personalized_items)
    """

    def __init__(self, user=None, affinity: Optional[ViewerAffinity] = None) -> None:
        self.user = user
        self.affinity = affinity if affinity is not None else load_viewer_affinity(user)

    def score_orm(self, services: List[ConnectedService], *, diversify: bool = False) -> List[ConnectedService]:
        scored = score_services_orm(services, affinity=self.affinity)
        if diversify:
            return diversify_services_orm(scored)
        return scored

    def rank_dicts(self, items: List[Dict[str, Any]], *, diversify: bool = True) -> List[Dict[str, Any]]:
        return rank_service_dicts(items, affinity=self.affinity, apply_diversity=diversify)

    def explain_score_dict(self, item: Dict[str, Any]) -> Dict[str, float]:
        """Score breakdown for one serialized post — debugging/admin use."""
        return {
            'popularity':      round(_popularity_score_dict(item), 2),
            'recency':         round(_recency_boost(item.get('created_at')), 2),
            'followed_poster': round(_affinity_boost_dict(item, self.affinity), 2),
            'verified_badge':  round(_badge_bonus_dict(item), 2),
            'fetch_health':    round(_fetch_health_score(item.get('fetch_status'), bool(item.get('is_stale'))), 2),
        }

    def __repr__(self) -> str:
        return f"<HomeFeedEngine user={getattr(self.user, 'pk', None)} affinity={self.affinity!r}>"


# ═════════════════════════════════════════════════════════════════════
# ██  RE-EXPORTS
# ═════════════════════════════════════════════════════════════════════

__all__ = [
    'ViewerAffinity',
    'HomeFeedEngine',
    'load_viewer_affinity',
    'invalidate_viewer_affinity',
    'score_services_orm',
    'rank_service_dicts',
    'diversify_service_dicts',
    'diversify_services_orm',
    'get_tab_filter_map',
    'TAB_FILTER_NAMES',
    'W_IMAGES', 'W_VIDEOS', 'W_LINKS', 'W_TEXT',
    'W_FOLLOWER_REACH', 'W_VERIFIED_POSTER',
    'W_RECENCY', 'HALF_LIFE_DAYS',
    'W_FOLLOWED_POSTER',
    'W_FETCH_SUCCESS_BONUS', 'W_STALE_PENALTY',
    'DIVERSITY_WINDOW', 'TTL_AFFINITY',
]