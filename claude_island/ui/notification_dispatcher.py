"""NotificationDispatcher — Qt-side subscriber that turns
WorldSnapshot.notify_events into NotifyBackend.post calls.

Why on the Qt main thread: ``backend.post`` needs to be called from
somewhere we control thread-wise — osascript/winrt are typically thread-
safe but the QSystemTrayIcon fallback path (Windows) **must** run on
the GUI thread. Cleanest design: dispatcher subscribes to world via
the same mechanism every other UI surface uses (capsule.render,
expanded.render), so it inherits the QueuedConnection marshaling that
ensures Qt-thread delivery.

Dedup: ``_dispatched_ids`` set lives for the process lifetime (well,
until evicted by a sliding-window TTL — same window as the rolling
WorldSnapshot.notify_events, so they age out together). This is what
makes the WorldSnapshot.notify_events "rolling window" semantics
correct: the dispatcher won't re-post a notification just because the
event is still present in subsequent snapshots.

Frontmost detection is OS-specific and lives in platform_/. The
dispatcher accepts a callable so tests can inject any FrontmostInfo.
"""
from __future__ import annotations

import logging
from collections import deque
from datetime import datetime, timedelta, timezone
from typing import Callable

from claude_island.core.notify import (
    DEBOUNCE_PER_SESSION_S,
    DispatchRecord,
    FrontmostInfo,
    NotifyBackend,
    NotifyDispatchPolicy,
    NotifyEvent,
    NotifyKind,
    NotifyKindHint,
)
from claude_island.core.snapshot import WorldSnapshot

log = logging.getLogger(__name__)


# Map core NotifyKind → backend NotifyKindHint. Keeps platform_ free
# of core's enum (and vice versa).
_KIND_TO_HINT: dict[NotifyKind, NotifyKindHint] = {
    NotifyKind.TURN_COMPLETE: NotifyKindHint.INFO,
    NotifyKind.TURN_FAILED:   NotifyKindHint.WARN,
}


# Rolling window for our _records (per-session dispatch history). Must
# be ≥ core.notify._RETENTION_S (currently 60 s) so that a queue-resident
# event never re-fires after we forget it. We pad to 120 s so a backed-
# up Qt thread that lags ~1 minute behind a snap rebuild still dedups
# correctly. (Comment fixed in code review C-002.)
_DEDUP_WINDOW_S = 120.0


# Resolver type: returns FrontmostInfo for the current OS state.
# Production wires in a platform_-side helper; tests inject a stub.
FrontmostResolver = Callable[[], FrontmostInfo]


# Resolver type: given a session_uuid, return the set of pids of the
# terminal(s) that own it. For frontmost-suppression: if any of those
# pids is currently the frontmost OS process, drop the notification.
SessionTerminalPidsResolver = Callable[[str], frozenset[int]]


def _no_frontmost() -> FrontmostInfo:
    """Default resolver: no frontmost info available → never suppress."""
    return FrontmostInfo()


def _no_session_pids(uuid: str) -> frozenset[int]:  # noqa: ARG001
    """Default resolver: no terminal pid known → can't suppress."""
    return frozenset()


class NotificationDispatcher:
    """One per app instance; subscribed to world.observable() at boot.

    Stateful across snapshot ticks (it remembers what it's already
    posted). State pruned on a sliding window so a long-running app
    doesn't accumulate forever.
    """

    def __init__(
        self,
        *,
        backend: NotifyBackend,
        policy: NotifyDispatchPolicy | None = None,
        frontmost_resolver: FrontmostResolver = _no_frontmost,
        session_terminal_pids: SessionTerminalPidsResolver = _no_session_pids,
    ) -> None:
        self._backend = backend
        self._policy = policy or NotifyDispatchPolicy()
        self._frontmost = frontmost_resolver
        self._session_pids = session_terminal_pids
        # event ids we've already posted; pruned on each on_snapshot
        self._dispatched_ids: set[str] = set()
        # rolling per-session debounce records
        self._records: deque[DispatchRecord] = deque(maxlen=512)

    def on_snapshot(self, snap: WorldSnapshot) -> None:
        """Called by the rx subscriber. Iterates new events and posts
        those that survive the policy. Idempotent: already-dispatched
        events are skipped via _dispatched_ids."""
        try:
            self._on_snapshot(snap)
        except Exception:
            log.exception("NotificationDispatcher.on_snapshot raised")

    # ── internals ───────────────────────────────────────────────────────

    def _on_snapshot(self, snap: WorldSnapshot) -> None:
        if not snap.notify_events:
            return
        now = datetime.now(timezone.utc)
        self._prune(now)

        events = list(snap.notify_events)
        for event in events:
            if event.id in self._dispatched_ids:
                continue
            try:
                fm = self._frontmost()
            except Exception:
                log.debug("frontmost_resolver raised; treating as no info")
                fm = FrontmostInfo()
            try:
                term_pids = self._session_pids(event.session_uuid)
            except Exception:
                log.debug("session_terminal_pids raised; treating as empty")
                term_pids = frozenset()

            decision = self._policy.evaluate(
                event,
                recent=tuple(self._records),
                frontmost=fm,
                sibling_events=tuple(e for e in events if e.id != event.id),
                session_terminal_pids=term_pids,
            )

            # Mark every coalesced id dispatched, even on drop — drop
            # here is "we decided not to post this event"; we don't
            # want to retry next tick. (Coalesce ids are only set on
            # post; on drop coalesced_ids is empty so we mark the
            # current event alone.)
            ids_to_mark = (
                set(decision.coalesced_ids)
                if decision.coalesced_ids
                else {event.id}
            )
            self._dispatched_ids |= ids_to_mark

            if decision.action == "drop":
                log.debug(
                    "notify dispatch drop for %s: %s",
                    event.session_uuid, decision.reason,
                )
                continue

            ok = False
            try:
                ok = self._backend.post(
                    title=decision.title or "claude-island",
                    body=decision.body or "",
                    kind=_KIND_TO_HINT.get(event.kind, NotifyKindHint.INFO),
                )
            except Exception:
                log.exception("NotifyBackend.post raised; continuing")

            if ok:
                self._records.append(DispatchRecord(
                    session_uuid=event.session_uuid,
                    posted_at=now,
                    notify_id=event.id,
                ))

    def _prune(self, now: datetime) -> None:
        """Drop dispatched-id memory + dispatch records older than window."""
        cutoff = now - timedelta(seconds=_DEDUP_WINDOW_S)
        # records: deque ordered by posted_at, so popleft until fresh.
        while self._records and self._records[0].posted_at < cutoff:
            self._records.popleft()
        # dispatched_ids: no timestamp, so we can't time-prune precisely.
        # Cap by size — beyond CAP, drop the oldest half (FIFO via
        # OrderedDict if we cared). Simpler: hard cap at 1024 entries
        # which is ~17 events/s sustained for 60 s.
        if len(self._dispatched_ids) > 1024:
            # Conservative reset — re-posting an event after this hard
            # cap is harmless (system notification center dedups too).
            log.debug("dispatched_ids cap hit; resetting")
            self._dispatched_ids.clear()
