"""macOS implementation of OsBackend — REVEAL_CWD via ``open -R``,
COPY_PATH via ``pbcopy``.

Both delegate to standard macOS CLI tools so the implementation is a
1-line subprocess call. No osascript, no AppleEvents, no permission
prompts (these tools are unconditionally available to any user-space
process).

Failure modes:
- subprocess.run timeout / non-zero exit → caught, return False.
  The dispatcher logs and the UI sees an action that didn't happen,
  but no crash and no propagating exception.
- ``open -R`` on a non-existent path: macOS itself surfaces a
  dialog box; we still return True because the call dispatched.
  (Matches today's _open_in_explorer behaviour in expanded_window.py.)
"""
from __future__ import annotations

import subprocess
from typing import ClassVar

from claude_island.core.capabilities import Capability, _CapabilityProvider, capability
from claude_island.core.snapshot import SessionView

_TIMEOUT_S = 3.0


class MacOsBackend(_CapabilityProvider):
    """OS-generic capabilities on macOS via shell utilities."""

    name: ClassVar[str] = "macos"

    @capability(Capability.REVEAL_CWD)
    def reveal_cwd(self, view: SessionView) -> bool:
        try:
            subprocess.run(
                ["open", "-R", str(view.project_path)],
                check=False, timeout=_TIMEOUT_S,
            )
            return True
        except (OSError, subprocess.TimeoutExpired):
            return False

    @capability(Capability.COPY_PATH)
    def copy_path(self, view: SessionView) -> bool:
        try:
            subprocess.run(
                ["pbcopy"],
                input=str(view.project_path).encode("utf-8"),
                check=False, timeout=_TIMEOUT_S,
            )
            return True
        except (OSError, subprocess.TimeoutExpired):
            return False
