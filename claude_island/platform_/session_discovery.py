from __future__ import annotations

import threading

from claude_island.core.session_registry import SessionRegistry
from .protocols import ProcessScannerProtocol


class SessionDiscovery:
    """Drives periodic process scanning and pushes results into SessionRegistry.

    Runs a repeating timer on a daemon thread so it never blocks app shutdown.
    The scan interval is intentionally low (10 s) to catch new sessions quickly
    without hammering psutil on every keystroke.

    Shutdown discipline:
    - stop() flips a flag and cancels the currently-scheduled timer.
    - _scan() checks the flag under the same lock before re-arming the next
      timer. Without this guard, an in-flight scan that started just before
      stop() is called would re-arm a fresh timer after stop() returns,
      keeping the loop running indefinitely and posting Qt events to a
      tearing-down event loop.
    """

    def __init__(
        self,
        *,
        scanner: ProcessScannerProtocol,
        registry: SessionRegistry,
        scan_interval: float = 10.0,
    ) -> None:
        self._scanner = scanner
        self._registry = registry
        self._scan_interval = scan_interval
        self._timer: threading.Timer | None = None
        self._stopped = False
        self._lock = threading.Lock()

    def start(self) -> None:
        """Run one scan immediately, then schedule recurring scans."""
        with self._lock:
            self._stopped = False
        self._scan()

    def stop(self) -> None:
        with self._lock:
            self._stopped = True
            if self._timer is not None:
                self._timer.cancel()
                self._timer = None

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _scan(self) -> None:
        try:
            sessions = self._scanner.scan()
        except Exception:
            sessions = []

        # The check-update-arm sequence must be atomic with stop(): if the
        # flag flipped between scanner.scan() and here, do nothing.
        # Otherwise stop() returns, the app starts tearing down its Qt
        # objects, and our registry.update emits into a half-destroyed
        # event loop. The lock window is short — registry.update only
        # iterates Event handlers and (via QtBridge) calls Signal.emit,
        # which is thread-safe and microseconds-fast.
        with self._lock:
            if self._stopped:
                return
            self._registry.update(sessions)
            self._timer = threading.Timer(self._scan_interval, self._scan)
            self._timer.daemon = True
            self._timer.start()
