"""
apps/ponno/product_badges.py
============================
Enterprise-grade badge resolver for product card image badges.

Supports any view context — ORM instances, formatted dicts, or raw
attribute mappings. Fully extensible via the BadgeRegistry.

Badge pipeline
──────────────
    ORM Product  ──►  resolve_badges(product)           → list[Badge]
    Formatted dict ►  resolve_badges_from_dict(d)        → list[Badge]
    Any object   ──►  resolve_badges_from_obj(obj, map)  → list[Badge]
    Registry     ──►  BADGE_REGISTRY.resolve(source)     → list[Badge]

Usage in any view
──────────────────
    from apps.ponno.product_badges import resolve_badges, resolve_badges_from_dict

    # ORM instance (product detail view, HomeView, etc.)
    badges = resolve_badges(product)

    # Formatted dict (DiscoveryEngineView, API serializers, etc.)
    badges = resolve_badges_from_dict(formatted_product)

    # Bulk — attach to a list of dicts in one pass
    from apps.ponno.product_badges import attach_badges_to_products
    products = attach_badges_to_products(product_list)   # adds 'badges' key

    # Serialized for JSON API responses
    from apps.ponno.product_badges import badges_to_json
    data['badges'] = badges_to_json(resolve_badges(product))

    # Template-less badge rendering (e.g. email / PDF)
    from apps.ponno.product_badges import render_badges_text
    label_str = render_badges_text(resolve_badges(product))
    # → "SALE · 20% OFF  |  TRENDING  |  IN STOCK"

Configuration
─────────────
Override badge definitions at project start-up via settings.py:

    PRODUCT_BADGE_DEFINITIONS = [
        {
            'key':        'sale',
            'label_fn':   lambda src: f"{int(src.get('discount_pct', 0))}% OFF",
            'condition':  lambda src: (src.get('discount_pct') or 0) > 0,
            'css_suffix': 'img-badge-sale',
            'icon_class': 'fa-solid fa-tag',
            'priority':   0,
        },
        ...
    ]
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence

from django.conf import settings

logger = logging.getLogger(__name__)


# ═════════════════════════════════════════════════════════════════════
# ██  BADGE DATACLASS
# ═════════════════════════════════════════════════════════════════════

@dataclass(frozen=True)
class Badge:
    """
    A single rendered badge.

    Attributes
    ──────────
    key        — internal identifier (sale, trending, instock …)
    css_class  — full CSS class string for the <span> wrapper
    icon_class — Font Awesome class for the <i> tag
    label      — optional text shown next to the icon ("20% OFF")
    priority   — render order (lower = rendered first)
    aria_label — accessibility label for screen readers
    """
    key:        str
    css_class:  str
    icon_class: str
    label:      str  = ''
    priority:   int  = 99
    aria_label: str  = ''

    @property
    def has_label(self) -> bool:
        return bool(self.label)

    def to_dict(self) -> Dict[str, Any]:
        """Serialise to a plain dict (JSON API / template context)."""
        return {
            'key':        self.key,
            'css_class':  self.css_class,
            'icon_class': self.icon_class,
            'label':      self.label,
            'priority':   self.priority,
            'aria_label': self.aria_label or self.label or self.key.upper(),
        }

    def __repr__(self) -> str:
        return f"<Badge key={self.key!r} label={self.label!r}>"


# ═════════════════════════════════════════════════════════════════════
# ██  BADGE DEFINITION  (registry entry)
# ═════════════════════════════════════════════════════════════════════

@dataclass
class BadgeDefinition:
    """
    A single badge rule registered in the BadgeRegistry.

    key         — unique string identifier
    condition   — callable(source_dict) → bool
    css_suffix  — appended to _BASE_CLASS to build css_class
    icon_class  — Font Awesome class
    label_fn    — callable(source_dict) → str  (empty string = icon only)
    priority    — render order (ascending)
    aria_label  — static ARIA label; falls back to label or key if empty
    """
    key:        str
    condition:  Callable[[Dict[str, Any]], bool]
    css_suffix: str
    icon_class: str
    label_fn:   Callable[[Dict[str, Any]], str] = field(default=lambda _: '')
    priority:   int  = 99
    aria_label: str  = ''

    def evaluate(self, source: Dict[str, Any]) -> Optional[Badge]:
        """
        Evaluate this definition against *source*.
        Returns a Badge if the condition is met, else None.
        """
        try:
            if not self.condition(source):
                return None
            label = self.label_fn(source)
            return Badge(
                key=self.key,
                css_class=f'{_BASE_CLASS} {self.css_suffix}',
                icon_class=self.icon_class,
                label=label,
                priority=self.priority,
                aria_label=self.aria_label or label or self.key.upper(),
            )
        except Exception:
            logger.debug(
                'BadgeDefinition.evaluate failed for key=%s', self.key, exc_info=True
            )
            return None


_BASE_CLASS = 'img-badge'


# ═════════════════════════════════════════════════════════════════════
# ██  DEFAULT BADGE DEFINITIONS
# ═════════════════════════════════════════════════════════════════════

_DEFAULT_BADGE_DEFINITIONS: List[Dict[str, Any]] = [
    {
        'key':        'sale',
        'condition':  lambda s: (s.get('discount_pct') or s.get('discount_percentage') or 0) > 0,
        'css_suffix': 'img-badge-sale',
        'icon_class': 'fa-solid fa-tag',
        'label_fn':   lambda s: (
            f"{int(s.get('discount_pct') or s.get('discount_percentage') or 0)}% OFF"
        ),
        'priority':   0,
        'aria_label': 'On Sale',
    },
    {
        'key':        'trending',
        'condition':  lambda s: bool(s.get('is_trending')),
        'css_suffix': 'img-badge-trending',
        'icon_class': 'fa-solid fa-fire',
        'priority':   1,
        'aria_label': 'Trending',
    },
    {
        'key':        'instock',
        'condition':  lambda s: bool(s.get('in_stock') or (s.get('stock') or 0) > 0),
        'css_suffix': 'img-badge-instock',
        'icon_class': 'fa-solid fa-boxes-stacked',
        'priority':   2,
        'aria_label': 'In Stock',
    },
    {
        'key':        'featured',
        'condition':  lambda s: bool(s.get('is_featured')),
        'css_suffix': 'img-badge-featured',
        'icon_class': 'fa-solid fa-crown',
        'priority':   3,
        'aria_label': 'Featured',
    },
    {
        'key':        'verified',
        'condition':  lambda s: bool(s.get('is_verified')),
        'css_suffix': 'img-badge-verified',
        'icon_class': 'fa-solid fa-circle-check',
        'priority':   4,
        'aria_label': 'Verified',
    },
    {
        'key':        'shipping',
        'condition':  lambda s: bool(s.get('free_shipping')),
        'css_suffix': 'img-badge-shipping',
        'icon_class': 'fa-solid fa-truck',
        'priority':   5,
        'aria_label': 'Free Shipping',
    },
]


# ═════════════════════════════════════════════════════════════════════
# ██  BADGE REGISTRY
# ═════════════════════════════════════════════════════════════════════

class BadgeRegistry:
    """
    Central registry that holds BadgeDefinition instances and resolves
    badges from any dict-like source.

    Supports registration of custom badges at runtime (e.g. from an
    app's AppConfig.ready()) and overrides via settings.PRODUCT_BADGE_DEFINITIONS.

    Thread-safe for read access after app startup.
    """

    def __init__(self) -> None:
        self._definitions: List[BadgeDefinition] = []
        self._key_index:   Dict[str, BadgeDefinition] = {}

    # ── Registration ──────────────────────────────────────────────

    def register(self, definition: BadgeDefinition) -> None:
        """
        Register a BadgeDefinition.
        If a definition with the same key already exists it is replaced.
        """
        if definition.key in self._key_index:
            self._definitions = [
                d for d in self._definitions if d.key != definition.key
            ]
        self._definitions.append(definition)
        self._definitions.sort(key=lambda d: d.priority)
        self._key_index[definition.key] = definition

    def register_many(self, definitions: Iterable[BadgeDefinition]) -> None:
        for d in definitions:
            self.register(d)

    def unregister(self, key: str) -> None:
        """Remove a badge definition by key."""
        self._definitions = [d for d in self._definitions if d.key != key]
        self._key_index.pop(key, None)

    def get(self, key: str) -> Optional[BadgeDefinition]:
        return self._key_index.get(key)

    @property
    def keys(self) -> List[str]:
        return [d.key for d in self._definitions]

    # ── Resolution ────────────────────────────────────────────────

    def resolve(self, source: Dict[str, Any]) -> List[Badge]:
        """
        Evaluate all registered definitions against *source* (a flat dict).
        Returns badges in priority order.
        """
        badges = []
        for definition in self._definitions:
            badge = definition.evaluate(source)
            if badge is not None:
                badges.append(badge)
        return badges

    def resolve_subset(self, source: Dict[str, Any], keys: Iterable[str]) -> List[Badge]:
        """Resolve only the badge definitions matching *keys*."""
        key_set = frozenset(keys)
        badges = []
        for definition in self._definitions:
            if definition.key not in key_set:
                continue
            badge = definition.evaluate(source)
            if badge is not None:
                badges.append(badge)
        return badges

    # ── Introspection ─────────────────────────────────────────────

    def __len__(self) -> int:
        return len(self._definitions)

    def __repr__(self) -> str:
        return f"<BadgeRegistry definitions={self.keys}>"


def _build_registry() -> BadgeRegistry:
    """
    Build the default registry, then apply any project-level overrides
    defined in settings.PRODUCT_BADGE_DEFINITIONS.

    Project definitions completely replace defaults when provided.
    """
    registry = BadgeRegistry()

    defs_config: List[Dict[str, Any]] = getattr(
        settings, 'PRODUCT_BADGE_DEFINITIONS', _DEFAULT_BADGE_DEFINITIONS
    )

    for raw in defs_config:
        registry.register(BadgeDefinition(
            key=raw['key'],
            condition=raw['condition'],
            css_suffix=raw['css_suffix'],
            icon_class=raw['icon_class'],
            label_fn=raw.get('label_fn', lambda _: ''),
            priority=raw.get('priority', 99),
            aria_label=raw.get('aria_label', ''),
        ))

    return registry


# Singleton — built once at import time.
BADGE_REGISTRY: BadgeRegistry = _build_registry()


# ═════════════════════════════════════════════════════════════════════
# ██  SOURCE NORMALISER
# ═════════════════════════════════════════════════════════════════════

def _normalise_orm(product) -> Dict[str, Any]:
    """
    Convert an ORM Product instance into the canonical flat dict
    understood by BadgeDefinition.condition callables.
    """
    return {
        'discount_pct':        float(getattr(product, 'discount_percentage', 0) or 0),
        'discount_percentage': float(getattr(product, 'discount_percentage', 0) or 0),
        'is_trending':         bool(getattr(product, 'is_trending',   False)),
        'stock':               int(getattr(product, 'stock',          0) or 0),
        'in_stock':            (int(getattr(product, 'stock', 0) or 0)) > 0,
        'is_featured':         bool(getattr(product, 'is_featured',   False)),
        'is_verified':         bool(getattr(product, 'is_verified',   False)),
        'free_shipping':       bool(getattr(product, 'free_shipping', False)),
    }


def _normalise_dict(product: Dict[str, Any]) -> Dict[str, Any]:
    """
    Ensure both field-name variants are present in a formatted product dict.
    Mutates and returns the dict (cheap — only on existing keys).
    """
    # Unify discount field names
    if 'discount_badge' in product and 'discount_pct' not in product:
        badge_str = product.get('discount_badge') or ''
        try:
            product['discount_pct'] = float(badge_str.replace('%', '').split()[0])
        except (ValueError, IndexError, AttributeError):
            product['discount_pct'] = 0

    if 'discount_percentage' in product and 'discount_pct' not in product:
        product['discount_pct'] = float(product['discount_percentage'] or 0)

    # Unify stock field names
    if 'in_stock' not in product:
        product['in_stock'] = (product.get('stock') or 0) > 0

    return product


# ═════════════════════════════════════════════════════════════════════
# ██  PUBLIC API — SINGLE PRODUCT
# ═════════════════════════════════════════════════════════════════════

def resolve_badges(product) -> List[Badge]:
    """
    Resolve badges from an **ORM Product instance**.

    Suitable for: product detail views, HomeView, admin panels,
    any view that works directly with ORM objects.

    Args:
        product: ORM Product instance.

    Returns:
        List of Badge objects in priority (render) order.
    """
    return BADGE_REGISTRY.resolve(_normalise_orm(product))


def resolve_badges_from_dict(product: Dict[str, Any]) -> List[Badge]:
    """
    Resolve badges from a **formatted product dict**
    (output of _format_products() or any serialiser).

    Suitable for: DiscoveryEngineView, DRF serializers, template context
    dicts, cached product representations.

    Args:
        product: Formatted product dict. Accepts either naming convention:
                   discount_badge / discount_percentage / discount_pct
                   in_stock / stock

    Returns:
        List of Badge objects in priority (render) order.
    """
    return BADGE_REGISTRY.resolve(_normalise_dict(dict(product)))


def resolve_badges_from_obj(
    obj: Any,
    attr_map: Optional[Dict[str, str]] = None,
) -> List[Badge]:
    """
    Resolve badges from **any object** by mapping its attributes to the
    canonical badge source keys.

    Useful for non-standard ORM models, dataclasses, namedtuples, etc.

    Args:
        obj:      Any object with readable attributes.
        attr_map: Optional mapping of canonical key → attribute name.
                  Defaults to the standard ORM field names.

    Returns:
        List of Badge objects in priority (render) order.

    Example::

        badges = resolve_badges_from_obj(
            api_product,
            attr_map={
                'discount_pct':  'discount',
                'is_trending':   'trending',
                'stock':         'quantity',
                'is_featured':   'featured',
                'is_verified':   'verified',
                'free_shipping': 'shipping_free',
            }
        )
    """
    _default_map = {
        'discount_pct':        'discount_percentage',
        'discount_percentage': 'discount_percentage',
        'is_trending':         'is_trending',
        'stock':               'stock',
        'in_stock':            'in_stock',
        'is_featured':         'is_featured',
        'is_verified':         'is_verified',
        'free_shipping':       'free_shipping',
    }
    mapping = {**_default_map, **(attr_map or {})}
    source: Dict[str, Any] = {}
    for canonical, attr in mapping.items():
        source[canonical] = getattr(obj, attr, None)
    return BADGE_REGISTRY.resolve(_normalise_dict(source))


# ═════════════════════════════════════════════════════════════════════
# ██  PUBLIC API — BULK / COLLECTION HELPERS
# ═════════════════════════════════════════════════════════════════════

def attach_badges_to_products(
    products: List[Dict[str, Any]],
    *,
    key: str = 'badges',
    serialized: bool = False,
) -> List[Dict[str, Any]]:
    """
    Attach badge data to every product dict in a list **in place**.

    Designed for use in _format_products() or any view that builds
    a list of product dicts before passing to template / serializer.

    Args:
        products:   List of formatted product dicts.
        key:        Dict key under which badges are stored (default 'badges').
        serialized: If True, store list[dict] instead of list[Badge].
                    Use for JSON API responses.

    Returns:
        The same list (mutated in place) for chaining.

    Example::

        products = attach_badges_to_products(product_list)
        # Each dict now has products[i]['badges'] = [Badge(...), ...]
    """
    for product in products:
        badges = resolve_badges_from_dict(product)
        product[key] = [b.to_dict() for b in badges] if serialized else badges
    return products


def attach_badges_to_orm_products(
    products: Sequence,
    *,
    attr: str = 'badges',
) -> Sequence:
    """
    Attach a `.badges` attribute to each ORM Product instance in a sequence.

    Designed for HomeView, product list views, or any view that works
    with ORM querysets converted to lists.

    Args:
        products: Sequence of ORM Product instances.
        attr:     Attribute name to attach (default 'badges').

    Returns:
        The same sequence for chaining.

    Example::

        products = list(Product.objects.active_products()[:20])
        products = attach_badges_to_orm_products(products)
        # Each instance now has product.badges = [Badge(...), ...]
    """
    for product in products:
        setattr(product, attr, resolve_badges(product))
    return products


# ═════════════════════════════════════════════════════════════════════
# ██  SERIALISATION HELPERS
# ═════════════════════════════════════════════════════════════════════

def badges_to_json(badges: List[Badge]) -> List[Dict[str, Any]]:
    """
    Convert a list of Badge objects to plain dicts suitable for JSON
    serialisation (DRF, django.http.JsonResponse, etc.).

    Example::

        data['badges'] = badges_to_json(resolve_badges(product))
    """
    return [b.to_dict() for b in badges]


def render_badges_text(badges: List[Badge], separator: str = '  |  ') -> str:
    """
    Render badges as a plain text string — useful for email templates,
    PDF generation, logging, and accessibility descriptions.

    Example::

        render_badges_text(resolve_badges(product))
        # → "SALE · 20% OFF  |  TRENDING  |  IN STOCK"
    """
    parts = []
    for b in badges:
        if b.label:
            parts.append(f"{b.key.upper()} · {b.label}")
        else:
            parts.append(b.key.upper())
    return separator.join(parts)


def get_badge_keys(badges: List[Badge]) -> List[str]:
    """Return just the badge keys — useful for analytics / logging."""
    return [b.key for b in badges]


# ═════════════════════════════════════════════════════════════════════
# ██  RE-EXPORTS
# ═════════════════════════════════════════════════════════════════════

__all__ = [
    # Core types
    'Badge',
    'BadgeDefinition',
    'BadgeRegistry',

    # Singleton registry
    'BADGE_REGISTRY',

    # Single-product resolvers
    'resolve_badges',
    'resolve_badges_from_dict',
    'resolve_badges_from_obj',

    # Bulk helpers
    'attach_badges_to_products',
    'attach_badges_to_orm_products',

    # Serialisation
    'badges_to_json',
    'render_badges_text',
    'get_badge_keys',
]