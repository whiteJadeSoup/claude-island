"""Unit tests for ITerm2Adapter.

Strategy: mock subprocess.run (osascript) and psutil.Process so the
tests run on any OS — no actual iTerm2 or psutil session enumeration
needed. The mocks are at the trust boundary the adapter actually
talks across.
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
from claude_island.platform_.terminals.iterm2 import (
    ITerm2Adapter,
    _ENUM_SCRIPT,
    _FOCUS_SCRIPT_TEMPLATE,
    _escape_applescript_string,
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
def adapter() -> ITerm2Adapter:
    """Fresh adapter instance — bypass the @adapter registry so tests
    don't depend on the platform filter (the registry only registers
    on macOS, but tests run on Windows too)."""
    a = ITerm2Adapter()
    a.name = "iterm2"
    a._priority = 100
    return a


def _mock_run(stdout: str = "", returncode: int = 0):
    """Build a Mock subprocess result."""
    return mock.Mock(stdout=stdout.encode("utf-8"), returncode=returncode)


# ── _parse_enum_output ────────────────────────────────────────────────

class TestParseEnumOutput:
    def test_empty_string_returns_empty_dict(self):
        assert _parse_enum_output("") == {}

    def test_single_pane(self):
        text = "12345|1|/dev/ttys001\n"
        assert _parse_enum_output(text) == {"/dev/ttys001": (12345, 1)}

    def test_multiple_panes_across_windows(self):
        text = (
            "100|1|/dev/ttys001\n"
            "100|1|/dev/ttys002\n"   # same window+tab, two panes
            "100|2|/dev/ttys003\n"   # same window, second tab
            "200|1|/dev/ttys004\n"   # different window
        )
        result = _parse_enum_output(text)
        assert result == {
            "/dev/ttys001": (100, 1),
            "/dev/ttys002": (100, 1),
            "/dev/ttys003": (100, 2),
            "/dev/ttys004": (200, 1),
        }

    def test_skips_blank_lines(self):
        text = "\n100|1|/dev/ttys001\n\n  \n"
        assert _parse_enum_output(text) == {"/dev/ttys001": (100, 1)}

    def test_skips_malformed_rows(self):
        text = (
            "100|1|/dev/ttys001\n"
            "garbage row\n"                 # too few fields
            "100|abc|/dev/ttys002\n"        # tab not int
            "200|1|/dev/ttys003\n"
        )
        assert _parse_enum_output(text) == {
            "/dev/ttys001": (100, 1),
            "/dev/ttys003": (200, 1),
        }

    def test_skips_rows_with_empty_tty(self):
        text = "100|1|\n200|1|/dev/ttys001\n"
        assert _parse_enum_output(text) == {"/dev/ttys001": (200, 1)}


# ── _escape_applescript_string ────────────────────────────────────────

class TestEscapeAppleScriptString:
    def test_plain_path_unchanged(self):
        assert _escape_applescript_string("/dev/ttys001") == "/dev/ttys001"

    def test_quote_escaped(self):
        assert _escape_applescript_string('a"b') == 'a\\"b'

    def test_backslash_escaped(self):
        assert _escape_applescript_string("a\\b") == "a\\\\b"

    def test_both(self):
        assert _escape_applescript_string('a"\\b') == 'a\\"\\\\b'


# ── can_handle ────────────────────────────────────────────────────────

class TestCanHandle:
    def test_iterm2_in_ancestor_chain(self, adapter):
        """iTerm2 in process ancestry → can_handle returns True."""
        s = _session(pid=1234)
        # Build a fake parent chain: shell → login → iTerm2
        shell = mock.Mock()
        shell.name = lambda: "zsh"
        login = mock.Mock()
        login.name = lambda: "login"
        iterm = mock.Mock()
        iterm.name = lambda: "iTerm2"
        # parent() chain: leaf → shell → login → iterm → None
        leaf = mock.Mock()
        leaf.parent = lambda: shell
        shell.parent = lambda: login
        login.parent = lambda: iterm
        iterm.parent = lambda: None

        with mock.patch("psutil.Process", return_value=leaf):
            assert adapter.can_handle(s) is True

    def test_no_iterm2_in_chain(self, adapter):
        """sshd → bash → claude — no iTerm2 → can_handle False."""
        s = _session(pid=1234)
        bash = mock.Mock(); bash.name = lambda: "bash"
        sshd = mock.Mock(); sshd.name = lambda: "sshd"
        leaf = mock.Mock()
        leaf.parent = lambda: bash
        bash.parent = lambda: sshd
        sshd.parent = lambda: None

        with mock.patch("psutil.Process", return_value=leaf):
            assert adapter.can_handle(s) is False

    def test_case_insensitive_match(self, adapter):
        """Process name 'iterm' lowercase should match too."""
        s = _session(pid=1234)
        iterm = mock.Mock(); iterm.name = lambda: "iterm"
        iterm.parent = lambda: None
        leaf = mock.Mock(); leaf.parent = lambda: iterm

        with mock.patch("psutil.Process", return_value=leaf):
            assert adapter.can_handle(s) is True

    def test_psutil_no_such_process_returns_false(self, adapter):
        import psutil
        with mock.patch("psutil.Process", side_effect=psutil.NoSuchProcess(pid=1234)):
            assert adapter.can_handle(_session(1234)) is False

    def test_chain_walk_capped_at_max_depth(self, adapter):
        """A pathological infinite parent loop must terminate within
        _MAX_ANCESTOR_DEPTH iterations and return False."""
        from claude_island.platform_.terminals.iterm2 import _MAX_ANCESTOR_DEPTH
        loop_proc = mock.Mock()
        loop_proc.name = lambda: "not-iterm"
        loop_proc.parent = lambda: loop_proc  # self-cycle
        with mock.patch("psutil.Process", return_value=loop_proc):
            # Should return False without hanging (capped at depth 10).
            assert adapter.can_handle(_session(1234)) is False


# ── group ─────────────────────────────────────────────────────────────

def _proc_with_tty(tty: str | None) -> mock.Mock:
    p = mock.Mock()
    p.terminal = lambda: tty
    return p


class TestGroup:
    def test_two_panes_same_tab_share_group(self, adapter):
        v1 = _view(pid=10, cwd="/proj")
        v2 = _view(pid=20, cwd="/proj")

        # tty per pid
        ttys = {10: "/dev/ttys001", 20: "/dev/ttys002"}
        # Both ttys in same iTerm2 (window 100, tab 1)
        enum_out = "100|1|/dev/ttys001\n100|1|/dev/ttys002\n"

        with (
            mock.patch("subprocess.run", return_value=_mock_run(enum_out)),
            mock.patch("psutil.Process",
                       side_effect=lambda pid: _proc_with_tty(ttys[pid])),
        ):
            groups = adapter.group([v1, v2])

        assert len(groups) == 1
        g = groups[0]
        assert g.group_id == "iterm2:100:1"
        assert {v.pid for v in g.views} == {10, 20}
        # Stamp checks
        for v in g.views:
            assert v.adapter_id == "iterm2"
            assert v.focus_granularity == FocusGranularity.PANE
            assert Capability.FOCUS in v.capabilities

    def test_different_tabs_split_into_groups(self, adapter):
        v1 = _view(pid=10)
        v2 = _view(pid=20)

        ttys = {10: "/dev/ttys001", 20: "/dev/ttys002"}
        # Same window, different tabs
        enum_out = "100|1|/dev/ttys001\n100|2|/dev/ttys002\n"

        with (
            mock.patch("subprocess.run", return_value=_mock_run(enum_out)),
            mock.patch("psutil.Process",
                       side_effect=lambda pid: _proc_with_tty(ttys[pid])),
        ):
            groups = adapter.group([v1, v2])

        assert len(groups) == 2
        assert {g.group_id for g in groups} == {"iterm2:100:1", "iterm2:100:2"}

    def test_view_tty_not_in_iterm_tree_becomes_singleton(self, adapter):
        """A view whose tty isn't enumerated by iTerm2 (e.g. process
        was spawned by iTerm2 then reparented away, or tmux session)
        gets its own singleton group instead of being dropped."""
        v = _view(pid=10)
        ttys = {10: "/dev/ttys999"}   # not in iTerm2's tree
        enum_out = "100|1|/dev/ttys001\n"

        with (
            mock.patch("subprocess.run", return_value=_mock_run(enum_out)),
            mock.patch("psutil.Process",
                       side_effect=lambda pid: _proc_with_tty(ttys[pid])),
        ):
            groups = adapter.group([v])

        assert len(groups) == 1
        assert groups[0].group_id == "iterm2:singleton:10"

    def test_osascript_failure_falls_back_to_singletons(self, adapter):
        """If iTerm2 quit mid-tick or AppleScript permission was
        revoked, enumerate returns None and we render singletons so
        rows don't disappear."""
        v1 = _view(pid=10)
        v2 = _view(pid=20)

        with mock.patch("subprocess.run",
                        return_value=_mock_run(returncode=1)):
            groups = adapter.group([v1, v2])

        assert len(groups) == 2
        assert all(g.group_id.startswith("iterm2:singleton:") for g in groups)

    def test_osascript_timeout_falls_back_to_singletons(self, adapter):
        v = _view(pid=10)
        with mock.patch("subprocess.run",
                        side_effect=subprocess.TimeoutExpired(cmd=["x"], timeout=3)):
            groups = adapter.group([v])
        assert len(groups) == 1
        assert groups[0].group_id.startswith("iterm2:singleton:")

    def test_psutil_terminal_failure_makes_singleton(self, adapter):
        """psutil can't read the tty for one view → singleton (no
        coordinates to bucket on). Other views in the same call still
        bucket normally."""
        import psutil
        v1 = _view(pid=10)
        v2 = _view(pid=20)
        enum_out = "100|1|/dev/ttys001\n100|1|/dev/ttys002\n"

        def proc_factory(pid: int):
            if pid == 10:
                raise psutil.NoSuchProcess(pid=pid)
            return _proc_with_tty("/dev/ttys002")

        with (
            mock.patch("subprocess.run", return_value=_mock_run(enum_out)),
            mock.patch("psutil.Process", side_effect=proc_factory),
        ):
            groups = adapter.group([v1, v2])

        assert len(groups) == 2
        # One singleton (pid 10, tty unresolved), one normal bucket (pid 20)
        ids = {g.group_id for g in groups}
        assert "iterm2:singleton:10" in ids
        assert "iterm2:100:1" in ids


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
            # Verify the AppleScript embedded the tty
            called_args = run.call_args[0][0]
            assert called_args[0] == "osascript"
            assert "/dev/ttys001" in called_args[2]

    def test_focus_falls_back_to_app_frontmost_on_tty_miss(self, adapter):
        """tty matched psutil but iTerm2's tree has no session for it
        (tmux pty, pane closed between scan and click). The fallback
        should raise iTerm2 to the front via ``focus_host_app`` rather
        than silently no-op the click — better-than-nothing UX when
        pane precision is impossible."""
        v = _view(pid=10)
        with (
            mock.patch("psutil.Process",
                       return_value=_proc_with_tty("/dev/ttys999")),
            mock.patch("subprocess.run",
                       return_value=_mock_run(stdout="miss\n")),
            mock.patch(
                "claude_island.platform_.terminals.iterm2.focus_host_app",
                return_value=True,
            ) as fha,
        ):
            assert adapter.focus(v) is True
            fha.assert_called_once_with(10)

    def test_focus_returns_false_when_tty_miss_and_no_ui_ancestor(self, adapter):
        """tty miss AND no UI ancestor (tmux/screen with daemonized
        server) — the fallback can't recover, focus returns False."""
        v = _view(pid=10)
        with (
            mock.patch("psutil.Process",
                       return_value=_proc_with_tty("/dev/ttys999")),
            mock.patch("subprocess.run",
                       return_value=_mock_run(stdout="miss\n")),
            mock.patch(
                "claude_island.platform_.terminals.iterm2.focus_host_app",
                return_value=False,
            ),
        ):
            assert adapter.focus(v) is False

    def test_focus_falls_back_when_psutil_terminal_missing(self, adapter):
        """psutil returns no tty (process detached / non-controlling
        terminal) — can't run the per-pane AppleScript, but the host
        UI app fallback can still raise iTerm2."""
        v = _view(pid=10)
        with (
            mock.patch("psutil.Process",
                       return_value=_proc_with_tty(None)),
            mock.patch(
                "claude_island.platform_.terminals.iterm2.focus_host_app",
                return_value=True,
            ),
        ):
            assert adapter.focus(v) is True

    def test_focus_returns_false_on_osascript_error_without_ancestor(self, adapter):
        """osascript errored (returncode=1) AND no UI ancestor → False."""
        v = _view(pid=10)
        with (
            mock.patch("psutil.Process",
                       return_value=_proc_with_tty("/dev/ttys001")),
            mock.patch("subprocess.run",
                       return_value=_mock_run(returncode=1)),
            mock.patch(
                "claude_island.platform_.terminals.iterm2.focus_host_app",
                return_value=False,
            ),
        ):
            assert adapter.focus(v) is False

    def test_focus_accepts_and_ignores_siblings_kwarg(self, adapter):
        """For dispatcher uniformity. iTerm2's tty match is precise
        enough that no sibling fallback is ever needed."""
        v = _view(pid=10)
        with (
            mock.patch("psutil.Process",
                       return_value=_proc_with_tty("/dev/ttys001")),
            mock.patch("subprocess.run",
                       return_value=_mock_run(stdout="ok\n")),
        ):
            # Should accept siblings kwarg without TypeError.
            assert adapter.focus(v, siblings=[100, 200]) is True

    def test_focus_escapes_special_chars_in_tty(self, adapter):
        """Defence-in-depth: even if a tty path ever contained quotes
        or backslashes, the AppleScript injection should be safe."""
        v = _view(pid=10)
        # Hypothetical malicious tty (real ttys don't contain quotes)
        weird_tty = '/dev/tty"hax\\'
        with (
            mock.patch("psutil.Process",
                       return_value=_proc_with_tty(weird_tty)),
            mock.patch("subprocess.run",
                       return_value=_mock_run(stdout="miss\n")) as run,
            # Stub the fallback so this test only asserts on the
            # tty-path AppleScript shape, not the System Events call
            # the new fallback would trigger after a "miss".
            mock.patch(
                "claude_island.platform_.terminals.iterm2.focus_host_app",
                return_value=False,
            ),
        ):
            adapter.focus(v)
            # First subprocess.run call is the tty AppleScript.
            script = run.call_args_list[0][0][0][2]
            # Quote and backslash are escaped in the embedded literal
            assert '\\"' in script
            assert "\\\\" in script

    def test_focus_script_brings_iterm2_frontmost_via_system_events(self, adapter):
        """The AppleScript MUST raise iTerm2 via System Events, not
        via ``tell iTerm to activate``. claude-island's panel has
        Qt.WindowStaysOnTopHint and is the active app at click time;
        the in-app activate path is gated by AppKit's "non-active
        cannot order above active" rule and silently fails to surface
        the target window — verified live via the AppKit log
        message ``Window … ordered front from a non-active application
        and may order beneath the active application's windows``.
        System Events bypasses that rule via accessibility privilege."""
        v = _view(pid=10)
        with (
            mock.patch("psutil.Process",
                       return_value=_proc_with_tty("/dev/ttys001")),
            mock.patch("subprocess.run",
                       return_value=_mock_run(stdout="ok\n")) as run,
        ):
            adapter.focus(v)
            script = run.call_args[0][0][2]
            # System Events frontmost must come BEFORE the iTerm tell
            # block so the window-order privilege is in place when
            # ``select w`` runs.
            assert 'tell application "System Events"' in script
            assert 'set frontmost of process "iTerm2"' in script
            i_se = script.index('tell application "System Events"')
            i_iterm = script.index('tell application "iTerm"')
            assert i_se < i_iterm, (
                "System Events frontmost must run before the iTerm "
                "select block; otherwise the AppKit non-active rule "
                "kicks in before we've escalated privilege"
            )
            # All three selects still required for multi-window case:
            # raising iTerm2 to OS foreground doesn't pick which
            # iTerm2 window is on top inside the app.
            assert "select s" in script
            assert "select t" in script
            assert "select w" in script
            i_s = script.index("select s")
            i_t = script.index("select t")
            i_w = script.index("select w")
            assert i_s < i_t < i_w


# ── Phase 4 (resume-offline): LAUNCH capability ──────────────────────────

class TestITerm2Launch:
    """Verify @capability(LAUNCH) launch on ITerm2Adapter.

    Mocks subprocess.Popen so osascript never actually runs."""

    def test_launch_advertised_in_capabilities(self):
        assert Capability.LAUNCH in ITerm2Adapter.capabilities

    def test_launch_constructs_applescript_with_quoted_command(self):
        from claude_island.core.capabilities import SpawnResult
        adapter = ITerm2Adapter()
        # PurePosixPath so the test asserts a Unix-style cwd regardless of
        # the host running the suite (the production target is macOS).
        from pathlib import PurePosixPath
        cwd = PurePosixPath("/Users/me/proj with space")
        with mock.patch(
            "claude_island.platform_.terminals.iterm2.subprocess.Popen",
        ) as mock_popen:
            mock_popen.return_value.pid = 7777
            result = adapter.launch(
                cwd=cwd,
                command=("claude", "--resume", "u1", "--dangerously-skip-permissions"),
            )

        assert isinstance(result, SpawnResult)
        assert result.terminal_pid == 7777
        assert result.terminal_name == adapter.name

        argv = mock_popen.call_args[0][0]
        assert argv[0] == "osascript"
        assert argv[1] == "-e"
        script = argv[2]
        assert "tell application \"iTerm\"" in script
        assert "create window with default profile" in script
        # cwd with space should be shell-quoted (shlex.quote uses single quotes)
        assert "'/Users/me/proj with space'" in script
        assert "--dangerously-skip-permissions" in script

    def test_launch_wraps_oserror(self):
        from claude_island.core.capabilities import LauncherSpawnError
        adapter = ITerm2Adapter()
        with mock.patch(
            "claude_island.platform_.terminals.iterm2.subprocess.Popen",
            side_effect=FileNotFoundError("osascript missing"),
        ):
            with pytest.raises(LauncherSpawnError, match="osascript missing"):
                adapter.launch(cwd=Path("/x"), command=("claude",))
