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
