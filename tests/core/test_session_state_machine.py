"""Tests for SessionStateMachine — the hook-event reducer.

T1.x family from Detail Design v2 §7. Each transition gets its own test
so a regression points at the exact rule that broke. Idempotency,
concurrency, and graceful invariant handling are tested last.
"""
from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from claude_island.core.hook_events import (
    CompactStarted,
    NotificationFired,
    PermissionRequested,
    PromptSubmitted,
    SessionEnded,
    SessionLiveState,
    SessionStarted,
    ToolFinished,
    ToolStarted,
    TurnCompleted,
)
from claude_island.core.session_phase import SessionPhase
from claude_island.core.session_state_machine import (
    SessionStateMachine,
    _state_eq,
    _transition,
)

_NOW = datetime(2026, 5, 13, 12, 0, 0, tzinfo=timezone.utc)
_CWD = Path("D:/coding projects/foo")
_UUID = "abc-uuid-1"


def _started_at(n: int = 0, *, uuid: str = _UUID, cwd: Path = _CWD) -> SessionStarted:
    """Build a SessionStarted event at _NOW + n seconds."""
    at = _NOW + timedelta(seconds=n)
    return SessionStarted(
        session_uuid=uuid, cwd=cwd, started_at=at,
        source="startup", transcript_path=None, at=at,
    )


# ---------------------------------------------------------------------------
# T1.1 — Initial SessionStarted creates IDLE state with full fields.
# ---------------------------------------------------------------------------


def test_t1_1_session_started_creates_idle():
    sm = SessionStateMachine()
    changed = sm.apply(_started_at())
    assert changed == {_UUID}

    state = sm.read(_UUID)
    assert state is not None
    assert state.session_uuid == _UUID
    assert state.phase == SessionPhase.IDLE
    assert state.cwd == _CWD
    assert state.started_at == _NOW
    assert state.last_hook_at == _NOW
    assert state.current_tool is None
    assert state.last_prompt is None


# ---------------------------------------------------------------------------
# T1.2 — IDLE + PromptSubmitted → THINKING, last_prompt preserved.
# ---------------------------------------------------------------------------


def test_t1_2_prompt_submitted_transitions_to_thinking():
    sm = SessionStateMachine()
    sm.apply(_started_at())
    changed = sm.apply(PromptSubmitted(_UUID, "explain this code", _NOW + timedelta(seconds=1)))
    assert changed == {_UUID}
    state = sm.read(_UUID)
    assert state.phase == SessionPhase.THINKING
    assert state.last_prompt == "explain this code"


# ---------------------------------------------------------------------------
# T1.3 — THINKING + ToolStarted → TOOL_USE, current_tool set.
# ---------------------------------------------------------------------------


def test_t1_3_tool_started_transitions_to_tool_use():
    sm = SessionStateMachine()
    sm.apply(_started_at())
    sm.apply(PromptSubmitted(_UUID, "do it", _NOW + timedelta(seconds=1)))
    sm.apply(ToolStarted(
        session_uuid=_UUID,
        tool_name="Bash",
        tool_input_preview="ls -la",
        tool_use_id="tu_1",
        at=_NOW + timedelta(seconds=2),
    ))
    state = sm.read(_UUID)
    assert state.phase == SessionPhase.TOOL_USE
    assert state.current_tool == "Bash"


# ---------------------------------------------------------------------------
# T1.4 — TOOL_USE + ToolFinished → THINKING, current_tool cleared.
# ---------------------------------------------------------------------------


def test_t1_4_tool_finished_clears_current_tool():
    sm = SessionStateMachine()
    sm.apply(_started_at())
    sm.apply(PromptSubmitted(_UUID, "do it", _NOW + timedelta(seconds=1)))
    sm.apply(ToolStarted(_UUID, "Bash", None, None, _NOW + timedelta(seconds=2)))
    sm.apply(ToolFinished(_UUID, "Bash", None, False, _NOW + timedelta(seconds=3)))
    state = sm.read(_UUID)
    assert state.phase == SessionPhase.THINKING
    assert state.current_tool is None


# ---------------------------------------------------------------------------
# T1.5 — Multi-tool turn: Tool→Finish→Tool→Finish→Turn → IDLE.
# ---------------------------------------------------------------------------


def test_t1_5_multi_tool_turn_ends_idle():
    sm = SessionStateMachine()
    sm.apply(_started_at())
    sm.apply(PromptSubmitted(_UUID, "fix bug", _NOW + timedelta(seconds=1)))
    sm.apply(ToolStarted(_UUID, "Read", "file.py", None, _NOW + timedelta(seconds=2)))
    sm.apply(ToolFinished(_UUID, "Read", None, False, _NOW + timedelta(seconds=3)))
    sm.apply(ToolStarted(_UUID, "Edit", "file.py", None, _NOW + timedelta(seconds=4)))
    sm.apply(ToolFinished(_UUID, "Edit", None, False, _NOW + timedelta(seconds=5)))
    sm.apply(TurnCompleted(_UUID, "Fixed.", False, _NOW + timedelta(seconds=6)))
    state = sm.read(_UUID)
    assert state.phase == SessionPhase.IDLE
    assert state.current_tool is None
    assert state.last_assistant_message == "Fixed."


# ---------------------------------------------------------------------------
# Plan F — current_tool_input flows through ToolStarted and gets cleared
# on every phase exit. The UI ticker row depends on this field staying
# in sync with current_tool / phase=TOOL_USE.
# ---------------------------------------------------------------------------


def test_plan_f_tool_started_sets_current_tool_input():
    sm = SessionStateMachine()
    sm.apply(_started_at())
    sm.apply(PromptSubmitted(_UUID, "do it", _NOW + timedelta(seconds=1)))
    sm.apply(ToolStarted(
        session_uuid=_UUID,
        tool_name="Bash",
        tool_input_preview="pytest tests/test_login.py -v",
        tool_use_id="tu_1",
        at=_NOW + timedelta(seconds=2),
    ))
    state = sm.read(_UUID)
    assert state.phase == SessionPhase.TOOL_USE
    assert state.current_tool == "Bash"
    assert state.current_tool_input == "pytest tests/test_login.py -v"


def test_plan_f_tool_finished_clears_tool_input():
    """The TOOL_USE → THINKING transition must clear both current_tool
    and current_tool_input — otherwise the invariant
    ``current_tool_input is not None ⇒ phase=TOOL_USE`` fires an
    AssertionError and the state gets tombstoned."""
    sm = SessionStateMachine()
    sm.apply(_started_at())
    sm.apply(PromptSubmitted(_UUID, "do it", _NOW + timedelta(seconds=1)))
    sm.apply(ToolStarted(_UUID, "Bash", "ls -la", None, _NOW + timedelta(seconds=2)))
    sm.apply(ToolFinished(_UUID, "Bash", None, False, _NOW + timedelta(seconds=3)))
    state = sm.read(_UUID)
    assert state.phase == SessionPhase.THINKING
    assert state.current_tool is None
    assert state.current_tool_input is None


def test_plan_f_tool_input_none_when_extractor_returned_none():
    """The hook's ``_extract_tool_input_preview`` returns None for
    opaque tool_input shapes (novel MCP tools, JSON shapes the
    extractor doesn't know). State machine must accept that — TOOL_USE
    with current_tool set but current_tool_input=None is valid."""
    sm = SessionStateMachine()
    sm.apply(_started_at())
    sm.apply(PromptSubmitted(_UUID, "do it", _NOW + timedelta(seconds=1)))
    sm.apply(ToolStarted(
        session_uuid=_UUID,
        tool_name="ExoticMcpTool",
        tool_input_preview=None,  # extractor couldn't render it
        tool_use_id="tu_1",
        at=_NOW + timedelta(seconds=2),
    ))
    state = sm.read(_UUID)
    assert state.phase == SessionPhase.TOOL_USE
    assert state.current_tool == "ExoticMcpTool"
    assert state.current_tool_input is None


def test_plan_f_invariant_violated_when_tool_input_set_outside_tool_use():
    """The SessionLiveState invariant catches a bug where someone
    constructs a state with current_tool_input set but phase!=TOOL_USE
    — the assertion is the safety net for that."""
    with pytest.raises(AssertionError, match="current_tool_input"):
        SessionLiveState(
            session_uuid=_UUID,
            phase=SessionPhase.THINKING,
            cwd=_CWD,
            started_at=_NOW,
            last_hook_at=_NOW,
            current_tool=None,
            current_tool_input="ls -la",  # illegal: phase != TOOL_USE
        )


def test_plan_f_prompt_submitted_clears_stale_tool_input():
    """PromptSubmitted must clear current_tool_input alongside
    current_tool — a new prompt ends the previous tool cycle
    regardless of whether ToolFinished fired."""
    sm = SessionStateMachine()
    sm.apply(_started_at())
    sm.apply(PromptSubmitted(_UUID, "first", _NOW + timedelta(seconds=1)))
    sm.apply(ToolStarted(_UUID, "Bash", "x", None, _NOW + timedelta(seconds=2)))
    # Skip ToolFinished — simulate dropped close event.
    sm.apply(PromptSubmitted(_UUID, "second", _NOW + timedelta(seconds=4)))
    state = sm.read(_UUID)
    assert state.phase == SessionPhase.THINKING
    assert state.current_tool is None
    assert state.current_tool_input is None


def test_plan_f_compact_started_clears_tool_input():
    sm = SessionStateMachine()
    sm.apply(_started_at())
    sm.apply(PromptSubmitted(_UUID, "do it", _NOW + timedelta(seconds=1)))
    sm.apply(ToolStarted(_UUID, "Bash", "x", None, _NOW + timedelta(seconds=2)))
    sm.apply(CompactStarted(_UUID, _NOW + timedelta(seconds=3)))
    state = sm.read(_UUID)
    assert state.phase == SessionPhase.COMPACTING
    assert state.current_tool is None
    assert state.current_tool_input is None


# ---------------------------------------------------------------------------
# T1.6 — THINKING + PermissionRequested → WAITING_APPROVAL, tool set.
# ---------------------------------------------------------------------------


def test_t1_6_permission_requested_transitions_to_waiting():
    sm = SessionStateMachine()
    sm.apply(_started_at())
    sm.apply(PromptSubmitted(_UUID, "rm -rf", _NOW + timedelta(seconds=1)))
    sm.apply(PermissionRequested(_UUID, "Bash", _NOW + timedelta(seconds=2)))
    state = sm.read(_UUID)
    assert state.phase == SessionPhase.WAITING_APPROVAL
    assert state.pending_permission_tool == "Bash"
    assert state.current_tool is None  # invariant: WAITING_APPROVAL clears current_tool


# ---------------------------------------------------------------------------
# T1.7 — WAITING_APPROVAL + TurnCompleted → IDLE, pending cleared.
# ---------------------------------------------------------------------------


def test_t1_7_turn_completed_from_waiting_clears_pending():
    sm = SessionStateMachine()
    sm.apply(_started_at())
    sm.apply(PromptSubmitted(_UUID, "rm", _NOW + timedelta(seconds=1)))
    sm.apply(PermissionRequested(_UUID, "Bash", _NOW + timedelta(seconds=2)))
    sm.apply(TurnCompleted(_UUID, "User denied.", False, _NOW + timedelta(seconds=3)))
    state = sm.read(_UUID)
    assert state.phase == SessionPhase.IDLE
    assert state.pending_permission_tool is None


# ---------------------------------------------------------------------------
# T1.7b — WAITING_APPROVAL + ToolFinished → THINKING, pending cleared.
# Regression: previously ToolFinished only cleared current_tool, leaving
# pending_permission_tool stale and tripping the WAITING_APPROVAL iff
# invariant when PostToolUse(Failure) fired after a denied PermissionRequest.
# ---------------------------------------------------------------------------


def test_t1_7b_tool_finished_from_waiting_clears_pending():
    sm = SessionStateMachine()
    sm.apply(_started_at())
    sm.apply(PromptSubmitted(_UUID, "rm -rf /", _NOW + timedelta(seconds=1)))
    sm.apply(PermissionRequested(_UUID, "Bash", _NOW + timedelta(seconds=2)))
    sm.apply(ToolFinished(_UUID, "Bash", None, True, _NOW + timedelta(seconds=3)))
    state = sm.read(_UUID)
    assert state.phase == SessionPhase.THINKING
    assert state.current_tool is None
    assert state.pending_permission_tool is None


# ---------------------------------------------------------------------------
# T1.8 — Any phase + SessionEnded → ENDED with overlays cleared.
# ---------------------------------------------------------------------------


def test_t1_8_session_ended_clears_overlays():
    sm = SessionStateMachine()
    sm.apply(_started_at())
    sm.apply(PromptSubmitted(_UUID, "do it", _NOW + timedelta(seconds=1)))
    sm.apply(ToolStarted(_UUID, "Bash", "x", None, _NOW + timedelta(seconds=2)))
    # ENDED while TOOL_USE: must clear current_tool (else invariant violated)
    sm.apply(SessionEnded(_UUID, _NOW + timedelta(seconds=3)))
    state = sm.read(_UUID)
    assert state.phase == SessionPhase.ENDED
    assert state.current_tool is None
    assert state.pending_permission_tool is None


# ---------------------------------------------------------------------------
# T1.9 — ENDED + any event → ENDED (terminal, idempotent).
# ---------------------------------------------------------------------------


def test_t1_9_ended_is_terminal():
    sm = SessionStateMachine()
    sm.apply(_started_at())
    sm.apply(SessionEnded(_UUID, _NOW + timedelta(seconds=1)))

    # Future events arrive — phase must stay ENDED.
    for ev in [
        PromptSubmitted(_UUID, "?", _NOW + timedelta(seconds=2)),
        ToolStarted(_UUID, "Bash", None, None, _NOW + timedelta(seconds=3)),
        TurnCompleted(_UUID, None, False, _NOW + timedelta(seconds=4)),
    ]:
        sm.apply(ev)
    assert sm.read(_UUID).phase == SessionPhase.ENDED


# ---------------------------------------------------------------------------
# T1.10 — Unknown event variant → no-op (only last_hook_at), no raise.
# ---------------------------------------------------------------------------


def test_t1_10_unknown_event_variant_graceful():
    """Construct a mock event that doesn't match any isinstance branch.
    The reducer's final fallback should just update last_hook_at."""

    class _FakeEvent:
        session_uuid = _UUID
        at = _NOW + timedelta(seconds=1)

    sm = SessionStateMachine()
    sm.apply(_started_at())
    initial = sm.read(_UUID)
    # _transition is called directly because apply checks .session_uuid
    # via duck typing — but we want the no-op behaviour validated cleanly.
    sm.apply(_FakeEvent())  # type: ignore[arg-type]
    after = sm.read(_UUID)
    assert after.phase == SessionPhase.IDLE  # unchanged
    assert after.last_hook_at == _NOW + timedelta(seconds=1)
    # Phase didn't change so set should have been empty — apply returns
    # set(). _state_eq excludes last_hook_at so it dedupes.


# ---------------------------------------------------------------------------
# T1.11 — Idempotent at state level: applying the same logical event
# again returns empty changed-set (state unchanged modulo last_hook_at).
# ---------------------------------------------------------------------------


def test_t1_11_apply_idempotent_at_state_level():
    sm = SessionStateMachine()
    sm.apply(_started_at())
    sm.apply(PromptSubmitted(_UUID, "go", _NOW + timedelta(seconds=1)))

    # Apply ANOTHER PromptSubmitted with same prompt at a later time —
    # state stays THINKING with same prompt, so apply should return
    # empty set (only last_hook_at bumped internally).
    changed = sm.apply(PromptSubmitted(_UUID, "go", _NOW + timedelta(seconds=2)))
    assert changed == set()
    state = sm.read(_UUID)
    assert state.last_hook_at == _NOW + timedelta(seconds=2)


# ---------------------------------------------------------------------------
# T1.12 — Invariant violation in _transition triggers tombstone, no crash.
# ---------------------------------------------------------------------------


def test_t1_12_invariant_violation_force_tombstones(monkeypatch):
    """If _transition produces a state that fails __post_init__,
    apply() catches the AssertionError, force-tombstones, does NOT raise."""
    sm = SessionStateMachine()
    sm.apply(_started_at())

    # Monkey-patch _transition to produce an illegal state on the next
    # apply (TOOL_USE without current_tool). The SessionLiveState
    # constructor will raise inside _transition.
    from claude_island.core import session_state_machine as ssm

    def bad_transition(prev, event):
        return SessionLiveState(
            session_uuid=_UUID,
            phase=SessionPhase.TOOL_USE,  # ← requires current_tool
            cwd=_CWD,
            started_at=_NOW,
            last_hook_at=event.at,
            current_tool=None,  # ← violates invariant
        )

    monkeypatch.setattr(ssm, "_transition", bad_transition)

    # apply must NOT raise
    changed = sm.apply(PromptSubmitted(_UUID, "trigger", _NOW + timedelta(seconds=1)))
    assert changed == {_UUID}

    # Session was tombstoned
    state = sm.read(_UUID)
    assert state.phase == SessionPhase.ENDED


# ---------------------------------------------------------------------------
# T1.13 — live_state_changed Subject emits only on real state change.
# ---------------------------------------------------------------------------


def test_t1_13_live_state_changed_emits_on_change_only():
    sm = SessionStateMachine()
    emissions: list[set[str]] = []
    sm.live_state_changed.subscribe(on_next=emissions.append)

    # Initial SessionStart → emit
    sm.apply(_started_at())
    assert emissions == [{_UUID}]

    # Re-apply same SessionStarted (would produce same state) → no emit
    emissions.clear()
    sm.apply(SessionStarted(
        session_uuid=_UUID, cwd=_CWD, started_at=_NOW,
        source="startup", transcript_path=None, at=_NOW + timedelta(seconds=1),
    ))
    # State equality holds (last_hook_at excluded), so no emit.
    assert emissions == []


# ---------------------------------------------------------------------------
# T1.14 — Concurrent apply: 4 threads × 100 events each, final state coherent.
# ---------------------------------------------------------------------------


def test_t1_14_concurrent_apply_thread_safe():
    sm = SessionStateMachine()
    # Pre-seed many uuids so threads contend on different keys
    for i in range(10):
        sm.apply(_started_at(uuid=f"u-{i}"))

    errors: list[Exception] = []

    def worker(thread_id: int) -> None:
        try:
            for i in range(100):
                uuid = f"u-{i % 10}"
                sm.apply(PromptSubmitted(
                    uuid, f"t{thread_id}-{i}",
                    _NOW + timedelta(seconds=thread_id * 1000 + i),
                ))
                sm.apply(ToolStarted(
                    uuid, "Bash", None, None,
                    _NOW + timedelta(seconds=thread_id * 1000 + i, microseconds=1),
                ))
                sm.apply(ToolFinished(
                    uuid, "Bash", None, False,
                    _NOW + timedelta(seconds=thread_id * 1000 + i, microseconds=2),
                ))
        except Exception as e:
            errors.append(e)

    threads = [threading.Thread(target=worker, args=(t,)) for t in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == [], f"Concurrent apply raised: {errors}"
    # Final state coherent: all sessions in THINKING (after last ToolFinished)
    for i in range(10):
        state = sm.read(f"u-{i}")
        assert state is not None
        assert state.phase == SessionPhase.THINKING


# ---------------------------------------------------------------------------
# Extra: SessionStart with source=compact during COMPACTING → IDLE.
# ---------------------------------------------------------------------------


def test_compact_finish_returns_to_idle():
    sm = SessionStateMachine()
    sm.apply(_started_at())
    sm.apply(PromptSubmitted(_UUID, "long", _NOW + timedelta(seconds=1)))
    sm.apply(CompactStarted(_UUID, _NOW + timedelta(seconds=2)))
    assert sm.read(_UUID).phase == SessionPhase.COMPACTING

    sm.apply(SessionStarted(
        session_uuid=_UUID, cwd=_CWD, started_at=_NOW,
        source="compact", transcript_path=None,
        at=_NOW + timedelta(seconds=3),
    ))
    state = sm.read(_UUID)
    assert state.phase == SessionPhase.IDLE
    # last_prompt is kept (history of pre-compact session)
    assert state.last_prompt == "long"


# ---------------------------------------------------------------------------
# Extra: Hook event for unknown uuid (no prior SessionStart) is tolerated.
# ---------------------------------------------------------------------------


def test_orphan_event_creates_placeholder_state():
    """If an event arrives for an unknown uuid, the reducer synthesizes
    a placeholder state. The phase reflects the event's effect."""
    sm = SessionStateMachine()
    sm.apply(PromptSubmitted("orphan-uuid", "prompt", _NOW))
    state = sm.read("orphan-uuid")
    assert state is not None
    assert state.phase == SessionPhase.THINKING
    assert state.last_prompt == "prompt"


# ---------------------------------------------------------------------------
# tombstone()
# ---------------------------------------------------------------------------


def test_tombstone_transitions_to_ended():
    sm = SessionStateMachine()
    sm.apply(_started_at())
    assert sm.tombstone(_UUID) is True
    assert sm.read(_UUID).phase == SessionPhase.ENDED


def test_tombstone_idempotent():
    sm = SessionStateMachine()
    sm.apply(_started_at())
    sm.tombstone(_UUID)
    # Already ENDED → no further change
    assert sm.tombstone(_UUID) is False


def test_tombstone_unknown_uuid_returns_false():
    sm = SessionStateMachine()
    assert sm.tombstone("never-existed") is False


def test_tombstone_emits_live_state_changed():
    sm = SessionStateMachine()
    sm.apply(_started_at())
    emissions: list[set[str]] = []
    sm.live_state_changed.subscribe(on_next=emissions.append)
    sm.tombstone(_UUID)
    assert emissions == [{_UUID}]


# ---------------------------------------------------------------------------
# Notifications never change phase
# ---------------------------------------------------------------------------


def test_notification_does_not_change_phase():
    sm = SessionStateMachine()
    sm.apply(_started_at())
    sm.apply(PromptSubmitted(_UUID, "go", _NOW + timedelta(seconds=1)))
    sm.apply(NotificationFired(_UUID, is_idle=True, at=_NOW + timedelta(seconds=2)))
    assert sm.read(_UUID).phase == SessionPhase.THINKING


# ---------------------------------------------------------------------------
# snapshot() returns frozen mapping
# ---------------------------------------------------------------------------


def test_snapshot_is_immutable():
    sm = SessionStateMachine()
    sm.apply(_started_at())
    snap = sm.snapshot()
    assert _UUID in snap
    # MappingProxyType doesn't support item assignment
    with pytest.raises(TypeError):
        snap[_UUID] = None  # type: ignore[index]


# ---------------------------------------------------------------------------
# Edge: empty session_uuid is dropped
# ---------------------------------------------------------------------------


def test_empty_uuid_dropped():
    sm = SessionStateMachine()
    ev = PromptSubmitted("", "x", _NOW)
    changed = sm.apply(ev)
    assert changed == set()
    assert sm.read("") is None


# ---------------------------------------------------------------------------
# _state_eq excludes last_hook_at
# ---------------------------------------------------------------------------


def test_state_eq_ignores_last_hook_at():
    a = SessionLiveState(
        session_uuid=_UUID, phase=SessionPhase.IDLE, cwd=_CWD,
        started_at=_NOW, last_hook_at=_NOW,
    )
    b = SessionLiveState(
        session_uuid=_UUID, phase=SessionPhase.IDLE, cwd=_CWD,
        started_at=_NOW, last_hook_at=_NOW + timedelta(seconds=10),
    )
    assert _state_eq(a, b) is True
    assert a != b  # structural equality differs because of last_hook_at


def test_state_eq_catches_real_change():
    a = SessionLiveState(
        session_uuid=_UUID, phase=SessionPhase.IDLE, cwd=_CWD,
        started_at=_NOW, last_hook_at=_NOW,
    )
    b = SessionLiveState(
        session_uuid=_UUID, phase=SessionPhase.THINKING, cwd=_CWD,
        started_at=_NOW, last_hook_at=_NOW,
    )
    assert _state_eq(a, b) is False


# ── gc_ended (P1-a) ─────────────────────────────────────────────────────


def test_gc_ended_removes_stale_ended_entries():
    """ENDED entries whose last_hook_at is older than retain_seconds
    are removed. Active states (THINKING / IDLE / etc.) are never
    touched regardless of age."""
    from claude_island.core.hook_events import SessionStarted, SessionEnded
    sm = SessionStateMachine()
    long_ago = _NOW - timedelta(hours=2)

    # u-old: ENDED, last_hook_at far in past → should GC
    sm.apply(SessionStarted("u-old", _CWD, long_ago, "startup", None, long_ago))
    sm.apply(SessionEnded("u-old", long_ago))

    # u-recent: ENDED but JUST ended → should NOT GC (within retain window)
    sm.apply(SessionStarted("u-recent", _CWD, _NOW, "startup", None, _NOW))
    sm.apply(SessionEnded("u-recent", _NOW))

    # u-active: still THINKING, ancient — never GC regardless of age
    sm.apply(SessionStarted("u-active", _CWD, long_ago, "startup", None, long_ago))

    removed = sm.gc_ended(retain_seconds=3600.0, now=_NOW)
    assert removed == 1
    assert sm.read("u-old") is None             # GC'd
    assert sm.read("u-recent") is not None      # too fresh
    assert sm.read("u-active") is not None      # never GC'd (not ENDED)


def test_gc_ended_increments_sm_ended_gc_metric():
    from claude_island.core.hook_events import SessionStarted, SessionEnded
    from claude_island.core.metrics import metrics
    sm = SessionStateMachine()
    long_ago = _NOW - timedelta(hours=2)
    sm.apply(SessionStarted("u1", _CWD, long_ago, "startup", None, long_ago))
    sm.apply(SessionEnded("u1", long_ago))
    sm.apply(SessionStarted("u2", _CWD, long_ago, "startup", None, long_ago))
    sm.apply(SessionEnded("u2", long_ago))

    sm.gc_ended(retain_seconds=3600.0, now=_NOW)
    assert metrics.snapshot().counters.get("sm.ended.gc") == 2


def test_gc_ended_returns_zero_when_nothing_to_drop():
    sm = SessionStateMachine()
    # Empty state machine — GC must be a clean no-op (no exceptions, count = 0).
    assert sm.gc_ended(retain_seconds=3600.0, now=_NOW) == 0


def test_gc_ended_with_custom_retain_window():
    """Caller can pick the retention horizon to suit the deployment.
    A short window (1s) GC's a freshly-ended state from 5s ago."""
    from claude_island.core.hook_events import SessionStarted, SessionEnded
    sm = SessionStateMachine()
    five_s_ago = _NOW - timedelta(seconds=5)
    sm.apply(SessionStarted("u", _CWD, five_s_ago, "startup", None, five_s_ago))
    sm.apply(SessionEnded("u", five_s_ago))

    assert sm.gc_ended(retain_seconds=1.0, now=_NOW) == 1
    assert sm.read("u") is None
