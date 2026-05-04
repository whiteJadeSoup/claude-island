"""LaunchIntentRegistry — the third source feeding ``Snapshotter``.

A ``LaunchIntent`` represents "the user just hit Resume on a dormant
session". The intent lives briefly:

* ``add(intent)``   — UI calls this immediately after
  ``TerminalDispatcher.launch()`` returns a SpawnResult, then triggers
  ``snapshotter.wake()`` so the next snapshot shows the row in the
  *launching* state (⏳) instead of *dormant* (▶ Resume).

* ``reconcile(live_uuids, now)`` — Snapshotter calls this on every
  build. Two pruning rules apply:
    1. If the intent's uuid now appears in live_uuids, the new
       claude.exe was detected → discard the intent (it has been
       upgraded to live).
    2. If ``now - intent.requested_at > ttl``, give up — claude
       didn't show up in time → discard. UI will see the row reappear
       in dormant and (optionally) toast a "couldn't detect" message.

There is **no internal timer / scheduler / watcher**. Reconcile only
runs on Snapshotter wakes, which are triggered by:
* ProcessScanner ticks (≤10 s) → live appears → upgrade
* JsonlParser activity → another reason to wake
* This module's own ``add()`` doesn't trigger wake — UI does that
  immediately after add() so the launching row appears within ~100 ms.

This means: in a dead-quiet environment with no live sessions and no
JSONL writes, an unfulfilled intent could outlive its ttl by up to
``scan_interval`` seconds. That's acceptable — the user will see ⏳ a
bit longer than 30 s in pathological cases, but never forever (the
process scanner always ticks). If this ever becomes a real-world
problem, add ``snapshotter.wake_in(ttl + 1)`` to ``add()``; for now,
keeping the registry timer-free is simpler.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path


@dataclass(frozen=True)
class LaunchIntent:
    """One pending Resume request waiting for ProcessScanner to confirm.

    Carries the metadata the HistoryDrawer needs to render a launching
    row (terminal_name + terminal_pid for the diagnostic toast on
    timeout) and the raw flags the user effectively passed to ``claude``
    (so future telemetry / debugging can see what got run).
    """
    session_uuid: str
    cwd: Path
    flags: tuple[str, ...]      # e.g. ('--dangerously-skip-permissions',)
    terminal_name: str          # 'windows-terminal' / 'iterm2'
    terminal_pid: int           # spawned host process pid (wt.exe / osascript)
    requested_at: datetime      # tz-aware UTC


class LaunchIntentRegistry:
    """Thread-safe short-lived store of pending Resume intents.

    Used by ``Snapshotter._build_snapshot``'s reconcile pass and by
    HistoryDrawer's Resume click handler. No subscribers / signals —
    callers explicitly trigger ``snapshotter.wake()`` after ``add()``;
    Snapshotter calls ``reconcile()`` + ``snapshot()`` during build.
    """

    def __init__(self, *, ttl_seconds: float = 30.0) -> None:
        # Why 30s default: that's enough for ProcessScanner's 10s tick to
        # hit at least twice (handles a boot in progress + the first scan
        # being unlucky), plus headroom for slow shells / antivirus scans.
        self._ttl = timedelta(seconds=ttl_seconds)
        self._intents: dict[str, LaunchIntent] = {}
        self._lock = threading.Lock()

    # ── write API ─────────────────────────────────────────────────────

    def add(self, intent: LaunchIntent) -> None:
        """Register a new intent. If the same uuid already has an intent
        (rare — would mean the user double-clicked Resume), the newer
        intent overwrites the older one."""
        with self._lock:
            self._intents[intent.session_uuid] = intent

    def discard(self, session_uuid: str) -> None:
        """Idempotent removal by uuid."""
        with self._lock:
            self._intents.pop(session_uuid, None)

    # ── read API ──────────────────────────────────────────────────────

    def snapshot(self) -> tuple[LaunchIntent, ...]:
        """Lock-protected copy of the current intent set, sorted by
        ``requested_at`` desc so the UI can render newest-first."""
        with self._lock:
            return tuple(sorted(
                self._intents.values(),
                key=lambda i: i.requested_at,
                reverse=True,
            ))

    # ── maintenance API (called by Snapshotter._build_snapshot) ──────

    def reconcile(self, *, live_uuids: set[str], now: datetime) -> None:
        """Two-rule prune:
            1. If intent.uuid in live_uuids → upgraded → discard.
            2. If now - requested_at > ttl → timed out → discard.

        Both rules run together inside one lock acquire so callers
        observe a single atomic state transition."""
        with self._lock:
            for uuid in list(self._intents.keys()):
                intent = self._intents[uuid]
                if uuid in live_uuids:
                    del self._intents[uuid]
                    continue
                if now - intent.requested_at > self._ttl:
                    del self._intents[uuid]
