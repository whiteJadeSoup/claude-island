"""MiniMax quota provider.

Endpoint: ``{host}/v1/api/openplatform/coding_plan/remains``

Hosts (region-routed by the user's API key — cross-region returns the
misleading 1004 ``cookie is missing`` error):
  - CN (most users):     ``https://api.minimaxi.com`` / ``https://www.minimaxi.com``
  - International keys:  ``https://api.minimax.io``

Auth: Bearer token. We accept the token from any of:
  1. ``ANTHROPIC_AUTH_TOKEN`` environment variable (the way the user
     normally supplies it to Claude Code itself).
  2. ``providers.json`` → ``providers.minimax.auth_token``. This is the
     way the user gives the token to claude-island specifically when
     the app is launched without those env vars (e.g. from a desktop
     shortcut, not a shell).

Host selection is similar:
  1. Explicit ``providers.json`` → ``providers.minimax.base_url``
  2. Explicit ``ANTHROPIC_BASE_URL`` env (if it points to a MiniMax host)
  3. Auto-detect by trying CN then international, caching the host that
     answered with a real (non-1004) payload so subsequent fetches go
     direct.

Response shape (key fields):
  ``{"model_remains": [{ "model_name", "current_interval_total_count",
       "current_interval_usage_count", "current_weekly_total_count",
       "current_weekly_usage_count", "end_time" (ms), "weekly_end_time"
       (ms) }], "base_resp": { "status_code", "status_msg" }}``

NOTE: ``current_interval_usage_count`` is REMAINING, not used.
      ``used = total - remaining``.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from . import (
    HTTP_TIMEOUT,
    provider,
    read_env_token, read_cache, write_cache,
    get_provider_setting,
    _parse_ms, snapshot_from_cache,
)


# Module-level cache of the host that last answered with a real payload.
# Populated on the first successful fetch so subsequent calls skip the
# CN-vs-intl probing dance. Reset only on process restart — the user's
# region doesn't change at runtime.
_HOST_CACHE: str | None = None

# Probing order when nothing else specifies a host. CN first because
# (a) the Coding Plan is sold predominantly in CN and (b) CN keys 1004
# silently on the .io host with no other distinguishing signal — better
# to fail-fast on the more-common case.
_DEFAULT_HOSTS = ("https://api.minimaxi.com", "https://api.minimax.io")


def _read_token() -> str | None:
    """Token chain: env var first (matches Claude Code's own ordering),
    then providers.json. Empty string is treated as missing."""
    return read_env_token() or get_provider_setting("minimax", "auth_token")


def _candidate_hosts() -> list[str]:
    """Ordered list of base URLs to try. Most-specific source wins."""
    # 1. Explicit override in providers.json
    cfg_url = get_provider_setting("minimax", "base_url")
    if cfg_url:
        return [cfg_url.rstrip("/")]
    # 2. Explicit override via ANTHROPIC_BASE_URL env
    env = os.environ.get("ANTHROPIC_BASE_URL", "")
    if "minimaxi.com" in env:
        return ["https://api.minimaxi.com"]
    if "minimax.io" in env:
        return ["https://api.minimax.io"]
    # 3. Stick with whichever host worked last time
    if _HOST_CACHE:
        return [_HOST_CACHE]
    # 4. Probe both regions, CN first
    return list(_DEFAULT_HOSTS)


def _path() -> str:
    return "/v1/api/openplatform/coding_plan/remains"


def _is_auth_error(payload: dict) -> bool:
    """MiniMax wraps auth/region failures as a 200 OK with a non-zero
    ``base_resp.status_code`` (commonly 1004 ``cookie is missing``).
    Treat any non-zero status as "this host won't authenticate us"
    so the caller can try the next candidate host.
    """
    base = payload.get("base_resp")
    if not isinstance(base, dict):
        # No base_resp at all → treat as success and let _normalise sort it out.
        return False
    code = base.get("status_code")
    return isinstance(code, int) and code != 0


def _find_coding_model(models: list) -> dict | None:
    """Return the primary coding-plan model entry from ``model_remains``.

    Prefers the ``MiniMax-M*`` wildcard / ``MiniMax-M…`` family (the
    interactive coding model); falls back to anything that looks like a
    coding plan; last resort returns the first entry.
    """
    for m in models:
        name = str(m.get("model_name", ""))
        if name.startswith("MiniMax-M"):
            return m
    for m in models:
        name = str(m.get("model_name", ""))
        if any(k in name for k in ("coding", "plan", "vlm", "search")):
            return m
    return models[0] if models else None


@provider("minimax")
class MiniMaxProvider:
    name = "minimax"

    @classmethod
    def default_config(cls) -> dict:
        """Seed block for ``providers.json`` → ``providers.minimax``.

        Empty ``auth_token`` so the tab does NOT appear until the user
        pastes a key in. ``base_url`` defaults to the CN host because
        the Coding Plan is sold predominantly in CN; international users
        flip it to ``api.minimax.io``. Auto-included by the package's
        ``_build_default_config()`` — no manual wiring."""
        return {
            "_help": (
                "Paste your MiniMax sk-cp-... Coding-Plan key into "
                "auth_token below. Get one at https://platform.minimaxi.com . "
                "The MiniMax tab appears in the 5h card once auth_token is "
                "non-empty. base_url is optional — leave the default "
                "(api.minimaxi.com, CN) or set https://api.minimax.io for "
                "international keys."
            ),
            "auth_token": "",
            "base_url": "https://api.minimaxi.com",
        }

    def detect(self) -> bool:
        """Returns True when the user has *signalled* they want to use
        MiniMax — either via ``ANTHROPIC_BASE_URL`` (Claude Code's
        runtime config) OR by configuring a token in ``providers.json``.

        Used by ``ProviderEngine._detect_active()`` for the no-explicit-
        selection fallback. The tab UI passes a provider name explicitly
        instead of relying on detection.
        """
        base = os.environ.get("ANTHROPIC_BASE_URL", "")
        if "minimaxi.com" in base or "minimax.io" in base:
            return True
        # User configured a MiniMax token in providers.json — they want
        # the tab even if their shell isn't currently set up for MiniMax.
        return _read_token() is not None

    def fetch(
        self,
        *,
        cache_dir: Path,
        bypass_cache: bool = False,
    ) -> "QuotaSnapshot | None":  # noqa: F821
        """Fetch from MiniMax coding_plan/remains with disk cache."""
        cache_path = cache_dir / "minimax-quota.json"
        now = datetime.now(timezone.utc)

        if not bypass_cache:
            cached = read_cache(cache_path)
            if cached is not None:
                snap = _from_cache(cached, now)
                if snap is not None and not _is_expired(cached, now):
                    return snap

        token = _read_token()
        if not token:
            return None if bypass_cache else _from_cache(read_cache(cache_path), now)

        data = _try_hosts(token)
        if data is None:
            return None if bypass_cache else _from_cache(read_cache(cache_path), now)

        payload = _normalise(data, fetched_at=now)
        write_cache(cache_path, payload)
        return _from_cache(payload, now)


def _try_hosts(token: str) -> dict | None:
    """Walk the candidate hosts until one returns a real payload.

    Updates ``_HOST_CACHE`` on the first success so the next call skips
    the dead host. Returns the parsed JSON (with the auth-error
    response filtered out) or None if every candidate failed.
    """
    global _HOST_CACHE
    for host in _candidate_hosts():
        url = f"{host}{_path()}"
        data = _fetch_http(url, token)
        if data is None:
            continue
        if _is_auth_error(data):
            # Wrong region or invalid key for this host — silently move
            # on; the next host might be the right region. We don't
            # surface the 1004 because that label is misleading.
            continue
        _HOST_CACHE = host
        return data
    return None


def _fetch_http(url: str, token: str) -> dict | None:
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
                return None
            return json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, urllib.error.HTTPError, OSError,
            json.JSONDecodeError, UnicodeDecodeError):
        return None


def _normalise(data: dict, *, fetched_at: datetime) -> dict:
    """Normalise MiniMax response to the same shape as our cache.

    Note: MiniMax returns REMAINING count, not used. pct = (total - remaining).
    """
    models: list = data.get("model_remains") or []
    m = _find_coding_model(models)

    five_pct = 0.0
    five_resets: datetime | None = None
    seven_pct = 0.0
    seven_resets: datetime | None = None

    if m:
        five_total = int(m.get("current_interval_total_count", 0))
        five_rem = int(m.get("current_interval_usage_count", 0))
        five_used = five_total - five_rem
        if five_total > 0:
            five_pct = round(five_used / five_total * 100, 1)
        five_resets = _parse_ms(m.get("end_time"))

        seven_total = int(m.get("current_weekly_total_count", 0))
        seven_rem = int(m.get("current_weekly_usage_count", 0))
        if seven_total > 0:
            seven_used = seven_total - seven_rem
            seven_pct = round(seven_used / seven_total * 100, 1)
        seven_resets = _parse_ms(m.get("weekly_end_time"))

    return {
        "provider": "minimax",
        "fetched_at": fetched_at.isoformat(),
        "five_hour": {
            "pct": float(five_pct),
            "resets_at": five_resets.isoformat() if five_resets else None,
        },
        "seven_day": {
            "pct": float(seven_pct),
            "resets_at": seven_resets.isoformat() if seven_resets else None,
        },
    }


def _from_cache(cached: dict | None, now: datetime):
    if cached is None:
        return None
    return snapshot_from_cache(cached, provider="minimax", now=now)


def _is_expired(cached: dict, now: datetime) -> bool:
    fetched_str = cached.get("fetched_at", "")
    if not isinstance(fetched_str, str):
        return True
    try:
        dt = datetime.fromisoformat(fetched_str.replace("Z", "+00:00"))
    except ValueError:
        return True
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)
    return (now - dt).total_seconds() > 300
