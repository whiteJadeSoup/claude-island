#!/usr/bin/env python3
"""End-to-end probe of the WT focus path.

Walks every running claude.exe / node.exe(claude) process, builds a
SessionView for each, and traces what _activate_windows would do for
that session — without actually firing SetForegroundWindow (we just
report what each step would resolve to).

Used to verify click behavior end-to-end without launching the full
claude-island GUI. If a session's click "doesn't work" in the panel,
this tells us exactly where the chain breaks.
"""
from __future__ import annotations

import io
import sys
from pathlib import Path


def _force_utf8():
    if sys.platform == "win32":
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            sys.stdout = io.TextIOWrapper(
                sys.stdout.buffer, encoding="utf-8", errors="replace",
            )


def find_claude_pids() -> list[tuple[int, str, str]]:
    """Return list of (pid, exe_name, ancestor_chain_str) for processes
    that look like claude sessions inside Windows Terminal."""
    import psutil
    out: list[tuple[int, str, str]] = []
    for p in psutil.process_iter(["pid", "name", "exe", "cmdline"]):
        try:
            name = (p.info.get("name") or "").lower()
            cmdline = p.info.get("cmdline") or []
            if name not in ("claude.exe", "node.exe"):
                continue
            # Heuristic: must have "claude" in cmdline somewhere.
            if not any("claude" in (a or "").lower() for a in cmdline):
                continue
            # Walk ancestors to confirm WT host.
            ancestors: list[str] = []
            cur = p
            for _ in range(10):
                try:
                    parent = cur.parent()
                except Exception:
                    break
                if parent is None:
                    break
                ancestors.append(parent.name())
                cur = parent
            if not any(a.lower() in ("windowsterminal.exe", "wt.exe")
                       for a in ancestors):
                continue
            out.append((p.info["pid"], name, " > ".join(ancestors[:5])))
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return out


def trace_focus(pid: int, all_pid_cwd: dict[int, tuple[str, int]] | None = None) -> None:
    """Trace what _activate_windows would do for *pid*. Doesn't
    actually call SetForegroundWindow — only reports the resolved
    state.

    *all_pid_cwd* maps every claude pid to (cwd, wt_hwnd) so we can
    simulate the cwd-matched-sibling fallback step exactly as the WT
    adapter would.
    """
    import psutil
    from pathlib import Path
    from claude_island.core.snapshot import _normalize_project_path
    from claude_island.platform_ import win32_console, wt_uia, window_activator
    from claude_island.platform_.wt_session_title import sentinel_title

    print(f"\n--- pid={pid} ---")

    info = win32_console.get_console_info(pid)
    if info is None:
        print("  [FAIL] AttachConsole failed (orphan? access denied?)")
        return
    conpty_hwnd, current_title = info
    print(f"  conpty_hwnd  = {hex(conpty_hwnd)}")
    print(f"  current_title= {current_title!r}")

    try:
        import win32gui
    except ImportError:
        print("  [FAIL] pywin32 not installed")
        return

    wt_hwnd = window_activator.walk_to_visible_host(conpty_hwnd, win32gui)
    if not wt_hwnd:
        print("  [FAIL] walk_to_visible_host → None")
        return
    print(f"  wt_hwnd      = {hex(wt_hwnd)}")

    # Get THIS pid's cwd (used to filter siblings).
    try:
        my_cwd = psutil.Process(pid).cwd()
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        my_cwd = "<unknown>"
    print(f"  my_cwd       = {my_cwd!r}")

    # Lookup what TabItems exist in this WT window.
    tab_titles = _list_tab_titles(wt_hwnd)
    print(f"  tab_titles in this WT window:")
    for t in tab_titles:
        marker = "  ← matches current console" if t == current_title else ""
        print(f"     - {t!r}{marker}")

    target = current_title if current_title.startswith("ci:") else current_title
    matches = target in tab_titles
    print(
        f"  step1 select_tab_by_title({target!r}) → "
        f"{'HIT' if matches else 'MISS'}"
    )

    if matches:
        print(f"  → done. WT switches to that tab.")
        return

    # Inactive-pane case. Compute cwd-matched sibling sentinels
    # (worktree paths normalized — claude-code split-pane between main
    # repo and worktree subdirectory is common).
    my_cwd_norm = _normalize_project_path(Path(my_cwd))
    print(f"  step2 same-norm-cwd same-wt-hwnd siblings (my_cwd_norm={my_cwd_norm!r}):")
    sibling_sentinels: list[str] = []
    if all_pid_cwd:
        for sib_pid, (sib_cwd, sib_wt) in all_pid_cwd.items():
            if sib_pid == pid:
                continue
            if sib_wt != wt_hwnd:
                continue
            sib_cwd_norm = _normalize_project_path(Path(sib_cwd))
            if sib_cwd_norm != my_cwd_norm:
                continue
            # Read sib's current console title to use as its sentinel.
            sib_info = win32_console.get_console_info(sib_pid)
            if sib_info is None:
                continue
            sib_title = sib_info[1]
            sibling_sentinels.append(sib_title)
            print(f"     candidate: pid={sib_pid} cwd={sib_cwd!r} title={sib_title!r}")

    if not sibling_sentinels:
        print(f"  step2 no same-cwd siblings → falls to _force_foreground")
        print(f"  → user sees WT window come to front; tab unchanged → 'no visible response'")
        return

    # Try each sibling sentinel against tab_titles (simulating select_tab_by_title).
    chosen = None
    for sib in sibling_sentinels:
        if sib in tab_titles:
            chosen = sib
            break
    if chosen:
        print(f"  step2 select_tab_by_title({chosen!r}) → HIT")
        print(f"  → WT switches to that tab. Likely the click target's tab "
              f"(split-pane sibling). User uses Alt+arrow to focus correct pane.")
    else:
        print(f"  step2 all sibling sentinels miss → falls to _force_foreground")
        print(f"  → user sees WT come to front, tab unchanged.")


def _list_tab_titles(wt_hwnd: int) -> list[str]:
    """Enumerate TabItemControl.Name values under *wt_hwnd*."""
    if sys.platform != "win32":
        return []
    try:
        import uiautomation as auto
    except ImportError:
        return []
    out: list[str] = []
    try:
        root = auto.ControlFromHandle(wt_hwnd)
        if root is None:
            return []
        tab_control = root.TabControl(searchDepth=10)
        if not tab_control.Exists(0.1):
            return []

        # BFS collect TabItemControl.Name (all of them, ci:* or not).
        frontier = [(tab_control, 0)]
        while frontier:
            node, depth = frontier.pop(0)
            try:
                children = node.GetChildren()
            except Exception:
                continue
            for c in children:
                if getattr(c, "ControlTypeName", "") == "TabItemControl":
                    name = getattr(c, "Name", "") or ""
                    if name:
                        out.append(name)
                    continue
                if depth + 1 < 4:
                    frontier.append((c, depth + 1))
    except Exception as exc:
        print(f"  [_list_tab_titles] {exc}")
    return out


def trace_grouping(pid_cwd: dict[int, tuple[str, int]]) -> None:
    """Simulate the WT adapter's group() bucketing decision for the
    discovered pids and report the resulting cards."""
    from pathlib import Path
    from claude_island.core.snapshot import _normalize_project_path
    from claude_island.platform_ import wt_uia

    print("\n=== group() bucket simulation ===")

    # Bucket by (wt_hwnd, normalized_cwd).
    buckets: dict[tuple, list[int]] = {}
    singletons: list[int] = []
    for pid, (cwd, wt) in pid_cwd.items():
        if not wt:
            singletons.append(pid)
            continue
        norm = _normalize_project_path(Path(cwd))
        buckets.setdefault((wt, norm), []).append(pid)

    # Sentinel-presence detection per multi-pid bucket.
    cache: dict[int, set[str]] = {}
    for (wt, cwd), pids_in in list(buckets.items()):
        if len(pids_in) <= 1:
            continue
        if wt not in cache:
            cache[wt] = wt_uia.list_ci_tab_names(wt)
        tab_names = cache[wt]
        # Read each pid's current title as its sentinel.
        from claude_island.platform_ import win32_console
        sentinels: set[str] = set()
        for p in pids_in:
            info = win32_console.get_console_info(p)
            if info and info[1].startswith("ci:"):
                sentinels.add(info[1])
        if sentinels and sentinels.issubset(tab_names):
            print(f"  bucket (wt={hex(wt)}, cwd={cwd!r}): pids={pids_in} "
                  "→ all sentinels present → DEMOTED to singletons (separate tabs)")
            del buckets[(wt, cwd)]
            singletons.extend(pids_in)
        else:
            print(f"  bucket (wt={hex(wt)}, cwd={cwd!r}): pids={pids_in} "
                  f"→ at least one sentinel missing → KEPT grouped (split panes)")

    print("\n=== final grouping (UI cards) ===")
    n = 1
    for (wt, cwd), pids_in in buckets.items():
        print(f"  card #{n}: wt={hex(wt)} cwd={cwd!r} pids={pids_in}")
        n += 1
    for p in singletons:
        cwd, wt = pid_cwd.get(p, ("?", 0))
        print(f"  card #{n}: SINGLETON pid={p} cwd={cwd!r} wt={hex(wt)}")
        n += 1


def main() -> int:
    _force_utf8()
    pids = find_claude_pids()
    if not pids:
        print("No claude processes found.")
        return 1
    print(f"Found {len(pids)} claude session pids:")

    # Pre-resolve (cwd, wt_hwnd) for every claude pid — used by
    # trace_focus to simulate the cwd-matched-sibling fallback.
    import psutil
    try:
        import win32gui
    except ImportError:
        win32gui = None
    from claude_island.platform_ import win32_console, window_activator

    pid_cwd: dict[int, tuple[str, int]] = {}
    for pid, name, anc in pids:
        cwd = "<unknown>"
        try:
            cwd = psutil.Process(pid).cwd()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
        info = win32_console.get_console_info(pid)
        wt = 0
        if info and win32gui:
            wt = window_activator.walk_to_visible_host(info[0], win32gui) or 0
        pid_cwd[pid] = (cwd, wt)
        print(f"  pid={pid} cwd={cwd!r} wt={hex(wt)} ancestors=[{anc}]")

    trace_grouping(pid_cwd)

    for pid, _, _ in pids:
        trace_focus(pid, all_pid_cwd=pid_cwd)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
