"""Generic fallback adapter for Windows processes not in Windows Terminal.

Handles sessions running under conhost directly, pythonw-hosted
processes, sandboxed shells, etc. Always groups as singletons
(one group per session) with FOCUS granularity=APP.

Registered with priority=0 so any specific adapter (windows-terminal,
future ones) claims sessions first. This adapter catches leftovers
so no session falls through the cracks.
"""
from __future__ import annotations

from dataclasses import replace
from typing import ClassVar

from claude_island.core.capabilities import (
    Capability, FocusGranularity, _CapabilityProvider, capability,
)
from claude_island.core.models import Session
from claude_island.core.snapshot import SessionGroup, SessionView
from claude_island.platform_.terminals import adapter
from claude_island.platform_.terminals.protocols import TerminalAdapter


@adapter("generic-windows", priority=0, platform="win")
class GenericWindowsAdapter(_CapabilityProvider):
    """Last-resort adapter for any Windows process with a CLI ancestor."""

    name: ClassVar[str] = ""  # set by @adapter
    _priority: int = 0

    def can_handle(self, session: Session) -> bool:
        """Always True — the dispatcher chain makes this last in line
        so only unclaimed sessions reach here."""
        return True

    def group(self, views: list[SessionView]) -> list[SessionGroup]:
        """Each view becomes its own singleton group, stamped with the
        generic-windows adapter identity. No re-resolution — the views
        are already fully populated by the snapshotter."""
        groups: list[SessionGroup] = []
        for v in views:
            stamped = replace(
                v,
                adapter_id=self.name,
                focus_granularity=FocusGranularity.APP,
                capabilities=type(self).capabilities,
            )
            groups.append(SessionGroup(
                group_id=f"win:{stamped.pid}",
                title_hint=None,
                adapter_id=self.name,
                views=(stamped,),
            ))
        return groups

    @capability(Capability.FOCUS)
    def focus(self, view: SessionView, *, siblings: list[int] = ()) -> bool:
        """Activate via ancestor-pid EnumWindows walk.

        Won't select a specific tab — only brings the host window to
        the foreground, which is the best we can do for non-WT hosts.

        ``siblings`` is accepted (and ignored) so the dispatcher can
        pass the same kwargs to every adapter regardless of which one
        ends up handling the view. The WT adapter uses siblings for
        inactive-pane tab fallback; non-WT hosts have no analogue."""
        del siblings  # ignored — generic Windows hosts have no tab concept
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
