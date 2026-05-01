from __future__ import annotations

import platform
import subprocess
import sys

import psutil

from claude_island.core.models import Session

_MAX_ANCESTOR_DEPTH = 10


class WindowActivator:
    """Brings the terminal window that hosts a Claude Code session to the foreground.

    Windows: EnumWindows → find HWND by PID → SetForegroundWindow.
    macOS:   AppleScript → activate process by unix id (tab-level focus is v2).

    Returns True if activation succeeded, False otherwise.
    """

    def activate(self, session: Session) -> bool:
        os_name = platform.system()
        if os_name == "Windows":
            return self._activate_windows(session.pid)
        if os_name == "Darwin":
            return self._activate_macos(session.pid)
        return False

    # ------------------------------------------------------------------
    # Windows
    # ------------------------------------------------------------------

    def _activate_windows(self, pid: int) -> bool:
        try:
            import win32con
            import win32gui
            import win32process
        except ImportError:
            # pywin32 missing — surface this loudly so the user can fix it
            # rather than silently failing every click.
            print(
                "[claude-island] pywin32 not installed; cannot activate windows. "
                "Run: pip install pywin32",
                file=sys.stderr,
            )
            return False

        # claude.exe is a console child with no HWND. Walk up through the
        # shell (powershell/cmd) to the terminal host (WindowsTerminal.exe,
        # conhost.exe, etc.) and take the first ancestor whose pid owns a
        # visible window.
        candidate_pids = _ancestor_pids(pid)
        if not candidate_pids:
            return False

        hwnd = _find_window_for_pids(candidate_pids, win32gui, win32process)
        if hwnd is None:
            return False

        try:
            win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
            win32gui.SetForegroundWindow(hwnd)
            return True
        except Exception:
            return False

    # ------------------------------------------------------------------
    # macOS
    # ------------------------------------------------------------------

    def _activate_macos(self, pid: int) -> bool:
        # Tab-level focus is not achievable without the terminal app's own
        # AppleScript dictionary; this raises the host app window only (D5).
        script = (
            f'tell application "System Events" to set frontmost of '
            f'(first process whose unix id is {pid}) to true'
        )
        try:
            result = subprocess.run(
                ["osascript", "-e", script],
                capture_output=True,
                timeout=3,
            )
            return result.returncode == 0
        except Exception:
            return False


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------

def _ancestor_pids(pid: int) -> list[int]:
    """Return [pid, parent_pid, grandparent_pid, ...] up to _MAX_ANCESTOR_DEPTH.

    Order is significant: the original pid comes first so callers prefer the
    most specific match when they walk the list looking for a window owner.
    """
    pids: list[int] = []
    try:
        proc: psutil.Process | None = psutil.Process(pid)
        for _ in range(_MAX_ANCESTOR_DEPTH):
            if proc is None:
                break
            pids.append(proc.pid)
            proc = proc.parent()
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        pass
    return pids


def _find_window_for_pids(pids: list[int], win32gui, win32process) -> int | None:
    """Single EnumWindows pass; pick the visible top-level HWND owned by the
    most specific ancestor (the one closest to the original pid).

    Filters out ConPTY pseudo-console windows: powershell.exe / cmd.exe each
    own a hidden console window with WS_VISIBLE set but an empty title that
    SetForegroundWindow cannot surface. Requiring a non-empty title skips
    those and lets us walk up to the real terminal host (WindowsTerminal,
    conhost) which always has the active tab's title.
    """
    priority = {pid: i for i, pid in enumerate(pids)}
    found: list[tuple[int, int]] = []  # (priority, hwnd)

    def _cb(hwnd: int, _: object) -> bool:
        if not win32gui.IsWindowVisible(hwnd):
            return True
        if not win32gui.GetWindowText(hwnd):
            return True
        _, wpid = win32process.GetWindowThreadProcessId(hwnd)
        if wpid in priority:
            found.append((priority[wpid], hwnd))
        return True

    win32gui.EnumWindows(_cb, None)
    if not found:
        return None
    found.sort(key=lambda t: t[0])
    return found[0][1]
