from __future__ import annotations

import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import psutil

from claude_island.core.models import Session
from claude_island.platform_ import win32_console

# Claude Code is a Node.js CLI; on Windows it may appear as node.exe wrapping
# the claude script, or as a bundled "claude.exe".
_DIRECT_NAMES = {"claude", "claude.exe"}
_NODE_NAMES = {"node", "node.exe"}
# The macOS installer (~/.local/share/claude/versions/<version>) names
# the binary after its version string, so psutil reports
# name="2.1.126" instead of "claude". Recognise version-like names as
# candidates and confirm via cmdline. Pattern is intentionally loose
# (X.Y.Z optionally followed by -beta, .rc1, etc.) to survive future
# version-string changes; the cmdline check rejects false positives.
_VERSION_LIKE = re.compile(r"^\d+\.\d+\.\d+(?:[.-]\S+)?$")


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
        ``_filter_orphans`` pass. Returns sessions immediately.

        Used at startup so the UI populates sessions in ~200ms. A
        follow-up ``scan()`` call ~500ms later runs the full orphan
        filter. Per-session WT window discovery (the wt_hwnd that drives
        same-tab grouping) now happens inside
        ``WindowsTerminalAdapter.group()``, not here — process_scanner
        only enumerates and orphan-filters.
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

                # Confirmation-required path: either a node host that
                # might be running claude, or a versioned binary on
                # macOS. Both need a cmdline() call to distinguish from
                # unrelated processes.
                if name not in _NODE_NAMES and not _VERSION_LIKE.match(name):
                    continue

                try:
                    cmdline = proc.cmdline()
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    continue
                if not cmdline:
                    continue

                # Match if "claude" appears anywhere in cmdline. For
                # node hosts that's the script path; for the macOS
                # versioned binary it's argv[0] (basename "claude").
                argv0_base = os.path.basename(cmdline[0]).lower()
                if argv0_base in _DIRECT_NAMES:
                    pass  # versioned-binary confirmation
                elif name in _NODE_NAMES and any(
                    "claude" in arg.lower() for arg in cmdline
                ):
                    pass  # node-host confirmation
                else:
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
    """Drop orphan sessions whose console pipe was severed.

    A single pass: ``get_console_info(pid)`` — if AttachConsole fails
    the process has no console attached (its conPTY pipe was severed
    when its WT pane closed) and we drop it.

    The wt_hwnd discovery that PR1 used to do here moved to
    ``WindowsTerminalAdapter.group()`` along with the rest of WT
    integration. process_scanner stays pure psutil + AttachConsole.

    Sanity tripwire: if every session would be filtered (system-wide
    AttachConsole brokenness, scan-thread race with our own console
    state, etc.), return the originals unchanged. Better stale than
    blank — the user can still see and manually triage from the list.

    Non-Windows shortcut: AttachConsole is Windows-only and
    ``get_console_info`` returns None for every pid on macOS / Linux.
    Without this guard every session would be dropped, and only the
    "all-filtered" tripwire would save us from an empty list — which is
    fragile (one accidental non-None makes other sessions disappear).
    """
    if sys.platform != "win32":
        return sessions
    if not sessions:
        return sessions

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
