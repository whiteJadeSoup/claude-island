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
    read_cache, write_cache,
    _parse_ms, snapshot_from_cache,
    record_failed_attempt,
    is_fetch_due,
    log_fetch_failure,
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


def _normalise(data: dict, *, fetched_at: datetime) -> dict:
    """Convert Z.AI's response to the shared cache schema.

    Sort rule mirrors cc-switch: filter type==TOKENS_LIMIT, sort
    ascending by nextResetTime (missing → 2**63-1 so they sort last
    rather than crashing the comparator), first entry = 5h, second =
    weekly.

    Legacy single-limit subscriptions: weekly is missing. We synthesise
    a sentinel ``fetched_at + 7 days`` so :func:`snapshot_from_cache`
    accepts the snapshot (it requires both reset timestamps to be real
    and in the future). The UI then renders Weekly as 0% — honest to
    "no weekly limit info available", and lets the 5h bar still show.
    """
    payload_data = data.get("data") if isinstance(data, dict) else None
    limits = (payload_data or {}).get("limits") or []
    tokens_limits = [
        l for l in limits
        if isinstance(l, dict) and str(l.get("type", "")).upper() == "TOKENS_LIMIT"
    ]
    # Sort ascending by nextResetTime; entries with missing timestamps
    # sort last (huge sentinel) rather than blowing up the key fn.
    tokens_limits.sort(
        key=lambda l: l.get("nextResetTime") if isinstance(l.get("nextResetTime"), (int, float)) else (2 ** 63 - 1)
    )
    five_hour = tokens_limits[0] if len(tokens_limits) >= 1 else None
    weekly = tokens_limits[1] if len(tokens_limits) >= 2 else None

    five_pct = float(five_hour["percentage"]) if (five_hour and isinstance(five_hour.get("percentage"), (int, float))) else 0.0
    five_resets = _parse_ms(five_hour.get("nextResetTime")) if five_hour else None

    if weekly:
        seven_pct = float(weekly["percentage"]) if isinstance(weekly.get("percentage"), (int, float)) else 0.0
        seven_resets = _parse_ms(weekly.get("nextResetTime"))
    else:
        # Legacy single-limit plan — synthesise a sentinel weekly so the
        # snapshot_from_cache guard doesn't drop the whole snapshot.
        seven_pct = 0.0
        seven_resets = fetched_at + timedelta(days=7)

    return {
        "provider": "zhipu",
        "fetched_at": fetched_at.isoformat(),
        "five_hour": {
            "pct": five_pct,
            "resets_at": five_resets.isoformat() if five_resets else None,
        },
        "seven_day": {
            "pct": seven_pct,
            "resets_at": seven_resets.isoformat() if seven_resets else None,
        },
    }


def _from_cache(cached: dict | None, now: datetime):
    if cached is None:
        return None
    return snapshot_from_cache(cached, provider="zhipu", now=now)


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
        """Fetch from Z.AI's quota endpoint with disk cache. Mirrors
        the structure of MiniMax / Anthropic providers — disk cache
        first (skipped on bypass), HTTP fallback, write-through on
        success, cache fallback on failure."""
        cache_path = cache_dir / "zhipu-quota.json"
        now = datetime.now(timezone.utc)

        if not bypass_cache:
            cached = read_cache(cache_path)
            if cached is not None and not is_fetch_due(cached, now=now):
                # Throttle window active — see anthropic.py for the
                # full rationale. Returns prior snap if there was a
                # successful refresh, None if this is a first-failure
                # marker. Never re-issues HTTP within POLL_TTL.
                return _from_cache(cached, now)

        token = _read_token()
        if not token:
            if bypass_cache:
                return None
            log_fetch_failure(
                cache_path, now=now, provider="zhipu",
                reason="auth token not configured",
            )
            record_failed_attempt(cache_path, now=now, provider="zhipu")
            return _from_cache(read_cache(cache_path), now)

        data, reason = _fetch_http(token)
        if data is None:
            if bypass_cache:
                return None
            log_fetch_failure(
                cache_path, now=now, provider="zhipu",
                reason=reason or "unknown error",
            )
            record_failed_attempt(cache_path, now=now, provider="zhipu")
            return _from_cache(read_cache(cache_path), now)

        payload = _normalise(data, fetched_at=now)
        payload["last_attempt_at"] = now.isoformat()
        write_cache(cache_path, payload)
        return _from_cache(payload, now)
