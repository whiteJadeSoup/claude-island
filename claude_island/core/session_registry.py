from __future__ import annotations

import threading

from reactivex.subject import Subject

from .models import Session


class SessionRegistry:
    """Live set of Claude Code sessions as observed by ProcessScanner.

    Holds the canonical list (pid, project_path/cwd, baseline
    last_activity = process create_time) and emits ``sessions_changed``
    when the list itself changes. Per-session JSONL-derived activity is
    NOT merged here — it lives uuid-keyed on
    ``JsonlParser._session_meta`` and is folded into ``SessionView`` by
    ``compose_session_view``. Keeping that path out of the registry
    prevents two sessions sharing a cwd from cross-contaminating each
    other's last_activity (the bug a previous project-keyed override
    used to cause).
    """

    def __init__(self) -> None:
        # on_next(list[Session]) synchronously notifies subscribers on
        # the calling thread. Subscribers wake the snapshotter; nobody
        # reads the data shape from this signal.
        self.sessions_changed: Subject[list[Session]] = Subject()
        self._sessions: list[Session] = []
        # Sentinel != any real list (None never compares equal to a list)
        # so the first update() always emits.
        self._last_emitted: list[Session] | None = None
        self._lock = threading.Lock()

    def update(self, sessions: list[Session]) -> None:
        """Replace the session list (typically called by the process scanner).

        Emits sessions_changed only if the list differs from the
        previously-emitted one. The scanner ticks every ~10s with usually
        identical content; skipping the redundant emit avoids work in every
        downstream UI subscriber for a no-op update.
        """
        with self._lock:
            self._sessions = list(sessions)
            if self._sessions == self._last_emitted:
                return
            self._last_emitted = list(self._sessions)
            snapshot = list(self._sessions)
        self.sessions_changed.on_next(snapshot)

    @property
    def sessions(self) -> list[Session]:
        with self._lock:
            return list(self._sessions)
