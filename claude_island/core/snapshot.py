"""WorldSnapshot broadcast — the single source of truth for UI state.

The architecture this file establishes:

    Snapshotter (worker thread, lives in this module)
        ↓ builds
    WorldSnapshot (frozen dataclass, this module)
        ↓ pushed via
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
  Signal. Here we only guarantee: ``world.push(snap)`` synchronously
  notifies subscribers on the calling thread. The wiring layer's job
  is to ensure that calling thread is the Qt main thread.

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

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from reactivex import Observable
from reactivex.subject import BehaviorSubject

from .models import QuotaSnapshot


# Spend threshold (USD) above which a session is flagged "high cost"
# and the cost label paints in the warning colour. Lifted from the UI
# layer constant of the same name so SessionView can pre-resolve it
# (the UI no longer needs to recompute the threshold per render).
HIGH_COST_USD_THRESHOLD = 50.0


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
    window_handle: int | None       # passthrough from Session

    def __post_init__(self) -> None:
        # Self-consistency invariant — guards against the UI and the
        # Snapshotter drifting on what counts as "high cost".
        assert self.is_high_cost == (self.cost_usd >= HIGH_COST_USD_THRESHOLD), (
            f"SessionView invariant violated: cost_usd={self.cost_usd}, "
            f"is_high_cost={self.is_high_cost}"
        )


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

    sessions: tuple[SessionView, ...]
    today_cost_usd: float
    quota: QuotaSnapshot | None
    available_providers: tuple[str, ...]
    selected_provider: str | None
    fetched_at: datetime

    @classmethod
    def empty(cls) -> "WorldSnapshot":
        """Initial value for the BehaviorSubject — UI must safely
        render this without errors. Represents "we haven't built any
        snapshot yet"."""
        return cls(
            sessions=(),
            today_cost_usd=0.0,
            quota=None,
            available_providers=(),
            selected_provider=None,
            fetched_at=datetime.fromtimestamp(0, tz=timezone.utc),
        )


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
