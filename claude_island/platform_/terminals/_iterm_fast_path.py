"""iTerm2 focus fast-path — main-thread NSRunningApplication.activate + worker NSAppleScript.

See ``design/2026-05-iterm-focus-performance-detail/`` for the full design.

Two collaborating pieces:

* ``try_fast_path()`` — runs on the Qt main thread, calls
  ``NSRunningApplication.activate`` (~0.3 ms warm) to raise the iTerm
  host app, then schedules a ``_PaneSelectTask`` onto the single-thread
  ``FocusWorker`` pool. Returns True iff the host raise succeeded; pane
  select is fire-and-forget.

* The worker pool runs ``_PaneSelectTask.run()`` on a background thread.
  Each task asks ``AppleScriptCache`` for a compiled ``NSAppleScript``
  handler (id-match first, tty-match fallback) and invokes it via
  ``executeAppleEvent_error_`` with subroutine semantics so the script
  body compiles once and arguments come in as AppleEvent descriptors.

The split lets the main thread return within ~1 ms (Goal G1) while the
pane-precision step runs without blocking the panel-hide animation
(Goal G2). PyObjC unavailability or any failure inside this module
falls back to the existing subprocess osascript path in the caller.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from typing import Any

from PySide6.QtCore import QRunnable, QThreadPool

log = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────
# PyObjC lazy loader
# ─────────────────────────────────────────────────────────────────────
#
# Module-level cache so the import cost (~30 ms) is paid at most once
# per process, on the FIRST fast-path attempt. After that, every call
# is a single bool check. ``None`` means "not yet probed".

_HAS_PYOBJC: bool | None = None
_NSRunningApplication: Any = None
_NSApplicationActivateIgnoringOtherApps: int | None = None
_NSAppleScript: Any = None
_NSAppleEventDescriptor: Any = None


def _ensure_pyobjc() -> bool:
    """Probe + cache PyObjC symbol availability.

    Returns True iff every symbol the fast-path needs is importable.
    On failure (ImportError or partial install) sets module flag to
    False and logs once — caller falls back to the subprocess path.
    """
    global _HAS_PYOBJC, _NSRunningApplication, _NSApplicationActivateIgnoringOtherApps
    global _NSAppleScript, _NSAppleEventDescriptor

    if _HAS_PYOBJC is not None:
        return _HAS_PYOBJC

    try:
        from AppKit import (  # type: ignore[import-not-found]
            NSRunningApplication,
            NSApplicationActivateIgnoringOtherApps,
        )
        from Foundation import (  # type: ignore[import-not-found]
            NSAppleEventDescriptor,
            NSAppleScript,
        )
    except ImportError as e:
        log.info("iterm2 fast-path disabled: PyObjC unavailable (%s)", e)
        _HAS_PYOBJC = False
        return False

    _NSRunningApplication = NSRunningApplication
    _NSApplicationActivateIgnoringOtherApps = NSApplicationActivateIgnoringOtherApps
    _NSAppleScript = NSAppleScript
    _NSAppleEventDescriptor = NSAppleEventDescriptor
    _HAS_PYOBJC = True
    return True


# ─────────────────────────────────────────────────────────────────────
# AppleEvent four-char codes
# ─────────────────────────────────────────────────────────────────────
#
# These are the canonical Apple-Events constants used to build an
# "execute subroutine" event (``aevt/psbr``) against a compiled script.
# Packing them at module load avoids re-computing on every dispatch.

def _fcc(s: str) -> int:
    """Pack a 4-character code into a signed-32-bit OSType int."""
    if len(s) != 4:
        raise ValueError(f"FourCharCode must be 4 bytes, got {s!r}")
    return (ord(s[0]) << 24) | (ord(s[1]) << 16) | (ord(s[2]) << 8) | ord(s[3])


_kASAppleScriptSuite = _fcc("ascr")
_kASSubroutineEvent = _fcc("psbr")
_keyASSubroutineName = _fcc("snam")
_keyDirectObject = _fcc("----")
_kAutoGenerateReturnID = -1
_kAnyTransactionID = 0


# ─────────────────────────────────────────────────────────────────────
# AppleScript subroutine sources
# ─────────────────────────────────────────────────────────────────────
#
# Two compiled-once scripts. Arguments are passed via AppleEvent
# descriptors at runtime, so the source strings never get interpolated
# — no escaping needed, and compile happens at most once per handler
# per process.

_FOCUS_BY_ID_SOURCE = """
on focusByID(sessionID, hostPID)
    tell application "System Events"
        set frontmost of (first process whose unix id is (hostPID as integer)) to true
    end tell
    tell application "iTerm"
        repeat with w in windows
            repeat with t in tabs of w
                repeat with s in sessions of t
                    if (id of s as text) is sessionID then
                        select s
                        select t
                        select w
                        return "ok"
                    end if
                end repeat
            end repeat
        end repeat
        return "miss"
    end tell
end focusByID
"""

_FOCUS_BY_TTY_SOURCE = """
on focusByTTY(targetTTY, hostPID)
    tell application "System Events"
        set frontmost of (first process whose unix id is (hostPID as integer)) to true
    end tell
    tell application "iTerm"
        repeat with w in windows
            repeat with t in tabs of w
                repeat with s in sessions of t
                    if (tty of s) is targetTTY then
                        select s
                        select t
                        select w
                        return "ok"
                    end if
                end repeat
            end repeat
        end repeat
        return "miss"
    end tell
end focusByTTY
"""


# ─────────────────────────────────────────────────────────────────────
# AppleScriptCache — compiles + retains NSAppleScript instances
# ─────────────────────────────────────────────────────────────────────

# Threshold of consecutive failures before a handler is invalidated and
# recompiled. 3 balances "ignore transient iTerm hiccups" (a single fail
# shouldn't bin the cache) against "recover when the script object is
# genuinely broken" (after 3 strikes the cost of recompile is worth it).
# Per-handler counters reset on any "ok" — see note_success.
_CACHE_FAILURE_THRESHOLD = 3


class AppleScriptCache:
    """Lazy-compiled, single-threaded ``NSAppleScript`` holder.

    Two handlers (id-match, tty-match) live as separate ``NSAppleScript``
    instances so a failure in one doesn't taint the other.

    Thread model
    ------------
    Apple's docs say ``NSAppleScript`` "is not thread-safe". The
    contract we satisfy: every execute call goes through the
    ``FocusWorker`` (maxThreadCount=1), serialising all access to the
    same script object. ``_lock`` here protects only the lazy-compile
    + invalidate transitions; the hot path (already-compiled execute)
    runs lock-free.
    """

    def __init__(self) -> None:
        self._id_script: Any = None
        self._tty_script: Any = None
        self._lock = threading.Lock()
        self._id_failures = 0
        self._tty_failures = 0
        # Terminal state: compilation itself failed. Don't retry — that
        # means the source string is wrong (a coding bug, not a runtime
        # issue). Worker falls back to the legacy subprocess path.
        self._id_compile_failed = False
        self._tty_compile_failed = False

    # ── public API ──────────────────────────────────────────────────

    def get_id_handler(self) -> Any:
        return self._get(
            attr="_id_script",
            fail_attr="_id_compile_failed",
            source=_FOCUS_BY_ID_SOURCE,
            label="id_handler",
        )

    def get_tty_handler(self) -> Any:
        return self._get(
            attr="_tty_script",
            fail_attr="_tty_compile_failed",
            source=_FOCUS_BY_TTY_SOURCE,
            label="tty_handler",
        )

    def note_failure(self, handler: str) -> bool:
        """Increment per-handler failure counter; invalidate at threshold.

        Returns True iff the handler was just invalidated (so the caller
        knows to log it once at WARNING level)."""
        if handler == "id":
            self._id_failures += 1
            if self._id_failures >= _CACHE_FAILURE_THRESHOLD:
                with self._lock:
                    self._id_script = None
                    self._id_failures = 0
                log.warning(
                    "AppleScriptCache invalidated id_handler after %d consecutive failures",
                    _CACHE_FAILURE_THRESHOLD,
                )
                return True
        elif handler == "tty":
            self._tty_failures += 1
            if self._tty_failures >= _CACHE_FAILURE_THRESHOLD:
                with self._lock:
                    self._tty_script = None
                    self._tty_failures = 0
                log.warning(
                    "AppleScriptCache invalidated tty_handler after %d consecutive failures",
                    _CACHE_FAILURE_THRESHOLD,
                )
                return True
        return False

    def note_success(self, handler: str) -> None:
        """An "ok" return resets the counter for this handler."""
        if handler == "id":
            self._id_failures = 0
        elif handler == "tty":
            self._tty_failures = 0

    def invalidate(self) -> None:
        """Force-drop both compiled handlers (e.g. for tests)."""
        with self._lock:
            self._id_script = None
            self._tty_script = None
            self._id_failures = 0
            self._tty_failures = 0
            self._id_compile_failed = False
            self._tty_compile_failed = False

    # ── internals ───────────────────────────────────────────────────

    def _get(self, *, attr: str, fail_attr: str, source: str, label: str) -> Any:
        with self._lock:
            if getattr(self, fail_attr):
                return None
            cached = getattr(self, attr)
            if cached is not None:
                return cached
            if _NSAppleScript is None:
                # Should not happen — _ensure_pyobjc gates the worker
                # path. Defensive: refuse to compile rather than crash.
                setattr(self, fail_attr, True)
                return None
            script = _NSAppleScript.alloc().initWithSource_(source)
            ok, err = script.compileAndReturnError_(None)
            if not ok:
                setattr(self, fail_attr, True)
                msg = (
                    err.get("NSAppleScriptErrorMessage")
                    if err is not None
                    else "unknown"
                )
                log.error("AppleScript compile failed (%s): %s", label, msg)
                return None
            setattr(self, attr, script)
            return script


# Module singleton. Lazily created so import order is cheap.
_cache_singleton: AppleScriptCache | None = None


def get_cache() -> AppleScriptCache:
    global _cache_singleton
    if _cache_singleton is None:
        _cache_singleton = AppleScriptCache()
    return _cache_singleton


# ─────────────────────────────────────────────────────────────────────
# FocusWorker — single-thread QThreadPool wrapper
# ─────────────────────────────────────────────────────────────────────

# Backlog thresholds. Steady-state queue length under normal click
# cadence is ~0 (worker finishes a task in ~100 ms; humans click at
# 3-300 Hz at most). Warn at 4 (something's wrong), reject at 10
# (iTerm engine likely hung — protect the worker from runaway queue).
_BACKLOG_WARN = 4
_BACKLOG_REJECT = 10
# How long to wait at process shutdown for in-flight tasks to drain.
# Half a second is generous; missing the deadline drops the pending
# pane select but doesn't block process exit.
_SHUTDOWN_TIMEOUT_MS = 500


class FocusWorker:
    """``QThreadPool`` of size 1, FIFO. The single-worker constraint is
    a hard requirement for ``NSAppleScript`` thread-safety.

    Backlog tracking
    ----------------
    PySide6's ``QThreadPool`` doesn't expose a queued-count API in a
    stable cross-version way, so we keep our own ``_inflight`` counter
    (submitted minus completed). Submit increments at start; each task
    decrements in its ``finally`` block via ``_on_task_done``.
    """

    BACKLOG_WARN = _BACKLOG_WARN
    BACKLOG_REJECT = _BACKLOG_REJECT

    def __init__(self) -> None:
        self._pool = QThreadPool()
        self._pool.setMaxThreadCount(1)
        self._counter_lock = threading.Lock()
        self._inflight = 0
        # Suppress duplicate "rejected" ERROR logs within a window.
        self._last_reject_log_at: float = 0.0

    def submit(self, task: _PaneSelectTask) -> bool:
        """Enqueue a task. Returns True if accepted, False if rejected
        due to backlog. On reject, caller has already done the
        main-thread NSRunningApplication.activate; the user sees the
        host app in front but the pane stays at its previous position."""
        with self._counter_lock:
            backlog = self._inflight

        if backlog >= self.BACKLOG_REJECT:
            now = time.monotonic()
            if now - self._last_reject_log_at > 60.0:
                log.error(
                    "FocusWorker rejected pane select; backlog=%d; iTerm likely hung",
                    backlog,
                )
                self._last_reject_log_at = now
            return False
        if backlog >= self.BACKLOG_WARN:
            log.warning("FocusWorker backlog=%d", backlog)

        task._worker = self
        with self._counter_lock:
            self._inflight += 1
        self._pool.start(task)
        return True

    def _on_task_done(self) -> None:
        """Called from each task's ``finally`` block on the worker thread."""
        with self._counter_lock:
            self._inflight = max(0, self._inflight - 1)

    def backlog(self) -> int:
        with self._counter_lock:
            return self._inflight

    def shutdown(self, timeout_ms: int = _SHUTDOWN_TIMEOUT_MS) -> None:
        self._pool.waitForDone(timeout_ms)


_worker_singleton: FocusWorker | None = None


def get_worker() -> FocusWorker:
    global _worker_singleton
    if _worker_singleton is None:
        _worker_singleton = FocusWorker()
    return _worker_singleton


# ─────────────────────────────────────────────────────────────────────
# _PaneSelectTask — runs on the worker thread
# ─────────────────────────────────────────────────────────────────────


class _PaneSelectTask(QRunnable):
    """One pane-select attempt. Frozen-by-convention (don't mutate after submit).

    Inherits QRunnable so it can be started by QThreadPool. Fields are
    captured at construction time so the worker thread doesn't read
    them from a potentially-stale SessionView.
    """

    def __init__(
        self,
        *,
        host_pid: int,
        session_id: str | None,
        tty: str | None,
    ) -> None:
        super().__init__()
        if host_pid <= 0:
            raise ValueError(f"host_pid must be positive, got {host_pid}")
        if not session_id and not tty:
            raise ValueError("session_id or tty must be non-empty")
        self.host_pid = int(host_pid)
        self.session_id = session_id or None
        self.tty = tty or None
        self.created_at = time.monotonic()
        self._worker: FocusWorker | None = None

    def run(self) -> None:
        """QThreadPool entry point. Always runs on the worker thread."""
        try:
            self._run_impl()
        except Exception as e:
            # Last-resort catch — the worker thread MUST NOT propagate
            # exceptions back into Qt's pool internals.
            log.warning("_PaneSelectTask raised: %s", e)
        finally:
            if self._worker is not None:
                self._worker._on_task_done()

    def _run_impl(self) -> None:
        cache = get_cache()

        # 1) Try id-match handler if we have a session_id.
        if self.session_id:
            ret = self._try_handler(
                cache=cache,
                handler=cache.get_id_handler(),
                handler_label="id",
                subroutine="focusByID",
                arg=self.session_id,
            )
            if ret == "ok":
                cache.note_success("id")
                log.debug("iterm2 fast-path: id match host=%d", self.host_pid)
                return
            # "miss" or None (exception/error) → fall through to tty.

        # 2) Try tty-match handler if we have a tty.
        if self.tty:
            ret = self._try_handler(
                cache=cache,
                handler=cache.get_tty_handler(),
                handler_label="tty",
                subroutine="focusByTTY",
                arg=self.tty,
            )
            if ret == "ok":
                cache.note_success("tty")
                log.debug("iterm2 fast-path: tty match host=%d", self.host_pid)
                return

        # Both lines (or only available one) missed. Main-thread already
        # raised the host app, so user still sees iTerm in front — the
        # specific pane just stays where iTerm had it last.
        log.info(
            "iterm2 pane select miss (id=%r, tty=%r, host=%d)",
            self.session_id, self.tty, self.host_pid,
        )

    def _try_handler(
        self,
        *,
        cache: AppleScriptCache,
        handler: Any,
        handler_label: str,
        subroutine: str,
        arg: str,
    ) -> str | None:
        """Invoke one handler. Returns "ok" / "miss" / None (error)."""
        if handler is None:
            # Compile failed — cache already logged at error level.
            return None
        try:
            event = _build_subroutine_event(subroutine, arg, self.host_pid)
            result, err = handler.executeAppleEvent_error_(event, None)
        except Exception as e:
            log.warning(
                "NSAppleScript execute raised (%s): %s", handler_label, e,
            )
            cache.note_failure(handler_label)
            return None
        if err is not None:
            errno = err.get("NSAppleScriptErrorNumber", 0) if err else 0
            msg = err.get("NSAppleScriptErrorMessage", "") if err else ""
            if errno == -1743:
                # Automation permission revoked — terminal state for
                # this run; tell the user how to recover.
                log.error(
                    "System Events Automation permission revoked; "
                    "restore via System Settings ▶ Privacy & Security ▶ Automation",
                )
            else:
                log.warning(
                    "NSAppleScript error (%s) errno=%d msg=%s",
                    handler_label, errno, msg,
                )
            cache.note_failure(handler_label)
            return None
        if result is None:
            cache.note_failure(handler_label)
            return None
        return result.stringValue()


def _build_subroutine_event(handler_name: str, arg: str, host_pid: int) -> Any:
    """Build an ``aevt/psbr`` AppleEvent that invokes a subroutine.

    The event targets the current process (we send it to our own
    NSAppleScript instance). Arguments are packed as a positional list:
    [arg (string), host_pid (int32)].
    """
    target = _NSAppleEventDescriptor.descriptorWithProcessIdentifier_(os.getpid())
    event = _NSAppleEventDescriptor.appleEventWithEventClass_eventID_targetDescriptor_returnID_transactionID_(
        _kASAppleScriptSuite,
        _kASSubroutineEvent,
        target,
        _kAutoGenerateReturnID,
        _kAnyTransactionID,
    )
    name_desc = _NSAppleEventDescriptor.descriptorWithString_(handler_name)
    event.setParamDescriptor_forKeyword_(name_desc, _keyASSubroutineName)

    arg_list = _NSAppleEventDescriptor.listDescriptor()
    # AppleEvent list descriptors use 1-based indexing.
    arg_list.insertDescriptor_atIndex_(
        _NSAppleEventDescriptor.descriptorWithString_(arg), 1,
    )
    arg_list.insertDescriptor_atIndex_(
        _NSAppleEventDescriptor.descriptorWithInt32_(int(host_pid)), 2,
    )
    event.setParamDescriptor_forKeyword_(arg_list, _keyDirectObject)
    return event


# ─────────────────────────────────────────────────────────────────────
# Main entry point
# ─────────────────────────────────────────────────────────────────────


def try_fast_path(
    *,
    host_pid: int,
    session_id: str | None,
    tty: str | None,
) -> bool:
    """Main-thread fast-path.

    All inputs already resolved by the caller — this function does no
    psutil walks and no jump_target lookups. The caller (the iTerm2
    adapter's ``focus``) is responsible for resolving:
      - ``host_pid``: pid of the target iTerm2 process (jump_target.terminal_pid
        when fresh, ``_iterm_host_pid()`` fallback otherwise).
      - ``session_id``: iTerm session id from hook capture, or None.
      - ``tty``: ``psutil.Process(pid).terminal()`` result, or None.

    Returns True iff:
      1. PyObjC is available,
      2. ``NSRunningApplication.activate`` reported success.

    On True, a ``_PaneSelectTask`` has been queued onto the worker
    pool (fire-and-forget). On False, caller falls back to the
    subprocess osascript chain.

    Must be called from the Qt main thread — ``NSRunningApplication``
    is in-process AppKit and inherits the caller's thread affinity.
    """
    if not _ensure_pyobjc():
        return False
    if host_pid <= 0:
        return False

    app = _NSRunningApplication.runningApplicationWithProcessIdentifier_(host_pid)
    if app is None:
        return False
    try:
        ok = app.activateWithOptions_(_NSApplicationActivateIgnoringOtherApps)
    except Exception as e:
        log.warning("NSRunningApplication.activate raised: %s", e)
        return False
    if not ok:
        return False

    # Host raised — schedule pane select if any identifying signal.
    if session_id or tty:
        try:
            task = _PaneSelectTask(
                host_pid=host_pid,
                session_id=session_id,
                tty=tty,
            )
            get_worker().submit(task)
        except Exception as e:
            # Failure to schedule the task doesn't undo the host raise.
            # User still sees the right app in front; pane stays put.
            log.warning("PaneSelectTask not scheduled: %s", e)

    return True


# ─────────────────────────────────────────────────────────────────────
# Testing hooks
# ─────────────────────────────────────────────────────────────────────


def _reset_for_tests() -> None:
    """Drop module singletons + PyObjC cache. Tests only."""
    global _cache_singleton, _worker_singleton, _HAS_PYOBJC
    global _NSRunningApplication, _NSApplicationActivateIgnoringOtherApps
    global _NSAppleScript, _NSAppleEventDescriptor

    if _worker_singleton is not None:
        try:
            _worker_singleton.shutdown(timeout_ms=200)
        except Exception:
            pass

    _cache_singleton = None
    _worker_singleton = None
    _HAS_PYOBJC = None
    _NSRunningApplication = None
    _NSApplicationActivateIgnoringOtherApps = None
    _NSAppleScript = None
    _NSAppleEventDescriptor = None
