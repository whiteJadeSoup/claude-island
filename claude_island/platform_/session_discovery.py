from __future__ import annotations

import threading

from claude_island.core.session_registry import SessionRegistry
from .protocols import ProcessScannerProtocol


class SessionDiscovery:
    """Drives periodic process scanning and pushes results into SessionRegistry.

    Runs a repeating timer on a daemon thread so it never blocks app shutdown.
    The scan interval is intentionally low (10 s) to catch new sessions quickly
    without hammering psutil on every keystroke.
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

    def start(self) -> None:
        """Run one scan immediately, then schedule recurring scans."""
        self._scan()

    def stop(self) -> None:
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

        self._registry.update(sessions)

        self._timer = threading.Timer(self._scan_interval, self._scan)
        self._timer.daemon = True
        self._timer.start()
