"""Tests for R1 Deliverable 1: rolling token-rate history in WorldViewModel."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from PySide6.QtCore import QCoreApplication

from claude_island.core.models import Session
from claude_island.core.session_phase import SessionPhase
from claude_island.core.snapshot import SessionGroup, SessionView, WorldSnapshot
from claude_island.ui.world_view_model import WorldViewModel, _RATE_HISTORY_MAX

_app = QCoreApplication.instance() or QCoreApplication([])
_NOW = datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_view(uuid: str, tokens_per_min: int | None = None) -> SessionView:
    sess = Session(pid=1, project_path=Path("D:/proj"), last_activity=_NOW, session_uuid=uuid)
    return SessionView(
        pid=1,
        name="test",
        project_path=Path("D:/proj"),
        project_basename="proj",
        last_activity=_NOW,
        cost_usd=0.0,
        is_high_cost=False,
        latest_model=None,
        status_word=None,
        session=sess,
        session_uuid=uuid,
        phase=SessionPhase.IDLE,
        tokens_per_min=tokens_per_min,
    )


def _snap_with_view(view: SessionView) -> WorldSnapshot:
    return WorldSnapshot(
        today_cost_usd=0.0,
        quota=None,
        available_providers=(),
        selected_provider=None,
        fetched_at=_NOW,
        session_groups=(SessionGroup("g", None, "", (view,)),),
    )


def _empty_snap() -> WorldSnapshot:
    return WorldSnapshot(
        today_cost_usd=0.0,
        quota=None,
        available_providers=(),
        selected_provider=None,
        fetched_at=_NOW,
        session_groups=(),
    )


# ---------------------------------------------------------------------------
# Test: rate_series accumulates across updates
# ---------------------------------------------------------------------------

def test_rate_series_accumulates_three_updates():
    """Three updates with rates 100, 200, 300 → rate_series == [100, 200, 300]."""
    vm = WorldViewModel()
    uuid = "sess-abc"
    for rate in (100, 200, 300):
        view = _make_view(uuid, tokens_per_min=rate)
        vm.update(_snap_with_view(view))

    series = vm.sessions[0]["rate_series"]
    assert series == [100, 200, 300]


def test_rate_series_uses_zero_when_tokens_per_min_is_none():
    """tokens_per_min=None → 0 appended to rate_series (keeps waveform continuous)."""
    vm = WorldViewModel()
    uuid = "sess-none"
    vm.update(_snap_with_view(_make_view(uuid, tokens_per_min=None)))
    vm.update(_snap_with_view(_make_view(uuid, tokens_per_min=50)))
    vm.update(_snap_with_view(_make_view(uuid, tokens_per_min=None)))

    assert vm.sessions[0]["rate_series"] == [0, 50, 0]


# ---------------------------------------------------------------------------
# Test: cap at _RATE_HISTORY_MAX (60 samples)
# ---------------------------------------------------------------------------

def test_rate_series_capped_at_max():
    """After _RATE_HISTORY_MAX + 10 updates the history never exceeds the cap."""
    vm = WorldViewModel()
    uuid = "sess-cap"
    n = _RATE_HISTORY_MAX + 10
    for i in range(n):
        vm.update(_snap_with_view(_make_view(uuid, tokens_per_min=i)))

    series = vm.sessions[0]["rate_series"]
    assert len(series) == _RATE_HISTORY_MAX
    # Oldest samples were dropped — the last _RATE_HISTORY_MAX values are kept.
    expected = list(range(n - _RATE_HISTORY_MAX, n))
    assert series == expected


# ---------------------------------------------------------------------------
# Test: pruning on session disappearance
# ---------------------------------------------------------------------------

def test_stale_session_pruned_from_history():
    """A session that leaves the snapshot has its history removed from _rate_history."""
    vm = WorldViewModel()
    uuid = "sess-leaving"

    # Build up some history.
    for rate in (10, 20, 30):
        vm.update(_snap_with_view(_make_view(uuid, tokens_per_min=rate)))

    assert uuid in vm._rate_history

    # Now push an empty snapshot — session is gone.
    vm.update(_empty_snap())

    assert uuid not in vm._rate_history


# ---------------------------------------------------------------------------
# Test: multiple sessions maintain independent histories
# ---------------------------------------------------------------------------

def test_independent_histories_per_session():
    """Two sessions accumulate separate rate_series without cross-contamination."""
    vm = WorldViewModel()
    uuid_a, uuid_b = "sess-a", "sess-b"

    view_a = _make_view(uuid_a, tokens_per_min=100)
    view_b = _make_view(uuid_b, tokens_per_min=200)
    snap_both = WorldSnapshot(
        today_cost_usd=0.0,
        quota=None,
        available_providers=(),
        selected_provider=None,
        fetched_at=_NOW,
        session_groups=(
            SessionGroup("g1", None, "", (view_a,)),
            SessionGroup("g2", None, "", (view_b,)),
        ),
    )
    vm.update(snap_both)

    sessions_by_id = {s["id"]: s for s in vm.sessions}
    assert sessions_by_id[uuid_a]["rate_series"] == [100]
    assert sessions_by_id[uuid_b]["rate_series"] == [200]
