"""Zhipu Z.AI / GLM Coding Plan quota provider.

Endpoint: ``{host}/api/monitor/usage/quota/limit``
Default host: ``https://api.z.ai`` (international). Override via
``providers.json`` → ``providers.zhipu.base_url`` for the 国内
``https://open.bigmodel.cn`` endpoint should Zhipu ever add CN routing
for the coding-plan quota API (currently single-region).

Auth: API key in the ``Authorization`` header **without** a ``Bearer``
prefix. Verified against cc-switch v3.14.1 (``src-tauri/src/services/
coding_plan.rs``), which carries an explicit comment on this exact
gotcha — Anthropic and MiniMax both use ``Bearer <token>``, Zhipu does
not.

Response shape::

    {
      "data": {
        "level": "<tier-name string>",
        "limits": [
          {"type": "TOKENS_LIMIT",
           "percentage": <0-100 float>,
           "nextResetTime": <ms epoch>},
          ...
        ]
      }
    }

Tier assignment: filter ``type == "TOKENS_LIMIT"`` (case-insensitive)
→ sort ascending by ``nextResetTime`` (missing values sort last) →
first entry = 5h tier, second entry = weekly. Pre-2026-02-12
subscriptions emit only one TOKENS_LIMIT (the legacy 5h-only plan);
in that case weekly degrades gracefully — pct=0 with a far-future
sentinel reset so the cache survives :func:`snapshot_from_cache`'s
"both windows must have a real reset time" gate.

Token sources (priority high → low):
  1. ``ZHIPU_API_KEY`` environment variable
  2. ``ANTHROPIC_AUTH_TOKEN`` env var (Claude Code's runtime config —
     reuse when the user proxies CC through Z.AI)
  3. ``providers.json`` → ``providers.zhipu.auth_token``
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

from claude_island.core.models import (
    register_model_colors,
    register_model_short_names,
)

from . import (
    HTTP_TIMEOUT,
    provider,
    get_provider_setting,
    _parse_ms,
    read_cache_state, write_cache_state,
    log_fetch_failure,
    Window,
)


_DEFAULT_HOST = "https://api.z.ai"
_PATH = "/api/monitor/usage/quota/limit"

# Display registry — cyan family for Zhipu / Z.AI. No pricing entries
# (Zhipu doesn't surface a public per-Mtok rate the way Anthropic /
# DeepSeek do — costs default to Sonnet rates via DEFAULT_PRICING),
# but the chip still wants a recognisable colour so multi-provider
# users can tell GLM rows apart from Anthropic rows in SPEND.
register_model_colors({
    "GLM-Pro": "#0891B2",  # cyan      — premium tier
    "GLM-Air": "#22D3EE",  # bright cyan — fast tier
    "GLM":     "#0891B2",  # family fallback
})
register_model_short_names({
    "GLM-Pro": "GLM Pro",
    "GLM-Air": "GLM Air",
    "GLM":     "GLM",
})


def _read_token() -> str | None:
    """Token chain. ``ZHIPU_API_KEY`` first since it's the obvious
    name; ``ANTHROPIC_AUTH_TOKEN`` second so Claude-Code-via-Z.AI
    setups don't have to set two env vars; providers.json last as the
    GUI / persistent fallback."""
    return (
        os.environ.get("ZHIPU_API_KEY")
        or os.environ.get("ANTHROPIC_AUTH_TOKEN")
        or get_provider_setting("zhipu", "auth_token")
    )


def _host() -> str:
    """Ordered host resolution. ``providers.json`` wins, then
    ``ANTHROPIC_BASE_URL`` if it points at a Zhipu host, finally the
    international default. No multi-region probing — Zhipu's quota API
    is single-region today."""
    cfg_url = get_provider_setting("zhipu", "base_url")
    if cfg_url:
        return cfg_url.rstrip("/")
    env = os.environ.get("ANTHROPIC_BASE_URL", "")
    if "z.ai" in env or "bigmodel.cn" in env:
        return env.rstrip("/")
    return _DEFAULT_HOST


def _fetch_http(token: str) -> tuple[dict | None, str | None]:
    """Single GET to the quota endpoint.

    Returns ``(data, None)`` on success, ``(None, reason)`` on failure.
    See ``anthropic.py:_fetch_http`` for the full rationale on why we
    return reason instead of printing it here."""
    req = urllib.request.Request(
        _host() + _PATH,
        headers={
            # NO "Bearer " prefix — Zhipu rejects requests when one
            # is included. cc-switch v3.14.1 carries the same comment
            # on this exact line.
            "Authorization": token,
            "Content-Type": "application/json",
            "Accept-Language": "en-US,en",
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


def _parse_response(data: dict, *, now: datetime) -> tuple[Window, Window]:
    """Parse Z.AI's response into (five_hour, weekly) windows.

    Sort rule mirrors cc-switch: filter type==TOKENS_LIMIT, sort
    ascending by nextResetTime, first entry = 5h, second = weekly.

    ``now`` is required because legacy single-limit subscriptions (only
    one TOKENS_LIMIT entry, the 5h tier) need a synthetic weekly with
    a future resets_at — without it the QuotaCacheState.to_snapshot
    guard ("five_hour.resets_at must be in the future") would still pass
    but the UI would render a malformed Weekly window. The sentinel
    ``now + 7 days`` is honest about "no weekly info" while keeping
    the 5h bar visible.

    Always returns a complete (Window, Window) — Z.AI's API guarantees
    at least one TOKENS_LIMIT for any active plan, and we synthesize
    the second when missing. If even the first is missing (degenerate
    response), we still produce a Window with a far-future sentinel
    so to_snapshot's "resets in the past" guard catches it cleanly.
    """
    payload_data = data.get("data") if isinstance(data, dict) else None
    limits = (payload_data or {}).get("limits") or []
    tokens_limits = [
        l for l in limits
        if isinstance(l, dict) and str(l.get("type", "")).upper() == "TOKENS_LIMIT"
    ]
    tokens_limits.sort(
        key=lambda l: l.get("nextResetTime") if isinstance(l.get("nextResetTime"), (int, float)) else (2 ** 63 - 1)
    )
    five_raw = tokens_limits[0] if len(tokens_limits) >= 1 else None
    weekly_raw = tokens_limits[1] if len(tokens_limits) >= 2 else None

    if five_raw is not None:
        five_pct = float(five_raw["percentage"]) if isinstance(five_raw.get("percentage"), (int, float)) else 0.0
        five_resets = _parse_ms(five_raw.get("nextResetTime")) or (now - timedelta(seconds=1))
    else:
        # Degenerate — no TOKENS_LIMIT at all. Past-resets_at signals
        # "expired" to to_snapshot which then returns None (UI "no quota").
        five_pct = 0.0
        five_resets = now - timedelta(seconds=1)

    if weekly_raw is not None:
        seven_pct = float(weekly_raw["percentage"]) if isinstance(weekly_raw.get("percentage"), (int, float)) else 0.0
        seven_resets = _parse_ms(weekly_raw.get("nextResetTime")) or (now + timedelta(days=7))
    else:
        # Legacy single-limit plan — synthesise sentinel so the 5h bar
        # still renders with the weekly displaying 0%.
        seven_pct = 0.0
        seven_resets = now + timedelta(days=7)

    return (
        Window(pct=five_pct, resets_at=five_resets),
        Window(pct=seven_pct, resets_at=seven_resets),
    )


@provider("zhipu")
class ZhipuProvider:
    name = "zhipu"

    @classmethod
    def default_config(cls) -> dict:
        """Seed block for ``providers.json`` → ``providers.zhipu``.
        Auto-included by the package's ``_build_default_config()`` —
        no manual wiring needed."""
        return {
            "_help": (
                "Paste your Zhipu Z.AI API key into auth_token below. "
                "Get one at https://z.ai/manage-apikey/apikey-list . "
                "The Zhipu tab appears in the 5h card once auth_token is "
                "non-empty. base_url is optional — leave the default "
                "(api.z.ai, international) or set https://open.bigmodel.cn "
                "for the 国内 endpoint if Zhipu adds CN routing."
            ),
            "auth_token": "",
            "base_url": "https://api.z.ai",
        }

    def detect(self) -> bool:
        """Truthy when the user has signalled Zhipu — either via
        ``ANTHROPIC_BASE_URL`` (Claude Code's runtime knob) or by
        pasting a token into ``providers.json``."""
        base = os.environ.get("ANTHROPIC_BASE_URL", "")
        if "z.ai" in base or "bigmodel.cn" in base:
            return True
        return _read_token() is not None

    def fetch(
        self,
        *,
        cache_dir: Path,
        bypass_cache: bool = False,
    ) -> "QuotaSnapshot | None":  # noqa: F821
        """Fetch from Z.AI's quota endpoint. See ``anthropic.py:fetch``
        for the QuotaCacheState transition rationale."""
        cache_path = cache_dir / "zhipu-quota.json"
        now = datetime.now(timezone.utc)
        state = read_cache_state(cache_path, fallback_provider="zhipu")

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

        data, reason = _fetch_http(token)
        if data is None:
            if bypass_cache:
                return None
            log_fetch_failure(state, reason=reason or "unknown error", now=now)
            new_state = state.with_failed_attempt(now=now)
            write_cache_state(cache_path, new_state)
            return new_state.to_snapshot(now=now)

        five_hour, seven_day = _parse_response(data, now=now)
        new_state = state.with_successful_fetch(
            now=now, five_hour=five_hour, seven_day=seven_day,
        )
        write_cache_state(cache_path, new_state)
        return new_state.to_snapshot(now=now)
