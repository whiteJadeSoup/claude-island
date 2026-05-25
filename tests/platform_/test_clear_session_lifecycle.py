"""Reproduce the "session disappears after /clear" bug.

User report 2026-05-25: a session that was THINKING in
``~/workProject/origin/made`` was /clear-ed. Result: the row vanished
from the live panel and BOTH the old uuid AND the new uuid appeared
in the Recents drawer. Expected: row stays live (same pid, fresh
session), only the old uuid moves to Recents.

These tests exercise the bridge + registry interaction across a
``/clear`` event sequence, then probe the snapshot pipeline's output
to verify the new uuid surfaces as a live view.

If the bridge/registry behavior is correct in isolation, the bug is
downstream (compose, filter, dispatcher, dedup).
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from claude_island.core.hook_events import (
    JumpTarget,
    SessionEnded,
    SessionStarted,
)
from claude_island.core.models import Session
from claude_island.core.session_phase import SessionPhase
from claude_island.core.session_registry import PLACEHOLDER_PID, SessionRegistry
from claude_island.core.session_state_machine import SessionStateMachine
from claude_island.platform_.hook_session_bridge import HookSessionBridge


_NOW = datetime(2026, 5, 25, 12, 0, 0, tzinfo=timezone.utc)
_CWD = Path("/Users/u/workProject/origin/made")
_HOST_PID = 12345  # claude.exe pid; stays the same across /clear


def _jt(host_pid: int = _HOST_PID) -> JumpTarget:
    """Build a macOS-shaped JumpTarget as hook.py would emit it."""
    return JumpTarget(
        terminal_app="iTerm.app",
        conhost_hwnd=0,
        host_pid=host_pid,
        wt_session_guid="",
        term_program="iTerm.app",
        iterm_session_id="abc-123",
        terminal_pid=999,
    )


def _make_triple():
    reg = SessionRegistry()
    sm = SessionStateMachine()
    bridge = HookSessionBridge(registry=reg, state_machine=sm)
    return reg, sm, bridge


# ---------------------------------------------------------------------------
# Establish baseline: pre-/clear, registry has one session under OLD_UUID
# ---------------------------------------------------------------------------


def _seed_active_session(reg, sm, uuid: str = "OLD_UUID") -> None:
    """Simulate the steady state before /clear: hook fired SessionStart
    with jt.host_pid set, bridge grafted the real pid, scanner has
    confirmed."""
    sm.apply(SessionStarted(
        session_uuid=uuid,
        cwd=_CWD,
        started_at=_NOW,
        source="startup",
        transcript_path=None,
        at=_NOW,
        jump_target=_jt(),
    ))
    # Scanner tick confirms pid=12345 in /made with empty uuid
    reg.update([Session(
        pid=_HOST_PID,
        project_path=_CWD,
        session_uuid="",
        last_activity=_NOW,
    )])


def test_baseline_registry_has_one_session_per_uuid():
    """Sanity: after SessionStart + scanner tick, registry has
    Session(pid=12345, /made, OLD_UUID)."""
    reg, sm, bridge = _make_triple()
    _seed_active_session(reg, sm)
    sessions = reg.sessions
    assert len(sessions) == 1, f"expected 1 session, got {len(sessions)}: {sessions}"
    s = sessions[0]
    assert s.pid == _HOST_PID
    assert s.project_path == _CWD
    assert s.session_uuid == "OLD_UUID", (
        f"expected OLD_UUID after scanner-graft, got {s.session_uuid!r}"
    )


# ---------------------------------------------------------------------------
# The /clear scenario — the actual bug repro
# ---------------------------------------------------------------------------


def test_clear_keeps_session_in_registry_with_new_uuid():
    """After /clear (SessionEnd OLD + SessionStart NEW with same pid+cwd),
    the registry MUST contain a Session(pid=12345, /made, NEW_UUID).

    This is the contract the snapshot pipeline relies on to render a
    live row for the freshly-cleared session.
    """
    reg, sm, bridge = _make_triple()
    _seed_active_session(reg, sm, uuid="OLD_UUID")

    # /clear fires both hooks back-to-back (HookServer order: end then start).
    sm.apply(SessionEnded(session_uuid="OLD_UUID", at=_NOW))
    sm.apply(SessionStarted(
        session_uuid="NEW_UUID",
        cwd=_CWD,
        started_at=_NOW,
        source="clear",
        transcript_path=None,
        at=_NOW,
        jump_target=_jt(),  # same host_pid=12345
    ))

    sessions = reg.sessions
    # Registry should have exactly one entry: the cleared session with
    # the new uuid + the real pid.
    by_uuid = {s.session_uuid: s for s in sessions}
    assert "NEW_UUID" in by_uuid, (
        f"NEW_UUID missing from registry. sessions={sessions}"
    )
    new_entry = by_uuid["NEW_UUID"]
    assert new_entry.pid == _HOST_PID, (
        f"NEW_UUID's pid={new_entry.pid}, expected {_HOST_PID}"
    )
    assert new_entry.project_path == _CWD


def test_clear_then_next_scanner_tick_preserves_new_uuid():
    """After /clear AND the next scanner tick, the registry must STILL
    have a Session with NEW_UUID. The scanner-merge path is where a
    grafted uuid can be lost (no placeholder to re-graft)."""
    reg, sm, bridge = _make_triple()
    _seed_active_session(reg, sm, uuid="OLD_UUID")

    sm.apply(SessionEnded(session_uuid="OLD_UUID", at=_NOW))
    sm.apply(SessionStarted(
        session_uuid="NEW_UUID",
        cwd=_CWD,
        started_at=_NOW,
        source="clear",
        transcript_path=None,
        at=_NOW,
        jump_target=_jt(),
    ))

    # Next scanner tick — claude.exe still alive at the same pid.
    reg.update([Session(
        pid=_HOST_PID,
        project_path=_CWD,
        session_uuid="",
        last_activity=_NOW,
    )])

    sessions = reg.sessions
    uuids = {s.session_uuid for s in sessions if s.session_uuid}
    assert "NEW_UUID" in uuids, (
        f"NEW_UUID lost after scanner tick. registry={sessions}"
    )
    # Also: the entry carrying NEW_UUID must have the real pid (so
    # _filter_stale_views' pid>0 check keeps it).
    new_entry = next(s for s in sessions if s.session_uuid == "NEW_UUID")
    assert new_entry.pid == _HOST_PID, (
        f"NEW_UUID entry has pid={new_entry.pid}, expected {_HOST_PID}"
    )


def test_clear_does_not_tombstone_old_uuid_via_miss_counter():
    """A subtle failure mode: if the bridge mis-counts OLD_UUID as
    'missed by scanner' even though scanner still sees the SAME cwd
    (now carrying NEW_UUID), it would tombstone OLD_UUID AND remove
    by uuid from the registry — wiping the entry. The bridge's
    'seen' check uses `live.cwd in seen_cwds` to avoid this.

    Even if the test passes today, regressions in the cwd-seen check
    would surface here.
    """
    reg, sm, bridge = _make_triple()
    _seed_active_session(reg, sm, uuid="OLD_UUID")

    sm.apply(SessionEnded(session_uuid="OLD_UUID", at=_NOW))
    sm.apply(SessionStarted(
        session_uuid="NEW_UUID",
        cwd=_CWD,
        started_at=_NOW,
        source="clear",
        transcript_path=None,
        at=_NOW,
        jump_target=_jt(),
    ))

    # Two scanner ticks (MISS_THRESHOLD=2), same pid + cwd, empty uuid.
    for _ in range(2):
        reg.update([Session(
            pid=_HOST_PID, project_path=_CWD,
            session_uuid="", last_activity=_NOW,
        )])

    # OLD_UUID is ENDED — bridge should NOT increment miss counter for it.
    # NEW_UUID is IDLE — scanner sees the cwd, so seen=True, no miss.
    new_state = sm.read("NEW_UUID")
    assert new_state is not None, "NEW_UUID lost from state machine"
    assert new_state.phase != SessionPhase.ENDED, (
        f"NEW_UUID was tombstoned (phase={new_state.phase})"
    )
