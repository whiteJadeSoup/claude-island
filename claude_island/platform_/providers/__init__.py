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
import os
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Protocol, runtime_checkable

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
POLL_TTL = 300      # 5 min
STALE_MULT = 3       # 3 × TTL = stale flag


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
        print(f"[claude-island] providers.json write failed: {e}", file=sys.stderr)


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
        print(f"[claude-island] cache write failed: {e}", file=sys.stderr)


def record_failed_attempt(
    cache_path: Path, *, now: datetime, provider: str,
) -> None:
    """Negative-cache a failed quota fetch.

    Each provider's auto-refresh path checks ``is_fetch_due`` to decide
    whether to issue HTTP. Without this helper, a failed fetch left
    ``fetched_at`` unchanged, so the next ``snapshotter.wake()`` (fired
    on every JSONL ingest, file-watch event, sessions_changed, etc.)
    flagged the cache as expired and retried within milliseconds —
    flooding stderr with the same 401/429/timeout and burning the
    network on a server already saying no.

    The fix uses a SEPARATE ``last_attempt_at`` field so retry throttling
    and ``is_stale`` business-data ageing don't collide. Bumping
    ``fetched_at`` directly would suppress retries correctly but also
    keep the UI's stale-warning grey clamp from ever firing — the user
    would see "fresh" colours on data that's actually 30+ min old.

    Manual refresh (``bypass_cache=True``) skips the cache read entirely
    so the negative-cache marker doesn't gate it.
    """
    cached = read_cache(cache_path) or {}
    cached.setdefault("provider", provider)
    cached["last_attempt_at"] = now.isoformat()
    write_cache(cache_path, cached)


def is_fetch_due(cached: dict, *, now: datetime) -> bool:
    """True when the cache permits a fresh HTTP fetch.

    Consults ``last_attempt_at`` first — it covers both success and
    failure timestamps, so 5 min after a 401 the gate opens just like
    5 min after a successful refresh. Falls back to ``fetched_at`` for
    caches written before the negative-cache logic landed (older
    snapshots have no ``last_attempt_at`` yet)."""
    last = _parse_iso(cached.get("last_attempt_at", ""))
    if last is None:
        last = _parse_iso(cached.get("fetched_at", ""))
    if last is None:
        return True
    return (now - last).total_seconds() > POLL_TTL


def _fmt_ago(td: timedelta) -> str:
    """Compact human duration: ``3s``, ``47m``, ``2h 13m``, ``3h``.

    Used by ``log_fetch_failure`` to render "last attempt 5m ago" style
    timing context. Sub-second deltas round up to ``0s`` rather than
    showing fractional seconds — log readability beats precision here.
    """
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


def log_fetch_failure(
    cache_path: Path,
    *,
    now: datetime,
    provider: str,
    reason: str,
) -> None:
    """Emit a single stderr line combining failure reason with timing.

    Caller must invoke this BEFORE ``record_failed_attempt`` bumps the
    cache's ``last_attempt_at`` — otherwise the "last attempt N ago"
    reading would be 0s on every failure, which is useless. The cache
    on disk at call time still reflects the prior attempt's timestamp,
    which is exactly what the user wants to see.

    Output format::

        [claude-island] anthropic quota fetch: HTTP 401 Unauthorized — last attempt 5m ago — last success 47m ago

    Edge cases:
      * No prior attempt → "first attempt"
      * No prior success (token never worked) → "no prior success"
      * Both missing (cold cache) → "first attempt — no prior success"

    Why a single line: stderr scrolls fast on a real terminal during
    debugging; a per-failure line is greppable, but a per-failure
    paragraph is just noise.
    """
    cached = read_cache(cache_path) or {}
    last_attempt = _parse_iso(cached.get("last_attempt_at", ""))
    last_success = _parse_iso(cached.get("fetched_at", ""))
    parts = [f"[claude-island] {provider} quota fetch: {reason}"]
    parts.append(
        f"last attempt {_fmt_ago(now - last_attempt)} ago"
        if last_attempt is not None
        else "first attempt"
    )
    parts.append(
        f"last success {_fmt_ago(now - last_success)} ago"
        if last_success is not None
        else "no prior success"
    )
    safe_stderr_write(" — ".join(parts))


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


def snapshot_from_cache(
    cached: dict,
    provider: str,
    now: datetime,
) -> QuotaSnapshot | None:
    """Materialise a QuotaSnapshot from a cache dict.

    Returns None when the cache is expired or malformed.
    """
    fetched_at = _parse_iso(cached.get("fetched_at", ""))
    if fetched_at is None:
        return None
    age = (now - fetched_at).total_seconds()
    is_stale = age > POLL_TTL * STALE_MULT

    five = cached.get("five_hour", {})
    seven = cached.get("seven_day", {})
    five_resets = _parse_iso(five.get("resets_at", "")) if isinstance(five.get("resets_at"), str) else _parse_ms(five.get("resets_at"))
    seven_resets = _parse_iso(seven.get("resets_at", "")) if isinstance(seven.get("resets_at"), str) else _parse_ms(seven.get("resets_at"))

    if five_resets is None or seven_resets is None:
        return None
    if five_resets <= now:
        return None  # window expired
    return QuotaSnapshot(
        five_hour_pct=float(five.get("pct", 0)),
        five_hour_resets_at=five_resets,
        seven_day_pct=float(seven.get("pct", 0)),
        seven_day_resets_at=seven_resets,
        fetched_at=fetched_at,
        is_stale=is_stale,
        provider=provider,
    )


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
