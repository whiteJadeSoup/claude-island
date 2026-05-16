"""WtFocusWorker pool + backlog + serialization tests.

Mirror of test_iterm2_focus_worker.py for the Windows-side worker.
"""
from __future__ import annotations

import logging
import threading
import time
from unittest import mock

import pytest
from PySide6.QtCore import QRunnable

from claude_island.platform_.terminals import _wt_fast_path as wfp


@pytest.fixture
def fresh_worker(monkeypatch):
    monkeypatch.setattr(wfp, "_worker_singleton", None)
    w = wfp.get_worker()
    yield w
    w.shutdown(timeout_ms=2000)


class _CountingTask(QRunnable):
    _concurrent_max = 0
    _concurrent_now = 0
    _lock = threading.Lock()

    def __init__(self, duration_s: float = 0.05) -> None:
        super().__init__()
        self.duration_s = duration_s
        self.started_at: float | None = None
        self.ended_at: float | None = None
        self._worker: wfp.WtFocusWorker | None = None

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


# ─────────────────────────────────────────────────────────────────────
# Serialization invariant (I-1 analogue)
# ─────────────────────────────────────────────────────────────────────


class TestSerialization:
    def test_pool_max_thread_count_is_one(self, fresh_worker):
        assert fresh_worker._pool.maxThreadCount() == 1

    def test_concurrent_tasks_run_serially(self, fresh_worker):
        """Critical for COM/UIA: max one in-flight task at a time."""
        _CountingTask.reset_counters()
        tasks = [_CountingTask(duration_s=0.02) for _ in range(5)]
        for t in tasks:
            assert fresh_worker.submit(t) is True

        fresh_worker._pool.waitForDone(2000)

        assert _CountingTask._concurrent_max == 1
        assert all(t.started_at is not None and t.ended_at is not None for t in tasks)

    def test_inflight_drops_to_zero_after_drain(self, fresh_worker):
        _CountingTask.reset_counters()
        for _ in range(3):
            fresh_worker.submit(_CountingTask(duration_s=0.01))
        fresh_worker._pool.waitForDone(2000)
        assert fresh_worker.backlog() == 0


# ─────────────────────────────────────────────────────────────────────
# Backlog reject (B-007 / Q-4)
# ─────────────────────────────────────────────────────────────────────


class TestBacklogReject:
    def test_under_threshold_accepts(self, fresh_worker):
        with mock.patch.object(fresh_worker, "_inflight", 5):
            t = _CountingTask(duration_s=0)
            assert fresh_worker.submit(t) is True
        fresh_worker._pool.waitForDone(2000)

    def test_at_reject_threshold_rejects(self, fresh_worker, caplog):
        with fresh_worker._counter_lock:
            fresh_worker._inflight = wfp.WtFocusWorker.BACKLOG_REJECT
        with caplog.at_level(logging.ERROR):
            t = _CountingTask(duration_s=0)
            assert fresh_worker.submit(t) is False
        # Reset for clean shutdown.
        with fresh_worker._counter_lock:
            fresh_worker._inflight = 0
        assert any("rejected pane select" in r.message for r in caplog.records)

    def test_above_threshold_rejects(self, fresh_worker):
        with fresh_worker._counter_lock:
            fresh_worker._inflight = wfp.WtFocusWorker.BACKLOG_REJECT + 5
        t = _CountingTask(duration_s=0)
        assert fresh_worker.submit(t) is False
        with fresh_worker._counter_lock:
            fresh_worker._inflight = 0

    def test_warn_threshold_still_accepts(self, fresh_worker, caplog):
        with fresh_worker._counter_lock:
            fresh_worker._inflight = wfp.WtFocusWorker.BACKLOG_WARN
        with caplog.at_level(logging.WARNING):
            t = _CountingTask(duration_s=0)
            assert fresh_worker.submit(t) is True
        fresh_worker._pool.waitForDone(2000)
        assert any("backlog=" in r.message for r in caplog.records)

    def test_reject_log_throttled(self, fresh_worker, caplog):
        """Repeated rejects within 60s only log once (ERROR throttle)."""
        with fresh_worker._counter_lock:
            fresh_worker._inflight = wfp.WtFocusWorker.BACKLOG_REJECT
        with caplog.at_level(logging.ERROR):
            for _ in range(5):
                fresh_worker.submit(_CountingTask(duration_s=0))
        reject_logs = [r for r in caplog.records if "rejected pane select" in r.message]
        assert len(reject_logs) == 1
        with fresh_worker._counter_lock:
            fresh_worker._inflight = 0
