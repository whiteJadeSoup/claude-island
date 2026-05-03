"""Generic fallback adapter for Windows processes not in Windows Terminal.

Handles sessions running under conhost directly, pythonw-hosted
processes, sandboxed shells, etc. Always groups as singletons
(one group per session) with FOCUS granularity=APP.

Registered with priority=0 so any specific adapter (windows-terminal,
future ones) claims sessions first. This adapter catches leftovers
so no session falls through the cracks.
"""
from __future__ import annotations

import sys
from dataclasses import replace
from typing import ClassVar

from claude_island.core.capabilities import Capability, FocusGranularity, _CapabilityProvider, capability
from claude_island.core.models import Session
from claude_island.core.snapshot import SessionGroup, SessionView, compose_session_view, _normalize_project_path
from claude_island.platform_.terminals import adapter
from claude_island.platform_.terminals.protocols import TerminalAdapter

# Reuse the null deps from windows_terminal — generic adapters don't
# need real compose_session_view sources because their emitted views
# are shells. The Snapshotter's own composition path fills in the
# actual fields.
from claude_island.platform_.terminals.windows_terminal import (
    _no_state, _no_meta, _no_usage, _no_names,
)


@adapter("generic-windows", priority=0, platform="win")
class GenericWindowsAdapter(_CapabilityProvider):
    """Last-resort adapter for any Windows process with a CLI ancestor."""

    name: ClassVar[str] = ""  # set by @adapter
    _priority: int = 0

    def can_handle(self, session: Session) -> bool:
        """Always True — the dispatcher chain makes this last in line
        so only unclaimed sessions reach here."""
        return True

    def group(self, sessions: list[Session]) -> list[SessionGroup]:
        groups: list[SessionGroup] = []
        for s in sessions:
            v = compose_session_view(
                s, state_reader=_no_state, metadata_provider=_no_meta,
                usage_registry=_no_usage, names_store=_no_names,
            )
            v = replace(
                v,
                adapter_id=self.name,
                focus_granularity=FocusGranularity.APP,
                capabilities=type(self).capabilities,
            )
            groups.append(SessionGroup(
                group_id=f"win:{s.pid}",
                title_hint=None,
                adapter_id=self.name,
                views=(v,),
            ))
        return groups

    @capability(Capability.FOCUS)
    def focus(self, view: SessionView) -> bool:
        """Activate via ancestor-pid EnumWindows walk.

        Same logic as the legacy WindowActivator._activate_windows
        fallback. Won't select a specific tab, but will bring the
        host window to the foreground — which is the best we can do
        for non-WT hosts."""
        try:
            import win32con
            import win32gui
            import win32process
        except ImportError:
            return False
        from claude_island.platform_.window_activator import (
            _ancestor_pids, _find_window_for_pids, _force_foreground,
        )
        pids = _ancestor_pids(view.session.pid)
        if not pids:
            return False
        hwnd = _find_window_for_pids(pids, win32gui, win32process)
        if hwnd is None:
            return False
        return _force_foreground(hwnd, win32con, win32gui, win32process)
