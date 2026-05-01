"""Anthropic /api/oauth/usage client with on-disk cache + graceful fallback.

Reverse-engineered endpoint also used by Claude Code's own /status
command and by ohugonnot/claude-code-statusline (MIT). Anthropic does
not advertise this as a public API and may break or block it without
notice (see HN 46625918, OpenCode incident, Oct 2025), hence every
call path is wrapped to fall back to last-good cache rather than crash.

Design constraints:
- ≤ 1 HTTP request per :data:`POLL_TTL_SECONDS` (5 min default).
  Anthropic 429s aggressive callers from ~60 s.
- Fail closed: any exception (file IO, JSON parse, HTTP, network,
  invalid response shape) returns ``None`` or the cached value with
  ``is_stale=True`` — never raises to the caller.
- Read-only credentials: we open ``~/.claude/.credentials.json`` for
  the OAuth token and never write to it. Cache stores only utilization
  numbers and timestamps — never the token.
"""
from __future__ import annotations

import json
import os
import sys
import threading
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from claude_island.core.models import QuotaSnapshot

URL = "https://api.anthropic.com/api/oauth/usage"
BETA_HEADER = "oauth-2025-04-20"
POLL_TTL_SECONDS = 300        # 5 min — user-confirmed pace, matches ohugonnot
STALE_MULTIPLIER = 3          # cache age > 15 min → flag UI as stale
HTTP_TIMEOUT = 3.0            # seconds; same as ohugonnot's curl --max-time 3

_CACHE_VERSION = 1            # bump if the cache JSON shape changes


class QuotaProvider:
    """Fetches /api/oauth/usage on demand, caching to disk between calls.

    Single instance per process — the in-process lock serialises
    concurrent calls so we don't issue parallel HTTP requests.

    The constructor does NOT touch the network or the disk; lazy I/O
    happens on the first :meth:`get` call. This keeps app startup
    fast and lets tests construct the object freely.
    """

    def __init__(
        self,
        *,
        credentials_path: Path,
        cache_path: Path,
        enabled: bool = True,
    ) -> None:
        self._credentials_path = credentials_path
        self._cache_path = cache_path
        self._enabled = enabled
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get(self) -> QuotaSnapshot | None:
        """Return the freshest QuotaSnapshot we know, or ``None`` when
        disabled / no credentials / no cache yet and refresh failed.

        Decision tree:
        - enabled=False                 → None
        - cache age ≤ TTL               → return cached (fresh)
        - cache age > TTL or no cache   → try refresh
            ↳ success                   → write cache, return fresh
            ↳ failure but cache exists  → return cached, is_stale=True
            ↳ failure and no cache      → None
        """
        if not self._enabled:
            return None

        with self._lock:
            cached = self._read_cache()
            now = datetime.now(timezone.utc)

            if cached is not None and not _is_expired(cached, now, POLL_TTL_SECONDS):
                return _snapshot_from_cache(cached, now)

            token = self._read_token()
            fresh = None
            if token is not None:
                fresh = self._fetch(token)

            if fresh is not None:
                payload = _normalise_payload(fresh, fetched_at=now)
                self._write_cache(payload)
                return _snapshot_from_cache(payload, now)

            # Refresh failed — serve last-good if we have one.
            if cached is not None:
                return _snapshot_from_cache(cached, now)
            return None

    # ------------------------------------------------------------------
    # Internal: cache I/O
    # ------------------------------------------------------------------

    def _read_cache(self) -> dict | None:
        """Return the parsed cache dict or None on any read/parse failure."""
        try:
            text = self._cache_path.read_text(encoding="utf-8")
            payload = json.loads(text)
        except (OSError, ValueError):
            return None
        # Version mismatch → ignore; will be overwritten on next successful fetch.
        if not isinstance(payload, dict) or payload.get("version") != _CACHE_VERSION:
            return None
        return payload

    def _write_cache(self, payload: dict) -> None:
        """Atomic write: tmp file + os.replace so a crash mid-write
        leaves either the old cache or the new, never a half-file."""
        try:
            self._cache_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._cache_path.with_suffix(self._cache_path.suffix + ".tmp")
            tmp.write_text(json.dumps(payload), encoding="utf-8")
            os.replace(tmp, self._cache_path)
        except OSError as e:
            # Cache write failure is non-fatal — we just won't persist
            # this fetch. Worst case we re-issue the HTTP call next time.
            print(f"[claude-island] quota cache write failed: {e}", file=sys.stderr)

    # ------------------------------------------------------------------
    # Internal: credentials
    # ------------------------------------------------------------------

    def _read_token(self) -> str | None:
        """Pull ``claudeAiOauth.accessToken`` from Claude Code's
        credentials file. Returns None if the file is missing, malformed,
        or doesn't have the expected key."""
        try:
            text = self._credentials_path.read_text(encoding="utf-8")
            data = json.loads(text)
        except (OSError, ValueError):
            return None
        oauth = data.get("claudeAiOauth") if isinstance(data, dict) else None
        if not isinstance(oauth, dict):
            return None
        token = oauth.get("accessToken")
        return token if isinstance(token, str) and token else None

    # ------------------------------------------------------------------
    # Internal: HTTP
    # ------------------------------------------------------------------

    def _fetch(self, token: str) -> dict | None:
        """Issue the GET. Returns the parsed JSON dict, or None on any
        HTTP/network/parse error. Never raises."""
        req = urllib.request.Request(
            URL,
            headers={
                "Authorization": f"Bearer {token}",
                "anthropic-beta": BETA_HEADER,
                "Content-Type": "application/json",
            },
            method="GET",
        )
        try:
            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
                if resp.status != 200:
                    return None
                body = resp.read()
        except (urllib.error.URLError, urllib.error.HTTPError, OSError):
            return None
        try:
            data = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            return None
        # Validate shape — both windows must be present and parseable.
        if not _has_quota_shape(data):
            return None
        return data


# ---------------------------------------------------------------------------
# Module-level helpers (pure functions; easy to unit-test in isolation)
# ---------------------------------------------------------------------------

def _has_quota_shape(data: object) -> bool:
    """Validate the API response shape before we trust / cache it."""
    if not isinstance(data, dict):
        return False
    for window_key in ("five_hour", "seven_day"):
        window = data.get(window_key)
        if not isinstance(window, dict):
            return False
        if not isinstance(window.get("utilization"), (int, float)):
            return False
        if not isinstance(window.get("resets_at"), str):
            return False
    return True


def _normalise_payload(api_response: dict, *, fetched_at: datetime) -> dict:
    """Build the on-disk cache payload from a fresh API response."""
    return {
        "version": _CACHE_VERSION,
        "fetched_at": fetched_at.isoformat(),
        "five_hour": {
            "utilization": float(api_response["five_hour"]["utilization"]),
            "resets_at": api_response["five_hour"]["resets_at"],
        },
        "seven_day": {
            "utilization": float(api_response["seven_day"]["utilization"]),
            "resets_at": api_response["seven_day"]["resets_at"],
        },
    }


def _is_expired(cached: dict, now: datetime, ttl_seconds: int) -> bool:
    """Cache freshness check used to decide whether to issue a new fetch."""
    fetched_at = _parse_iso(cached.get("fetched_at"))
    if fetched_at is None:
        return True
    return (now - fetched_at).total_seconds() > ttl_seconds


def _parse_iso(s: object) -> datetime | None:
    """Parse an ISO-8601 string to a UTC tz-aware datetime, None on failure.

    Mirrors core.jsonl_parser._parse_ts on the simpler subset we need:
    accept 'Z' suffix, force tz-aware UTC.
    """
    if not isinstance(s, str):
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _snapshot_from_cache(cached: dict, now: datetime) -> QuotaSnapshot | None:
    """Materialise a QuotaSnapshot from the on-disk cache dict.

    Returns None if the cache is unreadable enough that we can't fill
    the dataclass — caller treats that as "no quota available".
    """
    fetched_at = _parse_iso(cached.get("fetched_at"))
    five_hour = cached.get("five_hour")
    seven_day = cached.get("seven_day")
    if not (isinstance(five_hour, dict) and isinstance(seven_day, dict)
            and fetched_at is not None):
        return None
    five_resets = _parse_iso(five_hour.get("resets_at"))
    seven_resets = _parse_iso(seven_day.get("resets_at"))
    if five_resets is None or seven_resets is None:
        return None
    age_seconds = (now - fetched_at).total_seconds()
    return QuotaSnapshot(
        five_hour_pct=float(five_hour["utilization"]),
        five_hour_resets_at=five_resets,
        seven_day_pct=float(seven_day["utilization"]),
        seven_day_resets_at=seven_resets,
        fetched_at=fetched_at,
        is_stale=age_seconds > POLL_TTL_SECONDS * STALE_MULTIPLIER,
    )
