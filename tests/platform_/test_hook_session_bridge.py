"""Tests for HookSessionBridge — F-1 race + F-2 tombstone reconcile.

T9.x family from Detail Design v2 §7.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from claude_island.core.hook_events import (
    PromptSubmitted,
    SessionStarted,
    ToolStarted,
)
from claude_island.core.models import Session
from claude_island.core.session_phase import SessionPhase
from claude_island.core.session_registry import PLACEHOLDER_PID, SessionRegistry
from claude_island.core.session_state_machine import SessionStateMachine
from claude_island.platform_.hook_session_bridge import HookSessionBridge


_NOW = datetime(2026, 5, 13, 12, 0, 0, tzinfo=timezone.utc)
_CWD = Path("D:/coding projects/foo")


def _make_pair():
    """Build (registry, state_machine, bridge) wired together."""
    reg = SessionRegistry()
    sm = SessionStateMachine()
    bridge = HookSessionBridge(registry=reg, state_machine=sm)
    return reg, sm, bridge


# ---------------------------------------------------------------------------
# T9.1 — hook first → bridge upserts placeholder before scanner runs
# ---------------------------------------------------------------------------


def test_t9_1_hook_first_places_placeholder(_=None):
    reg, sm, bridge = _make_pair()
    sm.apply(SessionStarted(
        session_uuid="abc",
        cwd=_CWD,
        started_at=_NOW,
        source="startup",
        transcript_path=None,
        at=_NOW,
    ))
    # Bridge must have synchronously upserted a placeholder
    sessions = reg.sessions
    assert len(sessions) == 1
    assert sessions[0].session_uuid == "abc"
    assert sessions[0].pid == PLACEHOLDER_PID
    assert sessions[0].project_path == _CWD


# ---------------------------------------------------------------------------
# T9.2 — scanner arrives with real pid → placeholder gets uuid grafted
# ---------------------------------------------------------------------------


def test_t9_2_scanner_grafts_uuid_onto_real_pid():
    reg, sm, bridge = _make_pair()
    # Hook first → placeholder
    sm.apply(SessionStarted("abc", _CWD, _NOW, "startup", None, _NOW))
    assert reg.sessions[0].pid == PLACEHOLDER_PID

    # Scanner sees pid=12345 in the same cwd with no uuid
    scanner_session = Session(
        pid=12345,
        project_path=_CWD,
        session_uuid="",   # scanner doesn't know uuid
        last_activity=_NOW,
    )
    reg.update([scanner_session])

    # Merge result: real pid + uuid from placeholder
    sessions = reg.sessions
    assert len(sessions) == 1
    assert sessions[0].pid == 12345
    assert sessions[0].session_uuid == "abc"


# ---------------------------------------------------------------------------
# T9.3 — scanner sees a pid for an unknown uuid: don't tombstone
# (a hook hasn't been installed for this session yet)
# ---------------------------------------------------------------------------


def test_t9_3_scanner_only_session_not_tombstoned():
    """A claude session that started before island ran exists in scanner
    output without uuid. state_machine knows nothing about it; the bridge
    must not invent or remove anything for it."""
    reg, sm, bridge = _make_pair()
    reg.update([Session(
        pid=42,
        project_path=Path("/old-session"),
        session_uuid="",
        last_activity=_NOW,
    )])
    # state_machine empty → bridge does nothing
    assert sm.snapshot() == {}
    # Registry still has the scanner session
    assert reg.sessions[0].pid == 42


# ---------------------------------------------------------------------------
# T9.4 — single scanner miss doesn't tombstone
# ---------------------------------------------------------------------------


def test_t9_4_single_miss_no_tombstone():
    reg, sm, bridge = _make_pair()
    sm.apply(SessionStarted("abc", _CWD, _NOW, "startup", None, _NOW))
    # Scanner tick 1: empty
    reg.update([])
    # Bridge incremented miss counter to 1 but didn't tombstone
    assert sm.read("abc").phase != SessionPhase.ENDED


# ---------------------------------------------------------------------------
# T9.5 — MISS_THRESHOLD consecutive misses → tombstone
# ---------------------------------------------------------------------------


def test_t9_5_threshold_misses_tombstones():
    reg, sm, bridge = _make_pair()
    sm.apply(SessionStarted("abc", _CWD, _NOW, "startup", None, _NOW))
    # Bridge added placeholder. Now scanner sees nothing — but the
    # registry currently HAS the placeholder, so update([]) returns a
    # different list. The placeholder is kept by _merge_with_placeholders.
    # So scanner-misses must be counted by absence-from-incoming, not
    # absence-from-registry. The bridge looks at `sessions` (the merged
    # list) which still has the placeholder... let me think.
    #
    # Actually the bridge's _on_scanner_update gets the EMITTED list
    # (post-merge). The merged list still has the placeholder. So
    # `seen_uuids` will include 'abc', miss won't increment.
    #
    # This means the only way state_machine entries get tombstoned is
    # if the REGISTRY no longer has them — i.e., the bridge upserts
    # placeholder, scanner sees real pid, scanner stops seeing it,
    # placeholder long gone, registry shows nothing → bridge tombstones.
    #
    # Let's set up that scenario:

    # Scanner tick 1: real pid takes over the placeholder
    reg.update([Session(pid=42, project_path=_CWD, session_uuid="", last_activity=_NOW)])
    # Now registry has Session(pid=42, uuid="abc", ...). state_machine still
    # has uuid="abc". sm-bridge sees uuid in registry → no miss.

    # Scanner tick 2: pid gone (claude exited without firing SessionEnd)
    reg.update([])
    # miss count for "abc" = 1
    assert sm.read("abc").phase != SessionPhase.ENDED

    # Scanner tick 3: still gone
    reg.update([])
    # miss count = 2 = MISS_THRESHOLD → tombstone
    assert sm.read("abc").phase == SessionPhase.ENDED


# ---------------------------------------------------------------------------
# T9.6 — SessionEnded already set; scanner still sees pid:
# bridge does NOT resurrect; registry-side cleanup happens later naturally
# ---------------------------------------------------------------------------


def test_t9_6_ended_state_not_resurrected():
    from claude_island.core.hook_events import SessionEnded
    reg, sm, bridge = _make_pair()
    sm.apply(SessionStarted("abc", _CWD, _NOW, "startup", None, _NOW))
    sm.apply(SessionEnded("abc", _NOW))
    # state ENDED. Now another hook event arrives later for same uuid —
    # state machine drops the event (terminal state). Bridge sees the
    # state change emit (from SessionEnded) and should NOT re-upsert.
    sessions = reg.sessions
    # There IS a placeholder lingering from the SessionStarted emit;
    # that's expected. We test that NO new placeholder is added after
    # ENDED.
    snapshot_before = list(sessions)
    sm.live_state_changed.on_next({"abc"})  # re-emit
    snapshot_after = reg.sessions
    assert snapshot_after == snapshot_before


# ---------------------------------------------------------------------------
# Edge: bridge.stop() makes future state_machine emits not affect registry
# ---------------------------------------------------------------------------


def test_bridge_stop_disposes_subscriptions():
    reg, sm, bridge = _make_pair()
    bridge.stop()
    # After stop, applying a new event should NOT cause a placeholder
    sm.apply(SessionStarted("new", _CWD, _NOW, "startup", None, _NOW))
    assert reg.sessions == []


# ---------------------------------------------------------------------------
# Multi-uuid: independent tombstone counters
# ---------------------------------------------------------------------------


def test_independent_miss_counters_per_uuid():
    reg, sm, bridge = _make_pair()
    sm.apply(SessionStarted("u1", _CWD, _NOW, "startup", None, _NOW))
    sm.apply(SessionStarted("u2", Path("/other"), _NOW, "startup", None, _NOW))
    # Real pids picked up
    reg.update([
        Session(pid=1, project_path=_CWD, session_uuid="", last_activity=_NOW),
        Session(pid=2, project_path=Path("/other"), session_uuid="", last_activity=_NOW),
    ])
    # Now u1 disappears, u2 stays
    reg.update([Session(pid=2, project_path=Path("/other"), session_uuid="u2", last_activity=_NOW)])
    # u1 miss=1, u2 still seen
    assert sm.read("u1").phase != SessionPhase.ENDED
    assert sm.read("u2").phase != SessionPhase.ENDED

    reg.update([Session(pid=2, project_path=Path("/other"), session_uuid="u2", last_activity=_NOW)])
    # u1 miss=2 → tombstone; u2 still alive
    assert sm.read("u1").phase == SessionPhase.ENDED
    assert sm.read("u2").phase != SessionPhase.ENDED


# ---------------------------------------------------------------------------
# Hook-only session (no scanner): scanner empty + state machine has uuid
# → still tombstone after MISS_THRESHOLD, because placeholder is invisible
# to scanner
# ---------------------------------------------------------------------------


def test_placeholder_only_session_tombstones_after_threshold_misses():
    """Bug A' + A'' fix (2026-05-13): hook fires SessionStart but no
    matching claude.exe ever appears in scanner output. Placeholder
    must tombstone AND disappear from registry after MISS_THRESHOLD
    consecutive scanner ticks. Previously the bridge counted the
    placeholder itself as 'seen' (because sessions_changed re-emits
    the registry contents which include the placeholder), so
    miss_count never advanced."""
    reg, sm, bridge = _make_pair()
    sm.apply(SessionStarted("phantom", _CWD, _NOW, "startup", None, _NOW))
    assert any(s.session_uuid == "phantom" for s in reg.sessions)

    # Two scanner ticks both empty
    reg.update([])
    reg.update([])

    # State machine tombstoned the session
    assert sm.read("phantom").phase == SessionPhase.ENDED
    # Registry no longer shows the placeholder
    assert all(s.session_uuid != "phantom" for s in reg.sessions)


def test_placeholder_survives_when_scanner_sees_other_session_same_cwd():
    """Edge: two claude.exe in same cwd. Placeholder uuid is for one
    of them. Scanner sees the OTHER pid in the same cwd. Should we
    tombstone the placeholder?

    No — match-by-cwd lets us keep state_machine's uuid-keyed entry
    alive when the scanner has any same-cwd process. This is the
    'session started before island ran' resilience clause."""
    reg, sm, bridge = _make_pair()
    sm.apply(SessionStarted("hookless-twin", _CWD, _NOW, "startup", None, _NOW))
    # Scanner reports a DIFFERENT real claude.exe in the SAME cwd (no uuid)
    reg.update([Session(
        pid=999, project_path=_CWD,
        session_uuid="", last_activity=_NOW,
    )])
    reg.update([Session(
        pid=999, project_path=_CWD,
        session_uuid="", last_activity=_NOW,
    )])
    # state_machine STAYS active — scanner's same-cwd is taken as
    # evidence the broader session group is alive.
    assert sm.read("hookless-twin").phase != SessionPhase.ENDED


def test_orphan_tombstones_when_scanner_resolves_different_uuid_same_cwd():
    """Regression (2026-05-26): orphan placeholder in cwd X must
    tombstone when scanner sees a same-cwd process with a *different*
    resolved uuid. The cwd-match resilience clause only applies when
    scanner can't yet name the session it sees (uuid==''); a resolved
    uuid is proof of a *different* session and must not shield an
    unrelated orphan.

    Reproduction:
      1. Session A starts via hook → state_machine[A]=THINKING,
         registry has placeholder(pid=-1, uuid=A, cwd=X).
      2. Session A crashes without firing SessionEnded (kill -9 /
         hook chain drop) → state_machine still says THINKING.
      3. Independent session B starts in same cwd X with resolved
         uuid=B (via pid.json/host_pid). Scanner now reports
         Session(pid=42, uuid=B, cwd=X) each tick.
      4. Two consecutive ticks pass: orphan A must tombstone.

    Pre-fix: orphan A survived forever because seen_cwds contained X
    (from B's scanner entry) → miss_count for A never advanced →
    phantom row in the island showing whatever phase A was last in.
    """
    reg, sm, bridge = _make_pair()
    # Session A: hook-only orphan in cwd X.
    sm.apply(SessionStarted("orphan-A", _CWD, _NOW, "startup", None, _NOW))
    assert any(s.session_uuid == "orphan-A" for s in reg.sessions)

    # Session B: scanner sees it in same cwd with a RESOLVED uuid.
    # This is what happens once the registry merge has grafted B's
    # uuid onto the (cwd, pid) entry (via prior bridge upsert or
    # hook-driven host_pid path).
    reg.update([Session(
        pid=42, project_path=_CWD,
        session_uuid="real-B", last_activity=_NOW,
    )])
    reg.update([Session(
        pid=42, project_path=_CWD,
        session_uuid="real-B", last_activity=_NOW,
    )])

    # Orphan A must be tombstoned — scanner naming B is not proof
    # that A is alive.
    assert sm.read("orphan-A").phase == SessionPhase.ENDED
    # And the orphan placeholder must be gone from the registry.
    assert all(s.session_uuid != "orphan-A" for s in reg.sessions)


# ---------------------------------------------------------------------------
# Placeholder visible in registry before scanner ticks
# ---------------------------------------------------------------------------


def test_placeholder_visible_in_registry_immediately():
    """G1 latency target verification: between SessionStart hook and
    next snapshot build, the registry must contain the new session."""
    reg, sm, bridge = _make_pair()

    sm.apply(SessionStarted("immediate", _CWD, _NOW, "startup", None, _NOW))

    # Synchronous expectation: the bridge fires inside apply()'s on_next
    # emission, so by the time apply() returns the registry is updated.
    sessions = reg.sessions
    assert any(s.session_uuid == "immediate" for s in sessions)


# ---------------------------------------------------------------------------
# SessionRegistry placeholder merge — direct unit tests
# ---------------------------------------------------------------------------


def test_registry_upsert_inserts_when_uuid_unknown():
    reg = SessionRegistry()
    reg.upsert(Session(
        pid=PLACEHOLDER_PID, project_path=_CWD,
        session_uuid="x", last_activity=_NOW,
    ))
    assert len(reg.sessions) == 1
    assert reg.sessions[0].pid == PLACEHOLDER_PID


def test_registry_upsert_replaces_by_uuid():
    reg = SessionRegistry()
    reg.upsert(Session(
        pid=PLACEHOLDER_PID, project_path=_CWD,
        session_uuid="x", last_activity=_NOW,
    ))
    # Replace with real pid
    reg.upsert(Session(
        pid=100, project_path=_CWD,
        session_uuid="x", last_activity=_NOW,
    ))
    assert len(reg.sessions) == 1
    assert reg.sessions[0].pid == 100


def test_registry_upsert_emits_only_on_change():
    reg = SessionRegistry()
    received: list = []
    reg.sessions_changed.subscribe(received.append)
    same_session = Session(
        pid=PLACEHOLDER_PID, project_path=_CWD,
        session_uuid="x", last_activity=_NOW,
    )
    reg.upsert(same_session)
    reg.upsert(same_session)  # no-op
    assert len(received) == 1


def test_registry_update_merges_placeholder_with_scanner():
    reg = SessionRegistry()
    reg.upsert(Session(
        pid=PLACEHOLDER_PID, project_path=_CWD,
        session_uuid="x", last_activity=_NOW,
    ))
    reg.update([Session(
        pid=42, project_path=_CWD, session_uuid="", last_activity=_NOW,
    )])
    sessions = reg.sessions
    assert len(sessions) == 1
    assert sessions[0].pid == 42
    assert sessions[0].session_uuid == "x"


def test_registry_update_keeps_unmatched_placeholder():
    reg = SessionRegistry()
    reg.upsert(Session(
        pid=PLACEHOLDER_PID, project_path=_CWD,
        session_uuid="x", last_activity=_NOW,
    ))
    # Scanner sees a session in a DIFFERENT cwd
    reg.update([Session(
        pid=42, project_path=Path("/other"), session_uuid="", last_activity=_NOW,
    )])
    sessions = sorted(reg.sessions, key=lambda s: s.pid)
    assert len(sessions) == 2
    # Placeholder kept (pid < scanner pid)
    pids = [s.pid for s in sessions]
    assert PLACEHOLDER_PID in pids
    assert 42 in pids
