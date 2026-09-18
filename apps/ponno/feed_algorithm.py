"""
apps/ponno/feed_algorithm.py
============================
Enterprise-grade, view-agnostic feed-scoring and ranking engine.

Usable from any view — HomeView, DiscoveryEngineView, API views,
recommendation endpoints, email digest tasks, Celery workers,
management commands, or server-side rendered widgets.

Architecture
────────────
┌─────────────────────────────────────────────────────────┐
│  load_user_affinity(user)  ──cached──►  AffinityProfile │
│                                                         │
│  score_products_orm(products, affinity)   ←  HomeView   │
│  rank_and_diversify(products, affinity)   ←  Discovery  │
│                                                         │
│  FeedEngine  ──►  unified builder API (any view)        │
└─────────────────────────────────────────────────────────┘

All weights are overridable via settings.py (see TUNEABLE WEIGHTS).

Usage
─────
    # Quick usage in any view (ORM instances):
    from apps.ponno.feed_algorithm import FeedEngine
    engine = FeedEngine(request.user)
    scored = engine.score_orm(products_list)

    # Quick usage (formatted dicts):
    ranked = engine.rank_dicts(product_dicts, diversify=True)

    # Low-level (existing HomeView / DiscoveryEngineView pattern):
    from apps.ponno.feed_algorithm import score_products, rank_and_diversify, load_user_affinity

    # Invalidate affinity cache after purchase / wishlist / follow:
    from apps.ponno.feed_algorithm import invalidate_user_affinity
    invalidate_user_affinity(user.pk)

    # Tab filter map (shared across all views):
    from apps.ponno.feed_algorithm import get_tab_filter_map, TAB_FILTER_NAMES
"""

from __future__ import annotations

import logging
import math
from collections import defaultdict
from datetime import timedelta
from typing import Any, Dict, FrozenSet, Iterable, List, Optional, Sequence, Set

from django.conf import settings
from django.core.cache import cache
from django.db.models import Q
from django.utils import timezone

logger = logging.getLogger(__name__)


# ═════════════════════════════════════════════════════════════════════
# ██  TUNEABLE WEIGHTS  (override in settings.py)
# ═════════════════════════════════════════════════════════════════════

W_VIEWS    = getattr(settings, 'FEED_W_VIEWS',        1.0)
W_SALES    = getattr(settings, 'FEED_W_SALES',       10.0)
W_RATING   = getattr(settings, 'FEED_W_RATING',      20.0)
W_WISHLIST = getattr(settings, 'FEED_W_WISHLIST',     5.0)
W_DISCOUNT = getattr(settings, 'FEED_W_DISCOUNT',     0.5)

W_RECENCY      = getattr(settings, 'FEED_W_RECENCY',  30.0)
HALF_LIFE_DAYS = getattr(settings, 'FEED_HALF_LIFE',   7)

W_BRAND_AFFINITY    = getattr(settings, 'FEED_W_BRAND_AFFINITY',    20.0)
W_CATEGORY_AFFINITY = getattr(settings, 'FEED_W_CATEGORY_AFFINITY', 15.0)
W_SOCIAL_FOLLOW     = getattr(settings, 'FEED_W_SOCIAL_FOLLOW',     12.0)
W_SEARCH_MATCH      = getattr(settings, 'FEED_W_SEARCH_MATCH',      10.0)

DEALER_BOOST = getattr(settings, 'FEED_DEALER_BOOST', 15.0)
CAT_BOOST    = getattr(settings, 'FEED_CAT_BOOST',    10.0)

BONUS_FEATURED = getattr(settings, 'FEED_BONUS_FEATURED', 5.0)
BONUS_VERIFIED = getattr(settings, 'FEED_BONUS_VERIFIED', 3.0)
BONUS_TRENDING = getattr(settings, 'FEED_BONUS_TRENDING', 4.0)

DIVERSITY_WINDOW = getattr(settings, 'FEED_DIVERSITY_WINDOW', 4)
TTL_AFFINITY     = getattr(settings, 'FEED_TTL_AFFINITY',    300)

# New arrival window (days) used by tab filter
_NEW_ARRIVAL_DAYS: int = getattr(settings, 'FEED_NEW_ARRIVAL_DAYS', 7)


# ═════════════════════════════════════════════════════════════════════
# ██  TAB FILTER MAP  (shared across all views)
# ═════════════════════════════════════════════════════════════════════

def get_tab_filter_map() -> Dict[str, Q]:
    """
    Returns a fresh dict each call so timezone.now() is always current.
    Do NOT cache at module level — 'new' uses a live timestamp.
    Safe to call repeatedly; zero DB cost.
    """
    return {
        'trending': Q(is_trending=True),
        'new':      Q(created_at__gte=timezone.now() - timedelta(days=_NEW_ARRIVAL_DAYS)),
        'sale':     Q(discount_percentage__gt=0),
        'featured': Q(is_featured=True),
        'verified': Q(is_verified=True),
        'instock':  Q(stock__gt=0),
    }


TAB_FILTER_NAMES: Dict[str, str] = {
    'trending': 'Trending',
    'new':      'New Arrivals',
    'sale':     'On Sale',
    'featured': 'Featured',
    'verified': 'Verified',
    'instock':  'In Stock',
}

TAB_FILTER_SLUGS: FrozenSet[str] = frozenset(TAB_FILTER_NAMES.keys())


# ═════════════════════════════════════════════════════════════════════
# ██  AFFINITY PROFILE
# ═════════════════════════════════════════════════════════════════════

_AFF_BRAND   = 'brand_scores'
_AFF_CAT     = 'category_scores'
_AFF_DEALERS = 'followed_dealer_ids'
_AFF_SEARCH  = 'search_words'


class AffinityProfile:
    """
    Typed container for a user's affinity signals.

    Attributes
    ──────────
    brand_scores        — {brand_slug: float 0–1}  (normalised)
    category_scores     — {category_slug: float 0–1}  (normalised)
    followed_dealer_ids — set[int]
    search_words        — set[str]  (lowercased, min 3 chars)
    is_anonymous        — True for unauthenticated users
    """

    __slots__ = (
        'brand_scores',
        'category_scores',
        'followed_dealer_ids',
        'search_words',
        'is_anonymous',
    )

    def __init__(
        self,
        brand_scores:        Dict[str, float],
        category_scores:     Dict[str, float],
        followed_dealer_ids: Set[int],
        search_words:        Set[str],
        is_anonymous:        bool = False,
    ) -> None:
        self.brand_scores        = brand_scores
        self.category_scores     = category_scores
        self.followed_dealer_ids = followed_dealer_ids
        self.search_words        = search_words
        self.is_anonymous        = is_anonymous

    @classmethod
    def empty(cls) -> 'AffinityProfile':
        return cls({}, {}, set(), set(), is_anonymous=True)

    def brand_affinity(self, slug: Optional[str]) -> float:
        return self.brand_scores.get(slug or '', 0.0)

    def category_affinity(self, slug: Optional[str]) -> float:
        return self.category_scores.get(slug or '', 0.0)

    def follows_dealer(self, dealer_id: Optional[int]) -> bool:
        return dealer_id in self.followed_dealer_ids if dealer_id else False

    def matches_search(self, haystack: str) -> bool:
        if not self.search_words:
            return False
        hl = haystack.lower()
        return any(w in hl for w in self.search_words)

    def __repr__(self) -> str:
        return (
            f"<AffinityProfile brands={len(self.brand_scores)} "
            f"categories={len(self.category_scores)} "
            f"dealers={len(self.followed_dealer_ids)} "
            f"anon={self.is_anonymous}>"
        )


EMPTY_AFFINITY_DICT: Dict[str, Any] = {
    _AFF_BRAND:   {},
    _AFF_CAT:     {},
    _AFF_DEALERS: set(),
    _AFF_SEARCH:  set(),
}


# ═════════════════════════════════════════════════════════════════════
# ██  AFFINITY LOADER
# ═════════════════════════════════════════════════════════════════════

def load_user_affinity(user) -> AffinityProfile:
    """
    Build (or return cached) affinity signals for a user.

    Returns an AffinityProfile — a typed, immutable-ish object.
    Anonymous users get AffinityProfile.empty() immediately.
    Any individual signal source failing is logged and skipped
    gracefully so the rest of the profile is still built.

    Cached per user PK at TTL_AFFINITY seconds.
    """
    if not user or not getattr(user, 'is_authenticated', False):
        return AffinityProfile.empty()

    cache_key = f'aff:{user.pk}'
    cached = cache.get(cache_key)
    if cached is not None:
        if isinstance(cached, AffinityProfile):
            return cached
        # Legacy dict format — convert and re-cache
        return _dict_to_profile(cached)

    brand_raw:    Dict[str, int] = defaultdict(int)
    category_raw: Dict[str, int] = defaultdict(int)

    # ── a) ProductView history ────────────────────────────────────
    try:
        from apps.ponno.models.product import ProductView
        for row in (
            ProductView.objects
            .filter(user=user)
            .values('view_count',
                    'product__brand__brand_slug',
                    'product__category__category_slug')
        ):
            w  = row['view_count'] or 1
            bs = row['product__brand__brand_slug']
            cs = row['product__category__category_slug']
            if bs: brand_raw[bs]    += w
            if cs: category_raw[cs] += w
    except Exception:
        logger.debug('load_user_affinity: ProductView failed', exc_info=True)

    # ── b) Wishlist history  (weight × 3) ────────────────────────
    try:
        from apps.ponno.models.product import Wishlist
        for row in (
            Wishlist.objects
            .filter(user=user)
            .values('product__brand__brand_slug',
                    'product__category__category_slug')
        ):
            bs = row['product__brand__brand_slug']
            cs = row['product__category__category_slug']
            if bs: brand_raw[bs]    += 3
            if cs: category_raw[cs] += 3
    except Exception:
        logger.debug('load_user_affinity: Wishlist failed', exc_info=True)

    # ── c) Order history  (weight × 5 × qty) ─────────────────────
    try:
        from apps.ponno.models.product import Order
        for order in (
            Order.objects
            .filter(user=user,
                    status__in=['confirmed', 'processing', 'shipped', 'delivered'])
            .prefetch_related('items__product__brand', 'items__product__category')
        ):
            for item in order.items.all():
                p = item.product
                if not p:
                    continue
                qty = item.quantity or 1
                if p.brand and p.brand.brand_slug:
                    brand_raw[p.brand.brand_slug]          += 5 * qty
                if p.category and p.category.category_slug:
                    category_raw[p.category.category_slug] += 5 * qty
    except Exception:
        logger.debug('load_user_affinity: Order failed', exc_info=True)

    # ── d) Normalise to [0, 1] ────────────────────────────────────
    def _norm(raw: Dict[str, int]) -> Dict[str, float]:
        if not raw:
            return {}
        mx = max(raw.values())
        return {} if mx == 0 else {k: round(v / mx, 4) for k, v in raw.items()}

    # ── e) Followed dealers ───────────────────────────────────────
    followed: Set[int] = set()
    try:
        followed = set(user.profileinfo.following.values_list('pk', flat=True))
    except Exception:
        logger.debug('load_user_affinity: following failed', exc_info=True)

    # ── f) Recent search words ────────────────────────────────────
    search_words: Set[str] = set()
    try:
        from apps.ponno.models.product import SearchHistory
        for q in (
            SearchHistory.objects
            .filter(user=user)
            .order_by('-searched_at')
            .values_list('query', flat=True)[:20]
        ):
            for word in q.lower().split():
                if len(word) >= 3:
                    search_words.add(word)
    except Exception:
        logger.debug('load_user_affinity: SearchHistory failed', exc_info=True)

    profile = AffinityProfile(
        brand_scores=_norm(brand_raw),
        category_scores=_norm(category_raw),
        followed_dealer_ids=followed,
        search_words=search_words,
        is_anonymous=False,
    )

    cache.set(cache_key, profile, TTL_AFFINITY)
    return profile


def _dict_to_profile(d: Dict[str, Any]) -> AffinityProfile:
    """Convert legacy dict-format affinity to AffinityProfile."""
    return AffinityProfile(
        brand_scores=d.get(_AFF_BRAND, {}),
        category_scores=d.get(_AFF_CAT, {}),
        followed_dealer_ids=d.get(_AFF_DEALERS, set()),
        search_words=d.get(_AFF_SEARCH, set()),
        is_anonymous=False,
    )


def invalidate_user_affinity(user_id: int) -> None:
    """
    Bust the affinity cache for a user.
    Call after: purchase, wishlist toggle, product view, follow/unfollow.
    """
    cache.delete(f'aff:{user_id}')


# ═════════════════════════════════════════════════════════════════════
# ██  SCORING PRIMITIVES
# ═════════════════════════════════════════════════════════════════════

def _recency_boost(created_at) -> float:
    """Half-life recency decay. Works with both datetime and None."""
    if not created_at:
        return 0.0
    age_days = max((timezone.now() - created_at).total_seconds() / 86_400, 0.001)
    return W_RECENCY / (1.0 + age_days / HALF_LIFE_DAYS)


def _popularity_score_dict(p: Dict[str, Any]) -> float:
    """Popularity component from a formatted product dict."""
    return (
        math.log1p(p.get('views_raw',     0) or 0) * W_VIEWS    +
        math.log1p(p.get('total_sales',   0) or 0) * W_SALES    +
        (p.get('rating',                  0) or 0) * W_RATING   +
        math.log1p(p.get('wishlist_count',0) or 0) * W_WISHLIST +
        (p.get('discount_percentage',     0) or 0) * W_DISCOUNT
    )


def _popularity_score_orm(p) -> float:
    """Popularity component from an ORM Product instance."""
    return (
        float(p.view_count          or 0) * W_VIEWS    +
        float(p.total_sales         or 0) * W_SALES    +
        float(p.rating_average      or 0) * W_RATING   +
        float(p.wishlist_count      or 0) * W_WISHLIST +
        float(p.discount_percentage or 0) * W_DISCOUNT
    )


def _affinity_boost_dict(p: Dict[str, Any], affinity: AffinityProfile) -> float:
    """Affinity boost from a formatted product dict."""
    score  = affinity.brand_affinity(p.get('brand_slug'))    * W_BRAND_AFFINITY
    score += affinity.category_affinity(p.get('category_slug')) * W_CATEGORY_AFFINITY

    dealer_id = p.get('dealer_id')
    if dealer_id and affinity.follows_dealer(dealer_id):
        score += W_SOCIAL_FOLLOW

    haystack = (
        f"{(p.get('product_name') or '').lower()} "
        f"{(p.get('brand')        or '').lower()}"
    )
    if affinity.matches_search(haystack):
        score += W_SEARCH_MATCH

    return score


def _affinity_boost_orm(p, affinity: AffinityProfile) -> float:
    """Affinity boost from an ORM Product instance."""
    brand_slug    = p.brand.brand_slug    if p.brand    else None
    category_slug = p.category.category_slug if p.category else None

    score  = affinity.brand_affinity(brand_slug)    * W_BRAND_AFFINITY
    score += affinity.category_affinity(category_slug) * W_CATEGORY_AFFINITY

    if affinity.follows_dealer(getattr(p, 'dealer_id', None)):
        score += W_SOCIAL_FOLLOW

    haystack = f"{p.product_name or ''} {brand_slug or ''}".lower()
    if affinity.matches_search(haystack):
        score += W_SEARCH_MATCH

    return score


def _badge_bonus_dict(p: Dict[str, Any]) -> float:
    bonus = 0.0
    if p.get('is_featured'): bonus += BONUS_FEATURED
    if p.get('is_verified'): bonus += BONUS_VERIFIED
    if p.get('is_trending'): bonus += BONUS_TRENDING
    return bonus


def _badge_bonus_orm(p) -> float:
    bonus = 0.0
    if getattr(p, 'is_featured', False): bonus += BONUS_FEATURED
    if getattr(p, 'is_verified', False): bonus += BONUS_VERIFIED
    if getattr(p, 'is_trending', False): bonus += BONUS_TRENDING
    return bonus


# ═════════════════════════════════════════════════════════════════════
# ██  SCORE + RANK — FORMATTED DICTS  (DiscoveryEngineView / API)
# ═════════════════════════════════════════════════════════════════════

def rank_and_diversify(
    products:        List[Dict[str, Any]],
    user=None,
    affinity:        Optional[AffinityProfile] = None,
    apply_diversity: bool = True,
) -> List[Dict[str, Any]]:
    """
    Score and optionally diversity-reorder a list of formatted product dicts.

    Args:
        products:         Output of _format_products() or any serialiser.
        user:             Request user (anonymous → no affinity boost).
                          Ignored if *affinity* is passed explicitly.
        affinity:         Pre-loaded AffinityProfile — pass to avoid a
                          second cache lookup in views that already loaded it.
        apply_diversity:  Greedy window reorder to avoid brand/category clusters.

    Returns:
        Sorted list of the same dicts with no new keys added.

    Example (DiscoveryEngineView)::

        from apps.ponno.feed_algorithm import rank_and_diversify, load_user_affinity
        affinity = load_user_affinity(request.user)
        ranked   = rank_and_diversify(product_dicts, affinity=affinity)
    """
    if not products:
        return products

    if affinity is None:
        affinity = load_user_affinity(user)

    scored: List[tuple] = []

    for p in products:
        created_at = p.get('created_at')
        score = (
            _popularity_score_dict(p)
            + _recency_boost(created_at)
            + _affinity_boost_dict(p, affinity)
            + _badge_bonus_dict(p)
        )
        scored.append((score, p))

    scored.sort(key=lambda t: t[0], reverse=True)

    if not apply_diversity:
        return [p for _, p in scored]

    return _diversity_reorder([p for _, p in scored])


# ═════════════════════════════════════════════════════════════════════
# ██  SCORE + RANK — ORM INSTANCES  (HomeView / product list views)
# ═════════════════════════════════════════════════════════════════════

def score_products(
    products:     List,
    followed_ids: List[int],
    cat_ids:      List[int],
) -> List:
    """
    Score and sort a list of ORM Product instances.
    Attaches .feed_score and .age_days to each instance.

    Backward-compatible with original HomeView call signature.

    Args:
        products:     List of ORM Product instances (already fetched).
        followed_ids: Dealer PKs the user follows.
        cat_ids:      Category PKs from the user's recent views.

    Returns:
        Same list sorted descending by feed_score.
    """
    affinity = AffinityProfile(
        brand_scores={},
        category_scores={},
        followed_dealer_ids=set(followed_ids),
        search_words=set(),
    )
    return score_products_orm(products, affinity, cat_ids=set(cat_ids))


def score_products_orm(
    products: List,
    affinity: Optional[AffinityProfile] = None,
    *,
    user=None,
    cat_ids: Optional[Set[int]] = None,
) -> List:
    """
    Score and sort a list of ORM Product instances using a full
    AffinityProfile. Preferred API for new code.

    Args:
        products: List of ORM Product instances.
        affinity: Pre-loaded AffinityProfile. If None and user is given,
                  it will be loaded from cache/DB.
        user:     Request user — used only when affinity is None.
        cat_ids:  Optional set of recently viewed category IDs for
                  CAT_BOOST (legacy support; prefer affinity.category_scores).

    Returns:
        Same list sorted descending by feed_score, with .feed_score and
        .age_days set on each instance.

    Example (HomeView)::

        affinity = load_user_affinity(request.user)
        products = score_products_orm(list(qs), affinity=affinity)
    """
    if affinity is None:
        affinity = load_user_affinity(user) if user else AffinityProfile.empty()

    cat_set = cat_ids or set()

    for p in products:
        recency   = _recency_boost(p.created_at)
        pop       = _popularity_score_orm(p)
        aff       = _affinity_boost_orm(p, affinity)
        badge_b   = _badge_bonus_orm(p)

        # Legacy CAT_BOOST for category IDs (from _get_user_cats())
        legacy_cat_b = CAT_BOOST if getattr(p, 'category_id', None) in cat_set else 0.0

        p.feed_score = round(pop + recency + aff + badge_b + legacy_cat_b, 2)
        p.age_days   = round(
            max((timezone.now() - p.created_at).total_seconds() / 86_400, 0.001), 1
        )

    products.sort(key=lambda x: x.feed_score, reverse=True)
    return products


# ═════════════════════════════════════════════════════════════════════
# ██  DIVERSITY REORDER
# ═════════════════════════════════════════════════════════════════════

def _diversity_reorder(products: List[Any], *, is_orm: bool = False) -> List[Any]:
    """
    Greedy window reorder to avoid brand/category clusters.

    Works with both formatted dicts and ORM instances.
    Pass is_orm=True when working with ORM objects.
    """
    result:             List[Any] = []
    remaining                      = list(products)
    recent_brands:     List        = []
    recent_categories: List        = []

    def _brand(p):
        if is_orm:
            return getattr(p.brand, 'brand_slug', None) or getattr(p, 'dealer_id', '')
        return p.get('brand_slug') or p.get('brand_id') or ''

    def _category(p):
        if is_orm:
            return getattr(p.category, 'category_slug', None) or ''
        return p.get('category_slug') or p.get('category_id') or ''

    while remaining:
        placed = False
        for idx, p in enumerate(remaining):
            pb = _brand(p)
            pc = _category(p)

            if (
                pb not in recent_brands[-DIVERSITY_WINDOW:]
                and pc not in recent_categories[-DIVERSITY_WINDOW:]
            ):
                result.append(p)
                remaining.pop(idx)
                recent_brands.append(pb)
                recent_categories.append(pc)
                placed = True
                break

        if not placed:
            result.extend(remaining)
            break

    return result


def diversify_orm_products(products: List) -> List:
    """
    Apply diversity reorder to a list of pre-scored ORM Product instances.
    Call AFTER score_products_orm() if you want diversity without full ranking.
    """
    return _diversity_reorder(products, is_orm=True)


# ═════════════════════════════════════════════════════════════════════
# ██  FEED ENGINE  (unified builder for any view)
# ═════════════════════════════════════════════════════════════════════

class FeedEngine:
    """
    High-level, reusable feed scoring API for any view.

    Build once per request (affinity loaded once from cache),
    then call score_orm() or rank_dicts() as many times as needed.

    Example — HomeView::

        engine   = FeedEngine(request.user)
        products = engine.score_orm(list(qs), cat_ids=user_cat_ids)

    Example — DiscoveryEngineView::

        engine  = FeedEngine(request.user)
        ranked  = engine.rank_dicts(product_dicts, diversify=True)

    Example — API view (anonymous users handled automatically)::

        engine  = FeedEngine(request.user)  # empty affinity if anon
        ranked  = engine.rank_dicts(product_dicts)

    Example — Email digest (Celery task)::

        from django.contrib.auth import get_user_model
        User = get_user_model()
        user = User.objects.get(pk=user_id)
        engine  = FeedEngine(user)
        ranked  = engine.rank_dicts(product_dicts, diversify=False)
    """

    def __init__(self, user=None, affinity: Optional[AffinityProfile] = None) -> None:
        """
        Args:
            user:     Django user instance. Can be AnonymousUser or None.
            affinity: Pre-built AffinityProfile. If provided, user is ignored.
        """
        self.user     = user
        self.affinity = affinity if affinity is not None else load_user_affinity(user)

    # ── ORM instances ─────────────────────────────────────────────

    def score_orm(
        self,
        products: List,
        *,
        cat_ids:   Optional[Set[int]] = None,
        diversify: bool = False,
    ) -> List:
        """
        Score and sort a list of ORM Product instances.

        Args:
            products:  List of ORM Product instances.
            cat_ids:   Optional set of recently viewed category IDs for legacy boost.
            diversify: Apply diversity reorder after scoring.

        Returns:
            Sorted list with .feed_score and .age_days attached.
        """
        scored = score_products_orm(products, affinity=self.affinity, cat_ids=cat_ids or set())
        if diversify:
            return diversify_orm_products(scored)
        return scored

    # ── Formatted dicts ───────────────────────────────────────────

    def rank_dicts(
        self,
        products:  List[Dict[str, Any]],
        *,
        diversify: bool = True,
    ) -> List[Dict[str, Any]]:
        """
        Score and optionally diversity-reorder a list of formatted product dicts.

        Args:
            products:  List of formatted product dicts.
            diversify: Apply greedy diversity window (default True).

        Returns:
            Sorted list of the same dicts (no new keys added).
        """
        return rank_and_diversify(products, affinity=self.affinity, apply_diversity=diversify)

    # ── Introspection ─────────────────────────────────────────────

    def explain_score_dict(self, p: Dict[str, Any]) -> Dict[str, float]:
        """
        Return a breakdown of score components for a single formatted dict.
        Useful for debugging, admin panels, and A/B testing dashboards.
        """
        created_at = p.get('created_at')
        return {
            'popularity':       round(_popularity_score_dict(p), 2),
            'recency':          round(_recency_boost(created_at), 2),
            'brand_affinity':   round(self.affinity.brand_affinity(p.get('brand_slug'))    * W_BRAND_AFFINITY, 2),
            'cat_affinity':     round(self.affinity.category_affinity(p.get('category_slug')) * W_CATEGORY_AFFINITY, 2),
            'social_follow':    round(W_SOCIAL_FOLLOW if self.affinity.follows_dealer(p.get('dealer_id')) else 0.0, 2),
            'search_match':     round(W_SEARCH_MATCH if self.affinity.matches_search(
                f"{p.get('product_name', '')} {p.get('brand', '')}") else 0.0, 2),
            'badge_bonus':      round(_badge_bonus_dict(p), 2),
            'total':            round(
                _popularity_score_dict(p)
                + _recency_boost(created_at)
                + _affinity_boost_dict(p, self.affinity)
                + _badge_bonus_dict(p),
                2
            ),
        }

    def explain_score_orm(self, p) -> Dict[str, float]:
        """
        Return a breakdown of score components for a single ORM Product instance.
        """
        return {
            'popularity':    round(_popularity_score_orm(p), 2),
            'recency':       round(_recency_boost(p.created_at), 2),
            'affinity':      round(_affinity_boost_orm(p, self.affinity), 2),
            'badge_bonus':   round(_badge_bonus_orm(p), 2),
            'total':         round(
                _popularity_score_orm(p)
                + _recency_boost(p.created_at)
                + _affinity_boost_orm(p, self.affinity)
                + _badge_bonus_orm(p),
                2
            ),
        }

    def __repr__(self) -> str:
        return f"<FeedEngine user={getattr(self.user, 'pk', None)} affinity={self.affinity!r}>"


# ═════════════════════════════════════════════════════════════════════
# ██  RE-EXPORTS
# ═════════════════════════════════════════════════════════════════════

__all__ = [
    # Core types
    'AffinityProfile',
    'FeedEngine',

    # Affinity
    'load_user_affinity',
    'invalidate_user_affinity',

    # Scoring — ORM instances
    'score_products',           # backward-compatible (HomeView)
    'score_products_orm',       # preferred new API

    # Scoring — formatted dicts
    'rank_and_diversify',

    # Diversity
    'diversify_orm_products',

    # Tab filter
    'get_tab_filter_map',
    'TAB_FILTER_NAMES',
    'TAB_FILTER_SLUGS',

    # Constants
    'W_VIEWS', 'W_SALES', 'W_RATING', 'W_WISHLIST', 'W_DISCOUNT',
    'W_RECENCY', 'HALF_LIFE_DAYS',
    'W_BRAND_AFFINITY', 'W_CATEGORY_AFFINITY',
    'W_SOCIAL_FOLLOW', 'W_SEARCH_MATCH',
    'DEALER_BOOST', 'CAT_BOOST',
    'BONUS_FEATURED', 'BONUS_VERIFIED', 'BONUS_TRENDING',
    'DIVERSITY_WINDOW', 'TTL_AFFINITY',
]