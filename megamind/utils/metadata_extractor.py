# megamind/utils/metadata_extractor.py
"""


Fills in everything content_extractor.py doesn't: the long tail of
page metadata that a rich ConnectedService-style model tracks but a
basic scraper skips — Twitter Card, page identity (charset/viewport/
meta description/generator), alternate feeds and icons, article-level
metadata (author URL, modified time, section/tags, breadcrumb),
robots meta directives, and the extra OG fields (locale/video/audio)
content_extractor's extract_og() doesn't cover.

Designed to be called alongside content_extractor.extract_all() on the
same BeautifulSoup object — pass it in rather than re-parsing the HTML
a second time.

    from content_extractor import make_soup
    from metadata_extractor import extract_extended_metadata

    soup = make_soup(html)
    extra = extract_extended_metadata(soup, base_url, response_headers)
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional
from urllib.parse import urljoin

logger = logging.getLogger(__name__)


def _abs(href: Optional[str], base: str) -> str:
    if not href:
        return ""
    try:
        return urljoin(base, href.strip())
    except Exception:
        return ""


def _meta_content(soup: Any, attr: str, value: str) -> str:
    tag = soup.find("meta", attrs={attr: value})
    return (tag.get("content") or "").strip() if tag else ""


def _safe_extract(fn, *args, label: str = "", **kwargs) -> Dict[str, Any]:
    """Runs one metadata sub-extractor and returns {} instead of raising
    on failure — one malformed tag or unexpected structured_data shape
    shouldn't blank out every OTHER metadata field extract_extended_metadata
    would otherwise have collected."""
    try:
        return fn(*args, **kwargs) or {}
    except Exception as exc:
        logger.debug("metadata_extractor: %s failed: %s", label or fn.__name__, exc)
        return {}


# ---------------------------------------------------------------------------
# Twitter Card
# ---------------------------------------------------------------------------

def extract_twitter_card(soup: Any) -> Dict[str, str]:
    return {
        "twitter_card": _meta_content(soup, "name", "twitter:card"),
        "twitter_title": _meta_content(soup, "name", "twitter:title"),
        "twitter_description": _meta_content(soup, "name", "twitter:description"),
        "twitter_image": _meta_content(soup, "name", "twitter:image"),
        "twitter_site": _meta_content(soup, "name", "twitter:site"),
        "twitter_creator": _meta_content(soup, "name", "twitter:creator"),
    }


# ---------------------------------------------------------------------------
# Extended Open Graph (fields content_extractor.extract_og doesn't cover)
# ---------------------------------------------------------------------------

def extract_extended_og(soup: Any) -> Dict[str, str]:
    return {
        "og_locale": _meta_content(soup, "property", "og:locale"),
        "og_video": _meta_content(soup, "property", "og:video") or _meta_content(soup, "property", "og:video:url"),
        "og_audio": _meta_content(soup, "property", "og:audio"),
    }


# ---------------------------------------------------------------------------
# Page identity
# ---------------------------------------------------------------------------

def extract_page_identity(soup: Any) -> Dict[str, str]:
    title_tag = soup.find("title")
    charset_tag = soup.find("meta", attrs={"charset": True})
    charset = (charset_tag.get("charset") or "").strip() if charset_tag else ""
    if not charset:
        content_type_tag = soup.find("meta", attrs={"http-equiv": lambda v: v and v.lower() == "content-type"})
        if content_type_tag and "charset=" in (content_type_tag.get("content") or ""):
            charset = content_type_tag["content"].split("charset=")[-1].strip()

    html_tag = soup.find("html")
    content_language = ((html_tag.get("lang") or "").strip() if html_tag else "") or _meta_content(soup, "http-equiv", "content-language")

    return {
        "page_title": title_tag.get_text(strip=True) if title_tag else "",
        "meta_description": _meta_content(soup, "name", "description"),
        "meta_keywords": _meta_content(soup, "name", "keywords"),
        "meta_generator": _meta_content(soup, "name", "generator"),
        "charset": charset,
        "content_language": content_language,
        "theme_color": _meta_content(soup, "name", "theme-color"),
        "viewport": _meta_content(soup, "name", "viewport"),
    }


# ---------------------------------------------------------------------------
# Feeds / alternate icons
# ---------------------------------------------------------------------------

def extract_feeds_and_icons(soup: Any, base_url: str) -> Dict[str, str]:
    def _link_href(rel_value: str, type_filter: Optional[str] = None) -> str:
        for tag in soup.find_all("link", href=True):
            rels = tag.get("rel")
            rels = {r.lower() for r in rels} if isinstance(rels, list) else ({rels.lower()} if rels else set())
            if rel_value not in rels:
                continue
            if type_filter and type_filter not in (tag.get("type") or ""):
                continue
            return _abs(tag["href"], base_url)
        return ""

    return {
        "apple_touch_icon": _link_href("apple-touch-icon"),
        "manifest_url": _link_href("manifest"),
        "amp_url": _link_href("amphtml"),
        "rss_feed_url": _link_href("alternate", type_filter="rss"),
        "atom_feed_url": _link_href("alternate", type_filter="atom"),
        "author_url": _link_href("author"),
    }


# ---------------------------------------------------------------------------
# Article-level metadata
# ---------------------------------------------------------------------------

def extract_article_meta(soup: Any, base_url: str, structured_data: Optional[List[Any]] = None) -> Dict[str, Any]:
    modified_time = _meta_content(soup, "property", "article:modified_time")
    section = _meta_content(soup, "property", "article:section")
    tags = [
        (t.get("content") or "").strip()
        for t in soup.find_all("meta", attrs={"property": "article:tag"})
        if t.get("content")
    ]

    # Breadcrumb: prefer schema.org BreadcrumbList JSON-LD (authoritative),
    # fall back to a <nav aria-label="breadcrumb">/.breadcrumb element.
    breadcrumb: List[str] = []
    for block in structured_data or []:
        for node in (block if isinstance(block, list) else [block]):
            if not isinstance(node, dict):
                continue
            if node.get("@type") == "BreadcrumbList":
                items = sorted(node.get("itemListElement", []) or [], key=lambda i: (i or {}).get("position", 0))
                breadcrumb = [i.get("name") or (i.get("item") or {}).get("name", "") for i in items if isinstance(i, dict)]
                breadcrumb = [b for b in breadcrumb if b]
                break
        if breadcrumb:
            break
    if not breadcrumb:
        crumb_el = soup.find(attrs={"aria-label": re.compile("breadcrumb", re.I)}) or soup.find(class_=re.compile("breadcrumb", re.I))
        if crumb_el:
            breadcrumb = [a.get_text(strip=True) for a in crumb_el.find_all("a") if a.get_text(strip=True)]

    # same_as_links: schema.org sameAs (social/profile links a Person/
    # Organization/Product node lists about itself).
    same_as: List[str] = []
    for block in structured_data or []:
        for node in (block if isinstance(block, list) else [block]):
            if isinstance(node, dict) and node.get("sameAs"):
                value = node["sameAs"]
                same_as.extend(value if isinstance(value, list) else [value])

    # NOTE: previously resolved against "" instead of the page's actual
    # base_url, which meant any relative hreflang href (uncommon but
    # real — some sites use path-only alternate links) was returned
    # un-resolved instead of turned into an absolute URL. base_url is
    # now threaded through from extract_extended_metadata.
    alternate_languages = [
        {"lang": tag.get("hreflang"), "url": _abs(tag.get("href", ""), base_url)}
        for tag in soup.find_all("link", rel="alternate", hreflang=True) if tag.get("href")
    ]

    return {
        "modified_time": modified_time,
        "article_section": section,
        "article_tags": tags,
        "category_breadcrumb": breadcrumb,
        "same_as_links": list(dict.fromkeys(same_as)),  # de-duped, order preserved
        "alternate_languages": alternate_languages,
    }


# ---------------------------------------------------------------------------
# Robots directives (page-level, distinct from robots.txt — see robots_checker.py)
# ---------------------------------------------------------------------------

def extract_robots_directives(soup: Any, response_headers: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    """robots_meta: the <meta name="robots"> tag. x_robots_tag: the
    X-Robots-Tag response header (search engines honor both; a page can
    set one without the other). is_indexable: False if either directive
    contains noindex, True if both are absent/don't say noindex, else None
    if genuinely ambiguous (kept as a clear tri-state rather than
    guessing)."""
    robots_meta = _meta_content(soup, "name", "robots")
    x_robots_tag = ""
    if response_headers:
        for key, value in response_headers.items():
            if key.lower() == "x-robots-tag":
                x_robots_tag = value
                break

    combined = f"{robots_meta} {x_robots_tag}".lower()
    if "noindex" in combined:
        is_indexable = False
    elif robots_meta or x_robots_tag:
        # Any explicit directive present, and it's already been checked
        # above for noindex — so its mere presence (without noindex)
        # means indexable. (Previously written as `"index" in combined
        # or True`, which is always True regardless of the left side —
        # functionally identical but read like a leftover bug; written
        # plainly here instead.)
        is_indexable = True
    else:
        is_indexable = None  # no directive at all — unknown, not "yes"

    return {"robots_meta": robots_meta, "x_robots_tag": x_robots_tag, "is_indexable": is_indexable}


# ---------------------------------------------------------------------------
# Commerce fields, split into individual model-shaped fields
# (content_extractor.extract_product_info gives one nested dict; this
# reshapes/extends it to match individual ConnectedService columns.)
# ---------------------------------------------------------------------------

_AVAILABILITY_MAP = {
    "instock": "in_stock", "http://schema.org/instock": "in_stock",
    "outofstock": "out_of_stock", "http://schema.org/outofstock": "out_of_stock",
    "limitedavailability": "low_stock", "preorder": "pre_order",
    "http://schema.org/preorder": "pre_order",
}
_CONDITION_MAP = {
    "newcondition": "new", "http://schema.org/newcondition": "new",
    "usedcondition": "used", "http://schema.org/usedcondition": "used",
    "refurbishedcondition": "refurbished", "http://schema.org/refurbishedcondition": "refurbished",
}


def extract_commerce_fields(structured_data: Optional[List[Any]]) -> Dict[str, Any]:
    """Walks JSON-LD Product nodes directly (richer than product_info's
    price/brand/availability-only shape) to populate every commerce
    column: sku, gtin, condition, seller, rating."""
    fields: Dict[str, Any] = {
        "price_amount": None, "price_currency": None, "price_original": None,
        "availability": None, "product_sku": None, "product_gtin": None,
        "product_condition": None, "brand_name": None, "seller_name": None,
        "rating_value": None, "rating_count": None,
    }

    def _iter_nodes(block):
        for node in (block if isinstance(block, list) else [block]):
            if not isinstance(node, dict):
                continue
            if isinstance(node.get("@graph"), list):
                yield from (n for n in node["@graph"] if isinstance(n, dict))
            else:
                yield node

    for block in structured_data or []:
        for node in _iter_nodes(block):
            types = node.get("@type")
            types = types if isinstance(types, list) else [types]
            if "Product" not in (types or []):
                continue

            brand = node.get("brand")
            fields["brand_name"] = (brand.get("name") if isinstance(brand, dict) else brand) or fields["brand_name"]

            gtin = node.get("gtin13") or node.get("gtin") or node.get("gtin12") or node.get("gtin8")
            fields["product_gtin"] = fields["product_gtin"] or gtin
            fields["product_sku"] = fields["product_sku"] or node.get("sku") or node.get("mpn")

            offers = node.get("offers") or {}
            if isinstance(offers, list):
                offers = offers[0] if offers else {}
            if isinstance(offers, dict) and offers:
                fields["price_amount"] = fields["price_amount"] or offers.get("price")
                fields["price_currency"] = fields["price_currency"] or offers.get("priceCurrency")
                high = offers.get("highPrice")
                if high:
                    fields["price_original"] = high
                availability_raw = str(offers.get("availability") or "").strip().lower()
                if availability_raw:
                    fields["availability"] = _AVAILABILITY_MAP.get(availability_raw, fields["availability"])
                seller = offers.get("seller") or {}
                if isinstance(seller, dict) and seller.get("name"):
                    fields["seller_name"] = fields["seller_name"] or seller["name"]

            condition_raw = str(node.get("itemCondition") or "").strip().lower()
            if condition_raw:
                fields["product_condition"] = _CONDITION_MAP.get(condition_raw, fields["product_condition"])

            rating = node.get("aggregateRating") or {}
            if isinstance(rating, dict):
                fields["rating_value"] = fields["rating_value"] or rating.get("ratingValue")
                fields["rating_count"] = fields["rating_count"] or rating.get("ratingCount") or rating.get("reviewCount")

            if any(v is not None for v in fields.values()):
                break
        if any(v is not None for v in fields.values()):
            break

    return fields


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def extract_extended_metadata(
    soup: Any,
    base_url: str,
    structured_data: Optional[List[Any]] = None,
    response_headers: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    """Runs every extractor in this module and returns one merged dict.
    Pass the same `soup` and `structured_data` you already produced via
    content_extractor.extract_all() rather than re-parsing.

    Each sub-extractor is individually error-isolated: one malformed
    tag or unexpected structured_data shape degrades that extractor's
    own fields to {} rather than raising and losing every other
    metadata field this function would otherwise have collected.
    """
    result: Dict[str, Any] = {}
    result.update(_safe_extract(extract_twitter_card, soup, label="twitter_card"))
    result.update(_safe_extract(extract_extended_og, soup, label="extended_og"))
    result.update(_safe_extract(extract_page_identity, soup, label="page_identity"))
    result.update(_safe_extract(extract_feeds_and_icons, soup, base_url, label="feeds_and_icons"))
    result.update(_safe_extract(extract_article_meta, soup, base_url, structured_data, label="article_meta"))
    result.update(_safe_extract(extract_robots_directives, soup, response_headers, label="robots_directives"))
    result.update(_safe_extract(extract_commerce_fields, structured_data, label="commerce_fields"))
    return result