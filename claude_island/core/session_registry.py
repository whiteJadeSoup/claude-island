from __future__ import annotations

import dataclasses
import threading
from datetime import datetime

from .events import Event
from .models import Session, project_hash


class SessionRegistry:
    """Live set of Claude Code sessions.

    Two data sources converge here:
    - ProcessScanner (every ~10s): produces the canonical session list with
      pid, project_path (cwd), and a baseline last_activity = process start.
    - JsonlParser: emits per-project activity timestamps as JSONL lines arrive.

    JSONL activity does NOT itself trigger ``sessions_changed`` — it stores an
    override keyed by Claude Code's project hash. The next ``update()`` cycle
    merges the override into the session before emitting. This keeps the UI
    refresh rate bounded by the scanner cadence and avoids a flood of
    sessions_changed events while a session is actively producing turns.
    """

    def __init__(self) -> None:
        self.sessions_changed: Event[list[Session]] = Event()
        self._sessions: list[Session] = []
        self._activity_overrides: dict[str, datetime] = {}
        # Sentinel != any real list (None never compares equal to a list)
        # so the first update() always emits.
        self._last_emitted: list[Session] | None = None
        self._lock = threading.Lock()

    def update(self, sessions: list[Session]) -> None:
        """Replace the session list (typically called by the process scanner).

        Emits sessions_changed only if the *enriched* list differs from the
        previously-emitted one. The scanner ticks every ~10s with usually
        identical content; skipping the redundant emit avoids work in every
        downstream UI subscriber (panel row diff, capsule label rebuild)
        for a no-op update.
        """
        with self._lock:
            self._sessions = list(sessions)
            enriched = self._apply_overrides_locked(sessions)
            if enriched == self._last_emitted:
                return
            self._last_emitted = list(enriched)
        self.sessions_changed.emit(enriched)

    def update_activity(self, payload: tuple[str, datetime]) -> None:
        """Record a JSONL activity timestamp for a project.

        Called via QtBridge from JsonlParser.activity_updated. Stores the
        latest timestamp for the given project hash; does NOT emit. The next
        ``update()`` (or any other emit path) will pick up the override.
        """
        proj_hash, ts = payload
        with self._lock:
            existing = self._activity_overrides.get(proj_hash)
            if existing is None or ts > existing:
                self._activity_overrides[proj_hash] = ts

    @property
    def sessions(self) -> list[Session]:
        with self._lock:
            return self._apply_overrides_locked(self._sessions)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _apply_overrides_locked(self, sessions: list[Session]) -> list[Session]:
        if not self._activity_overrides:
            return list(sessions)
        out: list[Session] = []
        for s in sessions:
            override = self._activity_overrides.get(project_hash(s.project_path))
            if override is not None and override > s.last_activity:
                out.append(dataclasses.replace(s, last_activity=override))
            else:
                out.append(s)
        return out
