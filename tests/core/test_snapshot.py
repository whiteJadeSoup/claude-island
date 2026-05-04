"""Tests for the core snapshot primitives.

This file covers the data classes (SessionView, WorldSnapshot) and the
_WorldStore singleton. The Snapshotter (build pipeline) is exercised in
test_snapshotter.py once that lands in Phase C.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from claude_island.core.snapshot import (
    HIGH_COST_USD_THRESHOLD,
    SessionGroup,
    SessionView,
    WorldSnapshot,
    _WorldStore,
    world,
)


def _sg(*views: SessionView) -> tuple[SessionGroup, ...]:
    """Wrap each view in a singleton SessionGroup so test snapshots can be
    constructed without spinning up the real adapter chain. Order is
    preserved so order-sensitivity tests still work."""
    return tuple(
        SessionGroup(
            group_id=f"test:{v.pid}",
            title_hint=None,
            adapter_id="test",
            views=(v,),
        )
        for v in views
    )


# ---------------------------------------------------------------------------
# SessionView
# ---------------------------------------------------------------------------

# Fixed timestamp shared by all _view() instances — using datetime.now()
# inside the fixture leaks timing variability into "structural equality"
# assertions (two _view() calls would compare ≠ if the clock advanced
# between them, even by a microsecond). Tests that need a varying
# timestamp construct it explicitly.
_FIXED_TS = datetime(2026, 5, 1, 12, 0, 0, tzinfo=timezone.utc)


def _view(
    *,
    cost_usd: float = 1.0,
    is_high_cost: bool | None = None,
    is_running: bool = False,
) -> SessionView:
    """Test fixture producing a valid SessionView with sensible defaults
    so each test only specifies the fields it cares about."""
    if is_high_cost is None:
        is_high_cost = cost_usd >= HIGH_COST_USD_THRESHOLD
    from claude_island.core.models import Session
    sess = Session(
        pid=1234, project_path=Path("/tmp/test"),
        session_uuid="",
        last_activity=_FIXED_TS,
    )
    return SessionView(
        pid=1234,
        name="test",
        project_path=Path("/tmp/test"),
        project_basename="test",
        last_activity=_FIXED_TS,
        is_running=is_running,
        cost_usd=cost_usd,
        is_high_cost=is_high_cost,
        latest_model="claude-opus-4-7",
        status_word="idle",
        session=sess,
    )


class TestSessionView:
    def test_construction_with_valid_fields(self):
        v = _view(cost_usd=10.0, is_high_cost=False)
        assert v.cost_usd == 10.0
        assert v.is_high_cost is False

    def test_frozen_assignment_raises(self):
        v = _view()
        with pytest.raises(Exception):  # FrozenInstanceError or AttributeError under slots
            v.cost_usd = 999.0  # type: ignore[misc]

    def test_high_cost_invariant_true_above_threshold(self):
        v = _view(cost_usd=HIGH_COST_USD_THRESHOLD + 1, is_high_cost=True)
        assert v.is_high_cost is True

    def test_high_cost_invariant_false_below_threshold(self):
        v = _view(cost_usd=HIGH_COST_USD_THRESHOLD - 1, is_high_cost=False)
        assert v.is_high_cost is False

    def test_high_cost_invariant_violated_raises_on_construct(self):
        with pytest.raises(AssertionError):
            _view(cost_usd=HIGH_COST_USD_THRESHOLD + 1, is_high_cost=False)
        with pytest.raises(AssertionError):
            _view(cost_usd=0.0, is_high_cost=True)

    def test_structural_equality(self):
        a = _view(cost_usd=5.0)
        b = _view(cost_usd=5.0)
        assert a == b
        assert hash(a) == hash(b)

    def test_inequality_on_any_field_change(self):
        a = _view(cost_usd=5.0)
        b = _view(cost_usd=5.01)
        assert a != b


# ---------------------------------------------------------------------------
# WorldSnapshot
# ---------------------------------------------------------------------------

class TestWorldSnapshot:
    def test_empty_is_constructible_and_safe_for_render(self):
        snap = WorldSnapshot.empty()
        assert snap.session_groups == ()
        assert snap.today_cost_usd == 0.0
        assert snap.quota is None
        assert snap.available_providers == ()
        assert snap.selected_provider is None

    def test_two_empties_are_equal(self):
        # distinct_until_changed must skip when an empty is followed by
        # another empty (e.g. between Snapshotter rebuilds with no
        # sessions yet).
        assert WorldSnapshot.empty() == WorldSnapshot.empty()

    def test_with_one_session_is_equal_to_itself(self):
        v = _view()
        snap = WorldSnapshot(
            session_groups=_sg(v),
            today_cost_usd=1.0,
            quota=None,
            available_providers=("anthropic",),
            selected_provider="anthropic",
            fetched_at=datetime.now(timezone.utc),
        )
        same_snap = WorldSnapshot(
            session_groups=_sg(v),  # tuple element comparison
            today_cost_usd=1.0,
            quota=None,
            available_providers=("anthropic",),
            selected_provider="anthropic",
            fetched_at=snap.fetched_at,
        )
        assert snap == same_snap

    # NOTE: ``render_key`` was removed from WorldSnapshot in F4. UI dedup
    # is now per-surface — each surface declares its own ``compute(snap)``
    # function and ``distinct_until_changed`` is keyed on that. Per-
    # surface dedup behaviour is covered by tests/ui/test_render_snap.py.

    def test_session_order_is_significant(self):
        """session_groups tuples are order-sensitive — adapters return
        groups in a deterministic order so the UI can rely on consistent
        layout across renders."""
        from claude_island.core.models import Session
        a = _view(cost_usd=1.0)
        b_sess = Session(
            pid=99, project_path=Path("/b"),
            session_uuid="",
            last_activity=datetime.now(timezone.utc),
        )
        b = SessionView(
            pid=99, name="b", project_path=Path("/b"),
            project_basename="b",
            last_activity=datetime.now(timezone.utc),
            is_running=False, cost_usd=1.0, is_high_cost=False,
            latest_model=None, status_word=None, session=b_sess,
        )
        ts = datetime.now(timezone.utc)
        s_ab = WorldSnapshot(
            session_groups=_sg(a) + _sg(b), today_cost_usd=2.0, quota=None,
            available_providers=(), selected_provider=None,
            fetched_at=ts,
        )
        s_ba = WorldSnapshot(
            session_groups=_sg(b) + _sg(a), today_cost_usd=2.0, quota=None,
            available_providers=(), selected_provider=None,
            fetched_at=ts,
        )
        assert s_ab != s_ba


# ---------------------------------------------------------------------------
# _WorldStore
# ---------------------------------------------------------------------------

class TestWorldStore:
    def test_initial_current_is_empty(self):
        store = _WorldStore()
        assert store.current == WorldSnapshot.empty()

    def test_push_updates_current(self):
        store = _WorldStore()
        snap = WorldSnapshot(
            session_groups=_sg(_view()), today_cost_usd=5.0, quota=None,
            available_providers=(), selected_provider=None,
            fetched_at=datetime.now(timezone.utc),
        )
        store.push(snap)
        assert store.current == snap

    def test_subscribe_replays_current_value_immediately(self):
        store = _WorldStore()
        snap = WorldSnapshot(
            session_groups=(), today_cost_usd=42.0, quota=None,
            available_providers=(), selected_provider=None,
            fetched_at=datetime.now(timezone.utc),
        )
        store.push(snap)

        received: list[WorldSnapshot] = []
        store.observable().subscribe(received.append)

        assert received == [snap]

    def test_multiple_subscribers_all_receive_pushes(self):
        store = _WorldStore()
        a: list[WorldSnapshot] = []
        b: list[WorldSnapshot] = []
        store.observable().subscribe(a.append)
        store.observable().subscribe(b.append)

        snap = WorldSnapshot(
            session_groups=(), today_cost_usd=1.0, quota=None,
            available_providers=(), selected_provider=None,
            fetched_at=datetime.now(timezone.utc),
        )
        store.push(snap)

        assert len(a) == 2 and a[-1] == snap
        assert len(b) == 2 and b[-1] == snap

    def test_observable_does_not_expose_on_next(self):
        store = _WorldStore()
        obs = store.observable()
        from reactivex import Observable as _Observable
        assert isinstance(obs, _Observable)

    def test_reset_for_testing_clears_current(self):
        store = _WorldStore()
        snap = WorldSnapshot(
            session_groups=_sg(_view()), today_cost_usd=99.0, quota=None,
            available_providers=(), selected_provider=None,
            fetched_at=datetime.now(timezone.utc),
        )
        store.push(snap)
        assert store.current == snap

        store.reset_for_testing()
        assert store.current == WorldSnapshot.empty()

    def test_reset_for_testing_disposes_old_subscribers(self):
        store = _WorldStore()
        old_received: list[WorldSnapshot] = []
        store.observable().subscribe(old_received.append)
        assert len(old_received) == 1

        store.reset_for_testing()
        new_snap = WorldSnapshot(
            session_groups=(), today_cost_usd=7.0, quota=None,
            available_providers=(), selected_provider=None,
            fetched_at=datetime.now(timezone.utc),
        )
        store.push(new_snap)

        assert len(old_received) == 1


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

class TestWorldSingleton:
    def test_world_is_a_world_store_instance(self):
        assert isinstance(world, _WorldStore)

    def test_world_starts_with_empty_snapshot(self):
        world.reset_for_testing()
        assert world.current == WorldSnapshot.empty()
