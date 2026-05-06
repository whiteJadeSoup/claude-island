"""Thread-safe AttachConsole / GetConsoleTitleW wrapper.

AttachConsole modifies process-wide console state: it detaches the
calling process from its own console and re-attaches it to the target
pid's console. Two callers exercise this in claude-island:

- ProcessScanner (scan thread, ~10s tick): pulls the title for every
  candidate claude.exe so the orphan filter can compare against the
  set of visible WT tab titles.
- WindowActivator (Qt main thread, on click): resolves the visible
  host window AND captures the console title for tab selection.

Both contend for a single OS-global resource. The module-level lock
serialises the FreeConsole/AttachConsole/GetConsoleTitleW/FreeConsole
sequence so the threads don't trample each other's console state.
The lock is held only for the dance itself — UIA / GW_OWNER walks
are not held under it.
"""
from __future__ import annotations

import sys
import threading

_ATTACH_PARENT_PROCESS = 0xFFFFFFFF
_TITLE_BUF_LEN = 512
_lock = threading.Lock()


def get_console_info(pid: int) -> tuple[int, str] | None:
    """Return ``(conpty_hwnd, console_title)`` for the target *pid*.

    ``conpty_hwnd`` is the hidden pseudo-console window — callers that
    need a visible host must walk GW_OWNER from it. ``console_title``
    is captured while AttachConsole(pid) is active; may be empty.

    Returns ``None`` on any failure (non-Windows, ctypes unavailable,
    AttachConsole fails — target has no console / access denied /
    already attached). Never raises.

    Side effect: temporarily detaches our process from its own console.
    Re-attaches to the parent console only if we had one to begin with;
    pythonw / windowed-frozen-exe processes (GetConsoleWindow == 0 at
    entry) skip the re-attach to avoid being left console-less.
    """
    if sys.platform != "win32":
        return None
    try:
        import ctypes
    except ImportError:
        return None

    kernel32 = ctypes.windll.kernel32

    with _lock:
        original_console = kernel32.GetConsoleWindow()
        kernel32.FreeConsole()
        result: tuple[int, str] | None = None
        try:
            if kernel32.AttachConsole(pid):
                try:
                    hwnd = kernel32.GetConsoleWindow()
                    buf = ctypes.create_unicode_buffer(_TITLE_BUF_LEN)
                    kernel32.GetConsoleTitleW(buf, _TITLE_BUF_LEN)
                    if hwnd:
                        result = (hwnd, buf.value)
                finally:
                    kernel32.FreeConsole()
        finally:
            if original_console:
                try:
                    kernel32.AttachConsole(_ATTACH_PARENT_PROCESS)
                except Exception:
                    pass

    return result


def set_console_title(pid: int, title: str) -> bool:
    """Set the console title for *pid*'s attached console.

    AttachConsole + SetConsoleTitleW + FreeConsole, all under the same
    module-level lock as ``get_console_info`` so the two callers don't
    trample each other's transient console state.

    Returns ``True`` on success, ``False`` on any failure (non-Windows,
    AttachConsole fails, SetConsoleTitleW fails — the latter happens
    silently when the target tab is in a profile with
    ``suppressApplicationTitle: true``, since WT discards the
    propagated OSC update without reporting back). Never raises.

    Why this lives here rather than in window_activator: the
    AttachConsole/FreeConsole + parent-console-restore dance is
    identical to ``get_console_info``. Centralising both means there's
    one place that knows the rules (UTF-16 buffer for titles,
    pythonw "no parent console" handling, lock discipline).
    """
    if sys.platform != "win32":
        return False
    if title is None:
        return False
    try:
        import ctypes
    except ImportError:
        return False

    kernel32 = ctypes.windll.kernel32

    with _lock:
        original_console = kernel32.GetConsoleWindow()
        kernel32.FreeConsole()
        ok = False
        try:
            if kernel32.AttachConsole(pid):
                try:
                    # SetConsoleTitleW returns nonzero on success.
                    # Note: success here means "the syscall accepted
                    # the title", NOT "WT updated TabItem.Name". The
                    # latter is async (OSC propagation) and silent
                    # under suppressApplicationTitle. Caller polls
                    # UIA separately if it needs to confirm.
                    ok = bool(kernel32.SetConsoleTitleW(title))
                finally:
                    kernel32.FreeConsole()
        finally:
            if original_console:
                try:
                    kernel32.AttachConsole(_ATTACH_PARENT_PROCESS)
                except Exception:
                    pass

    return ok
