"""Windows Terminal focus fast-path — async UIA tab select on a worker thread.

See ``design/2026-05-wt-focus-performance.md`` for the full design.

The macOS sibling (``_iterm_fast_path.py``) splits the slow part of
focus onto a worker so the main thread returns within ~1 ms. The
Windows port mirrors the architecture with three caveats specific to
this side:

1. **Foreground raise stays on main thread.** Win32's
   ``SetForegroundWindow`` is already fast (~few ms) AND has the
   precondition "caller must currently be foreground." Our panel is
   foreground at click time; the worker thread is NOT. So
   ``_force_foreground`` must run on the main thread, as it does
   today. The fast-path module only schedules the UIA work.

2. **COM apartment.** ``uiautomation`` (via ``comtypes``) caches a
   process-wide ``IUIAutomation`` pointer; multiple threads calling
   it without explicit ``CoInitializeEx`` risk apartment-mismatch
   errors (``RPC_E_CHANGED_MODE`` / ``RPC_E_WRONG_THREAD``). The
   worker thread calls ``pythoncom.CoInitializeEx(COINIT_APARTMENTTHREADED)``
   on its first task; subsequent tasks on the same thread re-enter
   the existing STA. See review finding C-001.

3. **No caching layer (yet).** UIA element references (TabControl,
   TabItemControl) could be cached per ``wt_hwnd`` to skip the
   ``ControlFromHandle`` walk, but the design defers that — first
   ship the worker split, then measure whether 12 ms warm
   ``select_tab_by_title`` matters (review finding B-004 / D-4).

Caller contract::

    # main thread:
    1. resolve wt_hwnd (prehook → adapter cache → legacy resolve)
    2. _force_foreground(wt_hwnd)                          [~few ms]
    3. _wt_fast_path.try_schedule(...)                     [<1 ms]
    4. return True

    # worker thread (async):
    5. UIA select_tab_by_title(expected)
    6. on miss: set_console_title + wait_for_tab_name + select
    7. on miss: try sibling_sentinels in order
    8. on miss: _try_smart_guess_select / diagnostic
"""
from __future__ import annotations

import logging
import sys
import threading
import time
from typing import Any

from PySide6.QtCore import QRunnable, QThreadPool

log = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────
# Lazy dependency probe
# ─────────────────────────────────────────────────────────────────────

_HAS_DEPS: bool | None = None
_pythoncom: Any = None
_win32_console: Any = None
_wt_uia: Any = None
_COINIT_APARTMENTTHREADED: int | None = None


def _ensure_deps() -> bool:
    """Probe + cache pythoncom + project deps. Returns True iff available.

    Falls back to False on any ImportError (non-Windows, missing
    ``pywin32`` / ``uiautomation``). Caller treats False as "no fast
    path" and uses the legacy synchronous chain.

    TODO(windows-verify): the cached module flag means a False result
    is sticky for the process. If a user adds the missing dep
    mid-session that won't be picked up until restart. Acceptable —
    we don't expect runtime install/uninstall.
    """
    global _HAS_DEPS, _pythoncom, _win32_console, _wt_uia
    global _COINIT_APARTMENTTHREADED

    if _HAS_DEPS is not None:
        return _HAS_DEPS

    if sys.platform != "win32":
        _HAS_DEPS = False
        return False

    try:
        import pythoncom  # type: ignore[import-not-found]
        from claude_island.platform_ import win32_console, wt_uia
    except ImportError as e:
        log.info("wt fast-path disabled: deps unavailable (%s)", e)
        _HAS_DEPS = False
        return False

    _pythoncom = pythoncom
    _win32_console = win32_console
    _wt_uia = wt_uia
    # pythoncom.COINIT_APARTMENTTHREADED is the STA flag; we use STA
    # because uiautomation's IUIAutomation pointer behaves correctly
    # under STA per microsoft's UI Automation API guidance.
    _COINIT_APARTMENTTHREADED = getattr(
        pythoncom, "COINIT_APARTMENTTHREADED", 0x2,
    )
    _HAS_DEPS = True
    return True


# ─────────────────────────────────────────────────────────────────────
# FocusWorker — single-thread QThreadPool wrapper
# ─────────────────────────────────────────────────────────────────────
#
# Mirrors ``_iterm_fast_path.FocusWorker``. Constants reused verbatim
# per review finding B-007 / Q-4.

_BACKLOG_WARN = 4
_BACKLOG_REJECT = 10
_SHUTDOWN_TIMEOUT_MS = 500


class WtFocusWorker:
    """``QThreadPool`` of size 1, FIFO. Single-worker is a hard
    requirement: ``uiautomation``'s IUIAutomation pointer is STA-bound,
    and our chosen COINIT model (STA) requires serialised access.

    The first task to run on the worker thread initialises COM. Once
    initialised, the thread keeps the STA for its lifetime (QThreadPool
    workers persist between tasks)."""

    BACKLOG_WARN = _BACKLOG_WARN
    BACKLOG_REJECT = _BACKLOG_REJECT

    def __init__(self) -> None:
        self._pool = QThreadPool()
        self._pool.setMaxThreadCount(1)
        self._counter_lock = threading.Lock()
        self._inflight = 0
        # Suppress duplicate reject ERRORs within a window.
        self._last_reject_log_at: float = 0.0

    def submit(self, task: _WtFocusTask) -> bool:
        """Enqueue a task. Returns True if accepted, False if backlog
        threshold reached (main thread already raised the window;
        worker rejection just means pane stays at previous position)."""
        with self._counter_lock:
            backlog = self._inflight

        if backlog >= self.BACKLOG_REJECT:
            now = time.monotonic()
            if now - self._last_reject_log_at > 60.0:
                log.error(
                    "WtFocusWorker rejected pane select; backlog=%d; "
                    "WT engine likely hung",
                    backlog,
                )
                self._last_reject_log_at = now
            return False
        if backlog >= self.BACKLOG_WARN:
            log.warning("WtFocusWorker backlog=%d", backlog)

        task._worker = self
        with self._counter_lock:
            self._inflight += 1
        self._pool.start(task)
        return True

    def _on_task_done(self) -> None:
        with self._counter_lock:
            self._inflight = max(0, self._inflight - 1)

    def backlog(self) -> int:
        with self._counter_lock:
            return self._inflight

    def shutdown(self, timeout_ms: int = _SHUTDOWN_TIMEOUT_MS) -> None:
        self._pool.waitForDone(timeout_ms)


_worker_singleton: WtFocusWorker | None = None


def get_worker() -> WtFocusWorker:
    """Return the process-wide WtFocusWorker singleton (lazy init)."""
    global _worker_singleton
    if _worker_singleton is None:
        _worker_singleton = WtFocusWorker()
    return _worker_singleton


# ─────────────────────────────────────────────────────────────────────
# Worker thread COM initialization
# ─────────────────────────────────────────────────────────────────────
#
# Per review C-001: each worker thread must explicitly CoInitialize
# before touching uiautomation. We track per-thread init via a
# threading.local so re-entrant tasks on the same thread don't double-init.

_thread_local = threading.local()


def _ensure_com_apartment() -> None:
    """Initialise STA on the current thread. Idempotent.

    Called as the first action of every ``_WtFocusTask.run``. The
    pool has maxThreadCount=1, so the same worker thread services every
    task and pays the CoInitialize cost only once (first task).

    TODO(windows-verify): on a Windows host, confirm that ``comtypes``'
    auto-init does NOT preempt our explicit init. If it does, we must
    import ``uiautomation`` AFTER ``CoInitializeEx`` in the worker, not
    at module top. Mitigation: ``import uiautomation`` is already done
    lazily inside ``wt_uia.py``'s functions, so a worker thread's
    first ``uiautomation`` call happens AFTER our CoInitialize.
    """
    if getattr(_thread_local, "com_initialized", False):
        return
    if _pythoncom is None:
        # _ensure_deps must have run first; defensive bail.
        return
    try:
        _pythoncom.CoInitializeEx(_COINIT_APARTMENTTHREADED)
        _thread_local.com_initialized = True
    except Exception as e:
        # ``RPC_E_CHANGED_MODE`` (0x80010106) means this thread was
        # already inited in a different mode. Recover: try plain
        # CoInitialize (STA equivalent in most builds).
        log.warning(
            "CoInitializeEx failed (%s); falling back to plain CoInitialize",
            e,
        )
        try:
            _pythoncom.CoInitialize()
            _thread_local.com_initialized = True
        except Exception as e2:
            log.error("COM init failed on worker thread: %s", e2)


# ─────────────────────────────────────────────────────────────────────
# _WtFocusTask — runs on the worker thread
# ─────────────────────────────────────────────────────────────────────


class _WtFocusTask(QRunnable):
    """One tab-select attempt. Mirror of ``_iterm_fast_path._PaneSelectTask``.

    Fields are captured at construction time (frozen-by-convention) so
    the worker doesn't read from a potentially-stale SessionView.
    """

    def __init__(
        self,
        *,
        pid: int,
        wt_hwnd: int,
        expected_title: str | None,
        sibling_sentinels: tuple[str, ...] = (),
    ) -> None:
        super().__init__()
        if wt_hwnd <= 0:
            raise ValueError(f"wt_hwnd must be positive, got {wt_hwnd}")
        if not expected_title and not sibling_sentinels:
            raise ValueError(
                "at least one of expected_title or sibling_sentinels required"
            )
        self.pid = int(pid)
        self.wt_hwnd = int(wt_hwnd)
        self.expected_title = expected_title or None
        self.sibling_sentinels = tuple(sibling_sentinels)
        self.created_at = time.monotonic()
        self._worker: WtFocusWorker | None = None

    def run(self) -> None:
        """QThreadPool entry point. Runs on the worker thread."""
        _ensure_com_apartment()
        try:
            self._run_impl()
        except Exception as e:
            log.warning("_WtFocusTask raised: %s", e)
        finally:
            if self._worker is not None:
                self._worker._on_task_done()

    def _run_impl(self) -> None:
        # 1) Try direct tab select by expected_title.
        if self.expected_title and _wt_uia.select_tab_by_title(
            self.wt_hwnd, self.expected_title,
        ):
            log.debug(
                "wt fast-path: selected by expected_title hwnd=%s",
                hex(self.wt_hwnd),
            )
            return

        # 2) Title drift: re-assert sentinel via SetConsoleTitleW,
        # wait for WT to mirror it into TabItem.Name, retry select.
        # Skip when pid is unknown (placeholder) — set_console_title
        # needs a real pid for AttachConsole.
        if self.expected_title and self.pid > 0:
            if _win32_console.set_console_title(self.pid, self.expected_title):
                if _wt_uia.wait_for_tab_name(
                    self.wt_hwnd, self.expected_title, timeout_ms=80,
                ) and _wt_uia.select_tab_by_title(
                    self.wt_hwnd, self.expected_title,
                ):
                    log.debug(
                        "wt fast-path: selected after title re-assert hwnd=%s",
                        hex(self.wt_hwnd),
                    )
                    return

        # 3) Sibling sentinel fallback (inactive-pane case in a split tab).
        for sib in self.sibling_sentinels:
            if sib and sib != self.expected_title:
                if _wt_uia.select_tab_by_title(self.wt_hwnd, sib):
                    log.debug(
                        "wt fast-path: selected via sibling sentinel %r",
                        sib,
                    )
                    return

        # 4) Smart-guess (suppressApplicationTitle case).
        # Late-import to avoid circular dependency: windows_terminal
        # imports this module at top level, so we can only reach the
        # adapter's private helpers inside a function body.
        from claude_island.platform_.terminals.windows_terminal import (
            _try_smart_guess_select,
            _emit_suppress_title_diagnostic,
        )
        known: set[str] = set(self.sibling_sentinels)
        # Visible ci:* tabs are also "known" sentinels — exclude them.
        try:
            known.update(_wt_uia.list_ci_tab_names(self.wt_hwnd))
        except Exception:
            pass
        # Our own expected_title belongs in candidates, not known.
        if self.expected_title:
            known.discard(self.expected_title)
        if _try_smart_guess_select(self.wt_hwnd, exclude_names=known):
            log.debug(
                "wt fast-path: smart-guess selected for expected=%r",
                self.expected_title,
            )
            return

        # 5) All strategies missed. Log the diagnostic (once per
        # process) — surfaces the suppressApplicationTitle case to
        # the user. Main thread already raised WT to foreground.
        log.info(
            "wt pane select miss (pid=%d, expected=%r, sib_count=%d)",
            self.pid, self.expected_title, len(self.sibling_sentinels),
        )
        if self.expected_title and self.expected_title.startswith("ci:"):
            try:
                _emit_suppress_title_diagnostic(
                    self.expected_title.removeprefix("ci:"),
                )
            except Exception as e:
                log.debug("suppress diagnostic failed: %s", e)


# ─────────────────────────────────────────────────────────────────────
# Main entry point — caller is the WindowsTerminalAdapter.focus()
# ─────────────────────────────────────────────────────────────────────


def try_schedule(
    *,
    pid: int,
    wt_hwnd: int,
    expected_title: str | None,
    sibling_sentinels: tuple[str, ...] = (),
) -> bool:
    """Schedule a worker-thread tab-select. Returns True if accepted.

    Caller (``WindowsTerminalAdapter.focus``) must have already:
      1. Resolved ``wt_hwnd`` (validated via IsWindow + GetClassName).
      2. Called ``_force_foreground(wt_hwnd)`` on the main thread.

    Returns False on:
      - dep probe failure (non-Windows, missing pywin32 / uiautomation),
      - backlog rejection (WT engine likely hung),
      - constructor validation failure.

    Fire-and-forget — caller doesn't wait for the worker's result.
    """
    if not _ensure_deps():
        return False

    try:
        task = _WtFocusTask(
            pid=pid,
            wt_hwnd=wt_hwnd,
            expected_title=expected_title,
            sibling_sentinels=sibling_sentinels,
        )
    except ValueError as e:
        log.warning("_WtFocusTask construction failed: %s", e)
        return False

    return get_worker().submit(task)


def prewarm() -> None:
    """Initialise the worker pool + COM apartment at app startup.

    Removes the ~5-15 ms thread-spawn cost from the first click. Called
    from ``__main__.py`` after Qt event loop is up. No-op on non-Windows.

    Submits a tiny ``_PrewarmTask`` that just runs ``_ensure_com_apartment``
    on the worker thread. After that returns, the thread is alive,
    STA is initialised, and the first real click pays only its own cost.
    """
    if not _ensure_deps():
        return
    worker = get_worker()
    task = _PrewarmTask()
    worker._counter_lock.acquire()
    try:
        worker._inflight += 1
    finally:
        worker._counter_lock.release()
    task._worker = worker
    worker._pool.start(task)


class _PrewarmTask(QRunnable):
    """Tiny task that warms up the worker thread + COM apartment."""

    def __init__(self) -> None:
        super().__init__()
        self._worker: WtFocusWorker | None = None

    def run(self) -> None:
        try:
            _ensure_com_apartment()
            log.debug("WtFocusWorker prewarm: COM apartment initialised")
        except Exception as e:
            log.warning("Prewarm failed: %s", e)
        finally:
            if self._worker is not None:
                self._worker._on_task_done()


# ─────────────────────────────────────────────────────────────────────
# Testing hooks
# ─────────────────────────────────────────────────────────────────────


def _reset_for_tests() -> None:
    """Drop module singletons. Tests only."""
    global _HAS_DEPS, _worker_singleton, _pythoncom, _win32_console, _wt_uia
    global _COINIT_APARTMENTTHREADED

    if _worker_singleton is not None:
        try:
            _worker_singleton.shutdown(timeout_ms=200)
        except Exception:
            pass

    _HAS_DEPS = None
    _worker_singleton = None
    _pythoncom = None
    _win32_console = None
    _wt_uia = None
    _COINIT_APARTMENTTHREADED = None
    # Reset the thread-local flag too so the test thread can re-init.
    if hasattr(_thread_local, "com_initialized"):
        del _thread_local.com_initialized
