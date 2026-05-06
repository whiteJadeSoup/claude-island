"""Generic fallback adapter for macOS processes not claimed by a
specific terminal adapter (iTerm2, Kitty, etc. — future files).

Always groups as singletons. FOCUS raises the host app to the front
via a simple osascript call — "frontmost of process whose unix id is X".
LAUNCH spawns Terminal.app via ``osascript "tell application Terminal
to do script ..."`` so RecentsDrawer's Resume works for users who
aren't on iTerm2 (which has its own LAUNCH on iTerm2Adapter).
"""
from __future__ import annotations

import shlex
import subprocess
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import ClassVar

from claude_island.core.capabilities import (
    Capability, FocusGranularity, LauncherSpawnError, SpawnResult,
    _CapabilityProvider, capability,
)
from claude_island.core.models import Session
from claude_island.core.snapshot import SessionGroup, SessionView
from claude_island.platform_.terminals import adapter
from claude_island.platform_.terminals.iterm2 import _escape_applescript_string
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
    def focus(self, view: SessionView, *, siblings: list[int] = ()) -> bool:
        """Raise the host app to front via osascript.

        Pane/tab-level focus requires a specific terminal adapter
        (e.g. iTerm2 with AppleScript tty matching). This generic
        fallback can only guarantee the app is frontmost.

        ``siblings`` is accepted (and ignored) for dispatch-kwargs
        uniformity — see GenericWindowsAdapter.focus for the same
        rationale."""
        del siblings  # ignored — generic mac focus can't disambiguate panes
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

    @capability(Capability.LAUNCH)
    def launch(
        self,
        *,
        cwd: Path,
        command: tuple[str, ...],
        session_uuid: str | None = None,
    ) -> SpawnResult:
        """Spawn Terminal.app and run ``command`` in ``cwd``.

        ``session_uuid`` is accepted for kwargs uniformity with the WT
        adapter's Plan-L title-locking, but ignored — Terminal.app's
        AppleScript dictionary doesn't expose a per-session stable id
        we'd need to write to.

        Used by RecentsDrawer's Resume for macOS users who aren't on
        iTerm2 (iTerm2Adapter has its own LAUNCH). Terminal.app is
        present on every Mac — no install dependency.

        AppleScript path:
          ``tell application "Terminal" to do script "cd ... && claude ..."``

        ``do script`` opens a new window when Terminal isn't already
        the frontmost app, otherwise reuses the front window — Apple's
        default behaviour, the user can then Cmd-T for a new tab if
        they want a fresh window. ``activate`` brings Terminal to the
        front so the new shell is visible.

        Reports ``terminal_pid`` = the osascript pid (NOT Terminal.app's,
        which is a long-lived process). Same pattern as iTerm2Adapter.

        Raises ``LauncherSpawnError`` if osascript itself fails to
        spawn (PATH stripped, user has it disabled). Caller toasts."""
        del session_uuid  # ignored — Terminal.app has no Plan-L equivalent
        cmd_str = "cd " + shlex.quote(str(cwd)) + " && " + " ".join(
            shlex.quote(a) for a in command
        )
        cmd_escaped = _escape_applescript_string(cmd_str)
        script = (
            'tell application "Terminal"\n'
            '  activate\n'
            f'  do script "{cmd_escaped}"\n'
            'end tell\n'
        )
        try:
            proc = subprocess.Popen(
                ["osascript", "-e", script], close_fds=True,
            )
        except (OSError, FileNotFoundError) as e:
            raise LauncherSpawnError(f"osascript spawn failed: {e}") from e
        return SpawnResult(
            terminal_name=self.name,
            terminal_pid=proc.pid,
            started_at=datetime.now(timezone.utc),
        )
