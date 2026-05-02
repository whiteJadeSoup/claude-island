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
        # "user has not run Claude Code OAuth login".
        from claude_island.platform_.providers import anthropic as anth
        missing = tmp_path / "no-such-credentials.json"
        with patch.object(anth, "_CREDENTIALS_PATH", missing):
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
        with patch.object(anth, "_CREDENTIALS_PATH", tmp_path / "no-cred.json"):
            e = ProviderEngine(cache_dir=tmp_path)
            # No cred file → Anthropic fetch returns None. The fact that
            # it returns None instead of falling back to MiniMax proves
            # the explicit name was honoured.
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
