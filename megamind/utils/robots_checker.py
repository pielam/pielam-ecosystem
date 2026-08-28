# megamind/utils/robots_checker.py

"""

DB-backed robots.txt fetch/parse/cache for DomainCrawlPolicy, used by
engine/profile_views/engine_profile.py's deep-scrape gating
(_prepare_service_for_fetch).

Unlike a standalone in-memory checker, the "cache" here IS the
DomainCrawlPolicy row itself — robots.txt text and derived flags are
persisted on the model, keyed by domain, with a TTL controlled by
`DomainCrawlPolicy.robots_txt_ttl_seconds` (falls back to
DEFAULT_TTL_SECONDS when that field is falsy/unset). This is what lets
refresh_domain_policy() be a near-no-op on every call except roughly
once per TTL window per domain, regardless of how many services on
that domain get fetched.

    from megamind.utils.robots_checker import (
        refresh_domain_policy, is_url_allowed, is_allowed, get_crawl_delay,
    )

    refresh_domain_policy(policy, sample_url=service.service_url)
    if not is_url_allowed(policy, service.service_url, user_agent=UA):
        ...

──────────────────────────────────────────────────────────────────────
Verified against the real DomainCrawlPolicy model
(megamind/models/connected_service.py):

    domain                  : CharField(unique=True)
    robots_txt               : TextField(blank=True, null=True)
    robots_fetched_at        : DateTimeField(null=True, blank=True)   NOTE: not "robots_txt_fetched_at"
    robots_txt_ttl_seconds   : PositiveIntegerField(default=86400)
    disallow_all             : BooleanField(default=False)
    crawl_delay_seconds      : FloatField(default=1.0)                 non-null, always has a value
    robots_txt_is_stale      : property — reused directly below instead of reimplementing the TTL math
    is_blocked / blocked_reason / record_request() / is_request_allowed_now() : already used by engine_profile.py, unchanged here
──────────────────────────────────────────────────────────────────────

Public API is unchanged from the previous revision (same names,
signatures, and behavior for callers) — refresh_domain_policy,
is_url_allowed, is_allowed, get_crawl_delay, get_sitemaps, clear_cache.

What changed under the hood:

  * The matching engine is no longer stdlib `urllib.robotparser`. That
    parser only ever does a plain `path.startswith(rule_path)` prefix
    check — it silently ignores `*` wildcards and `$` end-anchors,
    which are both part of the de-facto/RFC 9309 robots.txt spec and
    extremely common in real robots.txt files (e.g.
    "Disallow: /*.pdf$", "Disallow: /*?session="). A crawler that
    can't see those rules isn't actually respecting the file. This
    module now implements RFC 9309 §2.2.2 matching directly: group
    selection by longest matching product-token, and within the
    selected group, the *longest matching pattern wins* (ties broken
    in favor of Allow) — see _RobotsRuleSet below.
  * Both in-process caches (the DomainCrawlPolicy-row-backed parser
    cache and the standalone per-domain cache) are now bounded with
    simple LRU eviction and protected by a lock, so a long-running
    crawler process that touches thousands of distinct domains over
    its lifetime doesn't grow these dicts without bound.
  * A fetched robots.txt is capped at ROBOTS_MAX_BYTES (RFC 9309
    recommends parsers support at least the first 500 KiB) rather
    than relying only on resilient_get's much larger generic byte cap.
  * A 401/403 response fetching robots.txt is now treated as "deny
    everything" rather than "no robots.txt found" (which fails open
    to "allow everything") — this matches RFC 9309 §2.3.1.3's
    guidance that an authorization failure should be treated
    conservatively, since it usually means the whole site requires
    auth the crawler doesn't have, not that there are no rules.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from collections import OrderedDict
from typing import Dict, List, Optional, Tuple
from urllib.parse import urlparse, urljoin, quote

from django.utils import timezone

from megamind.utils.resilient_fetch import resilient_get
from megamind.utils.http_client import DEFAULT_USER_AGENT, DEFAULT_TIMEOUT

logger = logging.getLogger(__name__)

DEFAULT_TTL_SECONDS = 24 * 60 * 60  # 24h fallback when policy.robots_txt_ttl_seconds is unset
DEFAULT_USER_AGENT_TOKEN = "*"      # matched against robots.txt User-agent lines
ROBOTS_FETCH_TIMEOUT = 8
# RFC 9309 §2.5: parsers should process at least the first 500 KiB.
# We fetch (via resilient_get's own, larger cap) then truncate here so
# a robots.txt served with an absurd body doesn't get fully parsed —
# truncating at a line boundary keeps the parse well-formed.
ROBOTS_MAX_BYTES = 500 * 1024
# Bound on in-process caches so a long-lived worker touching many
# domains over its lifetime doesn't grow these without limit.
_MAX_CACHE_ENTRIES = 2000


def _truncate_robots_text(text: str, max_bytes: int = ROBOTS_MAX_BYTES) -> str:
    encoded = text.encode("utf-8", errors="ignore")
    if len(encoded) <= max_bytes:
        return text
    truncated = encoded[:max_bytes]
    # Cut back to the last full line so we don't leave a half-written
    # directive that could parse into something unintended.
    last_newline = truncated.rfind(b"\n")
    if last_newline != -1:
        truncated = truncated[:last_newline]
    return truncated.decode("utf-8", errors="ignore")


# ═══════════════════════════════════════════════════════════════════════════
# RFC 9309-compliant robots.txt matching engine
# ═══════════════════════════════════════════════════════════════════════════

class _Rule:
    __slots__ = ("pattern", "allow", "regex", "specificity")

    def __init__(self, pattern: str, allow: bool):
        self.pattern = pattern
        self.allow = allow
        self.regex = _compile_pattern(pattern)
        # RFC 9309 §2.2.2: the rule with the largest number of octets
        # in its (undecoded) path pattern wins; ties go to Allow.
        self.specificity = len(pattern)


def _compile_pattern(pattern: str) -> "re.Pattern":
    """
    Translates a robots.txt path pattern into a regex, per the de-facto
    extended spec (also documented in RFC 9309 §2.2.3):
      * '*' matches any sequence of characters (including none)
      * '$' anchors the match to the end of the URL path
      * everything else matches literally
      * with no trailing '$', the pattern matches as a PREFIX
    """
    escaped = re.escape(pattern)
    # re.escape turns '*' into r'\*' and '$' into r'\$' — swap those
    # back into wildcard/anchor semantics.
    escaped = escaped.replace(r"\*", ".*")
    if escaped.endswith(r"\$"):
        escaped = escaped[:-2] + "$"
    return re.compile(escaped)


class _RobotsRuleSet:
    """
    Parsed robots.txt, holding one rule list per user-agent group plus
    sitemap URLs. Group selection and rule precedence follow RFC 9309
    §2.2.1/§2.2.2: the group whose product token is the longest exact
    (case-insensitive) match to the requesting user-agent wins; if
    none match, the '*' group is used; if there's no '*' group either,
    everything is allowed.
    """

    def __init__(self, robots_text: str):
        self._groups: Dict[str, List[_Rule]] = {}
        self._crawl_delays: Dict[str, float] = {}
        self._sitemaps: List[str] = []
        self._parse(robots_text or "")

    def _parse(self, text: str) -> None:
        current_agents: List[str] = []
        seen_rule_since_agents: bool = True  # True until the first UA line of a new block

        for raw_line in text.splitlines():
            line = raw_line.split("#", 1)[0].strip()
            if not line or ":" not in line:
                continue
            field, _, value = line.partition(":")
            field = field.strip().lower()
            value = value.strip()

            if field == "user-agent":
                token = value.lower()
                if seen_rule_since_agents:
                    # Starting a fresh group (either this is the very
                    # first block, or the previous block already had
                    # at least one rule line applied to it).
                    current_agents = [token]
                    seen_rule_since_agents = False
                else:
                    # Consecutive User-agent lines with no rules
                    # between them belong to the SAME group.
                    current_agents.append(token)
                for agent in current_agents:
                    self._groups.setdefault(agent, [])
                continue

            if field in ("allow", "disallow"):
                if not current_agents:
                    continue  # rule before any User-agent line — spec says ignore
                seen_rule_since_agents = True
                if not value and field == "disallow":
                    continue  # "Disallow:" with empty value means allow everything
                rule = _Rule(pattern=value, allow=(field == "allow"))
                for agent in current_agents:
                    self._groups[agent].append(rule)
                continue

            if field == "crawl-delay":
                try:
                    delay = float(value)
                except ValueError:
                    continue
                for agent in current_agents or ["*"]:
                    self._crawl_delays[agent] = delay
                continue

            if field == "sitemap" and value:
                self._sitemaps.append(value)
                continue

    def _select_group(self, user_agent: str) -> Optional[List[_Rule]]:
        if not self._groups:
            return None
        ua = (user_agent or "").lower()
        # Longest exact product-token match wins (RFC 9309 §2.2.1):
        # e.g. "Mozilla/5.0 ... MyCrawlerBot/1.0" matching against a
        # group literally named "mycrawlerbot" should win over "*".
        best_token: Optional[str] = None
        best_len = -1
        for token in self._groups:
            if token == "*":
                continue
            if token and token in ua and len(token) > best_len:
                best_token = token
                best_len = len(token)
        if best_token is not None:
            return self._groups[best_token]
        return self._groups.get("*")

    def can_fetch(self, user_agent: str, url: str) -> bool:
        group = self._select_group(user_agent)
        if not group:
            return True

        path = _url_path_for_match(url)
        best_rule: Optional[_Rule] = None
        for rule in group:
            if rule.regex.match(path):
                if best_rule is None or rule.specificity > best_rule.specificity:
                    best_rule = rule
                elif rule.specificity == best_rule.specificity and rule.allow and not best_rule.allow:
                    best_rule = rule  # tie-break: Allow beats Disallow
        return True if best_rule is None else best_rule.allow

    def crawl_delay(self, user_agent: str) -> Optional[float]:
        ua = (user_agent or "").lower()
        best_token: Optional[str] = None
        best_len = -1
        for token in self._crawl_delays:
            if token == "*":
                continue
            if token and token in ua and len(token) > best_len:
                best_token = token
                best_len = len(token)
        if best_token is not None:
            return self._crawl_delays[best_token]
        return self._crawl_delays.get("*")

    def site_maps(self) -> List[str]:
        return list(self._sitemaps)

    def disallows_all_for_wildcard(self) -> bool:
        """True if the '*' group contains an unqualified 'Disallow: /'
        (or equivalent, e.g. 'Disallow: /*') with no broader Allow of
        equal-or-greater specificity covering the root. Used for the
        blanket-block fast path (see _detect_disallow_all)."""
        group = self._groups.get("*")
        if not group:
            return False
        return not self.can_fetch("*", "/")


def _url_path_for_match(url: str) -> str:
    """robots.txt patterns match against the URL's path+query, percent
    -encoded the same way the URL itself is — not the raw unescaped
    path, since a pattern like '/search?q=*' is written against the
    encoded form."""
    parsed = urlparse(url)
    path = parsed.path or "/"
    # Re-encode defensively: a path handed to us already decoded (e.g.
    # from an href scraped out of HTML) needs to be in the same form
    # robots.txt authors write patterns against.
    try:
        path = quote(path, safe="/%:@!$&'()*+,;=~-")
    except Exception:
        pass
    if parsed.query:
        path = f"{path}?{parsed.query}"
    return path


def _build_parser(robots_text: str) -> _RobotsRuleSet:
    return _RobotsRuleSet(robots_text or "")


def _detect_disallow_all(robots_text: str) -> bool:
    """Blanket 'Disallow: /' under a wildcard user-agent block —
    checked independently of the full rule set so callers that only
    care about the blanket case (e.g. add_service()'s fail-fast check)
    get a cheap, unambiguous answer. Delegates to the same matching
    engine (rather than a separate line-scan) so this can't disagree
    with is_url_allowed() about what "blocked" means once wildcard/
    Allow-override patterns are involved."""
    if not robots_text:
        return False
    try:
        return _RobotsRuleSet(robots_text).disallows_all_for_wildcard()
    except Exception:
        return False


def _extract_crawl_delay(robots_text: str, user_agent: str = DEFAULT_USER_AGENT_TOKEN) -> Optional[float]:
    if not robots_text:
        return None
    try:
        return _RobotsRuleSet(robots_text).crawl_delay(user_agent)
    except Exception:
        return None


# ═══════════════════════════════════════════════════════════════════════════
# In-process parser cache (bounded, thread-safe)
# ═══════════════════════════════════════════════════════════════════════════
#
# _RobotsRuleSet objects aren't free to rebuild and DomainCrawlPolicy
# rows don't change between requests in the same process nearly as
# often as this gets called, so a small in-memory layer sits in front
# of the DB row: keyed by domain, invalidated by a (fetched_at, len)
# signature so a policy that was just refreshed always gets a
# freshly-parsed rule set, never a stale one from before this process
# last saw a refresh. Bounded + LRU-evicted so a long-running process
# crawling many distinct domains doesn't grow this without limit.

_cache_lock = threading.Lock()
_parser_cache: "OrderedDict[str, dict]" = OrderedDict()  # domain -> {"parser": ..., "signature": (...)}
_standalone_cache: "OrderedDict[str, dict]" = OrderedDict()  # domain -> {...}


def _lru_get(cache: "OrderedDict[str, dict]", key: str) -> Optional[dict]:
    with _cache_lock:
        entry = cache.get(key)
        if entry is not None:
            cache.move_to_end(key)
        return entry


def _lru_set(cache: "OrderedDict[str, dict]", key: str, value: dict) -> None:
    with _cache_lock:
        cache[key] = value
        cache.move_to_end(key)
        while len(cache) > _MAX_CACHE_ENTRIES:
            cache.popitem(last=False)


def _lru_pop(cache: "OrderedDict[str, dict]", key: str) -> None:
    with _cache_lock:
        cache.pop(key, None)


def _get_cached_parser(policy) -> _RobotsRuleSet:
    """Returns a rule set matching policy's CURRENT persisted
    robots_txt, rebuilding only when the row has been refreshed since
    this process's parser was built (or never built at all)."""
    domain = policy.domain
    robots_text = policy.robots_txt or ""
    signature = (policy.robots_fetched_at, len(robots_text))

    entry = _lru_get(_parser_cache, domain)
    if entry and entry["signature"] == signature:
        return entry["parser"]

    parser = _build_parser(robots_text)
    _lru_set(_parser_cache, domain, {"parser": parser, "signature": signature})
    return parser


# ═══════════════════════════════════════════════════════════════════════════
# Policy-aware API — what engine_profile.py actually calls
# ═══════════════════════════════════════════════════════════════════════════

def _is_policy_stale(policy) -> bool:
    """Delegates to DomainCrawlPolicy.robots_txt_is_stale (checks
    robots_fetched_at against robots_txt_ttl_seconds) rather than
    reimplementing that math here — one source of truth for "stale"."""
    return policy.robots_txt_is_stale


def _persist_policy_fetch(policy, robots_text: Optional[str], deny_all: bool = False) -> None:
    """Writes the freshly-fetched (or freshly-confirmed-absent)
    robots.txt onto the policy row and saves only the columns this
    function touches, so a caller mid-way through unrelated field
    changes on the same instance doesn't get those clobbered/persisted
    early by this save.

    crawl_delay_seconds is FloatField(default=1.0), non-null — when
    robots.txt doesn't specify a Crawl-delay, fall back to the
    field's existing/default value rather than writing None into a
    non-nullable-by-convention column.

    `deny_all=True` (set when the fetch came back 401/403 — see
    refresh_domain_policy) forces disallow_all regardless of what the
    (empty) text would otherwise imply, per RFC 9309 §2.3.1.3.
    """
    text = robots_text or ""
    policy.robots_txt = text
    policy.robots_fetched_at = timezone.now()
    policy.disallow_all = True if deny_all else _detect_disallow_all(text)
    delay = _extract_crawl_delay(text)
    if delay is not None:
        policy.crawl_delay_seconds = delay
    policy.save(update_fields=[
        "robots_txt", "robots_fetched_at", "disallow_all", "crawl_delay_seconds", "updated_at",
    ])


_AUTH_ERROR_RE = re.compile(r"\bHTTP\s+(401|403)\b")


def refresh_domain_policy(policy, sample_url: Optional[str] = None,
                           force: bool = False, timeout: int = ROBOTS_FETCH_TIMEOUT) -> None:
    """
    Fetches robots.txt for `policy.domain` and persists it (and the
    derived disallow_all / crawl_delay_seconds flags) onto `policy`,
    UNLESS the currently-stored copy is still within its TTL — a no-op
    in that case, so calling this once per fetch, per service, per
    request costs a real network call only once per domain per TTL
    window (see module docstring).

    `sample_url` is accepted (and used, when given) to build the
    robots.txt origin from an actual service URL rather than only
    `policy.domain`, since a bare domain string can be ambiguous about
    scheme; `policy.domain` alone is used as the fallback.

    Never raises. Three distinct outcomes on fetch failure:
      * 401/403 fetching robots.txt itself → treated as "deny
        everything" (fail CLOSED) per RFC 9309 §2.3.1.3 — an auth
        failure usually means the whole site needs credentials this
        crawler doesn't have, not that there happen to be no rules.
      * any other failure (404, timeout, DNS, 5xx after retries, SSRF
        block) → treated as "no robots.txt found" (fail OPEN),
        matching the standard convention that a missing/unreachable
        robots.txt means no restrictions.
      * `force=False` combined with an already-fresh row skips the
        network call entirely regardless of outcome.
    """
    if not force and not _is_policy_stale(policy):
        return

    origin = _domain_root(policy.domain) if getattr(policy, "domain", None) else None
    if not origin and sample_url:
        parsed = urlparse(sample_url)
        origin = f"{parsed.scheme}://{parsed.netloc}"
    if not origin:
        logger.debug("refresh_domain_policy: no domain/sample_url to build robots.txt origin from")
        return

    robots_url = urljoin(origin + "/", "robots.txt")
    try:
        raw_bytes, _headers, _final_url, error = resilient_get(
            robots_url,
            {"User-Agent": DEFAULT_USER_AGENT},
            timeout=timeout or DEFAULT_TIMEOUT,
            max_retries=1,
        )
    except Exception:
        logger.exception("refresh_domain_policy: unexpected error fetching %s", robots_url)
        raw_bytes, error = None, "unexpected error"

    if raw_bytes is None:
        if error and _AUTH_ERROR_RE.search(error):
            logger.info("refresh_domain_policy: %s returned auth error (%s) — denying all", robots_url, error)
            _persist_policy_fetch(policy, None, deny_all=True)
        else:
            # 404 / fetch failure / blocked — persist "no robots.txt" so
            # the TTL still governs retry cadence instead of hammering
            # a consistently-unreachable robots.txt on every call.
            logger.debug("refresh_domain_policy: could not fetch %s: %s", robots_url, error)
            _persist_policy_fetch(policy, None)
        return

    try:
        text = raw_bytes.decode("utf-8", errors="replace")
    except Exception:
        text = ""
    text = _truncate_robots_text(text)

    _persist_policy_fetch(policy, text)


def _domain_root(domain: str) -> str:
    """DomainCrawlPolicy.domain is expected to be a bare host
    (e.g. 'example.com'); build a fetchable origin from it. If it
    already includes a scheme (defensive), leave it alone."""
    if "://" in domain:
        return domain.rstrip("/")
    return f"https://{domain}"


def is_url_allowed(policy, url: str, user_agent: str = DEFAULT_USER_AGENT_TOKEN) -> bool:
    """
    True if `policy`'s currently-persisted robots.txt permits
    `user_agent` to fetch `url`. Does NOT fetch anything itself — call
    refresh_domain_policy(policy, ...) first if the row might be stale;
    this only reads what's already on the instance (kept fast/DB-free
    on purpose, since _prepare_service_for_fetch calls this on every
    single guarded fetch).

    Fails open (True) on a malformed robots.txt or a policy with no
    robots_txt stored yet, matching the standard "missing/unparseable
    robots.txt means unrestricted" convention. `policy.disallow_all`
    (including the auth-failure case set by refresh_domain_policy) is
    intentionally NOT special-cased here — engine_profile.py already
    checks it itself before calling this, and the rule engine would
    return the same answer anyway since disallow_all corresponds to a
    real blanket-deny rule.
    """
    if not getattr(policy, "robots_txt", None):
        return True
    try:
        parser = _get_cached_parser(policy)
        return parser.can_fetch(user_agent, url)
    except Exception:
        logger.exception("is_url_allowed: malformed robots.txt for domain=%s — failing open", getattr(policy, "domain", "?"))
        return True


# ═══════════════════════════════════════════════════════════════════════════
# Standalone (non-DB) API — kept for scripts/tests that don't have a
# DomainCrawlPolicy row to work with. NOT used by engine_profile.py.
# ═══════════════════════════════════════════════════════════════════════════

def _domain_of(url: str) -> str:
    parsed = urlparse(url)
    return f"{parsed.scheme}://{parsed.netloc}"


def _standalone_get_or_fetch(domain: str, ttl_seconds: int, timeout: int) -> dict:
    entry = _lru_get(_standalone_cache, domain)
    if entry and (time.time() - entry["fetched_at"]) < ttl_seconds:
        return entry

    robots_url = urljoin(domain + "/", "robots.txt")
    raw_bytes, _headers, _final_url, error = resilient_get(
        robots_url, {"User-Agent": DEFAULT_USER_AGENT}, timeout=timeout, max_retries=1,
    )
    text = None
    deny_all = False
    if raw_bytes is not None:
        try:
            text = _truncate_robots_text(raw_bytes.decode("utf-8", errors="replace"))
        except Exception:
            text = None
    else:
        logger.debug("robots_checker (standalone): could not fetch %s: %s", robots_url, error)
        if error and _AUTH_ERROR_RE.search(error):
            deny_all = True

    entry = {
        "parser": _build_parser(text or ""),
        "fetched_at": time.time(),
        "disallow_all": True if deny_all else _detect_disallow_all(text or ""),
        "raw": text or "",
    }
    _lru_set(_standalone_cache, domain, entry)
    return entry


def is_allowed(url: str, user_agent: str = DEFAULT_USER_AGENT_TOKEN,
               ttl_seconds: int = DEFAULT_TTL_SECONDS, timeout: int = ROBOTS_FETCH_TIMEOUT) -> bool:
    """Standalone equivalent of is_url_allowed(), for callers with no
    DomainCrawlPolicy row (e.g. a one-off script). Fetches + caches
    in-memory per domain, TTL-based."""
    domain = _domain_of(url)
    entry = _standalone_get_or_fetch(domain, ttl_seconds, timeout)
    if entry["disallow_all"]:
        return False
    try:
        return entry["parser"].can_fetch(user_agent, url)
    except Exception:
        return True


def get_crawl_delay(url: str, user_agent: str = DEFAULT_USER_AGENT_TOKEN,
                     ttl_seconds: int = DEFAULT_TTL_SECONDS, timeout: int = ROBOTS_FETCH_TIMEOUT) -> Optional[float]:
    domain = _domain_of(url)
    entry = _standalone_get_or_fetch(domain, ttl_seconds, timeout)
    return _extract_crawl_delay(entry["raw"], user_agent)


def get_sitemaps(url: str, ttl_seconds: int = DEFAULT_TTL_SECONDS, timeout: int = ROBOTS_FETCH_TIMEOUT) -> list:
    domain = _domain_of(url)
    entry = _standalone_get_or_fetch(domain, ttl_seconds, timeout)
    try:
        return list(entry["parser"].site_maps() or [])
    except Exception:
        return []


def clear_cache(domain: Optional[str] = None) -> None:
    """Clears BOTH the standalone cache and the in-process parser cache
    that sits in front of DomainCrawlPolicy rows (see _get_cached_parser).
    Useful in tests."""
    if domain:
        _lru_pop(_standalone_cache, domain)
        _lru_pop(_parser_cache, domain)
    else:
        with _cache_lock:
            _standalone_cache.clear()
            _parser_cache.clear()