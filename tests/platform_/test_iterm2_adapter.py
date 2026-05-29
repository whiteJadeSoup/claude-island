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
    _FOCUS_SCRIPT_BY_ID_TEMPLATE,
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

    def test_placeholder_pid_returns_false_without_calling_psutil(self, adapter):
        """PLACEHOLDER_PID (-1) sessions come from the hook bridge before
        the scanner sees a real process; psutil.Process(-1) would raise
        ValueError and explode the dispatcher's list comprehension."""
        with mock.patch("psutil.Process") as p:
            assert adapter.can_handle(_session(pid=-1)) is False
            p.assert_not_called()

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

def _proc_with_tty(tty: str | None, *, parent_chain: list[tuple[int, str]] | None = None) -> mock.Mock:
    """Build a Mock psutil.Process whose ``terminal()`` returns ``tty``.

    ``parent_chain`` is the ancestor list as ``(pid, name)`` pairs in
    walk order (immediate parent first). Each ancestor Mock has its
    own ``parent()`` returning the next, so ``_iterm_host_pid`` can
    walk the chain. Defaults to an empty chain → ``_iterm_host_pid``
    returns ``None`` and ``focus()`` falls through to ``focus_host_app``.
    """
    if parent_chain is None:
        parent_chain = []
    p = mock.Mock()
    p.terminal = lambda: tty
    # Build the chain from the tail inward so each ancestor knows its
    # own parent (a linked list rooted at the input proc).
    next_proc: mock.Mock | None = None
    for pid, name in reversed(parent_chain):
        anc = mock.Mock()
        anc.pid = pid
        anc.name = lambda n=name: n
        nxt = next_proc
        anc.parent = lambda _n=nxt: _n
        next_proc = anc
    p.parent = lambda _n=next_proc: _n
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

    def test_placeholder_pid_becomes_singleton_without_psutil_call(self, adapter):
        """A view with PLACEHOLDER_PID (-1) has no real process — must
        not call psutil.Process(-1) (raises ValueError) and must still
        be rendered as a singleton group."""
        v = _view(pid=-1)
        enum_out = "100|1|/dev/ttys001\n"
        with (
            mock.patch("subprocess.run", return_value=_mock_run(enum_out)),
            mock.patch("psutil.Process") as p,
        ):
            groups = adapter.group([v])
            p.assert_not_called()
        assert len(groups) == 1
        assert groups[0].group_id == "iterm2:singleton:-1"

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
                       return_value=_proc_with_tty(
                           "/dev/ttys001",
                           parent_chain=[(20, "zsh"), (30, "iTerm2")],
                       )),
            mock.patch("subprocess.run",
                       return_value=_mock_run(stdout="ok\n")) as run,
        ):
            assert adapter.focus(v) is True
            # Verify the AppleScript embedded the tty AND the resolved
            # iTerm2 host pid (30, from the parent chain). The host pid
            # disambiguates between multiple iTerm2 installations.
            called_args = run.call_args[0][0]
            assert called_args[0] == "osascript"
            assert "/dev/ttys001" in called_args[2]
            assert "unix id is 30" in called_args[2]

    def test_focus_placeholder_pid_skips_psutil_and_falls_back(self, adapter):
        """PLACEHOLDER_PID (-1): no real process to ask for tty. Must not
        call psutil.Process(-1) (raises ValueError); should hand off to
        focus_host_app so the click isn't a silent crash."""
        v = _view(pid=-1)
        with (
            mock.patch("psutil.Process") as p,
            mock.patch(
                "claude_island.platform_.terminals.iterm2.focus_host_app",
                return_value=False,
            ) as fha,
        ):
            assert adapter.focus(v) is False
            p.assert_not_called()
            fha.assert_called_once_with(-1)

    def test_focus_falls_back_to_app_frontmost_on_tty_miss(self, adapter):
        """tty matched psutil but iTerm2's tree has no session for it
        (tmux pty, pane closed between scan and click). The fallback
        should raise iTerm2 to the front via ``focus_host_app`` rather
        than silently no-op the click — better-than-nothing UX when
        pane precision is impossible."""
        v = _view(pid=10)
        with (
            mock.patch("psutil.Process",
                       return_value=_proc_with_tty(
                           "/dev/ttys999",
                           parent_chain=[(20, "zsh"), (30, "iTerm2")],
                       )),
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
                       return_value=_proc_with_tty(
                           "/dev/ttys999",
                           parent_chain=[(20, "zsh"), (30, "iTerm2")],
                       )),
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
                       return_value=_proc_with_tty(
                           "/dev/ttys001",
                           parent_chain=[(20, "zsh"), (30, "iTerm2")],
                       )),
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
                       return_value=_proc_with_tty(
                           "/dev/ttys001",
                           parent_chain=[(20, "zsh"), (30, "iTerm2")],
                       )),
            mock.patch("subprocess.run",
                       return_value=_mock_run(stdout="ok\n")),
        ):
            # Should accept siblings kwarg without TypeError.
            assert adapter.focus(v, siblings=[_view(pid=100), _view(pid=200)]) is True

    def test_focus_escapes_special_chars_in_tty(self, adapter):
        """Defence-in-depth: even if a tty path ever contained quotes
        or backslashes, the AppleScript injection should be safe."""
        v = _view(pid=10)
        # Hypothetical malicious tty (real ttys don't contain quotes)
        weird_tty = '/dev/tty"hax\\'
        with (
            mock.patch("psutil.Process",
                       return_value=_proc_with_tty(
                           weird_tty,
                           parent_chain=[(20, "zsh"), (30, "iTerm2")],
                       )),
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
                       return_value=_proc_with_tty(
                           "/dev/ttys001",
                           parent_chain=[(20, "zsh"), (30, "iTerm2")],
                       )),
            mock.patch("subprocess.run",
                       return_value=_mock_run(stdout="ok\n")) as run,
        ):
            adapter.focus(v)
            script = run.call_args[0][0][2]
            # System Events frontmost must come BEFORE the iTerm tell
            # block so the window-order privilege is in place when
            # ``select w`` runs.
            assert 'tell application "System Events"' in script
            # Frontmost targets the resolved iTerm2 host pid (30 from
            # the parent chain) by unix id rather than by process name
            # — required for the multi-iTerm-installation case.
            assert "unix id is 30" in script
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
            # I-8: broadest-scope first (w → t → s). select mutates
            # state on each call; doing window last would force an
            # extra z-order change after we've already pinned tab +
            # session. Most-precise selection ends last so it wins
            # regardless of what select w did to in-tab selection.
            i_s = script.index("select s")
            i_t = script.index("select t")
            i_w = script.index("select w")
            assert i_w < i_t < i_s, (
                "selects must run window → tab → session "
                "(broadest scope first); got s={} t={} w={}".format(i_s, i_t, i_w)
            )


class TestFocusScriptDeminiaturizesWindow:
    """Regression: clicking a session whose host iTerm window is
    minimized to the Dock used to silently fail — ``select w`` raises
    a window in iTerm's z-order but does not deminiaturize a window
    that's in the Dock. The script must set ``miniaturized of w to
    false`` before ``select w`` so the window comes out of the Dock.
    Idempotent: no-op when the window is already visible."""

    def _assert_deminiaturize_before_select_w(self, script: str) -> None:
        assert "set miniaturized of w to false" in script
        i_demin = script.index("set miniaturized of w to false")
        i_w = script.index("select w")
        assert i_demin < i_w, (
            "deminiaturize must run before select w; select alone "
            "won't pull the window out of the Dock"
        )

    def test_tty_template_deminiaturizes_before_select(self):
        script = _FOCUS_SCRIPT_TEMPLATE.format(host_pid=42, tty="/dev/ttys004")
        self._assert_deminiaturize_before_select_w(script)

    def test_id_template_deminiaturizes_before_select(self):
        script = _FOCUS_SCRIPT_BY_ID_TEMPLATE.format(
            host_pid=42, session_id="ABC-123",
        )
        self._assert_deminiaturize_before_select_w(script)


class TestFocusScriptGuardsRedundantMutators:
    """Subprocess templates carry the same guarded-mutator regression
    as the fast-path sources — see TestFocusSourceGuardsRedundantMutators
    in test_iterm2_apple_script_cache for the user-reported bug
    (apa-origin "flash to front and back" caused by unconditional
    ``set miniaturized``/``set index`` mutators)."""

    def _assert_guarded_mutators(self, script: str) -> None:
        assert "if miniaturized of w is true then" in script
        assert "if index of w is not 1 then" in script

    def test_tty_template_guards_mutators(self):
        s = _FOCUS_SCRIPT_TEMPLATE.format(host_pid=42, tty="/dev/ttys004")
        self._assert_guarded_mutators(s)

    def test_id_template_guards_mutators(self):
        s = _FOCUS_SCRIPT_BY_ID_TEMPLATE.format(
            host_pid=42, session_id="ABC-123",
        )
        self._assert_guarded_mutators(s)


class TestFocusScriptRaceTolerance:
    """Regression: subprocess focus AppleScripts now wrap the iTerm
    tell in retry-twice + try so a session/tab/window vanishing
    mid-iteration (errAEIllegalIndex -1719) is absorbed rather than
    surfacing as a focus failure."""

    def _assert_retry_wraps_iterm_tell(self, script: str) -> None:
        assert "repeat 2 times" in script
        assert "on error" in script
        i_repeat = script.index("repeat 2 times")
        i_iterm = script.index('tell application "iTerm"')
        i_on_error = script.index("on error")
        assert i_repeat < i_iterm < i_on_error

    def test_tty_template_wraps_in_retry(self):
        script = _FOCUS_SCRIPT_TEMPLATE.format(host_pid=42, tty="/dev/ttys004")
        self._assert_retry_wraps_iterm_tell(script)

    def test_id_template_wraps_in_retry(self):
        script = _FOCUS_SCRIPT_BY_ID_TEMPLATE.format(
            host_pid=42, session_id="ABC-123",
        )
        self._assert_retry_wraps_iterm_tell(script)


class TestFocusScriptSetsWindowIndex:
    """I-5: cross-Space hint — ``set index of w to 1`` after
    ``select w`` sometimes pulls the window onto the current macOS
    Space. Mirror of the same regression in the fast-path templates."""

    def _assert_index_after_select_w(self, script: str) -> None:
        # Strip ``-- ...`` AppleScript comments so comment text
        # mentioning these constructs doesn't break substring matching.
        import re
        bare = re.sub(r"--[^\n]*", "", script)
        assert "set index of w to 1" in bare
        i_select = bare.index("select w")
        i_index = bare.index("set index of w to 1")
        assert i_select < i_index

    def test_tty_template_sets_window_index(self):
        script = _FOCUS_SCRIPT_TEMPLATE.format(host_pid=42, tty="/dev/ttys004")
        self._assert_index_after_select_w(script)

    def test_id_template_sets_window_index(self):
        script = _FOCUS_SCRIPT_BY_ID_TEMPLATE.format(
            host_pid=42, session_id="ABC-123",
        )
        self._assert_index_after_select_w(script)


# ── Dual-iTerm host-pid resolution ──────────────────────────────────────


class TestITermHostPidResolution:
    """Regression coverage for the dual-iTerm2 click-no-response bug.

    When two iTerm2 installations are running simultaneously (e.g. the
    factory ``/Applications/iTerm.app`` plus a user-installed copy in
    ``~/Applications/iTerm 2.app``, both bundle id
    ``com.googlecode.iterm2``), the previous focus script used
    ``set frontmost of process "iTerm2"`` — System Events resolves
    that to the FIRST matching process by name, regardless of which
    instance hosts the clicked session. Sessions in the OTHER instance
    silently brought the wrong window forward.

    The fix walks the session's ancestor chain to resolve the specific
    iTerm2 host pid and targets it by ``unix id`` in the AppleScript.
    """

    def test_iterm_host_pid_walks_to_iterm_ancestor(self):
        """Helper returns the pid of the first iTerm2-named ancestor."""
        from claude_island.platform_.terminals.iterm2 import _iterm_host_pid
        proc = _proc_with_tty(
            "/dev/ttys001",
            parent_chain=[
                (100, "zsh"),
                (200, "login"),
                (300, "iTermServer-3.6.10"),  # NOT matched
                (400, "iTerm2"),                # matched here
                (1, "launchd"),
            ],
        )
        with mock.patch("psutil.Process", return_value=proc):
            assert _iterm_host_pid(1234) == 400

    def test_iterm_host_pid_returns_none_when_no_iterm_ancestor(self):
        """tmux/screen-style chain with no iTerm2 ancestor → None.
        Callers must treat None as 'fall back to focus_host_app' so
        the click isn't a silent no-op."""
        from claude_island.platform_.terminals.iterm2 import _iterm_host_pid
        proc = _proc_with_tty(
            "/dev/ttys001",
            parent_chain=[(100, "tmux"), (200, "launchd")],
        )
        with mock.patch("psutil.Process", return_value=proc):
            assert _iterm_host_pid(1234) is None

    def test_iterm_host_pid_returns_none_on_placeholder_pid(self):
        """PLACEHOLDER_PID (-1): no real process to walk. Helper must
        guard against ValueError from ``psutil.Process(-1)``."""
        from claude_island.platform_.terminals.iterm2 import _iterm_host_pid
        with mock.patch("psutil.Process") as p:
            assert _iterm_host_pid(-1) is None
            p.assert_not_called()

    def test_iterm_host_pid_is_case_insensitive(self):
        """``iTerm2`` vs ``iterm2`` vs ``iTerm`` — psutil reports the
        binary name as the OS provides it; the matcher must not care
        about case."""
        from claude_island.platform_.terminals.iterm2 import _iterm_host_pid
        proc = _proc_with_tty(
            "/dev/ttys001",
            parent_chain=[(100, "ITERM2")],
        )
        with mock.patch("psutil.Process", return_value=proc):
            assert _iterm_host_pid(1234) == 100

    def test_focus_targets_host_pid_not_process_name(self):
        """Bug regression: the focus AppleScript must reference the
        resolved host pid by ``unix id``, not the ambiguous
        ``process "iTerm2"``. Two iTerm2 installations are simulated
        via a chain whose iTerm2 ancestor pid is 999; the emitted
        script must contain ``unix id is 999``."""
        v = _view(pid=10)
        with (
            mock.patch("psutil.Process",
                       return_value=_proc_with_tty(
                           "/dev/ttys001",
                           parent_chain=[(50, "zsh"), (999, "iTerm2")],
                       )),
            mock.patch("subprocess.run",
                       return_value=_mock_run(stdout="ok\n")) as run,
        ):
            assert adapter_for_test().focus(v) is True
            script = run.call_args[0][0][2]
            assert "unix id is 999" in script
            # And it should NOT use the old name-based selector that
            # the bug originally triggered.
            assert 'process "iTerm2"' not in script

    def test_focus_falls_back_when_no_iterm_ancestor(self):
        """When the parent walk finds no iTerm2 ancestor (e.g. tmux
        daemon reparenting), focus skips the tty-precision script
        entirely and hands off to ``focus_host_app`` rather than
        emitting a malformed AppleScript."""
        v = _view(pid=10)
        with (
            mock.patch("psutil.Process",
                       return_value=_proc_with_tty(
                           "/dev/ttys001",
                           parent_chain=[(50, "tmux"), (60, "launchd")],
                       )),
            mock.patch("subprocess.run") as run,
            mock.patch(
                "claude_island.platform_.terminals.iterm2.focus_host_app",
                return_value=True,
            ) as fha,
        ):
            assert adapter_for_test().focus(v) is True
            # No tty-precision AppleScript emitted (host pid unknown).
            run.assert_not_called()
            fha.assert_called_once_with(10)


class TestStaleTerminalPidValidation:
    """C-1: Hook-captured ``jump_target.terminal_pid`` is frozen at
    SessionStart. By click time iTerm may have restarted (pid dead)
    or, worse, macOS may have recycled the pid to a different UI app
    (Slack, Mail). Trusting the stale pid silently steals focus.

    ``_resolve_host_pid`` now validates the cached pid via
    ``_pid_is_iterm`` (psutil name check) before trusting it; otherwise
    falls back to the runtime ancestor walk on the live claude pid.
    """

    @staticmethod
    def _view_with_terminal_pid(
        claude_pid: int, terminal_pid: int,
    ):
        from dataclasses import replace as _replace
        from claude_island.core.hook_events import JumpTarget
        v = _view(pid=claude_pid)
        return _replace(
            v,
            jump_target=JumpTarget(
                terminal_app="iTerm.app",
                term_program="iTerm.app",
                iterm_session_id="",
                terminal_pid=terminal_pid,
            ),
        )

    def test_resolve_host_pid_trusts_terminal_pid_when_still_iterm(self):
        """Common case: hook captured pid, iTerm is still that pid →
        return the cached pid directly without runtime walk."""
        adapter = adapter_for_test()
        v = self._view_with_terminal_pid(claude_pid=10, terminal_pid=999)
        # _pid_is_iterm returns True (process still alive and named iTerm).
        with mock.patch(
            "claude_island.platform_.terminals.iterm2._pid_is_iterm",
            return_value=True,
        ) as is_iterm:
            assert adapter._resolve_host_pid(v, v.jump_target) == 999
            is_iterm.assert_called_once_with(999)

    def test_resolve_host_pid_falls_back_when_terminal_pid_recycled(self):
        """If the cached pid is no longer iTerm (process died and
        macOS recycled the pid to Slack), fall back to the runtime
        walk on the live claude pid instead of trusting the stale pid.

        Critical: without this, NSRunningApplication.activate(stale_pid)
        would foreground Slack when the user clicked an iTerm session.
        """
        adapter = adapter_for_test()
        v = self._view_with_terminal_pid(claude_pid=10, terminal_pid=999)
        with (
            mock.patch(
                "claude_island.platform_.terminals.iterm2._pid_is_iterm",
                return_value=False,   # stale: pid is now non-iTerm
            ) as is_iterm,
            mock.patch(
                "claude_island.platform_.terminals.iterm2._iterm_host_pid",
                return_value=12345,
            ) as walk,
        ):
            assert adapter._resolve_host_pid(v, v.jump_target) == 12345
            is_iterm.assert_called_once_with(999)
            walk.assert_called_once_with(10)   # walk on live claude pid

    def test_pid_is_iterm_true_for_iterm_process(self):
        """Direct unit: live process whose name matches the iTerm
        ancestor set returns True."""
        from claude_island.platform_.terminals.iterm2 import _pid_is_iterm
        fake = mock.Mock()
        fake.name.return_value = "iTerm2"
        with mock.patch("psutil.Process", return_value=fake):
            assert _pid_is_iterm(999) is True

    def test_pid_is_iterm_false_for_recycled_pid_now_non_iterm(self):
        """Recycled pid: process exists but its name is something
        else (Slack, Mail). Must NOT be trusted."""
        from claude_island.platform_.terminals.iterm2 import _pid_is_iterm
        fake = mock.Mock()
        fake.name.return_value = "Slack"
        with mock.patch("psutil.Process", return_value=fake):
            assert _pid_is_iterm(999) is False

    def test_pid_is_iterm_false_when_process_dead(self):
        """psutil.NoSuchProcess → False (no Python exception escapes)."""
        import psutil
        from claude_island.platform_.terminals.iterm2 import _pid_is_iterm
        with mock.patch(
            "psutil.Process",
            side_effect=psutil.NoSuchProcess(pid=999),
        ):
            assert _pid_is_iterm(999) is False

    def test_pid_is_iterm_false_for_zero_and_negative(self):
        """Defensive: pid <= 0 short-circuits to False without
        touching psutil."""
        from claude_island.platform_.terminals.iterm2 import _pid_is_iterm
        with mock.patch("psutil.Process") as p:
            assert _pid_is_iterm(0) is False
            assert _pid_is_iterm(-1) is False
            p.assert_not_called()


def adapter_for_test() -> ITerm2Adapter:
    """Adapter instance for the dual-iTerm regression tests.
    Bypasses the @adapter registry so the tests run on any OS."""
    a = ITerm2Adapter()
    a.name = "iterm2"
    a._priority = 100
    return a


# ── Hook-captured identifiers fast path (v6) ────────────────────────────


class TestFocusByHookCapturedIds:
    """When the SessionStart hook captured ``iterm_session_id`` +
    ``terminal_pid`` into ``jump_target``, the focus path should
    skip all psutil walks and run a single id-match AppleScript.

    Falling back: if the captured id has aged out (iTerm restarted,
    session closed), focus must transparently fall through to the
    tty-based path rather than reporting failure outright.
    """

    @staticmethod
    def _view_with_jt(*, pid: int, iterm_session_id: str, terminal_pid: int) -> SessionView:
        from dataclasses import replace as _replace
        from claude_island.core.hook_events import JumpTarget
        v = _view(pid=pid)
        return _replace(
            v,
            jump_target=JumpTarget(
                terminal_app="iTerm.app",
                term_program="iTerm.app",
                iterm_session_id=iterm_session_id,
                terminal_pid=terminal_pid,
            ),
        )

    def test_id_path_skips_parent_walk_when_capture_present(self):
        """The fast path runs ONE osascript and returns True. The
        parent-walk helper (_iterm_host_pid) must not be touched —
        captured host pid is trusted directly after the cheap
        ``_pid_is_iterm`` liveness check."""
        v = self._view_with_jt(
            pid=12345, iterm_session_id="ABC-123", terminal_pid=90559,
        )
        with (
            # Captured pid is still iTerm — short-circuit liveness check
            # so we don't drag psutil.Process into the assertion surface.
            mock.patch(
                "claude_island.platform_.terminals.iterm2._pid_is_iterm",
                return_value=True,
            ),
            mock.patch(
                "claude_island.platform_.terminals.iterm2._iterm_host_pid",
            ) as walk,
            mock.patch("subprocess.run",
                       return_value=_mock_run(stdout="ok\n")) as run,
        ):
            assert adapter_for_test().focus(v) is True
            # Real intent: parent-ancestry walk is bypassed.
            walk.assert_not_called()
            assert run.call_count == 1
            script = run.call_args[0][0][2]
            # The id-match template, not the tty-match template.
            assert "id of s as text" in script
            assert "ABC-123" in script
            assert "unix id is 90559" in script

    def test_id_path_miss_falls_back_to_tty_path(self):
        """Captured id no longer resolves (iTerm restarted, etc.) —
        the slow path takes over and recovers via tty match."""
        v = self._view_with_jt(
            pid=12345, iterm_session_id="STALE-ID", terminal_pid=90559,
        )
        # First call (id template) returns "miss"; second call (tty
        # template) returns "ok". subprocess.run side_effect cycles.
        with (
            mock.patch("psutil.Process",
                       return_value=_proc_with_tty(
                           "/dev/ttys001",
                           parent_chain=[(50, "zsh"), (90559, "iTerm2")],
                       )),
            mock.patch("subprocess.run", side_effect=[
                _mock_run(stdout="miss\n"),  # id path miss
                _mock_run(stdout="ok\n"),     # tty path hit
            ]) as run,
        ):
            assert adapter_for_test().focus(v) is True
            assert run.call_count == 2
            # Second call should be the tty template, not the id one.
            second_script = run.call_args_list[1][0][0][2]
            assert "tty of s is " in second_script

    def test_id_path_uses_captured_host_pid_not_runtime_walk(self):
        """``jump_target.terminal_pid`` is preferred over the runtime
        ``_iterm_host_pid`` walk. When both the id-match script lands
        and host_pid is captured, the script must reference the
        captured pid (90559), NOT the result of an ancestor walk."""
        v = self._view_with_jt(
            pid=12345, iterm_session_id="ABC-123", terminal_pid=90559,
        )
        with (
            # Captured pid is still iTerm — short-circuit liveness check.
            mock.patch(
                "claude_island.platform_.terminals.iterm2._pid_is_iterm",
                return_value=True,
            ),
            mock.patch("subprocess.run",
                       return_value=_mock_run(stdout="ok\n")) as run,
            mock.patch(
                "claude_island.platform_.terminals.iterm2._iterm_host_pid",
            ) as walk,
        ):
            adapter_for_test().focus(v)
            walk.assert_not_called()
            assert "unix id is 90559" in run.call_args[0][0][2]

    def test_id_path_partial_capture_id_only_falls_through(self):
        """Only iterm_session_id captured, terminal_pid=0 (parent walk
        failed at hook time). Without a host_pid the id template can't
        run safely — fall through to the slow path which can derive
        host_pid at click time via _iterm_host_pid."""
        v = self._view_with_jt(
            pid=12345, iterm_session_id="ABC-123", terminal_pid=0,
        )
        with (
            mock.patch("psutil.Process",
                       return_value=_proc_with_tty(
                           "/dev/ttys001",
                           parent_chain=[(50, "zsh"), (77777, "iTerm2")],
                       )),
            mock.patch("subprocess.run",
                       return_value=_mock_run(stdout="ok\n")) as run,
        ):
            assert adapter_for_test().focus(v) is True
            # Only the tty template ran — id template was skipped.
            assert run.call_count == 1
            script = run.call_args[0][0][2]
            assert "tty of s is " in script
            assert "unix id is 77777" in script  # from the runtime walk

    def test_id_path_partial_capture_pid_only_falls_through(self):
        """Only terminal_pid captured. Without an id the id template
        can't run; fall through to the tty path but use the captured
        pid for frontmost (skipping the runtime walk)."""
        v = self._view_with_jt(
            pid=12345, iterm_session_id="", terminal_pid=90559,
        )
        with (
            # Captured pid is still iTerm — short-circuit liveness check.
            mock.patch(
                "claude_island.platform_.terminals.iterm2._pid_is_iterm",
                return_value=True,
            ),
            mock.patch("psutil.Process",
                       return_value=_proc_with_tty(
                           "/dev/ttys001",
                           parent_chain=[(50, "zsh"), (77777, "iTerm2")],
                       )),
            mock.patch("subprocess.run",
                       return_value=_mock_run(stdout="ok\n")) as run,
            mock.patch(
                "claude_island.platform_.terminals.iterm2._iterm_host_pid",
            ) as walk,
        ):
            assert adapter_for_test().focus(v) is True
            # Runtime walk must NOT run when captured pid is available.
            walk.assert_not_called()
            script = run.call_args[0][0][2]
            assert "unix id is 90559" in script


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


class TestWriteTextToITermSession:
    """write_text_to_iterm_session injects text into a target iTerm
    pane via AppleScript's ``write text`` — the workaround for
    Claude's terminal-prompt-not-dismissed-by-hook-allow bug on
    sensitive operations. Tested via mocked subprocess; the real
    AppleScript is exercised in the legacy-focus tests."""

    def test_returns_true_on_ok(self):
        from claude_island.platform_.terminals.iterm2 import write_text_to_iterm_session
        with mock.patch("subprocess.run", return_value=_mock_run(stdout="ok\n")) as run:
            assert write_text_to_iterm_session("/dev/ttys007", "1") is True
        # Script must contain the tty target and the text payload.
        called_script = run.call_args[0][0][2]
        assert "/dev/ttys007" in called_script
        # Text appears inside ``write text "..."`` — check substring match.
        assert 'write text "1"' in called_script

    def test_returns_false_on_miss(self):
        from claude_island.platform_.terminals.iterm2 import write_text_to_iterm_session
        with mock.patch("subprocess.run", return_value=_mock_run(stdout="miss\n")):
            assert write_text_to_iterm_session("/dev/ttys-bogus", "1") is False

    def test_returns_false_on_subprocess_failure(self):
        """OSError / TimeoutExpired must not crash the caller — the
        Allow flow already fired its hook response by the time this
        runs, so a failed inject just degrades to "user types in
        terminal" rather than surfacing as an error."""
        import subprocess as _sp
        from claude_island.platform_.terminals.iterm2 import write_text_to_iterm_session
        with mock.patch("subprocess.run", side_effect=_sp.TimeoutExpired("osascript", 3)):
            assert write_text_to_iterm_session("/dev/ttys007", "1") is False

    def test_empty_tty_short_circuits_without_subprocess(self):
        """Empty tty (placeholder sessions, lookup failure) skips the
        subprocess entirely — no point asking iTerm about a non-tty."""
        from claude_island.platform_.terminals.iterm2 import write_text_to_iterm_session
        with mock.patch("subprocess.run") as run:
            assert write_text_to_iterm_session("", "1") is False
            run.assert_not_called()

    def test_text_with_quote_or_backslash_is_escaped(self):
        """User-provided text could contain AppleScript metacharacters.
        We use _escape_applescript_string (already used by focus
        scripts) so injecting ``"`` or ``\\`` doesn't break the
        script. The Allow callback always passes "1", but defending
        the helper makes it safe for other callers."""
        from claude_island.platform_.terminals.iterm2 import write_text_to_iterm_session
        with mock.patch("subprocess.run", return_value=_mock_run(stdout="miss\n")) as run:
            write_text_to_iterm_session("/dev/ttys007", 'a"b\\c')
        script = run.call_args[0][0][2]
        # Escaped: " → \", \ → \\
        assert r'a\"b\\c' in script


class TestFocusScriptSwitchesSpace:
    """Cross-Space regression: clicking a session whose iTerm window is
    on another macOS Space did nothing — ``select w`` only reorders
    iTerm's internal window list, it does NOT switch Spaces. The script
    must AXRaise the target window (matched by its title in System
    Events) to pull its Space to the foreground. ``try``-guarded so a
    title mismatch / AX error degrades to the prior behaviour."""

    def _assert_axraise(self, script: str, host_pid: int) -> None:
        # Title captured from the iTerm window before any mutation.
        assert "set winName to name of w" in script
        # AXRaise targets the resolved host pid by unix id (multi-iTerm
        # correctness) and matches the window by the captured title.
        assert "unix id is {}".format(host_pid) in script
        assert 'perform action "AXRaise" of (first window whose name is winName)' in script
        # Ordering: title captured, AXRaise, THEN selects.
        i_name = script.index("set winName to name of w")
        i_select_w = script.index("select w")
        i_raise = script.index('perform action "AXRaise"')
        # AXRaise must run BEFORE select w/t/s: on a multi-pane window
        # the select changes the window title, so raising after select
        # would search for a title the window no longer has and miss.
        assert i_name < i_raise < i_select_w, (
            "must capture title, AXRaise, THEN select; "
            "got name={} raise={} select_w={}".format(i_name, i_raise, i_select_w)
        )
        # Graceful degradation: AXRaise is inside a try block that closes
        # before the selects run.
        i_end_try = script.index("end try", i_raise)
        assert i_end_try < i_select_w, "AXRaise must be try-guarded, closing before select w"

    def test_tty_template_axraises_after_select(self):
        script = _FOCUS_SCRIPT_TEMPLATE.format(host_pid=42, tty="/dev/ttys004")
        self._assert_axraise(script, 42)

    def test_id_template_axraises_after_select(self):
        script = _FOCUS_SCRIPT_BY_ID_TEMPLATE.format(host_pid=42, session_id="ABC-123")
        self._assert_axraise(script, 42)
