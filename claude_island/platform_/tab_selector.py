from __future__ import annotations

import sys


def select_tab_by_title(hwnd: int, title: str) -> bool:
    """Select the Windows Terminal tab whose Name equals *title*.

    Accepts any HWND — self-defensive against invalid or already-destroyed handles.

    Returns True  if the tab is now selected (including already-selected no-op).
    Returns False for any failure: library absent, no TabControl found, no
              matching tab title, UIA pattern unavailable, or any exception.
    Never raises.
    """
    if not title:
        return False
    try:
        import uiautomation as auto  # conditional win32 dep; lazy to keep module importable everywhere

        root = auto.ControlFromHandle(hwnd)
        if root is None:
            return False

        # searchDepth=10 covers Windows Terminal's WinUI3 tree (~6 levels deep in practice)
        tab_control = root.TabControl(searchDepth=10)
        if not tab_control.Exists(0.1):
            return False  # non-tabbed terminal (ConEmu, conhost, cmd.exe, etc.)

        tab_item = tab_control.TabItemControl(Name=title, searchDepth=2)
        if not tab_item.Exists(0.1):
            return False  # no tab with this exact title; caller falls back to foreground-only

        pattern = tab_item.GetSelectionItemPattern()
        if pattern is None:
            return False

        if pattern.IsSelected:
            return True  # already active — no-op, still a success

        pattern.Select()
        return True
    except Exception as exc:
        print(f"[claude-island] tab_selector: {exc}", file=sys.stderr)
        return False
