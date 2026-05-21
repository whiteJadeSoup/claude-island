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
    HTTP_TIMEOUT,
    provider,
    read_oauth_token,
    read_cache_state, write_cache_state,
    log_fetch_failure,
    Window,
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
    ) -> "QuotaSnapshot | None":  # noqa: F821 — string annotation, model imported lazily
        """Fetch from Anthropic's /api/oauth/usage with disk cache.

        Linearised on QuotaCacheState transitions:

            read state ──► throttle gate? ── True ──► return state.to_snapshot()
                                │ False
                                ▼
                            read token? ── No ──► log + with_failed_attempt
                                │ Yes
                                ▼
                            HTTP fetch ── (None, reason) ──► log + with_failed_attempt
                                │ (data, None)
                                ▼
                            with_successful_fetch(windows)
                                │
                                ▼
                            write_cache_state ──► state.to_snapshot()

        Every transition produces a NEW state — no in-place mutation.
        Side effects (write_cache_state, log_fetch_failure) are explicit
        and live at the IO boundary, not inside the state class itself.
        """
        cache_path = cache_dir / "anthropic-quota.json"
        now = datetime.now(timezone.utc)
        state = read_cache_state(cache_path, fallback_provider="anthropic")

        # Throttle gate: only honoured for auto-refresh; manual ⟳ ignores it.
        if not bypass_cache and not state.is_fetch_due(now=now):
            return state.to_snapshot(now=now)

        token = read_oauth_token(_CREDENTIALS_PATH)
        if not token:
            if bypass_cache:
                # Manual ⟳ must leave a trail — UI copy promises
                # "errors print to the terminal". Bypass-cache path
                # skips log_fetch_failure (which is paired with state
                # writes), so emit the line directly here.
                safe_stderr_write(
                    "[claude-island] anthropic manual ⟳ failed: "
                    "OAuth credentials not found"
                )
                return None
            log_fetch_failure(state, reason="OAuth credentials not found", now=now)
            new_state = state.with_failed_attempt(now=now)
            write_cache_state(cache_path, new_state)
            return new_state.to_snapshot(now=now)

        data, reason = _fetch_http(token)
        if data is None:
            if bypass_cache:
                # Manual ⟳ failure: mirror the auto-refresh log line so
                # the user can tell the click was processed (just the
                # server / network said no). State is intentionally not
                # updated — manual probes don't count against the
                # circuit-breaker streak.
                safe_stderr_write(
                    f"[claude-island] anthropic manual ⟳ failed: "
                    f"{reason or 'unknown error'}"
                )
                return None
            # log_fetch_failure must run BEFORE with_failed_attempt — the
            # helper reads state.last_attempt_at as the prior-attempt
            # timestamp, which the new state will have overwritten with now.
            log_fetch_failure(state, reason=reason or "unknown error", now=now)
            new_state = state.with_failed_attempt(now=now)
            write_cache_state(cache_path, new_state)
            return new_state.to_snapshot(now=now)

        five_hour, seven_day = _parse_response(data)
        new_state = state.with_successful_fetch(
            now=now, five_hour=five_hour, seven_day=seven_day,
        )
        write_cache_state(cache_path, new_state)
        return new_state.to_snapshot(now=now)


def _fetch_http(token: str) -> tuple[dict | None, str | None]:
    """Hit the Anthropic /api/oauth/usage endpoint with a Bearer token.

    Returns ``(data, None)`` on success, ``(None, reason)`` on any
    failure. The reason string is consumed by ``log_fetch_failure`` in
    the caller, which combines it with cache-derived timing context
    (last attempt N ago, last success M ago) into a single stderr line.

    Why we don't print here anymore: a stderr line emitted at the HTTP
    layer can't include "last success was 47m ago" — that fact lives
    in the cache, which only the fetch() coordinator reads. Pushing
    the print up one level lets one log line carry the full picture.
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
                return None, f"HTTP {resp.status}"
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        # 401 here typically means the OAuth token expired — Claude Code
        # refreshes it on its next interaction, so the next 5 min tick
        # usually self-heals.
        return None, f"HTTP {e.code} {e.reason}"
    except urllib.error.URLError as e:
        # DNS / connection refused / timeout. ``e.reason`` is either a
        # str ("timed out") or an OSError; stringifying handles both.
        return None, f"network error: {e.reason}"
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        return None, f"bad response body ({type(e).__name__})"
    except OSError as e:
        return None, f"{type(e).__name__}: {e}"
    if not _has_shape(data):
        return None, "response missing five_hour/seven_day fields (API contract changed?)"
    return data, None


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


def _parse_response(data: dict) -> tuple[Window, Window]:
    """Parse the validated HTTP response into the throttle layer's
    Window value objects. Must run AFTER _has_shape — it trusts that
    five_hour/seven_day exist with utilization (number) and
    resets_at (ISO string) keys."""
    return (
        Window(
            pct=float(data["five_hour"]["utilization"]),
            resets_at=_parse_resets(data["five_hour"]["resets_at"]),
        ),
        Window(
            pct=float(data["seven_day"]["utilization"]),
            resets_at=_parse_resets(data["seven_day"]["resets_at"]),
        ),
    )


def _parse_resets(s: str) -> datetime:
    """ISO 8601 → tz-aware UTC datetime. Z suffix is normalised to
    +00:00 because Python's fromisoformat only learned to accept Z in
    3.11 — keep this for older interpreter compatibility."""
    dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)
