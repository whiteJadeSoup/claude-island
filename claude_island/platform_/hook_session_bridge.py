"""HookSessionBridge — connects the hook state machine to the session
registry + drives scanner-based liveness tombstones.

Solves two problems from Detail Design v2 §1.2:

  F-1: hook arrives before scanner sees the process. Without this bridge,
       SessionRegistry stays empty for ~10s (scanner tick interval) after
       a new Claude session starts → UI shows nothing → violates G1 (<1s
       discovery target). Fix: subscribe to ``live_state_changed``;
       whenever a hook event creates/updates a session in the state
       machine, if SessionRegistry doesn't yet have an entry with that
       uuid, upsert a placeholder (pid=PLACEHOLDER_PID) so the snapshot
       pipeline can render it immediately.

  F-2: hook stream may go dark while the process is still alive (listener
       restart) OR the process may die without firing SessionEnd (crash,
       kill -9). Without periodic reconciliation against scanner output,
       the state machine accumulates ghost entries. Fix: subscribe to
       ``sessions_changed``; for each uuid in the state machine that is
       NOT in the scanner's pid set, increment a miss counter. After
       MISS_THRESHOLD consecutive misses, force-tombstone the uuid in
       the state machine. The threshold is 2 because a single scanner
       tick can transiently miss a pid (process starting/scanner timing);
       requiring two consecutive misses filters that.

The bridge lives in ``platform_/`` because it imports SessionRegistry
(core) AND SessionStateMachine (core) AND mediates between them — it's
wiring, not policy. The reverse direction (core importing this) is
forbidden by import-linter.

Threading: subscribe callbacks fire on whatever thread emitted the event
— state_machine.live_state_changed emits from the HookServer thread,
session_registry.sessions_changed emits from the scanner thread. Both
callbacks acquire ``self._lock`` so the miss-counter and placeholder
upsert paths don't race.
"""
from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone

from claude_island.core.hook_events import SessionLiveState
from claude_island.core.models import Session
from claude_island.core.session_phase import SessionPhase
from claude_island.core.session_registry import PLACEHOLDER_PID, SessionRegistry
from claude_island.core.session_state_machine import SessionStateMachine

log = logging.getLogger(__name__)


class HookSessionBridge:
    """Subscribes to both signal sources and keeps them in sync.

    Construct AFTER both the state machine and the registry exist, but
    BEFORE the scanner starts producing ticks — otherwise the first
    tick may run before the bridge installs its placeholder reconcile
    rule, briefly showing 0 sessions even though hooks already
    populated state_machine."""

    # Consecutive scanner ticks a uuid must be absent before we force
    # tombstone. 1 would over-react to transient scanner gaps; 3+ is
    # slow to clean up real exits. 2 is the same threshold open-vibe-island
    # uses (SessionState.swift:397).
    MISS_THRESHOLD = 2

    def __init__(
        self,
        *,
        registry: SessionRegistry,
        state_machine: SessionStateMachine,
    ) -> None:
        self._reg = registry
        self._sm = state_machine
        # uuid → consecutive scanner-miss count. Single lock guards both
        # this dict and the upsert path so we don't have two readers
        # racing each other (one from state-changed, one from
        # scanner-changed).
        self._miss_count: dict[str, int] = {}
        self._lock = threading.Lock()
        # Subscriptions kept so they're not GC'd. Reactivex subscriptions
        # are reference-counted via the returned Disposable; dropping
        # the reference cancels the subscription. Keep them as instance
        # attrs.
        self._sub_state = state_machine.live_state_changed.subscribe(
            on_next=self._on_state_changed,
            on_error=lambda e: log.exception(
                "state_machine.live_state_changed subscription died: %s", e,
            ),
        )
        self._sub_scanner = registry.sessions_changed.subscribe(
            on_next=self._on_scanner_update,
            on_error=lambda e: log.exception(
                "registry.sessions_changed subscription died: %s", e,
            ),
        )

    def stop(self) -> None:
        """Idempotent. Disposes both subscriptions. Used in tests +
        a clean app shutdown path."""
        for sub in (self._sub_state, self._sub_scanner):
            try:
                sub.dispose()
            except Exception:
                pass

    # -- callbacks --------------------------------------------------------

    def _on_state_changed(self, changed_uuids: set[str]) -> None:
        """Hook event updated state_machine for one or more uuids.

        For each changed uuid that's NOT yet in the registry (or is
        in the registry without a uuid, e.g. scanner-only entry that
        we should associate with the new hook stream): upsert a
        placeholder Session so the snapshot pipeline picks it up.

        ENDED states are not placeholder-upserted — we don't want a
        recently-ended session to suddenly reappear as a placeholder
        because of the hook flush ordering."""
        for uuid in changed_uuids:
            live = self._sm.read(uuid)
            if live is None:
                continue
            if live.phase == SessionPhase.ENDED:
                # Don't reanimate a tombstoned session. The registry
                # entry (if any) is left alone — scanner controls
                # removal of real entries; placeholders age out via
                # the miss-counter path on the next tick.
                continue
            # Already known to the registry under this uuid? Then the
            # bridge has nothing to do — scanner has caught up.
            if any(s.session_uuid == uuid for s in self._reg.sessions):
                continue
            # Open-vibe-island alignment (2026-05-14): when the hook
            # shipped a JumpTarget with host_pid, use it directly. The
            # hook ran INSIDE the claude.exe process so host_pid is
            # authoritative — there's no race window where it could be
            # wrong. Falls back to PLACEHOLDER_PID for older hook.py
            # versions / capture failures (jt is None or host_pid==0).
            jt = live.jump_target
            real_pid = (
                jt.host_pid if (jt is not None and jt.host_pid > 0)
                else PLACEHOLDER_PID
            )
            self._reg.upsert(Session(
                pid=real_pid,
                project_path=live.cwd,
                session_uuid=uuid,
                last_activity=live.started_at,
            ))
            with self._lock:
                # Reset miss counter for this uuid in case it was being
                # tracked toward tombstone — we just got fresh activity.
                self._miss_count.pop(uuid, None)

    def _on_scanner_update(self, sessions: list[Session]) -> None:
        """Scanner produced a fresh full list of live processes.

        For each non-ENDED uuid in state_machine:
          * If the scanner can see it (matched by uuid OR by cwd when
            the uuid is empty — scanner doesn't read transcripts), reset
            the miss counter.
          * Else, increment the miss counter. At MISS_THRESHOLD, force
            tombstone in state_machine AND remove the placeholder from
            the registry (so UI stops showing it).

        Why "match by cwd too": a session that started before
        claude-island was running has uuid="" in scanner output;
        matching by cwd lets us keep state_machine's uuid-keyed entry
        from being miss-counted to ENDED when scanner has a same-cwd
        live process.

        Why filter placeholders out: sessions_changed re-emits the
        REGISTRY contents post-merge, which still contains placeholders
        that scanner couldn't graft (no matching cwd). Including them
        in seen_uuids/seen_cwds would have the bridge effectively
        "observing itself" and miss_count would never advance. The
        intent of "seen by scanner" requires a real pid (Bug A' 2026-05-13).
        """
        seen_uuids: set[str] = set()
        seen_cwds: set = set()
        for s in sessions:
            if s.pid <= 0:
                # Placeholder — scanner did NOT actually see this, so
                # don't let it count toward "session is alive".
                continue
            if s.session_uuid:
                seen_uuids.add(s.session_uuid)
            seen_cwds.add(s.project_path)

        with self._lock:
            for uuid, live in self._sm.snapshot().items():
                if live.phase == SessionPhase.ENDED:
                    self._miss_count.pop(uuid, None)
                    continue

                seen = uuid in seen_uuids or live.cwd in seen_cwds
                if seen:
                    self._miss_count.pop(uuid, None)
                    continue

                count = self._miss_count.get(uuid, 0) + 1
                if count >= self.MISS_THRESHOLD:
                    log.info(
                        "tombstoning %s — scanner missed it %d ticks in a row",
                        uuid, count,
                    )
                    self._miss_count.pop(uuid, None)
                    # Drop the lock briefly while tombstoning AND
                    # removing from registry — both emit signals that
                    # can re-enter this bridge's other callback (with
                    # the registry's own lock involved). Acquiring our
                    # own lock here is a different lock from the
                    # registry's, so we must not hold ours during the
                    # re-entrant calls.
                    self._lock.release()
                    try:
                        self._sm.tombstone(uuid)
                        # Drop any placeholder still in the registry
                        # for this uuid — without this the UI keeps
                        # showing the dead session (Bug A'' 2026-05-13).
                        self._reg.remove_by_uuid(uuid)
                    finally:
                        self._lock.acquire()
                else:
                    self._miss_count[uuid] = count
