"""Declarative multi-provider quota engine.

Architecture: the framework declares WHAT it needs (a QuotaSnapshot), each
provider declares HOW to get it. The :class:`ProviderEngine` auto-detects
the active provider and dispatches to the right implementation.

Adding a new provider (e.g. Kimi, GLM):
  1. Create ``platform_/providers/kimi.py``
  2. Implement a class with ``@provider("kimi")``
  3. Done — the engine picks it up automatically.
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Protocol, runtime_checkable

log = logging.getLogger(__name__)

from claude_island.core.models import QuotaSnapshot
from claude_island.core.safe_stderr import safe_stderr_write

_PROVIDERS: dict[str, type] = {}

# In-memory cache TTL for QuotaSnapshots, in seconds. Sits in front of
# the provider's disk cache (which has its own 5-min HTTP-fetch TTL).
# 90 s strikes a balance:
#   - Tab clicks and the 60 s heartbeat both hit memory (no JSON parse,
#     no disk read) → switching providers feels instant.
#   - Below the 5-min HTTP TTL by enough margin that even when the disk
#     cache expires, the next miss only costs one HTTP fetch, not many.
#   - Short enough that a force_refresh-driven update is reflected in
#     all subsequent reads within ~90 s, no manual invalidation needed.
_MEMORY_CACHE_TTL = 90.0

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def provider(name: str):
    """Decorator that registers a provider class.

    Usage::

        @provider("minimax")
        class MiniMaxProvider:
            name = "minimax"
            ...
    """
    def deco(cls):
        _PROVIDERS[name] = cls
        return cls
    return deco


def all_providers() -> dict[str, type]:
    """Snapshot of registered providers. Used by tests."""
    return dict(_PROVIDERS)


class ProviderEngine:
    """Declarative dispatch engine.

    Two modes:
    - ``get(provider_name=...)`` / ``force_refresh(provider_name=...)``
      → fetch from a specific provider regardless of detect(). Used by
      the UI tab so the user can flip between providers manually.
    - ``get()`` / ``force_refresh()`` (no name) → auto-pick via
      ``_detect_active()``: the first registered provider whose
      ``detect()`` returns True. Used as the no-explicit-selection
      default at startup.

    Provider detection runs on every call so the engine reacts to
    ``ANTHROPIC_BASE_URL`` changes without a restart.
    """

    def __init__(self, cache_dir: Path) -> None:
        self._cache_dir = cache_dir
        self._lock = threading.Lock()
        # In-memory cache keyed by provider name → (snapshot, fetched_at).
        # Front of the disk cache; populated on every successful fetch
        # and invalidated lazily on read when older than _MEMORY_CACHE_TTL.
        self._mem: dict[str, tuple[QuotaSnapshot, float]] = {}

    def get(self, provider_name: str | None = None) -> QuotaSnapshot | None:
        """Return a QuotaSnapshot. ``provider_name`` selects a specific
        provider; None falls back to auto-detect. Returns ``None`` when
        the provider isn't registered or its fetch fails.

        Read path: in-memory cache → provider's disk cache → HTTP. The
        in-memory hit is the common case for UI tick traffic (heartbeat
        + tab clicks); cold start or > 90 s falls through to disk."""
        prov = self._resolve(provider_name)
        if prov is None:
            return None
        cached = self._mem.get(prov.name)
        if cached is not None:
            snap, t = cached
            if time.time() - t < _MEMORY_CACHE_TTL:
                return snap
        snap = prov.fetch(cache_dir=self._cache_dir)
        if snap is not None:
            self._mem[prov.name] = (snap, time.time())
        return snap

    def force_refresh(self, provider_name: str | None = None) -> QuotaSnapshot | None:
        """Bypass cache and fetch fresh. Same selection rules as ``get``.

        Updates the in-memory cache on success, evicts on failure so a
        subsequent ``get()`` retries instead of returning a stale value
        the user explicitly asked to invalidate."""
        prov = self._resolve(provider_name)
        if prov is None:
            return None
        snap = prov.fetch(cache_dir=self._cache_dir, bypass_cache=True)
        if snap is not None:
            self._mem[prov.name] = (snap, time.time())
        else:
            self._mem.pop(prov.name, None)
        return snap

    def _resolve(self, provider_name: str | None) -> "Provider | None":
        if provider_name is None:
            return self._detect_active()
        cls = _PROVIDERS.get(provider_name)
        if cls is None:
            return None
        return cls()

    def _detect_active(self) -> "Provider | None":
        for name, cls in _PROVIDERS.items():
            inst = cls()
            if inst.detect():
                return inst
        return None


# ---------------------------------------------------------------------------
# Provider protocol
# ---------------------------------------------------------------------------

class Provider(Protocol):
    """Contract that every registered provider must fulfil."""

    name: str  # e.g. "anthropic", "minimax"

    def detect(self) -> bool:
        """Return ``True`` if this provider is currently active."""

    def fetch(
        self,
        *,
        cache_dir: Path,
        bypass_cache: bool = False,
    ) -> QuotaSnapshot | None:
        """Return a QuotaSnapshot, using a local cache if available."""

    # Optional. When implemented, returns the provider's seed entry for
    # the auto-assembled providers.json (auth_token / base_url / _help).
    # Return ``None`` if the provider needs no providers.json entry —
    # e.g. AnthropicProvider reads OAuth from ~/.claude/.credentials.json
    # so its block would just be noise.
    @classmethod
    def default_config(cls) -> dict | None:  # pragma: no cover - protocol stub
        """Return this provider's default block in providers.json, or None."""
        ...


# ---------------------------------------------------------------------------
# Shared utilities (used by provider implementations)
# ---------------------------------------------------------------------------

HTTP_TIMEOUT = 3.0
POLL_TTL = 300      # 5 min — base interval between fetch attempts
POLL_TTL_MAX = 18000  # 5 h — backoff cap for consecutive failures
STALE_MULT = 3       # 3 × TTL = stale flag

# After this many consecutive failures, auto-refresh stops issuing
# HTTP entirely — the doubling schedule alone would still ping a known-
# broken endpoint every 5 h, which is wasteful and frequently produces
# the same identical 401/network noise in stderr. The manual ⟳ button
# always bypasses this circuit-breaker (see fetch()'s
# ``bypass_cache=True`` path), so the user can probe a fix on demand;
# a successful manual fetch resets the counter via
# ``with_successful_fetch`` and resumes auto polling. Five attempts is
# chosen because it covers the natural backoff schedule out to ~2.5 h
# of cumulative wait — long enough to ride out transient outages,
# short enough that a persistent failure surfaces clearly to the user.
AUTO_REFRESH_FAILURE_THRESHOLD = 5

# Exponential-backoff schedule on consecutive failures: window doubles
# each failure, capped at POLL_TTL_MAX. Once
# ``consecutive_failures >= AUTO_REFRESH_FAILURE_THRESHOLD`` auto-refresh
# is paused entirely (see is_fetch_due) — the schedule below applies
# only while the circuit-breaker remains closed.
#   failures=0  →  POLL_TTL          (5 min — happy path)
#   failures=1  →  POLL_TTL × 2      (10 min)
#   failures=2  →  POLL_TTL × 4      (20 min)
#   failures=3  →  POLL_TTL × 8      (40 min)
#   failures=4  →  POLL_TTL × 16     (80 min — final auto attempt)
#   failures≥5  →  auto-refresh stops (manual ⟳ still works)
# Cumulative ~155 min before the circuit opens — long enough to ride
# out transient 429s/network blips, short enough that a persistent
# 401/network failure surfaces a clear "paused" indicator to the user
# instead of silently retrying every 5 h forever.


def read_env_token() -> str | None:
    """Read ``ANTHROPIC_AUTH_TOKEN`` from the environment.

    Used by providers (e.g. MiniMax) whose auth scheme is "user puts
    an API key in this env var". Anthropic's OAuth flow is different —
    its token lives in ``~/.claude/.credentials.json`` and the
    Anthropic provider reads it directly there. Don't centralise
    those two into one function: the auth scheme is part of the
    provider's contract, not a shared utility.
    """
    return os.environ.get("ANTHROPIC_AUTH_TOKEN") or None


# ---------------------------------------------------------------------------
# Multi-provider config (~/.claude-island/providers.json)
# ---------------------------------------------------------------------------
#
# Shape:
#   {
#     "selected": "anthropic",          // optional: which tab the UI shows
#     "providers": {
#       "minimax": {
#         "auth_token": "sk-cp-...",    // optional: MiniMax API key
#         "base_url":  "https://api.minimaxi.com"   // optional: region host
#       }
#     }
#   }
#
# Lives outside ~/.claude/ on purpose: that directory belongs to Claude Code
# itself, and the broader switcher-tool ecosystem (ccs, cc-switch, etc) treats
# it as off-limits for third-party state. Mirroring that convention also
# avoids silently breaking when Claude Code reformats its own files.

PROVIDER_CONFIG_PATH = Path.home() / ".claude-island" / "providers.json"


# Self-documenting comment string that ships at the top of the seed
# providers.json so users discover the schema without trawling the
# README. JSON has no real comment syntax, but our reader ignores any
# key it doesn't explicitly look up, so "_comment" / "_help" are safe
# to leave in the file forever.
_CONFIG_COMMENT = (
    "claude-island provider config. Anthropic is always available "
    "(reads OAuth token from ~/.claude/.credentials.json — no setup "
    "needed). To enable additional providers, edit the relevant block "
    "under 'providers' below. Schema: { selected: <provider name shown "
    "in the 5h card>, providers: { <name>: { auth_token, base_url? } } }"
)


def _build_default_config() -> dict:
    """Assemble the seed providers.json by collecting each provider's
    own ``default_config()``. Provider order follows registration order
    (anthropic, minimax, zhipu, ...). Providers that return None are
    skipped — they don't need a config block (e.g. Anthropic reads
    OAuth from ~/.claude/.credentials.json elsewhere).

    Built fresh on every call so a newly-registered provider class
    automatically contributes its block on first run, no ``__init__.py``
    edit needed."""
    blocks: dict[str, dict] = {}
    for name, cls in _PROVIDERS.items():
        cfg_fn = getattr(cls, "default_config", None)
        if cfg_fn is None:
            continue
        try:
            cfg = cfg_fn()
        except Exception:
            cfg = None
        if isinstance(cfg, dict):
            blocks[name] = cfg
    return {
        "_comment": _CONFIG_COMMENT,
        # "anthropic" by design: the default-installed provider should
        # match the most-common case. Even when other providers are
        # registered first in import order, the seed file always names
        # anthropic. This pairs with the explicit-fallback rule in
        # __main__.py so the rule "default tab is Anthropic" is one
        # coherent contract end-to-end.
        "selected": "anthropic",
        "providers": blocks,
    }


def ensure_provider_config(path: Path | None = None) -> None:
    """Write the default config to disk if the file doesn't exist.

    Idempotent: a no-op when the file is already there. Called once at
    app startup so first-time users find a self-documented file at
    ``~/.claude-island/providers.json`` instead of "where do I configure
    this?". Existing files are NEVER overwritten — the user's tokens
    and selection are sacred.
    """
    if path is None:
        path = PROVIDER_CONFIG_PATH
    if path.exists():
        return
    write_provider_config(_build_default_config(), path)


def read_provider_config(path: Path | None = None) -> dict:
    """Parse providers.json. Returns {} on any read / parse failure
    so callers can do ``cfg.get(...)`` without try/except sprinkles.

    ``path`` defaults to ``PROVIDER_CONFIG_PATH`` resolved *at call time*
    (not function-definition time) so tests can monkeypatch the module
    attribute without having to thread the path through every caller.
    """
    if path is None:
        path = PROVIDER_CONFIG_PATH
    try:
        text = path.read_text(encoding="utf-8")
        data = json.loads(text)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def write_provider_config(config: dict, path: Path | None = None) -> None:
    """Atomic write of providers.json (tmp + os.replace).

    ``path`` resolution mirrors :func:`read_provider_config` — call-time
    lookup of the module attribute, so tests can patch.

    Write failure is non-fatal — printed to stderr but does not raise.
    Worst case the user's tab selection won't persist across restarts;
    the in-process state is unaffected.
    """
    if path is None:
        path = PROVIDER_CONFIG_PATH
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(config, indent=2), encoding="utf-8")
        os.replace(tmp, path)
    except OSError as e:
        log.warning("providers.json write failed: %s", e)


def get_provider_setting(provider_name: str, key: str) -> str | None:
    """Read a string value out of ``providers[<name>][<key>]``.

    Returns None when the file, the provider entry, or the key is
    missing — callers should treat None as "user hasn't configured
    this; fall back to defaults".
    """
    cfg = read_provider_config()
    providers = cfg.get("providers")
    if not isinstance(providers, dict):
        return None
    entry = providers.get(provider_name)
    if not isinstance(entry, dict):
        return None
    val = entry.get(key)
    return val if isinstance(val, str) and val else None


def get_selected_provider() -> str | None:
    """Return the user's last-selected provider name, or None."""
    cfg = read_provider_config()
    sel = cfg.get("selected")
    return sel if isinstance(sel, str) and sel else None


def set_selected_provider(name: str) -> None:
    """Persist the user's tab choice. Merges into the existing config
    so other fields (provider tokens) aren't clobbered."""
    cfg = read_provider_config()
    cfg["selected"] = name
    write_provider_config(cfg)


def delete_provider_settings(name: str) -> None:
    """Remove a provider's block from ``providers.json``.

    Used by the right-click → Delete action on quota tabs. After this,
    the provider's ``detect()`` will fall back to env-var-only checks
    and (for token-required providers like MiniMax / Zhipu / DeepSeek)
    return False, so the tab disappears from the strip on the next
    ``_resolve_available_providers()`` call.

    No-op when the file is missing, the provider entry doesn't exist,
    or the providers map is malformed — the caller can invoke
    unconditionally without checking. Atomic write via
    :func:`write_provider_config`. Anthropic is intentionally not
    special-cased here (the UI wires the menu only for non-anthropic
    tabs); this function will happily remove any name handed to it.
    """
    cfg = read_provider_config()
    providers = cfg.get("providers")
    if not isinstance(providers, dict) or name not in providers:
        return
    providers.pop(name, None)
    # If the deleted provider was the selected one, fall back to anthropic
    # so the UI doesn't end up pointing at a removed tab on next launch.
    if cfg.get("selected") == name:
        cfg["selected"] = "anthropic"
    write_provider_config(cfg)


def set_provider_settings(name: str, fields: dict) -> None:
    """Merge ``fields`` into ``providers.json`` under ``providers[name]``.

    Round-trips the existing config so other providers' tokens, the
    ``selected`` pointer, and any user-added keys are preserved. Atomic
    write via :func:`write_provider_config`. No-op when ``fields`` is
    empty so the in-app + dialog can call it unconditionally without
    rewriting the file when the user clicks Save with no changes.

    Used by the in-app provider-add dialog so a freshly configured
    provider's auth token gets persisted without overwriting unrelated
    state (the broader "user's tokens are sacred" invariant).
    """
    if not fields:
        return
    cfg = read_provider_config()
    providers = cfg.get("providers")
    if not isinstance(providers, dict):
        providers = {}
        cfg["providers"] = providers
    block = providers.get(name)
    if not isinstance(block, dict):
        block = {}
        providers[name] = block
    block.update(fields)
    write_provider_config(cfg)


def read_oauth_token(credentials_path: Path) -> str | None:
    """Read ``claudeAiOauth.accessToken`` from Claude Code's credentials.

    Returns ``None`` when the file is missing, malformed, or doesn't
    have the expected key. This is the OAuth access token Claude
    Code maintains for the user's consumer plan; the
    ``/api/oauth/usage`` endpoint accepts it as a Bearer token.

    macOS: Claude Code does not write the file — credentials live in
    the login keychain under service ``Claude Code-credentials``. When
    the file read fails on darwin, fall back to ``/usr/bin/security``.
    The keychain item's ACL trusts that binary, so no auth prompt.
    """
    text = _read_credentials_payload(credentials_path)
    if text is None:
        return None
    try:
        data = json.loads(text)
    except ValueError:
        return None
    oauth = data.get("claudeAiOauth") if isinstance(data, dict) else None
    if not isinstance(oauth, dict):
        return None
    token = oauth.get("accessToken")
    return token if isinstance(token, str) and token else None


def _read_credentials_payload(credentials_path: Path) -> str | None:
    try:
        return credentials_path.read_text(encoding="utf-8")
    except OSError:
        pass
    if sys.platform != "darwin":
        return None
    return _read_keychain_credentials()


def _read_keychain_credentials() -> str | None:
    try:
        result = subprocess.run(
            ["/usr/bin/security", "find-generic-password",
             "-s", "Claude Code-credentials", "-w"],
            capture_output=True,
            text=True,
            timeout=2.0,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    out = result.stdout.strip()
    return out or None


def http_get(url: str, token: str) -> dict | None:
    """Issue a GET with Bearer auth. Returns parsed JSON, or ``None``.

    Each failure mode emits one stderr line so silent failures stop
    looking like "no error happened" — the per-provider _fetch_http
    helpers do the same; this generic helper mirrors that contract for
    any future provider that uses it directly."""
    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            if resp.status != 200:
                safe_stderr_write(
                    f"[claude-island] http_get {url}: HTTP {resp.status}"
                )
                return None
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        safe_stderr_write(
            f"[claude-island] http_get {url}: HTTP {e.code} {e.reason}"
        )
        return None
    except urllib.error.URLError as e:
        safe_stderr_write(
            f"[claude-island] http_get {url} failed: {e.reason}"
        )
        return None
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        safe_stderr_write(
            f"[claude-island] http_get {url}: bad response body ({type(e).__name__})"
        )
        return None
    except OSError as e:
        safe_stderr_write(
            f"[claude-island] http_get {url}: {type(e).__name__}: {e}"
        )
        return None


def read_cache(cache_path: Path) -> dict | None:
    try:
        return json.loads(cache_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def write_cache(cache_path: Path, payload: dict) -> None:
    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = cache_path.with_suffix(cache_path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload), encoding="utf-8")
        os.replace(tmp, cache_path)
    except OSError as e:
        log.warning("cache write failed: %s", e)


def _parse_iso(s: str) -> datetime | None:
    if not isinstance(s, str):
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _parse_ms(ms: int | None) -> datetime | None:
    if not isinstance(ms, (int, float)) or ms <= 0:
        return None
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc)


def _fmt_ago(td: timedelta) -> str:
    """Compact human duration: ``3s``, ``47m``, ``2h 13m``, ``3h``.

    Used by ``log_fetch_failure`` for "last attempt 5m ago" style
    timing context. Sub-second deltas round up to ``0s`` rather than
    showing fractional seconds — log readability beats precision."""
    total = int(td.total_seconds())
    if total < 0:
        total = 0
    if total < 60:
        return f"{total}s"
    if total < 3600:
        return f"{total // 60}m"
    h, rem = divmod(total, 3600)
    m = rem // 60
    return f"{h}h {m}m" if m else f"{h}h"


# ---------------------------------------------------------------------------
# Throttle-layer first-class objects
# ---------------------------------------------------------------------------
#
# QuotaCacheState is the cache layer's domain object — it represents
# everything we know about a provider's quota at the throttle layer
# (last attempt, last success, current reading). The UI layer consumes
# QuotaSnapshot, which is a strict projection of this object that drops
# fields the UI doesn't render (e.g. last_attempt_at).
#
# Layering:
#   raw HTTP dict ──► (provider's _normalise) ──► (Window, Window)
#                                                        │
#                                                        ▼
#                                      state.with_successful_fetch(...)
#                                                        │
#                                                        ▼
#                                              QuotaCacheState
#                                              ├─► to_cache_dict() → JSON
#                                              └─► to_snapshot()   → UI
#
# Why dataclasses with frozen=True + slots=True: every state transition
# (success, failure, throttle check) returns a NEW instance, never
# mutates. This makes the throttle logic side-effect-free at the type
# layer — the only mutation is write_cache_state(path, new_state) at
# the IO boundary, which is explicit and grep-able.


@dataclass(frozen=True, slots=True)
class Window:
    """One quota window (5h or weekly): a percentage + when it resets."""
    pct: float
    resets_at: datetime


@dataclass(frozen=True, slots=True)
class QuotaCacheState:
    """All persistent state the throttle/cache layer keeps per provider.

    Two kinds of fields:

    Throttle metadata (UI never sees these — they go in cache JSON only):
      * last_attempt_at — when was the last fetch attempted?
        Used by ``is_fetch_due`` to gate retries; both successes and
        failures bump it. None on first-ever fetch.
      * fetched_at — when was the last SUCCESSFUL fetch?
        Used by ``is_stale`` to grey out the UI when data is too old.
        Only successful fetches bump it.

    Business data (also goes into QuotaSnapshot for the UI):
      * five_hour / seven_day — the actual quota windows.
        None if no fetch has ever succeeded (failure-only state).

    Backoff counter (throttle metadata, persisted to cache):
      * consecutive_failures — how many failed fetches in a row?
        Drives the exponential-backoff schedule in ``is_fetch_due``.
        Bumped by ``with_failed_attempt``, reset to 0 by
        ``with_successful_fetch``. Default 0 for round-trip with caches
        written before backoff existed (no field → no prior failures).
    """

    provider: str
    last_attempt_at: datetime | None
    fetched_at: datetime | None
    five_hour: Window | None
    seven_day: Window | None
    consecutive_failures: int = 0

    # ---- Constructors --------------------------------------------------

    @classmethod
    def empty(cls, provider: str) -> "QuotaCacheState":
        """Cold-start state: never attempted, never succeeded."""
        return cls(
            provider=provider,
            last_attempt_at=None, fetched_at=None,
            five_hour=None, seven_day=None,
            consecutive_failures=0,
        )

    @classmethod
    def from_cache_dict(cls, cached: dict, *, fallback_provider: str) -> "QuotaCacheState":
        """Parse a cache JSON dict into a state.

        Tolerates partial/missing fields — a cache containing only
        ``last_attempt_at`` (first-failure marker) round-trips correctly.
        ``fallback_provider`` covers very old caches written before the
        ``provider`` field was added.
        """
        provider = str(cached.get("provider") or fallback_provider)
        last_attempt = _parse_iso(cached.get("last_attempt_at", ""))
        fetched = _parse_iso(cached.get("fetched_at", ""))

        def _parse_window(d: object) -> Window | None:
            if not isinstance(d, dict):
                return None
            resets_raw = d.get("resets_at")
            if isinstance(resets_raw, str):
                resets = _parse_iso(resets_raw)
            else:
                resets = _parse_ms(resets_raw)
            if resets is None:
                return None
            return Window(pct=float(d.get("pct", 0)), resets_at=resets)

        raw_failures = cached.get("consecutive_failures", 0)
        try:
            failures = max(0, int(raw_failures))
        except (TypeError, ValueError):
            failures = 0
        return cls(
            provider=provider,
            last_attempt_at=last_attempt,
            fetched_at=fetched,
            five_hour=_parse_window(cached.get("five_hour")),
            seven_day=_parse_window(cached.get("seven_day")),
            consecutive_failures=failures,
        )

    # ---- Serialisation -------------------------------------------------

    def to_cache_dict(self) -> dict:
        """Serialise to the cache JSON shape (datetimes → ISO 8601 str).

        Schema is the same one the previous dict-based code wrote, so
        a state-shaped writer can round-trip with caches written before
        this refactor."""
        d: dict = {"provider": self.provider}
        if self.fetched_at is not None:
            d["fetched_at"] = self.fetched_at.isoformat()
        if self.last_attempt_at is not None:
            d["last_attempt_at"] = self.last_attempt_at.isoformat()
        if self.five_hour is not None:
            d["five_hour"] = {
                "pct": self.five_hour.pct,
                "resets_at": self.five_hour.resets_at.isoformat(),
            }
        if self.seven_day is not None:
            d["seven_day"] = {
                "pct": self.seven_day.pct,
                "resets_at": self.seven_day.resets_at.isoformat(),
            }
        # Zero-suppress: caches written before backoff existed had no
        # such key, and writing 0 on every successful fetch adds noise
        # to the (often hand-inspected) cache file. The from_cache_dict
        # default already covers the round-trip.
        if self.consecutive_failures:
            d["consecutive_failures"] = self.consecutive_failures
        return d

    # ---- Throttle queries (pure functions of self + now) ---------------

    def is_fetch_due(self, *, now: datetime) -> bool:
        """True when the cache permits a fresh HTTP fetch.

        Gates on ``last_attempt_at`` (covers success AND failure) so
        the window opens once the backoff interval has passed. Falls
        back to ``fetched_at`` for caches that pre-date the negative-
        cache logic (no last_attempt_at field).

        Window length is ``POLL_TTL × 2^consecutive_failures`` clamped
        at ``POLL_TTL_MAX`` — happy path is plain POLL_TTL; each failure
        doubles the wait so a persistently broken provider doesn't burn
        a request every 5 min.

        Circuit-breaker: once ``consecutive_failures`` reaches
        ``AUTO_REFRESH_FAILURE_THRESHOLD`` we return False regardless of
        elapsed time — the auto-refresh path uses this check to decide
        whether to issue HTTP, so the auto-refresh stops entirely.
        Manual ⟳ bypasses this check at the provider level
        (``bypass_cache=True``), so the user can always probe; a manual
        success resets the counter and resumes auto polling.
        """
        if self.is_auto_refresh_paused:
            return False
        last = self.last_attempt_at or self.fetched_at
        if last is None:
            return True
        window = self._backoff_window_seconds()
        return (now - last).total_seconds() > window

    @property
    def is_auto_refresh_paused(self) -> bool:
        """True when the circuit-breaker is open — too many consecutive
        failures, auto-refresh has stopped issuing HTTP. UI uses this
        (via the snapshot's ``consecutive_failures`` field) to render
        the "auto-paused, N consecutive failures" hint."""
        return self.consecutive_failures >= AUTO_REFRESH_FAILURE_THRESHOLD

    def _backoff_window_seconds(self) -> float:
        """Current backoff interval in seconds, clamped at POLL_TTL_MAX.

        Computed as ``POLL_TTL << consecutive_failures`` (cheap integer
        shift) and clamped. ``min(failures, 30)`` bound on the shift
        prevents a wildly corrupt counter from overflowing — at 30
        shifts we'd already be ~10⁹ × POLL_TTL anyway, all clamped to
        the max."""
        shift = min(max(self.consecutive_failures, 0), 30)
        return float(min(POLL_TTL << shift, POLL_TTL_MAX))

    def is_stale(self, *, now: datetime) -> bool:
        """True when the displayed business data is too old to trust.

        Used by the UI to swap the bar colour to grey + show ⚠.
        ``fetched_at`` (NOT last_attempt_at) drives this — failure
        attempts must NOT make a 30-min-old reading look fresh."""
        if self.fetched_at is None:
            return True
        return (now - self.fetched_at).total_seconds() > POLL_TTL * STALE_MULT

    # ---- Transitions (return new state, never mutate) ------------------

    def with_failed_attempt(self, *, now: datetime) -> "QuotaCacheState":
        """Bump last_attempt_at to ``now`` so the gate re-closes for the
        next backoff window, and increment ``consecutive_failures`` so
        that window doubles. Business data and fetched_at are unchanged
        — the UI continues to show the last-known reading with rising
        is_stale until either a success refreshes it or the window
        ages out."""
        return replace(
            self,
            last_attempt_at=now,
            consecutive_failures=self.consecutive_failures + 1,
        )

    def with_successful_fetch(
        self,
        *,
        now: datetime,
        five_hour: Window,
        seven_day: Window,
    ) -> "QuotaCacheState":
        """Replace business data with a fresh reading. Both timestamps
        move to ``now`` so is_fetch_due gates the next attempt to
        POLL_TTL after THIS success — not POLL_TTL after some stale
        prior attempt. ``consecutive_failures`` resets to 0 so the next
        cycle resumes the happy-path 5-min cadence — applies equally to
        auto fetches and manual ⟳ that happened to succeed."""
        return replace(
            self,
            fetched_at=now, last_attempt_at=now,
            five_hour=five_hour, seven_day=seven_day,
            consecutive_failures=0,
        )

    # ---- UI projection -------------------------------------------------

    def to_snapshot(self, *, now: datetime) -> QuotaSnapshot | None:
        """Project to the UI's QuotaSnapshot model.

        Returns None if there's no business data to show (cold cache,
        or first attempt failed). Returns None ALSO when the cached
        five-hour window has already passed — the data has logically
        rolled over, so showing it would mislead the user.

        Does NOT carry last_attempt_at across — that field is throttle
        metadata, deliberately invisible to the UI (see module-level
        comment for layering rationale).
        """
        if self.fetched_at is None or self.five_hour is None or self.seven_day is None:
            return None
        if self.five_hour.resets_at <= now:
            return None  # window expired, the cached reading is stale beyond use
        return QuotaSnapshot(
            five_hour_pct=self.five_hour.pct,
            five_hour_resets_at=self.five_hour.resets_at,
            seven_day_pct=self.seven_day.pct,
            seven_day_resets_at=self.seven_day.resets_at,
            fetched_at=self.fetched_at,
            is_stale=self.is_stale(now=now),
            provider=self.provider,
            consecutive_failures=self.consecutive_failures,
            is_auto_refresh_paused=self.is_auto_refresh_paused,
        )


# ---------------------------------------------------------------------------
# Cache I/O — typed (state-based) helpers
# ---------------------------------------------------------------------------


def read_cache_state(
    cache_path: Path, *, fallback_provider: str,
) -> QuotaCacheState:
    """Read the cache file and parse into a QuotaCacheState.

    Returns ``QuotaCacheState.empty(fallback_provider)`` when the file
    doesn't exist or is malformed — callers don't need a None branch.
    The fallback_provider covers cold-start where there's no prior
    cache to learn the name from."""
    cached = read_cache(cache_path)
    if cached is None:
        return QuotaCacheState.empty(fallback_provider)
    return QuotaCacheState.from_cache_dict(cached, fallback_provider=fallback_provider)


def write_cache_state(cache_path: Path, state: QuotaCacheState) -> None:
    """Atomic write — see ``write_cache`` for the tmp + os.replace dance."""
    write_cache(cache_path, state.to_cache_dict())


# ---------------------------------------------------------------------------
# Diagnostics
# ---------------------------------------------------------------------------


def log_fetch_failure(
    prior: QuotaCacheState, *, reason: str, now: datetime,
) -> None:
    """Emit a single stderr line: failure reason + timing context.

    Caller must pass the state BEFORE bumping it via
    ``with_failed_attempt`` — the helper reads
    ``prior.last_attempt_at`` as the prior-attempt timestamp, which a
    bumped state would have already overwritten with ``now``.

    Output format::

        [2026-05-16 14:23:45] [claude-island] anthropic quota fetch: HTTP 401 Unauthorized — last attempt 5m ago — last success 47m ago

    The leading bracket is the LOCAL wall-clock timestamp of the
    failure (``now`` converted to the host's local timezone). Stderr
    scrollback otherwise gives only relative ages — useless when the
    user comes back after lunch and wants to know *when* it actually
    broke. We accept the ~22-char overhead because every failure line
    is a discrete event the user needs to anchor in real time.

    Edge cases:
      * No prior attempt → "first attempt"
      * No prior success (token never worked) → "no prior success"
      * Both missing (cold cache) → "first attempt — no prior success"
    """
    # now arrives tz-aware in UTC from callers; astimezone() with no
    # arg converts to the host's local zone, matching what the user
    # reads on their system clock.
    local_ts = now.astimezone().strftime("%Y-%m-%d %H:%M:%S")
    parts = [f"[{local_ts}] [claude-island] {prior.provider} quota fetch: {reason}"]
    parts.append(
        f"last attempt {_fmt_ago(now - prior.last_attempt_at)} ago"
        if prior.last_attempt_at is not None
        else "first attempt"
    )
    parts.append(
        f"last success {_fmt_ago(now - prior.fetched_at)} ago"
        if prior.fetched_at is not None
        else "no prior success"
    )
    safe_stderr_write(" — ".join(parts))


# ---------------------------------------------------------------------------
# Side-effect imports: each sub-module registers its provider class via the
# @provider decorator at import time. Without these imports, _PROVIDERS is
# empty in production and ProviderEngine.get() always returns None — the
# 5h-session progress bar then silently disappears. Tests don't catch this
# because they import the sub-modules explicitly.
#
# Placed at the bottom because the decorator's `provider` and the cache /
# HTTP helpers must be defined before the sub-modules import them.
# ---------------------------------------------------------------------------
from . import anthropic, minimax, zhipu, deepseek  # noqa: F401, E402
