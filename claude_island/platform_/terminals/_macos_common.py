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

Failure semantics:

When the System Events query fails (permission denied, osascript
timeout, OSError), we deliberately do NOT cache the empty result. A
naive write of ``frozenset()`` into the cache would freeze the
"FOCUS unavailable for everyone" state for the full TTL — a single
osascript hiccup (Spotlight indexing, System Events momentarily busy,
permission toggle race) would cascade into the entire UI showing
ArrowCursor + "unavailable" tooltip for 30 s. Instead we keep the
last-known-good cached value (or empty if we've never succeeded) and
let the next caller retry. Failure also logs a warning with the
osascript stderr so the user can diagnose permission issues from
``claude-island``'s stderr.
"""
from __future__ import annotations

import logging
import subprocess
import threading
import time

import psutil


log = logging.getLogger(__name__)


# Sentinel returned by find_ui_app_ancestor when the target pid no longer
# exists. Callers that want to distinguish "process gone (race)" from "process
# exists but has no UI app ancestor (tmux/screen)" can check against this.
# generic_mac.group() uses it to keep FOCUS on views whose pid raced out —
# the row will disappear on the next ProcessScanner tick anyway, but until
# then the button should remain clickable rather than going dark with no
# feedback.
PROCESS_GONE: object = object()


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
OSASCRIPT_TIMEOUT_S = 3.0

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
# stderr from the most recent failed osascript run, kept once-per-
# distinct-message so persistent permission denials log a single
# warning instead of one per snapshot tick.
_last_logged_stderr: str | None = None


def find_ui_app_ancestor(pid: int, *, max_depth: int = _MAX_DEPTH) -> "int | None | object":
    """Walk the parent chain from ``pid``; return the first pid that
    is a UI application (visible to System Events).

    The starting pid itself is checked first — if it already names a
    UI app, that pid is returned. Otherwise we follow ``parent()`` up
    to ``max_depth`` hops.

    Return values:
      * ``int``  — the UI-app ancestor pid found in the chain.
      * ``None`` — process exists and chain was fully walked, but no
        ancestor is a UI app (tmux/screen daemonization case) OR the
        osascript query failed and there is no cached value.
      * :data:`PROCESS_GONE` — psutil couldn't open ``pid`` because it
        no longer exists. Distinct from ``None`` so callers can treat
        a ProcessScanner race gracefully (keep FOCUS) instead of
        permanently disabling it as they would for tmux/screen.

    The caller treats None as "FOCUS isn't supported for this view".
    PROCESS_GONE is a softer signal: the view is about to disappear
    from the snapshot anyway; don't disable the button prematurely.
    """
    ui_pids = _ui_app_pids()
    if not ui_pids:
        return None
    # Hook-bridge placeholder (pid<=0): no real process to walk. Treat
    # like a vanished pid so callers (focus_host_app) cleanly return
    # False instead of psutil raising ValueError mid-click.
    if pid <= 0:
        return PROCESS_GONE
    try:
        proc = psutil.Process(pid)
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return PROCESS_GONE
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
            timeout=OSASCRIPT_TIMEOUT_S,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        log.warning("frontmost_app(%d): osascript failed: %s", pid, e)
        return False
    if result.returncode != 0:
        log.warning(
            "frontmost_app(%d): osascript exit %d: %s",
            pid, result.returncode,
            result.stderr.decode("utf-8", errors="replace").strip()[:200],
        )
        return False
    return True


def focus_host_app(pid: int) -> bool:
    """Raise the host UI app behind a CLI ``pid`` to the front.

    Convenience wrapper: ``find_ui_app_ancestor`` then ``frontmost_app``.
    Used as the per-adapter fallback when pane-precision focus fails
    (tty mismatch, AppleScript miss, etc.) so a click is never silent.

    Returns False when the chain has no UI ancestor (tmux/screen) or
    when the System Events query failed and there's no cached fallback.
    """
    ui_pid = find_ui_app_ancestor(pid)
    if ui_pid is None:
        return False
    return frontmost_app(ui_pid)


def prewarm_ui_pid_cache() -> None:
    """Populate the UI-pids cache from the worker thread.

    Called by terminal adapters from inside ``group()`` (which runs
    on the snapshotter's worker thread) so the cache is warm by the
    time a UI-thread click triggers ``focus_host_app`` and the
    fallback chain. Without this, an iterm2-only user whose first
    click hits a tty-miss session pays a cold-cache osascript
    (~270 ms) on top of the unavoidable focus AppleScript
    (~290 ms) — back to back on the Qt main thread.

    No-op when the cache is already warm (hits the TTL check). Safe
    to call from any thread; cache writes use ``_cache_lock``."""
    _ui_app_pids()


def _ui_app_pids() -> frozenset[int]:
    """Cached set of UI app pids. See module docstring for cache TTL
    and failure-handling rationale.

    On query failure: keeps the previous cached value (last-known-good)
    rather than overwriting with an empty set. This prevents a single
    osascript hiccup from cascading into a 30 s app-wide degradation.
    """
    global _cached_ui_pids, _cached_at
    now = time.monotonic()
    with _cache_lock:
        if _cached_ui_pids is not None and now - _cached_at < _UI_PIDS_CACHE_TTL_S:
            return _cached_ui_pids
    pids = _query_ui_app_pids()
    if pids is None:
        # Query failed; do NOT poison the cache with an empty result.
        # Return last-known-good (or empty if we never succeeded) and
        # let the next caller retry on its own. The caller treats an
        # empty return the same as a real-but-empty UI process list:
        # find_ui_app_ancestor returns None, which the adapter handles
        # gracefully (FOCUS dropped from caps / fallback returns False).
        with _cache_lock:
            return _cached_ui_pids if _cached_ui_pids is not None else frozenset()
    with _cache_lock:
        _cached_ui_pids = pids
        _cached_at = now
    return pids


def _query_ui_app_pids() -> frozenset[int] | None:
    """Run the System Events osascript that lists UI app pids.

    Returns the parsed pid set on success, ``None`` on any failure —
    timeout, OSError, non-zero exit. ``None`` is the signal callers
    use to skip the cache write so a single failure doesn't freeze
    the cache empty for a full TTL.

    Failures log a warning once per distinct stderr message so a
    persistent permission denial doesn't spam stderr on every tick.
    """
    global _last_logged_stderr
    try:
        result = subprocess.run(
            [
                "/usr/bin/osascript", "-e",
                "tell application \"System Events\" to get unix id of every process",
            ],
            capture_output=True,
            timeout=OSASCRIPT_TIMEOUT_S,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        log.warning("ui_app_pids query failed: %s", e)
        return None
    if result.returncode != 0:
        # System Events returns ``Not authorized to send Apple events
        # (-1743)`` when the user has revoked Automation permission.
        # Surface that exact string so the user can search for it +
        # find Privacy & Security ▶ Automation in the docs.
        stderr_text = result.stderr.decode("utf-8", errors="replace").strip()
        truncated = stderr_text[:200] or "(no stderr)"
        if truncated != _last_logged_stderr:
            log.warning(
                "ui_app_pids query failed (osascript exit %d): %s",
                result.returncode, truncated,
            )
            _last_logged_stderr = truncated
        return None
    # capture_output without text=True returns bytes; decode the same
    # way iterm2._enumerate_panes does so the helper is safe across
    # any locale's stdout encoding.
    text = result.stdout.decode("utf-8", errors="replace")
    out: set[int] = set()
    # osascript returns "123, 456, 789" — comma-separated, sometimes
    # with extra whitespace. Splitting on commas + whitespace handles
    # both shapes; non-numeric tokens (shouldn't happen) are skipped.
    skipped: list[str] = []
    for tok in text.replace(",", " ").split():
        tok = tok.strip()
        if tok.isdigit():
            out.add(int(tok))
        elif tok:
            skipped.append(tok)
    if skipped:
        # Should not happen — flag at debug so operators investigating
        # weird empty / partial results have a breadcrumb.
        log.debug("ui_app_pids: skipped non-numeric tokens: %r", skipped[:5])
    return frozenset(out)


def _reset_cache_for_testing() -> None:
    """Tests call this between runs so a stub UI-pid set doesn't leak
    between cases."""
    global _cached_ui_pids, _cached_at, _last_logged_stderr
    with _cache_lock:
        _cached_ui_pids = None
        _cached_at = 0.0
        _last_logged_stderr = None
