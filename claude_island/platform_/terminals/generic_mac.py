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
import sys
from dataclasses import replace
from typing import ClassVar

from claude_island.core.capabilities import Capability, FocusGranularity, _CapabilityProvider, capability
from claude_island.core.models import Session
from claude_island.core.snapshot import SessionGroup, SessionView, compose_session_view
from claude_island.platform_.terminals import adapter
from claude_island.platform_.terminals.protocols import TerminalAdapter
from claude_island.platform_.terminals.windows_terminal import (
    _no_state, _no_meta, _no_usage, _no_names,
)

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
                group_id=f"mac:{s.pid}",
                title_hint=None,
                adapter_id=self.name,
                views=(v,),
            ))
        return groups

    @capability(Capability.FOCUS)
    def focus(self, view: SessionView) -> bool:
        """Raise the host app to front via osascript.

        Pane/tab-level focus requires a specific terminal adapter
        (e.g. iTerm2 with AppleScript tty matching). This generic
        fallback can only garanee the app is frontmost."""
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
