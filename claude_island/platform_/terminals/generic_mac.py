"""Generic fallback adapter for macOS processes not claimed by a
specific terminal adapter (iTerm2, Kitty, etc. — future files).

Always groups as singletons. FOCUS raises the host app to the front
via a simple osascript call — "frontmost of process whose unix id is X".

This is the only adapter shipped for macOS in PR1 (iterm2/kitty/
terminal-app adapters come in follow-up PRs when mac hardware testing
is available).
"""
from __future__ import annotations

import subprocess
from dataclasses import replace
from typing import ClassVar

from claude_island.core.capabilities import (
    Capability, FocusGranularity, _CapabilityProvider, capability,
)
from claude_island.core.models import Session
from claude_island.core.snapshot import SessionGroup, SessionView
from claude_island.platform_.terminals import adapter
from claude_island.platform_.terminals.protocols import TerminalAdapter

_TIMEOUT_S = 3.0


@adapter("generic-mac", priority=0, platform="mac")
class GenericMacAdapter(_CapabilityProvider):
    """Fallback adapter for any macOS process.

    Registered with priority=0 so future specific adapters (iTerm2=100,
    Kitty=80, TerminalApp=20) claim sessions first."""

    name: ClassVar[str] = ""  # set by @adapter
    _priority: int = 0

    def can_handle(self, session: Session) -> bool:
        return True

    def group(self, views: list[SessionView]) -> list[SessionGroup]:
        """Each view becomes its own singleton group, stamped with the
        generic-mac adapter identity. No re-resolution — the views are
        already fully populated by the snapshotter."""
        groups: list[SessionGroup] = []
        for v in views:
            stamped = replace(
                v,
                adapter_id=self.name,
                focus_granularity=FocusGranularity.APP,
                capabilities=type(self).capabilities,
            )
            groups.append(SessionGroup(
                group_id=f"mac:{stamped.pid}",
                title_hint=None,
                adapter_id=self.name,
                views=(stamped,),
            ))
        return groups

    @capability(Capability.FOCUS)
    def focus(self, view: SessionView) -> bool:
        """Raise the host app to front via osascript.

        Pane/tab-level focus requires a specific terminal adapter
        (e.g. iTerm2 with AppleScript tty matching). This generic
        fallback can only guarantee the app is frontmost."""
        script = (
            'tell application "System Events" to set frontmost of '
            f'(first process whose unix id is {view.session.pid}) to true'
        )
        try:
            result = subprocess.run(
                ["osascript", "-e", script],
                capture_output=True, timeout=_TIMEOUT_S,
            )
            return result.returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            return False
