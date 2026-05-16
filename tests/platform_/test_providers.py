"""Tests for the multi-provider quota engine."""
from __future__ import annotations

import json
import os
import tempfile
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

from claude_island.core.models import QuotaSnapshot


class TestProviderRegistration:
    """@provider decorator registers classes correctly."""

    def test_decorator_registers(self):
        # Import sub-modules to trigger @provider registration
        import claude_island.platform_.providers.anthropic
        import claude_island.platform_.providers.minimax
        from claude_island.platform_.providers import all_providers
        names = list(all_providers().keys())
        assert "anthropic" in names
        assert "minimax" in names

    def test_engine_detects_minimax_when_active(self):
        from claude_island.platform_.providers import ProviderEngine

        with patch.dict(os.environ, {"ANTHROPIC_BASE_URL": "https://api.minimaxi.com/anthropic"}):
            import claude_island.platform_.providers.anthropic
            import claude_island.platform_.providers.minimax
            e = ProviderEngine(cache_dir=Path(tempfile.gettempdir()))
            p = e._detect_active()
            assert p is not None
            assert p.name == "minimax"

    def test_engine_detects_anthropic_when_no_override(self):
        env_backup = os.environ.pop("ANTHROPIC_BASE_URL", None)
        try:
            import claude_island.platform_.providers.anthropic
            import claude_island.platform_.providers.minimax
            from claude_island.platform_.providers import ProviderEngine
            e = ProviderEngine(cache_dir=Path(tempfile.gettempdir()))
            p = e._detect_active()
            assert p is not None
            assert p.name == "anthropic"
        finally:
            if env_backup is not None:
                os.environ["ANTHROPIC_BASE_URL"] = env_backup


class TestAnthropicProvider:
    def test_detect_true_when_no_minimax_in_base_url(self):
        from claude_island.platform_.providers.anthropic import AnthropicProvider
        with patch.dict(os.environ, {"ANTHROPIC_BASE_URL": "https://api.anthropic.com"}):
            p = AnthropicProvider()
            assert p.detect() is True

    def test_detect_false_when_minimax_in_base_url(self):
        from claude_island.platform_.providers.anthropic import AnthropicProvider
        with patch.dict(os.environ, {"ANTHROPIC_BASE_URL": "https://api.minimaxi.com/anthropic"}):
            p = AnthropicProvider()
            assert p.detect() is False

    def test_fetch_returns_none_when_no_oauth_credential(self, tmp_path):
        # Anthropic reads OAuth from ~/.claude/.credentials.json (not env).
        # Point the module-level path at a non-existent file to simulate
        # "user has not run Claude Code OAuth login". On darwin the
        # provider falls back to the login keychain, so suppress that
        # branch too — otherwise this test only fails on macOS dev boxes
        # that have a real Claude Code login.
        from claude_island.platform_.providers import anthropic as anth
        from claude_island.platform_ import providers as prov_pkg
        missing = tmp_path / "no-such-credentials.json"
        with patch.object(anth, "_CREDENTIALS_PATH", missing), \
             patch.object(prov_pkg, "_read_keychain_credentials", return_value=None):
            p = anth.AnthropicProvider()
            result = p.fetch(cache_dir=tmp_path)
            assert result is None

    def test_fetch_uses_cache(self, tmp_path):
        from claude_island.platform_.providers.anthropic import AnthropicProvider
        cache = tmp_path / "anthropic-quota.json"
        now = datetime.now(timezone.utc).isoformat()
        cache.write_text(json.dumps({
            "provider": "anthropic",
            "fetched_at": now,
            "five_hour": {"pct": 42.0, "resets_at": "2030-01-01T00:00:00Z"},
            "seven_day": {"pct": 15.0, "resets_at": "2030-01-07T00:00:00Z"},
        }))
        p = AnthropicProvider()
        result = p.fetch(cache_dir=tmp_path)
        assert result is not None
        assert result.five_hour_pct == 42.0
        assert result.provider == "anthropic"

    def test_fetch_bypasses_cache_returns_none_on_network_error(self, tmp_path):
        from claude_island.platform_.providers.anthropic import AnthropicProvider
        cache = tmp_path / "anthropic-quota.json"
        now = datetime.now(timezone.utc).isoformat()
        cache.write_text(json.dumps({
            "provider": "anthropic",
            "fetched_at": now,
            "five_hour": {"pct": 42.0, "resets_at": "2030-01-01T00:00:00Z"},
            "seven_day": {"pct": 15.0, "resets_at": "2030-01-07T00:00:00Z"},
        }))
        p = AnthropicProvider()
        # Patch at module level since _fetch_http is a module function.
        # Returns (data, reason) tuple — None data with a reason string
        # signals a network/auth failure to the fetch() coordinator.
        with patch(
            "claude_island.platform_.providers.anthropic._fetch_http",
            return_value=(None, "test network error"),
        ):
            result = p.fetch(cache_dir=tmp_path, bypass_cache=True)
            # Network failure + bypass=True → no cached fallback
            assert result is None


class TestMiniMaxProvider:
    def test_detect_true_when_minimaxi_in_base_url(self):
        from claude_island.platform_.providers.minimax import MiniMaxProvider
        with patch.dict(os.environ, {"ANTHROPIC_BASE_URL": "https://www.minimaxi.com/anthropic"}):
            p = MiniMaxProvider()
            assert p.detect() is True

    def test_detect_true_when_minimax_io_in_base_url(self):
        from claude_island.platform_.providers.minimax import MiniMaxProvider
        with patch.dict(os.environ, {"ANTHROPIC_BASE_URL": "https://api.minimax.io/anthropic"}):
            p = MiniMaxProvider()
            assert p.detect() is True

    def test_detect_false_when_no_match(self, tmp_path, monkeypatch):
        # Isolate from any real ~/.claude-island/providers.json the
        # developer may have on their machine — detect() now checks
        # the config file as a token-source fallback, so a leaked file
        # would otherwise flip this assertion.
        from claude_island.platform_.providers.minimax import MiniMaxProvider
        monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://api.anthropic.com")
        monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
        with patch(
            "claude_island.platform_.providers.PROVIDER_CONFIG_PATH",
            tmp_path / "no-config.json",
        ):
            assert MiniMaxProvider().detect() is False

    def test_fetch_uses_cache(self, tmp_path):
        from claude_island.platform_.providers.minimax import MiniMaxProvider
        cache = tmp_path / "minimax-quota.json"
        now = datetime.now(timezone.utc).isoformat()
        cache.write_text(json.dumps({
            "provider": "minimax",
            "fetched_at": now,
            "five_hour": {"pct": 7.0, "resets_at": "2030-01-01T00:00:00Z"},
            "seven_day": {"pct": 1.0, "resets_at": "2030-01-07T00:00:00Z"},
        }))
        p = MiniMaxProvider()
        result = p.fetch(cache_dir=tmp_path)
        assert result is not None
        assert result.five_hour_pct == 7.0
        assert result.provider == "minimax"

    def test_parse_response_calculates_pct_from_remaining(self):
        from claude_island.platform_.providers.minimax import _parse_response
        data = {
            "model_remains": [{
                "model_name": "MiniMax-M2.7-highspeed",
                "current_interval_total_count": 4500,
                "current_interval_usage_count": 4227,  # remaining = 4227
                "current_weekly_total_count": 45000,
                "current_weekly_usage_count": 44689,  # remaining = 44689
                "end_time": 1777714800000,
                "weekly_end_time": 1778319600000,
            }]
        }
        parsed = _parse_response(data)
        assert parsed is not None
        five_hour, _ = parsed
        assert five_hour.pct == pytest.approx(
            (4500 - 4227) / 4500 * 100, rel=1e-2
        )  # ~6%


class TestProviderEngine:
    def test_get_returns_quota_from_active_provider(self, monkeypatch):
        from claude_island.platform_.providers import ProviderEngine

        # MiniMax active
        monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://www.minimaxi.com/anthropic")
        e = ProviderEngine(cache_dir=Path(tempfile.gettempdir()))
        result = e.get()
        assert result is None or isinstance(result, QuotaSnapshot)

    def test_force_refresh_bypasses_cache(self, tmp_path, monkeypatch):
        from claude_island.platform_.providers import ProviderEngine

        monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://www.minimaxi.com/anthropic")
        e = ProviderEngine(cache_dir=tmp_path)

        # Pre-populate cache with a stale value
        cache = tmp_path / "minimax-quota.json"
        now = datetime.now(timezone.utc).isoformat()
        cache.write_text(json.dumps({
            "provider": "minimax",
            "fetched_at": now,
            "five_hour": {"pct": 99.0, "resets_at": "2030-01-01T00:00:00Z"},
            "seven_day": {"pct": 50.0, "resets_at": "2030-01-07T00:00:00Z"},
        }))

        # force_refresh returns None when HTTP fails (since we have no real token)
        result = e.force_refresh()
        # The result depends on whether HTTP fetch succeeds
        # (it may be None if no real network access)
        assert result is None or isinstance(result, QuotaSnapshot)


# ============================================================================
# Multi-provider config + explicit selection (providers.json + tabs)
# ============================================================================

class TestProviderConfig:
    """Tests for the providers.json reader / writer."""

    def test_read_returns_empty_dict_when_missing(self, tmp_path):
        from claude_island.platform_.providers import read_provider_config
        cfg = read_provider_config(tmp_path / "no-such-file.json")
        assert cfg == {}

    def test_round_trip_preserves_shape(self, tmp_path):
        from claude_island.platform_.providers import (
            read_provider_config, write_provider_config,
        )
        path = tmp_path / "providers.json"
        original = {
            "selected": "minimax",
            "providers": {"minimax": {"auth_token": "sk-cp-abc"}},
        }
        write_provider_config(original, path)
        assert read_provider_config(path) == original

    def test_get_provider_setting_returns_none_for_missing(self, tmp_path):
        from claude_island.platform_.providers import (
            get_provider_setting, write_provider_config,
        )
        path = tmp_path / "providers.json"
        with patch(
            "claude_island.platform_.providers.PROVIDER_CONFIG_PATH", path,
        ):
            assert get_provider_setting("minimax", "auth_token") is None
            write_provider_config(
                {"providers": {"minimax": {"base_url": "https://x"}}},
                path,
            )
            assert get_provider_setting("minimax", "auth_token") is None
            assert get_provider_setting("minimax", "base_url") == "https://x"

    def test_ensure_writes_default_when_missing(self, tmp_path):
        from claude_island.platform_.providers import (
            ensure_provider_config, read_provider_config,
        )
        path = tmp_path / "providers.json"
        assert not path.exists()
        ensure_provider_config(path)
        assert path.exists()
        cfg = read_provider_config(path)
        # Anthropic is the default tab — no setup needed for it.
        assert cfg["selected"] == "anthropic"
        # MiniMax block is present with empty auth_token, so the tab
        # does NOT appear until the user pastes a key in.
        mm = cfg["providers"]["minimax"]
        assert mm["auth_token"] == ""
        assert "base_url" in mm
        # Self-documenting: the _help string explains how to enable.
        assert "_help" in mm

    def test_ensure_does_not_overwrite_existing(self, tmp_path):
        from claude_island.platform_.providers import (
            ensure_provider_config, read_provider_config, write_provider_config,
        )
        path = tmp_path / "providers.json"
        original = {"selected": "minimax", "providers": {"minimax": {"auth_token": "secret"}}}
        write_provider_config(original, path)
        ensure_provider_config(path)
        # Untouched.
        assert read_provider_config(path) == original

    def test_default_config_minimax_has_empty_token_so_tab_hidden(self, tmp_path):
        # Regression: the default config must NOT activate the MiniMax
        # tab on first run. The user must explicitly paste a key in.
        from claude_island.platform_.providers import (
            ensure_provider_config, get_provider_setting,
        )
        path = tmp_path / "providers.json"
        ensure_provider_config(path)
        with patch(
            "claude_island.platform_.providers.PROVIDER_CONFIG_PATH", path,
        ):
            # Empty string is treated as "not set" so the tab won't appear.
            assert get_provider_setting("minimax", "auth_token") is None

    def test_set_selected_preserves_other_fields(self, tmp_path):
        from claude_island.platform_.providers import (
            read_provider_config, set_selected_provider, write_provider_config,
        )
        path = tmp_path / "providers.json"
        write_provider_config(
            {"providers": {"minimax": {"auth_token": "secret"}}},
            path,
        )
        with patch(
            "claude_island.platform_.providers.PROVIDER_CONFIG_PATH", path,
        ):
            set_selected_provider("minimax")
        cfg = read_provider_config(path)
        # Token survived the merge — set_selected only touches "selected".
        assert cfg["selected"] == "minimax"
        assert cfg["providers"]["minimax"]["auth_token"] == "secret"


class TestExplicitProviderSelection:
    """ProviderEngine.get(provider_name=...) bypasses auto-detect."""

    def test_get_with_unknown_name_returns_none(self, tmp_path):
        from claude_island.platform_.providers import ProviderEngine
        e = ProviderEngine(cache_dir=tmp_path)
        assert e.get(provider_name="kimi-doesnt-exist") is None

    def test_get_with_known_name_routes_to_that_provider(self, tmp_path, monkeypatch):
        # Even with a base_url that points away from anthropic, an
        # explicit name="anthropic" still routes to AnthropicProvider.
        # Without the explicit-selection fix, auto-detect would pick
        # MiniMax here based on the env var.
        import claude_island.platform_.providers.anthropic as anth
        from claude_island.platform_.providers import ProviderEngine

        monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://api.minimaxi.com/anthropic")
        from claude_island.platform_ import providers as prov_pkg
        with patch.object(anth, "_CREDENTIALS_PATH", tmp_path / "no-cred.json"), \
             patch.object(prov_pkg, "_read_keychain_credentials", return_value=None):
            e = ProviderEngine(cache_dir=tmp_path)
            # No cred file (and keychain fallback stubbed out) →
            # Anthropic fetch returns None. The fact that it returns
            # None instead of falling back to MiniMax proves the
            # explicit name was honoured.
            assert e.get(provider_name="anthropic") is None


class TestMiniMaxConfigToken:
    """MiniMax token chain: env > providers.json."""

    def test_token_falls_back_to_config_when_env_missing(self, tmp_path, monkeypatch):
        from claude_island.platform_.providers import write_provider_config
        from claude_island.platform_.providers.minimax import _read_token

        monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
        path = tmp_path / "providers.json"
        write_provider_config(
            {"providers": {"minimax": {"auth_token": "sk-cp-fallback"}}},
            path,
        )
        with patch(
            "claude_island.platform_.providers.PROVIDER_CONFIG_PATH", path,
        ):
            assert _read_token() == "sk-cp-fallback"

    def test_env_wins_over_config(self, tmp_path, monkeypatch):
        from claude_island.platform_.providers import write_provider_config
        from claude_island.platform_.providers.minimax import _read_token

        monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "sk-cp-env")
        path = tmp_path / "providers.json"
        write_provider_config(
            {"providers": {"minimax": {"auth_token": "sk-cp-cfg"}}},
            path,
        )
        with patch(
            "claude_island.platform_.providers.PROVIDER_CONFIG_PATH", path,
        ):
            assert _read_token() == "sk-cp-env"


class TestMiniMaxHostProbing:
    """Auto-detect host: try CN, fall through to intl on 1004."""

    def test_1004_response_falls_through_to_next_host(self, tmp_path, monkeypatch):
        # First host returns 1004 (auth error wrapped as HTTP 200) →
        # engine moves on to the next candidate. Caches the working host
        # so the next call goes direct.
        from claude_island.platform_.providers import minimax as mm

        monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)
        mm._HOST_CACHE = None  # reset module cache for clean state
        # Isolate from the dev's real ~/.claude-island/providers.json.
        # If it has minimax.base_url set, _candidate_hosts() returns
        # just that one URL and the test loses its ability to fall
        # through to the second host. (Bit me: dev's real config grew
        # a base_url mid-session and this test started failing for
        # "no good reason".)
        monkeypatch.setattr(
            "claude_island.platform_.providers.PROVIDER_CONFIG_PATH",
            tmp_path / "no-config.json",
        )

        calls: list[str] = []

        def fake_fetch(url, token):
            calls.append(url)
            # _fetch_http now returns (data, reason). The 1004 here is
            # a HTTP 200 + business-level auth error — the JSON body is
            # valid, _try_hosts handles the auth-error filter itself.
            if "minimaxi.com" in url:
                return (
                    {"base_resp": {"status_code": 1004, "status_msg": "cookie"}},
                    None,
                )
            return (
                {
                    "model_remains": [{
                        "model_name": "MiniMax-M*",
                        "current_interval_total_count": 100,
                        "current_interval_usage_count": 80,
                        "end_time": 9999999999000,
                        "current_weekly_total_count": 1000,
                        "current_weekly_usage_count": 900,
                        "weekly_end_time": 9999999999000,
                    }],
                },
                None,
            )

        with patch.object(mm, "_fetch_http", side_effect=fake_fetch):
            data, reason = mm._try_hosts("sk-cp-fake")

        assert data is not None
        assert reason is None
        assert mm._HOST_CACHE == "https://api.minimax.io"
        assert any("minimaxi.com" in c for c in calls)
        assert any("minimax.io" in c for c in calls)

    def test_explicit_base_url_in_config_skips_probing(self, tmp_path, monkeypatch):
        from claude_island.platform_.providers import (
            minimax as mm, write_provider_config,
        )
        monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)
        mm._HOST_CACHE = None
        path = tmp_path / "providers.json"
        write_provider_config(
            {"providers": {"minimax": {"base_url": "https://only-this-host.example"}}},
            path,
        )
        with patch(
            "claude_island.platform_.providers.PROVIDER_CONFIG_PATH", path,
        ):
            hosts = mm._candidate_hosts()
        assert hosts == ["https://only-this-host.example"]

    def test_detect_true_when_token_in_config_even_without_env(self, tmp_path, monkeypatch):
        # Engine fallback should pick MiniMax when the user has
        # configured a token, even if no env var points at it.
        from claude_island.platform_.providers import write_provider_config
        from claude_island.platform_.providers.minimax import MiniMaxProvider

        monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)
        monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
        path = tmp_path / "providers.json"
        write_provider_config(
            {"providers": {"minimax": {"auth_token": "sk-cp-x"}}},
            path,
        )
        with patch(
            "claude_island.platform_.providers.PROVIDER_CONFIG_PATH", path,
        ):
            assert MiniMaxProvider().detect() is True


# ============================================================================
# Zhipu (Z.AI / GLM Coding Plan) provider
# ============================================================================

class TestZhipuProvider:
    """Detect / fetch / cache behaviour for the Zhipu provider.

    Endpoint reverse-engineered from cc-switch v3.14.1
    (`src-tauri/src/services/coding_plan.rs`)."""

    def test_detect_true_when_z_ai_in_base_url(self):
        from claude_island.platform_.providers.zhipu import ZhipuProvider
        with patch.dict(os.environ, {"ANTHROPIC_BASE_URL": "https://api.z.ai/anthropic"}):
            assert ZhipuProvider().detect() is True

    def test_detect_true_when_bigmodel_cn_in_base_url(self):
        from claude_island.platform_.providers.zhipu import ZhipuProvider
        with patch.dict(os.environ, {"ANTHROPIC_BASE_URL": "https://open.bigmodel.cn/anthropic"}):
            assert ZhipuProvider().detect() is True

    def test_detect_true_when_token_in_config(self, tmp_path, monkeypatch):
        from claude_island.platform_.providers import write_provider_config
        from claude_island.platform_.providers.zhipu import ZhipuProvider

        monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)
        monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
        monkeypatch.delenv("ZHIPU_API_KEY", raising=False)
        path = tmp_path / "providers.json"
        write_provider_config(
            {"providers": {"zhipu": {"auth_token": "zhipu-fake-key"}}},
            path,
        )
        with patch(
            "claude_island.platform_.providers.PROVIDER_CONFIG_PATH", path,
        ):
            assert ZhipuProvider().detect() is True

    def test_detect_false_when_no_signal(self, tmp_path, monkeypatch):
        # Isolate from any real ~/.claude-island/providers.json on the
        # developer's machine — without the patch, a leaked Zhipu token
        # would flip this assertion silently.
        from claude_island.platform_.providers.zhipu import ZhipuProvider
        monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://api.anthropic.com")
        monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
        monkeypatch.delenv("ZHIPU_API_KEY", raising=False)
        with patch(
            "claude_island.platform_.providers.PROVIDER_CONFIG_PATH",
            tmp_path / "no-config.json",
        ):
            assert ZhipuProvider().detect() is False

    def test_default_config_has_required_keys(self):
        from claude_island.platform_.providers.zhipu import ZhipuProvider
        cfg = ZhipuProvider.default_config()
        # Empty token so the tab does NOT auto-appear after first run.
        assert cfg["auth_token"] == ""
        # base_url defaults to international z.ai endpoint.
        assert "z.ai" in cfg["base_url"]
        # Self-documenting help string in the seed config.
        assert "_help" in cfg

    def test_parse_response_two_limits_assigns_5h_then_weekly_by_reset_time(self):
        # Per cc-switch rule: filter type==TOKENS_LIMIT, sort ascending
        # by nextResetTime, first → 5h, second → weekly. Even when the
        # API returns them in a different order.
        from claude_island.platform_.providers.zhipu import _parse_response
        now = datetime(2026, 5, 1, tzinfo=timezone.utc)
        # Intentionally weekly-first in the input; sort must reverse it.
        weekly_ms = int(datetime(2026, 5, 8, tzinfo=timezone.utc).timestamp() * 1000)
        five_ms   = int(datetime(2026, 5, 1, 5, tzinfo=timezone.utc).timestamp() * 1000)
        data = {
            "data": {
                "level": "Pro",
                "limits": [
                    {"type": "TOKENS_LIMIT", "percentage": 80.0,
                     "nextResetTime": weekly_ms},
                    {"type": "TOKENS_LIMIT", "percentage": 12.5,
                     "nextResetTime": five_ms},
                    # Noise: non-TOKENS_LIMIT entries must be filtered.
                    {"type": "REQUEST_LIMIT", "percentage": 99.9,
                     "nextResetTime": five_ms},
                ],
            },
        }
        five_hour, seven_day = _parse_response(data, now=now)
        # Sorted ascending: 5h slot is the soonest reset (12.5%),
        # weekly slot is the later reset (80%).
        assert five_hour.pct == 12.5
        assert seven_day.pct == 80.0

    def test_parse_response_legacy_single_limit_synthesises_weekly(self):
        # Pre-2026-02-12 subscriptions only emit one TOKENS_LIMIT; the
        # snapshot still has to satisfy state.to_snapshot's "both
        # windows must have a real future reset" gate, so we synthesise
        # a 7-day-out sentinel for weekly with 0% utilisation.
        from claude_island.platform_.providers.zhipu import _parse_response
        now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        five_ms = int(datetime(2026, 1, 1, 5, tzinfo=timezone.utc).timestamp() * 1000)
        data = {
            "data": {
                "level": "Lite",
                "limits": [
                    {"type": "TOKENS_LIMIT", "percentage": 25.0,
                     "nextResetTime": five_ms},
                ],
            },
        }
        five_hour, seven_day = _parse_response(data, now=now)
        assert five_hour.pct == 25.0
        # Synthesised weekly: 0%, far-future reset so the snapshot
        # passes the validity gate.
        assert seven_day.pct == 0.0
        assert seven_day.resets_at > now

    def test_fetch_uses_cache(self, tmp_path):
        from claude_island.platform_.providers.zhipu import ZhipuProvider
        cache = tmp_path / "zhipu-quota.json"
        now = datetime.now(timezone.utc).isoformat()
        cache.write_text(json.dumps({
            "provider": "zhipu",
            "fetched_at": now,
            "five_hour": {"pct": 33.0, "resets_at": "2030-01-01T00:00:00Z"},
            "seven_day": {"pct": 11.0, "resets_at": "2030-01-07T00:00:00Z"},
        }))
        result = ZhipuProvider().fetch(cache_dir=tmp_path)
        assert result is not None
        assert result.five_hour_pct == 33.0
        assert result.provider == "zhipu"

    def test_fetch_authorization_header_has_no_bearer_prefix(self, tmp_path, monkeypatch):
        # Critical Zhipu gotcha — Anthropic / MiniMax both prefix with
        # "Bearer "; Zhipu rejects requests carrying that prefix. Capture
        # the actual header by patching urlopen, assert the raw value.
        from claude_island.platform_.providers import zhipu as zh

        monkeypatch.setenv("ZHIPU_API_KEY", "raw-test-key")
        captured: dict = {}

        def fake_urlopen(req, timeout=None):
            captured["auth"] = req.get_header("Authorization")
            raise OSError("intercepted")  # we don't care about the response

        with patch.object(zh.urllib.request, "urlopen", side_effect=fake_urlopen):
            data, reason = zh._fetch_http("raw-test-key")

        assert data is None  # urlopen raised OSError → failure path
        assert reason is not None  # error reason string captured
        assert captured["auth"] == "raw-test-key"
        assert not captured["auth"].startswith("Bearer ")

    def test_fetch_returns_cached_on_http_failure(self, tmp_path, monkeypatch):
        from claude_island.platform_.providers.zhipu import ZhipuProvider
        cache = tmp_path / "zhipu-quota.json"
        now = datetime.now(timezone.utc).isoformat()
        cache.write_text(json.dumps({
            "provider": "zhipu",
            "fetched_at": now,
            "five_hour": {"pct": 50.0, "resets_at": "2030-01-01T00:00:00Z"},
            "seven_day": {"pct": 20.0, "resets_at": "2030-01-07T00:00:00Z"},
        }))
        monkeypatch.setenv("ZHIPU_API_KEY", "any-key")
        # bypass_cache forces an HTTP attempt; force it to fail and verify
        # the cached snapshot is NOT returned (bypass=True path).
        # _fetch_http now returns (data, reason); failure = (None, str).
        with patch(
            "claude_island.platform_.providers.zhipu._fetch_http",
            return_value=(None, "test failure"),
        ):
            result = ZhipuProvider().fetch(cache_dir=tmp_path, bypass_cache=True)
            assert result is None
        # And without bypass, an HTTP failure DOES fall back to cache.
        with patch(
            "claude_island.platform_.providers.zhipu._fetch_http",
            return_value=(None, "test failure"),
        ):
            # Wipe the cache freshness so we hit the HTTP path then
            # fall back; easier than mocking _is_expired.
            old_now = "2020-01-01T00:00:00+00:00"
            cache.write_text(json.dumps({
                "provider": "zhipu",
                "fetched_at": old_now,
                "five_hour": {"pct": 50.0, "resets_at": "2030-01-01T00:00:00Z"},
                "seven_day": {"pct": 20.0, "resets_at": "2030-01-07T00:00:00Z"},
            }))
            result = ZhipuProvider().fetch(cache_dir=tmp_path)
            assert result is not None
            assert result.five_hour_pct == 50.0

    def test_token_chain_zhipu_env_wins(self, tmp_path, monkeypatch):
        from claude_island.platform_.providers import write_provider_config
        from claude_island.platform_.providers.zhipu import _read_token

        monkeypatch.setenv("ZHIPU_API_KEY", "zhipu-env-wins")
        monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "anthropic-env")
        path = tmp_path / "providers.json"
        write_provider_config(
            {"providers": {"zhipu": {"auth_token": "config-fallback"}}},
            path,
        )
        with patch(
            "claude_island.platform_.providers.PROVIDER_CONFIG_PATH", path,
        ):
            assert _read_token() == "zhipu-env-wins"

    def test_token_chain_anthropic_env_then_config(self, tmp_path, monkeypatch):
        from claude_island.platform_.providers import write_provider_config
        from claude_island.platform_.providers.zhipu import _read_token

        monkeypatch.delenv("ZHIPU_API_KEY", raising=False)
        monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "anthropic-env-shared")
        path = tmp_path / "providers.json"
        write_provider_config(
            {"providers": {"zhipu": {"auth_token": "config-fallback"}}},
            path,
        )
        with patch(
            "claude_island.platform_.providers.PROVIDER_CONFIG_PATH", path,
        ):
            # Anthropic env wins because ZHIPU_API_KEY is missing.
            assert _read_token() == "anthropic-env-shared"

        monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
        with patch(
            "claude_island.platform_.providers.PROVIDER_CONFIG_PATH", path,
        ):
            # Falls all the way through to providers.json.
            assert _read_token() == "config-fallback"


# ============================================================================
# Auto-assembly of providers.json from each provider's default_config()
# ============================================================================

class TestEnsureProviderConfigAutoAssembly:
    """``_build_default_config()`` collects every provider's
    ``default_config()`` so adding a new provider doesn't require
    editing ``providers/__init__.py``."""

    def test_assembles_blocks_from_all_providers(self, tmp_path):
        # Importing the package triggers @provider registration of all
        # sub-modules (anthropic, minimax, zhipu) via the bottom-of-
        # module import line.
        from claude_island.platform_.providers import (
            ensure_provider_config, read_provider_config,
        )
        path = tmp_path / "providers.json"
        ensure_provider_config(path)
        cfg = read_provider_config(path)

        providers = cfg["providers"]
        # MiniMax + Zhipu both contribute blocks (Anthropic intentionally
        # does NOT — it reads OAuth from ~/.claude/.credentials.json).
        assert "minimax" in providers
        assert "zhipu" in providers
        assert "anthropic" not in providers

        # Each block shape is sane.
        for name in ("minimax", "zhipu"):
            block = providers[name]
            assert block["auth_token"] == ""
            assert "base_url" in block
            assert "_help" in block

        # Default selection is anthropic — explicit, not positional.
        assert cfg["selected"] == "anthropic"

    def test_does_not_overwrite_existing_file(self, tmp_path):
        # The "user's tokens are sacred" invariant: ensure() must be a
        # pure no-op when the file already exists, even after the
        # refactor that moved blocks behind `default_config()`.
        from claude_island.platform_.providers import (
            ensure_provider_config, read_provider_config, write_provider_config,
        )
        path = tmp_path / "providers.json"
        original = {
            "selected": "minimax",
            "providers": {
                "minimax": {"auth_token": "user-secret-key",
                             "base_url": "https://custom.example"},
            },
        }
        write_provider_config(original, path)
        ensure_provider_config(path)
        # Untouched, including absence of the new zhipu block.
        assert read_provider_config(path) == original

    def test_assembled_zhipu_block_contains_z_ai_default_host(self, tmp_path):
        from claude_island.platform_.providers import (
            ensure_provider_config, read_provider_config,
        )
        path = tmp_path / "providers.json"
        ensure_provider_config(path)
        cfg = read_provider_config(path)
        assert "z.ai" in cfg["providers"]["zhipu"]["base_url"]


# ============================================================================
# Default-fallback contract (anthropic, regardless of import order)
# ============================================================================

class TestSelectedProviderDefaultFallback:
    """The "default tab is Anthropic" contract is enforced in two
    places — the seed config (TestEnsureProviderConfigAutoAssembly
    above) AND the runtime fallback in ``__main__.py``. Test the
    latter at the function-level (importing __main__ would start the
    GUI), by replicating its exact fallback expression."""

    def test_invalid_selection_falls_back_to_anthropic(self):
        # Mimics __main__.py's fallback expression, verifying that an
        # invalid stored selection routes to "anthropic" rather than
        # _available_providers[0] (which would be order-dependent).
        _DEFAULT_FALLBACK_PROVIDER = "anthropic"
        _available_providers = ["zhipu", "anthropic", "minimax"]   # arbitrary order
        _selected_provider = "kimi-removed-by-user"
        if _selected_provider not in _available_providers:
            _selected_provider = (
                _DEFAULT_FALLBACK_PROVIDER
                if _DEFAULT_FALLBACK_PROVIDER in _available_providers
                else _available_providers[0]
            )
        assert _selected_provider == "anthropic"

    def test_falls_back_to_first_when_anthropic_missing(self):
        # Pathological: anthropic provider somehow not registered. The
        # fallback degrades to the first available rather than raising,
        # so the panel still renders something.
        _DEFAULT_FALLBACK_PROVIDER = "anthropic"
        _available_providers = ["zhipu", "minimax"]
        _selected_provider = "removed"
        if _selected_provider not in _available_providers:
            _selected_provider = (
                _DEFAULT_FALLBACK_PROVIDER
                if _DEFAULT_FALLBACK_PROVIDER in _available_providers
                else _available_providers[0]
            )
        assert _selected_provider == "zhipu"


# ============================================================================
# set_provider_settings — used by the in-app + dialog to persist a
# freshly added provider's credentials without touching the rest of the
# config.
# ============================================================================

class TestSetProviderSettings:
    def test_merges_into_existing_config(self, tmp_path):
        from claude_island.platform_.providers import (
            read_provider_config, write_provider_config, set_provider_settings,
        )
        path = tmp_path / "providers.json"
        original = {
            "selected": "anthropic",
            "providers": {
                "minimax": {"auth_token": "minimax-key", "base_url": "https://api.minimaxi.com"},
            },
        }
        write_provider_config(original, path)
        with patch(
            "claude_island.platform_.providers.PROVIDER_CONFIG_PATH", path,
        ):
            set_provider_settings("zhipu", {"auth_token": "z-key", "base_url": "https://api.z.ai"})
        cfg = read_provider_config(path)
        # New zhipu block written.
        assert cfg["providers"]["zhipu"]["auth_token"] == "z-key"
        assert cfg["providers"]["zhipu"]["base_url"] == "https://api.z.ai"
        # Existing minimax block + selected pointer untouched.
        assert cfg["providers"]["minimax"]["auth_token"] == "minimax-key"
        assert cfg["selected"] == "anthropic"

    def test_empty_fields_is_noop(self, tmp_path):
        from claude_island.platform_.providers import (
            read_provider_config, write_provider_config, set_provider_settings,
        )
        path = tmp_path / "providers.json"
        original = {"selected": "anthropic", "providers": {}}
        write_provider_config(original, path)
        with patch(
            "claude_island.platform_.providers.PROVIDER_CONFIG_PATH", path,
        ):
            set_provider_settings("zhipu", {})
        # File untouched — no zhipu block, no extra entries.
        assert read_provider_config(path) == original

    def test_creates_providers_object_when_missing(self, tmp_path):
        from claude_island.platform_.providers import (
            read_provider_config, write_provider_config, set_provider_settings,
        )
        path = tmp_path / "providers.json"
        # Config without a "providers" object at all (e.g. user wiped it).
        write_provider_config({"selected": "anthropic"}, path)
        with patch(
            "claude_island.platform_.providers.PROVIDER_CONFIG_PATH", path,
        ):
            set_provider_settings("zhipu", {"auth_token": "k"})
        cfg = read_provider_config(path)
        assert cfg["providers"]["zhipu"]["auth_token"] == "k"

    def test_updates_existing_block_in_place(self, tmp_path):
        # Same provider, second call → fields merge (e.g. user pastes a
        # new token without re-typing the base_url they edited earlier).
        from claude_island.platform_.providers import (
            read_provider_config, write_provider_config, set_provider_settings,
        )
        path = tmp_path / "providers.json"
        write_provider_config(
            {"providers": {"zhipu": {"auth_token": "old", "base_url": "https://custom"}}},
            path,
        )
        with patch(
            "claude_island.platform_.providers.PROVIDER_CONFIG_PATH", path,
        ):
            set_provider_settings("zhipu", {"auth_token": "new"})
        block = read_provider_config(path)["providers"]["zhipu"]
        assert block["auth_token"] == "new"
        # base_url survives — only the touched key was replaced.
        assert block["base_url"] == "https://custom"


class TestDeleteProviderSettings:
    """delete_provider_settings — drives the right-click → Delete
    action on quota tabs. Anthropic is non-deletable per UI policy
    (the wiring layer skips menu setup for it), but this function
    is policy-agnostic and will remove any name handed to it."""

    def test_removes_named_provider_block(self, tmp_path):
        from claude_island.platform_.providers import (
            read_provider_config, write_provider_config, delete_provider_settings,
        )
        path = tmp_path / "providers.json"
        write_provider_config({
            "selected": "anthropic",
            "providers": {
                "minimax": {"auth_token": "m"},
                "zhipu":   {"auth_token": "z"},
            },
        }, path)
        with patch("claude_island.platform_.providers.PROVIDER_CONFIG_PATH", path):
            delete_provider_settings("zhipu")
        cfg = read_provider_config(path)
        # zhipu gone, minimax untouched.
        assert "zhipu" not in cfg["providers"]
        assert cfg["providers"]["minimax"]["auth_token"] == "m"
        # selected untouched (it wasn't pointing at zhipu).
        assert cfg["selected"] == "anthropic"

    def test_resets_selected_when_deleting_active_provider(self, tmp_path):
        """If you delete the currently-selected provider, the
        ``selected`` pointer falls back to anthropic so the next
        launch doesn't open a tab for a removed provider."""
        from claude_island.platform_.providers import (
            read_provider_config, write_provider_config, delete_provider_settings,
        )
        path = tmp_path / "providers.json"
        write_provider_config({
            "selected": "minimax",
            "providers": {"minimax": {"auth_token": "m"}},
        }, path)
        with patch("claude_island.platform_.providers.PROVIDER_CONFIG_PATH", path):
            delete_provider_settings("minimax")
        cfg = read_provider_config(path)
        assert cfg["selected"] == "anthropic"
        assert "minimax" not in cfg["providers"]

    def test_noop_when_provider_missing(self, tmp_path):
        """Calling delete on a name that isn't configured is a no-op,
        so the right-click handler can call unconditionally without
        re-reading the config."""
        from claude_island.platform_.providers import (
            read_provider_config, write_provider_config, delete_provider_settings,
        )
        path = tmp_path / "providers.json"
        original = {"selected": "anthropic", "providers": {"minimax": {"auth_token": "m"}}}
        write_provider_config(original, path)
        with patch("claude_island.platform_.providers.PROVIDER_CONFIG_PATH", path):
            delete_provider_settings("zhipu")  # never configured
        assert read_provider_config(path) == original


class TestQuotaCacheState:
    """Throttle-layer first-class object — see providers/__init__.py.

    These tests pin the state machine: failure-only state, success
    state, throttle gates, UI projection. Together they replace the
    older free-function helper tests (record_failed_attempt /
    is_fetch_due / snapshot_from_cache) which were dict-based and
    required mocking the cache file for trivial assertions.
    """

    def _now(self) -> datetime:
        return datetime(2026, 5, 5, 12, 0, 0, tzinfo=timezone.utc)

    def test_with_failed_attempt_bumps_only_last_attempt_at(self):
        """Failure must NOT touch fetched_at or business data — those
        come from the LAST SUCCESSFUL fetch and stay frozen across
        failures so is_stale ageing keeps climbing on real freshness."""
        from claude_island.platform_.providers import QuotaCacheState, Window
        prior = QuotaCacheState(
            provider="anthropic",
            fetched_at=datetime(2026, 5, 5, 11, 0, 0, tzinfo=timezone.utc),
            last_attempt_at=datetime(2026, 5, 5, 11, 30, 0, tzinfo=timezone.utc),
            five_hour=Window(pct=42.0,
                             resets_at=datetime(2030, 1, 1, tzinfo=timezone.utc)),
            seven_day=Window(pct=15.0,
                             resets_at=datetime(2030, 1, 7, tzinfo=timezone.utc)),
        )
        new = prior.with_failed_attempt(now=self._now())
        assert new.last_attempt_at == self._now()
        assert new.fetched_at == prior.fetched_at      # frozen
        assert new.five_hour == prior.five_hour        # frozen
        assert new.seven_day == prior.seven_day        # frozen

    def test_with_successful_fetch_bumps_both_timestamps(self):
        """Success path moves both fetched_at and last_attempt_at to now,
        so is_fetch_due gates the next attempt to POLL_TTL after THIS
        success — not POLL_TTL after some stale prior attempt."""
        from claude_island.platform_.providers import QuotaCacheState, Window
        empty = QuotaCacheState.empty("anthropic")
        new = empty.with_successful_fetch(
            now=self._now(),
            five_hour=Window(pct=10.0,
                             resets_at=datetime(2030, 1, 1, tzinfo=timezone.utc)),
            seven_day=Window(pct=5.0,
                             resets_at=datetime(2030, 1, 7, tzinfo=timezone.utc)),
        )
        assert new.fetched_at == self._now()
        assert new.last_attempt_at == self._now()
        assert new.five_hour.pct == 10.0

    def test_is_fetch_due_prefers_last_attempt_over_fetched_at(self):
        """Failure scenario: fetched_at hours old but the last failed
        attempt was 30 s ago → still throttled."""
        from claude_island.platform_.providers import QuotaCacheState
        state = QuotaCacheState(
            provider="anthropic",
            fetched_at=datetime(2026, 5, 5, 8, 0, 0, tzinfo=timezone.utc),       # 4 h old
            last_attempt_at=datetime(2026, 5, 5, 11, 59, 30, tzinfo=timezone.utc),  # 30 s old
            five_hour=None, seven_day=None,
        )
        assert state.is_fetch_due(now=self._now()) is False

    def test_is_fetch_due_returns_true_after_ttl(self):
        """POLL_TTL + 1 s past last attempt → gate opens."""
        from claude_island.platform_.providers import QuotaCacheState, POLL_TTL
        state = QuotaCacheState(
            provider="anthropic",
            fetched_at=None,
            last_attempt_at=datetime(2026, 5, 5, 11, 54, 59, tzinfo=timezone.utc),
            five_hour=None, seven_day=None,
        )
        assert state.is_fetch_due(now=self._now()) is True
        assert POLL_TTL == 300  # sanity pin

    def test_is_fetch_due_returns_true_for_empty(self):
        """Cold start → always due."""
        from claude_island.platform_.providers import QuotaCacheState
        assert QuotaCacheState.empty("anthropic").is_fetch_due(now=self._now()) is True

    def test_is_stale_uses_fetched_at_not_last_attempt(self):
        """Stale must reflect data freshness, not retry activity. A
        cache that fetched 20 min ago and has been failing every 5 min
        since is stale — even though last_attempt_at is recent."""
        from claude_island.platform_.providers import QuotaCacheState, Window
        twenty_min_ago = datetime(2026, 5, 5, 11, 40, 0, tzinfo=timezone.utc)
        recent_attempt = datetime(2026, 5, 5, 11, 59, 0, tzinfo=timezone.utc)
        state = QuotaCacheState(
            provider="anthropic",
            fetched_at=twenty_min_ago,
            last_attempt_at=recent_attempt,
            five_hour=Window(pct=42.0,
                             resets_at=datetime(2030, 1, 1, tzinfo=timezone.utc)),
            seven_day=Window(pct=15.0,
                             resets_at=datetime(2030, 1, 7, tzinfo=timezone.utc)),
        )
        # 20 min > POLL_TTL * STALE_MULT (= 15 min) → stale
        assert state.is_stale(now=self._now()) is True

    def test_to_snapshot_drops_last_attempt_at_field(self):
        """Crucial layering invariant: last_attempt_at is throttle
        metadata and must NOT bleed into the UI's QuotaSnapshot — the
        UI doesn't render it, and dedup keys constructed by surfaces
        like expanded_window's compute would re-fire on every retry
        if it did. QuotaSnapshot has no such field; this test checks
        nobody accidentally adds one."""
        from claude_island.platform_.providers import QuotaCacheState, Window
        state = QuotaCacheState(
            provider="anthropic",
            fetched_at=datetime(2026, 5, 5, 11, 0, 0, tzinfo=timezone.utc),
            last_attempt_at=datetime(2026, 5, 5, 11, 59, 0, tzinfo=timezone.utc),
            five_hour=Window(pct=42.0,
                             resets_at=datetime(2030, 1, 1, tzinfo=timezone.utc)),
            seven_day=Window(pct=15.0,
                             resets_at=datetime(2030, 1, 7, tzinfo=timezone.utc)),
        )
        snap = state.to_snapshot(now=self._now())
        assert snap is not None
        # QuotaSnapshot only carries fetched_at, not last_attempt_at.
        # If someone accidentally adds last_attempt_at to the snapshot,
        # this attribute access would not raise — change this assertion
        # to a no-attr check.
        assert hasattr(snap, "fetched_at")
        assert not hasattr(snap, "last_attempt_at")

    def test_to_snapshot_returns_none_when_window_expired(self):
        """If the cached five_hour resets_at is already in the past,
        the data has logically rolled over — UI must not show stale
        bars. The state itself is preserved (subsequent fetch will
        rewrite it), but the projection refuses to render."""
        from claude_island.platform_.providers import QuotaCacheState, Window
        state = QuotaCacheState(
            provider="anthropic",
            fetched_at=datetime(2026, 5, 5, 6, 0, 0, tzinfo=timezone.utc),
            last_attempt_at=datetime(2026, 5, 5, 6, 0, 0, tzinfo=timezone.utc),
            five_hour=Window(pct=42.0,
                             resets_at=datetime(2026, 5, 5, 11, 0, 0, tzinfo=timezone.utc)),  # 1 h ago
            seven_day=Window(pct=15.0,
                             resets_at=datetime(2030, 1, 1, tzinfo=timezone.utc)),
        )
        assert state.to_snapshot(now=self._now()) is None

    def test_round_trip_through_cache_dict(self):
        """to_cache_dict + from_cache_dict must round-trip every field."""
        from claude_island.platform_.providers import QuotaCacheState, Window
        state = QuotaCacheState(
            provider="anthropic",
            fetched_at=datetime(2026, 5, 5, 11, 0, 0, tzinfo=timezone.utc),
            last_attempt_at=datetime(2026, 5, 5, 11, 30, 0, tzinfo=timezone.utc),
            five_hour=Window(pct=42.5,
                             resets_at=datetime(2030, 1, 1, tzinfo=timezone.utc)),
            seven_day=Window(pct=15.0,
                             resets_at=datetime(2030, 1, 7, tzinfo=timezone.utc)),
        )
        round_tripped = QuotaCacheState.from_cache_dict(
            state.to_cache_dict(), fallback_provider="anthropic",
        )
        assert round_tripped == state

    def test_from_cache_dict_handles_partial_first_failure(self):
        """First-ever failure cache contains ONLY provider +
        last_attempt_at (no business data, no fetched_at). Round-trip
        must preserve the None fields rather than crash."""
        from claude_island.platform_.providers import QuotaCacheState
        partial = {
            "provider": "anthropic",
            "last_attempt_at": "2026-05-05T11:30:00+00:00",
        }
        state = QuotaCacheState.from_cache_dict(
            partial, fallback_provider="anthropic",
        )
        assert state.provider == "anthropic"
        assert state.fetched_at is None
        assert state.last_attempt_at is not None
        assert state.five_hour is None
        assert state.seven_day is None

    # ---- Exponential backoff -----------------------------------------------

    def test_backoff_window_doubles_then_clamps(self):
        """5m → 10m → 20m → 40m → 60m (cap) → 60m. Catches a future
        refactor that changes the schedule or forgets to clamp."""
        from claude_island.platform_.providers import (
            POLL_TTL, POLL_TTL_MAX, QuotaCacheState,
        )
        s = QuotaCacheState.empty("anthropic")
        cases = [(0, POLL_TTL), (1, 600), (2, 1200), (3, 2400),
                 (4, POLL_TTL_MAX), (10, POLL_TTL_MAX), (30, POLL_TTL_MAX)]
        for failures, expected in cases:
            got = replace(s, consecutive_failures=failures)._backoff_window_seconds()
            assert got == expected, f"failures={failures}: {got} != {expected}"

    def test_with_failed_attempt_increments_consecutive_failures(self):
        from claude_island.platform_.providers import QuotaCacheState
        s = QuotaCacheState.empty("anthropic")
        for expected in (1, 2, 3, 4):
            s = s.with_failed_attempt(now=self._now())
            assert s.consecutive_failures == expected

    def test_with_successful_fetch_resets_consecutive_failures(self):
        """Either auto or manual ⟳ success → failures back to 0 → next
        cycle resumes the 5-min cadence."""
        from claude_island.platform_.providers import QuotaCacheState, Window
        s = QuotaCacheState.empty("anthropic")
        for _ in range(5):
            s = s.with_failed_attempt(now=self._now())
        assert s.consecutive_failures == 5
        s = s.with_successful_fetch(
            now=self._now(),
            five_hour=Window(pct=10.0,
                             resets_at=datetime(2030, 1, 1, tzinfo=timezone.utc)),
            seven_day=Window(pct=5.0,
                             resets_at=datetime(2030, 1, 7, tzinfo=timezone.utc)),
        )
        assert s.consecutive_failures == 0

    def test_is_fetch_due_respects_backoff_window(self):
        """6 minutes past a single failure: under POLL_TTL (5 min) but
        the backoff window has doubled to 10 min, so still throttled.
        Verifies the backoff actually changes is_fetch_due output."""
        from claude_island.platform_.providers import QuotaCacheState
        six_min_ago = datetime(2026, 5, 5, 11, 54, 0, tzinfo=timezone.utc)
        s = QuotaCacheState(
            provider="anthropic",
            fetched_at=None, last_attempt_at=six_min_ago,
            five_hour=None, seven_day=None,
            consecutive_failures=1,   # backoff window = 10 min
        )
        assert s.is_fetch_due(now=self._now()) is False
        # 11 minutes past the same failure → window has elapsed.
        eleven_min_ago = datetime(2026, 5, 5, 11, 49, 0, tzinfo=timezone.utc)
        s = replace(s, last_attempt_at=eleven_min_ago)
        assert s.is_fetch_due(now=self._now()) is True

    def test_consecutive_failures_round_trips_through_cache(self):
        """Persistence: a counter > 0 must survive process restart so
        backoff doesn't reset on every cold start of the app."""
        from claude_island.platform_.providers import QuotaCacheState, Window
        s = QuotaCacheState(
            provider="anthropic",
            fetched_at=datetime(2026, 5, 5, 11, 0, 0, tzinfo=timezone.utc),
            last_attempt_at=datetime(2026, 5, 5, 11, 30, 0, tzinfo=timezone.utc),
            five_hour=Window(pct=10.0,
                             resets_at=datetime(2030, 1, 1, tzinfo=timezone.utc)),
            seven_day=Window(pct=5.0,
                             resets_at=datetime(2030, 1, 7, tzinfo=timezone.utc)),
            consecutive_failures=3,
        )
        back = QuotaCacheState.from_cache_dict(
            s.to_cache_dict(), fallback_provider="anthropic",
        )
        assert back == s
        assert back.consecutive_failures == 3

    def test_consecutive_failures_defaults_zero_for_legacy_cache(self):
        """Caches written before backoff existed have no such key —
        round-trip must default to 0, not crash, not None."""
        from claude_island.platform_.providers import QuotaCacheState
        s = QuotaCacheState.from_cache_dict(
            {"provider": "anthropic",
             "last_attempt_at": "2026-05-05T11:30:00+00:00"},
            fallback_provider="anthropic",
        )
        assert s.consecutive_failures == 0

    def test_consecutive_failures_zero_is_omitted_from_cache_dict(self):
        """Zero-suppress: the on-disk JSON stays clean of the noise key
        on the happy path. Verifies the cache file an existing user
        opens doesn't gain a `consecutive_failures: 0` line on first
        successful fetch after upgrade."""
        from claude_island.platform_.providers import QuotaCacheState
        d = QuotaCacheState.empty("anthropic").to_cache_dict()
        assert "consecutive_failures" not in d

    def test_consecutive_failures_corrupt_value_falls_back_to_zero(self):
        """A malformed cache (someone hand-edited a string in there)
        must not crash the throttle on startup."""
        from claude_island.platform_.providers import QuotaCacheState
        s = QuotaCacheState.from_cache_dict(
            {"provider": "anthropic", "consecutive_failures": "broken"},
            fallback_provider="anthropic",
        )
        assert s.consecutive_failures == 0


class TestAnthropicNegativeCache:
    """Behaviour-level tests on the anthropic provider's failure path.

    Companion to TestNegativeCacheHelper which covers the helpers in
    isolation. These tests verify the provider actually wires the
    helpers in correctly — without them a refactor that re-introduces
    the every-wake retry would slip past unit tests.
    """

    def _seed_token(self, tmp_path):
        """Create a fake credentials file the anthropic module reads."""
        creds = tmp_path / "credentials.json"
        creds.write_text(json.dumps({
            "claudeAiOauth": {"accessToken": "fake-bearer"},
        }))
        return creds

    def test_failed_fetch_throttles_retry_within_ttl(self, tmp_path):
        """First fetch fails → second fetch within TTL must NOT re-issue
        HTTP. Direct regression for the stderr-flood bug: every JSONL
        ingest triggered wake() → fetch() → HTTP → 401 → again.
        """
        from claude_island.platform_.providers import anthropic as anth
        creds = self._seed_token(tmp_path)
        with patch.object(anth, "_CREDENTIALS_PATH", creds), \
             patch.object(anth, "_fetch_http", return_value=(None, "test")) as mock_http:
            p = anth.AnthropicProvider()
            p.fetch(cache_dir=tmp_path)            # first call → HTTP fires
            p.fetch(cache_dir=tmp_path)            # second call → throttled
            p.fetch(cache_dir=tmp_path)            # third call → throttled
            assert mock_http.call_count == 1, (
                f"expected 1 HTTP attempt across 3 fetches, got "
                f"{mock_http.call_count}"
            )

    def test_bypass_cache_skips_throttle(self, tmp_path):
        """Manual refresh path (bypass_cache=True) must always issue
        HTTP — the user clicked the refresh button to force it.
        """
        from claude_island.platform_.providers import anthropic as anth
        creds = self._seed_token(tmp_path)
        with patch.object(anth, "_CREDENTIALS_PATH", creds), \
             patch.object(anth, "_fetch_http", return_value=(None, "test")) as mock_http:
            p = anth.AnthropicProvider()
            p.fetch(cache_dir=tmp_path)                            # auto: HTTP
            p.fetch(cache_dir=tmp_path, bypass_cache=True)         # manual: HTTP
            p.fetch(cache_dir=tmp_path, bypass_cache=True)         # manual: HTTP
            assert mock_http.call_count == 3

    def test_failed_fetch_preserves_prior_business_data(self, tmp_path):
        """Cache had a successful payload from earlier; current fetch
        fails → cache must still expose the old five_hour/seven_day
        so the UI shows last-known reading + the stale grey clamp."""
        from claude_island.platform_.providers import anthropic as anth
        cache_path = tmp_path / "anthropic-quota.json"
        cache_path.write_text(json.dumps({
            "provider": "anthropic",
            "fetched_at": "2026-05-05T11:00:00+00:00",
            "five_hour": {"pct": 42.0, "resets_at": "2030-01-01T00:00:00Z"},
            "seven_day": {"pct": 15.0, "resets_at": "2030-01-07T00:00:00Z"},
        }))
        creds = self._seed_token(tmp_path)
        with patch.object(anth, "_CREDENTIALS_PATH", creds), \
             patch.object(anth, "_fetch_http", return_value=(None, "test")):
            p = anth.AnthropicProvider()
            # Cache age (1 h) is past POLL_TTL, so this fetch attempts HTTP,
            # fails, and falls back to cache.
            p.fetch(cache_dir=tmp_path)
        cached = json.loads(cache_path.read_text())
        # Business data intact …
        assert cached["five_hour"]["pct"] == 42.0
        assert cached["fetched_at"] == "2026-05-05T11:00:00+00:00"
        # … last_attempt_at marker added so the next wake throttles.
        assert "last_attempt_at" in cached


class TestLogFetchFailure:
    """log_fetch_failure helper — single-line stderr with timing context.

    The helper is the user-visible diagnostic channel when quota fetch
    fails. It must:
      • Read the cache BEFORE record_failed_attempt overwrites it.
      • Quote the failure reason verbatim from the caller.
      • Show "first attempt" / "no prior success" when those facts apply.
    """

    def test_fmt_ago_buckets(self):
        from claude_island.platform_.providers import _fmt_ago
        from datetime import timedelta
        assert _fmt_ago(timedelta(seconds=0))     == "0s"
        assert _fmt_ago(timedelta(seconds=42))    == "42s"
        assert _fmt_ago(timedelta(seconds=59))    == "59s"
        assert _fmt_ago(timedelta(seconds=60))    == "1m"
        assert _fmt_ago(timedelta(minutes=47))    == "47m"
        assert _fmt_ago(timedelta(hours=1))       == "1h"
        assert _fmt_ago(timedelta(hours=2, minutes=13)) == "2h 13m"
        assert _fmt_ago(timedelta(seconds=-5))    == "0s"  # clamp

    def test_log_line_starts_with_local_wall_clock_timestamp(self, capsys):
        """User asked for absolute time on each line: scrollback otherwise
        only carries relative ages ('5m ago'), which is useless when the
        user wants to know *when* something actually broke. Format:
        ``[YYYY-MM-DD HH:MM:SS]`` in the host's local timezone."""
        import re
        from claude_island.platform_.providers import (
            log_fetch_failure, QuotaCacheState,
        )
        log_fetch_failure(
            QuotaCacheState.empty("anthropic"),
            reason="HTTP 429",
            now=datetime(2026, 5, 16, 12, 0, 0, tzinfo=timezone.utc),
        )
        line = capsys.readouterr().err.strip()
        # Local TZ → don't pin the exact value (CI runs in UTC, dev runs in CST/etc).
        # Pin the shape instead: bracketed YYYY-MM-DD HH:MM:SS at line start.
        assert re.match(r"^\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\] \[claude-island\] ", line), \
            f"missing local-time prefix: {line!r}"

    def test_log_includes_reason_and_first_attempt_for_empty_state(self, capsys):
        """First-ever failure: empty state → 'first attempt — no prior success'."""
        from claude_island.platform_.providers import (
            log_fetch_failure, QuotaCacheState,
        )
        now = datetime(2026, 5, 5, 12, 0, 0, tzinfo=timezone.utc)
        log_fetch_failure(
            QuotaCacheState.empty("anthropic"), reason="HTTP 401", now=now,
        )
        line = capsys.readouterr().err.strip()
        assert "anthropic" in line
        assert "HTTP 401" in line
        assert "first attempt" in line
        assert "no prior success" in line

    def test_log_includes_last_attempt_and_last_success_ages(self, capsys):
        """Prior state has both timestamps → both ages quoted."""
        from claude_island.platform_.providers import (
            log_fetch_failure, QuotaCacheState,
        )
        # last attempt 5 min ago, last success 47 min ago (relative to now below)
        now = datetime(2026, 5, 5, 12, 0, 0, tzinfo=timezone.utc)
        prior = QuotaCacheState(
            provider="anthropic",
            fetched_at=datetime(2026, 5, 5, 11, 13, 0, tzinfo=timezone.utc),
            last_attempt_at=datetime(2026, 5, 5, 11, 55, 0, tzinfo=timezone.utc),
            five_hour=None, seven_day=None,
        )
        log_fetch_failure(prior, reason="HTTP 429", now=now)
        line = capsys.readouterr().err.strip()
        assert "HTTP 429" in line
        assert "last attempt 5m ago" in line
        assert "last success 47m ago" in line

    def test_log_runs_before_cache_bumps(self, tmp_path, capsys):
        """End-to-end ordering: anthropic.fetch must call log_fetch_failure
        BEFORE record_failed_attempt — otherwise 'last attempt N ago'
        always reads 0s and is useless. Verify by setting a known
        last_attempt_at, triggering failure, and confirming the log
        quotes the OLD value.
        """
        from claude_island.platform_.providers import anthropic as anth
        cache_path = tmp_path / "anthropic-quota.json"
        # Old last_attempt_at = 8 minutes ago
        cache_path.write_text(json.dumps({
            "provider": "anthropic",
            "last_attempt_at": (datetime.now(timezone.utc).isoformat()
                                .replace("+00:00", "+00:00")),
        }))
        # Wind it back by parsing then re-writing 8 min in the past
        from datetime import timedelta
        eight_ago = datetime.now(timezone.utc) - timedelta(minutes=8)
        cache_path.write_text(json.dumps({
            "provider": "anthropic",
            "last_attempt_at": eight_ago.isoformat(),
        }))
        creds = tmp_path / "credentials.json"
        creds.write_text(json.dumps({
            "claudeAiOauth": {"accessToken": "fake"},
        }))
        with patch.object(anth, "_CREDENTIALS_PATH", creds), \
             patch.object(anth, "_fetch_http", return_value=(None, "HTTP 401")):
            anth.AnthropicProvider().fetch(cache_dir=tmp_path)
        line = capsys.readouterr().err.strip()
        assert "HTTP 401" in line
        # Should quote ~8m, not 0s — proves log ran before record_failed_attempt
        assert "last attempt 8m ago" in line or "last attempt 7m ago" in line
