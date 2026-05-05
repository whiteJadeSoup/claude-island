"""Windows implementation of OsBackend.

REVEAL_CWD via ``explorer /select,<path>`` so the folder is selected
in its parent (matches what users expect from "reveal in Explorer"),
not opened *into*; the latter is what ``os.startfile`` does.

COPY_PATH via the built-in ``clip.exe`` — present on every Windows
since Vista. No pywin32 dependency required.

Failure modes match macos.py: subprocess errors are caught and turned
into False; UI sees a no-op rather than a crash.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import ClassVar

from claude_island.core.capabilities import Capability, _CapabilityProvider, capability
from claude_island.core.models import project_hash
from claude_island.core.snapshot import SessionView

_TIMEOUT_S = 3.0


class WindowsOsBackend(_CapabilityProvider):
    """OS-generic capabilities on Windows via built-in shell utilities."""

    name: ClassVar[str] = "windows"

    @capability(Capability.REVEAL_CWD)
    def reveal_cwd(self, view: SessionView) -> bool:
        # /select, takes the path and selects it within its parent.
        # No space after the comma — explorer.exe's CLI parser is
        # fussy about it. The trailing slash on the path is OK either
        # way; we just stringify whatever Path object we got.
        # Note: explorer.exe is unusual — it returns 1 even on success
        # for a `/select,<path>` call. Treat any completed run as OK
        # (returncode is unreliable here); only catch the OS-level
        # failures that mean we couldn't even spawn the process.
        try:
            subprocess.run(
                ["explorer", f"/select,{view.project_path}"],
                check=False, timeout=_TIMEOUT_S,
            )
            return True
        except (OSError, subprocess.TimeoutExpired):
            return False

    @capability(Capability.REVEAL_TRANSCRIPT)
    def reveal_transcript(self, view: SessionView) -> bool:
        # Open the session's .jsonl with whatever Windows associates
        # to the extension (typically Notepad / VS Code / a custom
        # JSON viewer). os.startfile is the Win32-native launcher —
        # it spawns ShellExecute under the hood, so file associations
        # work the same as a double-click in Explorer.
        # Returns False if the uuid isn't resolved yet (transcript not
        # written) or if the file genuinely doesn't exist on disk;
        # the popup surfaces the failure as a "❌ Failed" toast.
        path = _transcript_path(view)
        if path is None or not path.exists():
            return False
        try:
            os.startfile(str(path))  # type: ignore[attr-defined]  # Windows-only
            return True
        except OSError:
            return False

    @capability(Capability.COPY_PATH)
    def copy_path(self, view: SessionView) -> bool:
        # clip.exe relies on a UTF-16-LE BOM (\xff\xfe) to recognise
        # Unicode input. Without the BOM it falls back to the system
        # OEM codepage, which mojibakes any CJK / non-ASCII path.
        # Return value follows clip's exit code; 0 means the clipboard
        # was set, anything else means we couldn't reach the clipboard.
        try:
            result = subprocess.run(
                ["clip"],
                input=b"\xff\xfe" + str(view.project_path).encode("utf-16-le"),
                check=False, timeout=_TIMEOUT_S,
            )
            return result.returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            return False


def _transcript_path(view: SessionView) -> Path | None:
    """``~/.claude/projects/<hash>/<uuid>.jsonl`` for this view, or
    None when the session uuid hasn't been resolved yet (a fresh
    process scan that hasn't merged transcript info)."""
    if not view.session_uuid:
        return None
    return Path.home() / ".claude" / "projects" / project_hash(view.project_path) / f"{view.session_uuid}.jsonl"
