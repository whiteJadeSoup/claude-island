"""Unit tests for TerminalAppAdapter — macOS Terminal.app integration.

Mocks subprocess.run (osascript) and psutil so the suite passes on
any host OS. Mirrors the structure of test_iterm2_adapter.py.
"""
from __future__ import annotations

import subprocess
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

import pytest

from claude_island.core.capabilities import Capability, FocusGranularity
from claude_island.core.models import Session
from claude_island.core.snapshot import SessionView, _degraded_view
from claude_island.platform_.terminals.terminal_app import (
    TerminalAppAdapter,
    _ENUM_SCRIPT,
    _FOCUS_SCRIPT_TEMPLATE,
    _parse_enum_output,
)


# ── Fixtures ──────────────────────────────────────────────────────────

def _session(pid: int = 1234, cwd: str = "/tmp/proj") -> Session:
    return Session(
        pid=pid, project_path=Path(cwd), session_uuid="",
        last_activity=datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc),
    )


def _view(pid: int = 1234, cwd: str = "/tmp/proj") -> SessionView:
    return _degraded_view(_session(pid, cwd))


@pytest.fixture
def adapter() -> TerminalAppAdapter:
    """Fresh adapter — bypass the @adapter registry so tests don't
    depend on running on darwin (the registry only registers on mac)."""
    a = TerminalAppAdapter()
    a.name = "terminal-app"
    a._priority = 50
    return a


def _mock_run(stdout: str = "", returncode: int = 0):
    return mock.Mock(stdout=stdout.encode("utf-8"), returncode=returncode)


# ── _parse_enum_output ────────────────────────────────────────────────

class TestParseEnumOutput:
    def test_empty_string_returns_empty_dict(self):
        assert _parse_enum_output("") == {}

    def test_single_tab(self):
        assert _parse_enum_output("12345|/dev/ttys001\n") == {
            "/dev/ttys001": (12345,)
        }

    def test_multiple_tabs_across_windows(self):
        text = (
            "100|/dev/ttys001\n"
            "100|/dev/ttys002\n"   # second tab in same window
            "200|/dev/ttys003\n"   # different window
        )
        assert _parse_enum_output(text) == {
            "/dev/ttys001": (100,),
            "/dev/ttys002": (100,),
            "/dev/ttys003": (200,),
        }

    def test_skips_blank_lines_and_malformed_rows(self):
        text = (
            "\n"
            "100|/dev/ttys001\n"
            "garbage\n"
            "abc|/dev/ttys002\n"   # window id not int
            "200|/dev/ttys003\n"
            "100|\n"               # empty tty
        )
        assert _parse_enum_output(text) == {
            "/dev/ttys001": (100,),
            "/dev/ttys003": (200,),
        }


# ── can_handle ────────────────────────────────────────────────────────

class TestCanHandle:
    def test_terminal_in_ancestor_chain(self, adapter):
        s = _session(pid=10)
        shell = mock.Mock(); shell.name = lambda: "zsh"
        login = mock.Mock(); login.name = lambda: "login"
        term = mock.Mock(); term.name = lambda: "Terminal"
        leaf = mock.Mock()
        leaf.parent = lambda: shell
        shell.parent = lambda: login
        login.parent = lambda: term
        term.parent = lambda: None
        with mock.patch("psutil.Process", return_value=leaf):
            assert adapter.can_handle(s) is True

    def test_no_terminal_in_chain(self, adapter):
        s = _session(pid=10)
        bash = mock.Mock(); bash.name = lambda: "bash"
        sshd = mock.Mock(); sshd.name = lambda: "sshd"
        leaf = mock.Mock()
        leaf.parent = lambda: bash
        bash.parent = lambda: sshd
        sshd.parent = lambda: None
        with mock.patch("psutil.Process", return_value=leaf):
            assert adapter.can_handle(s) is False

    def test_case_insensitive_match(self, adapter):
        """Process name 'TERMINAL' should still match (defensive)."""
        s = _session(pid=10)
        term = mock.Mock(); term.name = lambda: "TERMINAL"
        term.parent = lambda: None
        leaf = mock.Mock(); leaf.parent = lambda: term
        with mock.patch("psutil.Process", return_value=leaf):
            assert adapter.can_handle(s) is True

    def test_substring_terminal_does_not_match(self, adapter):
        """``terminal-notifier`` or ``my-terminal-helper`` shouldn't
        be claimed — exact match only. Without this, false-positive
        adapter claims would route those clicks to a Terminal that
        doesn't host the session."""
        s = _session(pid=10)
        notifier = mock.Mock(); notifier.name = lambda: "terminal-notifier"
        notifier.parent = lambda: None
        leaf = mock.Mock(); leaf.parent = lambda: notifier
        with mock.patch("psutil.Process", return_value=leaf):
            assert adapter.can_handle(s) is False

    def test_psutil_no_such_process_returns_false(self, adapter):
        import psutil
        with mock.patch("psutil.Process",
                        side_effect=psutil.NoSuchProcess(pid=10)):
            assert adapter.can_handle(_session(10)) is False

    def test_chain_walk_capped_at_max_depth(self, adapter):
        """Pathological infinite parent loop must terminate."""
        loop = mock.Mock()
        loop.name = lambda: "not-terminal"
        loop.parent = lambda: loop
        with mock.patch("psutil.Process", return_value=loop):
            assert adapter.can_handle(_session(10)) is False


# ── group ─────────────────────────────────────────────────────────────

def _proc_with_tty(tty: str | None) -> mock.Mock:
    p = mock.Mock()
    p.terminal = lambda: tty
    return p


class TestGroup:
    def test_two_tabs_in_same_window_get_separate_groups(self, adapter):
        """Terminal.app has no split panes — two ttys = two tabs.
        Even if they're in the same window we keep them in separate
        groups (one card per tab) so the user can navigate each."""
        v1 = _view(pid=10)
        v2 = _view(pid=20)
        ttys = {10: "/dev/ttys001", 20: "/dev/ttys002"}
        enum_out = "100|/dev/ttys001\n100|/dev/ttys002\n"
        with (
            mock.patch("subprocess.run", return_value=_mock_run(enum_out)),
            mock.patch("psutil.Process",
                       side_effect=lambda pid: _proc_with_tty(ttys[pid])),
        ):
            groups = adapter.group([v1, v2])
        assert len(groups) == 2
        assert {g.group_id for g in groups} == {
            "terminal-app:100:/dev/ttys001",
            "terminal-app:100:/dev/ttys002",
        }

    def test_view_tty_not_in_terminal_tree_becomes_singleton(self, adapter):
        v = _view(pid=10)
        ttys = {10: "/dev/ttys999"}  # not in Terminal's tree
        enum_out = "100|/dev/ttys001\n"
        with (
            mock.patch("subprocess.run", return_value=_mock_run(enum_out)),
            mock.patch("psutil.Process",
                       side_effect=lambda pid: _proc_with_tty(ttys[pid])),
        ):
            groups = adapter.group([v])
        assert len(groups) == 1
        assert groups[0].group_id == "terminal-app:singleton:10"

    def test_osascript_failure_falls_back_to_singletons(self, adapter):
        """Terminal.app misbehaving (-1712 timeout, permission denied,
        not running) → enumerate returns None → singleton groups so
        rows still render and FOCUS retries at click time."""
        v1 = _view(pid=10)
        v2 = _view(pid=20)
        with mock.patch("subprocess.run",
                        return_value=_mock_run(returncode=1)):
            groups = adapter.group([v1, v2])
        assert len(groups) == 2
        assert all(g.group_id.startswith("terminal-app:singleton:")
                   for g in groups)

    def test_osascript_timeout_falls_back_to_singletons(self, adapter):
        """The -1712 AppleEvent timeout case verified live: Terminal
        running with no windows hangs every AppleScript indefinitely.
        The 3 s subprocess timeout + singleton fallback is the safety
        net that prevents a hung Terminal from freezing the snapshot
        worker."""
        v = _view(pid=10)
        with mock.patch("subprocess.run",
                        side_effect=subprocess.TimeoutExpired(cmd=["x"], timeout=3)):
            groups = adapter.group([v])
        assert len(groups) == 1
        assert groups[0].group_id.startswith("terminal-app:singleton:")

    def test_psutil_terminal_failure_makes_singleton(self, adapter):
        import psutil
        v1 = _view(pid=10)
        v2 = _view(pid=20)
        enum_out = "100|/dev/ttys001\n100|/dev/ttys002\n"

        def proc_factory(pid):
            if pid == 10:
                raise psutil.NoSuchProcess(pid=pid)
            return _proc_with_tty("/dev/ttys002")

        with (
            mock.patch("subprocess.run", return_value=_mock_run(enum_out)),
            mock.patch("psutil.Process", side_effect=proc_factory),
        ):
            groups = adapter.group([v1, v2])
        ids = {g.group_id for g in groups}
        assert "terminal-app:singleton:10" in ids
        assert "terminal-app:100:/dev/ttys002" in ids

    def test_views_stamped_with_tab_granularity_and_caps(self, adapter):
        """Sessions placed on a known tab get TAB granularity per
        the adapter's docstring (Terminal.app has no panes — each tab
        is one tty / one session) and the adapter's full capability
        set. Matches FocusGranularity.TAB's documented set in
        capabilities.py: 'Windows Terminal, Terminal.app'."""
        v = _view(pid=10)
        ttys = {10: "/dev/ttys001"}
        enum_out = "100|/dev/ttys001\n"
        with (
            mock.patch("subprocess.run", return_value=_mock_run(enum_out)),
            mock.patch("psutil.Process",
                       side_effect=lambda pid: _proc_with_tty(ttys[pid])),
        ):
            groups = adapter.group([v])
        view = groups[0].views[0]
        assert view.adapter_id == "terminal-app"
        assert view.focus_granularity is FocusGranularity.TAB
        assert Capability.FOCUS in view.capabilities


# ── focus ─────────────────────────────────────────────────────────────

class TestFocus:
    def test_focus_with_matching_tty_returns_true(self, adapter):
        v = _view(pid=10)
        with (
            mock.patch("psutil.Process",
                       return_value=_proc_with_tty("/dev/ttys001")),
            mock.patch("subprocess.run",
                       return_value=_mock_run(stdout="ok\n")) as run,
        ):
            assert adapter.focus(v) is True
            argv = run.call_args[0][0]
            assert argv[0] == "osascript"
            assert "/dev/ttys001" in argv[2]

    def test_focus_falls_back_on_tty_miss(self, adapter):
        v = _view(pid=10)
        with (
            mock.patch("psutil.Process",
                       return_value=_proc_with_tty("/dev/ttys999")),
            mock.patch("subprocess.run",
                       return_value=_mock_run(stdout="miss\n")),
            mock.patch(
                "claude_island.platform_.terminals.terminal_app.find_ui_app_ancestor",
                return_value=12345,
            ),
            mock.patch(
                "claude_island.platform_.terminals.terminal_app.frontmost_app",
                return_value=True,
            ) as fa,
        ):
            assert adapter.focus(v) is True
            fa.assert_called_once_with(12345)

    def test_focus_falls_back_when_psutil_terminal_missing(self, adapter):
        v = _view(pid=10)
        with (
            mock.patch("psutil.Process",
                       return_value=_proc_with_tty(None)),
            mock.patch(
                "claude_island.platform_.terminals.terminal_app.find_ui_app_ancestor",
                return_value=12345,
            ),
            mock.patch(
                "claude_island.platform_.terminals.terminal_app.frontmost_app",
                return_value=True,
            ),
        ):
            assert adapter.focus(v) is True

    def test_focus_returns_false_when_miss_and_no_ui_ancestor(self, adapter):
        v = _view(pid=10)
        with (
            mock.patch("psutil.Process",
                       return_value=_proc_with_tty("/dev/ttys999")),
            mock.patch("subprocess.run",
                       return_value=_mock_run(stdout="miss\n")),
            mock.patch(
                "claude_island.platform_.terminals.terminal_app.find_ui_app_ancestor",
                return_value=None,
            ),
        ):
            assert adapter.focus(v) is False

    def test_focus_script_selects_window_and_activates(self, adapter):
        """Pin the AppleScript shape: select tab, then frontmost window,
        then activate. Without the explicit window-frontmost step,
        multi-window Terminal users would lose their target behind
        whatever window Terminal had previously frontmost."""
        v = _view(pid=10)
        with (
            mock.patch("psutil.Process",
                       return_value=_proc_with_tty("/dev/ttys001")),
            mock.patch("subprocess.run",
                       return_value=_mock_run(stdout="ok\n")) as run,
        ):
            adapter.focus(v)
            script = run.call_args[0][0][2]
            assert "set selected of t to true" in script
            assert "set frontmost of w to true" in script
            assert "activate" in script
            i_sel = script.index("set selected of t to true")
            i_w = script.index("set frontmost of w to true")
            i_a = script.index("activate")
            assert i_sel < i_w < i_a

    def test_focus_escapes_special_chars_in_tty(self, adapter):
        """Defence-in-depth: even if a tty path ever contained quotes
        or backslashes, AppleScript injection should be safe."""
        v = _view(pid=10)
        weird_tty = '/dev/tty"hax\\'
        with (
            mock.patch("psutil.Process",
                       return_value=_proc_with_tty(weird_tty)),
            mock.patch("subprocess.run",
                       return_value=_mock_run(stdout="miss\n")) as run,
            mock.patch(
                "claude_island.platform_.terminals.terminal_app.find_ui_app_ancestor",
                return_value=None,
            ),
        ):
            adapter.focus(v)
            script = run.call_args_list[0][0][0][2]
            assert '\\"' in script
            assert "\\\\" in script

    def test_focus_accepts_and_ignores_siblings_kwarg(self, adapter):
        v = _view(pid=10)
        with (
            mock.patch("psutil.Process",
                       return_value=_proc_with_tty("/dev/ttys001")),
            mock.patch("subprocess.run",
                       return_value=_mock_run(stdout="ok\n")),
        ):
            assert adapter.focus(v, siblings=[100, 200]) is True


# ── capability surface ────────────────────────────────────────────────

class TestCapabilities:
    def test_advertises_focus(self):
        assert Capability.FOCUS in TerminalAppAdapter.capabilities

    def test_does_not_advertise_launch(self):
        """LAUNCH stays on generic_mac (which spawns Terminal.app
        anyway). Re-implementing here would be ceremony. Keeping LAUNCH
        absence pinned so a future PR doesn't accidentally duplicate
        the path on both adapters."""
        assert Capability.LAUNCH not in TerminalAppAdapter.capabilities
