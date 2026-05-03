"""Unit tests for TerminalDispatcher — data flow (group_sessions)
and control flow (dispatch) with mock adapters/backends.

Verifies scope routing, capability merging, adapter chain ordering,
degraded state, and error handling.
"""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import pytest
from claude_island.core.capabilities import (
    CAPABILITY_SCOPE, Capability, FocusGranularity, Scope,
    _CapabilityProvider, capability,
)
from claude_island.core.models import Session
from claude_island.core.snapshot import SessionGroup, SessionView, compose_session_view, _degraded_view
from claude_island.platform_.dispatcher import TerminalDispatcher
from claude_island.platform_.terminals.protocols import TerminalAdapter


# ── Fixtures ──────────────────────────────────────────────────────────

@pytest.fixture
def view_a() -> SessionView:
    s = Session(pid=10, project_path=Path("/tmp/a"), session_uuid="u-a",
                window_handle=None, last_activity=datetime(2026,5,1,12,0,tzinfo=timezone.utc))
    return _degraded_view(s)


@pytest.fixture
def view_b() -> SessionView:
    s = Session(pid=20, project_path=Path("/tmp/b"), session_uuid="u-b",
                window_handle=None, last_activity=datetime(2026,5,1,12,0,tzinfo=timezone.utc))
    return _degraded_view(s)


# ── Stub adapters ─────────────────────────────────────────────────────

class StubOs(_CapabilityProvider):
    name = "stub-os"

    @capability(Capability.REVEAL_CWD)
    def reveal_cwd(self, view): return True

    @capability(Capability.COPY_PATH)
    def copy_path(self, view): return True


class StubApp(_CapabilityProvider):
    name = "stub-app"

    @capability(Capability.RENAME)
    def rename(self, view, *, new_name): return True

    @capability(Capability.RESET_THINKING)
    def reset_thinking(self, view): return True


class FakeTerminalAdapter(_CapabilityProvider):
    """Minimal adapter exposing FOCUS.

    Tests inject this into the dispatcher to verify chain routing,
    capability merging, and dispatch routing."""
    name = "fake-terminal"
    _priority = 100

    def __init__(self, *, handle: set[int] | None = None):
        self.handle = handle or set()
        self.group_log: list[list[Session]] = []

    def can_handle(self, session: Session) -> bool:
        return session.pid in self.handle

    def group(self, sessions: list[Session]) -> list[SessionGroup]:
        self.group_log.append(sessions)
        views = []
        for s in sessions:
            v = _degraded_view(s)
            v = replace(v, adapter_id=self.name,
                        focus_granularity=FocusGranularity.PANE,
                        capabilities=type(self).capabilities)
            views.append(v)
        return [SessionGroup(
            group_id="test-group",
            title_hint="test group",
            adapter_id=self.name,
            views=tuple(views),
        )]

    @capability(Capability.FOCUS)
    def focus(self, view): return True


class BuggyAdapter(FakeTerminalAdapter):
    name = "buggy-terminal"
    _priority = 50

    def group(self, sessions):
        raise RuntimeError("simulated adapter bug")


# ── group_sessions ────────────────────────────────────────────────────

class TestGroupSessions:
    def test_empty_sessions(self):
        d = TerminalDispatcher(terminals={}, os_backend=StubOs(), app_backend=StubApp())
        assert d.group_sessions([]) == []

    def test_default_empty_registry_returns_empty(self):
        d = TerminalDispatcher(terminals={}, os_backend=StubOs(), app_backend=StubApp())
        s = Session(pid=1, project_path="/x", session_uuid="", window_handle=None,
                    last_activity=datetime(2026,5,1,12,0,tzinfo=timezone.utc))
        groups = d.group_sessions([s])
        # No registered terminal adapter — sessions fall through.
        assert groups == []

    def test_adapter_claims_sessions(self, view_a, view_b):
        ad = FakeTerminalAdapter(handle={10, 20})
        d = TerminalDispatcher(terminals={ad.name: ad}, os_backend=StubOs(), app_backend=StubApp())
        groups = d.group_sessions([view_a.session, view_b.session])
        assert len(groups) == 1
        g = groups[0]
        assert len(g.views) == 2
        assert g.adapter_id == "fake-terminal"

    def test_os_app_caps_merged_into_views(self, view_a):
        ad = FakeTerminalAdapter(handle={10})
        d = TerminalDispatcher(terminals={ad.name: ad}, os_backend=StubOs(), app_backend=StubApp())
        groups = d.group_sessions([view_a.session])
        v = groups[0].views[0]
        # Terminal caps + OS caps + APP caps
        assert Capability.FOCUS in v.capabilities
        assert Capability.REVEAL_CWD in v.capabilities
        assert Capability.RENAME in v.capabilities

    def test_two_adapters_split_sessions(self, view_a, view_b):
        ad1 = FakeTerminalAdapter(handle={10})
        ad1.name = "ad1"; ad1._priority = 100
        ad2 = FakeTerminalAdapter(handle={20})
        ad2.name = "ad2"; ad2._priority = 50
        d = TerminalDispatcher(terminals={ad1.name: ad1, ad2.name: ad2},
                               os_backend=StubOs(), app_backend=StubApp())
        groups = d.group_sessions([view_a.session, view_b.session])
        assert len(groups) == 2
        names = {g.adapter_id for g in groups}
        assert names == {"ad1", "ad2"}

    def test_buggy_adapter_is_skipped(self, view_a):
        buggy = BuggyAdapter(handle={10})
        good = FakeTerminalAdapter(handle={10})
        good.name = "good"; good._priority = 40
        d = TerminalDispatcher(terminals={buggy.name: buggy, good.name: good},
                               os_backend=StubOs(), app_backend=StubApp())
        groups = d.group_sessions([view_a.session])
        # Buggy raises → skipped. Good claims the rest.
        assert len(groups) == 1
        assert groups[0].adapter_id == "good"

    def test_group_exception_preserved_as_empty_when_no_fallback(self, view_a):
        buggy = BuggyAdapter(handle={10})
        d = TerminalDispatcher(terminals={buggy.name: buggy},
                               os_backend=StubOs(), app_backend=StubApp())
        groups = d.group_sessions([view_a.session])
        # Only one adapter, and it raises → no groups
        assert groups == []


# ── dispatch ──────────────────────────────────────────────────────────

class TestDispatch:
    @pytest.fixture
    def disp(self):
        ad = FakeTerminalAdapter(handle={10})
        return TerminalDispatcher(terminals={ad.name: ad}, os_backend=StubOs(), app_backend=StubApp())

    def test_terminal_scope_routes_to_adapter(self, disp, view_a):
        view = replace(view_a, adapter_id="fake-terminal",
                       capabilities={Capability.FOCUS})
        assert disp.dispatch(view, Capability.FOCUS) is True

    def test_os_scope_routes_to_os_backend(self, disp, view_a):
        view = replace(view_a, capabilities={Capability.REVEAL_CWD})
        assert disp.dispatch(view, Capability.REVEAL_CWD) is True

    def test_app_scope_routes_to_app_backend(self, disp, view_a):
        view = replace(view_a, capabilities={Capability.RENAME})
        assert disp.dispatch(view, Capability.RENAME, new_name="test") is True

    def test_cap_not_in_view_returns_false(self, disp, view_a):
        view = replace(view_a, capabilities=frozenset())
        assert disp.dispatch(view, Capability.FOCUS) is False

    def test_method_returns_false_propagates(self, disp, view_a):
        # Inject an adapter whose focus() returns False (simulates
        # "HWND gone" or similar transient failure).
        failing = FakeTerminalAdapter(handle={10})
        failing.name = "failing-adapter"; failing._priority = 50
        failing.focus = lambda view: False
        disp._terminals[failing.name] = failing
        view = replace(view_a, adapter_id="failing-adapter",
                       capabilities={Capability.FOCUS})
        assert disp.dispatch(view, Capability.FOCUS) is False

    def test_unknown_adapter_id_returns_false(self, disp, view_a):
        view = replace(view_a, adapter_id="nonexistent",
                       capabilities={Capability.FOCUS})
        assert disp.dispatch(view, Capability.FOCUS) is False


# ── Capability routing table ──────────────────────────────────────────

class TestCapabilityRouting:
    def test_all_caps_in_routing_table(self):
        for cap in Capability:
            assert cap in CAPABILITY_SCOPE, f"{cap} missing scope"
            assert CAPABILITY_SCOPE[cap] in Scope
