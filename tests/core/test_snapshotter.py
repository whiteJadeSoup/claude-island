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
from unittest import mock

import pytest

from claude_island.core.hook_events import SessionLiveState
from claude_island.core.models import Session, UsageTotals
from claude_island.core.session_phase import SessionPhase
from claude_island.core.snapshot import (
    HIGH_COST_USD_THRESHOLD,
    SessionView,
    Snapshotter,
    WorldSnapshot,
    _dedup_views_by_session_uuid,
    _phase_from_pid_json,
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
# _phase_from_pid_json — pid.json fallback (used when hook live_state absent)
# ---------------------------------------------------------------------------

class TestPhaseFromPidJson:
    """T6.x family: the degraded-path phase mapping that fires when
    hook live_state is None (session pre-dates the listener / no
    listener at all). Maps Claude's status word + activity timestamp
    to a SessionPhase."""

    def test_busy_status_with_recent_activity_maps_to_thinking(self):
        from claude_island.core.session_phase import SessionPhase
        recent = datetime.now(timezone.utc) - timedelta(seconds=10)
        assert _phase_from_pid_json(
            status_word="busy", last_activity=recent, active_threshold_s=30,
        ) is SessionPhase.THINKING

    def test_busy_status_with_stale_activity_falls_through_to_idle(self):
        """Bug B (2026-05-13): pid.json status='busy' must NOT mark a
        6h-stale session as running. Claude doesn't always flip status
        back to idle on crash/kill, so we require recent activity to
        trust the status word."""
        from claude_island.core.session_phase import SessionPhase
        very_old = datetime.now(timezone.utc) - timedelta(hours=6)
        assert _phase_from_pid_json(
            status_word="busy", last_activity=very_old, active_threshold_s=30,
        ) is SessionPhase.IDLE

    def test_waiting_status_with_recent_activity_maps_to_waiting_approval(self):
        from claude_island.core.session_phase import SessionPhase
        recent = datetime.now(timezone.utc) - timedelta(seconds=30)
        assert _phase_from_pid_json(
            status_word="waiting", last_activity=recent, active_threshold_s=30,
        ) is SessionPhase.WAITING_APPROVAL

    def test_waiting_status_stale_falls_to_idle(self):
        from claude_island.core.session_phase import SessionPhase
        old = datetime.now(timezone.utc) - timedelta(hours=1)
        assert _phase_from_pid_json(
            status_word="waiting", last_activity=old, active_threshold_s=30,
        ) is SessionPhase.IDLE

    def test_busy_within_5min_threshold(self):
        """Boundary: 4min59s old activity + busy status should still
        be THINKING (within the 5-minute freshness window)."""
        from claude_island.core.session_phase import SessionPhase
        from claude_island.core.snapshot import _PID_JSON_FRESHNESS_S
        just_inside = datetime.now(timezone.utc) - timedelta(
            seconds=_PID_JSON_FRESHNESS_S - 60,
        )
        assert _phase_from_pid_json(
            status_word="busy", last_activity=just_inside, active_threshold_s=30,
        ) is SessionPhase.THINKING

    def test_busy_just_past_5min_threshold(self):
        """Boundary: 5min1s old activity + busy → IDLE."""
        from claude_island.core.session_phase import SessionPhase
        from claude_island.core.snapshot import _PID_JSON_FRESHNESS_S
        just_outside = datetime.now(timezone.utc) - timedelta(
            seconds=_PID_JSON_FRESHNESS_S + 60,
        )
        assert _phase_from_pid_json(
            status_word="busy", last_activity=just_outside, active_threshold_s=30,
        ) is SessionPhase.IDLE

    def test_idle_status_blocks_heuristic(self):
        # Even with very recent activity, idle status → IDLE (no escalation).
        from claude_island.core.session_phase import SessionPhase
        recent = datetime.now(timezone.utc)
        assert _phase_from_pid_json(
            status_word="idle", last_activity=recent, active_threshold_s=30,
        ) is SessionPhase.IDLE

    def test_status_case_insensitive(self):
        from claude_island.core.session_phase import SessionPhase
        recent = datetime.now(timezone.utc)
        assert _phase_from_pid_json(
            status_word="IDLE", last_activity=recent, active_threshold_s=30,
        ) is SessionPhase.IDLE
        assert _phase_from_pid_json(
            status_word="Busy", last_activity=recent, active_threshold_s=30,
        ) is SessionPhase.THINKING

    def test_no_status_recent_activity_thinking(self):
        from claude_island.core.session_phase import SessionPhase
        recent = datetime.now(timezone.utc) - timedelta(seconds=5)
        assert _phase_from_pid_json(
            status_word=None, last_activity=recent, active_threshold_s=30,
        ) is SessionPhase.THINKING

    def test_no_status_stale_activity_idle(self):
        from claude_island.core.session_phase import SessionPhase
        old = datetime.now(timezone.utc) - timedelta(minutes=5)
        assert _phase_from_pid_json(
            status_word=None, last_activity=old, active_threshold_s=30,
        ) is SessionPhase.IDLE

    def test_garbage_status_falls_through_to_heuristic(self):
        from claude_island.core.session_phase import SessionPhase
        recent = datetime.now(timezone.utc)
        old = datetime.now(timezone.utc) - timedelta(minutes=5)
        assert _phase_from_pid_json(
            status_word="garbage", last_activity=recent, active_threshold_s=30,
        ) is SessionPhase.THINKING
        assert _phase_from_pid_json(
            status_word="garbage", last_activity=old, active_threshold_s=30,
        ) is SessionPhase.IDLE

    def test_invalid_last_activity_returns_idle(self):
        from claude_island.core.session_phase import SessionPhase
        assert _phase_from_pid_json(
            status_word=None, last_activity=None,  # type: ignore[arg-type]
            active_threshold_s=30,
        ) is SessionPhase.IDLE


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


class TestComposeUuidResolution:
    """Session uuid resolution priority: pid.json's ``sessionId`` is
    authoritative (claude.exe rewrites it on every status transition,
    including after ``/clear`` and ``/resume <other>``); the bridge-
    populated ``session.session_uuid`` is the fallback for the brief
    window before pid.json is written. There is no cmdline path —
    cmdline ``--resume`` is frozen at process launch and goes stale
    immediately on any session change (user bug 2026-05-25)."""

    def test_pid_json_session_id_is_used(self):
        """Happy path: pid.json has a sessionId → that's the view's uuid,
        and downstream lookups (cost / latest_model) key off it."""
        s = _session(pid=97372, uuid="")
        new = "f56fb0ca-649d-4708-8c24-76a18857a0c6"
        view = compose_session_view(
            s,
            state_reader=FakeStateReader({97372: {"sessionId": new, "status": "idle"}}),
            metadata_provider=FakeMetadataProvider(),
            usage_registry=FakeUsageRegistry(
                summaries={new: (12.34, 5, 1)},
                latest_models={new: "claude-opus-4-7"},
            ),
            names_store=FakeNamesStore(),
        )
        assert view.session_uuid == new
        assert view.cost_usd == 12.34
        assert view.latest_model == "claude-opus-4-7"

    def test_pid_json_session_id_overrides_session_session_uuid(self):
        """``/clear`` divergence: bridge had upserted OLD into the registry
        before claude rewrote pid.json to NEW. compose must trust pid.json
        and produce a view keyed on NEW, not the registry's OLD."""
        old = "413eda01-6271-43cb-934b-035b236c0154"  # stale registry uuid
        new = "f56fb0ca-649d-4708-8c24-76a18857a0c6"  # pid.json after /clear
        s = _session(pid=97372, uuid=old)
        view = compose_session_view(
            s,
            state_reader=FakeStateReader({97372: {"sessionId": new, "status": "idle"}}),
            metadata_provider=FakeMetadataProvider(),
            usage_registry=FakeUsageRegistry(
                summaries={new: (5.0, 3, 1)},
                latest_models={new: "claude-opus-4-7"},
            ),
            names_store=FakeNamesStore(),
        )
        assert view.session_uuid == new, (
            f"expected pid.json NEW to win over registry OLD, got {view.session_uuid!r}"
        )
        assert view.latest_model == "claude-opus-4-7"

    def test_falls_back_to_session_session_uuid_when_pid_json_missing(self):
        """pid.json absent (read race / fresh process / permission denied):
        the bridge-populated ``session.session_uuid`` (from SessionStart
        hook payload) is the fallback so lookups still hit the right key."""
        bridge_uuid = "11111111-1111-1111-1111-111111111111"
        s = _session(pid=1, uuid=bridge_uuid)
        view = compose_session_view(
            s,
            state_reader=FakeStateReader(),  # pid.json missing
            metadata_provider=FakeMetadataProvider(),
            usage_registry=FakeUsageRegistry(
                latest_models={bridge_uuid: "claude-opus-4-7"},
            ),
            names_store=FakeNamesStore(),
        )
        assert view.session_uuid == bridge_uuid
        assert view.latest_model == "claude-opus-4-7"

    def test_empty_when_no_source_has_uuid(self):
        """Pre-island session that hasn't fired a hook AND has no pid.json
        (unusual but possible on slow startup): all sources empty → view's
        session_uuid is empty string; downstream lookups degrade gracefully."""
        s = _session(pid=1, uuid="")
        view = compose_session_view(
            s,
            state_reader=FakeStateReader(),
            metadata_provider=FakeMetadataProvider(),
            usage_registry=FakeUsageRegistry(),
            names_store=FakeNamesStore(),
        )
        assert view.session_uuid == ""
        assert view.cost_usd == 0.0
        assert view.latest_model is None


class TestHookLivePhaseIdleOverride:
    """Phase resolution cross-references pid.json against hook live_state.

    Regression: the hook chain occasionally loses its closing event
    (PostToolUse / Stop POST timeout, app restart between Pre and Post,
    an API socket error mid-turn that prevents Stop from firing). With
    no override, the state machine stays pinned at THINKING / TOOL_USE
    indefinitely and the capsule keeps showing "running" forever. When
    pid.json (which claude writes on every status transition) says
    "idle", trust it and downgrade the rendered phase to IDLE.
    """

    @staticmethod
    def _live(phase, uuid="uuid-1", tool=None, tool_input=None):
        return SessionLiveState(
            session_uuid=uuid,
            phase=phase,
            cwd=Path("/proj"),
            started_at=datetime(2026, 5, 14, 12, 0, tzinfo=timezone.utc),
            last_hook_at=datetime(2026, 5, 14, 12, 0, tzinfo=timezone.utc),
            current_tool=tool,
            current_tool_input=tool_input,
        )

    def _compose(self, *, live, pid_status):
        sess = Session(
            pid=1,
            project_path=Path("/proj"),
            session_uuid="uuid-1",
            last_activity=datetime(2026, 5, 14, 12, 0, tzinfo=timezone.utc),
        )
        return compose_session_view(
            sess,
            state_reader=FakeStateReader({
                1: {"sessionId": "uuid-1", "status": pid_status},
            }),
            metadata_provider=FakeMetadataProvider(),
            usage_registry=FakeUsageRegistry(),
            names_store=FakeNamesStore(),
            live_state_reader=lambda _uuid: live,
        )

    def test_hook_tool_use_with_pid_idle_renders_as_idle(self):
        live = self._live(SessionPhase.TOOL_USE, tool="Bash", tool_input="ls -la")
        view = self._compose(live=live, pid_status="idle")
        assert view.phase == SessionPhase.IDLE
        assert view.current_tool is None
        # Plan F: idle override must also drop current_tool_input,
        # otherwise the view invariant fires (cti non-None ⇒ TOOL_USE).
        assert view.current_tool_input is None

    def test_hook_thinking_with_pid_idle_renders_as_idle(self):
        live = self._live(SessionPhase.THINKING)
        view = self._compose(live=live, pid_status="idle")
        assert view.phase == SessionPhase.IDLE

    def test_hook_waiting_with_pid_idle_renders_as_idle(self):
        live = SessionLiveState(
            session_uuid="uuid-1",
            phase=SessionPhase.WAITING_APPROVAL,
            cwd=Path("/proj"),
            started_at=datetime(2026, 5, 14, 12, 0, tzinfo=timezone.utc),
            last_hook_at=datetime(2026, 5, 14, 12, 0, tzinfo=timezone.utc),
            pending_permission_tool="Bash",
        )
        view = self._compose(live=live, pid_status="idle")
        assert view.phase == SessionPhase.IDLE

    def test_hook_tool_use_with_pid_busy_preserves_tool_use(self):
        live = self._live(
            SessionPhase.TOOL_USE,
            tool="Bash",
            tool_input="pytest tests/test_login.py",
        )
        view = self._compose(live=live, pid_status="busy")
        assert view.phase == SessionPhase.TOOL_USE
        assert view.current_tool == "Bash"
        # Plan F: command preview must travel to the view so the
        # row-ticker has something to render.
        assert view.current_tool_input == "pytest tests/test_login.py"

    def test_plan_f_tool_use_with_no_tool_input_surfaces_none(self):
        """The hook's ``_extract_tool_input_preview`` returns None for
        opaque MCP tools. View must keep current_tool_input=None then
        — the UI degrades to no ticker line."""
        live = self._live(SessionPhase.TOOL_USE, tool="ExoticMcp", tool_input=None)
        view = self._compose(live=live, pid_status="busy")
        assert view.phase == SessionPhase.TOOL_USE
        assert view.current_tool == "ExoticMcp"
        assert view.current_tool_input is None

    def test_hook_idle_with_pid_busy_stays_idle(self):
        # Hook fresher than pid.json — keep hook's IDLE without override.
        live = self._live(SessionPhase.IDLE)
        view = self._compose(live=live, pid_status="busy")
        assert view.phase == SessionPhase.IDLE

    def test_hook_compacting_with_pid_idle_renders_as_idle(self):
        """User report 2026-05-23: ``/compact`` with "Not enough
        messages to compact" fires PreCompact (phase → COMPACTING)
        but errors before spawning a new session, so the
        SessionStart(source='compact') event that normally closes
        the compact cycle never arrives. The phase stays stuck in
        COMPACTING until the next PromptSubmitted, even though
        Claude is back at the prompt (probe-confirmed
        ``pid.json.status='idle'``).

        Fix: COMPACTING joins the idle-override set so the
        authoritative pid.json signal can recover from this
        dropped-closing-event case, same as it already does for
        THINKING / TOOL_USE / WAITING_APPROVAL."""
        live = self._live(SessionPhase.COMPACTING)
        view = self._compose(live=live, pid_status="idle")
        assert view.phase == SessionPhase.IDLE

    def test_hook_compacting_with_pid_busy_preserves_compacting(self):
        """The override only fires when pid.json reports idle —
        during a real compact in progress (status='busy'), the
        live COMPACTING phase is preserved so the UI still shows
        "compacting · Ns" until the compact finishes."""
        live = self._live(SessionPhase.COMPACTING)
        view = self._compose(live=live, pid_status="busy")
        assert view.phase == SessionPhase.COMPACTING

    def test_hook_tool_use_with_no_pid_status_preserves_tool_use(self):
        # No pid.json status field — no override signal; trust hook.
        live = self._live(SessionPhase.TOOL_USE, tool="Bash")
        sess = Session(
            pid=1,
            project_path=Path("/proj"),
            session_uuid="uuid-1",
            last_activity=datetime(2026, 5, 14, 12, 0, tzinfo=timezone.utc),
        )
        view = compose_session_view(
            sess,
            state_reader=FakeStateReader({1: {"sessionId": "uuid-1"}}),
            metadata_provider=FakeMetadataProvider(),
            usage_registry=FakeUsageRegistry(),
            names_store=FakeNamesStore(),
            live_state_reader=lambda _uuid: live,
        )
        assert view.phase == SessionPhase.TOOL_USE
        assert view.current_tool == "Bash"


class TestComposeLastActivityFromMeta:
    """Per-uuid JSONL activity must override the scanner's create_time
    baseline. Two sessions in the same cwd with different uuids must NOT
    cross-contaminate — that was the project-keyed-override bug."""

    def _compose(
        self,
        session: Session,
        *,
        sess_uuid: str,
        meta_last: datetime | None,
    ):
        meta = {"last_activity": meta_last} if meta_last is not None else {}
        return compose_session_view(
            session,
            state_reader=FakeStateReader({session.pid: {"sessionId": sess_uuid}}),
            metadata_provider=FakeMetadataProvider({sess_uuid: meta}),
            usage_registry=FakeUsageRegistry(),
            names_store=FakeNamesStore(),
        )

    def test_meta_last_supersedes_session_baseline(self):
        scan_time = datetime(2025, 1, 1, 10, 0, tzinfo=timezone.utc)
        meta_last = datetime(2025, 1, 1, 14, 0, tzinfo=timezone.utc)
        view = self._compose(
            _session(pid=1, last_activity=scan_time),
            sess_uuid="uuid-A", meta_last=meta_last,
        )
        assert view.last_activity == meta_last

    def test_session_baseline_kept_when_meta_older(self):
        """Old transcripts (e.g., a JSONL from a previous run with the
        same uuid via --resume) must NOT drag last_activity backward."""
        scan_time = datetime(2025, 1, 1, 14, 0, tzinfo=timezone.utc)
        meta_last = datetime(2025, 1, 1, 10, 0, tzinfo=timezone.utc)
        view = self._compose(
            _session(pid=1, last_activity=scan_time),
            sess_uuid="uuid-A", meta_last=meta_last,
        )
        assert view.last_activity == scan_time

    def test_session_baseline_kept_when_no_meta(self):
        """Session known but no JSONL yet — falls back to scanner's baseline."""
        scan_time = datetime(2025, 1, 1, 10, 0, tzinfo=timezone.utc)
        view = self._compose(
            _session(pid=1, last_activity=scan_time),
            sess_uuid="uuid-A", meta_last=None,
        )
        assert view.last_activity == scan_time

    def test_two_sessions_same_cwd_get_independent_activity(self):
        """The bug: two sessions in /home/x — only one has recent JSONL
        activity. The dormant one must NOT inherit the active one's
        timestamp.
        """
        scan_time = datetime(2025, 1, 1, 9, 0, tzinfo=timezone.utc)
        active_ts = datetime(2025, 1, 1, 14, 0, tzinfo=timezone.utc)

        active = compose_session_view(
            _session(pid=1, cwd="/home/x", last_activity=scan_time),
            state_reader=FakeStateReader({1: {"sessionId": "uuid-A"}}),
            metadata_provider=FakeMetadataProvider(
                {"uuid-A": {"last_activity": active_ts},
                 "uuid-B": {}},
            ),
            usage_registry=FakeUsageRegistry(),
            names_store=FakeNamesStore(),
        )
        dormant = compose_session_view(
            _session(pid=2, cwd="/home/x", last_activity=scan_time),
            state_reader=FakeStateReader({2: {"sessionId": "uuid-B"}}),
            metadata_provider=FakeMetadataProvider(
                {"uuid-A": {"last_activity": active_ts},
                 "uuid-B": {}},
            ),
            usage_registry=FakeUsageRegistry(),
            names_store=FakeNamesStore(),
        )
        assert active.last_activity == active_ts
        assert dormant.last_activity == scan_time


# ---------------------------------------------------------------------------
# _dedup_views_by_session_uuid
# ---------------------------------------------------------------------------

def _view(
    *,
    pid: int,
    uuid: str,
    last_activity: datetime,
    cwd: str = "/tmp",
) -> SessionView:
    """Minimal SessionView for dedup tests — only the fields the dedup
    helper touches matter (pid, session_uuid, last_activity)."""
    sess = Session(
        pid=pid,
        project_path=Path(cwd),
        session_uuid=uuid,
        last_activity=last_activity,
    )
    return SessionView(
        pid=pid,
        name=f"pid-{pid}",
        project_path=sess.project_path,
        project_basename=sess.project_path.name,
        last_activity=last_activity,
        cost_usd=0.0,
        is_high_cost=False,
        latest_model=None,
        status_word=None,
        session=sess,
        session_uuid=uuid,
    )


class TestDedupViewsBySessionUuid:
    """Two pids attached to the same `claude --resume <uuid>` produce
    two SessionViews that share session_uuid. The dedup helper collapses
    them so the UI renders one row per logical session."""

    def test_same_uuid_keeps_one(self):
        t_old = datetime(2026, 5, 14, 10, 0, tzinfo=timezone.utc)
        t_new = datetime(2026, 5, 14, 11, 0, tzinfo=timezone.utc)
        older = _view(pid=100, uuid="abc", last_activity=t_old)
        newer = _view(pid=200, uuid="abc", last_activity=t_new)
        result = _dedup_views_by_session_uuid([older, newer])
        assert len(result) == 1
        assert result[0].pid == 200
        assert result[0].last_activity == t_new

    def test_different_uuids_both_kept(self):
        t = datetime(2026, 5, 14, 11, 0, tzinfo=timezone.utc)
        a = _view(pid=1, uuid="aaa", last_activity=t)
        b = _view(pid=2, uuid="bbb", last_activity=t)
        result = _dedup_views_by_session_uuid([a, b])
        assert len(result) == 2
        assert {v.session_uuid for v in result} == {"aaa", "bbb"}

    def test_empty_uuid_passes_through(self):
        t = datetime(2026, 5, 14, 11, 0, tzinfo=timezone.utc)
        v1 = _view(pid=1, uuid="", last_activity=t)
        v2 = _view(pid=2, uuid="", last_activity=t)
        # Both kept — empty uuid has no merge key.
        result = _dedup_views_by_session_uuid([v1, v2])
        assert len(result) == 2

    def test_tie_on_last_activity_higher_pid_wins(self):
        t = datetime(2026, 5, 14, 11, 0, tzinfo=timezone.utc)
        low = _view(pid=100, uuid="abc", last_activity=t)
        high = _view(pid=999, uuid="abc", last_activity=t)
        result = _dedup_views_by_session_uuid([low, high])
        assert len(result) == 1
        assert result[0].pid == 999

    def test_order_preserved_with_first_occurrence_slot(self):
        t_old = datetime(2026, 5, 14, 10, 0, tzinfo=timezone.utc)
        t_new = datetime(2026, 5, 14, 11, 0, tzinfo=timezone.utc)
        # Order: A, B(old), C, B(new) — B's winner inherits A→B slot.
        a = _view(pid=1, uuid="A", last_activity=t_new)
        b_old = _view(pid=2, uuid="B", last_activity=t_old)
        c = _view(pid=3, uuid="C", last_activity=t_new)
        b_new = _view(pid=4, uuid="B", last_activity=t_new)
        result = _dedup_views_by_session_uuid([a, b_old, c, b_new])
        assert [v.session_uuid for v in result] == ["A", "B", "C"]
        # The B slot now holds the newer pid.
        assert result[1].pid == 4


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
    state_reader: "FakeStateReader | None" = None,
    metadata_provider: "FakeMetadataProvider | None" = None,
    usage_registry: "FakeUsageRegistry | None" = None,
    names_store: "FakeNamesStore | None" = None,
    get_state_version=None,
) -> tuple[Snapshotter, list[WorldSnapshot]]:
    """Build a Snapshotter wired with fakes; return (snapshotter,
    received_snapshots_list). The publish callback appends to the
    list so tests assert on what got published.

    ``get_state_version`` defaults to None → the Snapshotter's own
    default (-1 sentinel) is used, disabling the incremental cache.
    Pass a callable returning a real version to exercise caching."""
    received: list[WorldSnapshot] = []
    kwargs = {}
    if get_state_version is not None:
        kwargs["get_state_version"] = get_state_version
    snap = Snapshotter(
        session_source=FakeSessionSource(sessions or []),
        state_reader=state_reader or FakeStateReader(),
        metadata_provider=metadata_provider or FakeMetadataProvider(),
        usage_registry=usage_registry or FakeUsageRegistry(today_cost=today_cost),
        names_store=names_store or FakeNamesStore(),
        get_quota=lambda: None,
        get_available_providers=lambda: [],
        get_selected_provider=lambda: None,
        publish=publish or received.append,
        debounce_window_s=debounce_window_s,
        throttle_first_window_s=throttle_first_window_s,
        **kwargs,
    )
    return snap, received


class TestSnapshotterBuildNow:
    """build_now is the synchronous path used at boot + in tests."""

    def test_build_now_returns_empty_for_no_sessions(self):
        snap, _ = _make_snapshotter()
        result = snap.build_now()
        assert result.session_groups == ()

    def test_build_now_includes_sessions(self):
        s1 = _session(pid=1, cwd="/a")
        s2 = _session(pid=2, cwd="/b")
        snap, _ = _make_snapshotter(sessions=[s1, s2])
        result = snap.build_now()
        # Default group_sessions produces one singleton group per view.
        flat = [v for g in result.session_groups for v in g.views]
        assert len(flat) == 2
        assert {v.pid for v in flat} == {1, 2}

    def test_build_now_carries_today_cost(self):
        snap, _ = _make_snapshotter(today_cost=42.5)
        assert snap.build_now().today_cost_usd == 42.5

    def test_build_now_does_not_publish(self):
        snap, received = _make_snapshotter()
        snap.build_now()
        assert received == []  # build_now is synchronous, returns; no push

    def test_two_pids_sharing_session_uuid_collapse_to_one_view(self):
        """Regression: two `claude --resume <uuid>` processes write the
        same sessionId into their respective pid.json. The composer
        resolves both to the same session_uuid; the snapshot pipeline
        must collapse them so the UI renders one row per session, not
        per pid.

        Timestamps must be fresh (well within _STALE_LIVE_VIEW_S = 30 min)
        so the staleness filter (2026-05-16) doesn't drop both views
        before dedup runs.
        """
        from datetime import timedelta
        now = datetime.now(timezone.utc)
        t_old = now - timedelta(minutes=5)
        t_new = now - timedelta(minutes=2)
        cwd = "/Users/x/proj"
        older = _session(pid=100, cwd=cwd, last_activity=t_old)
        newer = _session(pid=200, cwd=cwd, last_activity=t_new)
        # Both pid.json files point at the same sessionId.
        state = FakeStateReader({
            100: {"sessionId": "shared-uuid"},
            200: {"sessionId": "shared-uuid"},
        })
        snap, _ = _make_snapshotter(
            sessions=[older, newer],
            state_reader=state,
        )
        result = snap.build_now()
        flat = [v for g in result.session_groups for v in g.views]
        assert len(flat) == 1
        # Newer pid (more recent last_activity) wins.
        assert flat[0].pid == 200
        assert flat[0].session_uuid == "shared-uuid"

    def test_os_alive_process_always_shows_regardless_of_age(self):
        """Policy 2026-05-16 (revised): an OS-confirmed alive
        claude.exe (pid > 0 from ProcessScanner) appears in the live
        list regardless of last_activity age.

        Why: ``session.last_activity`` is populated with the process
        create_time, which can be days old for a long-running session.
        Filtering by age would hide the user's current conversation
        whenever they pause between turns. The user decides what to
        click; UI shows phase via the activity-dot glyph."""
        from datetime import timedelta
        now = datetime.now(timezone.utc)
        fresh = _session(pid=10, cwd="/a", last_activity=now - timedelta(minutes=5))
        old = _session(pid=20, cwd="/b", last_activity=now - timedelta(hours=24))
        snap, _ = _make_snapshotter(sessions=[fresh, old])
        result = snap.build_now()
        flat = [v for g in result.session_groups for v in g.views]
        assert {v.pid for v in flat} == {10, 20}

    def test_stale_hook_placeholder_dropped_from_live_list(self):
        """Hook bridge upserted a placeholder (pid=PLACEHOLDER_PID) for
        a uuid the scanner never confirmed AND no hook live state
        exists (state machine ENDED/empty). Drop so it doesn't appear
        as an unclickable row.

        Bug fix 2026-05-16: placeholders with no scanner backup and
        no live hook activity were rendering as ghost rows because
        _build_snapshot included every SessionRegistry entry."""
        from claude_island.core.session_registry import PLACEHOLDER_PID
        ghost = _session(
            pid=PLACEHOLDER_PID, cwd="/x",
            last_activity=datetime.now(timezone.utc),
        )
        snap, _ = _make_snapshotter(sessions=[ghost])
        # No live_state_reader injection → state machine has no entry
        # for this session → view degrades to phase=IDLE without prompt.
        result = snap.build_now()
        flat = [v for g in result.session_groups for v in g.views]
        assert flat == [], (
            f"ghost placeholder leaked into live list: "
            f"pids={[v.pid for v in flat]}"
        )

    def test_current_conversation_kept_when_hook_recent_jsonl_stale(self):
        """Regression 2026-05-16: between turns the live conversation
        sits at phase=IDLE and JSONL may not have been appended for
        minutes (user reading the reply). But the hook stream's
        last_hook_at is fresh — every PreToolUse, PostToolUse,
        Notification, Stop bumps it. compose_session_view must fold
        last_hook_at into last_activity so the staleness filter does
        NOT drop the live conversation."""
        from datetime import timedelta
        from claude_island.core.hook_events import SessionLiveState
        from claude_island.core.session_phase import SessionPhase
        now = datetime.now(timezone.utc)
        # JSONL hasn't been updated in 2 hours (the user has been
        # reading). Scanner sees this as last_activity.
        session_jsonl_stale = _session(
            pid=42, cwd="/proj",
            last_activity=now - timedelta(hours=2),
        )
        state = FakeStateReader({42: {"sessionId": "uuid-live"}})
        # Hook live state: phase IDLE (between turns) but last_hook_at
        # is fresh (PreToolUse just fired).
        live_states = {
            "uuid-live": SessionLiveState(
                session_uuid="uuid-live",
                phase=SessionPhase.IDLE,
                cwd=Path("/proj"),
                started_at=now - timedelta(hours=2),
                last_hook_at=now - timedelta(seconds=10),
            ),
        }
        snap, _ = _make_snapshotter(
            sessions=[session_jsonl_stale],
            state_reader=state,
        )
        snap._live_state_reader = lambda uuid: live_states.get(uuid)
        result = snap.build_now()
        flat = [v for g in result.session_groups for v in g.views]
        assert {v.pid for v in flat} == {42}, (
            f"current conversation lost: pids={[v.pid for v in flat]}"
        )

    def test_view_with_active_hook_phase_kept_regardless_of_age(self):
        """Hook live state is authoritative — if SessionStateMachine
        reports a non-IDLE phase, the view stays in the live list even
        if JSONL's last_activity is stale. Covers the case where the
        user is reading Claude output without typing, so JSONL hasn't
        been written, but the hook stream shows TOOL_USE."""
        from datetime import timedelta
        from claude_island.core.hook_events import SessionLiveState
        from claude_island.core.session_phase import SessionPhase
        now = datetime.now(timezone.utc)
        stale_but_live = _session(
            pid=30, cwd="/c", last_activity=now - timedelta(hours=24),
        )
        # Match pid.json's sessionId so composer assigns this uuid.
        state = FakeStateReader({30: {"sessionId": "uuid-active"}})
        # Provide a live state with TOOL_USE phase.
        live_states = {
            "uuid-active": SessionLiveState(
                session_uuid="uuid-active",
                phase=SessionPhase.TOOL_USE,
                cwd=Path("/c"),
                started_at=now,
                last_hook_at=now,
                current_tool="Bash",
            ),
        }
        snap, _ = _make_snapshotter(
            sessions=[stale_but_live],
            state_reader=state,
        )
        # Inject the live state reader directly.
        snap._live_state_reader = lambda uuid: live_states.get(uuid)
        result = snap.build_now()
        flat = [v for g in result.session_groups for v in g.views]
        assert {v.pid for v in flat} == {30}


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
            assert received[0].session_groups == ()  # degraded to empty

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

    def test_publish_runs_on_worker_thread_not_main(self):
        """Tightens the threading contract: ``publish`` is invoked on
        the EventLoopScheduler's worker thread, NOT on the caller's
        thread. The wiring layer relies on this — its WorldMarshaler
        emits a Qt Signal with QueuedConnection; if publish ever ran
        on the main thread by accident the QueuedConnection becomes
        a same-thread DirectConnection and the marshaling guarantee
        silently disappears.

        Asserts the ident captured by publish differs from the test's
        thread ident. Fails fast if a future refactor moves the build
        path to a synchronous code path."""
        main_id = threading.get_ident()
        ident_holder: list[int] = []

        def capture(_snap):
            ident_holder.append(threading.get_ident())

        snap, _ = _make_snapshotter(publish=capture, debounce_window_s=0.05)
        snap.start()
        try:
            snap.wake()
            time.sleep(0.2)
            assert len(ident_holder) == 1
            assert ident_holder[0] != main_id, (
                f"publish ran on main thread (id={main_id}) — "
                f"WorldMarshaler's QueuedConnection guarantee broken"
            )
        finally:
            snap.stop()

    def test_stop_waits_for_in_flight_build(self):
        """``stop()`` must not return while a build is mid-iteration.
        Acquiring the build lock inside ``_do_build`` and inside
        ``stop`` serialises them; this test verifies ``stop`` blocks
        until the build is done by injecting a slow build."""
        import threading as _threading

        build_in_progress = _threading.Event()
        let_build_finish = _threading.Event()
        build_finished = _threading.Event()

        class SlowSource:
            @property
            def sessions(self):
                build_in_progress.set()
                # Block until the test releases us — simulates a slow
                # IO-bound build (e.g. SQLite query, network fetch).
                let_build_finish.wait(timeout=2.0)
                return []

        def publish(snap):
            build_finished.set()

        from claude_island.core.snapshot import Snapshotter
        snap = Snapshotter(
            session_source=SlowSource(),
            state_reader=FakeStateReader(),
            metadata_provider=FakeMetadataProvider(),
            usage_registry=FakeUsageRegistry(),
            names_store=FakeNamesStore(),
            get_quota=lambda: None,
            get_available_providers=lambda: [],
            get_selected_provider=lambda: None,
            publish=publish,
            debounce_window_s=0.0,
            throttle_first_window_s=0.0,
        )
        snap.start()
        snap.wake()
        # Wait until build has started (worker thread blocked inside
        # SlowSource.sessions).
        assert build_in_progress.wait(timeout=2.0), "build never started"

        # Run stop in a thread; it should block on the build_lock.
        stop_returned = _threading.Event()

        def call_stop():
            snap.stop()
            stop_returned.set()

        stop_thread = _threading.Thread(target=call_stop)
        stop_thread.start()

        # Stop should NOT return while build is in-flight.
        assert not stop_returned.wait(timeout=0.2), (
            "stop() returned while build was still mid-iteration — "
            "_build_lock not acquired by either side"
        )

        # Release the build; stop should now complete.
        let_build_finish.set()
        assert stop_returned.wait(timeout=2.0), "stop() never returned"
        # And the build's publish call did happen (proving the build
        # ran to completion before stop's dispose ran).
        assert build_finished.is_set()

        stop_thread.join(timeout=1.0)


class TestEndToEndPublishToRender:
    """End-to-end: Snapshotter(publish=marshaler.snap_ready.emit) →
    WorldMarshaler → world.push → render. Verifies the production
    threading chain produces a render call on the Qt main thread.

    Lives in test_snapshotter.py rather than test_world_marshaler.py
    because the entry point is Snapshotter — verifying the whole
    pipeline at once catches integration bugs neither layer's unit
    tests would surface."""

    def test_full_chain_renders_on_qt_main_thread(self, qtbot):
        import os
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from claude_island.core.snapshot import world
        from claude_island.ui.world_marshaler import WorldMarshaler

        marshaler = WorldMarshaler()
        main_id = threading.get_ident()
        idents: list[int] = []
        world.observable().subscribe(
            lambda _snap: idents.append(threading.get_ident())
        )
        baseline = len(idents)  # initial empty replay landed on main

        snap, _ = _make_snapshotter(
            publish=marshaler.snap_ready.emit,
            debounce_window_s=0.05,
        )
        snap.start()
        try:
            snap.wake()
            qtbot.wait(300)  # allow build + queued render
            new_renders = idents[baseline:]
            assert len(new_renders) >= 1, (
                f"render never reached the world subscriber; "
                f"baseline={baseline}, idents={idents}"
            )
            for ident in new_renders:
                assert ident == main_id, (
                    f"render landed on thread {ident}, expected main {main_id}"
                )
        finally:
            snap.stop()


# ---------------------------------------------------------------------------
# Phase 3 (resume-offline): 3-source reconcile (live + dormant + intent)
# ---------------------------------------------------------------------------

class _FakeDormantSource:
    def __init__(self, sessions):
        self._sessions = list(sessions)

    @property
    def sessions(self):
        return list(self._sessions)


class _FakeLaunchIntent:
    """Records reconcile arguments + lets tests pre-load intents."""

    def __init__(self, intents=None):
        self._intents = list(intents or [])
        self.last_reconcile_args = None

    def reconcile(self, *, live_uuids, now):
        self.last_reconcile_args = {"live_uuids": set(live_uuids), "now": now}
        # Simulate the real registry's "drop on upgrade" rule so the
        # snapshot test mirrors production semantics.
        self._intents = [i for i in self._intents if i.session_uuid not in live_uuids]

    def snapshot(self):
        return tuple(self._intents)


def _dormant(uuid: str, last_activity: datetime | None = None):
    from claude_island.core.models import DormantSession
    return DormantSession(
        session_uuid=uuid,
        cwd=Path("/tmp/proj"),
        name=None,
        last_prompt=None,
        last_activity=last_activity or datetime.now(timezone.utc),
        started_at=None,
        permission_mode=None,
        git_branch=None,
        cost_usd=0.0,
        turn_count=0,
    )


def _intent(uuid: str):
    from claude_island.core.launch_intent import LaunchIntent
    return LaunchIntent(
        session_uuid=uuid,
        cwd=Path("/tmp/proj"),
        flags=(),
        terminal_name="windows-terminal",
        terminal_pid=1234,
        requested_at=datetime.now(timezone.utc),
    )


def _make_snapshotter_with_offline(
    *,
    live_sessions=None,
    dormant_sessions=None,
    intents=None,
):
    """Same as _make_snapshotter but wires dormant_source + launch_intent."""
    received: list[WorldSnapshot] = []
    snap = Snapshotter(
        session_source=FakeSessionSource(live_sessions or []),
        state_reader=FakeStateReader(),
        metadata_provider=FakeMetadataProvider(),
        usage_registry=FakeUsageRegistry(),
        names_store=FakeNamesStore(),
        get_quota=lambda: None,
        get_available_providers=lambda: [],
        get_selected_provider=lambda: None,
        publish=received.append,
        dormant_source=_FakeDormantSource(dormant_sessions or []),
        launch_intent=_FakeLaunchIntent(intents or []),
        debounce_window_s=0.05,
        throttle_first_window_s=0.0,
    )
    return snap, received


class TestSnapshotterReconcile:
    """The 3-source merge inside _build_snapshot."""

    def test_dormant_only_no_live_no_intent(self):
        d = _dormant("u-dorm")
        snap, _ = _make_snapshotter_with_offline(dormant_sessions=[d])
        result = snap.build_now()
        assert {x.session_uuid for x in result.dormant_sessions} == {"u-dorm"}
        assert result.launching_sessions == ()

    def test_live_uuid_filtered_out_of_dormant(self):
        """A session that's both 'live' (process scanner saw it) and
        'dormant' (transcript on disk) appears only in live — never in
        the dormant tuple."""
        live_session = _session(pid=1, cwd="/tmp/p", uuid="u-overlap")
        # State reader returning the same uuid binds it onto the SessionView.
        state_reader = FakeStateReader(table={1: {"sessionId": "u-overlap"}})
        d = _dormant("u-overlap")
        received: list[WorldSnapshot] = []
        snap = Snapshotter(
            session_source=FakeSessionSource([live_session]),
            state_reader=state_reader,
            metadata_provider=FakeMetadataProvider(),
            usage_registry=FakeUsageRegistry(),
            names_store=FakeNamesStore(),
            get_quota=lambda: None,
            get_available_providers=lambda: [],
            get_selected_provider=lambda: None,
            publish=received.append,
            dormant_source=_FakeDormantSource([d]),
            launch_intent=_FakeLaunchIntent([]),
        )
        result = snap.build_now()
        assert result.dormant_sessions == ()  # filtered out
        live_uuids = {v.session_uuid for g in result.session_groups for v in g.views}
        assert "u-overlap" in live_uuids

    def test_launching_uuid_filtered_out_of_dormant(self):
        """An intent in flight should suppress the dormant entry — UI
        renders it in the launching section instead."""
        d = _dormant("u-launching")
        i = _intent("u-launching")
        snap, _ = _make_snapshotter_with_offline(
            dormant_sessions=[d], intents=[i],
        )
        result = snap.build_now()
        assert result.dormant_sessions == ()  # suppressed by intent
        assert {x.session_uuid for x in result.launching_sessions} == {"u-launching"}

    def test_intent_dropped_when_uuid_appears_live(self):
        """The "upgrade" path: live_uuids includes the intent uuid →
        FakeLaunchIntent drops it during reconcile → not in launching."""
        live_session = _session(pid=1, cwd="/tmp/p", uuid="u-live")
        state_reader = FakeStateReader(table={1: {"sessionId": "u-live"}})
        i = _intent("u-live")
        received: list[WorldSnapshot] = []
        snap = Snapshotter(
            session_source=FakeSessionSource([live_session]),
            state_reader=state_reader,
            metadata_provider=FakeMetadataProvider(),
            usage_registry=FakeUsageRegistry(),
            names_store=FakeNamesStore(),
            get_quota=lambda: None,
            get_available_providers=lambda: [],
            get_selected_provider=lambda: None,
            publish=received.append,
            dormant_source=_FakeDormantSource([]),
            launch_intent=_FakeLaunchIntent([i]),
        )
        result = snap.build_now()
        # FakeLaunchIntent.reconcile drops the intent when uuid in live_uuids.
        assert result.launching_sessions == ()

    def test_no_dormant_source_yields_empty_tuple(self):
        """When the resume-offline feature is disabled (production not
        wired yet), dormant_sessions / launching_sessions stay ()."""
        snap, _ = _make_snapshotter()
        result = snap.build_now()
        assert result.dormant_sessions == ()
        assert result.launching_sessions == ()


# ---------------------------------------------------------------------------
# Incremental SessionView cache (2026-05-26)
# ---------------------------------------------------------------------------

class TestSnapshotterIncrementalCache:
    """The per-uuid + whole-list SessionView cache: a build that sees
    identical source data (same session fingerprint + unchanged
    meta/record/state versions) must reuse cached views instead of
    recomposing. A bump in ANY source version invalidates the cache.

    compose_session_view is the expensive step; we spy on its call count
    to distinguish a cache hit (no recompose) from a miss (recompose)."""

    def _spy_compose(self):
        import claude_island.core.snapshot as snapshot_mod
        return mock.patch.object(
            snapshot_mod,
            "compose_session_view",
            wraps=snapshot_mod.compose_session_view,
        )

    def _cached_snapshotter(self, *, state_version: int = 0):
        """A Snapshotter with one session and all source-version tracking
        wired (so the cache is enabled). Returns the snapshotter plus the
        mutable version sources so a test can bump them."""
        s = _session(pid=1, cwd="/a")
        md = FakeMetadataProvider()
        md.meta_version = 0  # read via getattr in _build_snapshot
        ur = FakeUsageRegistry()
        ur.record_version = 0
        holder = {"state": state_version}
        snap, _ = _make_snapshotter(
            sessions=[s],
            metadata_provider=md,
            usage_registry=ur,
            get_state_version=lambda: holder["state"],
        )
        return snap, md, ur, holder

    def test_second_build_reuses_cache_when_nothing_changed(self):
        snap, md, ur, holder = self._cached_snapshotter()
        with self._spy_compose() as spy:
            snap.build_now()
            n1 = spy.call_count
            assert n1 == 1  # first build composes the single session once
            snap.build_now()
            assert spy.call_count == n1, "cache hit must skip recompose"

    def test_meta_version_bump_invalidates_cache(self):
        snap, md, ur, holder = self._cached_snapshotter()
        with self._spy_compose() as spy:
            snap.build_now()
            n1 = spy.call_count
            md.meta_version += 1
            snap.build_now()
            assert spy.call_count > n1, "meta_version bump must recompose"

    def test_record_version_bump_invalidates_cache(self):
        snap, md, ur, holder = self._cached_snapshotter()
        with self._spy_compose() as spy:
            snap.build_now()
            n1 = spy.call_count
            ur.record_version += 1
            snap.build_now()
            assert spy.call_count > n1, "record_version bump must recompose"

    def test_state_version_bump_invalidates_cache(self):
        snap, md, ur, holder = self._cached_snapshotter()
        with self._spy_compose() as spy:
            snap.build_now()
            n1 = spy.call_count
            holder["state"] += 1
            snap.build_now()
            assert spy.call_count > n1, "state_version bump must recompose"

    def test_sentinel_state_version_disables_cache(self):
        """When get_state_version returns -1 (state tracking unavailable),
        the cache is bypassed entirely — every build recomposes. This
        preserves legacy always-recompute behaviour for callers that
        never wire a state machine."""
        s = _session(pid=1, cwd="/a")
        snap, _ = _make_snapshotter(sessions=[s], get_state_version=lambda: -1)
        with self._spy_compose() as spy:
            snap.build_now()
            n1 = spy.call_count
            assert n1 == 1
            snap.build_now()
            assert spy.call_count == 2 * n1, "sentinel must disable caching"

    def test_dead_session_evicted_from_view_cache(self):
        """A pid that disappears between builds must have its cache entry
        evicted — the per-identity cache must not grow unbounded over a
        long-running process."""
        s1 = _session(pid=1, cwd="/a")
        s2 = _session(pid=2, cwd="/b")
        source = FakeSessionSource([s1, s2])
        md = FakeMetadataProvider()
        md.meta_version = 0
        ur = FakeUsageRegistry()
        ur.record_version = 0
        snap = Snapshotter(
            session_source=source,
            state_reader=FakeStateReader(),
            metadata_provider=md,
            usage_registry=ur,
            names_store=FakeNamesStore(),
            get_quota=lambda: None,
            get_available_providers=lambda: [],
            get_selected_provider=lambda: None,
            publish=lambda _s: None,
            debounce_window_s=0.05,
            throttle_first_window_s=0.0,
            get_state_version=lambda: 0,
        )
        snap.build_now()
        assert len(snap._view_cache) == 2

        # pid=2 vanishes; a version bump forces the miss path (which is
        # where eviction runs).
        source._sessions = [s1]
        md.meta_version = 1
        snap.build_now()
        assert len(snap._view_cache) == 1
        assert all(k[1] == 1 for k in snap._view_cache)  # only pid=1 remains

    def test_rename_invalidates_cache(self):
        """Renaming a session via names_store changes view.name; the cache
        must invalidate even though meta/record/state versions and the
        session fingerprint are unchanged.

        Regression (cache-001): names_store is a FOURTH compose input not
        covered by any version counter. Before the fix, a rename hit the
        whole-list cache and the UI kept rendering the OLD name. Production
        rename (set_session_name) bumps names_store.names_version; this test
        mirrors that and asserts the new name surfaces."""
        s = _session(pid=1, cwd="/a", uuid="u1")
        md = FakeMetadataProvider(); md.meta_version = 0
        ur = FakeUsageRegistry(); ur.record_version = 0
        names = FakeNamesStore({"u1": "old"})
        names.names_version = 0
        snap, _ = _make_snapshotter(
            sessions=[s], metadata_provider=md, usage_registry=ur,
            names_store=names, get_state_version=lambda: 0,
        )
        r1 = snap.build_now()
        name1 = [v for g in r1.session_groups for v in g.views][0].name
        assert name1 == "old"

        # Rename: change the store + bump its version (mirrors production).
        names.names["u1"] = "new"
        names.names_version += 1
        r2 = snap.build_now()
        name2 = [v for g in r2.session_groups for v in g.views][0].name
        assert name2 == "new", "rename must invalidate the SessionView cache"
