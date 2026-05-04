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
from pathlib import Path
from typing import ClassVar

from claude_island.core.capabilities import Capability, _CapabilityProvider, capability
from claude_island.core.models import project_hash
from claude_island.core.snapshot import SessionView

_TIMEOUT_S = 3.0


class MacOsBackend(_CapabilityProvider):
    """OS-generic capabilities on macOS via shell utilities."""

    name: ClassVar[str] = "macos"

    @capability(Capability.REVEAL_CWD)
    def reveal_cwd(self, view: SessionView) -> bool:
        # `open -R <path>` selects the path in Finder. Returns 0 when
        # successful, non-zero when the path is missing — propagate
        # that as the bool result so the popup can show "❌ Failed".
        try:
            result = subprocess.run(
                ["open", "-R", str(view.project_path)],
                check=False, timeout=_TIMEOUT_S,
            )
            return result.returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            return False

    @capability(Capability.REVEAL_TRANSCRIPT)
    def reveal_transcript(self, view: SessionView) -> bool:
        # `open <file>` launches the user's default app for the
        # extension. Without -R the file is opened (not just selected),
        # which is what "view this transcript" means.
        path = _transcript_path(view)
        if path is None or not path.exists():
            return False
        try:
            result = subprocess.run(
                ["open", str(path)],
                check=False, timeout=_TIMEOUT_S,
            )
            return result.returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            return False

    @capability(Capability.COPY_PATH)
    def copy_path(self, view: SessionView) -> bool:
        # pbcopy reads UTF-8 by default in modern macOS locales.
        # Returns 0 when the clipboard write succeeded.
        try:
            result = subprocess.run(
                ["pbcopy"],
                input=str(view.project_path).encode("utf-8"),
                check=False, timeout=_TIMEOUT_S,
            )
            return result.returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            return False


def _transcript_path(view: SessionView) -> Path | None:
    """``~/.claude/projects/<hash>/<uuid>.jsonl`` for this view, or
    None when the session uuid hasn't been resolved yet."""
    if not view.session_uuid:
        return None
    return Path.home() / ".claude" / "projects" / project_hash(view.project_path) / f"{view.session_uuid}.jsonl"
