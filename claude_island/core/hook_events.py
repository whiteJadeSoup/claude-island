"""HookEvent variants + SessionLiveState — the typed payloads the state
machine consumes.

Each Claude Code hook event name maps to exactly one variant here. The
mapping happens at the platform boundary (``platform_/hook_server.py``);
core only sees parsed events. Unknown hook event names are dropped at
the boundary, never reach this module.

Why a union of variant dataclasses rather than one mega-dataclass with
Optional fields: every variant has its own required fields and invariants.
A ``ToolStarted`` without ``tool_name`` is meaningless; making the field
Optional would push that invariant out to every consumer. Variants keep
the contract precise — the type checker enforces "you can't read
``tool_name`` off a ``SessionStarted``" because that field doesn't exist
there.

All payloads frozen + slotted: WorldSnapshot needs structural equality
for distinct_until_changed dedup (see CLAUDE.md design principle 7).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Protocol, Union

from .session_phase import SessionPhase


# ---------------------------------------------------------------------------
# JumpTarget — terminal-identifying metadata captured at SessionStart hook
# time, used at click time to skip syscalls and disambiguate tabs.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class JumpTarget:
    """Snapshot of where a Claude session lives at hook time.

    Filled in by the hook script (running INSIDE the target claude.exe
    process) and persisted on SessionLiveState. The click handler reads
    this to navigate without re-querying the OS at click time.

    Modeled after open-vibe-island's ``JumpTarget`` (Sources/OpenIslandCore/
    AgentSession.swift). They store per-terminal-app fields like
    ``warpPaneUUID``, ``terminalSessionID``, ``terminalTTY`` — we mirror
    the structure (terminal_app + per-app identifiers) so adding a new
    terminal app later means one new field and one new adapter branch,
    not a refactor.

    All fields are optional/Nullable to survive:
      * old hook payloads (no jump_target at all)
      * partial captures (hook ran but a sub-call failed)
      * cross-platform variation (WT-only fields are None on macOS)

    Identification of WHICH tab a session lives in is approximate — WT
    doesn't expose conhost ↔ TabItem mapping publicly, so even with
    full JumpTarget we may not always be able to auto-navigate. But
    storing the data lets the adapter use the best available signal
    at click time, and lets the UI render context info (which terminal
    app hosts which session) without per-click syscalls.
    """

    # Which terminal app hosts this session. Derived from TERM_PROGRAM
    # env, fallback to "unknown". Examples: "WindowsTerminal",
    # "ConsoleHost", "iTerm.app", "Terminal".
    terminal_app: str | None = None

    # The conhost (PseudoConsoleWindow) hwnd that owns this session.
    # On Windows, this is what GetConsoleWindow() returns for the
    # target process. 0 when unknown.
    conhost_hwnd: int = 0

    # PID of the **claude process itself** at hook time. NOT the
    # hosting terminal's pid (see ``terminal_pid`` for that). The
    # ``hook_session_bridge`` uses this as the canonical pid for
    # the SessionRegistry entry before the scanner catches up; the
    # WT adapter uses it as a sanity check against ``Session.pid``.
    host_pid: int = 0

    # Windows Terminal pane GUID (WT 1.18+ env var WT_SESSION).
    # Empty string when not in WT or older versions. Stable per-pane
    # identifier even when titles drift.
    wt_session_guid: str = ""

    # Verbatim TERM_PROGRAM env var. Used by the wiring layer to
    # pick the right adapter at click time.
    term_program: str = ""

    # iTerm2's stable per-session id, captured at hook time via
    # ``tell application "iTerm" to id of session whose tty is …``.
    # When non-empty, the iTerm2 adapter matches by id instead of by
    # tty in the focus AppleScript — id is stable across reconnects
    # and process restarts that tty isn't. Empty when the session
    # doesn't run inside iTerm2, the hook ran before iTerm was
    # scriptable, or AppleScript permission was denied.
    iterm_session_id: str = ""

    # PID of the hosting terminal application (iTerm2.app process,
    # WindowsTerminal.exe, etc.) — NOT the claude pid (see
    # ``host_pid``). Captured at hook time by walking the claude
    # process's parent chain. Used at click time to disambiguate
    # when multiple instances of the same terminal app are running
    # (e.g. two iTerm2 installs with the same bundle id). 0 when
    # the parent walk found no recognised terminal ancestor.
    terminal_pid: int = 0


# ---------------------------------------------------------------------------
# Event variants — one per Claude hook_event_name we consume.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SessionStarted:
    """Claude Code's ``SessionStart`` hook.

    ``source`` reflects how the session began: ``startup`` (fresh start),
    ``resume`` (claude --resume), ``clear`` (/clear command), or
    ``compact`` (after a /compact — note: same session_uuid as before
    the compact, so this is treated as a phase transition, not a new
    session).

    ``jump_target`` is the terminal-identifying metadata captured by
    the hook script running INSIDE the claude.exe process. Optional;
    None when the hook is from an older claude-island version (forward
    compat) or when capture itself failed."""
    session_uuid: str
    cwd: Path
    started_at: datetime
    source: str | None
    transcript_path: Path | None
    at: datetime
    jump_target: JumpTarget | None = None


@dataclass(frozen=True, slots=True)
class PromptSubmitted:
    """Claude Code's ``UserPromptSubmit`` hook. Fires when the user hits
    enter. ``prompt`` is truncated to 200 characters at the boundary to
    bound payload size and limit how much PII flows through the pipe."""
    session_uuid: str
    prompt: str
    at: datetime


@dataclass(frozen=True, slots=True)
class ToolStarted:
    """Claude Code's ``PreToolUse`` hook. ``tool_input_preview`` extracts
    the most relevant single value from ``tool_input`` (the command
    string, file path, glob pattern, etc.) — full input dict would be
    too noisy to render in the UI row."""
    session_uuid: str
    tool_name: str
    tool_input_preview: str | None
    tool_use_id: str | None
    at: datetime


@dataclass(frozen=True, slots=True)
class ToolFinished:
    """Claude Code's ``PostToolUse`` (is_failure=False) or
    ``PostToolUseFailure`` (is_failure=True) hook."""
    session_uuid: str
    tool_name: str
    tool_use_id: str | None
    is_failure: bool
    at: datetime


@dataclass(frozen=True, slots=True)
class TurnCompleted:
    """Claude Code's ``Stop`` (is_failure=False) or ``StopFailure``
    (is_failure=True) hook. The CLI is still alive after Stop — only
    the current turn is done. SessionEnded is the real exit signal."""
    session_uuid: str
    last_assistant_message: str | None
    is_failure: bool
    at: datetime


@dataclass(frozen=True, slots=True)
class SessionEnded:
    """Claude Code's ``SessionEnd`` hook. The CLI is exiting. The state
    machine sets phase=ENDED but does NOT remove the entry — that is
    HookSessionBridge's responsibility, and only after scanner also
    confirms the process is gone (handles the case where SessionEnd
    arrives but the user just /clear-ed and the same pid is about to
    SessionStart again)."""
    session_uuid: str
    at: datetime


@dataclass(frozen=True, slots=True)
class PermissionRequested:
    """Claude Code's ``PermissionRequest`` hook. **v1 BLOCKS** on this
    after the v5 swap (see ``hook.py`` __version__): the hook server
    registers a pending decision and waits up to ~600 s for the user
    to allow / deny via the inline approval card. Fail-open: timeout
    returns ``defer`` and Claude falls back to its built-in terminal
    prompt. See ``hook_server._handle_permission_request``."""
    session_uuid: str
    tool_name: str | None
    at: datetime


@dataclass(frozen=True, slots=True)
class CompactStarted:
    """Claude Code's ``PreCompact`` hook. Transitions phase=COMPACTING
    until the SessionStart(source='compact') marks the end."""
    session_uuid: str
    at: datetime


@dataclass(frozen=True, slots=True)
class NotificationFired:
    """Claude Code's ``Notification`` hook. Used by Claude for idle
    prompts and similar. ``is_idle`` is True when the notification
    type/subtype indicates an idle/away hint — those should NOT
    escalate phase to RUNNING (notifications are informational, not
    activity)."""
    session_uuid: str
    is_idle: bool
    at: datetime


# Union of all variants. The state machine accepts this and dispatches
# by isinstance — verbose but type-safe and explicit.
HookEvent = Union[
    SessionStarted,
    PromptSubmitted,
    ToolStarted,
    ToolFinished,
    TurnCompleted,
    SessionEnded,
    PermissionRequested,
    CompactStarted,
    NotificationFired,
]


# ---------------------------------------------------------------------------
# SessionLiveState — the per-session state the machine produces.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SessionLiveState:
    """Per-session state derived from hook events. Immutable snapshot.

    The state machine produces new SessionLiveState via
    ``dataclasses.replace()`` — never mutate in place, that's how
    ``distinct_until_changed`` silently breaks (the reference stays
    the same but content changes, so structural equality returns True
    when it shouldn't).

    Invariants checked in ``__post_init__``:

      * phase == TOOL_USE          ⇔ current_tool is not None
      * phase == WAITING_APPROVAL  ⇔ pending_permission_tool is not None
      * phase == ENDED             ⇒ current_tool is None
                                      AND pending_permission_tool is None

    On violation, ``AssertionError`` is raised. The state machine catches
    it in ``apply()`` and tombstones the session — fail-soft so a bug
    here doesn't kill the whole app.
    """
    session_uuid: str
    phase: SessionPhase
    cwd: Path
    started_at: datetime
    last_hook_at: datetime
    current_tool: str | None = None
    last_prompt: str | None = None
    last_assistant_message: str | None = None
    pending_permission_tool: str | None = None
    # Terminal-identifying metadata captured at SessionStart hook time.
    # None when the hook didn't ship one (older hook.py, capture failure,
    # or session arrived to state machine via non-SessionStart events).
    jump_target: JumpTarget | None = None

    def __post_init__(self) -> None:
        # ENDED clears overlays checked FIRST because the next two iff
        # checks would also flag the same bug but with a misleading
        # error message (e.g. "phase=TOOL_USE iff current_tool!=None"
        # firing when the real problem is "ENDED leftover field").
        if self.phase == SessionPhase.ENDED:
            if self.current_tool is not None:
                raise AssertionError(
                    f"phase=ENDED requires current_tool=None, got "
                    f"current_tool={self.current_tool!r}"
                )
            if self.pending_permission_tool is not None:
                raise AssertionError(
                    f"phase=ENDED requires pending_permission_tool=None, got "
                    f"pending={self.pending_permission_tool!r}"
                )
            return
        # Each remaining invariant is independently checked so the error
        # message tells you which one failed (a single big assert with
        # `and` would only report "True is not False").
        if (self.phase == SessionPhase.TOOL_USE) != (self.current_tool is not None):
            raise AssertionError(
                f"phase=TOOL_USE iff current_tool!=None violated: "
                f"phase={self.phase}, current_tool={self.current_tool!r}"
            )
        if (self.phase == SessionPhase.WAITING_APPROVAL) != (
            self.pending_permission_tool is not None
        ):
            raise AssertionError(
                f"phase=WAITING_APPROVAL iff pending_permission_tool!=None violated: "
                f"phase={self.phase}, pending={self.pending_permission_tool!r}"
            )


# ---------------------------------------------------------------------------
# Protocol for compose_session_view's live_state lookup callback.
# Kept in this module rather than snapshot.py to keep the live-state
# vocabulary in one place.
# ---------------------------------------------------------------------------


class LiveStateProto(Protocol):
    """Callable that returns the current SessionLiveState for a uuid,
    or None when no hook events have been observed for it.

    Production implementation: ``SessionStateMachine.read``.
    Test fakes: just a lambda or a dict.get bound method.
    """

    def __call__(self, uuid: str) -> SessionLiveState | None: ...
