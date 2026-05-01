"""Tests for SessionDiscovery shutdown discipline (B2)."""

from __future__ import annotations

import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from claude_island.core.models import Session
from claude_island.core.session_registry import SessionRegistry
from claude_island.platform_.session_discovery import SessionDiscovery


class FakeScanner:
    """Scanner stub that lets us inject delay and count invocations."""
    def __init__(self, delay: float = 0.0):
        self.delay = delay
        self.calls = 0
        self._lock = threading.Lock()

    def scan(self) -> list[Session]:
        with self._lock:
            self.calls += 1
        if self.delay:
            time.sleep(self.delay)
        return []


def test_stop_prevents_further_scans():
    """After stop() returns, no further scans should happen.

    Run a short scan_interval so we'd see many ticks if the loop were
    not stopped; assert call count stays bounded after stop().
    """
    reg = SessionRegistry()
    scanner = FakeScanner()
    discovery = SessionDiscovery(scanner=scanner, registry=reg, scan_interval=0.05)

    discovery.start()
    time.sleep(0.18)  # ~3 ticks expected
    discovery.stop()
    calls_after_stop = scanner.calls

    time.sleep(0.30)  # would be ~6 more ticks if still running
    assert scanner.calls == calls_after_stop, (
        f"scans continued after stop(): {scanner.calls} > {calls_after_stop}"
    )


def test_stop_during_in_flight_scan_does_not_re_arm_timer():
    """The race the fix targets: a scan that started just before stop()
    must not arm a new timer after stop() returns.

    We simulate this with a slow scanner: stop() is called while scan()
    is still sleeping. The next timer must NOT be scheduled.
    """
    reg = SessionRegistry()
    scanner = FakeScanner(delay=0.15)
    discovery = SessionDiscovery(scanner=scanner, registry=reg, scan_interval=0.05)

    discovery.start()  # kicks off _scan() on the calling thread (synchronous first call)
    # The first scan is now running synchronously (because start() calls _scan
    # directly, and _scan blocks for `delay` inside scanner.scan). To exercise
    # the in-flight race we need a separate thread:
    discovery.stop()  # called while initial scan still inside scanner.scan()

    calls_at_stop = scanner.calls
    time.sleep(0.40)
    assert scanner.calls == calls_at_stop, (
        f"timer re-armed after stop during in-flight scan: "
        f"{scanner.calls} > {calls_at_stop}"
    )
    assert discovery._timer is None


def test_stop_in_separate_thread_during_slow_scan_blocks_re_arm():
    """Real-world race: scan() is on the timer thread (slow), stop() is on
    the Qt main thread. stop() must win the lock and prevent re-arming."""
    reg = SessionRegistry()
    scanner = FakeScanner(delay=0.20)
    discovery = SessionDiscovery(scanner=scanner, registry=reg, scan_interval=0.05)

    # Run start() on a background thread so we can call stop() while
    # the synchronous first scan() is still inside the slow sleep.
    t = threading.Thread(target=discovery.start)
    t.start()
    time.sleep(0.05)  # let the first scan begin
    discovery.stop()
    t.join(timeout=1.0)

    calls_at_stop = scanner.calls
    time.sleep(0.30)
    assert scanner.calls == calls_at_stop


def test_start_after_stop_resumes():
    """Start should be re-entrant: a stop()-then-start() cycle resumes."""
    reg = SessionRegistry()
    scanner = FakeScanner()
    discovery = SessionDiscovery(scanner=scanner, registry=reg, scan_interval=0.05)

    discovery.start()
    time.sleep(0.12)
    discovery.stop()
    calls_after_first_run = scanner.calls

    discovery.start()
    time.sleep(0.12)
    discovery.stop()
    assert scanner.calls > calls_after_first_run
