"""Tests for hook_events module — event variants + SessionLiveState invariants.

The state-machine reducer is tested in test_session_state_machine.py;
this file only covers the value objects in isolation.
"""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import pytest

from claude_island.core.hook_events import (
    CompactStarted,
    HookEvent,
    JumpTarget,
    LiveStateProto,
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

_NOW = datetime(2026, 5, 13, 12, 0, 0, tzinfo=timezone.utc)
_CWD = Path("D:/coding projects/foo")


# ---------------------------------------------------------------------------
# Event variants: frozen + structural equality + slots
# ---------------------------------------------------------------------------


def test_session_started_structural_equality():
    """Two SessionStarted with identical fields compare equal — required
    for distinct_until_changed dedup downstream."""
    a = SessionStarted(
        session_uuid="abc",
        cwd=_CWD,
        started_at=_NOW,
        source="startup",
        transcript_path=None,
        at=_NOW,
    )
    b = SessionStarted(
        session_uuid="abc",
        cwd=_CWD,
        started_at=_NOW,
        source="startup",
        transcript_path=None,
        at=_NOW,
    )
    assert a == b
    assert hash(a) == hash(b)


def test_event_variants_are_frozen():
    """Cannot mutate fields after construction — frozen dataclass guarantee."""
    ev = ToolStarted(
        session_uuid="abc",
        tool_name="Bash",
        tool_input_preview="ls",
        tool_use_id="tu_1",
        at=_NOW,
    )
    with pytest.raises((AttributeError, TypeError)):
        ev.tool_name = "Read"  # type: ignore[misc]


def test_event_variants_have_slots():
    """slots=True means no __dict__ — cannot stash extra attributes.

    The exact exception type depends on Python's interaction between
    frozen and slots: frozen's __setattr__ raises FrozenInstanceError
    (subclass of AttributeError) for declared fields, but for undeclared
    fields the slots layer can raise TypeError before frozen's check
    runs. Accept either."""
    ev = SessionEnded(session_uuid="abc", at=_NOW)
    with pytest.raises((AttributeError, TypeError)):
        ev.extra_field = "nope"  # type: ignore[attr-defined]


def test_all_event_variants_construct():
    """Smoke test: every variant in the HookEvent union constructs cleanly
    with minimal valid args. Catches refactor mistakes that drop a field
    or add a required one without updating callers."""
    events: list[HookEvent] = [
        SessionStarted(
            session_uuid="u1", cwd=_CWD, started_at=_NOW,
            source=None, transcript_path=None, at=_NOW,
        ),
        PromptSubmitted(session_uuid="u1", prompt="hi", at=_NOW),
        ToolStarted(
            session_uuid="u1", tool_name="Read",
            tool_input_preview=None, tool_use_id=None, at=_NOW,
        ),
        ToolFinished(
            session_uuid="u1", tool_name="Read",
            tool_use_id=None, is_failure=False, at=_NOW,
        ),
        TurnCompleted(
            session_uuid="u1", last_assistant_message=None,
            is_failure=False, at=_NOW,
        ),
        SessionEnded(session_uuid="u1", at=_NOW),
        PermissionRequested(
            session_uuid="u1", tool_name="Bash", at=_NOW,
        ),
        CompactStarted(session_uuid="u1", at=_NOW),
        NotificationFired(session_uuid="u1", is_idle=False, at=_NOW),
    ]
    # Mypy-style assertion: every constructed object satisfies the union
    for e in events:
        assert hasattr(e, "session_uuid")
        assert hasattr(e, "at")


# ---------------------------------------------------------------------------
# SessionLiveState invariants — T1.12 family.
# ---------------------------------------------------------------------------


def _base_state(**overrides) -> dict:
    """Minimal valid SessionLiveState kwargs. Tests override one field
    at a time to construct invalid states."""
    defaults = dict(
        session_uuid="abc",
        phase=SessionPhase.IDLE,
        cwd=_CWD,
        started_at=_NOW,
        last_hook_at=_NOW,
    )
    defaults.update(overrides)
    return defaults


def test_session_live_state_valid_idle():
    """IDLE with no overlays — the simplest valid construction."""
    state = SessionLiveState(**_base_state())
    assert state.phase == SessionPhase.IDLE
    assert state.current_tool is None


def test_session_live_state_valid_tool_use():
    """TOOL_USE requires current_tool — the canonical paired case."""
    state = SessionLiveState(**_base_state(
        phase=SessionPhase.TOOL_USE,
        current_tool="Bash",
    ))
    assert state.phase == SessionPhase.TOOL_USE
    assert state.current_tool == "Bash"


def test_session_live_state_tool_use_without_current_tool_rejected():
    """TOOL_USE with current_tool=None violates the invariant."""
    with pytest.raises(AssertionError, match="phase=TOOL_USE"):
        SessionLiveState(**_base_state(phase=SessionPhase.TOOL_USE))


def test_session_live_state_current_tool_set_but_phase_not_tool_use_rejected():
    """current_tool=non-None requires phase=TOOL_USE — the other direction
    of the iff. Catches the bug where someone replace()s phase without
    clearing current_tool."""
    with pytest.raises(AssertionError, match="phase=TOOL_USE"):
        SessionLiveState(**_base_state(
            phase=SessionPhase.IDLE,
            current_tool="Bash",  # leftover field
        ))


def test_session_live_state_waiting_approval_requires_pending():
    """WAITING_APPROVAL ⇔ pending_permission_tool set."""
    # Valid
    s = SessionLiveState(**_base_state(
        phase=SessionPhase.WAITING_APPROVAL,
        pending_permission_tool="Bash",
    ))
    assert s.pending_permission_tool == "Bash"

    # Missing pending
    with pytest.raises(AssertionError, match="WAITING_APPROVAL"):
        SessionLiveState(**_base_state(phase=SessionPhase.WAITING_APPROVAL))

    # Pending set without WAITING_APPROVAL phase
    with pytest.raises(AssertionError, match="WAITING_APPROVAL"):
        SessionLiveState(**_base_state(
            phase=SessionPhase.IDLE,
            pending_permission_tool="Bash",
        ))


def test_session_live_state_ended_clears_overlays():
    """ENDED forbids current_tool and pending_permission_tool — the
    transition into ENDED must clear them. Catches the bug where the
    state machine sets phase=ENDED without resetting overlays."""
    # Valid ENDED (no overlays)
    s = SessionLiveState(**_base_state(phase=SessionPhase.ENDED))
    assert s.phase == SessionPhase.ENDED

    # ENDED + current_tool leftover
    with pytest.raises(AssertionError, match="ENDED"):
        SessionLiveState(**_base_state(
            phase=SessionPhase.ENDED,
            current_tool="Bash",
        ))

    # ENDED + pending_permission_tool leftover
    with pytest.raises(AssertionError, match="ENDED"):
        SessionLiveState(**_base_state(
            phase=SessionPhase.ENDED,
            pending_permission_tool="Bash",
        ))


def test_session_live_state_thinking_no_overlays_required():
    """THINKING/COMPACTING don't impose overlay invariants — the prompt
    is optional, the assistant message is optional. Sanity check that
    we don't over-constrain."""
    SessionLiveState(**_base_state(phase=SessionPhase.THINKING))
    SessionLiveState(**_base_state(phase=SessionPhase.COMPACTING))


def test_session_live_state_immutable():
    """replace() produces a new instance; original is untouched."""
    s = SessionLiveState(**_base_state())
    s2 = replace(s, phase=SessionPhase.THINKING)
    assert s.phase == SessionPhase.IDLE       # original untouched
    assert s2.phase == SessionPhase.THINKING
    assert s != s2


def test_session_live_state_structural_equality():
    """Two states with identical fields compare equal — required for
    state-level idempotency in apply()."""
    a = SessionLiveState(**_base_state())
    b = SessionLiveState(**_base_state())
    assert a == b


# ---------------------------------------------------------------------------
# SessionPhase enum — coverage check on is_active() classifier
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("phase,expected", [
    (SessionPhase.IDLE, False),
    (SessionPhase.THINKING, True),
    (SessionPhase.TOOL_USE, True),
    (SessionPhase.WAITING_APPROVAL, True),
    (SessionPhase.COMPACTING, True),
    (SessionPhase.ENDED, False),
])
def test_session_phase_is_active(phase: SessionPhase, expected: bool):
    """is_active classifier drives SessionView.is_running @property —
    pinning this prevents accidental UI regressions when a new phase
    is added."""
    assert phase.is_active() == expected


def test_session_phase_is_str_compatible():
    """StrEnum: SessionPhase.IDLE == 'idle' for JSON / log round-trip."""
    assert SessionPhase.IDLE == "idle"
    assert SessionPhase("idle") == SessionPhase.IDLE


# ---------------------------------------------------------------------------
# LiveStateProto — just a smoke check that a lambda satisfies the duck type
# ---------------------------------------------------------------------------


def test_live_state_proto_satisfied_by_lambda():
    """The compose_session_view boundary uses LiveStateProto for the
    callback. A plain lambda must satisfy the duck type — Protocol is
    structural, not nominal."""
    proto: LiveStateProto = lambda uuid: None  # noqa: E731
    assert proto("anything") is None


# ---------------------------------------------------------------------------
# JumpTarget — 2026-05-14 open-vibe-island redesign
# ---------------------------------------------------------------------------


def test_jump_target_default_construction():
    """Empty JumpTarget should be valid (all fields default)."""
    jt = JumpTarget()
    assert jt.terminal_app is None
    assert jt.conhost_hwnd == 0
    assert jt.host_pid == 0
    assert jt.wt_session_guid == ""
    assert jt.term_program == ""


def test_jump_target_frozen():
    """JumpTarget is frozen — cannot mutate after construction."""
    jt = JumpTarget(terminal_app="WindowsTerminal", conhost_hwnd=12345)
    with pytest.raises((AttributeError, TypeError)):
        jt.terminal_app = "iTerm.app"  # type: ignore


def test_jump_target_structural_equality():
    """Two JumpTargets with same fields compare equal — required for
    SessionLiveState dedup via _state_eq."""
    a = JumpTarget(terminal_app="WindowsTerminal", conhost_hwnd=12345)
    b = JumpTarget(terminal_app="WindowsTerminal", conhost_hwnd=12345)
    assert a == b
    assert hash(a) == hash(b)


def test_session_started_with_jump_target():
    """SessionStarted should accept and carry JumpTarget."""
    jt = JumpTarget(
        terminal_app="WindowsTerminal",
        conhost_hwnd=12521090,
        host_pid=82508,
        wt_session_guid="b2d0e4f0-1234-5678-90ab-cdef12345678",
        term_program="vscode",
    )
    ev = SessionStarted(
        session_uuid="abc",
        cwd=_CWD,
        started_at=_NOW,
        source="startup",
        transcript_path=None,
        at=_NOW,
        jump_target=jt,
    )
    assert ev.jump_target is jt
    assert ev.jump_target.terminal_app == "WindowsTerminal"


def test_session_started_jump_target_defaults_to_none():
    """Backward compat: old payloads without jump_target should still
    construct SessionStarted successfully."""
    ev = SessionStarted(
        session_uuid="abc",
        cwd=_CWD,
        started_at=_NOW,
        source=None,
        transcript_path=None,
        at=_NOW,
    )
    assert ev.jump_target is None


def test_session_live_state_carries_jump_target():
    """SessionLiveState exposes jump_target. compose_session_view uses
    this to populate SessionView."""
    jt = JumpTarget(terminal_app="WindowsTerminal", host_pid=999)
    state = SessionLiveState(
        session_uuid="abc",
        phase=SessionPhase.IDLE,
        cwd=_CWD,
        started_at=_NOW,
        last_hook_at=_NOW,
        jump_target=jt,
    )
    assert state.jump_target is jt
