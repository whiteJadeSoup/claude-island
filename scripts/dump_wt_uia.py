#!/usr/bin/env python3
"""Dump the UIA tree of every visible Windows Terminal window.

Used to verify what claude-island's UIA enumeration can actually see
when probing a real WT window. Run this with WT in the foreground,
having activated the tab whose pane structure you want to inspect.

Usage:
    python scripts/dump_wt_uia.py                  # to stdout
    python scripts/dump_wt_uia.py > wt_uia.txt     # to file (utf-8)
"""
from __future__ import annotations

import io
import sys


_WT_CLASS_PREFIX = "CASCADIA_HOSTING_WINDOW_CLASS"


def find_wt_hwnds() -> list[int]:
    import win32gui
    hwnds: list[int] = []

    def _cb(h: int, _: object) -> bool:
        try:
            cls = win32gui.GetClassName(h)
            if cls.startswith(_WT_CLASS_PREFIX) and win32gui.IsWindowVisible(h):
                hwnds.append(h)
        except Exception:
            pass
        return True

    win32gui.EnumWindows(_cb, None)
    return hwnds


def dump(elem: object, depth: int = 0, *, max_depth: int = 25) -> None:
    if depth > max_depth:
        sys.stdout.write(f"{'  ' * depth}<truncated at depth {max_depth}>\n")
        return
    try:
        ctn = getattr(elem, "ControlTypeName", "?") or "?"
        cn = getattr(elem, "ClassName", "") or ""
        name = getattr(elem, "Name", "") or ""
        marker = "  <-- ci: SENTINEL" if name.startswith("ci:") else ""
        short_name = name if len(name) <= 60 else name[:57] + "..."
        sys.stdout.write(
            f"{'  ' * depth}{ctn} class={cn!r} name={short_name!r}{marker}\n"
        )
    except Exception as e:
        sys.stdout.write(f"{'  ' * depth}<read error: {e}>\n")
        return
    try:
        children = elem.GetChildren() or []
    except Exception as e:
        sys.stdout.write(f"{'  ' * (depth + 1)}<GetChildren failed: {e}>\n")
        return
    for c in children:
        dump(c, depth + 1, max_depth=max_depth)


def main() -> int:
    # Force UTF-8 output: PowerShell defaults to UTF-16 LE for stdout
    # redirection, which makes Inspect-style text dumps unreadable
    # (every other byte is \0). Wrap stdout in a UTF-8 TextIOWrapper.
    if sys.platform == "win32":
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            sys.stdout = io.TextIOWrapper(
                sys.stdout.buffer, encoding="utf-8", errors="replace",
            )

    try:
        import uiautomation as auto
    except ImportError:
        sys.stderr.write("uiautomation not installed: pip install uiautomation\n")
        return 1

    hwnds = find_wt_hwnds()
    if not hwnds:
        sys.stderr.write("No visible WT windows found\n")
        return 1

    for h in hwnds:
        sys.stdout.write(f"\n=== WT window hwnd={hex(h)} ===\n")
        root = auto.ControlFromHandle(h)
        if root is None:
            sys.stdout.write("  ControlFromHandle returned None\n")
            continue
        dump(root, max_depth=25)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
