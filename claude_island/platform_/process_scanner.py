from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import psutil

from claude_island.core.models import Session
from claude_island.platform_ import win32_console

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

                # Node process — only now pay the cmdline cost.
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

        return _filter_orphans(sessions)

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
            window_handle=None, # resolved by WindowActivator on demand
            last_activity=create_time,
        )


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------

def _filter_orphans(sessions: list[Session]) -> list[Session]:
    """Drop sessions whose process has no console attached.

    A claude.exe whose hosting WT pane was closed becomes a no-tty
    zombie: its conPTY pipe is broken, AttachConsole(pid) refuses to
    attach (the kernel reports "process has no console"). That's a
    genuine orphan — drop it.

    Sanity tripwire: if every session would be filtered (system-wide
    AttachConsole brokenness, scan-thread race with our own console
    state, etc.), return the originals unchanged. Better stale than
    blank — the user can still see and manually triage from the list.
    """
    if not sessions:
        return sessions

    kept: list[Session] = []
    for s in sessions:
        if win32_console.get_console_info(s.pid) is not None:
            kept.append(s)
        # else: AttachConsole failed → no console attached → orphan.

    if not kept:
        return sessions  # tripwire: don't wipe everything silently
    return kept
