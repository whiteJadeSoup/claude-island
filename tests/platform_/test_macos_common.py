"""Tests for the shared macOS terminal-adapter helpers.

The helpers translate a CLI session pid into a UI app pid that
System Events can ``set frontmost``. Tests mock psutil + osascript
at the trust boundary so they pass on any host OS.
"""
from __future__ import annotations

from unittest import mock

import psutil
import pytest

from claude_island.platform_.terminals import _macos_common


@pytest.fixture(autouse=True)
def _reset_cache():
    _macos_common._reset_cache_for_testing()
    yield
    _macos_common._reset_cache_for_testing()


def _proc(pid: int, parent: "mock.Mock | None" = None) -> mock.Mock:
    p = mock.Mock(name=f"proc-{pid}")
    p.pid = pid
    p.parent = lambda: parent
    return p


def _bytes_run(stdout_text: str = "", returncode: int = 0) -> mock.Mock:
    """Build a fake subprocess.run result mirroring what
    capture_output (without text=True) yields: bytes stdout."""
    return mock.Mock(stdout=stdout_text.encode("utf-8"), returncode=returncode)


# ── _query_ui_app_pids ────────────────────────────────────────────────

class TestQueryUiAppPids:
    def test_parses_comma_separated_ids(self):
        with mock.patch(
            "claude_island.platform_.terminals._macos_common.subprocess.run",
            return_value=_bytes_run("123, 456, 789"),
        ):
            assert _macos_common._query_ui_app_pids() == frozenset({123, 456, 789})

    def test_parses_space_separated_ids(self):
        with mock.patch(
            "claude_island.platform_.terminals._macos_common.subprocess.run",
            return_value=_bytes_run("123 456 789"),
        ):
            assert _macos_common._query_ui_app_pids() == frozenset({123, 456, 789})

    def test_empty_when_returncode_nonzero(self):
        with mock.patch(
            "claude_island.platform_.terminals._macos_common.subprocess.run",
            return_value=_bytes_run("123, 456", returncode=1),
        ):
            assert _macos_common._query_ui_app_pids() == frozenset()

    def test_empty_on_oserror(self):
        with mock.patch(
            "claude_island.platform_.terminals._macos_common.subprocess.run",
            side_effect=OSError("not found"),
        ):
            assert _macos_common._query_ui_app_pids() == frozenset()

    def test_empty_on_timeout(self):
        import subprocess
        with mock.patch(
            "claude_island.platform_.terminals._macos_common.subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd=["x"], timeout=3),
        ):
            assert _macos_common._query_ui_app_pids() == frozenset()

    def test_skips_non_numeric_tokens(self):
        with mock.patch(
            "claude_island.platform_.terminals._macos_common.subprocess.run",
            return_value=_bytes_run("123, foo, 456"),
        ):
            assert _macos_common._query_ui_app_pids() == frozenset({123, 456})


# ── _ui_app_pids cache ────────────────────────────────────────────────

class TestUiAppPidsCache:
    def test_second_call_within_ttl_uses_cache(self):
        with mock.patch(
            "claude_island.platform_.terminals._macos_common.subprocess.run",
            return_value=_bytes_run("100"),
        ) as run:
            a = _macos_common._ui_app_pids()
            b = _macos_common._ui_app_pids()
        # Cached → only one osascript call across two _ui_app_pids
        # invocations. Critical for the focus-click hot path.
        assert run.call_count == 1
        assert a == b == frozenset({100})


# ── find_ui_app_ancestor ──────────────────────────────────────────────

class TestFindUiAppAncestor:
    def test_returns_pid_when_pid_itself_is_ui_app(self):
        # Self-match path: pid == iTerm2 directly. Caller hands us the
        # session pid (a CLI claude in practice) but the API should
        # also handle "I'm already the UI app" cleanly.
        ui_pid = 100
        with (
            mock.patch.object(_macos_common, "_ui_app_pids",
                              return_value=frozenset({ui_pid})),
            mock.patch("psutil.Process", return_value=_proc(ui_pid)),
        ):
            assert _macos_common.find_ui_app_ancestor(ui_pid) == ui_pid

    def test_walks_to_parent_ui_app(self):
        # claude (cli) → zsh → iTerm2 (ui)
        iterm = _proc(50)
        zsh = _proc(40, parent=iterm)
        claude = _proc(30, parent=zsh)
        with (
            mock.patch.object(_macos_common, "_ui_app_pids",
                              return_value=frozenset({50})),
            mock.patch("psutil.Process", return_value=claude),
        ):
            assert _macos_common.find_ui_app_ancestor(30) == 50

    def test_returns_none_when_no_ui_ancestor(self):
        # tmux scenario: claude → zsh → tmux server → launchd (none of
        # those are in the UI app set).
        launchd = _proc(1)
        tmux = _proc(20, parent=launchd)
        zsh = _proc(15, parent=tmux)
        claude = _proc(10, parent=zsh)
        # launchd has no parent
        launchd.parent = lambda: None
        with (
            mock.patch.object(_macos_common, "_ui_app_pids",
                              return_value=frozenset({999})),
            mock.patch("psutil.Process", return_value=claude),
        ):
            assert _macos_common.find_ui_app_ancestor(10) is None

    def test_returns_none_when_psutil_no_such_process(self):
        with (
            mock.patch.object(_macos_common, "_ui_app_pids",
                              return_value=frozenset({100})),
            mock.patch("psutil.Process",
                       side_effect=psutil.NoSuchProcess(pid=10)),
        ):
            assert _macos_common.find_ui_app_ancestor(10) is None

    def test_returns_none_when_query_returned_empty(self):
        # If osascript permission is denied (System Events) we get an
        # empty UI pid set. Returning None here is what makes the
        # caller treat "FOCUS not supported" gracefully.
        with mock.patch.object(_macos_common, "_ui_app_pids",
                               return_value=frozenset()):
            assert _macos_common.find_ui_app_ancestor(10) is None

    def test_returns_none_when_chain_exceeds_max_depth(self):
        # Pathological deep chain — never reaches a UI app within the
        # depth cap. The walk must terminate, not run forever.
        leaf = _proc(1)
        leaf.parent = lambda: leaf  # self-cycle
        with (
            mock.patch.object(_macos_common, "_ui_app_pids",
                              return_value=frozenset({999})),
            mock.patch("psutil.Process", return_value=leaf),
        ):
            assert _macos_common.find_ui_app_ancestor(1) is None


# ── frontmost_app ─────────────────────────────────────────────────────

class TestFrontmostApp:
    def test_returns_true_on_returncode_0(self):
        with mock.patch(
            "claude_island.platform_.terminals._macos_common.subprocess.run",
            return_value=_bytes_run(returncode=0),
        ) as run:
            assert _macos_common.frontmost_app(123) is True
            argv = run.call_args[0][0]
            assert argv[0] == "/usr/bin/osascript"
            # Pid must appear inside the AppleScript literal.
            assert "123" in argv[2]

    def test_returns_false_on_returncode_nonzero(self):
        with mock.patch(
            "claude_island.platform_.terminals._macos_common.subprocess.run",
            return_value=_bytes_run(returncode=1),
        ):
            assert _macos_common.frontmost_app(123) is False

    def test_returns_false_on_oserror(self):
        with mock.patch(
            "claude_island.platform_.terminals._macos_common.subprocess.run",
            side_effect=OSError("nope"),
        ):
            assert _macos_common.frontmost_app(123) is False

    def test_returns_false_on_timeout(self):
        import subprocess
        with mock.patch(
            "claude_island.platform_.terminals._macos_common.subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd=["x"], timeout=3),
        ):
            assert _macos_common.frontmost_app(123) is False
