"""Command-hero persistence — the active card's "$ <cmd>" line.

Unlike ``current_tool_input`` (cleared the moment we leave TOOL_USE to
satisfy the "non-None ⇒ TOOL_USE" invariant), ``last_command`` /
``last_command_at`` PERSIST across phases so the active card keeps showing
"what this session most recently ran" while the model thinks between tool
calls. A new user prompt resets them.

These tests pin that persistence behaviour at the state-machine layer.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from claude_island.core.hook_events import (
    PromptSubmitted,
    SessionStarted,
    ToolFinished,
    ToolStarted,
    TurnCompleted,
)
from claude_island.core.session_phase import SessionPhase
from claude_island.core.session_state_machine import SessionStateMachine

_UUID = "cmd-hero-uuid"
_NOW = datetime(2026, 5, 1, 12, 0, 0, tzinfo=timezone.utc)


def _t(secs: int) -> datetime:
    return _NOW + timedelta(seconds=secs)


def _started() -> SessionStarted:
    return SessionStarted(
        session_uuid=_UUID, cwd=Path("/tmp/proj"), started_at=_NOW,
        source="startup", transcript_path=None, at=_NOW,
    )


def test_tool_started_stamps_last_command():
    """Happy: ToolStarted records the command preview + start time."""
    sm = SessionStateMachine()
    sm.apply(_started())
    sm.apply(PromptSubmitted(_UUID, "do it", _t(1)))
    sm.apply(ToolStarted(_UUID, "Bash", "uv run pytest -q", None, _t(2)))
    s = sm.read(_UUID)
    assert s.phase == SessionPhase.TOOL_USE
    assert s.last_command == "uv run pytest -q"
    assert s.last_command_at == _t(2)


def test_last_command_persists_into_thinking():
    """Core of the design: after the tool finishes (→ THINKING), the
    command-hero line must still be available, even though
    current_tool_input is cleared."""
    sm = SessionStateMachine()
    sm.apply(_started())
    sm.apply(PromptSubmitted(_UUID, "do it", _t(1)))
    sm.apply(ToolStarted(_UUID, "Bash", "uv run pytest -q", None, _t(2)))
    sm.apply(ToolFinished(_UUID, "Bash", None, False, _t(3)))
    s = sm.read(_UUID)
    assert s.phase == SessionPhase.THINKING
    assert s.current_tool_input is None          # invariant still holds
    assert s.last_command == "uv run pytest -q"  # but hero line persists
    assert s.last_command_at == _t(2)            # original start time kept


def test_last_command_falls_back_to_tool_name():
    """Edge: when the extractor returns no renderable preview, the hero
    line falls back to the tool name so it is never blank for an active
    tool."""
    sm = SessionStateMachine()
    sm.apply(_started())
    sm.apply(PromptSubmitted(_UUID, "do it", _t(1)))
    sm.apply(ToolStarted(_UUID, "WebSearch", None, None, _t(2)))
    s = sm.read(_UUID)
    assert s.last_command == "WebSearch"


def test_new_prompt_resets_last_command():
    """Edge: a brand-new user prompt starts a fresh turn, so the prior
    command no longer describes the session — it must reset."""
    sm = SessionStateMachine()
    sm.apply(_started())
    sm.apply(PromptSubmitted(_UUID, "first", _t(1)))
    sm.apply(ToolStarted(_UUID, "Bash", "ls -la", None, _t(2)))
    sm.apply(ToolFinished(_UUID, "Bash", None, False, _t(3)))
    sm.apply(TurnCompleted(_UUID, "done", False, _t(4)))
    # mid-state: command still around through IDLE
    assert sm.read(_UUID).last_command == "ls -la"
    sm.apply(PromptSubmitted(_UUID, "second question", _t(5)))
    s = sm.read(_UUID)
    assert s.phase == SessionPhase.THINKING
    assert s.last_command is None
    assert s.last_command_at is None


def test_last_command_persists_through_turn_completed():
    """The command stays available through TurnCompleted (IDLE) so an
    idle-but-recently-active session can still show what it last ran
    until the user starts a new turn."""
    sm = SessionStateMachine()
    sm.apply(_started())
    sm.apply(PromptSubmitted(_UUID, "do it", _t(1)))
    sm.apply(ToolStarted(_UUID, "Edit", "main.py", None, _t(2)))
    sm.apply(ToolFinished(_UUID, "Edit", None, False, _t(3)))
    sm.apply(TurnCompleted(_UUID, "done", False, _t(4)))
    s = sm.read(_UUID)
    assert s.phase == SessionPhase.IDLE
    assert s.last_command == "main.py"
