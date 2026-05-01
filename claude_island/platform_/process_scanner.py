from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

import psutil

from claude_island.core.models import Session
from claude_island.platform_.window_activator import _ancestor_pids

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
    """

    def scan(self) -> list[Session]:
        # Collect window-owning PIDs once per scan tick (single EnumWindows
        # pass). None means the check is unavailable; orphan filter is skipped
        # to fail-open (show extra sessions rather than hide live ones).
        live_window_pids = _live_window_pids()

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

        if live_window_pids is not None:
            sessions = [s for s in sessions
                        if not _is_orphan(s.pid, live_window_pids)]
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
            window_handle=None, # resolved by WindowActivator on demand
            last_activity=create_time,
        )


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------

def _live_window_pids() -> set[int] | None:
    """Return PIDs of all processes that own a visible top-level window.

    Returns None when the check cannot run (non-Windows, pywin32 absent,
    or EnumWindows failure). scan() treats None as "skip orphan filter"
    so we fail-open: better to show a stale session than to hide a live one.
    """
    if sys.platform != "win32":
        return None
    try:
        import win32gui
        import win32process
    except ImportError:
        return None

    pids: set[int] = set()

    def _cb(hwnd: int, _: object) -> bool:
        if not win32gui.IsWindowVisible(hwnd):
            return True
        if not win32gui.GetWindowText(hwnd):
            return True
        try:
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
            pids.add(pid)
        except Exception:
            pass
        return True

    try:
        win32gui.EnumWindows(_cb, None)
    except Exception:
        return None  # fail-open: caller skips the orphan filter entirely

    return pids


def _is_orphan(pid: int, live_window_pids: set[int]) -> bool:
    """Return True if no ancestor of *pid* owns a visible window.

    Live claude.exe chain:   claude → powershell → WindowsTerminal (visible) → not orphan
    Orphan claude.exe chain: claude → (powershell dead, NoSuchProcess) → orphan
    """
    for ancestor_pid in _ancestor_pids(pid):
        if ancestor_pid in live_window_pids:
            return False
    return True
