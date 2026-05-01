"""Windows Terminal UI Automation operations.

Single entry point for all UIA work in claude-island. Two public
functions:

- ``collect_wt_tab_titles`` — set of every visible TabItem.Name across
  every WT top-level window. Used by ProcessScanner to filter orphan
  claude.exe processes (a process whose console title doesn't appear
  in any visible WT tab is no longer rendered anywhere — it's an
  orphan, hide it).
- ``select_tab_by_title`` — within a specific WT window's UIA tree,
  bring the matching TabItem to the foreground. Used by WindowActivator
  on click.

Both functions accept and return primitives so future ConEmu / iTerm2
backends can sit alongside without coupling to claude-island domain types.
Both are fail-safe: any UIA failure returns the "unknown / no-op"
sentinel (``None`` / ``False``) so callers can fall back gracefully.

Reserved for PR2 (not implemented here): ``collect_wt_tab_panes`` —
maps console title to ``(wt_hwnd, tab_index)`` so the UI can group
same-tab sessions visually.
"""
from __future__ import annotations

import sys

# WT main window class. Same for stable, preview, and dev builds at the
# time of writing; if Microsoft introduces a new variant we'll learn
# about it via the orphan-filter regression rather than a crash.
_WT_CLASS_PREFIX = "CASCADIA_HOSTING_WINDOW_CLASS"


def collect_wt_tab_titles() -> set[str] | None:
    """Return the union of TabItem.Name values across every visible
    Windows Terminal top-level window.

    Returns:
        ``None`` when the check cannot run (non-Windows, library absent,
        no WT windows found, EnumWindows failure, or every UIA query
        raises). Caller treats this as "skip the orphan filter" — i.e.
        better to show a stale session than to hide a live one.

        Empty set: WT was found but exposed no tab names (degenerate;
        most likely UIA tree wasn't ready). Caller should also treat
        as fail-open via the same "not live_titles → skip" check.

        Otherwise: the set of distinct tab names. May contain duplicates
        across panes within a tab (UIA names per TabItem, not per pane).
    """
    if sys.platform != "win32":
        return None
    try:
        import uiautomation as auto
        import win32gui
    except ImportError:
        return None

    wt_hwnds: list[int] = []

    def _cb(hwnd: int, _: object) -> bool:
        try:
            cls = win32gui.GetClassName(hwnd)
            if cls.startswith(_WT_CLASS_PREFIX) and win32gui.IsWindowVisible(hwnd):
                wt_hwnds.append(hwnd)
        except Exception:
            pass
        return True

    try:
        win32gui.EnumWindows(_cb, None)
    except Exception:
        return None

    if not wt_hwnds:
        return None  # no WT — caller skips orphan filter

    titles: set[str] = set()
    for hwnd in wt_hwnds:
        try:
            root = auto.ControlFromHandle(hwnd)
            if root is None:
                continue
            tab_control = root.TabControl(searchDepth=10)
            if not tab_control.Exists(0.1):
                continue
            # WinUI3 TabView wraps tabs inside an inner ListControl; tab
            # buttons are not direct children of TabControl. BFS down a
            # bounded depth to collect every TabItemControl.
            _collect_tab_names(tab_control, titles, max_depth=4)
        except Exception:
            continue

    return titles if titles else None


def _collect_tab_names(elem: object, sink: set[str], *, max_depth: int) -> None:
    """Walk ``elem``'s subtree up to ``max_depth`` levels and add each
    descendant TabItemControl's Name into ``sink``.

    Iterative BFS so we don't blow the stack on a pathological tree.
    """
    frontier: list[tuple[object, int]] = [(elem, 0)]
    while frontier:
        node, depth = frontier.pop(0)
        try:
            children = node.GetChildren()
        except Exception:
            continue
        for child in children:
            if getattr(child, "ControlTypeName", "") == "TabItemControl":
                name = getattr(child, "Name", "")
                if name:
                    sink.add(name)
                # Don't descend into TabItemControl: its subtree is the
                # tab's content (TermControl etc.), not more tabs.
                continue
            if depth + 1 < max_depth:
                frontier.append((child, depth + 1))


def select_tab_by_title(hwnd: int, title: str) -> bool:
    """Select the WT TabItem named *title* in the UIA subtree at *hwnd*.

    Accepts any HWND — self-defensive against invalid or already-destroyed
    handles. Caller passes the WT host HWND obtained from the GW_OWNER
    walk on the conPTY pseudo-console.

    Returns:
        ``True`` — tab is now selected (including the no-op case where
        it was already selected).
        ``False`` — any failure: empty title, library absent, no
        TabControl, no matching tab, UIA pattern unavailable, or any
        exception. Caller should fall back to plain foreground.
    Never raises.
    """
    if not title:
        return False
    try:
        import uiautomation as auto

        root = auto.ControlFromHandle(hwnd)
        if root is None:
            return False

        # searchDepth=10 covers WT's WinUI3 tree (~6 levels in practice).
        tab_control = root.TabControl(searchDepth=10)
        if not tab_control.Exists(0.1):
            return False  # non-tabbed terminal (ConEmu, conhost, cmd.exe, etc.)

        tab_item = tab_control.TabItemControl(Name=title, searchDepth=2)
        if not tab_item.Exists(0.1):
            return False  # no tab with this exact title

        pattern = tab_item.GetSelectionItemPattern()
        if pattern is None:
            return False

        if pattern.IsSelected:
            return True  # already active — no-op, still success

        pattern.Select()
        return True
    except Exception as exc:
        import sys as _sys
        print(f"[claude-island] wt_uia.select_tab_by_title: {exc}",
              file=_sys.stderr)
        return False
