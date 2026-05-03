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
from claude_island.core.models import Session, project_hash
from claude_island.core.snapshot import SessionGroup, SessionView, compose_session_view, _degraded_view
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

    def group(self, sessions: list[Session]) -> list[SessionGroup]:
        """Filter orphans, label with wt_hwnd, group by wt_hwnd.

        Each group renders as one sesson card in the panel — every
        session in the same WT window collaps under that card."""
        from claude_island.platform_ import win32_console, window_activator

        win32gui = None
        try:
            import win32gui as _w32g
            win32gui = _w32g
        except ImportError:
            pass

        # --- orphan filter + wt_hwnd labelling ---
        labelled: list[Session] = []
        for s in sessions:
            info = win32_console.get_console_info(s.pid)
            if info is None:
                # AttachConsole failed → no console → orphan → drop.
                continue
            conpty_hwnd, _title = info
            wt_hwnd: int | None = None
            if win32gui is not None and conpty_hwnd:
                wt_hwnd = window_activator.walk_to_visible_host(
                    conpty_hwnd, win32gui,
                )
            from dataclasses import replace
            labelled.append(replace(s))

        # Tripwire: empty after filter → return originals unchanged.
        if not labelled:
            # All filtered — likely a race with our own console state.
            # Fallback: keep the sessions but use a nil wt_hwnd so the
            # panel still shows them as ungrouped rows.
            labelled = sessions

        # --- group by wt_hwnd ---
        groups: dict[int, list[Session]] = {}
        for s in labelled:
            key = getattr(s, "window_handle", None) or 0
            groups.setdefault(key, []).append(s)

        result: list[SessionGroup] = []
        for wt_hwnd, batch in groups.items():
            views: list[SessionView] = []
            for s in batch:
                v = compose_session_view(
                    s, state_reader=_no_state, metadata_provider=_no_meta,
                    usage_registry=_no_usage, names_store=_no_names,
                )
                views.append(replace(
                    v,
                    adapter_id=self.name,
                    focus_granularity=FocusGranularity.TAB,
                    capabilities=type(self).capabilities,
                ))
            gid = f"wt:{wt_hwnd}" if wt_hwnd else f"wt:orphan:{id(batch)}"
            project_paths = {_normalize_project_path(v.project_path) for v in views}
            title = ", ".join(sorted(project_paths)[:2]) if project_paths else None
            result.append(SessionGroup(
                group_id=gid, title_hint=title,
                adapter_id=self.name, views=tuple(views),
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


# ---- Null sources for compose_session_view when we only need the group ----
# Terminal adapters call compose_session_view during group() to produce
# views. The real resolution happens inside Snapshotter._build_snapshot
# (the sessions field). Adapters just stamp identity on a view shell.
# Passing no-op sources here keeps adapter.group() self-contained.

class _NullStateReader:
    def read_session_state(self, pid): return None

class _NullMetaProvider:
    def get_session_metadata(self, uuid): return None

class _NullUsageRegistry:
    def get_session_summary(self, uuid): return (0.0, 0, 0)
    def get_latest_model(self, uuid): return None
    def get_totals(self, period): ...

class _NullNamesStore:
    def get_session_name(self, uuid): return None

_no_state = _NullStateReader()
_no_meta = _NullMetaProvider()
_no_usage = _NullUsageRegistry()
_no_names = _NullNamesStore()
