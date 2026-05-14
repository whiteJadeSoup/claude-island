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


def _bytes_run(stdout_text: str = "", returncode: int = 0,
               stderr_text: str = "") -> mock.Mock:
    """Build a fake subprocess.run result mirroring what
    capture_output (without text=True) yields: bytes stdout + stderr."""
    return mock.Mock(
        stdout=stdout_text.encode("utf-8"),
        stderr=stderr_text.encode("utf-8"),
        returncode=returncode,
    )


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

    def test_returns_none_when_returncode_nonzero(self):
        """Failure returns None (sentinel) so the caller can skip the
        cache write — distinct from a successful query that legitimately
        returned an empty set. Empty-on-failure was the B1 bug: a single
        timeout poisoned the cache for 30 s and stripped FOCUS from
        every macOS view."""
        with mock.patch(
            "claude_island.platform_.terminals._macos_common.subprocess.run",
            return_value=_bytes_run("123, 456", returncode=1),
        ):
            assert _macos_common._query_ui_app_pids() is None

    def test_returns_none_on_oserror(self):
        with mock.patch(
            "claude_island.platform_.terminals._macos_common.subprocess.run",
            side_effect=OSError("not found"),
        ):
            assert _macos_common._query_ui_app_pids() is None

    def test_returns_none_on_timeout(self):
        import subprocess
        with mock.patch(
            "claude_island.platform_.terminals._macos_common.subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd=["x"], timeout=3),
        ):
            assert _macos_common._query_ui_app_pids() is None

    def test_logs_stderr_on_failure_once_per_distinct_message(self, caplog):
        """Permission denial returns ``Not authorized to send Apple
        events (-1743)`` in stderr. Surface it at WARNING so the user
        can find Privacy & Security ▶ Automation in stderr — but only
        once per distinct stderr text so a persistent denial doesn't
        spam the log on every snapshot tick."""
        import logging
        caplog.set_level(logging.WARNING)
        stderr_msg = "execution error: Not authorized to send Apple events (-1743)\n"
        with mock.patch(
            "claude_island.platform_.terminals._macos_common.subprocess.run",
            return_value=mock.Mock(
                stdout=b"",
                stderr=stderr_msg.encode("utf-8"),
                returncode=1,
            ),
        ):
            _macos_common._query_ui_app_pids()
            _macos_common._query_ui_app_pids()
            _macos_common._query_ui_app_pids()
        # First call logs; subsequent calls with same stderr are quiet.
        warning_records = [r for r in caplog.records if r.levelname == "WARNING"]
        assert len(warning_records) == 1
        assert "-1743" in warning_records[0].getMessage()

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

    def test_call_after_ttl_re_queries(self):
        """N3: time advances past TTL → cache invalidated → re-query.
        Without this guard, a stale cache would be returned indefinitely
        and freshly-launched terminals would never show as focusable."""
        with mock.patch(
            "claude_island.platform_.terminals._macos_common.subprocess.run",
            return_value=_bytes_run("100"),
        ) as run:
            base_t = 1000.0
            with mock.patch(
                "claude_island.platform_.terminals._macos_common.time.monotonic",
                return_value=base_t,
            ):
                _macos_common._ui_app_pids()
                assert run.call_count == 1
            # Within TTL: cache hit, no re-query
            with mock.patch(
                "claude_island.platform_.terminals._macos_common.time.monotonic",
                return_value=base_t + 10.0,
            ):
                _macos_common._ui_app_pids()
                assert run.call_count == 1
            # Past TTL: cache expired, re-query
            with mock.patch(
                "claude_island.platform_.terminals._macos_common.time.monotonic",
                return_value=base_t + 60.0,
            ):
                _macos_common._ui_app_pids()
                assert run.call_count == 2

    def test_failure_does_not_poison_cache_b1(self):
        """B1: a single osascript failure must NOT freeze the cache
        empty for 30 s. Failures are sentinel'd by ``_query_ui_app_pids``
        returning None; ``_ui_app_pids`` skips the cache write so the
        next caller retries.

        Concrete scenario: cold start → System Events times out →
        90 s later (one TTL+) → System Events recovers. Without the
        fix, the 90 s gap silently extends to indefinite (every call
        sees empty cache and never refreshes). With the fix, the 2nd
        call fires a fresh osascript that succeeds."""
        import subprocess
        timeout_exc = subprocess.TimeoutExpired(cmd=["x"], timeout=3)
        success_result = _bytes_run("100, 200")
        with mock.patch(
            "claude_island.platform_.terminals._macos_common.subprocess.run",
            side_effect=[timeout_exc, success_result],
        ) as run:
            base_t = 1000.0
            with mock.patch(
                "claude_island.platform_.terminals._macos_common.time.monotonic",
                return_value=base_t,
            ):
                first = _macos_common._ui_app_pids()
            # Failure didn't poison cache; second call retries.
            with mock.patch(
                "claude_island.platform_.terminals._macos_common.time.monotonic",
                return_value=base_t + 5.0,  # within TTL!
            ):
                second = _macos_common._ui_app_pids()
        assert first == frozenset(), "first call returns empty (no cached fallback)"
        assert second == frozenset({100, 200}), "second call retries and gets fresh data"
        assert run.call_count == 2

    def test_failure_keeps_last_known_good_b1(self):
        """B1 corollary: when a previous successful query is cached and
        a subsequent refresh fails, keep serving the last-known-good
        instead of degrading to empty. A network blip on a refresh
        shouldn't strip FOCUS from every UI."""
        import subprocess
        timeout_exc = subprocess.TimeoutExpired(cmd=["x"], timeout=3)
        success_result = _bytes_run("100, 200")
        with mock.patch(
            "claude_island.platform_.terminals._macos_common.subprocess.run",
            side_effect=[success_result, timeout_exc],
        ):
            base_t = 1000.0
            # First call: successful → cache populated.
            with mock.patch(
                "claude_island.platform_.terminals._macos_common.time.monotonic",
                return_value=base_t,
            ):
                _macos_common._ui_app_pids()
            # Past TTL → re-query → failure → keep last-known-good.
            with mock.patch(
                "claude_island.platform_.terminals._macos_common.time.monotonic",
                return_value=base_t + 60.0,
            ):
                still_good = _macos_common._ui_app_pids()
        assert still_good == frozenset({100, 200}), (
            "after refresh failure, last-known-good must be served"
        )


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

    def test_returns_process_gone_when_psutil_no_such_process(self):
        # Process raced out between scanner and snapshot build.
        # PROCESS_GONE (not None) lets callers keep FOCUS rather than
        # permanently disabling it for what is just a timing gap.
        with (
            mock.patch.object(_macos_common, "_ui_app_pids",
                              return_value=frozenset({100})),
            mock.patch("psutil.Process",
                       side_effect=psutil.NoSuchProcess(pid=10)),
        ):
            assert _macos_common.find_ui_app_ancestor(10) is _macos_common.PROCESS_GONE

    def test_returns_process_gone_for_placeholder_pid(self):
        """PLACEHOLDER_PID (-1) sessions come from the hook bridge before
        a real process exists. psutil.Process(-1) raises ValueError;
        guarding here keeps focus_host_app(-1) from crashing the click."""
        with (
            mock.patch.object(_macos_common, "_ui_app_pids",
                              return_value=frozenset({100})),
            mock.patch("psutil.Process") as p,
        ):
            assert _macos_common.find_ui_app_ancestor(-1) is _macos_common.PROCESS_GONE
            p.assert_not_called()

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
        # Default _bytes_run has empty stderr so we provide a real one
        # to exercise the new stderr-capturing log line.
        with mock.patch(
            "claude_island.platform_.terminals._macos_common.subprocess.run",
            return_value=mock.Mock(
                stdout=b"",
                stderr=b"some osascript stderr",
                returncode=1,
            ),
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


# ── focus_host_app (S1: shared adapter helper) ────────────────────────

class TestFocusHostApp:
    """The shared fallback helper that iterm2/terminal_app/generic_mac
    all delegate to. Centralising the chain (find ancestor → frontmost)
    makes the per-adapter focus methods one-liners and ensures any
    future fix (logging, telemetry) lands in one place."""

    def test_resolves_ancestor_then_frontmosts_it(self):
        with (
            mock.patch.object(_macos_common, "find_ui_app_ancestor",
                              return_value=5050) as find,
            mock.patch.object(_macos_common, "frontmost_app",
                              return_value=True) as fa,
        ):
            assert _macos_common.focus_host_app(10) is True
            find.assert_called_once_with(10)
            fa.assert_called_once_with(5050)

    def test_returns_false_when_no_ancestor(self):
        """tmux/screen scenario — chain has no UI ancestor. Must NOT
        call ``frontmost_app(None)`` which would error in osascript."""
        with (
            mock.patch.object(_macos_common, "find_ui_app_ancestor",
                              return_value=None),
            mock.patch.object(_macos_common, "frontmost_app") as fa,
        ):
            assert _macos_common.focus_host_app(10) is False
            fa.assert_not_called()

    def test_propagates_frontmost_failure(self):
        with (
            mock.patch.object(_macos_common, "find_ui_app_ancestor",
                              return_value=5050),
            mock.patch.object(_macos_common, "frontmost_app",
                              return_value=False),
        ):
            assert _macos_common.focus_host_app(10) is False


class TestPrewarmCache:
    """B2 mitigation: ``prewarm_ui_pid_cache()`` runs on the worker
    thread inside ``adapter.group()``, so when the user later clicks a
    row the Qt main thread skips the cold-cache osascript (~270 ms
    saved per fallback click)."""

    def test_prewarm_populates_cache(self):
        with mock.patch(
            "claude_island.platform_.terminals._macos_common.subprocess.run",
            return_value=_bytes_run("100, 200"),
        ) as run:
            _macos_common.prewarm_ui_pid_cache()
            # Cache populated; subsequent focus_host_app skips the query.
            assert run.call_count == 1
            with mock.patch("psutil.Process",
                            return_value=_proc(100)):
                ui_pid = _macos_common.find_ui_app_ancestor(100)
        assert ui_pid == 100
        assert run.call_count == 1  # find_ui_app_ancestor hit cache

    def test_prewarm_is_noop_when_cache_warm(self):
        with mock.patch(
            "claude_island.platform_.terminals._macos_common.subprocess.run",
            return_value=_bytes_run("100"),
        ) as run:
            _macos_common.prewarm_ui_pid_cache()
            _macos_common.prewarm_ui_pid_cache()
            _macos_common.prewarm_ui_pid_cache()
        # Three prewarms, only one osascript — cache TTL gates re-query.
        assert run.call_count == 1
