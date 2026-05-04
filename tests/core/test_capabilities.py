"""Unit tests for core/capabilities.py — decorator + mixin + enums.

Happy/edge/error per the detail design.
"""
from __future__ import annotations

import pytest
from claude_island.core.capabilities import (
    CAPABILITY_SCOPE, Capability, FocusGranularity, Scope,
    _CapabilityProvider, capability,
)


# ── Enums ─────────────────────────────────────────────────────────────

class TestCapabilityEnum:
    def test_every_cap_has_a_scope(self):
        for cap in Capability:
            assert cap in CAPABILITY_SCOPE, f"{cap} missing from CAPABILITY_SCOPE"

    def test_terminal_scope_caps(self):
        assert CAPABILITY_SCOPE[Capability.FOCUS] == Scope.TERMINAL

    def test_os_scope_caps(self):
        assert CAPABILITY_SCOPE[Capability.REVEAL_CWD] == Scope.OS
        assert CAPABILITY_SCOPE[Capability.COPY_PATH] == Scope.OS

    def test_app_scope_caps(self):
        assert CAPABILITY_SCOPE[Capability.RENAME] == Scope.APP
        assert CAPABILITY_SCOPE[Capability.RESET_THINKING] == Scope.APP

    def test_launch_is_terminal_scope(self):
        """LAUNCH is declarative-grouped under TERMINAL even though it's
        invoked via dispatcher.launch() (not dispatch())."""
        assert CAPABILITY_SCOPE[Capability.LAUNCH] == Scope.TERMINAL

    def test_focus_granularity_values(self):
        assert FocusGranularity.PANE == "pane"
        assert FocusGranularity.TAB == "tab"
        assert FocusGranularity.APP == "app"


# ── LAUNCH contract types ─────────────────────────────────────────────

class TestLaunchTypes:
    def test_spawn_result_importable_and_frozen(self):
        from datetime import datetime, timezone
        from claude_island.core.capabilities import SpawnResult
        sr = SpawnResult(
            terminal_name="x", terminal_pid=1,
            started_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        )
        with pytest.raises(Exception):
            sr.terminal_pid = 999  # frozen dataclass

    def test_launcher_spawn_error_is_runtime_error(self):
        from claude_island.core.capabilities import LauncherSpawnError
        assert issubclass(LauncherSpawnError, RuntimeError)


# ── @capability decorator ──────────────────────────────────────────────

class TestCapabilityDecorator:
    def test_stamps_capability_on_function(self):
        @capability(Capability.FOCUS)
        def focus(self, view): ...
        assert focus._capability == Capability.FOCUS

    def test_rejects_name_mismatch(self):
        with pytest.raises(TypeError) as ctx:
            @capability(Capability.FOCUS)
            def do_focus(self, view): ...
        assert "do_focus" in str(ctx.value)
        assert "focus" in str(ctx.value)


# ── _CapabilityProvider mixin ──────────────────────────────────────────

class TestCapabilityProvider:
    def test_empty_when_no_capabilities(self):
        class Blank(_CapabilityProvider): ...
        assert Blank.capabilities == frozenset()

    def test_collects_decorated_methods(self):
        class Worker(_CapabilityProvider):
            @capability(Capability.FOCUS)
            def focus(self, view): return True

        assert Worker.capabilities == {Capability.FOCUS}

    def test_union_with_inherited(self):
        class Base(_CapabilityProvider):
            @capability(Capability.FOCUS)
            def focus(self, view): return True

        class Derived(Base):
            @capability(Capability.RENAME)
            def rename(self, view, *, new_name): return True

        assert Derived.capabilities == {Capability.FOCUS, Capability.RENAME}
        assert Base.capabilities == {Capability.FOCUS}

    def test_override_does_not_change_set(self):
        class Base(_CapabilityProvider):
            @capability(Capability.FOCUS)
            def focus(self, view): ...

        class Override(Base):
            @capability(Capability.FOCUS)
            def focus(self, view): return False

        assert Override.capabilities == {Capability.FOCUS}
