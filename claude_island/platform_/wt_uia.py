"""Windows Terminal UI Automation operations.

Single entry point for all UIA work in claude-island. Five public
functions:

- ``collect_wt_tab_titles`` — set of every visible TabItem.Name across
  every WT top-level window. Used by ProcessScanner to filter orphan
  claude.exe processes (a process whose console title doesn't appear
  in any visible WT tab is no longer rendered anywhere — it's an
  orphan, hide it).
- ``select_tab_by_title`` — within a specific WT window's UIA tree,
  bring the matching TabItem to the foreground. Used by WindowActivator
  on click.
- ``select_any_ci_tab`` — within a specific WT window's UIA tree,
  select the first TabItem whose Name starts with ``ci:``. Used as a
  fallback when ``select_tab_by_title`` misses on the inactive pane
  of a split-tab (its TabItem.Name reflects the active sibling pane).
- ``wait_for_tab_name`` — poll the UIA tree until a TabItem with the
  given Name appears. Used after ``set_console_title`` to confirm WT
  picked up the OSC-propagated title change before we issue
  ``select_tab_by_title``.
- ``enumerate_active_tab_sentinels`` — enumerate the ``ci:*`` Names
  of every TermControl inside the active TabItem of *hwnd*. Powers
  the sibling-pane cache that resolves the split-pane click problem
  (only the active pane's sentinel reaches TabItem.Name; sibling
  panes' sentinels live on their own TermControl.Name inside the
  active tab's subtree, where WinUI3 does NOT virtualize them).

All three accept and return primitives so future ConEmu / iTerm2
backends can sit alongside without coupling to claude-island domain types.
All are fail-safe: any UIA failure returns the "unknown / no-op"
sentinel (``None`` / ``False``) so callers can fall back gracefully.
"""
from __future__ import annotations

import sys
import time

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


def select_any_ci_tab(hwnd: int) -> bool:
    """Select the first TabItem under *hwnd* whose Name starts with
    ``"ci:"`` — the sentinel-prefix fallback for split-pane click.

    Why this exists: when two claude sessions share one WT tab as
    split panes, ``TabItem.Name`` only reflects the *active pane*'s
    console title (WinUI3 lazy-loads inactive panes' TermControls).
    A click on the inactive-pane row in the panel runs
    ``select_tab_by_title`` with the inactive pane's sentinel — which
    matches no TabItem and fails. Falling back to "any ci:* tab"
    selects the sibling tab that DOES carry our sentinel as its
    active pane's title, so WT at least lands on the correct tab.
    The user finishes by pressing Alt+arrow to focus the right pane.

    Limitation: when the same WT window holds *multiple* tabs whose
    active pane is one of our sessions (e.g. four sessions split
    across two tabs), this picks whichever ``ci:*`` tab UIA returns
    first — not necessarily the target session's tab. Disambiguating
    that requires per-session wt_hwnd tracking on SessionView, which
    is a larger refactor; this fallback is the "good in 70% of
    cases, never silently no-op" minimum.

    Returns ``True`` on successful select (including the no-op case
    where the matching tab was already selected). Returns ``False``
    on any failure: non-Windows, library absent, no TabControl, no
    ``ci:`` tab, UIA pattern unavailable, or any exception.
    Never raises.
    """
    if sys.platform != "win32":
        return False
    try:
        import uiautomation as auto

        root = auto.ControlFromHandle(hwnd)
        if root is None:
            return False
        tab_control = root.TabControl(searchDepth=10)
        if not tab_control.Exists(0.1):
            return False

        # WinUI3 wraps tab buttons in an inner ListControl; BFS down
        # bounded depth (matches _collect_tab_names walk).
        tab_item = _find_first_ci_tab(tab_control, max_depth=4)
        if tab_item is None:
            return False

        pattern = tab_item.GetSelectionItemPattern()
        if pattern is None:
            return False
        if pattern.IsSelected:
            return True
        pattern.Select()
        return True
    except Exception as exc:
        import sys as _sys
        print(f"[claude-island] wt_uia.select_any_ci_tab: {exc}",
              file=_sys.stderr)
        return False


def _find_first_ci_tab(elem: object, *, max_depth: int) -> object | None:
    """BFS the subtree under *elem* and return the first descendant
    TabItemControl whose Name starts with ``"ci:"``."""
    frontier: list[tuple[object, int]] = [(elem, 0)]
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
                    return child
                # TabItemControl subtree is the tab's content (TermControl
                # etc.), not more tabs — don't descend.
                continue
            if depth + 1 < max_depth:
                frontier.append((child, depth + 1))
    return None


def enumerate_active_tab_sentinels(hwnd: int) -> set[str]:
    """Return the set of ``ci:*`` Names of every TermControl inside
    the *active* TabItem under *hwnd*.

    Powers ``PaneSiblingTracker``: every sentinel returned here is a
    pane in the same WT tab as every other sentinel returned (they
    are observed siblings). The caller records the set so that future
    clicks on any of these sentinels can fall back to the others.

    Why active-tab only: WinUI3 TabView virtualizes inactive tabs —
    their TermControls aren't in the UIA tree until that tab becomes
    active. The active tab's subtree IS fully populated (including
    its inactive panes), which is exactly what we need.

    ``TermControl.Name`` is set to ``StartingTitle`` by
    ``TermControlAutomationPeer::GetNameCore``; this equals our
    ``--title ci:{uuid}`` arg from Plan-L spawn. For Plan-O sessions
    (sentinel set via SetConsoleTitleW after spawn), StartingTitle
    is empty so Name falls back to the live ``Title()`` — also our
    sentinel. Either way Name carries ``ci:*`` for the sessions we
    care about.

    Returns empty set on any failure (non-Windows, library absent,
    no active tab found, no TermControls under it, UIA exception).
    Never raises.
    """
    if sys.platform != "win32":
        return set()

    from claude_island.platform_.wt_pane_siblings import _dbg

    sentinels: set[str] = set()
    try:
        import uiautomation as auto

        root = auto.ControlFromHandle(hwnd)
        if root is None:
            _dbg(f"enumerate({hex(hwnd)}): root is None")
            return sentinels
        tab_control = root.TabControl(searchDepth=10)
        if not tab_control.Exists(0.1):
            _dbg(f"enumerate({hex(hwnd)}): no TabControl in UIA tree")
            return sentinels

        active_tab = _find_active_tab_item(tab_control, max_depth=4)
        if active_tab is None:
            _dbg(f"enumerate({hex(hwnd)}): no active TabItem found")
            return sentinels

        _dbg(f"enumerate({hex(hwnd)}): active TabItem.Name={active_tab.Name!r}")
        _collect_termcontrol_sentinels(active_tab, sentinels, max_depth=8)
        _dbg(f"enumerate({hex(hwnd)}): collected sentinels={sentinels!r}")
    except Exception as exc:
        import sys as _sys
        print(f"[claude-island] wt_uia.enumerate_active_tab_sentinels: {exc}",
              file=_sys.stderr)
    return sentinels


def _find_active_tab_item(elem: object, *, max_depth: int) -> object | None:
    """BFS for the TabItemControl whose SelectionItemPattern.IsSelected
    is True. Returns the element or None."""
    frontier: list[tuple[object, int]] = [(elem, 0)]
    while frontier:
        node, depth = frontier.pop(0)
        try:
            children = node.GetChildren()
        except Exception:
            continue
        for child in children:
            if getattr(child, "ControlTypeName", "") == "TabItemControl":
                try:
                    p = child.GetSelectionItemPattern()
                    if p is not None and p.IsSelected:
                        return child
                except Exception:
                    pass
                # Don't descend into other TabItems either way — they
                # are siblings in the strip, not parents of more tabs.
                continue
            if depth + 1 < max_depth:
                frontier.append((child, depth + 1))
    return None


def _collect_termcontrol_sentinels(
    elem: object, sink: set[str], *, max_depth: int,
) -> None:
    """BFS under *elem*; for each descendant whose Name starts with
    ``ci:``, add it to *sink*. Don't descend into TermControl-classed
    nodes (their subtree is screen-reader text, not more sentinels)
    but DO descend into anything else regardless of ClassName — WinUI3
    wraps TermControl in several layers of ContentPresenter / Border
    / Pane / etc, and we don't know all the wrapper class names. The
    Name match is what makes us precise; ClassName is just an
    optimization to skip TermControl's chatty interior."""
    from claude_island.platform_.wt_pane_siblings import _dbg

    frontier: list[tuple[object, int]] = [(elem, 0)]
    visited_classes: dict[str, int] = {}
    while frontier:
        node, depth = frontier.pop(0)
        try:
            children = node.GetChildren()
        except Exception:
            continue
        for child in children:
            class_name = getattr(child, "ClassName", "") or ""
            visited_classes[class_name] = visited_classes.get(class_name, 0) + 1
            name = getattr(child, "Name", "") or ""
            if name.startswith("ci:"):
                sink.add(name)
                _dbg(
                    f"  collect: matched name={name!r} "
                    f"class={class_name!r} depth={depth + 1}"
                )
                # If it's TermControl, don't descend — its subtree is
                # text content; we won't find more sentinels there.
                if class_name == "TermControl":
                    continue
            if depth + 1 < max_depth:
                frontier.append((child, depth + 1))
    _dbg(f"  collect: visited classes (count) = {visited_classes!r}")


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
