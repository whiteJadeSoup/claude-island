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

        return _force_foreground(hwnd, win32con, win32gui, win32process)

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


def _force_foreground(hwnd: int, win32con, win32gui, win32process) -> bool:
    """Bring the target HWND to the foreground.

    Precondition: the calling process is currently the foreground process
    (Win32 only allows SetForegroundWindow from the foreground process,
    and our expanded panel takes foreground via activateWindow before the
    user can click a row).

    If SetForegroundWindow still fails after that, we fall back to the
    AttachThreadInput dance and finally to SwitchToThisWindow (undocumented
    but used by the Windows task switcher). On total failure we log enough
    state to diagnose: GetLastError, who currently holds the foreground,
    and the calling thread vs target thread.
    """
    import ctypes
    import win32api

    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32

    # Restore if the window is minimised.
    try:
        if win32gui.IsIconic(hwnd):
            win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
    except Exception:
        pass

    # Pass 1: direct SetForegroundWindow (works when we are foreground).
    if _try_set_foreground(hwnd):
        return True

    # Pass 2: AttachThreadInput fallback for cases where the panel briefly
    # lost foreground between show() and the row click.
    our_thread = win32api.GetCurrentThreadId()
    target_thread, _ = win32process.GetWindowThreadProcessId(hwnd)
    fg_hwnd = win32gui.GetForegroundWindow()
    fg_thread = 0
    if fg_hwnd:
        try:
            fg_thread, _ = win32process.GetWindowThreadProcessId(fg_hwnd)
        except Exception:
            pass

    attached: list[int] = []
    try:
        for tid in (fg_thread, target_thread):
            if tid and tid != our_thread and tid not in attached:
                try:
                    win32process.AttachThreadInput(our_thread, tid, True)
                    attached.append(tid)
                except Exception:
                    pass
        if _try_set_foreground(hwnd):
            return True
    finally:
        for tid in attached:
            try:
                win32process.AttachThreadInput(our_thread, tid, False)
            except Exception:
                pass

    # Pass 3: SwitchToThisWindow (undocumented, used by Alt+Tab).
    try:
        user32.SwitchToThisWindow(hwnd, True)
        if user32.GetForegroundWindow() == hwnd:
            return True
    except Exception:
        pass

    # All passes failed — surface enough state to debug.
    last_err = kernel32.GetLastError()
    fg_owner = ""
    if fg_hwnd:
        try:
            title = win32gui.GetWindowText(fg_hwnd) or "<no title>"
            fg_owner = f"{title!r}(hwnd={fg_hwnd})"
        except Exception:
            fg_owner = f"hwnd={fg_hwnd}"
    print(
        f"[claude-island] could not surface HWND {hwnd}: "
        f"GetLastError={last_err}, foreground={fg_owner}, "
        f"our_thread={our_thread}, target_thread={target_thread}, "
        f"fg_thread={fg_thread}",
        file=sys.stderr,
    )
    return False


def _try_set_foreground(hwnd: int) -> bool:
    import ctypes
    user32 = ctypes.windll.user32
    try:
        user32.BringWindowToTop(hwnd)
    except Exception:
        pass
    try:
        return bool(user32.SetForegroundWindow(hwnd))
    except Exception:
        return False


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
