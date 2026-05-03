"""Windows Terminal adapter — AttachConsole + UIA grouping and focus.

Handles claude.exe / node.exe (claude) sessions launched inside
Windows Terminal. Groups by ``wt_hwnd`` (the visible WT main window);
within a group, sorts by tab-order heuristic from WT's UIA tree.

FOCUS capability: SetForegroundWindow on the WT window + UIA
Select on the tab whose TabItem.Name matches (via console title or
sibling fallback from inactive split panes).

This file absorbs the logic previously spread across:
- platform_/window_activator.py (click-to-focus)
- platform_/process_scanner._filter_orphans (wt_hwnd assignment)
- platform_/win32_console.py (kept as is, called from here)
- platform_/wt_uia.py (kept as is, called from here)
"""
from __future__ import annotations

import sys
from typing import ClassVar

from claude_island.core.capabilities import Capability, FocusGranularity, _CapabilityProvider, capability
from claude_island.core.models import Session
from claude_island.core.snapshot import SessionGroup, SessionView, _normalize_project_path
from claude_island.platform_.terminals import adapter
from claude_island.platform_.terminals.protocols import TerminalAdapter

# Internal Win32 helpers — import within methods so non-Windows import
# of this module (via __init__.py adapter registry) doesn't trigger
# ImportError. The @adapter decorator skips instantiation on non-win
# platforms anyway.

_WT_CLASS_PREFIX = "CASCADIA_HOSTING_WINDOW_CLASS"
_MAX_ANCESTOR_DEPTH = 10


@adapter("windows-terminal", priority=100, platform="win")
class WindowsTerminalAdapter(_CapabilityProvider):
    """Adapter for claude sessions running inside Windows Terminal."""

    name: ClassVar[str] = ""  # set by @adapter
    _priority: int = 0

    # ── can_handle ──────────────────────────────────────────────────────

    def can_handle(self, session: Session) -> bool:
        """True when the session's process ancestry includes
        WindowsTerminal.exe or a conpty host that traces there.

        Uses the same psutil ancestry walk the legacy WindowActivator
        relied on, but extended: on Windows *any* claude session is
        in a terminal (WT, conhost, or a bundled app). We only claim
        WT sessions — generic_windows adapter claims the rest."""
        import psutil
        try:
            proc = psutil.Process(session.pid)
            ancestors: list[psutil.Process] = []
            for _ in range(_MAX_ANCESTOR_DEPTH):
                p = proc.parent()
                if p is None: break
                ancestors.append(p)
                proc = p
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            return False
        return any(
            a.name().lower() in {"windowsterminal.exe", "wt.exe"}
            for a in ancestors
        )

    # ── group ───────────────────────────────────────────────────────────

    def group(self, views: list[SessionView]) -> list[SessionGroup]:
        """Bucket views by (wt_hwnd, normalized_cwd), drop orphans,
        stamp adapter identity.

        Input is already-resolved SessionViews from the snapshotter —
        we do NOT re-run compose_session_view. Per-view work here:
          1. AttachConsole probe to drop orphans (no console attached).
          2. walk_to_visible_host to compute the wt_hwnd grouping key.
          3. Bucket views by ``(wt_hwnd, normalized_cwd)`` — same WT
             window AND same project = same card. Different cwds in
             the same WT window stay separate (e.g. main repo + a
             different project open in two tabs). The cwd component is
             worktree-normalised so a Claude Code worktree merges with
             its parent repo.
          4. Views whose wt_hwnd can't be resolved (window_handle=None
             pre-PR2) become singleton groups — one card each, NEVER
             merged with anything. This avoids the bug where every
             unresolvable view collapsed into one mega-card just
             because their key all happened to be 0.

        Each emitted SessionGroup renders as one card in the panel."""
        from dataclasses import replace
        from claude_island.platform_ import win32_console, window_activator

        win32gui = None
        try:
            import win32gui as _w32g
            win32gui = _w32g
        except ImportError:
            pass

        # --- orphan filter + per-view wt_hwnd ---
        # Pair each surviving view with the wt_hwnd grouping key. Views
        # whose AttachConsole fails are dropped (no console = orphan).
        # ``wt_hwnd`` is None when we couldn't resolve to a WT window —
        # treated as ungroupable below.
        kept: list[tuple[int | None, SessionView]] = []
        for v in views:
            info = win32_console.get_console_info(v.session.pid)
            if info is None:
                continue
            conpty_hwnd, _title = info
            wt_hwnd: int | None = None
            if win32gui is not None and conpty_hwnd:
                wt_hwnd = window_activator.walk_to_visible_host(
                    conpty_hwnd, win32gui,
                )
            kept.append((wt_hwnd, v))

        # Tripwire: every view filtered → likely a race with our own
        # console state. Keep originals as ungroupable singletons so
        # the user still sees rows (rather than a blank list).
        if not kept:
            kept = [(None, v) for v in views]

        # --- bucket by (wt_hwnd, normalized_cwd) ---
        # Resolved wt_hwnd → ("wt", hwnd, cwd) — same hwnd + same cwd
        # collapse into one card.
        # Unresolved wt_hwnd → ("singleton", pid) — never merges with
        # anything (each unresolvable view gets its own card).
        buckets: dict[tuple, list[SessionView]] = {}
        for wt_hwnd, v in kept:
            if wt_hwnd is None:
                key: tuple = ("singleton", v.pid)
            else:
                key = ("wt", wt_hwnd, _normalize_project_path(v.project_path))
            buckets.setdefault(key, []).append(v)

        # --- stamp identity onto each view, build SessionGroups ---
        result: list[SessionGroup] = []
        for key, batch in buckets.items():
            stamped = [
                replace(
                    v,
                    adapter_id=self.name,
                    focus_granularity=FocusGranularity.TAB,
                    capabilities=type(self).capabilities,
                )
                for v in batch
            ]
            if key[0] == "wt":
                gid = f"wt:{key[1]}:{key[2]}"
            else:
                gid = f"wt:singleton:{key[1]}"
            project_paths = {_normalize_project_path(v.project_path) for v in stamped}
            title = ", ".join(sorted(project_paths)[:2]) if project_paths else None
            result.append(SessionGroup(
                group_id=gid, title_hint=title,
                adapter_id=self.name, views=tuple(stamped),
            ))
        return result

    # ── FOCUS ────────────────────────────────────────────────────────────

    @capability(Capability.FOCUS)
    def focus(self, view: SessionView) -> bool:
        """Bring the WT window to foreground + select the matching tab."""
        return _activate_windows(view.session.pid)


# ---------------------------------------------------------------------------
# Helpers — lifted from the legacy window_activator.py with adapter
# boundaries preserved.
# ---------------------------------------------------------------------------

def _activate_windows(pid: int) -> bool:
    """Resolve console window → SetForegroundWindow → UIA tab select.

    Mirrors legacy WindowActivator._activate_windows, minus the
    sibling-fallback path (that was always UI-layer logic — pass the
    sibling_list from the adapter's group if we ever need it again).
    """
    try:
        import win32con
        import win32gui
        import win32process
    except ImportError:
        print(
            "[claude-island] pywin32 not installed; cannot activate windows. "
            "Run: pip install pywin32",
            file=sys.stderr,
        )
        return False

    resolved = _resolve_console_window(pid, win32gui)
    hwnd: int | None = None
    if resolved is not None:
        hwnd, title = resolved
        from claude_island.platform_ import wt_uia
        if not wt_uia.select_tab_by_title(hwnd, title):
            # Tab selection failed — still go to foreground anyway.
            pass
    else:
        from claude_island.platform_.window_activator import _ancestor_pids, _find_window_for_pids
        candidate_pids = _ancestor_pids(pid)
        if candidate_pids:
            hwnd = _find_window_for_pids(candidate_pids, win32gui, win32process)

    if hwnd is None:
        return False
    from claude_island.platform_.window_activator import _force_foreground
    return _force_foreground(hwnd, win32con, win32gui, win32process)


def _resolve_console_window(pid: int, win32gui) -> tuple[int, str] | None:
    from claude_island.platform_ import win32_console
    from claude_island.platform_.window_activator import walk_to_visible_host
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
