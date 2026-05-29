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
_NSWorkspace: Any = None


def _ensure_pyobjc() -> bool:
    """Probe + cache PyObjC symbol availability.

    Returns True iff every symbol the fast-path needs is importable.
    On failure (ImportError or partial install) sets module flag to
    False and logs once — caller falls back to the subprocess path.
    """
    global _HAS_PYOBJC, _NSRunningApplication, _NSApplicationActivateIgnoringOtherApps
    global _NSAppleScript, _NSAppleEventDescriptor, _NSWorkspace

    if _HAS_PYOBJC is not None:
        return _HAS_PYOBJC

    try:
        from AppKit import (  # type: ignore[import-not-found]
            NSRunningApplication,
            NSApplicationActivateIgnoringOtherApps,
            NSWorkspace,
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
    _NSWorkspace = NSWorkspace
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

# ``with timeout of N seconds`` wraps the Apple Event dispatch so iTerm
# can't peg the worker thread indefinitely. Default AppleEvent timeout
# is 60 s — too long for an interactive click. 3 s matches the
# subprocess osascript path's ``timeout=3.0`` so the two paths fail in
# the same envelope; on overrun AppleScript raises errno -1712
# ("AppleEvent timed out") which our error handler catches and the
# AppleScriptCache failure counter treats as a normal failure (3
# strikes invalidates the compiled handler so the next click rebuilds
# fresh state). Without this, a single hung iTerm plugin saturated the
# single-thread worker pool, and after 10 backed-up clicks every
# subsequent pane-select silently dropped until app restart.
_PANE_SELECT_APPLESCRIPT_TIMEOUT_S = 3

_FOCUS_BY_ID_SOURCE = """
on focusByID(sessionID, hostPID)
    with timeout of {timeout} seconds
        tell application "System Events"
            set frontmost of (first process whose unix id is (hostPID as integer)) to true
        end tell
        -- Outer retry-twice + try guards against errAEIllegalIndex
        -- (-1719) when iTerm's window/tab/session collection changes
        -- mid-iteration (a pane closes, a user resizes a split). The
        -- error fires on dereference of a now-stale reference inside
        -- ``repeat with x in collection``. Most races resolve within
        -- microseconds, so one retry catches the typical case. If
        -- both attempts race, we return "miss" — the caller treats
        -- this as a normal not-found (no cache failure increment),
        -- which avoids the spurious invalidate-and-recompile cycle.
        repeat 2 times
            try
                tell application "iTerm"
                    repeat with w in windows
                        repeat with t in tabs of w
                            repeat with s in sessions of t
                                if (id of s as text) is sessionID then
                                    set winName to name of w
                                    -- All three mutators are guarded
                                    -- by ``is not`` checks so they
                                    -- only run when the target state
                                    -- isn't already current. Without
                                    -- these guards, the AppleScript
                                    -- triggered visible iTerm-side
                                    -- side effects (window-shuffle
                                    -- "flash") for the common case
                                    -- where the window was already
                                    -- visible and at index 1 — only
                                    -- the in-tab pane needed to
                                    -- change. select w + select t +
                                    -- select s themselves are still
                                    -- needed; iTerm tracks "last
                                    -- selected" per scope and we want
                                    -- to update all three regardless.
                                    if miniaturized of w is true then
                                        set miniaturized of w to false
                                    end if
                                    -- I-8: broadest-scope first
                                    -- (window → tab → session). iTerm's
                                    -- ``select`` mutates state on each
                                    -- call; doing window last would mean
                                    -- an extra z-order change after we'd
                                    -- already selected the right session
                                    -- and tab. Most-precise selection
                                    -- ends last so it wins regardless of
                                    -- what ``select w`` did to the
                                    -- in-tab selection.
                                    select w
                                    select t
                                    select s
                                    -- I-5: set index orders the window
                                    -- inside iTerm but does NOT switch
                                    -- macOS Spaces (AXRaise below does
                                    -- that). Guard: only set when it
                                    -- would change something -- select w
                                    -- already brings the window to iTerm
                                    -- idx 1 in the common case; running
                                    -- this unconditionally caused a
                                    -- visible reorder side effect.
                                    if index of w is not 1 then
                                        set index of w to 1
                                    end if
                                    -- Cross-Space: see focusByTTY. AXRaise the
                                    -- matched window so a session on another
                                    -- macOS Space is surfaced. try-guarded.
                                    tell application "System Events"
                                        try
                                            tell (first process whose unix id is (hostPID as integer))
                                                perform action "AXRaise" of (first window whose name is winName)
                                            end tell
                                        end try
                                    end tell
                                    return "ok"
                                end if
                            end repeat
                        end repeat
                    end repeat
                    return "miss"
                end tell
            on error errMsg number errNum
                -- transient race; retry once before giving up
            end try
        end repeat
        return "miss"
    end timeout
end focusByID
""".format(timeout=_PANE_SELECT_APPLESCRIPT_TIMEOUT_S)

_FOCUS_BY_TTY_SOURCE = """
on focusByTTY(targetTTY, hostPID)
    with timeout of {timeout} seconds
        tell application "System Events"
            set frontmost of (first process whose unix id is (hostPID as integer)) to true
        end tell
        -- See focusByID for the retry-twice + try rationale: iTerm's
        -- collection can change mid-iteration and raise -1719; retry
        -- catches the typical race, and on persistent failure we
        -- return "miss" so the cache failure counter isn't tripped
        -- spuriously.
        repeat 2 times
            try
                tell application "iTerm"
                    repeat with w in windows
                        repeat with t in tabs of w
                            repeat with s in sessions of t
                                if (tty of s) is targetTTY then
                                    set winName to name of w
                                    -- Guarded mutators (see focusByID)
                                    -- to suppress redundant operations
                                    -- whose visible side effects read
                                    -- as a "flash" when the window
                                    -- was already in the target state.
                                    if miniaturized of w is true then
                                        set miniaturized of w to false
                                    end if
                                    select w
                                    select t
                                    select s
                                    if index of w is not 1 then
                                        set index of w to 1
                                    end if
                                    -- Cross-Space: select w only reorders
                                    -- iTerm's internal window list; AXRaise
                                    -- pulls the target window's macOS Space
                                    -- to the front. try-guarded so a title
                                    -- mismatch / AX error degrades to the
                                    -- prior no-Space-switch behaviour.
                                    tell application "System Events"
                                        try
                                            tell (first process whose unix id is (hostPID as integer))
                                                perform action "AXRaise" of (first window whose name is winName)
                                            end tell
                                        end try
                                    end tell
                                    return "ok"
                                end if
                            end repeat
                        end repeat
                    end repeat
                    return "miss"
                end tell
            on error errMsg number errNum
                -- transient race; retry once before giving up
            end try
        end repeat
        return "miss"
    end timeout
end focusByTTY
""".format(timeout=_PANE_SELECT_APPLESCRIPT_TIMEOUT_S)


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
        host app in front but the pane stays at its previous position.

        Read-decision-increment runs as a single critical section to
        prevent the TOCTOU race where two concurrent submits both
        observe ``backlog < REJECT`` and both increment, exceeding the
        threshold the check was designed to enforce. Today all callers
        are main-thread (Qt event loop serialises them) so the race is
        theoretical — but nothing architectural enforces that, and a
        future caller (e.g. a keyboard-shortcut handler on a worker
        thread) would silently start sneaking past the limit. Cheap
        insurance — one extra lock acquisition per submit (~µs)."""
        task._worker = self
        with self._counter_lock:
            backlog = self._inflight
            if backlog >= self.BACKLOG_REJECT:
                rejected = True
            else:
                rejected = False
                self._inflight += 1
        if rejected:
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
        # If _pool.start raises (e.g. pool already shut down, Qt
        # internal corruption), the increment above would leak forever
        # because _on_task_done never fires for a task that never
        # started. Decrement + re-raise so the counter is conserved.
        # Without this guard, repeated failures drive _inflight up to
        # BACKLOG_REJECT and every subsequent click silently drops
        # pane select — only restart fixes it.
        try:
            self._pool.start(task)
        except Exception:
            with self._counter_lock:
                self._inflight = max(0, self._inflight - 1)
            raise
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
        # I-9: defensive normalisation. ``stringValue()`` may return
        # None if the descriptor isn't text (unexpected from our
        # scripts but possible from a malformed iTerm response or a
        # future iTerm version change), and our scripts return literal
        # "ok"/"miss" strings but the caller compares with strict ``==``
        # — any whitespace ("ok\n") would silently miss. The subprocess
        # osascript path already strips; mirror that here so both paths
        # have identical normalisation contract.
        sv = result.stringValue()
        return (sv or "").strip()


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
# Activation verification — see I-6
# ─────────────────────────────────────────────────────────────────────


# Total wall-clock budget for verify_frontmost. Three polls of 10 ms
# each balances "activate is async on Sonoma+; give it room to complete"
# against "Goal G1 wants main-thread return ~1 ms on success" (the
# success path returns on the first poll, so this only adds latency on
# failure paths that were going to take ~250 ms anyway).
_VERIFY_POLL_INTERVAL_S = 0.01
_VERIFY_POLL_COUNT = 3


def _verify_app_now_frontmost(app: Any, host_pid: int) -> bool:
    """True iff the running app actually became active within the
    poll budget.

    macOS 14+ Sonoma deprecation: ``activateWithOptions_`` is a
    request, not a synchronous transition. The OS may silently refuse
    the request (caller not active, stale activation rights) and still
    return True. The visible foreground app stays the same.

    Use the NSRunningApplication's own ``isActive`` property —
    cheaper than NSWorkspace.frontmostApplication (no workspace
    lookup; just reads the cached property on the wrapper we already
    have). False here is the signal for the caller to fall through to
    the legacy subprocess osascript path, which uses System Events
    ``set frontmost`` — different API, runs with Accessibility
    privilege, not subject to the same demotion rules.

    ``host_pid`` is accepted for diagnostic-logging callers and unused
    in the check itself (the app object IS the host).
    """
    del host_pid  # parameter retained for future-proof logging
    if app is None:
        return False
    for _ in range(_VERIFY_POLL_COUNT):
        try:
            if app.isActive():
                return True
        except Exception:
            # Defensive: if isActive raises (PyObjC bridge oddity),
            # fall through to legacy rather than treat as success.
            return False
        time.sleep(_VERIFY_POLL_INTERVAL_S)
    return False


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

    # I-6: On macOS 14+ Sonoma, ``activateWithOptions_`` is deprecated
    # and can return True without actually activating — when the caller
    # isn't currently the active app (or has stale activation rights),
    # the OS silently demotes the request. Without verification we'd
    # report success and skip the legacy fallback, leaving the user on
    # the previous app.
    #
    # Verify via NSWorkspace.frontmostApplication. Brief poll because
    # activation is asynchronous — Apple's docs describe it as a
    # request, and on a quiet machine it transitions within one
    # event-loop tick. 3 × 10 ms covers the typical transition without
    # blowing past Goal G1 on the success path (most calls return
    # True on the first poll, so cost ≈ one syscall).
    if not _verify_app_now_frontmost(app, host_pid):
        log.info(
            "iterm2 fast-path: activate(host=%d) reported True but "
            "app.isActive remained False; falling back to legacy "
            "subprocess osascript path",
            host_pid,
        )
        return False

    # Host raised — schedule pane select if any identifying signal.
    # The submit() return value carries critical information:
    #   * True  → task queued; pane will be selected asynchronously
    #   * False → worker backlog full (iTerm Apple Event handler is
    #             hung or overwhelmed); the task was silently dropped
    #
    # When False AND we had a pane signal, the user clicked expecting
    # pane precision but only got app-level activation. Return False
    # so ITerm2Adapter.focus falls through to _legacy_focus — the
    # subprocess osascript path is slower (~250 ms) but bounded by a
    # 3s timeout and not blocked by the same worker backlog, so it
    # has an independent shot at landing the pane. Without this, the
    # caller has no idea pane precision was dropped and the user is
    # stuck on the wrong tab until they manually navigate.
    if session_id or tty:
        try:
            task = _PaneSelectTask(
                host_pid=host_pid,
                session_id=session_id,
                tty=tty,
            )
            queued = get_worker().submit(task)
        except Exception as e:
            # Failure to schedule the task doesn't undo the host raise,
            # but we still want the legacy path to take a shot — it
            # uses a fully separate subprocess osascript pipeline.
            log.warning("PaneSelectTask not scheduled: %s", e)
            return False
        if not queued:
            log.info(
                "iterm2 fast-path: pane select rejected (backlog full); "
                "falling back to legacy osascript for pane precision",
            )
            return False

    return True


# ─────────────────────────────────────────────────────────────────────
# Prewarm — sibling of _wt_fast_path.prewarm()
# ─────────────────────────────────────────────────────────────────────


def prewarm() -> None:
    """Pre-import PyObjC, construct the worker pool, and compile the
    cached NSAppleScript handlers at app startup.

    First-click latency was the most user-visible perf surface:
      * ``_ensure_pyobjc`` cold imports AppKit + Foundation (~30 ms)
      * ``get_worker()`` constructs QThreadPool (~5-15 ms)
      * ``AppleScriptCache.get_*_handler`` compiles each handler the
        first time it's invoked (~5-10 ms apiece)

    All ~50-60 ms of that would otherwise land on the user's first
    click — the moment they're judging whether Island works. Running
    it ahead of time at boot moves that latency off the click path.
    No-op on non-macOS (``_ensure_pyobjc`` returns False) and on
    repeated calls (cache singletons are idempotent).

    Safe from any thread; we use the worker pool for the AppleScript
    compile so NSAppleScript stays single-threaded — same contract as
    real pane-select tasks."""
    if not _ensure_pyobjc():
        return
    # Touch the worker singleton so its QThreadPool is constructed
    # ahead of the first real click.
    worker = get_worker()
    # Submit a no-op task that just warms the AppleScript handlers on
    # the worker thread (NSAppleScript is not thread-safe, so compile
    # must happen on the same single thread that later runs execute).
    # Reuse submit() so the lock + leak guard live in one place — the
    # PaneSelectTask type hint is just a docstring; submit() runs any
    # QRunnable with the worker conventions. Submit failure here is
    # already handled by submit's C-2 guard.
    task = _PrewarmTask()
    try:
        worker.submit(task)
    except Exception as e:
        log.warning("iTerm fast-path prewarm: submit failed: %s", e)


class _PrewarmTask(QRunnable):
    """Tiny task that compiles the AppleScript handlers on the worker
    thread so they're ready before the first real click. Sibling of
    ``_wt_fast_path._PrewarmTask``."""

    def __init__(self) -> None:
        super().__init__()
        self._worker: FocusWorker | None = None

    def run(self) -> None:
        try:
            cache = get_cache()
            # Trigger lazy compile on both handlers — each costs ~5-10 ms,
            # both negligible on a warm worker thread.
            cache.get_id_handler()
            cache.get_tty_handler()
            log.debug("iTerm fast-path prewarm: handlers compiled")
        except Exception as e:
            log.warning("iTerm fast-path prewarm failed: %s", e)
        finally:
            if self._worker is not None:
                self._worker._on_task_done()


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
