"""Win32 window-foreground helpers shared by the WT adapter.

The legacy ``WindowActivator`` class that used to live here was the
v1 entry point for click → foreground; it has been superseded by
``WindowsTerminalAdapter._activate_windows`` (in
``terminals/windows_terminal.py``), which does the same console
resolve + tab select but uses sibling sentinels instead of console
titles for the inactive-pane fallback.

Only the module-level helpers remain — they are still imported by
``windows_terminal._activate_windows`` and ``generic_windows`` for
the foreground push.
"""
from __future__ import annotations

import sys

import psutil

from claude_island.platform_ import win32_console

_MAX_ANCESTOR_DEPTH = 10


def walk_to_visible_host(conpty_hwnd: int, win32gui) -> int | None:
    """Walk the GW_OWNER chain from a (typically hidden) conPTY HWND up to
    the first visible top-level window with a non-empty title.

    Used in two places:
    - At click time: ``_resolve_console_window`` pairs this with the
      console title to drive tab selection + foreground.
    - At scan time: ``WindowsTerminalAdapter.group()`` calls this to
      label each session with its hosting wt_hwnd, so the UI can group
      same-tab sessions visually.

    Returns ``None`` when the chain breaks before reaching a visible
    host within bounded depth (``_MAX_ANCESTOR_DEPTH`` = 10).
    """
    GW_OWNER = 4
    h = conpty_hwnd
    for _ in range(_MAX_ANCESTOR_DEPTH):
        if not h:
            return None
        try:
            if win32gui.IsWindowVisible(h) and win32gui.GetWindowText(h):
                return h
            nxt = win32gui.GetWindow(h, GW_OWNER) or win32gui.GetParent(h)
        except Exception:
            # Invalid / destroyed HWND mid-walk → treat as no host.
            return None
        if not nxt or nxt == h:
            return None
        h = nxt
    return None


def _resolve_console_window(pid: int, win32gui) -> tuple[int, str] | None:
    """Find the visible host window that owns the given pid's console, and
    capture the console title for use in tab selection.

    Delegates the AttachConsole / GetConsoleTitleW dance to the shared
    ``win32_console`` helper (lock-protected so the scanner thread and
    the activator thread don't race on the global console state). Once
    we have the conPTY pseudo-console HWND, we walk GW_OWNER up to the
    visible host (WindowsTerminal.exe / conhost.exe).

    Critical for the multi-Windows-Terminal case: a single WT process
    can own multiple top-level windows, and parent-pid walking can only
    return "some WT window" — not the specific one hosting this pid's
    tab. The conPTY's GW_OWNER chain pinpoints the correct WT window.

    Returns ``(host_hwnd, console_title)`` on success. ``console_title``
    may be empty if the process never set one; the caller must handle
    that (the tab_selector returns False on empty title).
    Returns ``None`` for non-console targets or if AttachConsole fails.
    """
    info = win32_console.get_console_info(pid)
    if info is None:
        return None
    console_hwnd, console_title = info
    if not console_hwnd:
        return None
    host = walk_to_visible_host(console_hwnd, win32gui)
    if host is None:
        return None
    return (host, console_title)


def _ancestor_pids(pid: int) -> list[int]:
    """Return [pid, parent_pid, grandparent_pid, ...] up to _MAX_ANCESTOR_DEPTH.

    Order is significant: the original pid comes first so callers prefer the
    most specific match when they walk the list looking for a window owner.

    Returns an empty list for non-positive pids (PLACEHOLDER_PID=-1 entries
    inserted by HookSessionBridge before the scanner has confirmed a real
    process). psutil raises ValueError on negative pid; without this early
    return that ValueError propagated up to focus() and silently no-op'd
    the user's click — see Bug A from live-run testing 2026-05-13.
    """
    if pid <= 0:
        return []
    pids: list[int] = []
    try:
        proc: psutil.Process | None = psutil.Process(pid)
        for _ in range(_MAX_ANCESTOR_DEPTH):
            if proc is None:
                break
            pids.append(proc.pid)
            proc = proc.parent()
    except (psutil.NoSuchProcess, psutil.AccessDenied, ValueError):
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
