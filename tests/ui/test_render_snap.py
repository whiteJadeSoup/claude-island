"""Tests for the new ``render(snap)`` entry points on CapsuleWindow
and ExpandedWindow (Phase D).

These tests ONLY cover the snap-driven path. The legacy refresh_xxx
methods continue to have their own test files (test_capsule_window.py,
test_expanded_window.py); those will lose tests when Phase G removes
the legacy methods.

Strategy: build a WorldSnapshot fixture, call render(snap), assert
the widget state matches what the snap describes — no comparison
against the legacy path.
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

# Force offscreen Qt — same convention as the rest of tests/ui/.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QLabel, QWidget

from claude_island.core.models import Session, UsageTotals
from claude_island.core.snapshot import (
    HIGH_COST_USD_THRESHOLD,
    SessionView,
    WorldSnapshot,
)
from claude_island.ui.capsule_window import CapsuleWindow
from claude_island.ui.controller import IslandController
from claude_island.ui.expanded_window import ExpandedWindow


# ---------------------------------------------------------------------------
# Snapshot builders
# ---------------------------------------------------------------------------

def _session(pid: int = 1234, cwd: str = "/tmp/proj") -> Session:
    return Session(
        pid=pid, project_path=Path(cwd), session_uuid="",
        window_handle=None,
        last_activity=datetime.now(timezone.utc),
    )


def _view(
    *,
    pid: int = 1234,
    name: str = "test-project",
    cwd: str = "/tmp/test",
    is_running: bool = False,
    cost_usd: float = 0.0,
) -> SessionView:
    sess = Session(
        pid=pid, project_path=Path(cwd), session_uuid="",
        window_handle=None,
        last_activity=datetime.now(timezone.utc),
    )
    return SessionView(
        pid=pid, name=name, project_path=Path(cwd),
        project_basename=Path(cwd).name,
        last_activity=sess.last_activity,
        is_running=is_running,
        cost_usd=cost_usd,
        is_high_cost=cost_usd >= HIGH_COST_USD_THRESHOLD,
        latest_model="claude-opus-4-7",
        status_word="busy" if is_running else "idle",
        window_handle=None,
        session=sess,
    )


def _snap(
    *,
    sessions: tuple[SessionView, ...] = (),
    today_cost_usd: float = 0.0,
) -> WorldSnapshot:
    return WorldSnapshot(
        sessions=sessions,
        today_cost_usd=today_cost_usd,
        quota=None,
        available_providers=(),
        selected_provider=None,
        fetched_at=datetime.now(timezone.utc),
    )


# ---------------------------------------------------------------------------
# CapsuleWindow.render(snap)
# ---------------------------------------------------------------------------

@pytest.fixture
def capsule(qtbot):
    controller = IslandController()
    cap = CapsuleWindow(controller)
    qtbot.addWidget(cap)
    # Force into capsule mode (default is dot mode after construction).
    cap._is_dot = False
    return cap


class TestCapsuleRender:
    def test_render_empty_snap_shows_zero_session_count(self, capsule):
        capsule.render(_snap())
        # Label shows "0 sessions" (no cost suffix because cost == 0)
        assert "0 sessions" in capsule._label.text()

    def test_render_one_idle_session_shows_count_not_name(self, capsule):
        # One session but it's idle → not the "single running" path,
        # so the label uses count form.
        v = _view(name="my-project", is_running=False)
        capsule.render(_snap(sessions=(v,)))
        assert "1 session" in capsule._label.text()
        # The session NAME does NOT appear — only the count form
        # surfaces a name when the session is the unique active one.
        assert "my-project" not in capsule._label.text()

    def test_render_one_running_session_shows_its_name(self, capsule):
        v = _view(name="my-feature-branch", is_running=True)
        capsule.render(_snap(sessions=(v,)))
        # Single running session → name in pill text.
        assert "my-feature-branch" in capsule._label.text()

    def test_render_two_running_starts_carousel(self, capsule):
        """≥2 running sessions: pill rotates through their names every
        ``_ROTATE_INTERVAL_MS`` instead of degrading to the count form
        (which loses information about WHICH sessions are live)."""
        v1 = _view(pid=1, name="alpha", cwd="/a", is_running=True)
        v2 = _view(pid=2, name="beta", cwd="/b", is_running=True)
        capsule.render(_snap(sessions=(v1, v2)))

        # Initial render shows the first rotation candidate.
        text = capsule._label.text()
        assert "alpha" in text
        assert capsule._rotation_timer.isActive() is True

    def test_carousel_advances_on_timer_tick(self, capsule, qtbot):
        v1 = _view(pid=1, name="alpha", cwd="/a", is_running=True)
        v2 = _view(pid=2, name="beta", cwd="/b", is_running=True)
        capsule.render(_snap(sessions=(v1, v2)))
        first = capsule._label.text()
        # Manually tick the rotation handler — bypasses waiting for
        # the 4 s timer to elapse (test would be slow + flaky).
        capsule._on_rotate_tick()
        second = capsule._label.text()
        assert first != second
        # Tick again — should wrap back to the first name.
        capsule._on_rotate_tick()
        assert capsule._label.text() == first

    def test_carousel_index_resets_when_running_set_changes(self, capsule):
        v1 = _view(pid=1, name="alpha", cwd="/a", is_running=True)
        v2 = _view(pid=2, name="beta", cwd="/b", is_running=True)
        capsule.render(_snap(sessions=(v1, v2)))
        capsule._on_rotate_tick()  # advance to "beta"
        assert capsule._rotation_index == 1

        # New running set — index resets to 0.
        v3 = _view(pid=3, name="gamma", cwd="/c", is_running=True)
        capsule.render(_snap(sessions=(v1, v3)))
        assert capsule._rotation_index == 0
        assert "alpha" in capsule._label.text()

    def test_carousel_index_does_not_reset_on_unrelated_snap_change(self, capsule):
        """Cost ticking up shouldn't jerk the carousel back to position
        0 — the carousel state is per running-name-set, not per snap."""
        v1 = _view(pid=1, name="alpha", cwd="/a", is_running=True)
        v2 = _view(pid=2, name="beta", cwd="/b", is_running=True)
        capsule.render(_snap(sessions=(v1, v2), today_cost_usd=10.0))
        capsule._on_rotate_tick()  # advance to index 1 ("beta")
        assert capsule._rotation_index == 1

        # Same running set, only cost changed → index preserved.
        capsule.render(_snap(sessions=(v1, v2), today_cost_usd=11.0))
        assert capsule._rotation_index == 1
        assert "beta" in capsule._label.text()

    def test_carousel_stops_when_running_drops_to_one(self, capsule):
        v1 = _view(pid=1, name="alpha", cwd="/a", is_running=True)
        v2 = _view(pid=2, name="beta", cwd="/b", is_running=True)
        capsule.render(_snap(sessions=(v1, v2)))
        assert capsule._rotation_timer.isActive() is True

        # Drop to one running.
        v2_idle = _view(pid=2, name="beta", cwd="/b", is_running=False)
        capsule.render(_snap(sessions=(v1, v2_idle)))
        # No rotation needed when only one is running — timer off, but
        # the single-running branch still surfaces alpha as the name.
        assert capsule._rotation_timer.isActive() is False
        assert "alpha" in capsule._label.text()

    def test_carousel_stops_when_no_running(self, capsule):
        v1 = _view(pid=1, name="alpha", cwd="/a", is_running=True)
        v2 = _view(pid=2, name="beta", cwd="/b", is_running=True)
        capsule.render(_snap(sessions=(v1, v2)))
        capsule.render(_snap(sessions=()))
        assert capsule._rotation_timer.isActive() is False
        assert "0 sessions" in capsule._label.text()

    def test_render_cost_suffix_appended_when_positive(self, capsule):
        v = _view(name="x", is_running=True)
        capsule.render(_snap(sessions=(v,), today_cost_usd=42.0))
        text = capsule._label.text()
        # Cost suffix follows the name with two-space separator.
        assert "$42" in text
        assert "x" in text

    def test_render_cost_suffix_omitted_when_zero(self, capsule):
        capsule.render(_snap(sessions=(), today_cost_usd=0.0))
        # No "$0" trailing — the pill should read cleanly.
        assert "$" not in capsule._label.text()

    def test_render_running_state_set_when_any_running(self, capsule):
        from claude_island.ui.expanded_window import _RowStatusGlyph
        v = _view(is_running=True)
        capsule.render(_snap(sessions=(v,)))
        # Internal state flag flips → equalizer-glyph state goes RUNNING.
        assert capsule._dot_label.state() == _RowStatusGlyph.STATE_RUNNING
        assert capsule._is_breathing is True

    def test_render_idle_state_when_none_running(self, capsule):
        from claude_island.ui.expanded_window import _RowStatusGlyph
        v = _view(is_running=False)
        capsule.render(_snap(sessions=(v,)))
        assert capsule._dot_label.state() == _RowStatusGlyph.STATE_IDLE
        assert capsule._is_breathing is False

    def test_render_caches_today_cost_for_paint(self, capsule):
        # _cost_cache is read by _paint_quota_bar / _compose_label_text;
        # render should keep it in sync with snap.today_cost_usd.
        capsule.render(_snap(today_cost_usd=99.5))
        assert capsule._cost_cache == 99.5

    def test_render_caches_quota_pct_for_paint(self, capsule):
        from claude_island.core.models import QuotaSnapshot
        q = QuotaSnapshot(
            five_hour_pct=72.5,
            five_hour_resets_at=datetime.now(timezone.utc) + timedelta(hours=2),
            seven_day_pct=10.0,
            seven_day_resets_at=datetime.now(timezone.utc) + timedelta(days=5),
            fetched_at=datetime.now(timezone.utc),
            is_stale=False,
        )
        snap = WorldSnapshot(
            sessions=(), today_cost_usd=0.0, quota=q,
            available_providers=("anthropic",), selected_provider="anthropic",
            fetched_at=datetime.now(timezone.utc),
        )
        capsule.render(snap)
        assert capsule._quota_pct_cache == 72.5

    def test_render_with_no_quota_clears_cache(self, capsule):
        capsule.render(_snap())  # snap.quota = None
        assert capsule._quota_pct_cache is None

    def test_render_in_dot_mode_is_no_op_for_label(self, capsule):
        # Force dot mode — render should still cache values but not
        # touch the (hidden) label widget.
        capsule._is_dot = True
        v = _view(is_running=True, name="x")
        capsule.render(_snap(sessions=(v,), today_cost_usd=10.0))
        # Caches updated…
        assert capsule._cost_cache == 10.0
        # …but capsule didn't switch out of dot mode.
        assert capsule._is_dot is True


# ---------------------------------------------------------------------------
# ExpandedWindow.render(snap)
# ---------------------------------------------------------------------------

@pytest.fixture
def panel(qtbot):
    capsule = QWidget(); capsule.show()
    controller = IslandController()
    p = ExpandedWindow(
        capsule=capsule, controller=controller,
        get_usage_totals=lambda period: UsageTotals(period=period),
    )
    qtbot.addWidget(p); qtbot.addWidget(capsule)
    return p


class TestExpandedRender:
    def test_render_empty_snap_renders_no_rows(self, panel):
        panel.render(_snap())
        assert panel._rows == {}

    def test_render_one_session_creates_one_row(self, panel):
        v = _view(pid=42, name="test")
        panel.render(_snap(sessions=(v,)))
        assert 42 in panel._rows

    def test_render_caches_snapshot_for_phase_g_consumption(self, panel):
        # Phase G will have _update_row read fields off self._latest_snap
        # directly. Phase D just stores the cache.
        snap = _snap(sessions=(_view(pid=1),))
        panel.render(snap)
        assert panel._latest_snap is snap

    def test_render_replaces_session_list_on_subsequent_call(self, panel):
        v1 = _view(pid=1)
        v2 = _view(pid=2)
        panel.render(_snap(sessions=(v1,)))
        assert set(panel._rows.keys()) == {1}
        panel.render(_snap(sessions=(v2,)))
        # pid=1 dropped, pid=2 added.
        assert set(panel._rows.keys()) == {2}

    def test_render_preserves_row_widget_when_pid_persists(self, panel):
        v = _view(pid=99)
        panel.render(_snap(sessions=(v,)))
        before = panel._rows[99]
        panel.render(_snap(sessions=(v,)))
        # Cached row reused — same widget instance survives across renders.
        assert panel._rows[99] is before
