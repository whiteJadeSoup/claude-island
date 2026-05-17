"""Dump the live Windows Terminal UIA tree for every claude.exe pid
we can map to a WT host hwnd. For each TabItem found, print every
property that might let us match the TabItem back to a conhost / pid
without relying on TabItem.Name.

Goal: decide between two fix paths for the tab-auto-switch issue:
  (a) Heuristic by name shape ("contains Claude / OSC spinner glyph")
  (b) Hard mapping via UIA properties

This script's output answers "is (b) actually exposed by WT's UIA tree?"
"""
from __future__ import annotations

import io
import sys

# Force UTF-8 stdout — the user's Git Bash on Windows defaults to GBK,
# which fails on the brail-spinner glyphs Claude writes in OSC titles.
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

import psutil
import uiautomation as auto

from claude_island.platform_ import win32_console
from claude_island.platform_.process_scanner import ProcessScanner
from claude_island.platform_.window_activator import walk_to_visible_host


def _dump_props(ctrl: object, depth: int = 0) -> None:
    indent = "  " * depth
    try:
        cls = ctrl.ClassName
    except Exception:
        cls = "?"
    try:
        ctn = ctrl.ControlTypeName
    except Exception:
        ctn = "?"
    try:
        name = ctrl.Name
    except Exception:
        name = "?"
    try:
        aid = ctrl.AutomationId
    except Exception:
        aid = "?"
    try:
        nhwnd = ctrl.NativeWindowHandle
    except Exception:
        nhwnd = "?"
    try:
        pid = ctrl.ProcessId
    except Exception:
        pid = "?"
    print(f"{indent}{ctn} cls={cls!r} name={name!r:<60} aid={aid!r} nhwnd={nhwnd} pid={pid}")


def _walk(ctrl: object, depth: int = 0, max_depth: int = 8) -> None:
    if depth > max_depth:
        return
    _dump_props(ctrl, depth)
    try:
        children = ctrl.GetChildren()
    except Exception:
        children = []
    for ch in children:
        _walk(ch, depth + 1, max_depth)


def main() -> None:
    # Find every live claude.exe pid + its WT host hwnd.
    sessions = ProcessScanner().scan()
    if not sessions:
        raise SystemExit("no live claude.exe")
    import win32gui  # type: ignore

    pid_to_wt: dict[int, int] = {}
    for s in sessions:
        info = win32_console.get_console_info(s.pid)
        if info is None:
            print(f"pid={s.pid}: no console info (skipped)")
            continue
        conhost_hwnd, console_title = info
        host = walk_to_visible_host(conhost_hwnd, win32gui)
        host_str = f"{host:#x}" if host else "None"
        print(
            f"pid={s.pid:>6} cwd={s.project_path} "
            f"conhost_hwnd={conhost_hwnd:#x} console_title={console_title!r:<60} "
            f"wt_host={host_str}",
        )
        if host:
            pid_to_wt[s.pid] = host

    print()
    print("=" * 80)
    seen_wt_hwnds: set[int] = set()
    for pid, wt_hwnd in pid_to_wt.items():
        if wt_hwnd in seen_wt_hwnds:
            continue
        seen_wt_hwnds.add(wt_hwnd)
        print()
        print(f"WT host hwnd={wt_hwnd:#x} (first seen for pid={pid})")
        print("-" * 80)
        root = auto.ControlFromHandle(wt_hwnd)
        if root is None:
            print("UIA could not bind to wt_host")
            continue
        # Find TabControl
        tab_control = root.TabControl(searchDepth=10)
        if not tab_control.Exists(0.2):
            print("no TabControl in subtree")
            continue
        # Dump TabControl subtree
        print("=== TabControl subtree ===")
        _walk(tab_control, 0, max_depth=6)
        # For each TabItem, additionally walk its content (Pane/Term)
        print()
        print("=== Per-TabItem content walk ===")
        for c in tab_control.GetChildren():
            if c.ControlTypeName != "ListControl":
                continue
            for item in c.GetChildren():
                if item.ControlTypeName != "TabItemControl":
                    continue
                name = item.Name or ""
                print(f"\n-- TabItem name={name!r}")
                # Try to find the linked content control. WT's TabView
                # keeps content separately from TabItems.
                # Walk siblings of TabControl for ContentPresenter / Pane.
                _dump_props(item, 0)

        # Also walk root subtree looking for any Pane / Document /
        # TerminalControl-like controls and their NativeWindowHandle.
        print()
        print("=== Root subtree looking for terminal panes ===")
        def walk_all(c, depth=0):
            if depth > 12:
                return
            try:
                ctn = c.ControlTypeName
                cls = c.ClassName
                nhwnd = c.NativeWindowHandle
            except Exception:
                ctn = cls = nhwnd = ""
            # Print only nodes that have a NativeWindowHandle != 0
            if nhwnd and nhwnd != 0:
                try:
                    aid = c.AutomationId
                except Exception:
                    aid = ""
                print(f"  {'  '*depth}{ctn} cls={cls!r} nhwnd={nhwnd:#x} aid={aid!r}")
            try:
                children = c.GetChildren()
            except Exception:
                children = []
            for ch in children:
                walk_all(ch, depth + 1)
        walk_all(root)


if __name__ == "__main__":
    main()
