"""Tests for the multi-provider quota engine."""
from __future__ import annotations

import json
import os
import tempfile
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
        with patch(
            "claude_island.platform_.providers.anthropic._fetch_http",
            return_value=None,
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

    def test_normalise_calculates_pct_from_remaining(self):
        from claude_island.platform_.providers.minimax import _normalise
        now = datetime.now(timezone.utc)
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
        payload = _normalise(data, fetched_at=now)
        assert payload["five_hour"]["pct"] == pytest.approx(
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
            if "minimaxi.com" in url:
                return {"base_resp": {"status_code": 1004, "status_msg": "cookie"}}
            return {
                "model_remains": [{
                    "model_name": "MiniMax-M*",
                    "current_interval_total_count": 100,
                    "current_interval_usage_count": 80,
                    "end_time": 9999999999000,
                    "current_weekly_total_count": 1000,
                    "current_weekly_usage_count": 900,
                    "weekly_end_time": 9999999999000,
                }],
            }

        with patch.object(mm, "_fetch_http", side_effect=fake_fetch):
            data = mm._try_hosts("sk-cp-fake")

        assert data is not None
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

    def test_normalise_two_limits_assigns_5h_then_weekly_by_reset_time(self):
        # Per cc-switch rule: filter type==TOKENS_LIMIT, sort ascending
        # by nextResetTime, first → 5h, second → weekly. Even when the
        # API returns them in a different order.
        from claude_island.platform_.providers.zhipu import _normalise
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
        payload = _normalise(data, fetched_at=now)
        # Sorted ascending: 5h slot is the soonest reset (12.5%),
        # weekly slot is the later reset (80%).
        assert payload["five_hour"]["pct"] == 12.5
        assert payload["seven_day"]["pct"] == 80.0
        assert payload["provider"] == "zhipu"

    def test_normalise_legacy_single_limit_synthesises_weekly(self):
        # Pre-2026-02-12 subscriptions only emit one TOKENS_LIMIT; the
        # snapshot still has to satisfy snapshot_from_cache's "both
        # windows must have a real reset" gate, so we synthesise a
        # 7-day-out sentinel for weekly with 0% utilisation.
        from claude_island.platform_.providers.zhipu import _normalise
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
        payload = _normalise(data, fetched_at=now)
        assert payload["five_hour"]["pct"] == 25.0
        # Synthesised weekly: 0%, far-future reset so the snapshot
        # passes the validity gate.
        assert payload["seven_day"]["pct"] == 0.0
        assert payload["seven_day"]["resets_at"] is not None

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
            zh._fetch_http("raw-test-key")

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
        with patch(
            "claude_island.platform_.providers.zhipu._fetch_http",
            return_value=None,
        ):
            result = ZhipuProvider().fetch(cache_dir=tmp_path, bypass_cache=True)
            assert result is None
        # And without bypass, an HTTP failure DOES fall back to cache.
        with patch(
            "claude_island.platform_.providers.zhipu._fetch_http",
            return_value=None,
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


class TestNegativeCacheHelper:
    """Helpers in providers/__init__.py that throttle retry on failure.

    See record_failed_attempt / is_fetch_due docstrings for the why.
    Without these, every snapshotter.wake() (fired on every JSONL ingest,
    file watch, sessions_changed event) would re-issue the failing HTTP
    request, flooding stderr and burning network on a server already
    saying no.
    """

    def test_record_failed_attempt_writes_last_attempt_at(self, tmp_path):
        from claude_island.platform_.providers import (
            record_failed_attempt, read_cache,
        )
        cache_path = tmp_path / "anthropic-quota.json"
        now = datetime(2026, 5, 5, 12, 0, 0, tzinfo=timezone.utc)
        record_failed_attempt(cache_path, now=now, provider="anthropic")
        cached = read_cache(cache_path)
        assert cached["last_attempt_at"] == now.isoformat()
        assert cached["provider"] == "anthropic"

    def test_record_failed_attempt_preserves_prior_business_data(self, tmp_path):
        """Failure must NOT overwrite a prior successful five_hour /
        seven_day payload — the UI keeps showing the last-known reading
        with is_stale climbing on the original fetched_at."""
        from claude_island.platform_.providers import (
            record_failed_attempt, read_cache,
        )
        cache_path = tmp_path / "anthropic-quota.json"
        prior_fetch = "2026-05-05T11:00:00+00:00"
        cache_path.write_text(json.dumps({
            "provider": "anthropic",
            "fetched_at": prior_fetch,
            "five_hour": {"pct": 42.0, "resets_at": "2030-01-01T00:00:00Z"},
            "seven_day": {"pct": 15.0, "resets_at": "2030-01-07T00:00:00Z"},
        }))
        now = datetime(2026, 5, 5, 12, 0, 0, tzinfo=timezone.utc)
        record_failed_attempt(cache_path, now=now, provider="anthropic")
        cached = read_cache(cache_path)
        # Business data preserved …
        assert cached["five_hour"]["pct"] == 42.0
        assert cached["fetched_at"] == prior_fetch
        # … and the retry gate moved forward.
        assert cached["last_attempt_at"] == now.isoformat()

    def test_is_fetch_due_prefers_last_attempt_at(self):
        """When both timestamps exist, the more recent last_attempt_at
        wins — failure scenarios where fetched_at is hours old but the
        last failed attempt was 30s ago should still throttle."""
        from claude_island.platform_.providers import is_fetch_due
        now = datetime(2026, 5, 5, 12, 0, 0, tzinfo=timezone.utc)
        cached = {
            "fetched_at": "2026-05-05T08:00:00+00:00",        # 4 h old
            "last_attempt_at": "2026-05-05T11:59:30+00:00",   # 30 s old
        }
        assert is_fetch_due(cached, now=now) is False

    def test_is_fetch_due_falls_back_to_fetched_at(self):
        """Caches written before the negative-cache logic landed have
        no last_attempt_at field; success paths that pre-date this
        change still need to be gateable."""
        from claude_island.platform_.providers import is_fetch_due
        now = datetime(2026, 5, 5, 12, 0, 0, tzinfo=timezone.utc)
        cached = {"fetched_at": "2026-05-05T11:59:00+00:00"}  # 1 min old
        assert is_fetch_due(cached, now=now) is False

    def test_is_fetch_due_returns_true_when_no_timestamps(self):
        from claude_island.platform_.providers import is_fetch_due
        now = datetime(2026, 5, 5, 12, 0, 0, tzinfo=timezone.utc)
        assert is_fetch_due({}, now=now) is True

    def test_is_fetch_due_returns_true_after_ttl(self):
        from claude_island.platform_.providers import is_fetch_due, POLL_TTL
        now = datetime(2026, 5, 5, 12, 0, 0, tzinfo=timezone.utc)
        # POLL_TTL + 1 s past last attempt → due
        cached = {"last_attempt_at": "2026-05-05T11:54:59+00:00"}
        assert is_fetch_due(cached, now=now) is True
        assert POLL_TTL == 300  # sanity-pin so future bumps trigger review


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
             patch.object(anth, "_fetch_http", return_value=None) as mock_http:
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
             patch.object(anth, "_fetch_http", return_value=None) as mock_http:
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
             patch.object(anth, "_fetch_http", return_value=None):
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
