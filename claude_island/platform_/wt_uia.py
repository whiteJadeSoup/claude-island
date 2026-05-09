"""Windows Terminal UI Automation operations.

Single entry point for all UIA work in claude-island. Four public
functions:

- ``collect_wt_tab_titles`` — set of every visible TabItem.Name across
  every WT top-level window. Used by ProcessScanner to filter orphan
  claude.exe processes (a process whose console title doesn't appear
  in any visible WT tab is no longer rendered anywhere — it's an
  orphan, hide it).
- ``list_ci_tab_names`` — set of every ``ci:*`` TabItem.Name in
  *hwnd*'s tab strip. Used by the WT adapter's group() to decide
  whether same-cwd sessions in one window are split panes or just
  separate tabs — sessions whose sentinel doesn't appear here are
  inactive panes (no own TabItem), so they belong with their
  active sibling in a multi-view group.
- ``select_tab_by_title`` — within a specific WT window's UIA tree,
  bring the matching TabItem to the foreground. Used by the WT
  adapter's focus path on click.
- ``wait_for_tab_name`` — poll the UIA tree until a TabItem with the
  given Name appears. Used after ``set_console_title`` to confirm WT
  picked up the OSC-propagated title change before we issue
  ``select_tab_by_title``.

All accept and return primitives so future ConEmu / iTerm2 backends
can sit alongside without coupling to claude-island domain types.
All are fail-safe: any UIA failure returns the "unknown / no-op"
sentinel (``None`` / ``False``) so callers can fall back gracefully.

Removed in 2026-05: ``select_any_ci_tab`` and
``enumerate_active_tab_sentinels`` (the sibling-cache machinery).
End-to-end UIA dump (scripts/dump_wt_uia.py) showed that
TermControl.Name never carries our sentinel — the ``--title`` arg
sets the Tab's title, not the TermControl's StartingTitle — so
sibling enumeration was structurally impossible. Inactive panes
within an active tab are also physically absent from the UIA tree
(WT clears tab content on switch via TabManagement.cpp), so no
pane-level identification of inactive split panes is reachable from
outside WT. The UI now groups same-window sessions visually
instead of trying to disambiguate within a window.
"""
from __future__ import annotations

import logging
import sys
import time

log = logging.getLogger(__name__)

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
        # Click-driven (one call per click), so a debug-level line
        # is enough — under-the-rug failures here become "click did
        # nothing" which the user reports separately.
        log.debug("select_tab_by_title(%s, %r) failed: %s",
                  hex(hwnd), title, exc)
        return False


def list_ci_tab_names(hwnd: int) -> set[str]:
    """Return the set of ``ci:*`` TabItem.Name values in *hwnd*'s
    tab strip.

    Used by the WT adapter's group() to decide whether two same-cwd
    same-window sessions are real split panes (≥1 sentinel missing
    from the set) vs. separate tabs that happen to share cwd (every
    sentinel present). Latter case was the dev/dev2 over-grouping
    bug that motivated the singleton-only fix earlier in this branch;
    we now restore cwd-based grouping but use this signal to detect
    and skip the false-positive case.

    Returns empty set on any failure or non-Windows.
    """
    if sys.platform != "win32":
        return set()
    out: set[str] = set()
    try:
        import uiautomation as auto

        root = auto.ControlFromHandle(hwnd)
        if root is None:
            return out
        tab_control = root.TabControl(searchDepth=10)
        if not tab_control.Exists(0.1):
            return out

        # BFS bounded — TabItemControls live a few levels under
        # TabControl (WinUI3 wraps in ListView).
        frontier: list[tuple[object, int]] = [(tab_control, 0)]
        while frontier:
            node, depth = frontier.pop(0)
            try:
                children = node.GetChildren()
            except Exception:
                continue
            for child in children:
                if getattr(child, "ControlTypeName", "") == "TabItemControl":
                    name = getattr(child, "Name", "") or ""
                    if name.startswith("ci:"):
                        out.add(name)
                    # Don't descend — TabItem subtree is label widgets.
                    continue
                if depth + 1 < 4:
                    frontier.append((child, depth + 1))
    except Exception as exc:
        # Wake-driven hot path (~5 Hz per multi-view bucket). Using
        # print(..., file=stderr) here would flood the console any time
        # WT's UIA service is briefly unavailable (WT updating, focus
        # stolen by another tool). debug() lets operators opt in via
        # logging config without touching the call site.
        log.debug("list_ci_tab_names(%s) failed: %s", hex(hwnd), exc)
    return out


def wait_for_tab_name(
    hwnd: int, name: str, *, timeout_ms: int = 200, poll_ms: int = 10,
) -> bool:
    """Poll the UIA tree under *hwnd* until a TabItem with this exact Name
    exists, or *timeout_ms* elapses.

    Used after ``set_console_title`` to wait for WT's OSC pipeline
    (kernel SetConsoleTitleW → conhost → conpty → WT XAML) to
    propagate the new title into TabItem.Name. Empirically this takes
    one to a few frames (~16–50 ms) on a quiet system; we use 10 ms
    poll cadence and a 200 ms hard cap. The cap is what triggers the
    silent-fail fallback when the target tab uses a profile with
    ``suppressApplicationTitle: true`` (in which case WT discards the
    propagated update and TabItem.Name will never become *name*).

    Returns ``True`` once a matching TabItem is observed, ``False`` on
    timeout or any UIA failure. Never raises.
    """
    if not name:
        return False
    if sys.platform != "win32":
        return False
    try:
        import uiautomation as auto
    except ImportError:
        return False

    deadline = time.monotonic() + (timeout_ms / 1000.0)
    poll_s = poll_ms / 1000.0
    while True:
        try:
            root = auto.ControlFromHandle(hwnd)
            if root is not None:
                tab_control = root.TabControl(searchDepth=10)
                if tab_control.Exists(0.0):
                    tab_item = tab_control.TabItemControl(
                        Name=name, searchDepth=2,
                    )
                    if tab_item.Exists(0.0):
                        return True
        except Exception:
            # UIA hiccup mid-poll (tab being created, WT busy painting).
            # Don't bail — give the deadline a chance.
            pass
        if time.monotonic() >= deadline:
            return False
        time.sleep(poll_s)
