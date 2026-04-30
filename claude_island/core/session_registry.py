from __future__ import annotations

import threading

from .events import Event
from .models import Session


class SessionRegistry:
    """Live set of Claude Code sessions.

    Multiple sources (process scanner, JSONL watcher) converge here.
    All writes go through ``update()``, which always fires ``sessions_changed``
    so subscribers stay in sync regardless of whether the list actually changed.
    """

    def __init__(self) -> None:
        self.sessions_changed: Event[list[Session]] = Event()
        self.permission_required: Event[None] = Event()
        self._sessions: list[Session] = []
        self._lock = threading.Lock()

    def update(self, sessions: list[Session]) -> None:
        with self._lock:
            self._sessions = list(sessions)
        self.sessions_changed.emit(list(sessions))

    def require_permission(self) -> None:
        self.permission_required.emit(None)

    @property
    def sessions(self) -> list[Session]:
        with self._lock:
            return list(self._sessions)
