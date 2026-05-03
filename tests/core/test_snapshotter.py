"""Tests for Snapshotter and compose_session_view.

Strategy: tests for ``compose_session_view`` use plain in-memory fakes
(no Qt, no sleeping). Tests for ``Snapshotter`` use a fake injected
``publish`` callable + a fake ``session_source`` and assert outcomes
on a short timeout — no need for QApplication because Snapshotter's
worker is reactivex's EventLoopScheduler (its own thread, fully
opaque to Qt).
"""
from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from claude_island.core.models import Session, UsageTotals
from claude_island.core.snapshot import (
    HIGH_COST_USD_THRESHOLD,
    SessionView,
    Snapshotter,
    WorldSnapshot,
    _resolve_is_running,
    compose_session_view,
)


# ---------------------------------------------------------------------------
# Helper fakes
# ---------------------------------------------------------------------------

def _session(
    pid: int = 1234,
    cwd: str = "/tmp/proj",
    *,
    uuid: str = "",
    last_activity: datetime | None = None,
) -> Session:
    return Session(
        pid=pid,
        project_path=Path(cwd),
        session_uuid=uuid,
        window_handle=None,
        last_activity=last_activity or datetime.now(timezone.utc),
    )


class FakeStateReader:
    """In-memory state reader. Each test sets the dict per pid."""
    def __init__(self, table: dict[int, dict | None] | None = None):
        self.table = table or {}

    def read_session_state(self, pid: int) -> dict | None:
        return self.table.get(pid)


class FakeMetadataProvider:
    def __init__(self, table: dict[str, dict] | None = None):
        self.table = table or {}

    def get_session_metadata(self, uuid: str) -> dict | None:
        return self.table.get(uuid)


class FakeUsageRegistry:
    def __init__(
        self,
        summaries: dict[str, tuple[float, int, int]] | None = None,
        latest_models: dict[str, str] | None = None,
        today_cost: float = 0.0,
    ):
        self.summaries = summaries or {}
        self.latest_models = latest_models or {}
        self.today_cost = today_cost

    def get_session_summary(self, uuid: str) -> tuple[float, int, int]:
        return self.summaries.get(uuid, (0.0, 0, 0))

    def get_latest_model(self, uuid: str) -> str | None:
        return self.latest_models.get(uuid)

    def get_totals(self, period: str) -> UsageTotals:
        # Simulate UsageTotals' cost_usd property by faking input_cost.
        return UsageTotals(period=period, input_cost=self.today_cost)


class FakeNamesStore:
    def __init__(self, names: dict[str, str] | None = None):
        self.names = names or {}

    def get_session_name(self, uuid: str) -> str | None:
        return self.names.get(uuid)


class FakeSessionSource:
    """Looks like SessionRegistry — exposes a ``sessions`` property."""
    def __init__(self, sessions: list[Session] | None = None):
        self._sessions = sessions or []

    @property
    def sessions(self) -> list[Session]:
        return list(self._sessions)


# ---------------------------------------------------------------------------
# _resolve_is_running — the priority chain
# ---------------------------------------------------------------------------

class TestResolveIsRunning:
    def test_busy_status_wins(self):
        # Old activity but status=busy → running
        old = datetime.now(timezone.utc) - timedelta(hours=1)
        assert _resolve_is_running(
            status_word="busy", last_activity=old, active_threshold_s=30,
        ) is True

    def test_waiting_status_wins(self):
        old = datetime.now(timezone.utc) - timedelta(hours=1)
        assert _resolve_is_running(
            status_word="waiting", last_activity=old, active_threshold_s=30,
        ) is True

    def test_idle_status_blocks_heuristic(self):
        # This is the bug-fix property: even with very recent activity,
        # idle status → NOT running.
        recent = datetime.now(timezone.utc)
        assert _resolve_is_running(
            status_word="idle", last_activity=recent, active_threshold_s=30,
        ) is False

    def test_idle_status_case_insensitive(self):
        recent = datetime.now(timezone.utc)
        assert _resolve_is_running(
            status_word="IDLE", last_activity=recent, active_threshold_s=30,
        ) is False
        assert _resolve_is_running(
            status_word="Busy", last_activity=recent, active_threshold_s=30,
        ) is True

    def test_unknown_status_uses_heuristic_recent(self):
        recent = datetime.now(timezone.utc) - timedelta(seconds=5)
        assert _resolve_is_running(
            status_word=None, last_activity=recent, active_threshold_s=30,
        ) is True

    def test_unknown_status_uses_heuristic_old(self):
        old = datetime.now(timezone.utc) - timedelta(minutes=5)
        assert _resolve_is_running(
            status_word=None, last_activity=old, active_threshold_s=30,
        ) is False

    def test_garbage_status_falls_through_to_heuristic(self):
        # An unrecognised status string ≠ idle/busy/waiting should
        # not silently mark the session as running — fall through to
        # heuristic.
        recent = datetime.now(timezone.utc)
        old = datetime.now(timezone.utc) - timedelta(minutes=5)
        assert _resolve_is_running(
            status_word="garbage", last_activity=recent, active_threshold_s=30,
        ) is True
        assert _resolve_is_running(
            status_word="garbage", last_activity=old, active_threshold_s=30,
        ) is False

    def test_invalid_last_activity_returns_false(self):
        # If last_activity is not a real datetime, treat as "not running"
        # rather than letting an exception propagate.
        assert _resolve_is_running(
            status_word=None, last_activity=None,  # type: ignore[arg-type]
            active_threshold_s=30,
        ) is False


# ---------------------------------------------------------------------------
# compose_session_view — single source of truth for SessionView shape
# ---------------------------------------------------------------------------

class TestComposeSessionView:
    def test_full_data_path(self):
        s = _session(pid=1, uuid="u1")
        view = compose_session_view(
            s,
            state_reader=FakeStateReader({1: {"sessionId": "u1", "status": "busy", "name": "my-feature"}}),
            metadata_provider=FakeMetadataProvider({"u1": {"ai_title": "ai title"}}),
            usage_registry=FakeUsageRegistry(
                summaries={"u1": (12.34, 5, 1)},
                latest_models={"u1": "claude-opus-4-7"},
            ),
            names_store=FakeNamesStore({"u1": "user-renamed"}),
        )
        # Custom name wins over state name wins over ai_title.
        assert view.name == "user-renamed"
        assert view.cost_usd == 12.34
        assert view.is_high_cost is False
        assert view.is_running is True
        assert view.status_word == "busy"
        assert view.latest_model == "claude-opus-4-7"

    def test_state_name_used_when_no_custom_name(self):
        s = _session(pid=1, uuid="u1")
        view = compose_session_view(
            s,
            state_reader=FakeStateReader({1: {"sessionId": "u1", "name": "auto-name"}}),
            metadata_provider=FakeMetadataProvider(),
            usage_registry=FakeUsageRegistry(),
            names_store=FakeNamesStore(),
        )
        assert view.name == "auto-name"

    def test_falls_back_to_basename_when_no_metadata(self):
        s = _session(pid=1, cwd="/tmp/foo")
        view = compose_session_view(
            s,
            state_reader=FakeStateReader(),
            metadata_provider=FakeMetadataProvider(),
            usage_registry=FakeUsageRegistry(),
            names_store=FakeNamesStore(),
        )
        assert view.name == "foo"

    def test_high_cost_threshold(self):
        s = _session(uuid="u1")
        view_high = compose_session_view(
            s,
            state_reader=FakeStateReader(),
            metadata_provider=FakeMetadataProvider(),
            usage_registry=FakeUsageRegistry(
                summaries={"u1": (HIGH_COST_USD_THRESHOLD + 1, 0, 0)},
            ),
            names_store=FakeNamesStore(),
        )
        assert view_high.is_high_cost is True

        view_low = compose_session_view(
            s,
            state_reader=FakeStateReader(),
            metadata_provider=FakeMetadataProvider(),
            usage_registry=FakeUsageRegistry(
                summaries={"u1": (HIGH_COST_USD_THRESHOLD - 1, 0, 0)},
            ),
            names_store=FakeNamesStore(),
        )
        assert view_low.is_high_cost is False

    def test_session_uuid_from_state_takes_precedence(self):
        """The state file's sessionId is canonical — overrides whatever
        ProcessScanner left in Session.session_uuid (which is often
        empty)."""
        s = _session(pid=1, uuid="from-scanner")
        view = compose_session_view(
            s,
            state_reader=FakeStateReader({1: {"sessionId": "from-state"}}),
            metadata_provider=FakeMetadataProvider(),
            usage_registry=FakeUsageRegistry(
                summaries={"from-state": (5.0, 1, 0)},
            ),
            names_store=FakeNamesStore(),
        )
        assert view.cost_usd == 5.0  # used from-state to look up summary

    def test_dependency_raises_returns_degraded_field(self):
        """Per-source exception isolation — if state reader explodes,
        the view still constructs with state-derived fields = None."""
        s = _session()

        class ExplodingReader:
            def read_session_state(self, pid):
                raise RuntimeError("disk on fire")

        view = compose_session_view(
            s,
            state_reader=ExplodingReader(),
            metadata_provider=FakeMetadataProvider(),
            usage_registry=FakeUsageRegistry(),
            names_store=FakeNamesStore(),
        )
        # state-derived fields gone, but the view exists.
        assert view.status_word is None
        assert view.is_high_cost is False


# ---------------------------------------------------------------------------
# Snapshotter
# ---------------------------------------------------------------------------

def _make_snapshotter(
    *,
    sessions: list[Session] | None = None,
    publish=None,
    debounce_window_s: float = 0.05,  # short for fast tests
    throttle_first_window_s: float = 0.0,  # disabled by default
    today_cost: float = 0.0,
) -> tuple[Snapshotter, list[WorldSnapshot]]:
    """Build a Snapshotter wired with fakes; return (snapshotter,
    received_snapshots_list). The publish callback appends to the
    list so tests assert on what got published."""
    received: list[WorldSnapshot] = []
    snap = Snapshotter(
        session_source=FakeSessionSource(sessions or []),
        state_reader=FakeStateReader(),
        metadata_provider=FakeMetadataProvider(),
        usage_registry=FakeUsageRegistry(today_cost=today_cost),
        names_store=FakeNamesStore(),
        get_quota=lambda: None,
        get_available_providers=lambda: [],
        get_selected_provider=lambda: None,
        publish=publish or received.append,
        debounce_window_s=debounce_window_s,
        throttle_first_window_s=throttle_first_window_s,
    )
    return snap, received


class TestSnapshotterBuildNow:
    """build_now is the synchronous path used at boot + in tests."""

    def test_build_now_returns_empty_for_no_sessions(self):
        snap, _ = _make_snapshotter()
        result = snap.build_now()
        assert result.sessions == ()

    def test_build_now_includes_sessions(self):
        s1 = _session(pid=1, cwd="/a")
        s2 = _session(pid=2, cwd="/b")
        snap, _ = _make_snapshotter(sessions=[s1, s2])
        result = snap.build_now()
        assert len(result.sessions) == 2
        assert {v.pid for v in result.sessions} == {1, 2}

    def test_build_now_carries_today_cost(self):
        snap, _ = _make_snapshotter(today_cost=42.5)
        assert snap.build_now().today_cost_usd == 42.5

    def test_build_now_does_not_publish(self):
        snap, received = _make_snapshotter()
        snap.build_now()
        assert received == []  # build_now is synchronous, returns; no push


class TestSnapshotterPipeline:
    """Tests for the wake → debounce → throttle → publish pipeline."""

    def test_start_then_wake_publishes_one_snapshot(self):
        snap, received = _make_snapshotter(debounce_window_s=0.05)
        snap.start()
        try:
            snap.wake()
            # debounce 50ms + processing → wait a generous margin
            time.sleep(0.25)
            assert len(received) == 1
        finally:
            snap.stop()

    def test_burst_of_wakes_debounced_to_one_publish(self):
        snap, received = _make_snapshotter(debounce_window_s=0.05)
        snap.start()
        try:
            for _ in range(5):
                snap.wake()
                time.sleep(0.005)  # tighter than debounce window
            time.sleep(0.25)
            # All 5 wakes within the debounce window → 1 build.
            assert len(received) == 1
        finally:
            snap.stop()

    def test_wake_after_publish_publishes_again(self):
        snap, received = _make_snapshotter(debounce_window_s=0.05)
        snap.start()
        try:
            snap.wake()
            time.sleep(0.2)
            snap.wake()
            time.sleep(0.2)
            assert len(received) == 2
        finally:
            snap.stop()

    def test_publish_raising_does_not_kill_pipeline(self):
        """If the publish callback explodes (e.g. UI render bug), the
        next wake should still produce a snapshot — the worker pipeline
        must not die from a downstream error."""
        call_log: list[str] = []

        def publish(_: WorldSnapshot) -> None:
            call_log.append("got")
            if len(call_log) == 1:
                raise RuntimeError("boom on first push")

        snap, _ = _make_snapshotter(publish=publish, debounce_window_s=0.05)
        snap.start()
        try:
            snap.wake()
            time.sleep(0.2)
            snap.wake()
            time.sleep(0.2)
            # Both pushes attempted — the second arrived even though
            # the first raised.
            assert len(call_log) == 2
        finally:
            snap.stop()

    def test_session_source_raising_degrades_to_empty_session_list(self):
        """If the session source raises (e.g. process scanner glitch),
        ``_safe_list_sessions`` catches and returns []. The build still
        succeeds and publishes — just with no sessions. The pipeline
        must NOT die from a flaky data source."""
        call_count = [0]

        class FlakySource:
            @property
            def sessions(self):
                call_count[0] += 1
                if call_count[0] == 1:
                    raise RuntimeError("scanner crashed")
                return []

        received: list[WorldSnapshot] = []
        snap = Snapshotter(
            session_source=FlakySource(),
            state_reader=FakeStateReader(),
            metadata_provider=FakeMetadataProvider(),
            usage_registry=FakeUsageRegistry(),
            names_store=FakeNamesStore(),
            get_quota=lambda: None,
            get_available_providers=lambda: [],
            get_selected_provider=lambda: None,
            publish=received.append,
            debounce_window_s=0.05,
            throttle_first_window_s=0.0,
        )
        snap.start()
        try:
            snap.wake()  # first source.sessions read raises
            time.sleep(0.2)
            assert len(received) == 1
            assert received[0].sessions == ()  # degraded to empty

            snap.wake()  # second read succeeds (returns [])
            time.sleep(0.2)
            assert len(received) == 2  # pipeline survived; second push happened
        finally:
            snap.stop()

    def test_throttle_first_caps_publish_rate(self):
        """Under sustained wakes, throttle_first should cap the publish
        frequency. Use a 100 ms cap window — five wakes 20 ms apart
        produce at most ~1 publish per 100 ms."""
        snap, received = _make_snapshotter(
            debounce_window_s=0.0,
            throttle_first_window_s=0.1,
        )
        snap.start()
        try:
            for _ in range(10):
                snap.wake()
                time.sleep(0.02)  # 20 ms between wakes
            time.sleep(0.3)
            # 200 ms of wakes + 300 ms idle:
            #   throttle_first emits the first wake immediately, then
            #   suppresses further wakes until 100 ms elapses. So we
            #   expect ~3 publishes (at t=0, 100, 200).
            assert 1 <= len(received) <= 4
        finally:
            snap.stop()


class TestSnapshotterLifecycle:
    def test_start_is_idempotent(self):
        snap, _ = _make_snapshotter()
        snap.start()
        snap.start()  # second call should be no-op, not crash
        snap.stop()

    def test_stop_is_idempotent(self):
        snap, _ = _make_snapshotter()
        snap.stop()  # before start — must not crash
        snap.start()
        snap.stop()
        snap.stop()  # after stop — must not crash

    def test_wake_before_start_is_buffered_silently(self):
        """Calling wake() before start() should not raise. The wake is
        sent to the Subject (which has no subscribers yet) — it goes
        nowhere. Once start() is called the next wake is the first
        the pipeline sees."""
        snap, received = _make_snapshotter()
        snap.wake()  # no-op effectively
        snap.start()
        try:
            snap.wake()
            time.sleep(0.2)
            assert len(received) == 1  # only the post-start wake
        finally:
            snap.stop()
