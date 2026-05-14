"""Notification event types + dispatch policy (G2).

Pure-policy module: defines what NotifyEvent carries and the rules that
decide ``send / coalesce / suppress`` given the current world state.
The actual OS-side ``post()`` call lives behind the
``NotifyBackend`` Protocol in ``platform_/notify/``.

Why split policy and transport: the policy needs to be tested with
table-driven inputs (frontmost yes/no, debounce window, coalesce count)
without spawning osascript or opening Qt. Keeping it pure makes the
test suite fast and cross-platform.
"""
from __future__ import annotations

import logging
import threading
import uuid as _uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Callable, Protocol, Sequence, runtime_checkable

log = logging.getLogger(__name__)


class NotifyKind(Enum):
    """Severity / category. Drives backend rendering (sound, urgency).
    Add new variants here as features need them; UI is closed-set."""
    TURN_COMPLETE = "turn_complete"
    TURN_FAILED = "turn_failed"


class NotifyKindHint(Enum):
    """Backend-side hint for sound / urgency. Loosely maps to NotifyKind
    via NotificationDispatcher (which lives in ui/). Defined here in
    core so UI subscribers + platform_ backends both depend on core
    only — no cross-layer import."""
    INFO = "info"      # default chime / no chime
    WARN = "warn"      # alert sound on macOS, attention urgency on Win
    ERROR = "error"    # critical sound, persistent on Win


@runtime_checkable
class NotifyBackend(Protocol):
    """Post a single notification to the OS notification center.

    Returns True iff the backend reports success (best effort —
    ``osascript`` exit 0, or ``winrt.show()`` returned without error).
    Never raises; failures are logged once per process and return False.

    Implementations live in ``platform_/notify/`` (macos.py / windows.py
    / noop.py). The Protocol lives here so UI's NotificationDispatcher
    can type-check against it without importing platform_.

    Implementations:
      - title and body should be UTF-8 strings; longer-than-system limits
        are silently truncated by the OS
      - kind drives sound + urgency hint where supported; INFO is the
        widely-supported lowest common denominator
    """

    def post(
        self, *, title: str, body: str, kind: NotifyKindHint = NotifyKindHint.INFO,
    ) -> bool:
        ...


# Window during which N≥3 events for the same session collapse into a
# single "N turns finished" notification. Picked at 5s on the assumption
# that bursty Stops (multi-session checkpoint) come within seconds, while
# normal sequential turn endings are minutes apart.
COALESCE_WINDOW_S = 5.0
COALESCE_MIN_COUNT = 3

# Minimum interval between notifications for the same session. Below this,
# the dispatcher drops the new event (debounce). Independent from the
# coalesce window — coalesce groups multiple sessions; debounce throttles
# repeated notifications about the same session.
DEBOUNCE_PER_SESSION_S = 3.0


# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class NotifyEvent:
    """A single notification trigger. Lives on WorldSnapshot for ~60 s
    rolling window (see snapshot.py); dispatcher dedups via id."""
    id: str                   # uuid4 hex; deduplication key
    kind: NotifyKind
    session_uuid: str
    session_name: str         # cached so dispatcher needn't re-resolve
    cwd_basename: str
    occurred_at: datetime


@dataclass(frozen=True, slots=True)
class FrontmostInfo:
    """What the dispatcher needs to know to suppress notifications.

    Both fields cross-platform: any failure to detect = (None, set()),
    which means "don't suppress".
    """
    island_is_frontmost: bool = False
    frontmost_terminal_pids: frozenset[int] = field(default_factory=frozenset)


@dataclass(frozen=True, slots=True)
class DispatchRecord:
    """One past dispatch — input to debounce / coalesce decisions.
    Keep small; dispatcher trims to last 60 s."""
    session_uuid: str
    posted_at: datetime
    notify_id: str            # the id of the original NotifyEvent


@dataclass(frozen=True, slots=True)
class DispatchDecision:
    """Output of policy.evaluate(). Plain data — render driven from this."""
    action: str               # "post" | "drop"
    reason: str               # human-readable for logs
    title: str | None = None
    body: str | None = None
    coalesced_ids: tuple[str, ...] = ()


# ---------------------------------------------------------------------------
# Pure policy
# ---------------------------------------------------------------------------


class NotifyDispatchPolicy:
    """Pure decision function: ``evaluate(event, recent, frontmost,
    pending_events) -> DispatchDecision``.

    The dispatcher calls this once per (event not yet dispatched). It
    has no state of its own; the dispatcher provides ``recent`` from
    its bookkeeping and decides what to do with the result.

    Tested table-driven against synthetic inputs.
    """

    def evaluate(
        self,
        event: NotifyEvent,
        *,
        recent: Sequence[DispatchRecord],
        frontmost: FrontmostInfo,
        sibling_events: Sequence[NotifyEvent] = (),
        session_terminal_pids: frozenset[int] | None = None,
    ) -> DispatchDecision:
        """Decide whether to post this event.

        Args:
          event: the notification candidate
          recent: dispatch history (any time window — caller pre-filters)
          frontmost: which app + terminals are foregrounded right now
          sibling_events: other unposted events near in time (for coalesce)
          session_terminal_pids: pids of terminals owning event's session
            — empty frozenset if unknown

        Returns: DispatchDecision with action="post" or "drop".
        """
        # Rule 1: suppress when island itself is frontmost. User already
        # sees the state; toasts would be redundant.
        if frontmost.island_is_frontmost:
            return DispatchDecision(
                action="drop", reason="island is frontmost",
            )

        # Rule 2: suppress when the OWNING terminal is frontmost. User
        # is watching the session; no need to ping.
        if (
            session_terminal_pids
            and frontmost.frontmost_terminal_pids
            and (session_terminal_pids & frontmost.frontmost_terminal_pids)
        ):
            return DispatchDecision(
                action="drop", reason="session terminal is frontmost",
            )

        # Rule 3: per-session debounce. Drop if a notification for the
        # same session was posted within the debounce window.
        cutoff = event.occurred_at - timedelta(seconds=DEBOUNCE_PER_SESSION_S)
        for r in recent:
            if r.session_uuid == event.session_uuid and r.posted_at >= cutoff:
                return DispatchDecision(
                    action="drop", reason=f"debounced (<{DEBOUNCE_PER_SESSION_S}s)",
                )

        # Rule 4: coalesce. If enough sibling events of the same kind
        # cluster within the coalesce window, batch them into one.
        coalesce_cutoff = event.occurred_at - timedelta(seconds=COALESCE_WINDOW_S)
        cluster = [event] + [
            s for s in sibling_events
            if s.kind == event.kind
            and s.occurred_at >= coalesce_cutoff
            and s.id != event.id
        ]
        if len(cluster) >= COALESCE_MIN_COUNT:
            return DispatchDecision(
                action="post",
                reason=f"coalesced n={len(cluster)}",
                title="claude-island",
                body=f"{len(cluster)} turns finished",
                coalesced_ids=tuple(c.id for c in cluster),
            )

        # Default: single post.
        body = self._format_single_body(event)
        return DispatchDecision(
            action="post",
            reason="single",
            title="claude-island",
            body=body,
            coalesced_ids=(event.id,),
        )

    @staticmethod
    def _format_single_body(event: NotifyEvent) -> str:
        """Body text for one notification. Keep short — system toasts
        truncate aggressively (~200 chars on macOS)."""
        name = event.session_name or event.cwd_basename or "session"
        if event.kind is NotifyKind.TURN_COMPLETE:
            return f"{name}: turn complete"
        if event.kind is NotifyKind.TURN_FAILED:
            return f"{name}: turn failed"
        return f"{name}: notification"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def new_notify_id() -> str:
    return _uuid.uuid4().hex


def make_turn_complete(
    *,
    session_uuid: str,
    session_name: str,
    cwd_basename: str,
    is_failure: bool = False,
    occurred_at: datetime | None = None,
) -> NotifyEvent:
    """Convenience constructor for the most common case (HookSessionBridge
    on Stop / StopFailure). Builds a fresh id + timestamp."""
    return NotifyEvent(
        id=new_notify_id(),
        kind=NotifyKind.TURN_FAILED if is_failure else NotifyKind.TURN_COMPLETE,
        session_uuid=session_uuid,
        session_name=session_name,
        cwd_basename=cwd_basename,
        occurred_at=occurred_at or datetime.now(timezone.utc),
    )


# ---------------------------------------------------------------------------
# NotifyEventQueue — rolling-window event store
# ---------------------------------------------------------------------------


# Events older than this are pruned on snapshot reads + push. The window
# is intentionally generous so a snapshot rebuild race (worker thread
# building snap N+1 while Qt is still rendering snap N) can't cause an
# event to be dropped before NotificationDispatcher dedups it.
_RETENTION_S = 60.0

# Hard cap on queue size — if events flood faster than the dispatcher
# consumes, the oldest fall off. Bounds memory; events older than
# retention also get pruned on every push.
_MAX_QUEUE_SIZE = 256


class NotifyEventQueue:
    """Thread-safe rolling-window queue of NotifyEvents.

    HookServer pushes events on its handler thread; Snapshotter reads
    via snapshot() on its worker thread. NotificationDispatcher (Qt
    thread) reads via WorldSnapshot.notify_events.

    Why rolling-window (vs consume-and-clear): NotificationDispatcher
    dedups via _dispatched_ids, so re-presenting the same event is
    harmless. Idempotent reads avoid the worker/Qt race where a
    snapshot rebuild between push and dispatch would lose the event.
    """

    def __init__(
        self,
        *,
        retention_s: float = _RETENTION_S,
        max_size: int = _MAX_QUEUE_SIZE,
        on_change: Callable[[], None] | None = None,
    ) -> None:
        self._retention = timedelta(seconds=retention_s)
        self._events: deque[NotifyEvent] = deque(maxlen=max_size)
        self._lock = threading.Lock()
        self._on_change = on_change or (lambda: None)

    def push(self, event: NotifyEvent) -> None:
        """Append a new event. Prunes events older than retention.
        Fires on_change so the snapshotter rebuilds."""
        with self._lock:
            self._events.append(event)
            self._prune_locked(now=event.occurred_at)
        self._on_change()

    def snapshot(self, *, now: datetime | None = None) -> tuple[NotifyEvent, ...]:
        """Immutable view of the current rolling window. Prunes stale
        on read so a long-quiet queue doesn't accumulate. Sorted by
        occurred_at ascending (deque insertion order = chronological)."""
        with self._lock:
            self._prune_locked(now=now or datetime.now(timezone.utc))
            return tuple(self._events)

    def __len__(self) -> int:
        with self._lock:
            return len(self._events)

    def _prune_locked(self, *, now: datetime) -> None:
        # Caller holds self._lock.
        cutoff = now - self._retention
        while self._events and self._events[0].occurred_at < cutoff:
            self._events.popleft()
