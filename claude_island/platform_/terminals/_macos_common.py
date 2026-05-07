"""Shared macOS helpers for terminal adapters.

Locates the host UI app behind a CLI session pid. Both iterm2 and
generic_mac need this: System Events' ``process whose unix id is X``
only enumerates UI applications, so handing it a CLI pid (claude is
always a CLI) returns error -1719 ``Invalid index`` and ``set frontmost``
silently fails. Walking the parent chain until we hit an ancestor that
System Events does enumerate gives us a pid we can actually frontmost.

Cases this fixes:

* macOS Terminal.app, Warp, Ghostty, VS Code integrated terminal,
  Kitty, Alacritty — anything that doesn't have a dedicated adapter
  (i.e. lands on generic_mac). claude → host shell → host app; we walk
  up to the host app.
* iTerm2 sessions whose tty match misses (tmux pty inside iTerm2,
  pane closed between scan and click, AppleScript permission revoked
  for iTerm but not for System Events). iTerm2 adapter falls back here
  so at least the app gets raised even when pane-precision focus fails.

Cases this does NOT fix:

* tmux/screen running anywhere: the daemonized server reparents to
  launchd, so ``psutil.Process.parent()`` walking from the in-tmux
  claude never reaches the iTerm/Terminal that hosts the tmux client.
  ``find_ui_app_ancestor`` returns None; callers treat this as "FOCUS
  isn't supported for this view" and stamp accordingly.
"""
from __future__ import annotations

import subprocess
import threading
import time

import psutil


# How far we'll walk the process tree looking for a UI ancestor.
# Real-world chains top out at ~5 hops (claude → zsh → login →
# ShellLauncher → iTermServer → iTerm2). 12 leaves slack for
# wrapped-shell setups (tmux outside the broken case, helper
# launchers, etc.) without risking a runaway walk on a pathological
# self-cycling parent.
_MAX_DEPTH = 12

# osascript timeout. Generous because System Events is talking to the
# Apple-Events daemon — first call after a long idle can be slow —
# but short enough that a hung osascript doesn't freeze the snapshot
# pipeline. 3 s mirrors the iterm2 enum timeout.
_OSASCRIPT_TIMEOUT_S = 3.0

# UI-app-pids cache TTL. The pid set changes only when the user opens
# or quits a UI application — typically several seconds to many
# minutes between events. 30 s means a focus click usually hits the
# cache (no osascript), and we re-enumerate at most twice a minute.
# Short enough that a freshly-launched terminal becomes a focusable
# target within half a minute even if the user never triggers a wake.
_UI_PIDS_CACHE_TTL_S = 30.0


_cache_lock = threading.Lock()
_cached_ui_pids: frozenset[int] | None = None
_cached_at: float = 0.0


def find_ui_app_ancestor(pid: int, *, max_depth: int = _MAX_DEPTH) -> int | None:
    """Walk the parent chain from ``pid``; return the first pid that
    is a UI application (visible to System Events).

    The starting pid itself is checked first — if it already names a
    UI app, that pid is returned. Otherwise we follow ``parent()`` up
    to ``max_depth`` hops.

    Returns ``None`` when:
      * The osascript that lists UI pids failed (permission denied,
        timeout, System Events not running).
      * psutil can't open ``pid`` (process gone or access denied).
      * No ancestor within ``max_depth`` hops is a UI app — most
        commonly the tmux/screen daemonization case where the server
        was reparented to launchd, severing the link to the host
        terminal application.

    The caller treats None as "FOCUS isn't supported for this view".
    """
    ui_pids = _ui_app_pids()
    if not ui_pids:
        return None
    try:
        proc = psutil.Process(pid)
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return None
    for _ in range(max_depth):
        if proc.pid in ui_pids:
            return proc.pid
        try:
            parent = proc.parent()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            return None
        if parent is None:
            return None
        proc = parent
    return None


def frontmost_app(pid: int) -> bool:
    """Raise the UI application at ``pid`` to the front via System
    Events. Returns True iff osascript exited 0.

    Caller is expected to have resolved ``pid`` to a UI app first
    (typically via :func:`find_ui_app_ancestor`); passing a CLI pid
    here would error out at the AppleScript level (``-1719`` invalid
    index) and we'd dutifully return False.
    """
    try:
        result = subprocess.run(
            [
                "/usr/bin/osascript", "-e",
                "tell application \"System Events\" to set frontmost of "
                f"(first process whose unix id is {int(pid)}) to true",
            ],
            capture_output=True,
            timeout=_OSASCRIPT_TIMEOUT_S,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def _ui_app_pids() -> frozenset[int]:
    """Cached list of UI app pids. See module docstring for cache TTL
    rationale."""
    global _cached_ui_pids, _cached_at
    now = time.monotonic()
    with _cache_lock:
        if _cached_ui_pids is not None and now - _cached_at < _UI_PIDS_CACHE_TTL_S:
            return _cached_ui_pids
    pids = _query_ui_app_pids()
    with _cache_lock:
        _cached_ui_pids = pids
        _cached_at = now
    return pids


def _query_ui_app_pids() -> frozenset[int]:
    try:
        result = subprocess.run(
            [
                "/usr/bin/osascript", "-e",
                "tell application \"System Events\" to get unix id of every process",
            ],
            capture_output=True,
            timeout=_OSASCRIPT_TIMEOUT_S,
        )
    except (OSError, subprocess.TimeoutExpired):
        return frozenset()
    if result.returncode != 0:
        return frozenset()
    # capture_output without text=True returns bytes; decode the same
    # way iterm2._enumerate_panes does so the helper is safe across
    # any locale's stdout encoding.
    text = result.stdout.decode("utf-8", errors="replace")
    out: set[int] = set()
    # osascript returns "123, 456, 789" — comma-separated, sometimes
    # with extra whitespace. Splitting on commas + whitespace handles
    # both shapes; non-numeric tokens (shouldn't happen) are skipped.
    for tok in text.replace(",", " ").split():
        tok = tok.strip()
        if tok.isdigit():
            out.add(int(tok))
    return frozenset(out)


def _reset_cache_for_testing() -> None:
    """Tests call this between runs so a stub UI-pid set doesn't leak
    between cases."""
    global _cached_ui_pids, _cached_at
    with _cache_lock:
        _cached_ui_pids = None
        _cached_at = 0.0
