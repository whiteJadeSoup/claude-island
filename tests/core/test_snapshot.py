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
    SessionView,
    WorldSnapshot,
    _WorldStore,
    world,
)


# ---------------------------------------------------------------------------
# SessionView
# ---------------------------------------------------------------------------

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
    return SessionView(
        pid=1234,
        name="test",
        project_path=Path("/tmp/test"),
        project_basename="test",
        last_activity=datetime.now(timezone.utc),
        is_running=is_running,
        cost_usd=cost_usd,
        is_high_cost=is_high_cost,
        latest_model="claude-opus-4-7",
        status_word="idle",
        window_handle=None,
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
        # cost above threshold + is_high_cost=True → ok
        v = _view(cost_usd=HIGH_COST_USD_THRESHOLD + 1, is_high_cost=True)
        assert v.is_high_cost is True

    def test_high_cost_invariant_false_below_threshold(self):
        v = _view(cost_usd=HIGH_COST_USD_THRESHOLD - 1, is_high_cost=False)
        assert v.is_high_cost is False

    def test_high_cost_invariant_violated_raises_on_construct(self):
        # cost above threshold but is_high_cost=False → invariant violated
        with pytest.raises(AssertionError):
            _view(cost_usd=HIGH_COST_USD_THRESHOLD + 1, is_high_cost=False)
        with pytest.raises(AssertionError):
            _view(cost_usd=0.0, is_high_cost=True)

    def test_structural_equality(self):
        """Two SessionViews with identical fields compare equal — this
        is the property distinct_until_changed depends on for skipping
        no-op snapshots."""
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
        assert snap.sessions == ()
        assert snap.today_cost_usd == 0.0
        assert snap.quota is None
        assert snap.available_providers == ()
        assert snap.selected_provider is None

    def test_two_empties_are_equal(self):
        # Important: distinct_until_changed must skip when an empty
        # is followed by another empty (e.g. between Snapshotter
        # rebuilds with no sessions yet).
        assert WorldSnapshot.empty() == WorldSnapshot.empty()

    def test_with_one_session_is_equal_to_itself(self):
        v = _view()
        snap = WorldSnapshot(
            sessions=(v,),
            today_cost_usd=1.0,
            quota=None,
            available_providers=("anthropic",),
            selected_provider="anthropic",
            fetched_at=datetime.now(timezone.utc),
        )
        same_snap = WorldSnapshot(
            sessions=(v,),  # tuple element comparison
            today_cost_usd=1.0,
            quota=None,
            available_providers=("anthropic",),
            selected_provider="anthropic",
            fetched_at=snap.fetched_at,
        )
        assert snap == same_snap

    def test_session_order_is_significant(self):
        """sessions tuples are order-sensitive — Snapshotter sorts
        deterministically before constructing the snapshot, so the UI
        can rely on the same order across renders."""
        a = _view(cost_usd=1.0)
        b = SessionView(
            pid=99, name="b", project_path=Path("/b"),
            project_basename="b",
            last_activity=datetime.now(timezone.utc),
            is_running=False, cost_usd=1.0, is_high_cost=False,
            latest_model=None, status_word=None, window_handle=None,
        )
        s1 = WorldSnapshot.empty()
        s_ab = WorldSnapshot(
            sessions=(a, b), today_cost_usd=2.0, quota=None,
            available_providers=(), selected_provider=None,
            fetched_at=s1.fetched_at,
        )
        s_ba = WorldSnapshot(
            sessions=(b, a), today_cost_usd=2.0, quota=None,
            available_providers=(), selected_provider=None,
            fetched_at=s1.fetched_at,
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
            sessions=(_view(),), today_cost_usd=5.0, quota=None,
            available_providers=(), selected_provider=None,
            fetched_at=datetime.now(timezone.utc),
        )
        store.push(snap)
        assert store.current == snap

    def test_subscribe_replays_current_value_immediately(self):
        """BehaviorSubject contract — proven in the smoke test for
        reactivex itself, but re-asserted on our wrapper to lock the
        contract our UI render relies on at this layer."""
        store = _WorldStore()
        snap = WorldSnapshot(
            sessions=(), today_cost_usd=42.0, quota=None,
            available_providers=(), selected_provider=None,
            fetched_at=datetime.now(timezone.utc),
        )
        store.push(snap)

        received: list[WorldSnapshot] = []
        store.observable().subscribe(received.append)

        # Subscriber receives the current value (snap) immediately on
        # subscribe — without waiting for any further on_next.
        assert received == [snap]

    def test_multiple_subscribers_all_receive_pushes(self):
        store = _WorldStore()
        a: list[WorldSnapshot] = []
        b: list[WorldSnapshot] = []
        store.observable().subscribe(a.append)
        store.observable().subscribe(b.append)

        snap = WorldSnapshot(
            sessions=(), today_cost_usd=1.0, quota=None,
            available_providers=(), selected_provider=None,
            fetched_at=datetime.now(timezone.utc),
        )
        store.push(snap)

        # Each subscriber received initial empty + snap = 2 values.
        assert len(a) == 2 and a[-1] == snap
        assert len(b) == 2 and b[-1] == snap

    def test_observable_does_not_expose_on_next(self):
        """The returned Observable must not give callers a back-door to
        push values directly. We can't make this *impossible* in
        Python (BehaviorSubject upcast still has on_next at runtime),
        but the type signature should not advertise it. This test
        documents the intent — a future refactor that wraps the
        Subject more strictly would tighten this further."""
        store = _WorldStore()
        obs = store.observable()
        # Observable is the abstract type — type checkers won't allow
        # ``obs.on_next(...)`` even though runtime would (BehaviorSubject
        # is the actual class). Static guarantee, not runtime.
        from reactivex import Observable as _Observable
        assert isinstance(obs, _Observable)

    def test_reset_for_testing_clears_current(self):
        store = _WorldStore()
        snap = WorldSnapshot(
            sessions=(_view(),), today_cost_usd=99.0, quota=None,
            available_providers=(), selected_provider=None,
            fetched_at=datetime.now(timezone.utc),
        )
        store.push(snap)
        assert store.current == snap

        store.reset_for_testing()
        assert store.current == WorldSnapshot.empty()

    def test_reset_for_testing_disposes_old_subscribers(self):
        """After reset, callbacks subscribed before the reset must NOT
        receive new pushes — otherwise stale test fixtures would
        accumulate across runs and produce confusing failures."""
        store = _WorldStore()
        old_received: list[WorldSnapshot] = []
        store.observable().subscribe(old_received.append)
        # Initial empty arrived.
        assert len(old_received) == 1

        store.reset_for_testing()
        new_snap = WorldSnapshot(
            sessions=(), today_cost_usd=7.0, quota=None,
            available_providers=(), selected_provider=None,
            fetched_at=datetime.now(timezone.utc),
        )
        store.push(new_snap)

        # Old subscriber count unchanged — the new push went to nobody
        # (nobody re-subscribed yet).
        assert len(old_received) == 1


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

class TestWorldSingleton:
    def test_world_is_a_world_store_instance(self):
        assert isinstance(world, _WorldStore)

    def test_world_starts_with_empty_snapshot(self):
        # Note: this test relies on conftest.py auto-resetting between
        # tests. Without that, an earlier test's push would leak here.
        world.reset_for_testing()
        assert world.current == WorldSnapshot.empty()
