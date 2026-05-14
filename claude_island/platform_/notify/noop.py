"""No-op notification backend.

Used as the default for unsupported platforms (Linux without a usable
tray) and as the test fixture for unit tests that don't want real
notifications spawned. Always returns True so dispatcher policy tests
that mock backends can assert on ``backend.post`` calls without seeing
False propagate as a "failure".
"""
from __future__ import annotations

import logging
from threading import Lock

from .protocols import NotifyKindHint

log = logging.getLogger(__name__)


class NoopNotifyBackend:
    """Records every post call (in memory) and returns True.

    The recorded calls are useful for tests; production callers can
    ignore ``posted_calls``.
    """

    def __init__(self) -> None:
        self._calls: list[tuple[str, str, NotifyKindHint]] = []
        self._lock = Lock()

    def post(self, *, title: str, body: str, kind: NotifyKindHint = NotifyKindHint.INFO) -> bool:
        with self._lock:
            self._calls.append((title, body, kind))
        log.debug("noop notify: title=%r body=%r kind=%s", title, body, kind.value)
        return True

    @property
    def posted_calls(self) -> list[tuple[str, str, NotifyKindHint]]:
        """Snapshot of all recorded calls (in chronological order).
        Returns a copy so callers can safely iterate even while new
        posts arrive on another thread."""
        with self._lock:
            return list(self._calls)

    def clear(self) -> None:
        """Reset the call log. Useful between test cases."""
        with self._lock:
            self._calls.clear()
