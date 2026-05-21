"""Integration tests for FocusWorker + _PaneSelectTask serialization.

Strategy: real ``QThreadPool`` via FocusWorker (single-thread per design),
but task ``run()`` is replaced with a minimal stub that records start/end
events. Lets us assert:

  * I-1 — at most one task executes concurrently
  * §3.3 — backlog threshold triggers reject

We don't touch NSAppleScript or NSRunningApplication here. The
_PaneSelectTask body relies on cache.get_*_handler() returning a real
NSAppleScript instance to call executeAppleEvent_error_ on; mocking the
cache to return a stub that "executes" without real Foundation is the
test boundary.
"""
from __future__ import annotations

import threading
import time
from unittest import mock

import pytest
from PySide6.QtCore import QRunnable

from claude_island.platform_.terminals import _iterm_fast_path as fp


# ── Fixtures ──────────────────────────────────────────────────────────


@pytest.fixture
def fresh_worker(monkeypatch):
    """Return a fresh FocusWorker with no inflight history."""
    monkeypatch.setattr(fp, "_worker_singleton", None)
    w = fp.get_worker()
    yield w
    w.shutdown(timeout_ms=2000)


class _CountingTask(QRunnable):
    """Records when run() starts and ends; sleeps for ``duration_s``."""

    _concurrent_max = 0
    _concurrent_now = 0
    _lock = threading.Lock()

    def __init__(self, duration_s: float = 0.05) -> None:
        super().__init__()
        self.duration_s = duration_s
        self.started_at: float | None = None
        self.ended_at: float | None = None
        self._worker: fp.FocusWorker | None = None

    def run(self) -> None:
        try:
            with _CountingTask._lock:
                _CountingTask._concurrent_now += 1
                if _CountingTask._concurrent_now > _CountingTask._concurrent_max:
                    _CountingTask._concurrent_max = _CountingTask._concurrent_now
            self.started_at = time.monotonic()
            time.sleep(self.duration_s)
            self.ended_at = time.monotonic()
            with _CountingTask._lock:
                _CountingTask._concurrent_now -= 1
        finally:
            if self._worker is not None:
                self._worker._on_task_done()

    @classmethod
    def reset_counters(cls) -> None:
        with cls._lock:
            cls._concurrent_max = 0
            cls._concurrent_now = 0


# ── I-1: serialization ────────────────────────────────────────────────


class TestSerialization:
    def test_pool_max_thread_count_is_one(self, fresh_worker):
        assert fresh_worker._pool.maxThreadCount() == 1

    def test_concurrent_tasks_run_serially(self, fresh_worker):
        """Submit 5 tasks each sleeping 30 ms. Concurrent peak must be 1.

        If pool were size > 1 (or NSAppleScript instance were shared
        across threads), we'd see concurrent_max ≥ 2 and risk crashes.
        """
        _CountingTask.reset_counters()
        tasks = [_CountingTask(duration_s=0.03) for _ in range(5)]
        for t in tasks:
            assert fresh_worker.submit(t) is True

        # Wait for all tasks to drain.
        fresh_worker._pool.waitForDone(2000)

        assert _CountingTask._concurrent_max == 1
        # All tasks ran.
        assert all(t.started_at is not None and t.ended_at is not None for t in tasks)

    def test_inflight_drops_back_to_zero_after_drain(self, fresh_worker):
        _CountingTask.reset_counters()
        for _ in range(3):
            fresh_worker.submit(_CountingTask(duration_s=0.01))
        fresh_worker._pool.waitForDone(2000)
        assert fresh_worker.backlog() == 0


# ── §3.3: backlog reject ──────────────────────────────────────────────


class TestBacklogReject:
    def test_under_threshold_accepts(self, fresh_worker, monkeypatch):
        """Inflight under BACKLOG_REJECT (10) should always accept."""
        # Don't actually run tasks — fake the inflight counter.
        with mock.patch.object(fresh_worker, "_inflight", 5):
            t = _CountingTask(duration_s=0)
            assert fresh_worker.submit(t) is True
        # Drain whatever we submitted.
        fresh_worker._pool.waitForDone(2000)

    def test_at_reject_threshold_rejects(self, fresh_worker, caplog):
        """Inflight == BACKLOG_REJECT means submit returns False."""
        import logging
        # Manually set the counter without actually scheduling work.
        with fresh_worker._counter_lock:
            fresh_worker._inflight = fp.FocusWorker.BACKLOG_REJECT
        with caplog.at_level(logging.ERROR):
            t = _CountingTask(duration_s=0)
            assert fresh_worker.submit(t) is False
        # Reset for clean shutdown.
        with fresh_worker._counter_lock:
            fresh_worker._inflight = 0
        assert any("rejected pane select" in r.message for r in caplog.records)

    def test_above_threshold_rejects(self, fresh_worker):
        with fresh_worker._counter_lock:
            fresh_worker._inflight = fp.FocusWorker.BACKLOG_REJECT + 5
        t = _CountingTask(duration_s=0)
        assert fresh_worker.submit(t) is False
        # Reset for clean shutdown.
        with fresh_worker._counter_lock:
            fresh_worker._inflight = 0

    def test_warn_threshold_still_accepts(self, fresh_worker, caplog):
        """Inflight at BACKLOG_WARN (4): accepts but logs warning."""
        import logging
        with fresh_worker._counter_lock:
            fresh_worker._inflight = fp.FocusWorker.BACKLOG_WARN
        with caplog.at_level(logging.WARNING):
            t = _CountingTask(duration_s=0)
            assert fresh_worker.submit(t) is True
        fresh_worker._pool.waitForDone(2000)
        assert any("backlog=" in r.message for r in caplog.records)

    def test_reject_log_throttled(self, fresh_worker, caplog):
        """Repeated rejects within 60 s window should only log once."""
        import logging
        with fresh_worker._counter_lock:
            fresh_worker._inflight = fp.FocusWorker.BACKLOG_REJECT
        with caplog.at_level(logging.ERROR):
            for _ in range(5):
                fresh_worker.submit(_CountingTask(duration_s=0))
        # Only one ERROR log (the first); subsequent ones suppressed.
        reject_logs = [r for r in caplog.records if "rejected pane select" in r.message]
        assert len(reject_logs) == 1
        # Reset for clean shutdown.
        with fresh_worker._counter_lock:
            fresh_worker._inflight = 0


# ── _PaneSelectTask integration with worker ───────────────────────────


class TestPaneSelectTaskIntegration:
    """Run a _PaneSelectTask through the worker with a mocked cache so
    we don't need real NSAppleScript."""

    @pytest.fixture
    def patched_cache(self, monkeypatch):
        cache = fp.AppleScriptCache()
        monkeypatch.setattr(fp, "_cache_singleton", cache)
        return cache

    def test_task_with_id_runs_and_decrements_inflight(
        self, fresh_worker, patched_cache,
    ):
        # cache returns None handler (no NSAppleScript) → task hits the
        # "handler is None" branch and logs miss; that's the behavior
        # under PyObjC ImportError or compile failure.
        task = fp._PaneSelectTask(
            host_pid=12345, session_id="abc", tty=None,
        )
        assert fresh_worker.submit(task) is True
        fresh_worker._pool.waitForDone(2000)
        assert fresh_worker.backlog() == 0

    def test_task_run_swallows_exceptions(
        self, fresh_worker, patched_cache, caplog,
    ):
        """Force an exception inside the task body and verify the
        worker doesn't leak / crash."""
        import logging
        task = fp._PaneSelectTask(
            host_pid=12345, session_id="abc", tty=None,
        )
        with mock.patch.object(
            task, "_run_impl", side_effect=RuntimeError("boom"),
        ):
            with caplog.at_level(logging.WARNING):
                fresh_worker.submit(task)
                fresh_worker._pool.waitForDone(2000)
        assert any("_PaneSelectTask raised" in r.message for r in caplog.records)
        assert fresh_worker.backlog() == 0


class TestSubmitBacklogRaceFree:
    """I-3: read-decision-increment must run as a single critical
    section. Without the lock spanning all three, two concurrent
    submits at backlog=BACKLOG_REJECT-1 can both observe "room
    available" and both increment, exceeding the threshold."""

    def test_concurrent_submits_never_exceed_reject_threshold(
        self, fresh_worker,
    ):
        """Hammer submit() from many threads while it's at the edge
        of the reject threshold. Without atomic read+decide+increment
        the counter would tick past BACKLOG_REJECT; with the fix it
        is bounded."""
        import concurrent.futures

        # Replace the pool with a no-op stub so start() doesn't actually
        # consume the tasks — we want them ALL to "queue" (logically)
        # and verify the counter respects the cap.
        fresh_worker._pool = mock.Mock()
        # Prime at REJECT-1 so the very next accepted submit pushes it
        # to exactly REJECT; any concurrent extra must be rejected.
        target_start = fp.FocusWorker.BACKLOG_REJECT - 1
        with fresh_worker._counter_lock:
            fresh_worker._inflight = target_start

        def hammer(_n):
            task = fp._PaneSelectTask(host_pid=1, session_id="x", tty=None)
            try:
                return fresh_worker.submit(task)
            except Exception:
                return None

        with concurrent.futures.ThreadPoolExecutor(max_workers=16) as ex:
            results = list(ex.map(hammer, range(32)))

        # Exactly one submit should have been accepted (the one that
        # incremented from REJECT-1 to REJECT); all others must have
        # been rejected because the counter check is atomic with the
        # increment. Without the fix, multiple submits could race past
        # the check and accept simultaneously.
        accepted = sum(1 for r in results if r is True)
        rejected = sum(1 for r in results if r is False)
        assert accepted == 1, (
            f"expected exactly one accept at the boundary, got {accepted}; "
            f"counter race let extras through"
        )
        assert accepted + rejected == 32
        # Counter must end at exactly REJECT (one accept added 1).
        assert fresh_worker.backlog() == target_start + 1

        # Reset for clean shutdown.
        with fresh_worker._counter_lock:
            fresh_worker._inflight = 0


class TestSubmitInflightLeak:
    """C-2: ``_pool.start(task)`` can raise (pool shut down, Qt internal
    corruption). The increment happens BEFORE start, so without the
    decrement-on-exception guard the counter leaks forever — driving
    backlog up to BACKLOG_REJECT and silently breaking pane-select
    until app restart."""

    def test_inflight_decrements_when_pool_start_raises(self, fresh_worker):
        """If _pool.start raises, backlog must NOT leak. The exception
        re-raises so the caller knows submit didn't actually queue."""
        fake_pool = mock.Mock()
        fake_pool.start.side_effect = RuntimeError("pool gone")
        fresh_worker._pool = fake_pool
        task = fp._PaneSelectTask(host_pid=1, session_id="x", tty=None)

        assert fresh_worker.backlog() == 0
        with pytest.raises(RuntimeError, match="pool gone"):
            fresh_worker.submit(task)
        # Counter conserved — leak fixed.
        assert fresh_worker.backlog() == 0

    def test_repeated_start_failures_dont_block_future_submits(
        self, fresh_worker,
    ):
        """Without the fix, 10 failed submits would saturate the counter
        at BACKLOG_REJECT and every subsequent submit would silently
        return False — even after the pool recovered. Verify the
        counter stays at 0 across many failures so submission can
        recover the moment the pool starts working again."""
        fake_pool = mock.Mock()
        fake_pool.start.side_effect = RuntimeError("flake")
        fresh_worker._pool = fake_pool

        for _ in range(fp.FocusWorker.BACKLOG_REJECT + 5):
            task = fp._PaneSelectTask(host_pid=1, session_id="x", tty=None)
            with pytest.raises(RuntimeError):
                fresh_worker.submit(task)

        assert fresh_worker.backlog() == 0, (
            "leak: counter should still be 0 after every failed start"
        )
