"""Tests for GenericMacAdapter — focus + LAUNCH on macOS.

Mocks subprocess so the suite doesn't actually spawn osascript /
Terminal.app. Runs cross-platform (the adapter only registers on
darwin, but the class is importable and callable everywhere)."""
from __future__ import annotations

from pathlib import PurePosixPath
from unittest import mock

import pytest

from claude_island.core.capabilities import (
    Capability, LauncherSpawnError, SpawnResult,
)
from claude_island.platform_.terminals.generic_mac import GenericMacAdapter


class TestGenericMacLaunch:
    """LAUNCH lets RecentsDrawer's Resume work for macOS users who
    aren't on iTerm2 (iTerm2Adapter has its own LAUNCH). Terminal.app
    is on every Mac, so this fallback covers all macOS users."""

    def test_launch_advertised_in_capabilities(self):
        assert Capability.LAUNCH in GenericMacAdapter.capabilities

    def test_launch_constructs_terminal_app_applescript(self):
        adapter = GenericMacAdapter()
        cwd = PurePosixPath("/Users/me/proj with space")
        with mock.patch(
            "claude_island.platform_.terminals.generic_mac.subprocess.Popen",
        ) as mock_popen:
            mock_popen.return_value.pid = 5151
            result = adapter.launch(
                cwd=cwd,
                command=("claude", "--resume", "u1", "--permission-mode", "plan"),
            )

        assert isinstance(result, SpawnResult)
        assert result.terminal_pid == 5151
        assert result.terminal_name == adapter.name

        argv = mock_popen.call_args[0][0]
        assert argv[0] == "osascript"
        assert argv[1] == "-e"
        script = argv[2]
        # Pin the AppleScript shape so a refactor doesn't accidentally
        # switch which terminal we're targeting.
        assert 'tell application "Terminal"' in script
        assert "do script" in script
        assert "activate" in script  # bring Terminal to front
        # Spaces in cwd must be shlex-quoted so the cd doesn't split.
        assert "'/Users/me/proj with space'" in script
        # Command args appear in the script verbatim.
        assert "--permission-mode" in script
        assert "plan" in script

    def test_launch_escapes_double_quotes_in_command(self):
        """AppleScript double-quoted strings need ``"`` escaped to
        ``\\"`` — without this, a cwd or arg containing a double
        quote terminates the literal early and the shell gets garbage.
        Pin the escape so a refactor doesn't drop it."""
        adapter = GenericMacAdapter()
        with mock.patch(
            "claude_island.platform_.terminals.generic_mac.subprocess.Popen",
        ) as mock_popen:
            mock_popen.return_value.pid = 1
            adapter.launch(
                cwd=PurePosixPath('/Users/me/has"quote'),
                command=("claude",),
            )
        script = mock_popen.call_args[0][0][2]
        # The literal " must appear escaped, never bare inside the
        # do script "..." literal.
        assert '\\"' in script

    def test_launch_wraps_oserror_as_launcher_spawn_error(self):
        adapter = GenericMacAdapter()
        with mock.patch(
            "claude_island.platform_.terminals.generic_mac.subprocess.Popen",
            side_effect=FileNotFoundError("osascript missing"),
        ):
            with pytest.raises(LauncherSpawnError, match="osascript missing"):
                adapter.launch(
                    cwd=PurePosixPath("/x"), command=("claude",),
                )
