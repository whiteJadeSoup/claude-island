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
    SessionGroup,
    SessionView,
    WorldSnapshot,
)
from claude_island.ui.capsule_window import CapsuleWindow
from claude_island.ui.controller import IslandController
from claude_island.ui.expanded_window import ExpandedWindow


# ---------------------------------------------------------------------------
# Snapshot builders
# ---------------------------------------------------------------------------

def _sg(*views: SessionView) -> tuple[SessionGroup, ...]:
    """Wrap each view in a singleton SessionGroup so test snapshots can be
    constructed without spinning up the real adapter chain."""
    return tuple(
        SessionGroup(
            group_id=f"test:{v.pid}",
            title_hint=None,
            adapter_id="test",
            views=(v,),
        )
        for v in views
    )


def _session(pid: int = 1234, cwd: str = "/tmp/proj") -> Session:
    return Session(
        pid=pid, project_path=Path(cwd), session_uuid="",
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
        session=sess,
    )


def _snap(
    *,
    sessions: tuple[SessionView, ...] = (),
    today_cost_usd: float = 0.0,
) -> WorldSnapshot:
    """Build a snapshot from a flat list of views — convenience wrapper
    that wraps each view in its own singleton SessionGroup so tests
    don't have to think about grouping unless they care."""
    return WorldSnapshot(
        session_groups=_sg(*sessions),
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
        assert "0 sessions" in capsule._label.text()

    def test_render_one_idle_session_shows_count_not_name(self, capsule):
        v = _view(name="my-project", is_running=False)
        capsule.render(_snap(sessions=(v,)))
        assert "1 session" in capsule._label.text()
        assert "my-project" not in capsule._label.text()

    def test_render_one_running_session_shows_its_name(self, capsule):
        v = _view(name="my-feature-branch", is_running=True)
        capsule.render(_snap(sessions=(v,)))
        assert "my-feature-branch" in capsule._label.text()

    def test_render_two_running_starts_carousel(self, capsule):
        v1 = _view(pid=1, name="alpha", cwd="/a", is_running=True)
        v2 = _view(pid=2, name="beta", cwd="/b", is_running=True)
        capsule.render(_snap(sessions=(v1, v2)))

        text = capsule._label.text()
        assert "alpha" in text
        assert capsule._rotation_timer.isActive() is True

    def test_carousel_advances_on_timer_tick(self, capsule, qtbot):
        v1 = _view(pid=1, name="alpha", cwd="/a", is_running=True)
        v2 = _view(pid=2, name="beta", cwd="/b", is_running=True)
        capsule.render(_snap(sessions=(v1, v2)))
        first = capsule._label.text()
        capsule._on_rotate_tick()
        second = capsule._label.text()
        assert first != second
        capsule._on_rotate_tick()
        assert capsule._label.text() == first

    def test_carousel_index_resets_when_running_set_changes(self, capsule):
        v1 = _view(pid=1, name="alpha", cwd="/a", is_running=True)
        v2 = _view(pid=2, name="beta", cwd="/b", is_running=True)
        capsule.render(_snap(sessions=(v1, v2)))
        capsule._on_rotate_tick()  # advance to "beta"
        assert capsule._rotation_index == 1

        v3 = _view(pid=3, name="gamma", cwd="/c", is_running=True)
        capsule.render(_snap(sessions=(v1, v3)))
        assert capsule._rotation_index == 0
        assert "alpha" in capsule._label.text()

    def test_carousel_index_does_not_reset_on_unrelated_snap_change(self, capsule):
        v1 = _view(pid=1, name="alpha", cwd="/a", is_running=True)
        v2 = _view(pid=2, name="beta", cwd="/b", is_running=True)
        capsule.render(_snap(sessions=(v1, v2), today_cost_usd=10.0))
        capsule._on_rotate_tick()
        assert capsule._rotation_index == 1

        capsule.render(_snap(sessions=(v1, v2), today_cost_usd=11.0))
        assert capsule._rotation_index == 1
        assert "beta" in capsule._label.text()

    def test_carousel_stops_when_running_drops_to_one(self, capsule):
        v1 = _view(pid=1, name="alpha", cwd="/a", is_running=True)
        v2 = _view(pid=2, name="beta", cwd="/b", is_running=True)
        capsule.render(_snap(sessions=(v1, v2)))
        assert capsule._rotation_timer.isActive() is True

        v2_idle = _view(pid=2, name="beta", cwd="/b", is_running=False)
        capsule.render(_snap(sessions=(v1, v2_idle)))
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
        assert "$42" in text
        assert "x" in text

    def test_render_cost_suffix_omitted_when_zero(self, capsule):
        capsule.render(_snap(sessions=(), today_cost_usd=0.0))
        assert "$" not in capsule._label.text()

    def test_render_running_state_set_when_any_running(self, capsule):
        from claude_island.ui.expanded_window import _RowStatusGlyph
        v = _view(is_running=True)
        capsule.render(_snap(sessions=(v,)))
        assert capsule._dot_label.state() == _RowStatusGlyph.STATE_RUNNING
        assert capsule._is_breathing is True

    def test_render_idle_state_when_none_running(self, capsule):
        from claude_island.ui.expanded_window import _RowStatusGlyph
        v = _view(is_running=False)
        capsule.render(_snap(sessions=(v,)))
        assert capsule._dot_label.state() == _RowStatusGlyph.STATE_IDLE
        assert capsule._is_breathing is False

    def test_render_caches_today_cost_for_paint(self, capsule):
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
            session_groups=(), today_cost_usd=0.0, quota=q,
            available_providers=("anthropic",), selected_provider="anthropic",
            fetched_at=datetime.now(timezone.utc),
        )
        capsule.render(snap)
        assert capsule._quota_pct_cache == 72.5

    def test_render_with_no_quota_clears_cache(self, capsule):
        capsule.render(_snap())
        assert capsule._quota_pct_cache is None

    def test_render_in_dot_mode_is_no_op_for_label(self, capsule):
        capsule._is_dot = True
        v = _view(is_running=True, name="x")
        capsule.render(_snap(sessions=(v,), today_cost_usd=10.0))
        assert capsule._cost_cache == 10.0
        assert capsule._is_dot is True


def _quota_snap_with_pct(pct: float) -> WorldSnapshot:
    from claude_island.core.models import QuotaSnapshot
    q = QuotaSnapshot(
        five_hour_pct=pct,
        five_hour_resets_at=datetime.now(timezone.utc) + timedelta(hours=2),
        seven_day_pct=10.0,
        seven_day_resets_at=datetime.now(timezone.utc) + timedelta(days=5),
        fetched_at=datetime.now(timezone.utc),
        is_stale=False,
    )
    return WorldSnapshot(
        session_groups=(), today_cost_usd=0.0, quota=q,
        available_providers=("anthropic",), selected_provider="anthropic",
        fetched_at=datetime.now(timezone.utc),
    )


class TestCapsuleQuotaBar:
    """Visibility + colour-threshold contract for the quota mini-bar."""

    def test_bar_hidden_below_warn_threshold(self, capsule):
        from claude_island.ui.capsule_window import _QUOTA_WARN_THRESHOLD
        capsule.render(_quota_snap_with_pct(_QUOTA_WARN_THRESHOLD - 1))
        assert capsule._should_show_quota_bar() is False

    def test_bar_appears_at_warn_threshold(self, capsule):
        from claude_island.ui.capsule_window import _QUOTA_WARN_THRESHOLD
        capsule.render(_quota_snap_with_pct(_QUOTA_WARN_THRESHOLD))
        assert capsule._should_show_quota_bar() is True

    def test_critical_threshold_widens_pill(self, capsule):
        from claude_island.ui.capsule_window import (
            _CAPSULE_W,
            _CAPSULE_W_WITH_QUOTA,
            _QUOTA_CRITICAL_THRESHOLD,
        )
        capsule.render(_quota_snap_with_pct(_QUOTA_CRITICAL_THRESHOLD))
        assert capsule.width() == _CAPSULE_W_WITH_QUOTA
        assert _CAPSULE_W_WITH_QUOTA > _CAPSULE_W

    def test_dropping_below_threshold_collapses_pill(self, capsule):
        from claude_island.ui.capsule_window import (
            _CAPSULE_W,
            _CAPSULE_W_WITH_QUOTA,
            _QUOTA_WARN_THRESHOLD,
        )
        capsule.render(_quota_snap_with_pct(_QUOTA_WARN_THRESHOLD + 5))
        assert capsule.width() == _CAPSULE_W_WITH_QUOTA
        capsule.render(_quota_snap_with_pct(_QUOTA_WARN_THRESHOLD - 5))
        assert capsule.width() == _CAPSULE_W


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
        snap = _snap(sessions=(_view(pid=1),))
        panel.render(snap)
        assert panel._latest_snap is snap

    def test_render_replaces_session_list_on_subsequent_call(self, panel):
        v1 = _view(pid=1)
        v2 = _view(pid=2)
        panel.render(_snap(sessions=(v1,)))
        assert set(panel._rows.keys()) == {1}
        panel.render(_snap(sessions=(v2,)))
        assert set(panel._rows.keys()) == {2}

    def test_render_preserves_row_widget_when_pid_persists(self, panel):
        v = _view(pid=99)
        panel.render(_snap(sessions=(v,)))
        before = panel._rows[99]
        panel.render(_snap(sessions=(v,)))
        assert panel._rows[99] is before


class TestDistinctUntilChangedDedup:
    """Verifies the wiring-layer ``distinct_until_changed(key_mapper=
    render_key)`` actually skips no-op snapshots."""

    def test_two_renders_with_same_data_share_render_key(self):
        v = _view(pid=1, name="alpha")
        snap_a = WorldSnapshot(
            session_groups=_sg(v), today_cost_usd=5.0, quota=None,
            available_providers=("anthropic",), selected_provider="anthropic",
            fetched_at=datetime.now(timezone.utc),
        )
        snap_b = WorldSnapshot(
            session_groups=_sg(v), today_cost_usd=5.0, quota=None,
            available_providers=("anthropic",), selected_provider="anthropic",
            fetched_at=datetime.now(timezone.utc) + timedelta(seconds=10),
        )
        assert snap_a.render_key() == snap_b.render_key()
        assert snap_a != snap_b

    def test_distinct_until_changed_skips_renders_for_equal_render_key(self, qtbot):
        import reactivex.operators as ops
        from claude_island.core.snapshot import world

        renders: list[WorldSnapshot] = []
        sub = (
            world.observable()
            .pipe(ops.distinct_until_changed(key_mapper=lambda s: s.render_key()))
            .subscribe(on_next=renders.append)
        )
        try:
            v = _view(pid=1)
            snap_a = WorldSnapshot(
                session_groups=_sg(v), today_cost_usd=5.0, quota=None,
                available_providers=(), selected_provider=None,
                fetched_at=datetime.now(timezone.utc),
            )
            snap_b = WorldSnapshot(
                session_groups=_sg(v), today_cost_usd=5.0, quota=None,
                available_providers=(), selected_provider=None,
                fetched_at=datetime.now(timezone.utc) + timedelta(seconds=10),
            )
            baseline = len(renders)
            world.push(snap_a)
            world.push(snap_b)  # same render_key → must dedupe
            assert len(renders) == baseline + 1, (
                f"distinct_until_changed failed to dedupe: "
                f"got {len(renders) - baseline} renders for two snaps "
                f"with identical render_key"
            )
        finally:
            sub.dispose()
