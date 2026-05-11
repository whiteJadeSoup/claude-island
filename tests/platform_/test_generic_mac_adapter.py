"""Tests for GenericMacAdapter — focus + LAUNCH on macOS.

Mocks subprocess so the suite doesn't actually spawn osascript /
Terminal.app. Runs cross-platform (the adapter only registers on
darwin, but the class is importable and callable everywhere)."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from unittest import mock

import psutil
import pytest

from claude_island.core.capabilities import (
    Capability, FocusGranularity, LauncherSpawnError, SpawnResult,
)
from claude_island.core.models import Session
from claude_island.core.snapshot import SessionView, _degraded_view
from claude_island.platform_.terminals import _macos_common
from claude_island.platform_.terminals.generic_mac import GenericMacAdapter


def _session(pid: int = 1234, cwd: str = "/tmp/proj") -> Session:
    return Session(
        pid=pid, project_path=Path(cwd), session_uuid="",
        last_activity=datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc),
    )


def _view(pid: int = 1234, cwd: str = "/tmp/proj") -> SessionView:
    return _degraded_view(_session(pid, cwd))


class TestGenericMacFocus:
    """generic_mac.focus must walk to a UI ancestor and frontmost
    that. Targeting the claude CLI pid directly errors -1719 in
    System Events; the helper layer protects us from that."""

    def test_focus_uses_focus_host_app_helper(self):
        """generic_mac delegates fully to ``focus_host_app(pid)`` — the
        shared helper centralises the find-ancestor → frontmost chain
        so all macOS adapters share one fix path."""
        adapter = GenericMacAdapter()
        v = _view(pid=10)
        with mock.patch(
            "claude_island.platform_.terminals.generic_mac.focus_host_app",
            return_value=True,
        ) as fha:
            assert adapter.focus(v) is True
            fha.assert_called_once_with(10)

    def test_focus_returns_false_when_helper_fails(self):
        """tmux/screen scenario: helper returns False → focus False."""
        adapter = GenericMacAdapter()
        v = _view(pid=10)
        with mock.patch(
            "claude_island.platform_.terminals.generic_mac.focus_host_app",
            return_value=False,
        ):
            assert adapter.focus(v) is False

    def test_focus_accepts_and_ignores_siblings_kwarg(self):
        adapter = GenericMacAdapter()
        v = _view(pid=10)
        with mock.patch(
            "claude_island.platform_.terminals.generic_mac.focus_host_app",
            return_value=True,
        ):
            assert adapter.focus(v, siblings=[_view(pid=100), _view(pid=200)]) is True


class TestGenericMacGroup:
    """group() must be honest about FOCUS support per view: drop the
    capability when the view's process tree can't reach a UI app."""

    def test_singleton_per_view(self):
        adapter = GenericMacAdapter()
        adapter.name = "generic-mac"
        v1 = _view(pid=10)
        v2 = _view(pid=20)
        with mock.patch(
            "claude_island.platform_.terminals.generic_mac.find_ui_app_ancestor",
            return_value=5050,
        ):
            groups = adapter.group([v1, v2])
        assert len(groups) == 2
        assert {g.group_id for g in groups} == {"mac:10", "mac:20"}
        for g in groups:
            assert len(g.views) == 1

    def test_view_with_ui_ancestor_keeps_focus(self):
        adapter = GenericMacAdapter()
        adapter.name = "generic-mac"
        v = _view(pid=10)
        with mock.patch(
            "claude_island.platform_.terminals.generic_mac.find_ui_app_ancestor",
            return_value=5050,
        ):
            groups = adapter.group([v])
        view = groups[0].views[0]
        assert Capability.FOCUS in view.capabilities
        assert view.adapter_id == "generic-mac"
        assert view.focus_granularity is FocusGranularity.APP

    def test_view_without_ui_ancestor_drops_focus(self):
        """Honest signal: tmux/screen sessions whose chain can't reach
        a UI app get FOCUS removed. The UI then disables the click
        affordance instead of letting the user click into the void."""
        adapter = GenericMacAdapter()
        adapter.name = "generic-mac"
        v = _view(pid=10)
        with mock.patch(
            "claude_island.platform_.terminals.generic_mac.find_ui_app_ancestor",
            return_value=None,
        ):
            groups = adapter.group([v])
        view = groups[0].views[0]
        assert Capability.FOCUS not in view.capabilities
        # Other capabilities the adapter advertises (e.g. LAUNCH for
        # the class) should still be present where applicable —
        # dropping FOCUS shouldn't cascade.
        assert view.adapter_id == "generic-mac"

    def test_per_view_decision_when_some_have_ui_some_dont(self):
        """Two views in one group() call: one inside iTerm2 (UI
        ancestor), one inside tmux (no ancestor). Each gets its own
        capability set."""
        adapter = GenericMacAdapter()
        adapter.name = "generic-mac"
        v_iterm = _view(pid=10)
        v_tmux = _view(pid=20)

        def _by_pid(pid):
            return 5050 if pid == 10 else None

        with mock.patch(
            "claude_island.platform_.terminals.generic_mac.find_ui_app_ancestor",
            side_effect=_by_pid,
        ):
            groups = adapter.group([v_iterm, v_tmux])
        by_pid = {g.views[0].pid: g.views[0] for g in groups}
        assert Capability.FOCUS in by_pid[10].capabilities
        assert Capability.FOCUS not in by_pid[20].capabilities

    def test_stale_pid_keeps_focus(self):
        """Race: process exited between ProcessScanner tick and snapshot
        build. find_ui_app_ancestor returns PROCESS_GONE. group() must
        keep FOCUS — the row is about to disappear anyway and going dark
        with a "click unavailable" tooltip in that brief window is
        confusing. Previously the adapter treated PROCESS_GONE the same
        as the tmux/screen case and stripped FOCUS incorrectly."""
        adapter = GenericMacAdapter()
        adapter.name = "generic-mac"
        v = _view(pid=9999)
        with mock.patch(
            "claude_island.platform_.terminals.generic_mac.find_ui_app_ancestor",
            return_value=_macos_common.PROCESS_GONE,
        ):
            groups = adapter.group([v])
        view = groups[0].views[0]
        assert Capability.FOCUS in view.capabilities


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
