"""WorldSnapshot broadcast — the single source of truth for UI state.

The architecture this file establishes:

    Snapshotter (worker thread, lives in this module)
        ↓ builds
    WorldSnapshot (frozen dataclass, this module)
        ↓ pushed via injected `publish` callback
    _WorldStore (BehaviorSubject wrapper, this module's `world` singleton)
        ↓ subscribed by
    capsule.render(snap), expanded.render(snap), ... (UI layer)

What lives here vs what lives elsewhere:

* This module is **pure Python** — it imports ``reactivex`` (also pure
  Python) but NOT PySide6 / Qt / OS-specific libs. The import-linter
  contract in pyproject.toml allows reactivex through the "no UI
  framework" rule explicitly.

* Cross-thread marshaling (worker → Qt main) does NOT happen here — it
  happens in the wiring layer via a small QObject + ``Qt.QueuedConnection``
  Signal. Snapshotter accepts a ``publish`` callback so the wiring layer
  can inject the marshaler's signal-emit; the default is ``world.push``
  for tests that don't need cross-thread marshaling.

* Snapshotter's worker thread is owned by reactivex's
  ``EventLoopScheduler`` — it is opaque to callers. ``start()`` /
  ``stop()`` are the lifecycle hooks; ``wake()`` is the only signal
  external code needs to send.

Why a singleton ``world`` instead of injection: the whole point of
"single source of truth" is *one* truth. Making it injected fights
the pattern. Test isolation comes from ``world.reset_for_testing()``,
called by an ``autouse`` fixture in ``tests/conftest.py``.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, NamedTuple, Protocol

import reactivex.operators as ops
from reactivex import Observable, abc
from reactivex.disposable import Disposable
from reactivex.scheduler import EventLoopScheduler
from reactivex.subject import BehaviorSubject, Subject

from .capabilities import Capability, FocusGranularity
from .hook_events import JumpTarget, LiveStateProto, SessionLiveState
from .launch_intent import LaunchIntent
from .models import DormantSession, QuotaSnapshot, Session
from .notify import NotifyEvent
from .pending_decisions import PendingDecisionView
from .session_phase import SessionPhase

log = logging.getLogger(__name__)


# Spend threshold (USD) above which a session is flagged "high cost"
# and the cost label paints in the warning colour. Lifted from the UI
# layer constant of the same name so SessionView can pre-resolve it
# (the UI no longer needs to recompute the threshold per render).
HIGH_COST_USD_THRESHOLD = 50.0

# Activity heuristic: when SessionDetails.status is unknown (provider
# without a state file), fall back to "JSONL was written within this
# many seconds = running". 30 s matches what the previous capsule and
# row code used independently — consolidating here so they can't drift.
ACTIVE_THRESHOLD_SECONDS = 30.0


@dataclass(frozen=True, slots=True)
class SessionView:
    """A single session, fully resolved for display.

    Every field that drove a per-render computation in the old code
    (``is_running`` priority chain, ``is_high_cost`` threshold check,
    name resolution) is pre-resolved here so the UI render is pure
    "draw what's in the snapshot" — no policy logic.

    ``frozen=True`` makes ``__eq__`` structural — two SessionViews are
    equal iff every field is equal — which lets ``distinct_until_changed``
    skip no-op snapshots without us writing any custom comparison.

    ``phase`` is the single source of truth for "what is this session
    doing right now". When the hook pipeline is connected, phase comes
    directly from ``SessionStateMachine.read(uuid).phase``. When the
    hook pipeline is offline (session started before the listener was
    bound, or no listener at all), phase is derived from the pid.json
    ``status`` field + a 30-second activity heuristic. UI MUST treat
    the two sources identically — no "degraded" badge or visual hint
    is shown.

    ``is_running`` is kept as a backwards-compat ``@property`` derived
    from phase so existing UI code that reads ``view.is_running``
    continues to work without churn. New UI code should switch on
    ``phase`` directly.
    """

    pid: int
    name: str                       # already resolved (custom > state > ai > basename)
    project_path: Path
    project_basename: str           # convenience for sort keys
    last_activity: datetime         # tz-aware UTC
    cost_usd: float                 # >= 0
    is_high_cost: bool              # == (cost_usd >= HIGH_COST_USD_THRESHOLD)
    latest_model: str | None        # None when no records yet
    status_word: str | None         # raw "busy" / "idle" / "waiting" / None
    # The original Session object the view was composed from. Carried
    # along so UI callbacks that accept a Session (e.g. row click
    # dispatch, the row's _siblings list) don't need to reconstruct
    # one. Frozen like everything else here — once the snapshot is
    # built, the ``session`` reference is stable for the lifetime of
    # the snapshot.
    session: Session
    # Resolved session UUID — sourced from ~/.claude/sessions/<pid>.json's
    # ``sessionId`` field with fallback to ``session.session_uuid``.
    # ProcessScanner can't fill in the uuid (it'd need to read the
    # transcript), so ``session.session_uuid`` is empty in nearly every
    # session built from a fresh scan. compose_session_view does the
    # state-file lookup once and pins the result here so capability
    # backends (RENAME / RESET_THINKING) and the popup all read from
    # one canonical place instead of re-running the resolution.
    # Empty string when no uuid could be resolved (transcript not
    # written yet); backends treat empty as "skip, no-op".
    session_uuid: str = ""
    # ── Capability framework fields ─────────────────────────────────
    # Frozen set of capabilities the user can trigger on this view.
    # Computed at group time = (terminal adapter caps for this view) ∪
    # (os backend caps) ∪ (app backend caps). UI checks membership to
    # decide which buttons to render; dispatcher uses it as a defensive
    # gate before routing.
    capabilities: frozenset[Capability] = frozenset()
    # How precise the FOCUS capability gets for this view. APP means
    # "best we can do is raise the host application"; PANE means we
    # can land directly on the right split. Only meaningful when
    # FOCUS ∈ capabilities. Defaults to APP for backward-compat with
    # any code path that constructs SessionView without an adapter.
    focus_granularity: FocusGranularity = FocusGranularity.APP
    # Opaque token identifying which TerminalAdapter created this view.
    # The dispatcher uses it to look up the adapter when dispatching
    # TERMINAL-scope capabilities. UI MUST NOT parse this string — its
    # value is internal to the platform layer (e.g. "windows-terminal",
    # "iterm2", "generic-mac"). Empty default exists so tests that
    # construct a SessionView without an adapter still validate.
    adapter_id: str = ""
    # ── Hook-derived state (Step 7) ─────────────────────────────────
    # Phase — the live activity state. Default IDLE for backward compat
    # with old tests that construct SessionView without specifying phase.
    phase: SessionPhase = SessionPhase.IDLE
    # Tool currently in use (only when phase == TOOL_USE, otherwise None).
    # Populated from SessionLiveState.current_tool.
    current_tool: str | None = None
    # Plan F (2026-05-24): tool-input preview for the row's third-line
    # "ticker" — Bash command line, file path being read, etc. Sourced
    # from ``SessionLiveState.current_tool_input`` which forwards
    # ``ToolStarted.tool_input_preview``. Invariant parallels the live
    # state: non-None only when ``phase == TOOL_USE``. May be None even
    # in TOOL_USE when the hook's extractor couldn't pull a single
    # renderable string out of tool_input.
    current_tool_input: str | None = None
    # Latest user prompt seen on this session (truncated to 200 chars at
    # the hook boundary). UI uses this as the row preview text.
    last_prompt: str | None = None
    # Latest assistant message — set when phase transitions IDLE via
    # TurnCompleted. UI uses for tooltip / detail popup.
    last_assistant_message: str | None = None
    # Terminal-identifying metadata captured at hook time (open-vibe-island
    # JumpTarget pattern). None when no hook coverage or capture failed.
    # The WT click handler uses this to skip syscalls at click time.
    jump_target: JumpTarget | None = None
    # True iff compose_session_view found a SessionLiveState in the
    # state machine for this uuid (the hook bridge has acknowledged
    # this session). Used by the live-list staleness filter to keep
    # hook-known placeholders (pid<=0 + hook saw it) while dropping
    # ghost placeholders (pid<=0 + state machine empty). Distinct from
    # ``phase`` because phase defaults to IDLE for both cases — only
    # this flag tells them apart.
    has_live_state: bool = False
    # ── Turn count (v4c row inline status) ──────────────────────────
    # Number of assistant turns in this session's transcript. Sourced
    # from ``usage_registry.get_session_summary``'s second return value
    # (the same number SessionDetails surfaces in the popup).  UI uses
    # it to render `· turn N` next to "thinking" and `· N turns` next
    # to "ended", mirroring prototype-v4c-github.html's status strings.
    # Default 0 keeps legacy degraded views compiling.
    turn_count: int = 0
    # ── Phase elapsed timestamps (v4c Phase 3) ──────────────────────
    # Seconds elapsed inside the current phase, populated only when
    # the row is in the matching phase.  Snapshot worker computes
    # ``now - live.tool_started_at`` once per build and freezes the
    # float here so the UI doesn't have to re-read the clock per
    # render.  Both fields are None when:
    #   - phase ≠ matching phase (TOOL_USE / COMPACTING)
    #   - state machine never saw the corresponding hook event
    # which gracefully degrades the row to no inline elapsed display.
    tool_elapsed_s: float | None = None
    compact_elapsed_s: float | None = None
    # v4c Phase 3b: rolling token-rate (input + output tokens / min)
    # over the last ~60 s.  Populated only when there's recent usage;
    # None when the session has been silent for over a minute, so the
    # UI can hide the rate label cleanly instead of showing "0 tk/min"
    # next to a stalled session.
    tokens_per_min: int | None = None
    # ── Command-hero (active card "$ <cmd>" line) ───────────────────────
    # The most recent command this session ran, PERSISTED across phases
    # (unlike current_tool_input, which is gated to TOOL_USE). Lets the
    # active card keep its hero line while the model thinks between tool
    # calls — matching the prototype where a "thinking" card still shows
    # "$ writing …". None until the session runs its first tool, or after
    # a new prompt resets the turn. No phase invariant binds these.
    last_command: str | None = None
    last_command_elapsed_s: float | None = None

    def __post_init__(self) -> None:
        # Self-consistency invariant — guards against the UI and the
        # Snapshotter drifting on what counts as "high cost".
        assert self.is_high_cost == (self.cost_usd >= HIGH_COST_USD_THRESHOLD), (
            f"SessionView invariant violated: cost_usd={self.cost_usd}, "
            f"is_high_cost={self.is_high_cost}"
        )
        # Plan F invariant — current_tool_input non-None ⇒ phase=TOOL_USE.
        # Mirrors SessionLiveState.__post_init__; protects the UI from
        # accidentally getting a tool-input string on a non-TOOL_USE row
        # (which would then render a stale ticker line).
        assert (
            self.current_tool_input is None
            or self.phase == SessionPhase.TOOL_USE
        ), (
            f"SessionView invariant violated: current_tool_input set "
            f"but phase={self.phase!r}, "
            f"current_tool_input={self.current_tool_input!r}"
        )

    @property
    def is_running(self) -> bool:
        """Derived: True iff phase is any active state.

        Backwards-compat shim — UI code that pre-dates the phase field
        keeps reading ``view.is_running`` and gets a sensible answer.
        New UI code should switch on ``phase`` directly to render
        finer-grained states (TOOL_USE → show tool chip; THINKING →
        spinner; etc.).
        """
        return self.phase.is_active()


@dataclass(frozen=True, slots=True)
class SessionGroup:
    """A group of SessionViews that should render as one card.

    Grouping is decided by the TerminalAdapter that emitted these
    views (e.g. "all sessions in the same WT window" or "all sessions
    in the same iTerm2 tab"). UI just renders one card per group,
    iterating ``views`` for inner rows. UI never decides grouping.

    group_id: Adapter-internal stable id (e.g. ``f"wt:{wt_hwnd}"`` or
        ``f"iterm:{window_id}:{tab_id}"``). Stable across snapshots so
        UI can keep DOM-equivalent identity (Qt widget reuse, fade
        animations) when the same group reappears.
    title_hint: Optional human-readable hint the adapter wants the UI
        to display as the card title (None ⇒ UI picks its own — e.g.
        first view's project basename).
    adapter_id: The terminal adapter that owns this group. Same value
        as every contained view's ``adapter_id``.
    views: Non-empty tuple of SessionViews in this group.
    """
    group_id: str
    title_hint: str | None
    adapter_id: str
    views: tuple[SessionView, ...]


@dataclass(frozen=True, slots=True)
class WorldSnapshot:
    """The full state of the application at one instant.

    Everything any UI surface needs to render itself is in this object.
    The UI takes a snapshot, renders, and forgets — there is no other
    state to consult.

    ``sessions`` is a tuple (not list) because:
      1. Frozen dataclasses can't hold mutable defaults — tuple is safe
      2. ``==`` on tuples is element-wise, which makes
         ``distinct_until_changed`` work without custom comparators
      3. Order is meaningful (already sorted by Snapshotter) — tuple
         preserves order
    """

    today_cost_usd: float
    quota: QuotaSnapshot | None
    available_providers: tuple[str, ...]
    selected_provider: str | None
    fetched_at: datetime
    # Session views, pre-grouped by the TerminalDispatcher's adapter chain.
    # Each group renders as one card; inner views are the rows.
    # Empty tuple = no sessions known yet (boot / empty machine).
    session_groups: tuple["SessionGroup", ...]
    # Offline sessions (have a JSONL transcript on disk but no live process).
    # Built from JsonlParser._session_meta + UsageRegistry by
    # DormantSessionSource. Snapshotter reconciles: any uuid that's also
    # live or launching is filtered out here. UI renders these in the
    # RecentsDrawer; never in the main capsule/expanded panel.
    dormant_sessions: tuple[DormantSession, ...] = ()
    # Sessions the user just hit Resume on; we've spawned a terminal but
    # ProcessScanner hasn't yet detected the new claude.exe. Lives at most
    # ttl seconds (default 30) inside LaunchIntentRegistry; reconcile
    # discards on upgrade-to-live or on timeout. UI renders these as
    # disabled rows with a ⏳ Launching… affordance.
    launching_sessions: tuple[LaunchIntent, ...] = ()
    # Bidirectional-hook payloads (Bidirectional Hooks v1, 2026-05-14).
    # ``pending_decisions`` is a snapshot of the PendingDecisionRegistry
    # — what the user must approve / review. Empty tuple = nothing to
    # decide. UI renders ApprovalCard (PRE_TOOL_USE) /
    # PromptReviewCard (USER_PROMPT_SUBMIT) per item.
    pending_decisions: tuple[PendingDecisionView, ...] = ()
    # ``notify_events`` is a 60 s rolling window of fired notifications
    # — Stop / StopFailure mostly. Rolling (not consume-and-clear) so a
    # snapshot rebuild race can't drop a notification mid-flight; the
    # NotificationDispatcher dedups via its own _dispatched_ids set.
    notify_events: tuple[NotifyEvent, ...] = ()

    @classmethod
    def empty(cls) -> "WorldSnapshot":
        """Initial value for the BehaviorSubject — UI must safely
        render this without errors. Represents "we haven't built any
        snapshot yet"."""
        return cls(
            today_cost_usd=0.0,
            quota=None,
            available_providers=(),
            selected_provider=None,
            fetched_at=datetime.fromtimestamp(0, tz=timezone.utc),
            session_groups=(),
            dormant_sessions=(),
            launching_sessions=(),
            pending_decisions=(),
            notify_events=(),
        )

    # ``render_key`` removed (F4): UI dedup is now per-surface — each
    # surface declares what it cares about via its own ``compute(snap)``
    # function, and ``distinct_until_changed`` is keyed on that
    # projection. The previous global ``render_key`` over-coupled all
    # surfaces to every SessionView field (including microsecond
    # ``last_activity`` ticks), which silently defeated dedup during
    # active sessions. See ``ui/capsule_window.compute`` /
    # ``ui/expanded_window.compute`` for the per-surface declarations.


class _WorldStore:
    """The single global ``BehaviorSubject[WorldSnapshot]`` — sole place
    UI subscribes to and the sole place Snapshotter pushes to.

    Wrapped in a class (not a bare module-level Subject) so we can:
      * Hide ``on_next`` from accidental external callers — they get
        only ``observable()`` (read view) and ``push()`` (intentional
        write). Same shape as a getter/setter pair.
      * Reset cleanly between tests by disposing + rebuilding the
        underlying Subject.
      * Add invariant checks or telemetry later without touching call
        sites.
    """

    def __init__(self) -> None:
        self._subject: BehaviorSubject[WorldSnapshot] = BehaviorSubject(
            WorldSnapshot.empty()
        )

    def push(self, snap: WorldSnapshot) -> None:
        """Publish a new snapshot. Synchronously notifies every
        subscriber on the calling thread.

        Thread safety: BehaviorSubject's internal mutex serializes
        concurrent ``on_next`` calls, so push() is safe to call from
        any thread. The thread that calls push determines the thread
        each subscriber's callback runs on — the wiring layer is
        responsible for ensuring this is the Qt main thread (via a
        QueuedConnection marshaler) before any UI subscriber is wired
        up."""
        self._subject.on_next(snap)

    def observable(self) -> Observable[WorldSnapshot]:
        """Read-only view for subscribers. The returned Observable is
        actually the BehaviorSubject upcast — subscribers can ``pipe``
        and ``subscribe`` but cannot push."""
        return self._subject

    @property
    def current(self) -> WorldSnapshot:
        """Synchronous read of the current snapshot. Useful for tests
        and for any code path that wants the latest state without
        subscribing (e.g. a debug command)."""
        return self._subject.value

    def reset_for_testing(self) -> None:
        """Dispose the existing Subject and create a fresh one.

        Called by ``tests/conftest.py``'s autouse fixture between tests
        so subscribers from one test don't leak into the next. Note:
        any code holding the OLD ``observable()`` reference will stop
        receiving updates after this call — that's intentional, it
        forces test setups to re-subscribe for each test rather than
        carrying state across the boundary."""
        self._subject.dispose()
        self._subject = BehaviorSubject(WorldSnapshot.empty())


# Module-level singleton. Importers do `from claude_island.core.snapshot
# import world` and use `world.push(...)` / `world.observable()`.
world = _WorldStore()


# ---------------------------------------------------------------------------
# Dependency protocols — Snapshotter accepts these instead of concrete
# classes so tests can inject minimal fakes. The duck-type shape is what
# matters; any object that exposes these methods works.
# ---------------------------------------------------------------------------

class _SessionSourceProto(Protocol):
    """Anything that exposes ``sessions`` as a snapshot list. In the
    real wiring this is ``SessionRegistry`` — it already merges raw
    process scans with JSONL-derived ``last_activity`` overrides, so
    consuming from here means the snapshot reflects the freshest
    activity timestamp without us re-running the process scan."""
    @property
    def sessions(self) -> list[Session]: ...


class _StateReaderProto(Protocol):
    def read_session_state(self, pid: int) -> dict | None: ...


class _MetadataProviderProto(Protocol):
    def get_session_metadata(self, uuid: str) -> dict | None: ...


class _UsageRegistryProto(Protocol):
    def get_session_summary(self, uuid: str) -> tuple[float, int, int]: ...
    def get_latest_model(self, uuid: str) -> str | None: ...
    def get_totals(self, period: str): ...  # returns UsageTotals


class _NamesStoreProto(Protocol):
    def get_session_name(self, uuid: str) -> str | None: ...


# Callable signature for the platform-side grouping function. Snapshotter
# accepts an instance and invokes it on every build to convert the flat
# sessions list into pre-grouped SessionGroups. Default implementation
# (``_default_group_sessions``) emits one singleton group per session.
# Production wires in ``TerminalDispatcher.group_sessions`` from the
# platform layer, which routes through the adapter chain.
class _GroupSessionsProto(Protocol):
    def __call__(self, views: list["SessionView"]) -> list["SessionGroup"]: ...


class _DormantSourceProto(Protocol):
    """Protocol for anything that lists offline-from-disk sessions.
    Production: claude_island.core.dormant_source.DormantSessionSource."""
    @property
    def sessions(self) -> list[DormantSession]: ...


class _LaunchIntentProto(Protocol):
    """Protocol for the LaunchIntentRegistry. Snapshotter uses two methods:
    reconcile() to prune upgraded/timed-out intents, snapshot() to read
    what's left for the WorldSnapshot.launching_sessions field."""

    def reconcile(self, *, live_uuids: set[str], now: datetime) -> None: ...
    def snapshot(self) -> tuple[LaunchIntent, ...]: ...


class _PendingDecisionProto(Protocol):
    """Snapshotter reads ``snapshot()`` to drop the current pending
    decisions into WorldSnapshot. Production wires in
    PendingDecisionRegistry; tests pass any object with this shape or
    leave it None to disable the feature."""

    def snapshot(self) -> tuple[PendingDecisionView, ...]: ...


class _NotifyQueueProto(Protocol):
    """Snapshotter reads ``snapshot()`` for the rolling 60 s window of
    notify events to publish in WorldSnapshot.notify_events."""

    def snapshot(self) -> tuple[NotifyEvent, ...]: ...


def _default_group_sessions(views: list["SessionView"]) -> list["SessionGroup"]:
    """Fallback grouping: one SessionGroup per view, with empty
    adapter_id and no title hint.

    Used when no real grouper is injected (most tests, and the boot
    sequence before ``__main__`` builds the dispatcher). UI renders
    each as a singleton card. Capabilities on the views are untouched
    (whatever the view came in with stays)."""
    return [
        SessionGroup(
            group_id=f"singleton:{v.pid}",
            title_hint=None,
            adapter_id=v.adapter_id,
            views=(v,),
        )
        for v in views
    ]


# ---------------------------------------------------------------------------
# compose_session_view — moved from __main__._build_session_details
# ---------------------------------------------------------------------------

def _noop_live_state(_: str) -> SessionLiveState | None:
    """Default live_state_reader for tests and boot before hook pipeline.
    Always returns None → phase resolution falls through to pid.json
    + activity heuristic."""
    return None


def compose_session_view(
    session: Session,
    *,
    state_reader: _StateReaderProto,
    metadata_provider: _MetadataProviderProto,
    usage_registry: _UsageRegistryProto,
    names_store: _NamesStoreProto,
    live_state_reader: LiveStateProto = _noop_live_state,
    high_cost_threshold: float = HIGH_COST_USD_THRESHOLD,
    active_threshold_s: float = ACTIVE_THRESHOLD_SECONDS,
) -> SessionView:
    """Compose a fully-resolved ``SessionView`` from one ``Session`` plus
    the data sources.

    Phase resolution is layered (most precise first):

      1. ``live_state_reader(uuid)`` returns a non-None SessionLiveState
         whose phase is not ENDED → use hook-derived phase + overlays
         (current_tool / last_prompt / last_assistant_message).
      2. Falls back to ``pid.json`` ``status`` field mapped via
         ``_phase_from_pid_json`` (busy→THINKING, waiting→WAITING_APPROVAL,
         idle→IDLE).
      3. Falls back to activity-timestamp heuristic (last_activity within
         ``active_threshold_s`` → THINKING, else IDLE).

    UI receives identical SessionPhase values from all three sources —
    no "degraded" marker. The internal layering exists so a session
    that started before claude-island was running (no hook events) still
    renders something reasonable.

    Never raises — all dependency exceptions are caught per-source so a
    single corrupted JSON file or transient read error degrades a single
    field rather than failing the whole compose.
    """
    state = _safe(state_reader.read_session_state, session.pid) or {}
    # Priority for the canonical session uuid (used for UsageRegistry
    # lookups, live state lookups, and the WT focus sentinel):
    #
    #   1. ``pid.json`` ``sessionId``. claude.exe rewrites this on every
    #      status transition, so it always reflects the in-memory current
    #      session — including the NEW uuid after ``/clear`` /
    #      ``/resume <other>``. Matches the JSONL file claude is actually
    #      appending to (UsageRegistry's index key).
    #   2. ``session.session_uuid`` — populated by the hook bridge from
    #      the SessionStart payload. Same uuid as pid.json for any
    #      session island has observed via hooks; covers the brief
    #      window where pid.json hasn't been written yet.
    pid_json_uuid = (
        state.get("sessionId")
        if isinstance(state.get("sessionId"), str)
        else None
    )
    sess_uuid = pid_json_uuid or session.session_uuid
    meta = _safe(metadata_provider.get_session_metadata, sess_uuid) or {}

    cost, turns, _sides = _safe_or(
        lambda: usage_registry.get_session_summary(sess_uuid),
        default=(0.0, 0, 0),
    )

    latest_model = (
        _safe(usage_registry.get_latest_model, sess_uuid) if sess_uuid else None
    )

    custom_name = _safe(names_store.get_session_name, sess_uuid or "")
    state_name = state.get("name") if isinstance(state.get("name"), str) else None
    name = (
        custom_name
        or state_name
        or meta.get("ai_title")
        or session.project_path.name
        or str(session.project_path)
    )

    status_word = state.get("status") if isinstance(state.get("status"), str) else None

    # Per-uuid JSONL activity supersedes the scanner's baseline (process
    # create_time). The scanner can only observe process-level facts; the
    # JSONL transcript is the source of truth for "is this session
    # actively producing turns?". Keyed by session_uuid via the parser's
    # _session_meta, so two sessions sharing a cwd don't pollute each
    # other (the bug the project-keyed activity override used to cause).
    meta_last = meta.get("last_activity")
    last_activity = (
        meta_last
        if isinstance(meta_last, datetime) and meta_last > session.last_activity
        else session.last_activity
    )

    # ── phase resolution: hook > pid.json > activity heuristic ──
    live = _safe(live_state_reader, sess_uuid) if sess_uuid else None

    if live is not None and live.phase != SessionPhase.ENDED:
        # Fold the hook stream's last_hook_at into last_activity. This
        # is the freshest "session is alive" signal we have — every
        # hook event (PreToolUse, PostToolUse, Stop, even Notification)
        # bumps it, even when no JSONL line is written (e.g. between
        # turns while the user is reading). Without folding this in,
        # the live-list staleness filter would drop a current Claude
        # session whose hook keeps firing but whose JSONL was last
        # appended >30 min ago — bug observed 2026-05-16 (live list
        # went empty after the turn-boundary IDLE transition).
        try:
            if live.last_hook_at > last_activity:
                last_activity = live.last_hook_at
        except (TypeError, AttributeError):
            pass

        # Cross-reference with pid.json: if claude itself reports the
        # session as idle, trust that over a potentially-stale active
        # hook phase. The hook chain can lose its closing event
        # (PostToolUse / Stop dropped after a POST timeout, app restart
        # between Pre and Post, an API error mid-turn that prevents
        # Stop from firing), which would otherwise pin the phase at
        # THINKING / TOOL_USE / WAITING_APPROVAL / COMPACTING forever.
        # pid.json is written by claude on every status transition, so
        # an "idle" reading is authoritative — fall back to a clean
        # IDLE view.
        #
        # COMPACTING was previously excluded under the assumption that
        # SessionStart(source='compact') would always close the
        # compact cycle. User report 2026-05-23 broke that assumption:
        # ``/compact`` with "Not enough messages to compact" emits
        # PreCompact but errors before spawning a new session, so no
        # SessionStart ever fires. Direct probe of pid.json in that
        # state confirmed Claude writes ``status='idle'`` once back
        # at the prompt — same signal as the other stuck-phase cases,
        # so the same override applies.
        _idle_override_phases = (
            SessionPhase.THINKING,
            SessionPhase.TOOL_USE,
            SessionPhase.WAITING_APPROVAL,
            SessionPhase.COMPACTING,
        )
        if status_word == "idle" and live.phase in _idle_override_phases:
            log.info(
                "idle-override: live.phase=%s → IDLE for uuid=%s "
                "(pid.json reports idle; assume hook chain dropped "
                "its closing event)",
                live.phase.value, sess_uuid[:8],
            )
            phase = SessionPhase.IDLE
            current_tool = None
            # idle-override leaves the hook-state's tool_input alone but
            # we MUST drop it from the view too — the invariant
            # current_tool_input non-None ⇒ phase=TOOL_USE would fire.
            current_tool_input = None
        else:
            phase = live.phase
            current_tool = live.current_tool
            # Plan F: ride along on the same gating as current_tool —
            # only forward when we are actually in TOOL_USE. live.cti
            # is already guaranteed-None outside TOOL_USE by the state
            # machine's invariants, but be defensive.
            current_tool_input = (
                live.current_tool_input
                if live.phase == SessionPhase.TOOL_USE
                else None
            )
        last_prompt = live.last_prompt
        last_assistant_message = live.last_assistant_message
        jump_target = live.jump_target
    else:
        phase = _phase_from_pid_json(
            status_word=status_word,
            last_activity=last_activity,
            active_threshold_s=active_threshold_s,
        )
        current_tool = None
        current_tool_input = None
        last_prompt = None
        last_assistant_message = None
        # Even for ENDED sessions, preserve jump_target so the UI can
        # display the terminal context (helpful for "this session was
        # in WT, last seen at ...").
        jump_target = live.jump_target if live is not None else None

    # v4c Phase 3: project phase-elapsed timestamps onto the view so
    # the UI renders "tool_use · Bash · 1.2s" / "compacting · 8s"
    # without re-reading the clock on every paint.  Only populate when
    # the row is in the matching phase + the timestamp exists — falls
    # back to None ⇒ UI omits the elapsed portion.
    now_utc = datetime.now(timezone.utc)
    tool_elapsed_s: float | None = None
    compact_elapsed_s: float | None = None
    if live is not None:
        if (
            phase == SessionPhase.TOOL_USE
            and getattr(live, "tool_started_at", None) is not None
        ):
            try:
                tool_elapsed_s = (now_utc - live.tool_started_at).total_seconds()
                if tool_elapsed_s < 0:   # clock skew — clamp
                    tool_elapsed_s = 0.0
            except Exception:
                tool_elapsed_s = None
        if (
            phase == SessionPhase.COMPACTING
            and getattr(live, "compact_started_at", None) is not None
        ):
            try:
                compact_elapsed_s = (now_utc - live.compact_started_at).total_seconds()
                if compact_elapsed_s < 0:
                    compact_elapsed_s = 0.0
            except Exception:
                compact_elapsed_s = None

    # Command-hero elapsed — seconds since the most recent command STARTED,
    # carried across phases (so a "thinking" card still shows "· 12m 03s").
    # NOT phase-gated, unlike tool_elapsed_s above. None when there's no
    # recorded command or the timestamp is missing/invalid.
    last_command = live.last_command if live is not None else None
    last_command_elapsed_s: float | None = None
    if live is not None and getattr(live, "last_command_at", None) is not None:
        try:
            last_command_elapsed_s = (now_utc - live.last_command_at).total_seconds()
            if last_command_elapsed_s < 0:   # clock skew — clamp
                last_command_elapsed_s = 0.0
        except Exception:
            last_command_elapsed_s = None

    # v4c Phase 3b: rolling token-rate over the last 60s.  Cheap
    # in-memory aggregation via the per-uuid inverted index.  Wrapped
    # in a lambda + getattr so older / test-stub UsageRegistry impls
    # without get_session_token_rate degrade to None (rather than
    # AttributeError).
    rate_fn = getattr(usage_registry, "get_session_token_rate", None)
    tokens_per_min = (
        _safe(rate_fn, sess_uuid) if (sess_uuid and rate_fn is not None) else None
    )

    return SessionView(
        pid=session.pid,
        name=name,
        project_path=session.project_path,
        project_basename=session.project_path.name or str(session.project_path),
        last_activity=last_activity,
        cost_usd=float(cost),
        is_high_cost=float(cost) >= high_cost_threshold,
        latest_model=latest_model,
        status_word=status_word.lower() if status_word else None,
        session_uuid=sess_uuid or "",
        session=session,
        phase=phase,
        current_tool=current_tool,
        current_tool_input=current_tool_input,
        last_prompt=last_prompt,
        last_assistant_message=last_assistant_message,
        jump_target=jump_target,
        has_live_state=live is not None,
        turn_count=int(turns or 0),
        tool_elapsed_s=tool_elapsed_s,
        compact_elapsed_s=compact_elapsed_s,
        tokens_per_min=tokens_per_min,
        last_command=last_command,
        last_command_elapsed_s=last_command_elapsed_s,
    )


# pid.json status flips to "busy" on every turn but Claude doesn't
# always flip it back to "idle" when the turn ends (process killed,
# crash, signal mishandled). Without a freshness gate, a session that
# was "busy" 6 hours ago shows as running indefinitely in the UI.
# Require activity within 5 minutes to TRUST a busy/waiting status
# word; otherwise fall through to IDLE.
#
# 5 minutes is generous enough to cover slow tools (long Bash runs
# don't write JSONL during execution) but tight enough to catch
# truly stale state — measured against real-world observation that
# a Claude session idle for >5min has clearly finished its turn.
_PID_JSON_FRESHNESS_S = 300.0


def _phase_from_pid_json(
    *,
    status_word: str | None,
    last_activity: datetime,
    active_threshold_s: float,
) -> SessionPhase:
    """Degraded-path phase derivation when no hook live_state is available.

    Mapping (F-4, with freshness gate added for Bug B 2026-05-13):
      status="busy"    + recent activity (<5min)  → THINKING
      status="busy"    + stale activity           → IDLE (status considered stale)
      status="waiting" + recent activity          → WAITING_APPROVAL
      status="waiting" + stale activity           → IDLE
      status="idle"                               → IDLE
      no status + recent activity (<30s)          → THINKING
      no status + stale                           → IDLE

    Why the freshness gate: pid.json is what Claude Code itself writes
    to track session state. It updates on every turn boundary but Claude
    doesn't always flip it back to "idle" when the turn ends (process
    killed, crash, signal mishandled). Without this gate, a session
    that went "busy" hours ago shows as running indefinitely.

    Note: ``waiting`` maps to WAITING_APPROVAL but this is rough; pid.json
    doesn't tell us which tool is awaiting approval, so the resulting
    SessionLiveState would violate ``WAITING_APPROVAL ⇔ pending tool set``.
    Since we don't construct a SessionLiveState on this path (we just
    pick a SessionView phase), the invariant doesn't apply here.
    """
    seconds_since = _seconds_since(last_activity)

    if status_word:
        sw = status_word.lower()
        if sw == "idle":
            return SessionPhase.IDLE
        if sw in ("busy", "waiting"):
            # Freshness gate — pid.json status alone is not authoritative.
            if seconds_since is None or seconds_since > _PID_JSON_FRESHNESS_S:
                return SessionPhase.IDLE
            return (
                SessionPhase.THINKING
                if sw == "busy"
                else SessionPhase.WAITING_APPROVAL
            )

    # No status word — heuristic on activity time
    if seconds_since is None:
        return SessionPhase.IDLE
    return (
        SessionPhase.THINKING
        if seconds_since < active_threshold_s
        else SessionPhase.IDLE
    )


def _seconds_since(t: datetime) -> float | None:
    """Wall-clock seconds since ``t``, or None if t isn't a valid
    tz-aware datetime."""
    try:
        return (
            datetime.now(timezone.utc) - t.astimezone(timezone.utc)
        ).total_seconds()
    except (TypeError, ValueError, AttributeError):
        return None


def _safe(func: Callable, *args, **kwargs):
    """Call ``func(*args, **kwargs)``; return None on any exception.

    Logs at debug — the caller decides whether the missing data is a
    problem (most are tolerated as "field stays None").
    """
    try:
        return func(*args, **kwargs)
    except Exception as e:
        log.debug("compose_session_view: %s raised %s", func.__name__, e)
        return None


def _safe_or(func: Callable, *, default):
    """Like ``_safe`` but returns ``default`` instead of None — useful
    when the call returns a tuple that the caller wants to unpack."""
    try:
        return func()
    except Exception as e:
        log.debug("compose_session_view: %s raised %s", func.__name__, e)
        return default


def _degraded_view(session: Session) -> SessionView:
    """Fallback used when ``compose_session_view`` fails entirely (the
    function itself shouldn't raise, but its callers can pre-empt with
    a degraded view if they want to skip composition for known-bad
    sessions). Renders as "this session exists but we know nothing
    else about it"."""
    name = session.project_path.name or str(session.project_path)
    return SessionView(
        pid=session.pid,
        name=name,
        project_path=session.project_path,
        project_basename=name,
        last_activity=session.last_activity,
        cost_usd=0.0,
        is_high_cost=False,
        latest_model=None,
        status_word=None,
        session=session,
        # phase defaults to IDLE; is_running property gives False
        # Propagate whatever uuid the Session already carries — usually
        # empty (ProcessScanner doesn't read transcripts) but tests
        # construct Sessions with explicit uuids and rely on the
        # degraded view exposing them.
        session_uuid=session.session_uuid or "",
    )


def _filter_stale_views(views: list[SessionView]) -> list[SessionView]:
    """Drop views that have no proof of being alive.

    A view is "alive" iff any one of:
      1. Has an OS-confirmed pid (``view.pid > 0`` — ProcessScanner saw
         the process this tick; OS guarantees it exists).
      2. Has a hook live state in a non-ENDED phase (state machine is
         tracking activity for this uuid — hook bridge confirmed
         existence in the recent past).

    A view with ``pid <= 0`` AND no live hook state is a STALE
    placeholder — the hook bridge upserted it, the scanner never
    confirmed, and the miss-counter tombstone hasn't fired yet. Drop
    so it doesn't appear in the live list.

    Why we do NOT use last_activity / staleness windows:
    ``ProcessScanner._build`` populates ``session.last_activity`` with
    the process create_time. A claude.exe that started 2 days ago and
    has been idle since is still a real, OS-alive process — the user
    can click its row and switch to that terminal. Filtering it by
    age would (a) hide the entire current conversation on Windows
    when the user is reading between turns, and (b) require a per-
    session "last seen" signal we don't reliably have for the no-hook
    fallback path. Phase IDLE alone is the correct "not active right
    now" signal — UI surfaces it via the dimmed-dot glyph; users
    decide what to click.
    """
    kept: list[SessionView] = []
    for v in views:
        # OS-confirmed alive process: keep regardless of phase or age.
        if v.pid > 0:
            kept.append(v)
            continue
        # Placeholder (pid <= 0): keep iff state machine has a live
        # state for this uuid (compose sets has_live_state=True). That
        # signal is independent of phase value, so it correctly admits
        # the "just-started, phase=IDLE, no prompts yet" case AND
        # rejects ghost placeholders whose only evidence is a stale
        # registry entry that the heuristic phase derivation flips to
        # THINKING based on a fresh last_activity.
        if v.has_live_state and v.phase != SessionPhase.ENDED:
            kept.append(v)
    return kept


def _dedup_views_by_session_uuid(views: list[SessionView]) -> list[SessionView]:
    """Collapse duplicates that share a non-empty ``session_uuid``.

    Two pids both attached to the same Claude session — typically when
    the user ran ``claude --resume <uuid>`` in two terminals — each get
    their own ``Session`` from ProcessScanner. ``compose_session_view``
    then resolves ``session_uuid`` from per-pid ``pid.json``, so both
    SessionViews point at the same logical session and render as
    identical rows. The UI's identity is the session, not the OS pid,
    so collapse to one row per uuid.

    Selection rule: highest ``last_activity`` wins; tie-break on higher
    ``pid`` (newer processes hand out larger pids). Views with empty
    ``session_uuid`` pass through — they're either hook placeholders or
    sessions whose transcript hasn't been observed yet, and we have no
    merge key for them.

    Order is preserved: a winner inherits the slot of the first
    occurrence of its uuid, keeping the UI's row order stable across
    snapshots.
    """
    seen_idx: dict[str, int] = {}
    result: list[SessionView] = []
    for v in views:
        if not v.session_uuid:
            result.append(v)
            continue
        idx = seen_idx.get(v.session_uuid)
        if idx is None:
            seen_idx[v.session_uuid] = len(result)
            result.append(v)
            continue
        existing = result[idx]
        if (v.last_activity, v.pid) > (existing.last_activity, existing.pid):
            result[idx] = v
    return result


def _normalize_project_path(path: Path) -> str:
    """Collapse Claude Code worktree paths back to their parent project.

    Claude Code creates per-feature git worktrees under
    ``<repo>/.claude/worktrees/<branch-name>``. Normalising back to the
    repo root lets adapters produce cleaner title hints (e.g. "repo-a"
    instead of two different worktree paths for the same project).
    """
    parts = path.parts
    for i in range(len(parts) - 1):
        if parts[i] == ".claude" and parts[i + 1] == "worktrees":
            return str(Path(*parts[:i]))
    return str(path)


# ---------------------------------------------------------------------------
# Snapshotter — the worker that builds WorldSnapshots and pushes them
# ---------------------------------------------------------------------------


class _CachedView(NamedTuple):
    """Cache entry for one composed SessionView with its dependency versions.

    The ``versions`` tuple captures the source version numbers at compose
    time. On a subsequent build, if all versions still match, the view is
    still fresh — no need to call compose_session_view again.
    """
    view: SessionView
    meta_version: int
    record_version: int
    state_version: int
    names_version: int


def _has_volatile_time_field(v: SessionView) -> bool:
    """True when a view carries a ``now()``-derived field — phase-elapsed
    timers or the rolling 60s token rate. These are pure functions of the
    wall clock, NOT of any version counter, so serving such a view from
    cache would freeze it: a thinking card's "· 12m 03s" would stall, and
    an idle session's token rate would never decay to None. The Snapshotter
    recomposes these views every build instead of caching them (cache-002).
    Static views (no live timer / rate) still cache normally — they're the
    common case, so the incremental cache keeps most of its benefit.
    """
    return (
        v.tool_elapsed_s is not None
        or v.compact_elapsed_s is not None
        or v.last_command_elapsed_s is not None
        or v.tokens_per_min is not None
    )


class Snapshotter:
    """Builds a fresh ``WorldSnapshot`` on every wake; runs the build on
    a single dedicated worker thread; pushes the result via an injected
    callback.

    Lifecycle:
        snapshotter = Snapshotter(deps..., publish=marshaler.snap_ready.emit)
        snapshotter.start()
        ...
        snapshotter.stop()  # at app shutdown / test teardown

    Triggering:
        snapshotter.wake()  # from any thread; debounced internally

    Threading model:
        - ``wake()`` is thread-safe (Subject.on_next has internal mutex)
        - ``_do_build()`` runs on the EventLoopScheduler's single
          worker thread — never on the caller's thread
        - ``publish`` callback is invoked from the worker thread; the
          wiring layer's marshaler converts it to a Qt main-thread call

    The wake → build pipeline uses two operators:
        debounce(window): coalesces a burst of wakes into one build
        throttle_first(cap): even under sustained wakes, build at most
                             once per ``throttle_first_window_s``
    Together: if a Claude Code response writes 5 JSONL lines in 80 ms,
    debounce coalesces all 5 into 1 build at t+window. If the system
    starts firing 100 wakes/sec, throttle_first caps the build rate so
    we don't burn CPU.
    """

    def __init__(
        self,
        *,
        session_source: _SessionSourceProto,
        state_reader: _StateReaderProto,
        metadata_provider: _MetadataProviderProto,
        usage_registry: _UsageRegistryProto,
        names_store: _NamesStoreProto,
        get_quota: Callable[[], QuotaSnapshot | None],
        get_available_providers: Callable[[], list[str]],
        get_selected_provider: Callable[[], str | None],
        publish: Callable[[WorldSnapshot], None],
        group_sessions: _GroupSessionsProto = _default_group_sessions,
        # Live state lookup from the hook pipeline. Defaults to "no live
        # state" so legacy tests / boot paths that pre-date the hook
        # work unchanged. Production injects ``SessionStateMachine.read``.
        live_state_reader: LiveStateProto = _noop_live_state,
        # Resume-offline sources. Both default to None so existing
        # tests that don't use the History drawer still work — when None,
        # dormant_sessions and launching_sessions in the published
        # snapshot stay empty.
        dormant_source: "_DormantSourceProto | None" = None,
        launch_intent: "_LaunchIntentProto | None" = None,
        # Bidirectional Hooks v1 sources (2026-05-14). Both default to
        # None — when None, the corresponding WorldSnapshot fields stay
        # empty. Production injects PendingDecisionRegistry +
        # NotifyEventQueue.
        pending_decisions: "_PendingDecisionProto | None" = None,
        notify_queue: "_NotifyQueueProto | None" = None,
        debounce_window_s: float = 0.1,
        throttle_first_window_s: float = 0.2,
        # Incremental cache: callable returning the state machine's
        # version counter. Default returns -1 (sentinel meaning
        # "state version tracking unavailable") so existing code
        # that doesn't wire a state machine falls through to the
        # legacy always-recompute path.  When wired to a real
        # SessionStateMachine, returns monotonically increasing
        # integers from its state_version counter.
        get_state_version: Callable[[], int] = (lambda: -1),
    ) -> None:
        # ``publish`` is required and keyword-only — it must NEVER
        # default to ``world.push``. WorldMarshaler exists to ensure
        # subscribers (capsule.render, expanded.render) fire on the
        # Qt main thread; defaulting to ``world.push`` would silently
        # route _do_build's worker-thread call straight into the
        # BehaviorSubject's synchronous dispatch, and the next Qt
        # widget mutation would crash. Tests must pass a thread-safe
        # callable (e.g. ``received.append``). Production passes
        # ``WorldMarshaler.snap_ready.emit``.
        self._session_source = session_source
        self._state_reader = state_reader
        self._metadata_provider = metadata_provider
        self._usage_registry = usage_registry
        self._names_store = names_store
        self._live_state_reader = live_state_reader
        self._get_quota = get_quota
        self._get_available_providers = get_available_providers
        self._get_selected_provider = get_selected_provider
        self._publish = publish
        # Injected platform-side grouping. Default produces one
        # singleton group per view so tests don't need to wire a
        # dispatcher; production injects ``TerminalDispatcher.group_sessions``
        # from __main__.py to get real adapter-driven grouping +
        # capability merging.
        self._group_sessions = group_sessions
        # Optional resume-offline sources. None = feature disabled
        # (snapshot's dormant_sessions / launching_sessions stay empty).
        self._dormant_source = dormant_source
        self._launch_intent = launch_intent
        # Bidirectional Hooks v1 sources. None ⇒ field stays empty tuple.
        self._pending_decisions = pending_decisions
        self._notify_queue = notify_queue
        self._debounce_window_s = debounce_window_s
        self._throttle_first_window_s = throttle_first_window_s
        self._get_state_version = get_state_version

        self._wake_signal: Subject[None] = Subject()
        self._scheduler: EventLoopScheduler | None = None
        self._wake_subscription: abc.DisposableBase | None = None
        # Acquired by ``_do_build`` for the duration of one build, and
        # by ``stop`` to wait for any in-flight build to complete
        # before disposing the scheduler. Without this, ``stop`` could
        # return while ``_do_build`` is mid-iteration over the registries
        # — fine today (everything is in-memory) but a hard crash the
        # moment a closeable resource (SQLite conn, network socket) is
        # added to the build path.
        import threading
        self._build_lock = threading.Lock()

        # ── Incremental cache (2026-05-26) ────────────────────────────────
        # Per-identity cache of composed SessionViews, keyed by
        # (session_uuid, pid, project_path).  Each entry records the
        # source versions at compose time so the next build can skip
        # recomposition when nothing changed for that identity.
        self._view_cache: dict[tuple, "_CachedView"] = {}
        # Versions snapshot from the last build — used to detect whether
        # source data changed since the cached views were composed.
        self._last_sessions_fp: object = None
        self._last_meta_version: int = -1
        self._last_record_version: int = -1
        self._last_state_version: int = -1
        self._last_names_version: int = -1
        # Cached results of downstream pipeline stages (dedup + filter
        # + group are pure functions of the view list; if the view list
        # hasn't changed, these are valid too).
        self._cached_views: list[SessionView] | None = None
        self._cached_groups: list[SessionGroup] | None = None
        # True when the last cached view set contained any volatile
        # (now()-derived) field. Forces the next build off the whole-list
        # fast path so live timers / token rates stay fresh (cache-002).
        self._cached_has_volatile: bool = False

    # -- lifecycle ----------------------------------------------------------

    def start(self) -> None:
        """Spin up the worker scheduler and subscribe the wake pipeline.

        Idempotent — second call is a no-op so it's safe to invoke from
        more than one wiring path.

        Pipeline:
          ``ops.observe_on(scheduler)``  ← funnel all upstream emits
              from arbitrary threads onto the worker thread BEFORE any
              stateful operator sees them. wake() can fire from the
              Qt main thread (QTimer / tab clicks), the scanner worker
              thread (sessions_changed) and the file-watcher thread
              (totals_changed); without observe_on first, debounce's
              internal cancellable / has_value state would mutate from
              all three threads concurrently.
          ``ops.debounce(window)``       ← coalesce burst of wakes
          ``ops.throttle_first(cap)``    ← upper-bound build rate
              (only chained when window > 0; reactivex raises on 0)
          ``→ _do_build()`` on the scheduler thread
        """
        if self._scheduler is not None:
            return
        self._scheduler = EventLoopScheduler()
        operators = [
            ops.observe_on(self._scheduler),
            ops.debounce(self._debounce_window_s, scheduler=self._scheduler),
        ]
        if self._throttle_first_window_s > 0:
            operators.append(
                ops.throttle_first(
                    self._throttle_first_window_s, scheduler=self._scheduler,
                )
            )
        self._wake_subscription = self._wake_signal.pipe(*operators).subscribe(
            on_next=lambda _: self._do_build(),
            on_error=lambda e: log.exception("snapshotter wake pipeline died: %s", e),
            scheduler=self._scheduler,
        )

    def stop(self) -> None:
        """Tear down: dispose subscription, wait for any in-flight
        build, then dispose the scheduler (stops the worker thread).
        Idempotent.

        Acquiring ``_build_lock`` blocks until ``_do_build`` (if
        currently mid-iteration) returns. This prevents the worker
        from holding references to soon-to-close registry resources
        when a future iteration adds them — today everything is
        in-memory and the worst case is "publish a stale snapshot
        once during shutdown", but the contract should be tight
        before we add any closeable resource to the build path."""
        if self._wake_subscription is not None:
            self._wake_subscription.dispose()
            self._wake_subscription = None
        # Wait for in-flight build before disposing the scheduler.
        with self._build_lock:
            if self._scheduler is not None:
                self._scheduler.dispose()
                self._scheduler = None

    # -- public API ---------------------------------------------------------

    def wake(self) -> None:
        """Request a fresh snapshot. Returns immediately; the build
        runs asynchronously on the worker thread, debounced.

        Safe to call from any thread."""
        self._wake_signal.on_next(None)

    def build_now(self) -> WorldSnapshot:
        """Synchronous build — bypasses the pipeline entirely.

        For tests and for the boot sequence (we want one snapshot
        published before app.exec() starts, without waiting for the
        first debounce). Runs on the calling thread."""
        return self._build_snapshot()

    # -- internals ----------------------------------------------------------

    def _do_build(self) -> None:
        """Build + publish, with top-level exception suppression so a
        bad source can't take down the worker pipeline.

        Holds ``_build_lock`` for the full build-and-publish — paired
        with the same lock acquired by ``stop()`` so a teardown waits
        for in-flight builds to finish before disposing the scheduler.

        Records ``snap.build.count`` and ``snap.build.duration_ms`` to
        the metrics registry so future perf-claim PRs can show before/
        after numbers. The build duration excludes publish — publish
        cost lives downstream (cross-thread marshal + render) and is
        not the snapshotter's to attribute.
        """
        import time as _time
        from claude_island.core.metrics import metrics as _metrics
        with self._build_lock:
            t0 = _time.perf_counter()
            try:
                snap = self._build_snapshot()
            except Exception:
                log.exception("snapshot build failed; previous snapshot preserved")
                _metrics.incr("snap.build.error")
                return
            finally:
                _metrics.incr("snap.build.count")
                _metrics.observe(
                    "snap.build.duration_ms",
                    (_time.perf_counter() - t0) * 1000.0,
                )
            try:
                self._publish(snap)
            except Exception:
                log.exception("snapshot publish failed")
                _metrics.incr("snap.publish.error")

    def _build_snapshot(self) -> WorldSnapshot:
        sessions_raw = self._safe_list_sessions()

        # ── Incremental compose ────────────────────────────────────────
        # Build a lightweight fingerprint of the session list and read
        # current source versions. If nothing changed since last build,
        # reuse the cached views, dedup, filter, and groups wholesale.
        sessions_fp = tuple(
            (s.pid, s.session_uuid, s.last_activity)
            for s in sessions_raw
        )
        meta_ver = getattr(self._metadata_provider, "meta_version", 0)
        record_ver = getattr(self._usage_registry, "record_version", 0)
        state_ver = self._get_state_version()
        # names_store is a fourth compose input (custom session names).
        # Default-0 so fakes without the attribute never invalidate here.
        names_ver = getattr(self._names_store, "names_version", 0)

        # Sentinel check: if any version is -1, that source's version
        # tracking is unavailable — fall through to the rebuild path.
        _sentinel = -1
        if (
            meta_ver != _sentinel
            and record_ver != _sentinel
            and state_ver != _sentinel
            and sessions_fp == self._last_sessions_fp
            and meta_ver == self._last_meta_version
            and record_ver == self._last_record_version
            and state_ver == self._last_state_version
            and names_ver == self._last_names_version
            and self._cached_views is not None
            and not self._cached_has_volatile
        ):
            # Full cache hit — all source data unchanged.
            views = self._cached_views
            groups = self._cached_groups  # type: ignore[assignment]
        else:
            # Partial or full miss — compose views incrementally.
            views = self._compose_views_incremental(
                sessions_raw, meta_ver, record_ver, state_ver, names_ver,
            )
            # Dedup and filter are pure functions of the view list.
            views = _dedup_views_by_session_uuid(views)
            views = _filter_stale_views(views)
            # Grouping depends only on the filtered view list.
            try:
                groups = list(self._group_sessions(views))
            except Exception:
                log.exception(
                    "group_sessions raised; using singleton fallback grouping"
                )
                groups = _default_group_sessions(views)

            # Update cache for next build.
            self._last_sessions_fp = sessions_fp
            self._last_meta_version = meta_ver
            self._last_record_version = record_ver
            self._last_state_version = state_ver
            self._last_names_version = names_ver
            self._cached_views = views
            self._cached_groups = groups
            self._cached_has_volatile = any(
                _has_volatile_time_field(v) for v in views
            )

        # Staleness filter and grouping are already applied in the
        # incremental compose block above — views and groups are
        # either cached or freshly computed.

        try:
            today_totals = self._usage_registry.get_totals("today")
            today_cost = float(today_totals.cost_usd)
        except Exception:
            log.debug("usage_registry.get_totals('today') raised", exc_info=True)
            today_cost = 0.0

        try:
            quota = self._get_quota()
        except Exception:
            log.debug("get_quota() raised", exc_info=True)
            quota = None

        try:
            available = tuple(self._get_available_providers())
        except Exception:
            log.debug("get_available_providers() raised", exc_info=True)
            available = ()

        try:
            selected = self._get_selected_provider()
        except Exception:
            log.debug("get_selected_provider() raised", exc_info=True)
            selected = None

        # ── 3-source reconcile (resume-offline) ─────────────────────────
        # live_uuids drives both LaunchIntent expiration and dormant filtering.
        # Iterating groups (already grouped) saves us a redundant pass over
        # the flat views list — same data, different shape.
        live_uuids: set[str] = set()
        for g in groups:
            for v in g.views:
                if v.session_uuid:
                    live_uuids.add(v.session_uuid)

        now_utc = datetime.now(timezone.utc)
        if self._launch_intent is not None:
            try:
                self._launch_intent.reconcile(live_uuids=live_uuids, now=now_utc)
                launching = self._launch_intent.snapshot()
            except Exception:
                log.exception("launch_intent reconcile/snapshot raised")
                launching = ()
        else:
            launching = ()

        if self._dormant_source is not None:
            try:
                all_dormant = list(self._dormant_source.sessions)
            except Exception:
                log.exception("dormant_source.sessions raised; treating as empty")
                all_dormant = []
            launching_uuids = {i.session_uuid for i in launching}
            dormant = tuple(
                d for d in all_dormant
                if d.session_uuid not in live_uuids
                and d.session_uuid not in launching_uuids
            )
        else:
            dormant = ()

        # ── Bidirectional Hooks v1 reads ──────────────────────────
        if self._pending_decisions is not None:
            try:
                pending = self._pending_decisions.snapshot()
            except Exception:
                log.exception("pending_decisions.snapshot raised; treating as empty")
                pending = ()
        else:
            pending = ()

        if self._notify_queue is not None:
            try:
                notify_evts = self._notify_queue.snapshot()
            except Exception:
                log.exception("notify_queue.snapshot raised; treating as empty")
                notify_evts = ()
        else:
            notify_evts = ()

        return WorldSnapshot(
            today_cost_usd=today_cost,
            quota=quota,
            available_providers=available,
            selected_provider=selected,
            fetched_at=now_utc,
            session_groups=tuple(groups),
            dormant_sessions=dormant,
            launching_sessions=launching,
            pending_decisions=pending,
            notify_events=notify_evts,
        )

    def _safe_list_sessions(self) -> list[Session]:
        try:
            return list(self._session_source.sessions)
        except Exception:
            log.exception(
                "session_source.sessions raised; treating as no sessions"
            )
            return []

    def _compose_views_incremental(
        self,
        sessions: list[Session],
        meta_ver: int,
        record_ver: int,
        state_ver: int,
        names_ver: int,
    ) -> list[SessionView]:
        """Compose SessionViews with per-identity caching.

        For each session, compute a stable identity key and check whether
        the cached view is still fresh (same source versions). Fresh views
        are reused; stale ones are recomposed and re-cached.
        """
        # Sentinel: if any version tracking is unavailable, skip the
        # per-uuid cache and always recompose (legacy behaviour).
        _sentinel = -1
        _cache_enabled = (
            meta_ver != _sentinel
            and record_ver != _sentinel
            and state_ver != _sentinel
        )

        views: list[SessionView] = []
        for s in sessions:
            key = (s.session_uuid, s.pid, str(s.project_path))
            cached = self._view_cache.get(key)
            if (
                _cache_enabled
                and cached is not None
                and cached.meta_version == meta_ver
                and cached.record_version == record_ver
                and cached.state_version == state_ver
                and cached.names_version == names_ver
                and not _has_volatile_time_field(cached.view)
            ):
                # All source versions unchanged — reuse cached view.
                views.append(cached.view)
                continue

            # Cache miss or stale — compose a fresh view.
            try:
                view = compose_session_view(
                    s,
                    state_reader=self._state_reader,
                    metadata_provider=self._metadata_provider,
                    usage_registry=self._usage_registry,
                    names_store=self._names_store,
                    live_state_reader=self._live_state_reader,
                )
            except Exception:
                log.exception(
                    "compose_session_view raised for pid=%s; using degraded view",
                    s.pid,
                )
                view = _degraded_view(s)

            views.append(view)
            self._view_cache[key] = _CachedView(
                view=view,
                meta_version=meta_ver,
                record_version=record_ver,
                state_version=state_ver,
                names_version=names_ver,
            )

        # Evict entries for identities no longer present this build (dead
        # pids, uuids resumed in a different terminal). Without this the
        # per-identity cache grows unbounded over a long-running process,
        # since each new (uuid, pid, project) is purely additive. Bounded
        # by the live session count, so the O(cache) sweep is cheap.
        live_keys = {
            (s.session_uuid, s.pid, str(s.project_path)) for s in sessions
        }
        for dead in [k for k in self._view_cache if k not in live_keys]:
            del self._view_cache[dead]

        return views
