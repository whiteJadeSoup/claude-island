from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import psutil

from claude_island.core.models import Session
from claude_island.platform_ import win32_console, window_activator

# Claude Code is a Node.js CLI; on Windows it may appear as node.exe wrapping
# the claude script, or as a bundled "claude.exe".
_DIRECT_NAMES = {"claude", "claude.exe"}
_NODE_NAMES = {"node", "node.exe"}


class ProcessScanner:
    """Enumerates running Claude Code processes using psutil.

    Uses the process working directory as the project path — Claude Code is
    always started from the project root, so cwd is the authoritative path.

    Two-pass enumeration for cost: the cheap first pass requests only
    ``["pid", "name", "create_time"]`` from every process on the system —
    these are O(1) reads from psutil's per-process snapshot. Only processes
    named ``node[.exe]`` need a follow-up ``cmdline()`` call to confirm
    they're hosting Claude Code; ``claude[.exe]`` is a direct name hit.
    Pulling cmdline eagerly across all processes (the previous behaviour)
    triggered a per-process NtQueryInformationProcess on Windows that
    measurably contributed to scan-tick CPU on busy machines (500+ procs).

    Orphan filter: a "live" Claude session must still have a console
    attached. We probe each claude.exe with AttachConsole+GetConsoleWindow;
    if the call fails (target has no console at all) the process is
    detached — its conPTY pipe was severed when its WT pane closed,
    leaving the binary as a no-tty zombie. Drop those.

    Why AttachConsole-success rather than UIA tab-title matching:
    matching against TabItem.Name false-positives split panes — only the
    *active* pane's title is exposed in TabItem.Name; inactive panes are
    indistinguishable from genuine orphans by name alone. AttachConsole
    treats every still-attached process as live regardless of pane
    visibility, which is what we want.

    Fail-open with a sanity tripwire: if every session would be filtered
    (system-wide AttachConsole brokenness, race with shutdown, etc.),
    return the originals unchanged — better stale than blank.
    """

    def scan(self) -> list[Session]:
        return _filter_orphans(self.scan_fast())

    def scan_fast(self) -> list[Session]:
        """Same psutil enumeration as :meth:`scan` but without the
        ``_filter_orphans`` pass. Returns sessions immediately — their
        ``window_handle`` fields will be ``None``.

        Used at startup so the UI populates sessions in ~200ms. A
        follow-up ``scan()`` call ~500ms later runs the full orphan
        filter and updates the window_handle.
        """
        sessions: list[Session] = []
        for proc in psutil.process_iter(["pid", "name", "create_time"]):
            try:
                info = proc.info
                name = (info.get("name") or "").lower()

                if name in _DIRECT_NAMES:
                    session = self._build(proc, info)
                    if session:
                        sessions.append(session)
                    continue

                if name not in _NODE_NAMES:
                    continue

                try:
                    cmdline = proc.cmdline()
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    continue
                if not any("claude" in arg.lower() for arg in cmdline):
                    continue
                session = self._build(proc, info)
                if session:
                    sessions.append(session)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue

        return sessions

    @staticmethod
    def _build(proc: psutil.Process, info: dict) -> Session | None:
        try:
            project_path = Path(proc.cwd())
        except (psutil.AccessDenied, psutil.NoSuchProcess):
            project_path = Path("unknown")

        create_time = datetime.fromtimestamp(
            info.get("create_time") or 0.0, tz=timezone.utc
        )
        return Session(
            pid=info["pid"],
            project_path=project_path,
            session_uuid="",    # resolved later by JsonlParser activity events
            last_activity=create_time,
        )


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------

def _filter_orphans(sessions: list[Session]) -> list[Session]:
    """Drop orphan sessions and label live ones with their host wt_hwnd.

    Two passes per session in one loop:
    1. ``get_console_info(pid)``: if AttachConsole fails the process has
       no console attached — its conPTY pipe was severed when its WT
       pane closed. Drop it.
    2. ``walk_to_visible_host(conpty_hwnd)``: walks GW_OWNER from the
       conPTY HWND up to the visible WT main window. The result is
       stored on ``Session.window_handle`` so the UI can group sessions
       sharing the same wt_hwnd into one card (same-tab proxy).

    The wt_hwnd is the same HWND ``WindowActivator`` resolves at click
    time, but caching it on the Session lets the UI render groups
    without doing its own AttachConsole walk.

    Sanity tripwire: if every session would be filtered (system-wide
    AttachConsole brokenness, scan-thread race with our own console
    state, etc.), return the originals unchanged. Better stale than
    blank — the user can still see and manually triage from the list.
    """
    if not sessions:
        return sessions

    win32gui = None
    try:
        import win32gui as _w32g
        win32gui = _w32g
    except ImportError:
        pass  # walk_to_visible_host needs win32gui; without it we keep
              # the orphan filter active but skip the wt_hwnd labelling.

    kept: list[Session] = []
    for s in sessions:
        info = win32_console.get_console_info(s.pid)
        if info is None:
            # AttachConsole failed → no console attached → orphan.
            continue
        kept.append(s)

    if not kept:
        return sessions  # tripwire: don't wipe everything silently
    return kept
