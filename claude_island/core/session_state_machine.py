"""SessionStateMachine — the single reducer over HookEvent.

Architecture:

    HookEvent  ─►  apply()  ─►  new SessionLiveState
                      │
                      └─►  live_state_changed.on_next({changed_uuids})

The state machine is the single source of truth for hook-derived state.
``compose_session_view`` reads from it via the ``LiveStateProto`` callback;
``HookSessionBridge`` subscribes to ``live_state_changed`` to drive
``SessionRegistry`` upserts (so the UI shows a new session before the
process scanner has caught up).

Thread safety: all reads / writes serialized by an internal RLock.
The Subject ``on_next`` fires OUTSIDE the lock so subscribers can safely
re-enter the state machine (e.g. ``read(other_uuid)``) without
deadlocking. Subscribers run on the caller's thread (whichever thread
called ``apply``) — usually the HookServer worker thread.

Reducer is pure: ``_transition()`` is a free function with no side
effects, easy to unit-test. ``apply()`` is the thin imperative wrapper
that does locking + emits.

Invariant violation handling (F-7): if ``_transition`` produces a state
that fails the ``SessionLiveState.__post_init__`` invariants, ``apply``
catches the AssertionError, logs at ERROR, and force-tombstones the
session by writing a clean ENDED state. The hook pipeline does NOT crash
— a state-machine bug would otherwise take down the whole app.
"""
from __future__ import annotations

import logging
import threading
from dataclasses import replace
from datetime import datetime
from types import MappingProxyType
from typing import Mapping

from reactivex.subject import Subject

from .hook_events import (
    CompactStarted,
    HookEvent,
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
from .session_phase import SessionPhase

log = logging.getLogger(__name__)


class SessionStateMachine:
    """Holds per-uuid SessionLiveState. Reduces HookEvents to state changes.

    Usage:
        sm = SessionStateMachine()
        sm.live_state_changed.subscribe(on_next=my_callback, on_error=...)
        sm.apply(some_event)               # may emit
        live = sm.read("uuid-abc")         # snapshot read
        all_states = sm.snapshot()         # frozen mapping read
        sm.tombstone("uuid-abc")           # force ENDED (used by bridge)
    """

    def __init__(self) -> None:
        self._states: dict[str, SessionLiveState] = {}
        # RLock not Lock — subscribers may want to read state from inside
        # their on_next callback, and we drop the lock before emit so
        # that case works. RLock is a defense-in-depth choice; today
        # nothing re-enters the locked section.
        self._lock = threading.RLock()
        # Subject emits the set of changed uuids per apply() call.
        # Always a set (possibly empty isn't emitted — emit only on real
        # change so distinct_until_changed downstream isn't confused).
        self.live_state_changed: Subject[set[str]] = Subject()

    # ── public API ───────────────────────────────────────────────────────

    def apply(self, event: HookEvent) -> set[str]:
        """Apply one event. Returns the set of uuids whose state actually
        changed (size 0 or 1 in v1 — one event affects one uuid).

        Idempotent at the **state** level: when ``_transition`` produces
        a SessionLiveState equal to the previous one, returns empty set
        and does NOT emit. last_hook_at is always bumped internally, but
        is excluded from the state-equality check (see ``_state_eq``)
        because every hook touches it and it would defeat dedup.

        Never raises. Invariant violations (a transition that produces
        an illegal state) are caught and force-tombstone the session
        — see module doc.
        """
        uuid = event.session_uuid
        if not uuid:
            log.warning("apply: empty session_uuid on %s; dropping", type(event).__name__)
            return set()

        with self._lock:
            prev = self._states.get(uuid)
            try:
                new = _transition(prev, event)
            except AssertionError as e:
                log.exception(
                    "invariant violated transitioning from %s on %s for uuid=%s; "
                    "tombstoning: %s",
                    prev.phase if prev else "<none>",
                    type(event).__name__,
                    uuid,
                    e,
                )
                # Force a clean ENDED state — no overlay fields so the
                # invariants are guaranteed to hold here. Preserve
                # jump_target so the UI can still render the session's
                # terminal context after tombstone.
                new = SessionLiveState(
                    session_uuid=uuid,
                    phase=SessionPhase.ENDED,
                    cwd=(prev.cwd if prev else _UNKNOWN_CWD),
                    started_at=(prev.started_at if prev else event.at),
                    last_hook_at=event.at,
                    jump_target=(prev.jump_target if prev else None),
                )

            if prev is not None and _state_eq(prev, new):
                # State unchanged (ignoring last_hook_at). Persist the new
                # last_hook_at so staleness detection works, but do NOT
                # emit — downstream would just rebuild a snapshot that
                # ends up dedup'd anyway.
                self._states[uuid] = new
                return set()

            self._states[uuid] = new
            changed = {uuid}

        # Emit OUTSIDE the lock so a subscriber's on_next callback
        # can re-enter read() without deadlocking.
        self.live_state_changed.on_next(changed)
        return changed

    def read(self, uuid: str) -> SessionLiveState | None:
        """Snapshot read of one uuid. Used by ``compose_session_view``."""
        with self._lock:
            return self._states.get(uuid)

    def snapshot(self) -> Mapping[str, SessionLiveState]:
        """Frozen read-only mapping of all current state. Used by
        ``--doctor`` and ``HookSessionBridge`` scanner reconciliation.

        Returns a MappingProxyType wrapping a fresh dict copy — caller
        gets a stable view that won't change while they iterate, and
        also cannot mutate the underlying state."""
        with self._lock:
            return MappingProxyType(dict(self._states))

    def tombstone(self, uuid: str) -> bool:
        """Force a uuid into ENDED. Returns True if state actually
        changed (i.e., the uuid existed and was not already ENDED).

        Called by ``HookSessionBridge`` when scanner reports the pid
        missing for ``MISS_THRESHOLD`` consecutive ticks — closes the
        gap where ``SessionEnd`` hook never arrived (process killed,
        machine crash, hook listener temporarily down)."""
        with self._lock:
            prev = self._states.get(uuid)
            if prev is None:
                return False
            if prev.phase == SessionPhase.ENDED:
                return False
            new = SessionLiveState(
                session_uuid=prev.session_uuid,
                phase=SessionPhase.ENDED,
                cwd=prev.cwd,
                started_at=prev.started_at,
                last_hook_at=prev.last_hook_at,
                jump_target=prev.jump_target,
            )
            self._states[uuid] = new
            changed = {uuid}
        self.live_state_changed.on_next(changed)
        return True


# ---------------------------------------------------------------------------
# Helpers — kept module-level so they're testable in isolation.
# ---------------------------------------------------------------------------


from pathlib import Path

_UNKNOWN_CWD = Path(".")


def _state_eq(a: SessionLiveState, b: SessionLiveState) -> bool:
    """State equality excluding ``last_hook_at``.

    Every hook bumps last_hook_at; including it in the equality check
    would mean every event is "changed" even when nothing observable
    happened. Excluding it makes ``apply`` truly idempotent at the
    state level (F-9).
    """
    return (
        a.session_uuid == b.session_uuid
        and a.phase == b.phase
        and a.cwd == b.cwd
        and a.started_at == b.started_at
        and a.current_tool == b.current_tool
        and a.current_tool_input == b.current_tool_input
        and a.last_prompt == b.last_prompt
        and a.last_assistant_message == b.last_assistant_message
        and a.pending_permission_tool == b.pending_permission_tool
    )


def _transition(
    prev: SessionLiveState | None,
    event: HookEvent,
) -> SessionLiveState:
    """Pure function: previous state + event → new state.

    See Detail Design §3.1 transition table for the full matrix. Unknown
    combinations preserve the current phase (graceful evolution) — only
    the timestamp updates.

    Raises AssertionError if the produced state violates a
    SessionLiveState invariant; ``apply()`` catches this and tombstones.
    """
    # ── SessionStart: create (or recreate after /compact) ─────────────────
    if isinstance(event, SessionStarted):
        if prev is not None and prev.phase == SessionPhase.COMPACTING:
            # Compact finished — phase back to IDLE, keep history fields.
            # Update jump_target if the new event has one (in case the
            # session is now in a different terminal — unlikely but cheap
            # to handle). If event.jump_target is None, preserve prev's.
            new_jt = event.jump_target if event.jump_target is not None else prev.jump_target
            return replace(
                prev,
                phase=SessionPhase.IDLE,
                last_hook_at=event.at,
                current_tool=None,
                current_tool_input=None,
                tool_started_at=None,
                compact_started_at=None,
                # Compact is a context boundary — drop the prior command.
                last_command=None,
                last_command_at=None,
                pending_permission_tool=None,
                jump_target=new_jt,
            )
        # Fresh session (no prev) OR a true new session reusing a uuid.
        # In both cases reset everything.
        return SessionLiveState(
            session_uuid=event.session_uuid,
            phase=SessionPhase.IDLE,
            cwd=event.cwd,
            started_at=event.started_at,
            last_hook_at=event.at,
            jump_target=event.jump_target,
        )

    # Events below require an existing state. If prev is None we synthesize
    # one — the hook event arrived before any SessionStart, which can
    # happen on app restart or for sessions that started before claude-island
    # was running. We make our best guess at started_at/cwd from the event.
    if prev is None:
        prev = _synthesize_prev_state(event)

    # Terminal state: anything after ENDED is a no-op except for
    # last_hook_at (which doesn't trigger emit per _state_eq).
    if prev.phase == SessionPhase.ENDED:
        return replace(prev, last_hook_at=event.at)

    # ── PromptSubmitted: user typed a new prompt → THINKING ──────────────
    if isinstance(event, PromptSubmitted):
        return replace(
            prev,
            phase=SessionPhase.THINKING,
            last_hook_at=event.at,
            last_prompt=event.prompt,
            # Clear stale overlays — a new prompt ends the previous
            # tool/permission cycle even if their close events were lost.
            current_tool=None,
            current_tool_input=None,
            tool_started_at=None,
            # Command-hero: a brand-new user prompt starts a fresh turn, so
            # the previous turn's command no longer describes "what this
            # session is doing". Reset it; the next ToolStarted re-stamps.
            last_command=None,
            last_command_at=None,
            pending_permission_tool=None,
        )

    # ── ToolStarted: PreToolUse → TOOL_USE ────────────────────────────────
    if isinstance(event, ToolStarted):
        return replace(
            prev,
            phase=SessionPhase.TOOL_USE,
            last_hook_at=event.at,
            current_tool=event.tool_name,
            # Plan F: surface the tool's input preview (Bash command,
            # file path, etc.) for the row-level ticker line. May be
            # None when the extractor can't pull a single renderable
            # string out of tool_input.
            current_tool_input=event.tool_input_preview,
            # v4c Phase 3a: stamp the tool-start moment so the snapshot
            # can compute elapsed for "Bash · 1.2s" inline display.
            tool_started_at=event.at,
            # Command-hero (prototype): persist the command + its start time
            # so the active card keeps showing "$ <cmd>" while the model
            # thinks between tool calls. Falls back to the tool name when the
            # extractor couldn't produce a renderable preview, so the hero
            # line is never blank for an active tool. Unlike current_tool_input
            # these are NOT cleared on ToolFinished/THINKING — only a new
            # prompt (below) resets them.
            last_command=event.tool_input_preview or event.tool_name,
            last_command_at=event.at,
            # If a permission was pending, the tool starting means it was
            # resolved (Claude got allow). Clear it.
            pending_permission_tool=None,
        )

    # ── ToolFinished: Post(ToolUse|ToolUseFailure) → THINKING ─────────────
    if isinstance(event, ToolFinished):
        return replace(
            prev,
            phase=SessionPhase.THINKING,
            last_hook_at=event.at,
            current_tool=None,
            current_tool_input=None,
            # Tool ended → clear the start timestamp (the iff invariant
            # is current_tool ↔ tool_started_at).
            tool_started_at=None,
            # If a permission was pending (WAITING_APPROVAL → ToolFinished,
            # e.g. user denied and PostToolUseFailure fired), the cycle is
            # over either way — clear pending to satisfy the iff invariant.
            pending_permission_tool=None,
        )

    # ── TurnCompleted: Stop/StopFailure → IDLE ────────────────────────────
    if isinstance(event, TurnCompleted):
        return replace(
            prev,
            phase=SessionPhase.IDLE,
            last_hook_at=event.at,
            last_assistant_message=event.last_assistant_message,
            current_tool=None,
            current_tool_input=None,
            tool_started_at=None,
            compact_started_at=None,
            pending_permission_tool=None,
        )

    # ── SessionEnded: SessionEnd hook → ENDED ─────────────────────────────
    if isinstance(event, SessionEnded):
        return SessionLiveState(
            session_uuid=prev.session_uuid,
            phase=SessionPhase.ENDED,
            cwd=prev.cwd,
            started_at=prev.started_at,
            last_hook_at=event.at,
            # Overlay fields explicitly NOT carried — ENDED invariant.
            last_prompt=prev.last_prompt,
            last_assistant_message=prev.last_assistant_message,
            jump_target=prev.jump_target,  # preserve for UI context
        )

    # ── PermissionRequested: PermissionRequest → WAITING_APPROVAL ────────
    if isinstance(event, PermissionRequested):
        tool = event.tool_name or "unknown"
        return replace(
            prev,
            phase=SessionPhase.WAITING_APPROVAL,
            last_hook_at=event.at,
            pending_permission_tool=tool,
            # If a tool was in progress, the permission means it stopped
            # mid-call to ask. Clear current_tool to satisfy the iff
            # invariant (phase=WAITING_APPROVAL ⇒ current_tool=None).
            current_tool=None,
            current_tool_input=None,
            tool_started_at=None,
        )

    # ── CompactStarted: PreCompact → COMPACTING ───────────────────────────
    if isinstance(event, CompactStarted):
        return replace(
            prev,
            phase=SessionPhase.COMPACTING,
            last_hook_at=event.at,
            current_tool=None,
            current_tool_input=None,
            tool_started_at=None,
            # v4c Phase 3c: stamp compact start so the snapshot can
            # compute elapsed for "compacting · 8s" inline display.
            compact_started_at=event.at,
            pending_permission_tool=None,
        )

    # ── NotificationFired: never changes phase, just timestamps ──────────
    if isinstance(event, NotificationFired):
        return replace(prev, last_hook_at=event.at)

    # Unknown variant — graceful no-op (only timestamp bump).
    log.warning("unknown HookEvent variant: %r", type(event).__name__)
    return replace(prev, last_hook_at=event.at)


def _synthesize_prev_state(event: HookEvent) -> SessionLiveState:
    """Construct a placeholder SessionLiveState when a non-SessionStarted
    event arrives for an unknown uuid.

    This handles two scenarios:
      1. App restart: hook events keep flowing for sessions whose
         SessionStart fired before we existed.
      2. Hook ordering quirks: occasional misorder between
         SessionStart and the first event after it.

    We can't perfectly reconstruct started_at/cwd, but ``event.at`` and
    ``Path(".")`` are workable placeholders — the real values get
    backfilled by ``HookSessionBridge`` from ``SessionRegistry`` when
    the scanner reports the pid+cwd.
    """
    return SessionLiveState(
        session_uuid=event.session_uuid,
        phase=SessionPhase.IDLE,
        cwd=_UNKNOWN_CWD,
        started_at=event.at,
        last_hook_at=event.at,
    )
