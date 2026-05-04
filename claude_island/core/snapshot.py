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
from typing import Callable, Protocol

import reactivex.operators as ops
from reactivex import Observable, abc
from reactivex.disposable import Disposable
from reactivex.scheduler import EventLoopScheduler
from reactivex.subject import BehaviorSubject, Subject

from .capabilities import Capability, FocusGranularity
from .launch_intent import LaunchIntent
from .models import DormantSession, QuotaSnapshot, Session

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
    """

    pid: int
    name: str                       # already resolved (custom > state > ai > basename)
    project_path: Path
    project_basename: str           # convenience for sort keys
    last_activity: datetime         # tz-aware UTC
    is_running: bool                # already resolved (status priority > heuristic)
    cost_usd: float                 # >= 0
    is_high_cost: bool              # == (cost_usd >= HIGH_COST_USD_THRESHOLD)
    latest_model: str | None        # None when no records yet
    status_word: str | None         # raw "busy" / "idle" / "waiting" / None
    # The original Session object the view was composed from. Carried
    # along so UI callbacks that accept a Session (e.g. WindowActivator,
    # the row's _siblings list) don't need to reconstruct one. Frozen
    # like everything else here — once the snapshot is built, the
    # ``session`` reference is stable for the lifetime of the snapshot.
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
    # ── Capability framework fields (PR1 added; PR2 makes UI consume) ──
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
    # "iterm2", "generic-mac"). Empty default for backward-compat with
    # legacy construction; PR2 makes adapters always populate it.
    adapter_id: str = ""

    def __post_init__(self) -> None:
        # Self-consistency invariant — guards against the UI and the
        # Snapshotter drifting on what counts as "high cost".
        assert self.is_high_cost == (self.cost_usd >= HIGH_COST_USD_THRESHOLD), (
            f"SessionView invariant violated: cost_usd={self.cost_usd}, "
            f"is_high_cost={self.is_high_cost}"
        )


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
    # HistoryDrawer; never in the main capsule/expanded panel.
    dormant_sessions: tuple[DormantSession, ...] = ()
    # Sessions the user just hit Resume on; we've spawned a terminal but
    # ProcessScanner hasn't yet detected the new claude.exe. Lives at most
    # ttl seconds (default 30) inside LaunchIntentRegistry; reconcile
    # discards on upgrade-to-live or on timeout. UI renders these as
    # disabled rows with a ⏳ Launching… affordance.
    launching_sessions: tuple[LaunchIntent, ...] = ()

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
# (``_default_group_sessions``) emits one group per session, matching
# pre-PR1 behaviour where the UI did its own grouping by window_handle.
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


def _default_group_sessions(views: list["SessionView"]) -> list["SessionGroup"]:
    """Fallback grouping: one SessionGroup per view, with empty
    adapter_id and no title hint.

    Used when no real grouper is injected (most tests, and the boot
    sequence before ``__main__`` builds the dispatcher). The output
    is structurally valid — UI consuming session_groups in PR2 will
    render each as a singleton card. Capabilities on the views are
    untouched (whatever the view came in with stays)."""
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

def compose_session_view(
    session: Session,
    *,
    state_reader: _StateReaderProto,
    metadata_provider: _MetadataProviderProto,
    usage_registry: _UsageRegistryProto,
    names_store: _NamesStoreProto,
    high_cost_threshold: float = HIGH_COST_USD_THRESHOLD,
    active_threshold_s: float = ACTIVE_THRESHOLD_SECONDS,
) -> SessionView:
    """Compose a fully-resolved ``SessionView`` from one ``Session`` plus
    the four data sources.

    This is the moved/renamed version of ``__main__._build_session_details``
    with two changes:

      1. Returns ``SessionView`` (UI-renderable) instead of
         ``SessionDetails`` (loose tuple of fields). The is_running and
         is_high_cost priority chains are pre-resolved here so render
         code has no policy logic.
      2. Dependencies are explicit parameters, not module-level singletons.
         Tests inject fakes; the wiring layer injects the real ones.

    Thread-safe: every dependency is internally serialised (StateReader
    has a lock + 5 s TTL cache, JsonlParser metadata reads use a lock,
    UsageRegistry uses a lock, NamesStore reads disk under a lock).

    Never raises — all dependency exceptions are caught per-source so a
    single corrupted JSON file or transient read error degrades a single
    field rather than failing the whole compose.
    """
    state = _safe(state_reader.read_session_state, session.pid) or {}
    sess_uuid = (
        state.get("sessionId")
        if isinstance(state.get("sessionId"), str)
        else session.session_uuid
    )
    meta = _safe(metadata_provider.get_session_metadata, sess_uuid) or {}

    cost, _turns, _sides = _safe_or(
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

    is_running = _resolve_is_running(
        status_word=status_word,
        last_activity=session.last_activity,
        active_threshold_s=active_threshold_s,
    )

    return SessionView(
        pid=session.pid,
        name=name,
        project_path=session.project_path,
        project_basename=session.project_path.name or str(session.project_path),
        last_activity=session.last_activity,
        is_running=is_running,
        cost_usd=float(cost),
        is_high_cost=float(cost) >= high_cost_threshold,
        latest_model=latest_model,
        status_word=status_word.lower() if status_word else None,
        session_uuid=sess_uuid or "",
        session=session,
    )


def _resolve_is_running(
    *,
    status_word: str | None,
    last_activity: datetime,
    active_threshold_s: float,
) -> bool:
    """Single-source-of-truth for the running/idle priority chain.

    Priority (matches the chain that used to be duplicated in
    capsule._active_sessions and expanded._update_row):

      1. status_word == "busy" / "waiting" → True (authoritative)
      2. status_word == "idle" → False (authoritative; overrides
         the heuristic so synthetic JSONL bumps after /compact don't
         falsely mark an idle session as running)
      3. status_word unknown → fall back to: last_activity within
         active_threshold_s seconds of now → True
    """
    if status_word:
        sw = status_word.lower()
        if sw in ("busy", "waiting"):
            return True
        if sw == "idle":
            return False
    try:
        seconds_since = (
            datetime.now(timezone.utc)
            - last_activity.astimezone(timezone.utc)
        ).total_seconds()
    except (TypeError, ValueError, AttributeError):
        return False
    return seconds_since < active_threshold_s


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
        is_running=False,
        cost_usd=0.0,
        is_high_cost=False,
        latest_model=None,
        status_word=None,
        session=session,
        # Propagate whatever uuid the Session already carries — usually
        # empty (ProcessScanner doesn't read transcripts) but tests
        # construct Sessions with explicit uuids and rely on the
        # degraded view exposing them.
        session_uuid=session.session_uuid or "",
    )


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
        # New keyword-only deps for the resume-offline feature. Both
        # default to None so existing tests / boot paths that don't use
        # the History drawer still work — when None, dormant_sessions
        # and launching_sessions in the published snapshot stay empty.
        dormant_source: "_DormantSourceProto | None" = None,
        launch_intent: "_LaunchIntentProto | None" = None,
        debounce_window_s: float = 0.1,
        throttle_first_window_s: float = 0.2,
    ) -> None:
        # ``publish`` is required and keyword-only — it must NEVER
        # default to ``world.push``. The whole point of the
        # WorldMarshaler shim is to ensure subscribers (capsule.render,
        # expanded.render) fire on the Qt main thread; defaulting to
        # ``world.push`` would silently route _do_build's worker-thread
        # call straight into the BehaviorSubject's synchronous
        # dispatch, and the next Qt widget mutation would crash.
        # Tests must pass a thread-safe callable (e.g. ``received.append``).
        # Production passes ``WorldMarshaler.snap_ready.emit``.
        self._session_source = session_source
        self._state_reader = state_reader
        self._metadata_provider = metadata_provider
        self._usage_registry = usage_registry
        self._names_store = names_store
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
        self._debounce_window_s = debounce_window_s
        self._throttle_first_window_s = throttle_first_window_s

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
        for in-flight builds to finish before disposing the scheduler."""
        with self._build_lock:
            try:
                snap = self._build_snapshot()
            except Exception:
                log.exception("snapshot build failed; previous snapshot preserved")
                return
            try:
                self._publish(snap)
            except Exception:
                log.exception("snapshot publish failed")

    def _build_snapshot(self) -> WorldSnapshot:
        sessions_raw = self._safe_list_sessions()

        views: list[SessionView] = []
        for s in sessions_raw:
            try:
                views.append(
                    compose_session_view(
                        s,
                        state_reader=self._state_reader,
                        metadata_provider=self._metadata_provider,
                        usage_registry=self._usage_registry,
                        names_store=self._names_store,
                    )
                )
            except Exception:
                log.exception(
                    "compose_session_view raised for pid=%s; using degraded view",
                    s.pid,
                )
                views.append(_degraded_view(s))

        # Adapter-driven grouping (dispatcher → chain → sessions bucketed
        # into SessionGroups). If the grouper raises (bug in an adapter),
        # fall back to singleton grouping — every session is its own
        # group — so session_groups is always structurally valid.
        try:
            groups = list(self._group_sessions(views))
        except Exception:
            log.exception(
                "group_sessions raised; using singleton fallback grouping"
            )
            groups = _default_group_sessions(views)

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

        return WorldSnapshot(
            today_cost_usd=today_cost,
            quota=quota,
            available_providers=available,
            selected_provider=selected,
            fetched_at=now_utc,
            session_groups=tuple(groups),
            dormant_sessions=dormant,
            launching_sessions=launching,
        )

    def _safe_list_sessions(self) -> list[Session]:
        try:
            return list(self._session_source.sessions)
        except Exception:
            log.exception(
                "session_source.sessions raised; treating as no sessions"
            )
            return []
