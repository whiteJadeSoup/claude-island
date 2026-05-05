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

from claude_island.core.models import (
    PricingTable,
    register_model_colors,
    register_model_short_names,
    register_pricing,
)

from . import (
    HTTP_TIMEOUT,
    provider,
    read_env_token,
    get_provider_setting,
    _parse_ms,
    read_cache_state, write_cache_state,
    log_fetch_failure,
    Window,
)


# Per-Mtok rates from https://platform.minimax.io/docs/guides/pricing-paygo
# M2 / M2.1 / M2.5 / M2.7 share input ($0.30) and output ($1.20) rates;
# only cache_read varies (M2.7 = $0.06, others = $0.03). "-highspeed"
# variants are 2× input/output, identical cache rates. The Coding-Plan
# API returns "MiniMax-M*" as a wildcard model name; treat as M2.7
# (current flagship per MiniMax's own setup docs). Cache write =
# 1.25 × input falls through to the default. Length-descending substring
# match in _resolve_pricing means "MiniMax-M2.7-highspeed" picks its
# specific entry before the shorter "MiniMax-M2.7" / "MiniMax-M*".
register_pricing({
    "MiniMax-M2.7-highspeed": PricingTable(0.60, 2.40, cache_read_per_mtok=0.06),
    "MiniMax-M2.5-highspeed": PricingTable(0.60, 2.40, cache_read_per_mtok=0.03),
    "MiniMax-M2.1-highspeed": PricingTable(0.60, 2.40, cache_read_per_mtok=0.03),
    "MiniMax-M2.7":           PricingTable(0.30, 1.20, cache_read_per_mtok=0.06),
    "MiniMax-M2.5":           PricingTable(0.30, 1.20, cache_read_per_mtok=0.03),
    "MiniMax-M2.1":           PricingTable(0.30, 1.20, cache_read_per_mtok=0.03),
    "MiniMax-M*":             PricingTable(0.30, 1.20, cache_read_per_mtok=0.06),
    "MiniMax-M2":             PricingTable(0.30, 1.20, cache_read_per_mtok=0.03),
})

# Display registry — magenta family for MiniMax. The newer M2.7 line
# gets the deeper hue (premium tier convention); the lighter rose
# tone covers M2.x base models. "-highspeed" variants share the base
# colour because they're the same model just provisioned faster —
# colour is about model identity, not throughput tier.
register_model_colors({
    "MiniMax-M2.7": "#EC4899",  # magenta — newest line
    "MiniMax-M2":   "#F472B6",  # lighter rose — base M2.x
    # Fallback so anything starting with "MiniMax" reads as the family.
    "MiniMax":      "#EC4899",
})
register_model_short_names({
    # Prefix "MiniMax" so the chip reads as self-explanatory —
    # "MiniMax M2.7" rather than bare "M2.7". Anthropic models
    # (Opus/Sonnet/Haiku) are globally recognised; MiniMax
    # version codes aren't, so the provider name gives the
    # user the context a casual glance needs.
    "MiniMax-M2.7": "MiniMax M2.7",
    "MiniMax-M2.5": "MiniMax M2.5",
    "MiniMax-M2.1": "MiniMax M2.1",
    "MiniMax-M2":   "MiniMax M2",
    "MiniMax-M*":   "MiniMax",
    "MiniMax":      "MiniMax",
})


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
        """Fetch from MiniMax coding_plan/remains with disk cache.

        See ``anthropic.py:fetch`` for the QuotaCacheState transition
        rationale — the structure here is identical, with ``_try_hosts``
        replacing the single-host HTTP call.
        """
        cache_path = cache_dir / "minimax-quota.json"
        now = datetime.now(timezone.utc)
        state = read_cache_state(cache_path, fallback_provider="minimax")

        if not bypass_cache and not state.is_fetch_due(now=now):
            return state.to_snapshot(now=now)

        token = _read_token()
        if not token:
            if bypass_cache:
                return None
            log_fetch_failure(state, reason="auth token not configured", now=now)
            new_state = state.with_failed_attempt(now=now)
            write_cache_state(cache_path, new_state)
            return new_state.to_snapshot(now=now)

        data, reason = _try_hosts(token)
        if data is None:
            if bypass_cache:
                return None
            log_fetch_failure(state, reason=reason or "unknown error", now=now)
            new_state = state.with_failed_attempt(now=now)
            write_cache_state(cache_path, new_state)
            return new_state.to_snapshot(now=now)

        parsed = _parse_response(data)
        if parsed is None:
            # Response shape is missing the coding-plan model or its
            # interval/weekly counters — treat as failure rather than
            # writing a cache entry with None timestamps that the UI
            # would silently drop. Mirrors anthropic's _has_shape guard.
            if bypass_cache:
                return None
            log_fetch_failure(
                state, reason="response missing coding_plan fields", now=now,
            )
            new_state = state.with_failed_attempt(now=now)
            write_cache_state(cache_path, new_state)
            return new_state.to_snapshot(now=now)

        five_hour, seven_day = parsed
        new_state = state.with_successful_fetch(
            now=now, five_hour=five_hour, seven_day=seven_day,
        )
        write_cache_state(cache_path, new_state)
        return new_state.to_snapshot(now=now)


def _try_hosts(token: str) -> tuple[dict | None, str | None]:
    """Walk the candidate hosts until one returns a real payload.

    Returns ``(data, None)`` on success or ``(None, reason)`` on
    failure. ``reason`` summarises the multi-host attempt so the caller
    can log "all hosts failed: api.minimaxi.com → HTTP 401, api.minimax.io
    → HTTP 401" — useful when only one of two regions is gated.

    Updates ``_HOST_CACHE`` on the first success so the next call skips
    the dead host.
    """
    global _HOST_CACHE
    per_host_reasons: list[str] = []
    for host in _candidate_hosts():
        url = f"{host}{_path()}"
        data, reason = _fetch_http(url, token)
        if data is None:
            per_host_reasons.append(f"{host} → {reason}")
            continue
        if _is_auth_error(data):
            # Wrong region or invalid key for this host — record the
            # 1004 generically and move on; another region may answer.
            per_host_reasons.append(f"{host} → auth-error (wrong region?)")
            continue
        _HOST_CACHE = host
        return data, None
    return None, "; ".join(per_host_reasons) if per_host_reasons else "no candidate hosts"


def _fetch_http(url: str, token: str) -> tuple[dict | None, str | None]:
    """GET ``url`` with Bearer auth.

    Returns ``(data, None)`` on success, ``(None, reason)`` on failure.
    See ``anthropic.py:_fetch_http`` for the rationale on returning
    reason instead of printing here."""
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
                return None, f"HTTP {resp.status}"
            return json.loads(resp.read().decode("utf-8")), None
    except urllib.error.HTTPError as e:
        return None, f"HTTP {e.code} {e.reason}"
    except urllib.error.URLError as e:
        return None, f"network error: {e.reason}"
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        return None, f"bad response body ({type(e).__name__})"
    except OSError as e:
        return None, f"{type(e).__name__}: {e}"


def _parse_response(data: dict) -> tuple[Window, Window] | None:
    """Parse MiniMax response into (five_hour, seven_day) windows.

    Returns None when the response can't be turned into a complete
    reading — caller treats that as fetch failure (logs + bumps
    last_attempt_at). Reasons we may fail to parse:
      * No matching coding-plan model in model_remains
      * end_time / weekly_end_time missing or invalid

    pct = (total - remaining) / total — MiniMax returns REMAINING,
    not used.
    """
    models: list = data.get("model_remains") or []
    m = _find_coding_model(models)
    if not m:
        return None

    five_total = int(m.get("current_interval_total_count", 0))
    five_rem = int(m.get("current_interval_usage_count", 0))
    five_pct = round((five_total - five_rem) / five_total * 100, 1) if five_total > 0 else 0.0
    five_resets = _parse_ms(m.get("end_time"))

    seven_total = int(m.get("current_weekly_total_count", 0))
    seven_rem = int(m.get("current_weekly_usage_count", 0))
    seven_pct = round((seven_total - seven_rem) / seven_total * 100, 1) if seven_total > 0 else 0.0
    seven_resets = _parse_ms(m.get("weekly_end_time"))

    if five_resets is None or seven_resets is None:
        return None

    return (
        Window(pct=float(five_pct), resets_at=five_resets),
        Window(pct=float(seven_pct), resets_at=seven_resets),
    )


