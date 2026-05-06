"""Tests for PaneSiblingTracker — the sibling-pane cache that
resolves the split-pane click problem on Windows Terminal."""
from __future__ import annotations

import threading
import time

import pytest

from claude_island.platform_.wt_pane_siblings import PaneSiblingTracker


# ---------------------------------------------------------------------------
# Helpers — fake enumeration callable lets tests stage what UIA would
# return without running real UIA.
# ---------------------------------------------------------------------------

class _FakeEnumerator:
    """Stub for wt_uia.enumerate_active_tab_sentinels.

    Each call records the hwnd and returns whatever the test staged
    via ``stage(hwnd, sentinels)``. Default empty set."""

    def __init__(self) -> None:
        self.calls: list[int] = []
        self._stage: dict[int, set[str]] = {}
        self._call_event = threading.Event()
        self._slow_until: float | None = None

    def __call__(self, hwnd: int) -> set[str]:
        self.calls.append(hwnd)
        if self._slow_until is not None:
            # Block until released, so we can race two calls.
            while time.monotonic() < self._slow_until:
                time.sleep(0.005)
        self._call_event.set()
        return set(self._stage.get(hwnd, ()))

    def stage(self, hwnd: int, sentinels: set[str]) -> None:
        self._stage[hwnd] = sentinels

    def make_slow(self, duration_s: float) -> None:
        self._slow_until = time.monotonic() + duration_s


# ---------------------------------------------------------------------------
# update_from_active_tab — the synchronous learning entry point.
# ---------------------------------------------------------------------------

class TestUpdateFromActiveTab:

    def test_records_pairwise_siblings(self):
        """Three sentinels in one tab → each maps to the other two."""
        enum = _FakeEnumerator()
        enum.stage(0xCAFE, {"ci:a", "ci:b", "ci:c"})
        tracker = PaneSiblingTracker(enumerate_fn=enum)

        tracker.update_from_active_tab(0xCAFE)

        assert tracker.siblings_of("ci:a") == {"ci:b", "ci:c"}
        assert tracker.siblings_of("ci:b") == {"ci:a", "ci:c"}
        assert tracker.siblings_of("ci:c") == {"ci:a", "ci:b"}

    def test_single_pane_tab_records_empty_sibling_set(self):
        """A non-split tab has one TermControl → no siblings, but the
        entry IS created so siblings_of is precise (empty, not 'unknown')."""
        enum = _FakeEnumerator()
        enum.stage(0xCAFE, {"ci:a"})
        tracker = PaneSiblingTracker(enumerate_fn=enum)

        tracker.update_from_active_tab(0xCAFE)

        assert tracker.siblings_of("ci:a") == set()

    def test_full_replace_drops_closed_pane(self):
        """First obs: tab has {a, b, c}. Second obs (after user closed
        c): tab has {a, b}. Cache must overwrite, not union — c must
        not linger as a's stale sibling."""
        enum = _FakeEnumerator()
        tracker = PaneSiblingTracker(enumerate_fn=enum)

        enum.stage(0xCAFE, {"ci:a", "ci:b", "ci:c"})
        tracker.update_from_active_tab(0xCAFE)

        enum.stage(0xCAFE, {"ci:a", "ci:b"})
        tracker.update_from_active_tab(0xCAFE)

        assert tracker.siblings_of("ci:a") == {"ci:b"}  # c gone ✓
        assert tracker.siblings_of("ci:b") == {"ci:a"}
        # c's entry is left stale (we didn't observe its tab); that's
        # acceptable — its sentinel sits in cache but next click on
        # it will fail step 1 (tab gone) and step 2 (siblings stale)
        # and trigger schedule_update.

    def test_empty_active_tab_does_not_touch_cache(self):
        """If the enumerator returns nothing (no ci:* sentinels in the
        active tab — e.g. user is on a PowerShell tab), the cache must
        not be wiped. Other tabs' learnings stay valid."""
        enum = _FakeEnumerator()
        tracker = PaneSiblingTracker(enumerate_fn=enum)

        # Establish: tab containing {a, b}.
        enum.stage(0xCAFE, {"ci:a", "ci:b"})
        tracker.update_from_active_tab(0xCAFE)

        # User switches to a non-claude tab; UIA returns empty.
        enum.stage(0xCAFE, set())
        tracker.update_from_active_tab(0xCAFE)

        # a/b still know each other.
        assert tracker.siblings_of("ci:a") == {"ci:b"}
        assert tracker.siblings_of("ci:b") == {"ci:a"}

    def test_siblings_of_returns_fresh_copy(self):
        """Caller must be safe to iterate without holding the lock —
        a concurrent update should not mutate the returned set."""
        enum = _FakeEnumerator()
        enum.stage(0xCAFE, {"ci:a", "ci:b"})
        tracker = PaneSiblingTracker(enumerate_fn=enum)
        tracker.update_from_active_tab(0xCAFE)

        sibs = tracker.siblings_of("ci:a")
        sibs.add("ci:injected")  # mutate caller's copy

        assert tracker.siblings_of("ci:a") == {"ci:b"}  # cache unchanged

    def test_siblings_of_unknown_sentinel_returns_empty(self):
        tracker = PaneSiblingTracker(enumerate_fn=_FakeEnumerator())
        assert tracker.siblings_of("ci:never_seen") == set()


# ---------------------------------------------------------------------------
# schedule_update — fire-and-forget refresh used at click time.
# ---------------------------------------------------------------------------

class TestScheduleUpdate:

    def test_runs_in_background_eventually(self):
        """schedule_update returns instantly; the refresh happens on
        a daemon thread. We poll the cache until it shows the new
        observation."""
        enum = _FakeEnumerator()
        enum.stage(0xCAFE, {"ci:x", "ci:y"})
        tracker = PaneSiblingTracker(enumerate_fn=enum)

        tracker.schedule_update(0xCAFE)

        # Poll up to 1s for the daemon thread to finish.
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline:
            if tracker.siblings_of("ci:x") == {"ci:y"}:
                return
            time.sleep(0.01)
        pytest.fail("schedule_update did not refresh cache within 1s")

    def test_returns_immediately(self):
        """Click handler must not block. Even if the underlying
        enumeration is slow, schedule_update returns instantly."""
        enum = _FakeEnumerator()
        enum.make_slow(0.5)  # simulate slow UIA enumeration
        enum.stage(0xCAFE, {"ci:a"})
        tracker = PaneSiblingTracker(enumerate_fn=enum)

        t0 = time.monotonic()
        tracker.schedule_update(0xCAFE)
        elapsed_ms = (time.monotonic() - t0) * 1000

        assert elapsed_ms < 50, f"schedule_update blocked for {elapsed_ms}ms"

    def test_single_flight_drops_concurrent_calls(self):
        """A burst of click-time refresh requests must not spawn N
        threads. The second call while the first is still running
        is dropped."""
        enum = _FakeEnumerator()
        enum.make_slow(0.3)
        enum.stage(0xCAFE, {"ci:a"})
        tracker = PaneSiblingTracker(enumerate_fn=enum)

        # First call kicks off a slow refresh.
        tracker.schedule_update(0xCAFE)
        # Burst of 9 more while it's still in flight.
        for _ in range(9):
            tracker.schedule_update(0xCAFE)

        # Wait for in-flight to drain.
        time.sleep(0.5)

        # Only one enumeration ran, despite 10 schedule_update calls.
        assert len(enum.calls) == 1, (
            f"single-flight broken: {len(enum.calls)} enumerations ran"
        )

    def test_swallows_enumerator_exception(self):
        """A broken enumerator must not crash the daemon thread or
        leave the refresh lock held forever."""
        def _broken(_hwnd: int) -> set[str]:
            raise RuntimeError("UIA disconnected")

        tracker = PaneSiblingTracker(enumerate_fn=_broken)

        # Should not raise.
        tracker.schedule_update(0xCAFE)

        # And the refresh lock must release so the next call can fire.
        time.sleep(0.1)  # let the daemon hit the except path
        # If lock leaked, this second schedule would also no-op
        # (acquire returns False); we can't directly test acquired
        # state, so assert that follow-up scheduling still tries
        # the enumerator.
        calls_before = []
        def _track(hwnd: int) -> set[str]:
            calls_before.append(hwnd)
            return set()
        tracker._enumerate = _track  # swap in
        tracker.schedule_update(0xCAFE)
        time.sleep(0.1)
        assert calls_before == [0xCAFE], (
            "refresh lock leaked after exception — second schedule never ran"
        )


# ---------------------------------------------------------------------------
# Concurrency — cache reads/writes from different threads must not
# corrupt the dict.
# ---------------------------------------------------------------------------

class TestThreadSafety:

    def test_concurrent_read_write_no_corruption(self):
        """Read on one thread while another writes — must not raise
        and must not return malformed sets."""
        enum = _FakeEnumerator()
        enum.stage(0xCAFE, {"ci:a", "ci:b", "ci:c"})
        tracker = PaneSiblingTracker(enumerate_fn=enum)

        stop = threading.Event()
        errors: list[BaseException] = []

        def _writer() -> None:
            try:
                while not stop.is_set():
                    tracker.update_from_active_tab(0xCAFE)
            except BaseException as exc:
                errors.append(exc)

        def _reader() -> None:
            try:
                while not stop.is_set():
                    s = tracker.siblings_of("ci:a")
                    # Each read should give us a valid subset.
                    assert s.issubset({"ci:b", "ci:c"})
            except BaseException as exc:
                errors.append(exc)

        threads = [
            threading.Thread(target=_writer, daemon=True)
            for _ in range(2)
        ] + [
            threading.Thread(target=_reader, daemon=True)
            for _ in range(4)
        ]
        for t in threads:
            t.start()
        time.sleep(0.2)
        stop.set()
        for t in threads:
            t.join(timeout=1.0)

        assert not errors, f"concurrent access errors: {errors}"
