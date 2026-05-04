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
    # F4 changed render's signature from render(snap) → render(data),
    # with `data = capsule.compute(snap)`. To keep these tests' old
    # ``capsule.render(snap)`` ergonomics, wrap render so a snap input
    # is auto-routed through compute first. Production wires
    # ``world.observable() | map(compute) | distinct | render`` —
    # this fixture mimics that single-shot end-to-end.
    _orig_render = cap.render
    def _render_through_compute(arg):
        if isinstance(arg, WorldSnapshot):
            return _orig_render(cap.compute(arg))
        return _orig_render(arg)
    cap.render = _render_through_compute  # type: ignore[method-assign]
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

    def test_render_cost_in_dedicated_slot_when_positive(self, capsule):
        # Three-region refactor moved cost into its own QLabel
        # (_cost_label) so a long session name can never push it
        # off the pill. Name and cost now live in separate widgets.
        v = _view(name="x", is_running=True)
        capsule.render(_snap(sessions=(v,), today_cost_usd=42.0))
        assert "$42" in capsule._cost_label.text()
        assert "x" in capsule._label.text()
        assert capsule._cost_label.isVisibleTo(capsule)

    def test_render_cost_slot_collapsed_when_zero(self, capsule):
        # When today's spend is $0 the cost slot is hidden entirely
        # so the name region gets the space back. Both the visible
        # text content AND the widget visibility flag should reflect
        # that — _apply_capsule branches on whichever is checked.
        capsule.render(_snap(sessions=(), today_cost_usd=0.0))
        assert capsule._cost_label.text() == ""
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
        # F4: cost is stored as the formatted string (display
        # precision) on _data.cost_str, not as a raw float — that's
        # what the dedup key sees and what _compose_label_text reads.
        assert capsule._data.cost_str == "$100"  # _fmt_money(99.5) rounds to "$100"

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
        # F4: quota stored as truncated int on _data.quota_pct,
        # not the raw float — sub-percent wobble doesn't change the
        # dedup key.
        assert capsule._data.quota_pct == 72  # int(72.5)

    def test_render_with_no_quota_clears_cache(self, capsule):
        capsule.render(_snap())
        assert capsule._data.quota_pct is None

    def test_render_in_dot_mode_is_no_op_for_label(self, capsule):
        capsule._is_dot = True
        v = _view(is_running=True, name="x")
        capsule.render(_snap(sessions=(v,), today_cost_usd=10.0))
        # cost still propagates into _data even in dot mode (label
        # update happens later when the user expands)
        assert capsule._data.cost_str == "$10"
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


class TestPerSurfaceDedup:
    """F4: per-surface ``compute(snap)`` is the dedup key. Verifies
    that microsecond ``last_activity`` ticks don't change a surface's
    compute output (so dedup correctly skips), while real visible
    changes do.

    Capsule and expanded use slightly different pipeline shapes:
      * capsule uses ``map(compute) → distinct → render(data)``.
      * expanded uses ``distinct(key_mapper=compute) → render(snap)``.
    Both end up calling render only when compute output changes."""

    def test_capsule_compute_skips_microsecond_jitter(self, capsule):
        """Two snaps differing only in last_activity microseconds
        produce equal CapsuleData → distinct dedupes."""
        v_a = _view(pid=1, name="alpha", is_running=True)
        # Same view but last_activity bumped by a few microseconds —
        # below the _fmt_started "now" 5s bucket boundary.
        from dataclasses import replace
        v_b = replace(
            v_a,
            last_activity=v_a.last_activity + timedelta(microseconds=123),
        )
        snap_a = _snap(sessions=(v_a,), today_cost_usd=5.0)
        snap_b = _snap(sessions=(v_b,), today_cost_usd=5.0)
        assert capsule.compute(snap_a) == capsule.compute(snap_b)

    def test_expanded_compute_skips_microsecond_jitter(self, qtbot):
        """Same as above but for ExpandedWindow.compute."""
        from dataclasses import replace

        controller = IslandController()
        capsule_w = QWidget(); capsule_w.show()
        panel = ExpandedWindow(
            capsule=capsule_w, controller=controller,
            get_usage_totals=lambda period: UsageTotals(period=period),
        )
        qtbot.addWidget(panel); qtbot.addWidget(capsule_w)

        v_a = _view(pid=1, name="alpha", is_running=True)
        v_b = replace(
            v_a,
            last_activity=v_a.last_activity + timedelta(microseconds=789),
        )
        snap_a = _snap(sessions=(v_a,), today_cost_usd=5.0)
        snap_b = _snap(sessions=(v_b,), today_cost_usd=5.0)
        assert panel.compute(snap_a) == panel.compute(snap_b)

    def test_expanded_compute_stable_across_quota_refetches(self, qtbot):
        """Regression: QuotaSnapshot's fetched_at field updates every
        ~5 min on each /api/oauth/usage poll, even when no displayed
        percentage actually changed. If compute() includes the raw
        QuotaSnapshot, every quota refresh causes a spurious panel
        re-render → user-visible 1-frame flash. This was the root
        cause of the "panel sometimes flashes" bug."""
        from claude_island.core.models import QuotaSnapshot

        controller = IslandController()
        capsule_w = QWidget(); capsule_w.show()
        panel = ExpandedWindow(
            capsule=capsule_w, controller=controller,
            get_usage_totals=lambda period: UsageTotals(period=period),
        )
        qtbot.addWidget(panel); qtbot.addWidget(capsule_w)

        base = datetime(2026, 5, 1, 12, tzinfo=timezone.utc)

        def _quota(fetched: datetime) -> QuotaSnapshot:
            return QuotaSnapshot(
                five_hour_pct=42.0,
                five_hour_resets_at=base + timedelta(hours=5),
                seven_day_pct=18.0,
                seven_day_resets_at=base + timedelta(days=7),
                fetched_at=fetched, is_stale=False, provider="anthropic",
            )

        v = _view(pid=1, name="alpha")
        snap_a = WorldSnapshot(
            session_groups=_sg(v), today_cost_usd=5.0,
            quota=_quota(base), available_providers=("anthropic",),
            selected_provider="anthropic", fetched_at=base,
            dormant_sessions=(), launching_sessions=(),
        )
        snap_b = WorldSnapshot(
            session_groups=_sg(v), today_cost_usd=5.0,
            quota=_quota(base + timedelta(minutes=5)),  # only fetched_at differs
            available_providers=("anthropic",),
            selected_provider="anthropic", fetched_at=base + timedelta(minutes=5),
            dormant_sessions=(), launching_sessions=(),
        )
        assert panel.compute(snap_a) == panel.compute(snap_b)

    def test_expanded_compute_catches_real_quota_change(self, qtbot):
        """Counterpart to the quota-refetch test: a real percentage
        change MUST flow through dedup so the bar updates."""
        from claude_island.core.models import QuotaSnapshot

        controller = IslandController()
        capsule_w = QWidget(); capsule_w.show()
        panel = ExpandedWindow(
            capsule=capsule_w, controller=controller,
            get_usage_totals=lambda period: UsageTotals(period=period),
        )
        qtbot.addWidget(panel); qtbot.addWidget(capsule_w)

        base = datetime(2026, 5, 1, 12, tzinfo=timezone.utc)
        snap_a = WorldSnapshot(
            session_groups=(), today_cost_usd=5.0,
            quota=QuotaSnapshot(
                five_hour_pct=42.0, five_hour_resets_at=base,
                seven_day_pct=18.0, seven_day_resets_at=base,
                fetched_at=base, is_stale=False, provider="anthropic",
            ),
            available_providers=("anthropic",), selected_provider="anthropic",
            fetched_at=base, dormant_sessions=(), launching_sessions=(),
        )
        snap_b = WorldSnapshot(
            session_groups=(), today_cost_usd=5.0,
            quota=QuotaSnapshot(
                five_hour_pct=43.0,  # 42 → 43, real change
                five_hour_resets_at=base,
                seven_day_pct=18.0, seven_day_resets_at=base,
                fetched_at=base, is_stale=False, provider="anthropic",
            ),
            available_providers=("anthropic",), selected_provider="anthropic",
            fetched_at=base, dormant_sessions=(), launching_sessions=(),
        )
        assert panel.compute(snap_a) != panel.compute(snap_b)

    def test_expanded_compute_catches_dormant_count_change(self, qtbot):
        """The history chip ("🗂 N") would silently go stale if dormant
        count changes weren't part of the dedup key."""
        controller = IslandController()
        capsule_w = QWidget(); capsule_w.show()
        panel = ExpandedWindow(
            capsule=capsule_w, controller=controller,
            get_usage_totals=lambda period: UsageTotals(period=period),
        )
        qtbot.addWidget(panel); qtbot.addWidget(capsule_w)

        base = datetime(2026, 5, 1, 12, tzinfo=timezone.utc)

        def make(dormant_count):
            return WorldSnapshot(
                session_groups=(), today_cost_usd=0.0, quota=None,
                available_providers=(), selected_provider=None,
                fetched_at=base,
                dormant_sessions=("u",) * dormant_count,
                launching_sessions=(),
            )
        assert panel.compute(make(3)) != panel.compute(make(4))

    def test_expanded_render_skips_layout_rebuild_on_identical_structure(self, qtbot):
        """When two consecutive renders have identical group structure
        (same group_ids in order, same view pids in each group), the
        in-place fast-path should be taken — no _clear_session_layout
        call, no widget detach/re-attach cycle. This eliminates the
        1-frame flash even when render IS legitimately called (e.g. a
        cost-band crossing forces a render)."""
        from dataclasses import replace
        from unittest.mock import patch

        controller = IslandController()
        capsule_w = QWidget(); capsule_w.show()
        panel = ExpandedWindow(
            capsule=capsule_w, controller=controller,
            get_usage_totals=lambda period: UsageTotals(period=period),
        )
        qtbot.addWidget(panel); qtbot.addWidget(capsule_w)

        v = _view(pid=1, name="alpha", cost_usd=5.0)
        snap_a = _snap(sessions=(v,), today_cost_usd=5.0)
        v2 = replace(v, cost_usd=12.0)  # cost change but same structure
        snap_b = _snap(sessions=(v2,), today_cost_usd=12.0)

        # Prime: first render builds the layout
        panel.render(snap_a)

        # Second render should NOT call _clear_session_layout (the
        # source of the visual flash).
        with patch.object(panel, "_clear_session_layout") as mock_clear:
            panel.render(snap_b)
            assert mock_clear.call_count == 0, (
                "expected fast path: identical group structure should "
                "skip _clear_session_layout (the flash culprit)"
            )

    def test_capsule_compute_changes_when_running_set_changes(self, capsule):
        """Compute output MUST change when the user-visible state
        actually changed (running flip), so dedup re-renders."""
        v_idle = _view(pid=1, name="alpha", is_running=False)
        v_busy = _view(pid=1, name="alpha", is_running=True)
        snap_a = _snap(sessions=(v_idle,))
        snap_b = _snap(sessions=(v_busy,))
        assert capsule.compute(snap_a) != capsule.compute(snap_b)

    def test_capsule_compute_changes_when_cost_crosses_band(self, capsule):
        """_fmt_money quantises into bands; compute output changes
        only when the formatted string changes."""
        snap_lo = _snap(today_cost_usd=9.99)
        snap_hi = _snap(today_cost_usd=10.0)
        # 9.99 → "$9.99", 10.0 → "$10" (different formatting bands)
        assert capsule.compute(snap_lo) != capsule.compute(snap_hi)

    def test_capsule_compute_unchanged_within_cost_band(self, capsule):
        """Two cost values inside the same _fmt_money band produce
        the same compute output → dedup skips."""
        snap_a = _snap(today_cost_usd=12.0)
        snap_b = _snap(today_cost_usd=12.4)
        # Both in the < $1000 band, both round to "$12"
        assert capsule.compute(snap_a) == capsule.compute(snap_b)

    def test_pipeline_dedupes_capsule_when_compute_unchanged(self, qtbot, capsule):
        """End-to-end: push two snaps that differ only in last_activity
        microseconds through the wired pipeline; render must run once."""
        import reactivex.operators as ops
        from claude_island.core.snapshot import world
        from dataclasses import replace as _replace

        renders: list = []
        sub = (
            world.observable()
            .pipe(
                ops.map(capsule.compute),
                ops.distinct_until_changed(),
            )
            .subscribe(on_next=renders.append)
        )
        try:
            v_a = _view(pid=1, name="alpha", is_running=True)
            v_b = _replace(
                v_a,
                last_activity=v_a.last_activity + timedelta(microseconds=42),
            )
            baseline = len(renders)
            world.push(_snap(sessions=(v_a,), today_cost_usd=5.0))
            world.push(_snap(sessions=(v_b,), today_cost_usd=5.0))
            # Same compute output → exactly ONE render past baseline.
            assert len(renders) == baseline + 1, (
                f"per-surface dedup failed: got {len(renders) - baseline} "
                f"renders for two snaps with equal compute output"
            )
        finally:
            sub.dispose()
