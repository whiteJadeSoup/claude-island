"""Anthropic quota provider.

Endpoint: https://api.anthropic.com/api/oauth/usage
Auth: Bearer token (ANTHROPIC_AUTH_TOKEN)
Response: { five_hour: { utilization, resets_at }, seven_day: { utilization, resets_at } }
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

from claude_island.core.safe_stderr import safe_stderr_write

from . import (
    HTTP_TIMEOUT, POLL_TTL,
    provider,
    read_oauth_token, read_cache, write_cache,
    _parse_iso, snapshot_from_cache,
)


# Per-Mtok rates from https://platform.claude.com/docs/en/about-claude/pricing
# Lookup is by family-token substring match.
register_pricing({
    "haiku":  PricingTable(input_per_mtok=1.0, output_per_mtok=5.0),
    "sonnet": PricingTable(input_per_mtok=3.0, output_per_mtok=15.0),
    "opus":   PricingTable(input_per_mtok=5.0, output_per_mtok=25.0),
})

# Display registry — chip colour follows the cool-spectrum tier scheme:
# the more powerful (and more expensive) the family, the deeper the
# hue. Opus = purple, Sonnet = blue, Haiku = green. Matches the
# Anthropic-house cool palette so the UI reads as on-brand.
register_model_colors({
    "opus":   "#8B5CF6",  # purple — premium tier
    "sonnet": "#3B82F6",  # blue   — mid tier
    "haiku":  "#10B981",  # green  — fast tier
})
register_model_short_names({
    "opus":   "Opus",
    "sonnet": "Sonnet",
    "haiku":  "Haiku",
})


# Claude Code stores the OAuth access token here. Hardcoded because
# there is exactly one place Claude Code writes credentials, and
# threading it through ProviderEngine would just be ceremony. If a
# non-Claude-Code use case ever needs a different path, take a
# constructor arg then. (macOS variant lives in the login keychain
# under service "Claude Code-credentials"; the file-read failure path
# in ``read_oauth_token`` falls back to ``/usr/bin/security`` there.)
_CREDENTIALS_PATH = Path.home() / ".claude" / ".credentials.json"


URL = "https://api.anthropic.com/api/oauth/usage"
BETA_HEADER = "oauth-2025-04-20"


@provider("anthropic")
class AnthropicProvider:
    name = "anthropic"

    def detect(self) -> bool:
        """Active when ANTHROPIC_BASE_URL does not contain a known non-Anthropic domain."""
        base = os.environ.get("ANTHROPIC_BASE_URL", "")
        # MiniMax uses minimaxi.com / minimax.io; others may use custom base URLs.
        # If the base URL is empty or points to api.anthropic.com, assume Anthropic.
        return "minimaxi" not in base and "minimax.io" not in base

    def fetch(
        self,
        *,
        cache_dir: Path,
        bypass_cache: bool = False,
    ) -> QuotaSnapshot | None:
        """Fetch from Anthropic's /api/oauth/usage with disk cache."""
        from claude_island.core.models import QuotaSnapshot

        cache_path = cache_dir / "anthropic-quota.json"
        now = datetime.now(timezone.utc)

        if not bypass_cache:
            cached = read_cache(cache_path)
            if cached is not None:
                snap = _from_cache(cached, now)
                if snap is not None and not _is_expired(cached, now):
                    return snap

        token = read_oauth_token(_CREDENTIALS_PATH)
        if not token:
            if not bypass_cache:
                return _from_cache(read_cache(cache_path), now)
            return None

        data = _fetch_http(token)
        if data is None:
            if bypass_cache:
                return None
            cached = read_cache(cache_path)
            return _from_cache(cached, now)

        payload = _normalise(data, fetched_at=now)
        write_cache(cache_path, payload)
        return _from_cache(payload, now)


def _fetch_http(token: str) -> dict | None:
    """Hit the Anthropic /api/oauth/usage endpoint with a Bearer token.

    Returns parsed JSON on success, ``None`` on any failure. Each
    failure mode emits a single stderr line so the user can tell network
    timeout from token rejection from server-shape mismatch — the UI
    "Quota unavailable" hint promises this and used to lie about it.
    """
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
                safe_stderr_write(
                    f"[claude-island] anthropic quota fetch: HTTP {resp.status}"
                )
                return None
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        # 401 here typically means the OAuth token expired — Claude Code
        # refreshes it on its next interaction, so the next 5 min tick
        # usually self-heals. Print so the user knows that's the cause.
        safe_stderr_write(
            f"[claude-island] anthropic quota fetch: HTTP {e.code} {e.reason}"
        )
        return None
    except urllib.error.URLError as e:
        # DNS / connection refused / timeout. ``e.reason`` is either a
        # str ("timed out") or an OSError; stringifying handles both.
        safe_stderr_write(
            f"[claude-island] anthropic quota fetch failed: {e.reason}"
        )
        return None
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        safe_stderr_write(
            f"[claude-island] anthropic quota fetch: bad response body ({type(e).__name__})"
        )
        return None
    except OSError as e:
        safe_stderr_write(
            f"[claude-island] anthropic quota fetch: {type(e).__name__}: {e}"
        )
        return None
    if not _has_shape(data):
        safe_stderr_write(
            "[claude-island] anthropic quota fetch: response missing "
            "five_hour/seven_day fields — API contract changed?"
        )
        return None
    return data


def _has_shape(data: object) -> bool:
    if not isinstance(data, dict):
        return False
    for key in ("five_hour", "seven_day"):
        w = data.get(key)
        if not isinstance(w, dict):
            return False
        if not isinstance(w.get("utilization"), (int, float)):
            return False
        if not isinstance(w.get("resets_at"), str):
            return False
    return True


def _normalise(data: dict, *, fetched_at: datetime) -> dict:
    return {
        "provider": "anthropic",
        "fetched_at": fetched_at.isoformat(),
        "five_hour": {
            "pct": float(data["five_hour"]["utilization"]),
            "resets_at": data["five_hour"]["resets_at"],
        },
        "seven_day": {
            "pct": float(data["seven_day"]["utilization"]),
            "resets_at": data["seven_day"]["resets_at"],
        },
    }


def _from_cache(cached: dict | None, now: datetime):
    if cached is None:
        return None
    return snapshot_from_cache(cached, provider="anthropic", now=now)


def _is_expired(cached: dict, now: datetime) -> bool:
    fetched = _parse_iso(cached.get("fetched_at", ""))
    if fetched is None:
        return True
    return (now - fetched).total_seconds() > POLL_TTL
