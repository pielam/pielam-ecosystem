# megamind/utils/cache.py

"""
cache.py

Disk cache for bind_content() results, keyed by URL. The single
biggest speed win for a URL you might extract more than once (retries
during dev, re-processing the same link, a scheduled refresh) is not
re-fetching and re-parsing it at all.

Storage: one JSON file per URL under a cache directory, named by a
hash of the URL so any URL is a safe filename. Writes go to a temp
file in the same directory first, then an atomic rename into place
(os.replace) — a straight write-to-final-path can leave a truncated/
corrupt JSON file behind if the process is killed mid-write or two
writers race the same URL; get_cached() would degrade that to a
harmless cache miss either way, but a torn write is avoidable, not
just tolerable. TTL is checked against each entry's stored
`fetched_at`; expired entries are treated as a miss (not deleted
automatically — call purge_expired() periodically if you want that).

    from cache import cached_bind_content
    result = cached_bind_content("https://example.com")   # fetches
    result = cached_bind_content("https://example.com")   # cache hit, instant

No extra dependencies beyond the rest of this toolkit.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

DEFAULT_TTL_SECONDS = 3600  # 1 hour

try:
    # Package-qualified import, matching this module's own location
    # (megamind/utils/cache.py) when used as part of the app. Resolved
    # once at import time rather than inside cached_bind_content() on
    # every call — the try/except still lets this module be dropped
    # next to a standalone bind_content.py for dev use.
    from megamind.utils.bind_content import bind_content as _bind_content
except ImportError:
    try:
        from bind_content import bind_content as _bind_content
    except ImportError:  # pragma: no cover - genuinely missing dependency
        _bind_content = None


def _default_cache_dir() -> Path:
    """Resolved fresh on every call that doesn't get an explicit
    cache_dir, rather than baked into a function's default argument —
    a `cache_dir: Path = Path(os.environ.get(...))` default is only
    evaluated once, at function-definition time, so it would silently
    keep using whatever BIND_CONTENT_CACHE_DIR was set to at import
    time even if the environment changes afterward (a common ordering
    issue in tests, or under Django settings applied post-import)."""
    return Path(os.environ.get("BIND_CONTENT_CACHE_DIR", ".bind_content_cache"))


# Kept for callers/introspection that want "the default at import time"
# (e.g. printing it in --help output); internal functions below never
# rely on this directly — they call _default_cache_dir() fresh instead.
DEFAULT_CACHE_DIR = _default_cache_dir()


def _cache_key(url: str) -> str:
    return hashlib.sha256(url.encode("utf-8")).hexdigest()


def _cache_path(url: str, cache_dir: Path) -> Path:
    return cache_dir / f"{_cache_key(url)}.json"


def get_cached(
    url: str,
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
    cache_dir: Optional[Path] = None,
) -> Optional[Dict[str, Any]]:
    """Returns the cached result dict for `url` if present and not
    expired, else None. Never raises — a corrupt/unreadable cache file
    is treated as a miss."""
    cache_dir = cache_dir if cache_dir is not None else _default_cache_dir()
    path = _cache_path(url, cache_dir)
    if not path.exists():
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            entry = json.load(f)
        if not isinstance(entry, dict):
            # A cache file that parses as valid JSON but isn't the
            # {"_cached_at", "data"} shape we write (e.g. hand-edited,
            # or corrupted into a bare list/string) — treat like any
            # other unreadable entry rather than raising AttributeError
            # on the next line.
            logger.debug("cache: %s did not contain a JSON object, treating as miss", path)
            return None
        cached_at = entry.get("_cached_at", 0)
        if time.time() - cached_at > ttl_seconds:
            return None
        return entry.get("data")
    except (json.JSONDecodeError, OSError) as exc:
        logger.debug("cache: failed to read %s: %s", path, exc)
        return None


def set_cached(url: str, data: Dict[str, Any], cache_dir: Optional[Path] = None) -> None:
    """Writes `data` to the cache for `url`. Never raises — a failed
    cache write should never break the caller's actual result.

    Writes to a temp file in the same directory and atomically renames
    it into place, rather than writing straight to the final path —
    see the module docstring for why.
    """
    cache_dir = cache_dir if cache_dir is not None else _default_cache_dir()
    path = _cache_path(url, cache_dir)

    tmp_path = None
    try:
        cache_dir.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(dir=cache_dir, prefix=f"{path.name}.", suffix=".tmp")
        tmp_path = Path(tmp_name)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump({"_cached_at": time.time(), "url": url, "data": data}, f)
        os.replace(tmp_path, path)
        tmp_path = None  # successfully renamed away; nothing left to clean up
    except Exception as exc:
        # Broad on purpose: a non-JSON-serializable value in `data`
        # raises TypeError from json.dump, not OSError — that must be
        # swallowed here too, matching this function's "never raises"
        # contract, not just filesystem-level failures.
        logger.warning("cache: failed to write %s: %s", path, exc)
    finally:
        if tmp_path is not None:
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                pass


def purge_expired(ttl_seconds: int = DEFAULT_TTL_SECONDS, cache_dir: Optional[Path] = None) -> int:
    """Deletes expired (or corrupt/unreadable) cache files. Returns how
    many were removed. One file that can't be read or can't be deleted
    (e.g. a permissions error) is logged and skipped rather than
    aborting the rest of the purge."""
    cache_dir = cache_dir if cache_dir is not None else _default_cache_dir()
    if not cache_dir.exists():
        return 0

    removed = 0
    for path in cache_dir.glob("*.json"):
        try:
            with open(path, "r", encoding="utf-8") as f:
                entry = json.load(f)
            expired = not isinstance(entry, dict) or (time.time() - entry.get("_cached_at", 0) > ttl_seconds)
        except (json.JSONDecodeError, OSError):
            expired = True  # corrupt/unreadable — treat as garbage, remove it

        if not expired:
            continue
        try:
            path.unlink(missing_ok=True)
            removed += 1
        except OSError as exc:
            logger.debug("cache: failed to remove %s: %s", path, exc)

    return removed


def cached_bind_content(
    url: str,
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
    cache_dir: Optional[Path] = None,
    force_refresh: bool = False,
    **bind_content_kwargs: Any,
) -> Dict[str, Any]:
    """
    Cache-aware wrapper around bind_content.bind_content(). Returns the
    cached result if present and fresh; otherwise fetches, caches, and
    returns the fresh result. A result with `error` set is NOT cached
    (a transient failure shouldn't get "stuck" for the whole TTL).

    Pass force_refresh=True to skip the cache lookup and always re-fetch
    (still writes the new result to cache).
    """
    if _bind_content is None:
        raise ImportError(
            "cached_bind_content: could not import bind_content from either "
            "megamind.utils.bind_content or a top-level bind_content module"
        )

    cache_dir = cache_dir if cache_dir is not None else _default_cache_dir()

    if not force_refresh:
        cached = get_cached(url, ttl_seconds, cache_dir)
        if cached is not None:
            cached = dict(cached)
            cached["_from_cache"] = True
            return cached

    result = _bind_content(url, **bind_content_kwargs)
    result["_from_cache"] = False
    if not result.get("error"):
        set_cached(url, result, cache_dir)
    return result