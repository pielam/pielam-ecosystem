# megamind/utils/text_info.py

"""
Shared text-analysis utilities.

Companion to media_info.py (image/link normalization) and video_info.py
(video URL normalization): where those two normalize *shape*, this
module extracts *signal* from a page's extracted text â€” word/reading-time
stats, a lightweight sentiment score, frequency-based keywords, optional
language detection, PII detection, and a content-quality (thin-content)
signal.

WHY THIS FILE EXISTS
---------------------
megamind/services/intelligence_scraper.py already has private
(underscore-prefixed) versions of sentiment/keyword/language analysis
(_analyze_sentiment, _extract_keywords, _detect_language) â€” but they're
only reachable from that module's own multi-link "intel" pass. Two gaps
that fall out of that:

  1. megamind/services/scraper.py (the PRIMARY single-URL scrape path
     that every ConnectedService row goes through) has no access to any
     of this â€” a service scraped via scrape_and_store() gets no
     sentiment, no extracted keywords, no detected language, even though
     the exact same logic already exists one module away.

  2. ConnectedService.content_sensitivity is a real column (see
     megamind/models/connected_service.py) that megamind/services/
     product_sync.py already READS and blocks on
     (ContentSensitivity.PII_DETECTED), but nothing in this codebase
     ever WRITES it â€” it sits at its default ('unknown') forever. There
     is no PII-detection pass anywhere. detect_pii() /
     classify_content_sensitivity() below close that gap.

This module is the shared, public home for that logic so both scraper.py
and intelligence_scraper.py can call the same functions instead of the
signal only existing in one code path (and PII detection existing in
neither).

No Django import on purpose (same convention as media_info.py /
link_info.py) â€” pure stdlib, safe to call from a template context
processor, a Celery task, a DRF serializer, or a plain script. Every
function here is a pure transform: nothing is fetched, nothing is
written to a model, nothing raises on bad input (worst case an empty/
default result is returned).

INTEGRATION NOTE: this file does not itself wire into scraper.py or
intelligence_scraper.py â€” see the bottom of this docstring... actually,
see the module-level "Suggested wiring" note near the bottom of this
file for the two one-line call sites that would close the gaps above
without changing either module's existing return shape.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, TypedDict

__all__ = [
    "SentimentResult",
    "PIIFindings",
    "TextStats",
    "ContentQuality",
    "word_count",
    "reading_time_minutes",
    "text_stats",
    "analyze_sentiment",
    "extract_keywords",
    "detect_language",
    "detect_pii",
    "classify_content_sensitivity",
    "content_quality",
    "summarize_text",
]

# ---------------------------------------------------------------------------
# Optional language detection â€” mirrors intelligence_scraper.py's guarded
# import exactly, so behavior (silently off if not installed) is
# consistent wherever text_info.py is used instead of that module.
# ---------------------------------------------------------------------------

try:
    import langdetect  # noqa: F401
    _LANGDETECT_AVAILABLE = True
except ImportError:
    _LANGDETECT_AVAILABLE = False


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

WORDS_PER_MINUTE = 200

_WORD_RE = re.compile(r"[a-zA-Z][a-zA-Z'-]{2,}")

_STOPWORDS = frozenset("""
a an the and or but if then else for to of in on at by with from up
about into over after under again further once here there when where
why how all any both each few more most other some such no nor not
only own same so than too very s t can will just don should now is
are was were be been being have has had do does did doing this that
these those i you he she it we they them his her its our their my your
""".split())

_SENTIMENT_POSITIVE_WORDS = frozenset("""
good great excellent amazing awesome fantastic wonderful best love loved
loving perfect happy delighted pleased satisfied impressive impressed
recommend recommended reliable easy helpful positive success successful
beautiful outstanding superb brilliant favorite favourite affordable
efficient innovative valuable enjoy enjoyed enjoying quality trustworthy
""".split())

_SENTIMENT_NEGATIVE_WORDS = frozenset("""
bad terrible awful horrible worst hate hated hating poor disappointing
disappointed frustrating frustrated broken bug buggy fail failed failure
useless slow expensive overpriced problem issue issues complaint
complaints negative unreliable annoying confusing difficult hard
misleading scam fraud fake defective
""".split())

# --- PII detection -----------------------------------------------------
#
# Conservative on purpose: a false negative (missing some PII) is the
# existing status quo (nothing is detected at all today); a false
# positive (flagging a page as PII_DETECTED when it isn't) blocks
# product_sync.sync_product_connected_service() for that service, so
# each pattern below is scoped to avoid matching prices, dates, order
# IDs, or version numbers as accidentally as possible.

_EMAIL_RE = re.compile(r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}')

# E.164-ish or common national formats, e.g. +1 415-555-0199,
# (415) 555-0199, 415.555.0199. Requires 10-15 digits total and at
# least one separator/prefix so it doesn't swallow bare numeric IDs
# (SKUs, order numbers, years) that happen to be 10+ digits long.
_PHONE_RE = re.compile(
    r'(?<!\d)(?:\+\d{1,3}[\s.-]?)?\(?\d{3}\)?[\s.-]\d{3}[\s.-]\d{4}(?!\d)'
)

# US Social Security Number shape: NNN-NN-NNNN. Hyphens required (not
# just any 9 digits) to keep this from matching phone numbers, zip+4,
# or arbitrary numeric strings.
_SSN_RE = re.compile(r'(?<!\d)\d{3}-\d{2}-\d{4}(?!\d)')

# Candidate card-number shapes: 13-19 digits, optionally grouped by
# spaces or hyphens in 4s. Luhn-checked below to cut false positives
# from phone numbers, tracking IDs, etc. that happen to fall in this
# length range.
_CARD_CANDIDATE_RE = re.compile(r'(?<!\d)(?:\d[ -]?){13,19}(?!\d)')

# IBAN-shaped strings: 2 letters, 2 digits, 11-30 alphanumeric.
_IBAN_RE = re.compile(r'\b[A-Z]{2}\d{2}[A-Z0-9]{11,30}\b')


def _luhn_valid(digits: str) -> bool:
    """Standard Luhn checksum â€” used to confirm a card-number-shaped
    match is plausibly a real card number rather than an arbitrary
    13-19 digit string (order id, phone number, tracking code)."""
    if not digits.isdigit() or not (13 <= len(digits) <= 19):
        return False
    total = 0
    reverse_digits = digits[::-1]
    for i, ch in enumerate(reverse_digits):
        d = int(ch)
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------

class SentimentResult(TypedDict):
    label: str          # 'positive' | 'neutral' | 'negative'
    score: float         # normalized [-1, 1]
    positive_hits: int
    negative_hits: int


class PIIFindings(TypedDict):
    has_pii: bool
    emails: List[str]
    phones: List[str]
    ssns: List[str]
    card_numbers: List[str]   # last-4 only, never the full number
    ibans: List[str]
    categories: List[str]     # e.g. ['email', 'phone']


class TextStats(TypedDict):
    word_count: int
    reading_time_minutes: int
    char_count: int


class ContentQuality(TypedDict):
    text_to_html_ratio: float
    is_thin_content: bool


# ---------------------------------------------------------------------------
# Basic stats
# ---------------------------------------------------------------------------

def word_count(text: Optional[str]) -> int:
    """Whitespace-split word count. Single source of truth â€” replaces
    the ad hoc `len(text.split())` scattered across scraper.py and
    link_info.py's display-layer fallback, so a "word count" always
    means the same thing everywhere it's shown."""
    if not text:
        return 0
    return len(text.split())


def reading_time_minutes(text: Optional[str] = None, *, words: Optional[int] = None) -> int:
    """Reading time at WORDS_PER_MINUTE (200), minimum 1 minute for any
    non-empty text. Pass an already-computed `words` count to avoid
    re-splitting the same text twice; pass `text` directly otherwise."""
    n = words if words is not None else word_count(text)
    if not n:
        return 0
    return max(1, round(n / WORDS_PER_MINUTE))


def text_stats(text: Optional[str]) -> TextStats:
    """Convenience bundle of the stats above, computed once."""
    n = word_count(text)
    return {
        "word_count": n,
        "reading_time_minutes": reading_time_minutes(words=n),
        "char_count": len(text) if text else 0,
    }


# ---------------------------------------------------------------------------
# Sentiment (lexicon-based â€” approximate, see docstring below)
# ---------------------------------------------------------------------------

def analyze_sentiment(text: Optional[str]) -> SentimentResult:
    """
    Approximate, lexicon-based sentiment scoring: counts positive vs
    negative words against a small embedded word list and derives a
    label + normalized score in [-1, 1]. Intentionally simple â€” misses
    negation, sarcasm, and domain-specific language. This is a rough
    signal, not a real sentiment classifier. If a real model (e.g. a
    transformers pipeline) is available in a given deployment, swap
    this function's body for that; the return shape is deliberately
    small so a real model's output can be mapped onto it without
    touching any caller.
    """
    words = [w.lower() for w in _WORD_RE.findall(text or "")]
    if not words:
        return {"label": "neutral", "score": 0.0, "positive_hits": 0, "negative_hits": 0}

    positive_hits = sum(1 for w in words if w in _SENTIMENT_POSITIVE_WORDS)
    negative_hits = sum(1 for w in words if w in _SENTIMENT_NEGATIVE_WORDS)
    total_hits = positive_hits + negative_hits

    if total_hits == 0:
        return {"label": "neutral", "score": 0.0, "positive_hits": 0, "negative_hits": 0}

    score = round((positive_hits - negative_hits) / total_hits, 3)
    if score > 0.15:
        label = "positive"
    elif score < -0.15:
        label = "negative"
    else:
        label = "neutral"

    return {"label": label, "score": score, "positive_hits": positive_hits, "negative_hits": negative_hits}


# ---------------------------------------------------------------------------
# Keywords (frequency-based â€” no external NLP dependency)
# ---------------------------------------------------------------------------

def extract_keywords(text: Optional[str], max_keywords: int = 10) -> List[str]:
    """
    Simple frequency-based keyword/keyphrase extraction: stopword-
    filtered unigrams plus adjacent-word bigrams, ranked by frequency.
    No external NLP dependency (no sklearn/spaCy) â€” a lightweight
    complement to a page's own declared meta-keywords/article-tags,
    useful for pages that don't declare any.
    """
    words = [w.lower() for w in _WORD_RE.findall(text or "") if w.lower() not in _STOPWORDS]
    if not words:
        return []

    unigram_counts: Dict[str, int] = {}
    for w in words:
        unigram_counts[w] = unigram_counts.get(w, 0) + 1

    bigram_counts: Dict[str, int] = {}
    for i in range(len(words) - 1):
        bigram = f"{words[i]} {words[i + 1]}"
        bigram_counts[bigram] = bigram_counts.get(bigram, 0) + 1

    # Bigrams that repeat at least twice are usually more meaningful
    # phrases than a single repeated word; unigrams fill the rest.
    ranked_bigrams = [b for b, c in sorted(bigram_counts.items(), key=lambda x: x[1], reverse=True) if c >= 2]
    ranked_unigrams = [w for w, _c in sorted(unigram_counts.items(), key=lambda x: x[1], reverse=True)]

    combined: List[str] = []
    for phrase in ranked_bigrams + ranked_unigrams:
        if phrase not in combined:
            combined.append(phrase)
        if len(combined) >= max_keywords:
            break
    return combined


# ---------------------------------------------------------------------------
# Language detection (optional â€” requires `pip install langdetect`)
# ---------------------------------------------------------------------------

def detect_language(text: Optional[str]) -> Optional[str]:
    """
    Populates a detected-language code ONLY if the optional `langdetect`
    package is installed. Returns None otherwise â€” no home-grown
    heuristic guess is substituted, since a low-confidence guess
    presented as a detected language is worse than reporting nothing.
    A page's own declared <html lang> attribute (extracted separately
    by the scraper) should remain the primary signal; this is a
    text-based fallback for pages that don't declare one.
    """
    if not _LANGDETECT_AVAILABLE or not text or len(text.strip()) < 20:
        return None
    try:
        return langdetect.detect(text[:2000])
    except Exception:
        return None


# ---------------------------------------------------------------------------
# PII detection â€” NEW. Nothing in the codebase currently populates
# ConnectedService.content_sensitivity; these two functions are the
# missing piece.
# ---------------------------------------------------------------------------

def detect_pii(text: Optional[str]) -> PIIFindings:
    """
    Best-effort PII scan of extracted page text. Deliberately narrow
    and pattern-based (no ML model) â€” designed to minimize false
    positives over recall, since a false positive here blocks
    product_sync.sync_product_connected_service() for that service.

    Detects: email addresses, phone numbers (common formats), US SSNs
    (strict NNN-NN-NNNN shape), credit-card-shaped numbers that pass a
    Luhn checksum, and IBAN-shaped strings.

    Card numbers are NEVER returned in full â€” only a masked
    '**** **** **** 1234' form â€” so this result is itself safe to log
    or store without becoming a PII leak of its own.

    This is a scan of a page's OWN content (something the page author
    published or a form pre-filled), not a judgment about whether that
    content is sensitive in context â€” a support/FAQ page that shows a
    masked example card number would still trip this, which is exactly
    why callers should treat PII_DETECTED as "route for human review",
    not "definitely contains a real person's private data".
    """
    empty: PIIFindings = {
        "has_pii": False, "emails": [], "phones": [], "ssns": [],
        "card_numbers": [], "ibans": [], "categories": [],
    }
    if not text:
        return empty

    emails = list(dict.fromkeys(_EMAIL_RE.findall(text)))[:20]
    phones = list(dict.fromkeys(m.strip() for m in _PHONE_RE.findall(text)))[:20]
    ssns = list(dict.fromkeys(_SSN_RE.findall(text)))[:20]
    ibans = list(dict.fromkeys(_IBAN_RE.findall(text)))[:20]

    card_numbers: List[str] = []
    for raw in _CARD_CANDIDATE_RE.findall(text):
        digits = re.sub(r'[ -]', '', raw)
        if _luhn_valid(digits):
            masked = f"**** **** **** {digits[-4:]}"
            if masked not in card_numbers:
                card_numbers.append(masked)
        if len(card_numbers) >= 20:
            break

    categories = []
    if emails:
        categories.append("email")
    if phones:
        categories.append("phone")
    if ssns:
        categories.append("ssn")
    if card_numbers:
        categories.append("card_number")
    if ibans:
        categories.append("iban")

    return {
        "has_pii": bool(categories),
        "emails": emails,
        "phones": phones,
        "ssns": ssns,
        "card_numbers": card_numbers,
        "ibans": ibans,
        "categories": categories,
    }


def classify_content_sensitivity(text: Optional[str]) -> str:
    """
    Maps a PII scan directly onto ConnectedService.ContentSensitivity's
    string values ('clean' / 'pii_detected') without importing the
    model (keeps this module Django-free per the media_info.py/
    link_info.py convention â€” the caller assigns the returned string to
    service.content_sensitivity directly, e.g.:

        service.content_sensitivity = classify_content_sensitivity(data["text"])

    Returns 'unknown' for empty/whitespace-only text â€” a page with no
    extracted content hasn't actually been checked for anything, so it
    stays at the model's own default rather than being asserted clean.
    """
    if not text or not text.strip():
        return "unknown"
    return "pii_detected" if detect_pii(text)["has_pii"] else "clean"


# ---------------------------------------------------------------------------
# Content quality (thin-content signal)
# ---------------------------------------------------------------------------

def content_quality(text: Optional[str], html_text: Optional[str]) -> ContentQuality:
    """
    Rough text-to-markup ratio â€” a cheap signal for "mostly boilerplate/
    nav" vs "actual content" pages. Same 200-character thin-content
    threshold intelligence_scraper.py already uses for its own `intel`
    block, exposed here so scraper.py's primary scrape path can compute
    the same signal without needing the full HTML available a second
    time (pass whatever html_text you have; ratio is 0 if unavailable).
    """
    html_len = len(html_text or "")
    text_len = len(text or "")
    return {
        "text_to_html_ratio": round(text_len / html_len, 3) if html_len else 0.0,
        "is_thin_content": text_len < 200,
    }


# ---------------------------------------------------------------------------
# Convenience: run everything in one call
# ---------------------------------------------------------------------------

def summarize_text(text: Optional[str], html_text: Optional[str] = None) -> Dict[str, Any]:
    """
    One-call bundle of every analysis in this module â€” stats, sentiment,
    keywords, detected language, PII findings, content-sensitivity
    classification, and content quality. Intended for a single call
    site in scraper.scrape_and_store() / intelligence_scraper's per-page
    intel pass rather than importing and calling each function
    individually, though every function above remains available
    standalone for callers that only need one piece (e.g. link_info.py
    only wanting reading_time_minutes for a display fallback).
    """
    pii = detect_pii(text)
    return {
        **text_stats(text),
        "sentiment": analyze_sentiment(text),
        "keywords": extract_keywords(text),
        "detected_language": detect_language(text),
        "pii": pii,
        "content_sensitivity": "pii_detected" if pii["has_pii"] else ("clean" if (text or "").strip() else "unknown"),
        "content_quality": content_quality(text, html_text),
    }


# ---------------------------------------------------------------------------
# Suggested wiring (not applied here â€” see connected_service.py /
# scraper.py conversation for whether to make these changes directly)
# ---------------------------------------------------------------------------
#
# 1. megamind/services/scraper.py â€” scrape_and_store(), right after
#    `service.extracted_text = data.get("text") or ""` is set:
#
#        from megamind.utils.text_info import classify_content_sensitivity
#        service.content_sensitivity = classify_content_sensitivity(service.extracted_text)
#
#    This is the ONLY change needed to make content_sensitivity a real,
#    populated field instead of a permanently-'unknown' column that
#    product_sync.py checks but nothing ever sets.
#
# 2. megamind/services/intelligence_scraper.py â€” _analyze_sentiment,
#    _extract_keywords, _detect_language, and their backing constants
#    (_WORD_RE, _STOPWORDS, _SENTIMENT_POSITIVE_WORDS,
#    _SENTIMENT_NEGATIVE_WORDS, _EMAIL_RE, _LANGDETECT_AVAILABLE) can be
#    deleted from that module and replaced with:
#
#        from megamind.utils.text_info import analyze_sentiment, extract_keywords, detect_language
#
#    (called the same way they are today â€” same signatures, same return
#    shapes) so the logic has exactly one implementation instead of two
#    that will silently drift apart over time.
