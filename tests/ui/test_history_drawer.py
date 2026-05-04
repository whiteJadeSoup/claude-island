"""Tests for HistoryDrawer — render + Resume click flow.

Strategy:
* Use pytest-qt's qtbot fixture for QApplication / signal handling.
* Stub the dispatcher so we can assert the LAUNCH dispatch contract
  without touching subprocess.Popen or any real adapter.
* Mock the launch_intent registry to verify add() is called with
  the exact LaunchIntent shape we expect.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

import pytest

from claude_island.core.capabilities import Capability, LauncherSpawnError, SpawnResult
from claude_island.core.launch_intent import LaunchIntent, LaunchIntentRegistry
from claude_island.core.models import DormantSession
from claude_island.core.snapshot import WorldSnapshot
from claude_island.ui.history_drawer import (
    HistoryDrawer,
    _DormantRow,
    _flags_for_mode,
    _relative_time,
)


# ── Helpers ─────────────────────────────────────────────────────────────

def _dormant(uuid: str = "u1", **kw) -> DormantSession:
    defaults = dict(
        session_uuid=uuid,
        cwd=Path("D:/projects/foo"),
        name=None,
        last_prompt=None,
        last_activity=datetime(2026, 5, 1, 12, tzinfo=timezone.utc),
        started_at=None,
        permission_mode=None,
        git_branch=None,
        cost_usd=0.0,
        turn_count=0,
    )
    defaults.update(kw)
    return DormantSession(**defaults)


def _empty_snap(dormant=(), launching=()) -> WorldSnapshot:
    return WorldSnapshot(
        today_cost_usd=0.0,
        quota=None,
        available_providers=(),
        selected_provider=None,
        fetched_at=datetime(2026, 5, 1, tzinfo=timezone.utc),
        session_groups=(),
        dormant_sessions=tuple(dormant),
        launching_sessions=tuple(launching),
    )


class _FakeDispatcher:
    """Captures launch() calls + lets tests choose what to return."""

    def __init__(
        self,
        *,
        adapters: tuple[str, ...] = ("windows-terminal",),
        spawn_result: SpawnResult | None = None,
        spawn_error: Exception | None = None,
    ):
        self.adapter_names = adapters
        self._spawn_result = spawn_result or SpawnResult(
            terminal_name="windows-terminal",
            terminal_pid=4242,
            started_at=datetime(2026, 5, 1, 12, tzinfo=timezone.utc),
        )
        self._spawn_error = spawn_error
        self.launch_calls: list[dict] = []

    def adapters_with(self, cap):
        if cap is not Capability.LAUNCH:
            return ()
        return tuple((n, mock.Mock()) for n in self.adapter_names)

    def launch(self, adapter_name, *, cwd, command):
        self.launch_calls.append({
            "adapter_name": adapter_name, "cwd": cwd, "command": command,
        })
        if self._spawn_error is not None:
            raise self._spawn_error
        return self._spawn_result


# ── Pure functions ──────────────────────────────────────────────────────

class TestFlagsForMode:
    def test_bypass_translates_to_dangerous_flag(self):
        assert _flags_for_mode("bypassPermissions") == ("--dangerously-skip-permissions",)

    def test_accept_edits_translates(self):
        assert _flags_for_mode("acceptEdits") == ("--permission-mode", "acceptEdits")

    def test_plan_mode_translates(self):
        assert _flags_for_mode("plan") == ("--permission-mode", "plan")

    def test_default_or_none_yields_no_flags(self):
        assert _flags_for_mode("default") == ()
        assert _flags_for_mode(None) == ()
        assert _flags_for_mode("") == ()

    def test_unknown_mode_yields_no_flags(self):
        assert _flags_for_mode("FUTURE_MODE_X") == ()


class TestRelativeTime:
    def test_minutes(self):
        now = datetime(2026, 5, 1, 12, 30, tzinfo=timezone.utc)
        then = datetime(2026, 5, 1, 12, 25, tzinfo=timezone.utc)
        assert _relative_time(then, now=now) == "5m ago"

    def test_hours(self):
        now = datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc)
        then = datetime(2026, 5, 1, 9, 0, tzinfo=timezone.utc)
        assert _relative_time(then, now=now) == "3h ago"

    def test_days(self):
        now = datetime(2026, 5, 5, 12, 0, tzinfo=timezone.utc)
        then = datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc)
        assert _relative_time(then, now=now) == "4d ago"

    def test_falls_back_to_iso_for_old_dates(self):
        now = datetime(2026, 6, 15, tzinfo=timezone.utc)
        then = datetime(2026, 5, 1, tzinfo=timezone.utc)
        assert _relative_time(then, now=now) == "2026-05-01"


# ── HistoryDrawer.compute (dedup key projection) ────────────────────────

class TestCompute:
    def test_compute_changes_when_dormant_changes(self):
        d_a = _dormant("u1", cost_usd=1.0)
        d_b = _dormant("u1", cost_usd=2.0)  # different cost
        snap_a = _empty_snap(dormant=[d_a])
        snap_b = _empty_snap(dormant=[d_b])
        assert HistoryDrawer.compute(snap_a) != HistoryDrawer.compute(snap_b)

    def test_compute_stable_when_unrelated_fields_change(self):
        """Distinct should NOT re-render on unrelated snapshot churn."""
        d = _dormant("u1")
        snap_a = _empty_snap(dormant=[d])
        snap_b = WorldSnapshot(
            today_cost_usd=99.0,  # different
            quota=None,
            available_providers=(),
            selected_provider=None,
            fetched_at=datetime(2026, 5, 1, tzinfo=timezone.utc),
            session_groups=(),
            dormant_sessions=(d,),
            launching_sessions=(),
        )
        assert HistoryDrawer.compute(snap_a) == HistoryDrawer.compute(snap_b)


# ── Drawer rendering ────────────────────────────────────────────────────

@pytest.fixture
def drawer(qtbot):
    """A drawer with a stubbed dispatcher + real LaunchIntentRegistry +
    a recording on_wake. Parent (expanded) is just a bare QWidget so
    _reposition() doesn't blow up."""
    from PySide6.QtWidgets import QWidget
    parent = QWidget()
    qtbot.addWidget(parent)
    dispatcher = _FakeDispatcher()
    registry = LaunchIntentRegistry()
    wakes: list[None] = []
    d = HistoryDrawer(
        expanded=parent,
        dispatcher=dispatcher,
        launch_intent=registry,
        on_wake=lambda: wakes.append(None),
    )
    qtbot.addWidget(d)
    return d, dispatcher, registry, wakes


class TestDrawerRender:
    def test_empty_snap_shows_placeholder(self, drawer):
        d, *_ = drawer
        d.render(_empty_snap())
        # Just assert no exception + the count label is empty/zero
        assert "0" in d._count_label.text()

    def test_dormant_rows_appear(self, drawer):
        d, *_ = drawer
        snap = _empty_snap(dormant=[
            _dormant("u1", name="refactor"),
            _dormant("u2", name="bug-fix"),
        ])
        d.render(snap)
        # Two _DormantRow children inside the rows container
        rows = [w for w in d._rows_container.children()
                if isinstance(w, _DormantRow)]
        assert len(rows) == 2

    def test_launching_rows_appear(self, drawer):
        d, *_ = drawer
        intent = LaunchIntent(
            session_uuid="u1", cwd=Path("D:/x"), flags=(),
            terminal_name="windows-terminal", terminal_pid=1234,
            requested_at=datetime(2026, 5, 1, tzinfo=timezone.utc),
        )
        d.render(_empty_snap(launching=[intent]))
        # The launching row text should mention the uuid prefix.
        assert "1 launching" in d._count_label.text()


class TestResumeClick:
    def test_successful_resume_records_intent_and_wakes(self, drawer):
        d, dispatcher, registry, wakes = drawer
        dormant = _dormant("u1", permission_mode="bypassPermissions")
        d.render(_empty_snap(dormant=[dormant]))
        rows = [w for w in d._rows_container.children()
                if isinstance(w, _DormantRow)]
        assert len(rows) == 1
        rows[0]._on_resume()
        # Dispatcher was asked about LAUNCH first, then launch() called
        assert dispatcher.launch_calls
        call = dispatcher.launch_calls[-1]
        assert call["adapter_name"] == "windows-terminal"
        assert call["cwd"] == Path("D:/projects/foo")
        # bypassPermissions → flag carried into command
        assert call["command"] == (
            "claude", "--resume", "u1", "--dangerously-skip-permissions",
        )
        # Intent registered with terminal pid from spawn result
        snap = registry.snapshot()
        assert len(snap) == 1
        assert snap[0].session_uuid == "u1"
        assert snap[0].terminal_pid == 4242
        # snapshotter.wake was triggered for immediate ⏳ render
        assert wakes

    def test_no_launcher_available_yields_toast_no_intent(self, qtbot):
        from PySide6.QtWidgets import QWidget
        parent = QWidget()
        qtbot.addWidget(parent)
        dispatcher = _FakeDispatcher(adapters=())  # no LAUNCH-capable adapter
        registry = LaunchIntentRegistry()
        d = HistoryDrawer(
            expanded=parent, dispatcher=dispatcher,
            launch_intent=registry, on_wake=lambda: None,
        )
        qtbot.addWidget(d)
        d.render(_empty_snap(dormant=[_dormant("u1")]))
        rows = [w for w in d._rows_container.children()
                if isinstance(w, _DormantRow)]
        rows[0]._on_resume()
        assert dispatcher.launch_calls == []
        assert registry.snapshot() == ()
        # Toast text is set + the setVisible(True) was called.
        # isVisible() depends on parent being shown, which we don't do
        # in tests — isHidden() reflects the explicit hide() state alone.
        assert not d._toast.isHidden()
        assert "Windows Terminal" in d._toast.text()

    def test_spawn_error_yields_toast_no_intent(self, qtbot):
        from PySide6.QtWidgets import QWidget
        parent = QWidget()
        qtbot.addWidget(parent)
        dispatcher = _FakeDispatcher(
            spawn_error=LauncherSpawnError("wt.exe not found"),
        )
        registry = LaunchIntentRegistry()
        d = HistoryDrawer(
            expanded=parent, dispatcher=dispatcher,
            launch_intent=registry, on_wake=lambda: None,
        )
        qtbot.addWidget(d)
        d.render(_empty_snap(dormant=[_dormant("u1")]))
        rows = [w for w in d._rows_container.children()
                if isinstance(w, _DormantRow)]
        rows[0]._on_resume()
        # launch was attempted but raised — registry stays empty
        assert dispatcher.launch_calls
        assert registry.snapshot() == ()
        assert not d._toast.isHidden()
        assert "wt.exe not found" in d._toast.text()


class TestToggle:
    def test_toggle_flips_visibility(self, drawer):
        d, *_ = drawer
        # isHidden() reflects the explicit hide() state; the bare drawer
        # starts hidden because the constructor doesn't call show().
        assert d.isHidden()
        d.toggle()
        assert not d.isHidden()
        d.toggle()
        assert d.isHidden()
