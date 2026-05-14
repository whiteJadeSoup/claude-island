"""SessionPhase enum — the single source of truth for "what is this session doing".

Drives UI status chip rendering and capability gating. Values are stable
strings (not opaque ints) so they round-trip through logs and JSON without
needing extra mapping tables.

The set of phases is intentionally compact — every value must answer one
question: "what should the row display right now?" Sub-states like
"compacting context but still on prompt N" don't get their own phase;
they ride as overlay fields on ``SessionLiveState``.
"""
from __future__ import annotations

from enum import StrEnum


class SessionPhase(StrEnum):
    """Coarse-grained activity state for one session.

    Transitions are driven by Claude Code hook events (live signal) when
    the hook pipeline is connected, and by ~/.claude/sessions/<pid>.json
    ``status`` field plus a 30-second activity heuristic when the hook
    pipeline is not connected (a session that started before claude-island
    was running, or with the hook listener temporarily down).

    UI MUST treat the two signal sources identically — no "degraded" badge
    or visual hint distinguishes hook-driven from pid.json-driven phase.
    """

    IDLE = "idle"
    """No active turn. Waiting for the next user prompt."""

    THINKING = "thinking"
    """User submitted a prompt; Claude is reasoning, has not yet called
    a tool. Also used as the pid.json-fallback mapping for ``status='busy'``
    when finer hook-driven granularity is unavailable."""

    TOOL_USE = "tool_use"
    """Claude is currently executing a tool (between PreToolUse and the
    matching PostToolUse/PostToolUseFailure). The specific tool name is
    carried on ``SessionLiveState.current_tool``."""

    WAITING_APPROVAL = "waiting_approval"
    """Claude requested permission for a tool and is blocked until the
    user resolves it. v1 does not let claude-island actually approve/deny
    — it only surfaces the state."""

    COMPACTING = "compacting"
    """Claude is compacting the conversation context. Transitions back to
    IDLE on the next SessionStart (with source='compact')."""

    ENDED = "ended"
    """Session terminated. Either via SessionEnd hook (clean exit) or via
    HookSessionBridge tombstone (process disappeared from scanner output
    for MISS_THRESHOLD consecutive ticks)."""

    def is_active(self) -> bool:
        """Whether this phase counts as "currently working" for UI purposes.

        ENDED and IDLE are not active. Every other phase is. WAITING_APPROVAL
        counts as active because Claude is blocked but the session is not
        idle — the user is expected to take action.

        Used by ``SessionView.is_running`` property (backwards-compat shim
        for UI code that hasn't migrated to ``phase`` directly).
        """
        return self not in (SessionPhase.IDLE, SessionPhase.ENDED)
