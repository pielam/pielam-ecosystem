# megamind/utils/readability_extractor.py

"""
Density-based "main content" extraction — the same family of algorithm
behind Readability.js / python-readability / newspaper3k, implemented
here with no extra dependency beyond BeautifulSoup (already required
by content_extractor.py).

Why this exists: content_extractor.extract_text() picks the first of
<main>/<article>/<body> it finds and takes ALL the text inside it. That
works fine on clean pages but grabs nav menus, cookie banners, related-
article widgets, and comment sections on any page that doesn't isolate
its content in a dedicated tag — which is most real-world sites. This
scores every block-level candidate by text density (real paragraph
text vs. link text vs. markup overhead) and returns the highest-scoring
one, the same way Readability.js identifies "the article" on an
arbitrary page.

Trade-off: this is a heuristic, not a parser — no algorithm gets every
page right. Use it as the default for "give me the article text"; fall
back to content_extractor.extract_text() (or the raw stripped-tag dump)
if a specific page it disagrees with matters enough to hand-tune.

    from readability_extractor import extract_main_content
    article_text, score_debug = extract_main_content(soup)
"""

import re
from bs4 import BeautifulSoup, NavigableString

# Tags whose contents are never real article content.
_JUNK_TAGS = {
    "script", "style", "noscript", "head", "nav", "footer", "header",
    "aside", "form", "iframe", "button", "svg", "figure",
}
# Class/id substrings that reliably indicate chrome, not content.
_JUNK_HINTS = (
    "nav", "menu", "sidebar", "footer", "header", "comment", "share",
    "social", "related", "widget", "cookie", "banner", "advert", "ad-",
    "promo", "subscribe", "newsletter", "breadcrumb", "pagination",
)
# Tags worth scoring as content-block candidates.
_CANDIDATE_TAGS = ("div", "section", "article", "main", "td")

MIN_PARAGRAPH_LENGTH = 25  # chars — shorter <p> tags are usually UI labels, not content


def _looks_like_junk(tag) -> bool:
    ident = " ".join(filter(None, [
        " ".join(tag.get("class", [])) if tag.get("class") else "",
        tag.get("id", "") or "",
    ])).lower()
    return any(hint in ident for hint in _JUNK_HINTS)


def _link_density(tag) -> float:
    text_len = len(tag.get_text(strip=True))
    if text_len == 0:
        return 1.0
    link_len = sum(len(a.get_text(strip=True)) for a in tag.find_all("a"))
    return link_len / text_len


def _score_candidate(tag) -> float:
    """Higher score = more likely to be the real article body.
    Rewards long paragraph text and punctuation density (real prose has
    commas/periods; nav menus and link lists don't); penalizes high
    link density (a wall of links is a menu, not an article) and
    chrome-like class/id names."""
    if _looks_like_junk(tag):
        return -1.0

    paragraphs = tag.find_all("p", recursive=True)
    text_chunks = [p.get_text(strip=True) for p in paragraphs if len(p.get_text(strip=True)) >= MIN_PARAGRAPH_LENGTH]
    if not text_chunks:
        # No substantial <p> tags directly — fall back to the tag's own
        # text (handles sites that don't wrap paragraphs in <p>).
        own_text = tag.get_text(strip=True)
        if len(own_text) < 200:
            return -1.0
        text_chunks = [own_text]

    combined = " ".join(text_chunks)
    length_score = min(len(combined) / 100.0, 40.0)  # cap so one giant block can't dominate purely on size
    punctuation_score = combined.count(",") * 0.3 + combined.count(". ") * 0.2
    density_penalty = _link_density(tag) * 20.0
    depth_bonus = 0.0  # (kept simple — deliberately no DOM-depth heuristic to stay dependency-free)

    return length_score + punctuation_score - density_penalty + depth_bonus


def extract_main_content(soup: BeautifulSoup) -> tuple:
    """
    Returns (text, debug) where `text` is the best-guess article body
    and `debug` is {"candidates_scored": N, "winning_score": float,
    "winning_tag": "div.article-body" or similar} for troubleshooting
    why a particular page extracted the way it did.

    Falls back to the whole page's visible text if no candidate scores
    above zero (e.g. a page with no real prose at all — a pure image
    gallery or link directory).
    """
    work_soup = soup  # mutated in place (junk tags decomposed below) — pass a copy in if the caller needs the original soup preserved

    for tag in work_soup(_JUNK_TAGS):
        tag.decompose()

    best_tag, best_score, scored = None, 0.0, 0
    for tag in work_soup.find_all(_CANDIDATE_TAGS):
        score = _score_candidate(tag)
        scored += 1
        if score > best_score:
            best_score, best_tag = score, tag

    if best_tag is None:
        text = re.sub(r"\n{3,}", "\n\n", work_soup.get_text("\n", strip=True))
        return text, {"candidates_scored": scored, "winning_score": 0.0, "winning_tag": None}

    text = best_tag.get_text(separator="\n", strip=True)
    text = re.sub(r"\n{3,}", "\n\n", text)
    tag_desc = best_tag.name
    if best_tag.get("id"):
        tag_desc += f"#{best_tag['id']}"
    elif best_tag.get("class"):
        tag_desc += f".{'.'.join(best_tag['class'][:2])}"

    return text, {"candidates_scored": scored, "winning_score": round(best_score, 2), "winning_tag": tag_desc}


def estimate_reading_time(text: str, words_per_minute: int = 225) -> int:
    """Minutes, rounded up, minimum 1 for any non-empty text."""
    if not text:
        return 0
    word_count = len(text.split())
    return max(1, round(word_count / words_per_minute))



"""
readability_extractor.py

Density-based "main content" extraction — the same family of algorithm
behind Readability.js / python-readability / newspaper3k, implemented
here with no extra dependency beyond BeautifulSoup.

Why this matters for text quality: naively grabbing all the text
inside <main>/<article>/<body> also grabs nav menus, cookie banners,
related-article widgets, and comment sections on any page that doesn't
isolate its content in a dedicated tag — which is most real-world
sites. This scores every block-level candidate by text density (real
paragraph text vs. link text vs. markup overhead) and returns the
highest-scoring one, the same way Readability.js identifies "the
article" on an arbitrary page.

Trade-off: this is a heuristic, not a parser — no algorithm gets every
page right, but it is meaningfully better than a naive tag-based dump.

    from readability_extractor import extract_main_content
    article_text, score_debug = extract_main_content(soup)
"""

import re
from bs4 import BeautifulSoup  # noqa: F401  (kept for type clarity / callers)

# Tags whose contents are never real article content.
_JUNK_TAGS = {
    "script", "style", "noscript", "head", "nav", "footer", "header",
    "aside", "form", "iframe", "button", "svg", "figure",
}
# Class/id substrings that reliably indicate chrome, not content.
_JUNK_HINTS = (
    "nav", "menu", "sidebar", "footer", "header", "comment", "share",
    "social", "related", "widget", "cookie", "banner", "advert", "ad-",
    "promo", "subscribe", "newsletter", "breadcrumb", "pagination",
)
# Tags worth scoring as content-block candidates.
_CANDIDATE_TAGS = ("div", "section", "article", "main", "td")

MIN_PARAGRAPH_LENGTH = 25  # chars — shorter <p> tags are usually UI labels, not content


def _looks_like_junk(tag) -> bool:
    ident = " ".join(filter(None, [
        " ".join(tag.get("class", [])) if tag.get("class") else "",
        tag.get("id", "") or "",
    ])).lower()
    return any(hint in ident for hint in _JUNK_HINTS)


def _link_density(tag) -> float:
    text_len = len(tag.get_text(strip=True))
    if text_len == 0:
        return 1.0
    link_len = sum(len(a.get_text(strip=True)) for a in tag.find_all("a"))
    return link_len / text_len


def _score_candidate(tag) -> float:
    """Higher score = more likely to be the real article body. Rewards
    long paragraph text and punctuation density (real prose has
    commas/periods; nav menus and link lists don't); penalizes high
    link density (a wall of links is a menu, not an article) and
    chrome-like class/id names."""
    if _looks_like_junk(tag):
        return -1.0

    paragraphs = tag.find_all("p", recursive=True)
    text_chunks = [p.get_text(strip=True) for p in paragraphs if len(p.get_text(strip=True)) >= MIN_PARAGRAPH_LENGTH]
    if not text_chunks:
        own_text = tag.get_text(strip=True)
        if len(own_text) < 200:
            return -1.0
        text_chunks = [own_text]

    combined = " ".join(text_chunks)
    length_score = min(len(combined) / 100.0, 40.0)
    punctuation_score = combined.count(",") * 0.3 + combined.count(". ") * 0.2
    density_penalty = _link_density(tag) * 20.0

    return length_score + punctuation_score - density_penalty


def extract_main_content(soup) -> tuple:
    """
    Returns (text, debug) where `text` is the best-guess main body and
    `debug` is {"candidates_scored": N, "winning_score": float,
    "winning_tag": "div.article-body" or similar}.

    Falls back to the whole page's visible text if no candidate scores
    above zero (e.g. a page with no real prose at all — a pure image
    gallery or link directory).

    NOTE: mutates `soup` in place (junk tags are decomposed). Pass a
    copy in if the caller needs the original soup preserved afterward.
    """
    for tag in soup(_JUNK_TAGS):
        tag.decompose()

    best_tag, best_score, scored = None, 0.0, 0
    for tag in soup.find_all(_CANDIDATE_TAGS):
        score = _score_candidate(tag)
        scored += 1
        if score > best_score:
            best_score, best_tag = score, tag

    if best_tag is None:
        text = re.sub(r"\n{3,}", "\n\n", soup.get_text("\n", strip=True))
        return text, {"candidates_scored": scored, "winning_score": 0.0, "winning_tag": None}

    text = best_tag.get_text(separator="\n", strip=True)
    text = re.sub(r"\n{3,}", "\n\n", text)
    tag_desc = best_tag.name
    if best_tag.get("id"):
        tag_desc += f"#{best_tag['id']}"
    elif best_tag.get("class"):
        tag_desc += f".{'.'.join(best_tag['class'][:2])}"

    return text, {"candidates_scored": scored, "winning_score": round(best_score, 2), "winning_tag": tag_desc}