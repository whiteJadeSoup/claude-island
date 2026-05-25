"""Defensive crash logger — captures unhandled exceptions to a file.

Why this module exists: claude-island runs as a long-lived desktop
process across Qt main thread + several worker threads (Snapshotter,
hook listener, terminal focus workers, AppleScript subprocess
launchers).  Stock Python prints uncaught exceptions to stderr — but
when the app is launched from a .app bundle, stderr is silently
swallowed by macOS; even when launched from a terminal, the user may
not be watching the terminal at the moment a crash happens.

This module installs three error sinks that all write to a single
file ``~/.claude-island/crash.log`` (rotated at 1 MB):

  1. ``sys.excepthook`` — main-thread Python exceptions (the default
     stops the interpreter; we record the traceback first).
  2. ``threading.excepthook`` — worker-thread Python exceptions (the
     default in Python 3.8+ prints to stderr but the thread dies
     silently; we record + keep the warning).
  3. ``faulthandler.enable(file=...)`` — hard C-level segfaults
     (cannot be caught by Python; faulthandler dumps the C stack
     on signal and exits).

Logs are append-mode + timestamped; rotated when file size > 1 MB
so the log doesn't grow without bound.  ``install()`` is idempotent.

This is **diagnostic-only** — it does not suppress crashes, it just
makes them visible after the fact.  Production-stable apps in this
threading model (Qt + reactivex + multiple Python threads) need
this; without it, a worker-thread exception simply disappears.
"""
from __future__ import annotations

import faulthandler
import logging
import os
import sys
import threading
import traceback
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger(__name__)


_CRASH_LOG_PATH = Path.home() / ".claude-island" / "crash.log"
_MAX_LOG_SIZE_BYTES = 1024 * 1024     # 1 MB before rotation
_installed = False


def _rotate_if_oversize() -> None:
    """If crash.log is > 1 MB, rename it to crash.log.prev so the
    next write starts fresh.  One generation of history is enough —
    if the user reproduced the crash, they'll capture it again."""
    try:
        if _CRASH_LOG_PATH.exists() and _CRASH_LOG_PATH.stat().st_size > _MAX_LOG_SIZE_BYTES:
            prev = _CRASH_LOG_PATH.with_suffix(".log.prev")
            try:
                if prev.exists():
                    prev.unlink()
            except OSError:
                pass
            try:
                _CRASH_LOG_PATH.rename(prev)
            except OSError:
                pass
    except OSError:
        pass


def _write_crash_entry(header: str, body: str) -> None:
    """Append a timestamped entry to crash.log.  Never raises — if the
    file can't be written, the exception is swallowed (we're already
    in a crash path; secondary failures would just compound noise)."""
    try:
        _CRASH_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        _rotate_if_oversize()
        ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
        entry = (
            f"\n{'=' * 70}\n"
            f"[{ts}] {header}\n"
            f"{'-' * 70}\n"
            f"{body}\n"
        )
        with open(_CRASH_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(entry)
            f.flush()
    except Exception:
        # Crash-time path — never let a secondary failure propagate.
        # We've already lost the main signal; nothing useful to do.
        pass


def _main_thread_hook(exc_type, exc_value, exc_tb) -> None:
    """sys.excepthook callback for main-thread uncaught exceptions.

    Python's default prints to stderr then exits.  We capture the
    traceback first so the user has it on disk before the interpreter
    tears down (and importantly, before macOS swallows the stderr
    output for .app bundle launches).
    """
    tb_text = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
    _write_crash_entry(
        f"UNCAUGHT EXCEPTION on main thread: {exc_type.__name__}: {exc_value}",
        tb_text,
    )
    # Preserve the default behaviour after recording — print to stderr
    # and let the interpreter exit naturally.
    sys.__excepthook__(exc_type, exc_value, exc_tb)


def _worker_thread_hook(args) -> None:
    """threading.excepthook callback for worker-thread uncaught
    exceptions.  Python 3.8+ prints to stderr but the thread dies
    silently — the rest of the app keeps running on the now-broken
    invariant.  We record the traceback so the silent failure leaves
    a paper trail.

    ``args`` is a ``threading.ExceptHookArgs`` namedtuple with
    ``exc_type, exc_value, exc_traceback, thread``.
    """
    exc_type = args.exc_type
    exc_value = args.exc_value
    exc_tb = args.exc_traceback
    thread_name = getattr(args.thread, "name", "?") if args.thread is not None else "?"
    tb_text = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
    _write_crash_entry(
        f"UNCAUGHT EXCEPTION on thread {thread_name!r}: "
        f"{exc_type.__name__}: {exc_value}",
        tb_text,
    )
    # Preserve default stderr printing so anyone watching the terminal
    # still sees it live.
    threading.__excepthook__(args)


def install() -> None:
    """Wire the three error sinks.  Idempotent — calling twice is a
    no-op.  Should be called once at process start from ``__main__``."""
    global _installed
    if _installed:
        return

    # 1. Main-thread Python exceptions.
    sys.excepthook = _main_thread_hook

    # 2. Worker-thread Python exceptions (Python 3.8+).
    threading.excepthook = _worker_thread_hook

    # 3. Hard C-level faults (segfault, SIGABRT, SIGFPE, SIGILL).
    # faulthandler writes the C stack to its file argument and exits.
    # We open the same crash.log in append mode so the user has one
    # file for both Python tracebacks and native crashes.
    try:
        _CRASH_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        _rotate_if_oversize()
        # Keep a handle alive — faulthandler needs the file open for
        # the lifetime of the process (it writes at signal time).
        global _fault_file
        _fault_file = open(_CRASH_LOG_PATH, "a", encoding="utf-8")
        _fault_file.write(
            f"\n{'=' * 70}\n"
            f"[{datetime.now(timezone.utc).isoformat(timespec='seconds')}] "
            f"faulthandler armed (pid={os.getpid()})\n"
        )
        _fault_file.flush()
        faulthandler.enable(file=_fault_file, all_threads=True)
    except Exception:
        log.exception("crash_log: failed to install faulthandler")

    _installed = True


_fault_file = None  # holder for faulthandler's file
