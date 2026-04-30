from __future__ import annotations

import platform
import subprocess

from claude_island.core.models import Session


class WindowActivator:
    """Brings the terminal window that hosts a Claude Code session to the foreground.

    Windows: EnumWindows → find HWND by PID → SetForegroundWindow.
    macOS:   AppleScript → activate process by unix id (tab-level focus is v2).

    Returns True if activation succeeded, False otherwise.
    """

    def activate(self, session: Session) -> bool:
        sys = platform.system()
        if sys == "Windows":
            return self._activate_windows(session.pid)
        if sys == "Darwin":
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

            hwnds: list[int] = []

            def _cb(hwnd: int, _: object) -> bool:
                if win32gui.IsWindowVisible(hwnd):
                    _, wpid = win32process.GetWindowThreadProcessId(hwnd)
                    if wpid == pid:
                        hwnds.append(hwnd)
                return True

            win32gui.EnumWindows(_cb, None)
            if not hwnds:
                return False

            hwnd = hwnds[0]
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
