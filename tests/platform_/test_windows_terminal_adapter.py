"""Unit tests for WindowsTerminalAdapter.

Strategy: mock ``win32_console.get_console_info`` and the
``walk_to_visible_host`` helper at the module's import boundary, then
assert call counts to prove the conpty_hwnd cache hits / misses /
GCs as designed.

These tests exist primarily to lock in the F1 cache invariants:

  1. First group() for a pid → AttachConsole called.
  2. Second group() for the same pid → AttachConsole NOT called
     (cache hit), but walk_to_visible_host IS still called
     (so a moved tab is reflected immediately).
  3. wt_hwnd CAN change between ticks for the same pid (drag-tab
     correctness invariant).
  4. Orphan results are NOT cached — re-probed every tick.
  5. pid leaving views is GC'd from the cache.

The class is exercised directly (bypassing the @adapter registry,
which only registers on win32) so the suite runs cross-platform.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

import pytest

from claude_island.core.models import Session
from claude_island.core.snapshot import SessionView, _degraded_view
from claude_island.platform_.terminals.windows_terminal import (
    WindowsTerminalAdapter,
)


# ── Fixtures ──────────────────────────────────────────────────────────

def _session(pid: int = 1234, cwd: str = "C:\\proj") -> Session:
    return Session(
        pid=pid, project_path=Path(cwd), session_uuid="",
        last_activity=datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc),
    )


def _view(pid: int = 1234, cwd: str = "C:\\proj") -> SessionView:
    return _degraded_view(_session(pid, cwd))


@pytest.fixture
def adapter() -> WindowsTerminalAdapter:
    """Bare instance — bypass @adapter registration so tests run on
    non-Windows platforms too. Cache is empty per-test (fresh instance)."""
    a = WindowsTerminalAdapter()
    a.name = "windows-terminal"
    a._priority = 100
    return a


@pytest.fixture
def patched(monkeypatch):
    """Patch the two trust-boundary helpers and return their mocks
    plus a `set_walk` setter that lets a test stage the wt_hwnd
    walk_to_visible_host returns on the *next* call.

    Why a setter and not just call_args: tests want to assert that the
    second tick can return a *different* wt_hwnd for the same pid
    (drag-tab) — so each tick stages its own value before calling group().
    """
    # Force the win32gui import inside group() to succeed with a sentinel
    # object; walk_to_visible_host is patched at the window_activator
    # module level, so the actual identity of win32gui doesn't matter
    # — group() just checks `is not None`.
    win32gui_sentinel = mock.Mock(name="win32gui")
    monkeypatch.setitem(
        __import__("sys").modules, "win32gui", win32gui_sentinel,
    )

    get_console_info = mock.Mock()
    walk = mock.Mock(return_value=None)

    monkeypatch.setattr(
        "claude_island.platform_.win32_console.get_console_info",
        get_console_info,
    )
    monkeypatch.setattr(
        "claude_island.platform_.window_activator.walk_to_visible_host",
        walk,
    )

    class _Bag:
        def __init__(self):
            self.get_console_info = get_console_info
            self.walk = walk

        def set_console(self, pid_to_conpty: dict[int, int | None]):
            """Stage get_console_info(pid) → (conpty_hwnd, '') or None."""
            def _impl(pid):
                conpty = pid_to_conpty.get(pid)
                if conpty is None:
                    return None
                return (conpty, "title")
            self.get_console_info.side_effect = _impl

        def set_walk(self, conpty_to_wt: dict[int, int | None]):
            """Stage walk_to_visible_host(conpty_hwnd, _) → wt_hwnd."""
            def _impl(conpty, _gui):
                return conpty_to_wt.get(conpty)
            self.walk.side_effect = _impl

    return _Bag()


# ── Cache hit / miss ──────────────────────────────────────────────────

class TestConptyCache:

    def test_first_call_invokes_attach_console(self, adapter, patched):
        patched.set_console({1234: 0xAA})
        patched.set_walk({0xAA: 0x11})

        adapter.group([_view(1234)])

        assert patched.get_console_info.call_count == 1
        assert adapter._conpty_cache == {1234: 0xAA}

    def test_second_call_skips_attach_console(self, adapter, patched):
        """Cache hit on the same pid avoids the AttachConsole syscall —
        this is the whole point of the conpty cache."""
        patched.set_console({1234: 0xAA})

        adapter.group([_view(1234)])
        adapter.group([_view(1234)])

        assert patched.get_console_info.call_count == 1
        # walk_to_visible_host is no longer called from group() — it's
        # only invoked at focus-click time inside _activate_windows.
        assert patched.walk.call_count == 0

    def test_multiple_pids_cached_independently(self, adapter, patched):
        patched.set_console({1234: 0xAA, 5678: 0xBB})
        patched.set_walk({0xAA: 0x11, 0xBB: 0x11})

        adapter.group([_view(1234), _view(5678)])
        adapter.group([_view(1234), _view(5678)])

        assert patched.get_console_info.call_count == 2  # once per pid, never again
        assert adapter._conpty_cache == {1234: 0xAA, 5678: 0xBB}


# ── Negative-cache discipline ─────────────────────────────────────────

class TestOrphanReprobing:

    def test_orphan_not_cached(self, adapter, patched):
        """A pid that AttachConsole rejects (orphan / startup race)
        is dropped from the result AND not cached — next tick will
        re-probe so a transient race doesn't permanently hide it."""
        patched.set_console({1234: None})
        patched.set_walk({})

        groups = adapter.group([_view(1234)])

        # views={1234} has only one element → tripwire promotes the
        # filtered-empty list back to a singleton fallback (kept tuple
        # is not empty under the tripwire). So we still get a group.
        # But the pid must NOT be in _conpty_cache.
        assert 1234 not in adapter._conpty_cache
        # And next tick MUST call get_console_info again.
        adapter.group([_view(1234)])
        assert patched.get_console_info.call_count == 2

    def test_orphan_then_recovers(self, adapter, patched):
        """Tick 1: pid is orphan (race) → not cached. Tick 2: pid has
        conPTY now → cached and used from then on."""
        patched.get_console_info.side_effect = [None, (0xAA, "title")]
        patched.set_walk({0xAA: 0x11})

        adapter.group([_view(1234)])
        assert 1234 not in adapter._conpty_cache

        adapter.group([_view(1234)])
        assert adapter._conpty_cache == {1234: 0xAA}

        # Third tick is a pure cache hit.
        adapter.group([_view(1234)])
        assert patched.get_console_info.call_count == 2


# ── GC ────────────────────────────────────────────────────────────────

class TestCacheGC:

    def test_pid_leaving_views_is_evicted(self, adapter, patched):
        patched.set_console({1234: 0xAA, 5678: 0xBB})
        patched.set_walk({0xAA: 0x11, 0xBB: 0x11})

        adapter.group([_view(1234), _view(5678)])
        assert set(adapter._conpty_cache.keys()) == {1234, 5678}

        # 5678 disappears (process exited)
        adapter.group([_view(1234)])
        assert set(adapter._conpty_cache.keys()) == {1234}

    def test_empty_views_clears_cache(self, adapter, patched):
        patched.set_console({1234: 0xAA})
        patched.set_walk({0xAA: 0x11})

        adapter.group([_view(1234)])
        assert adapter._conpty_cache

        adapter.group([])
        assert adapter._conpty_cache == {}

    def test_returning_pid_repopulates_cache(self, adapter, patched):
        """A pid GC'd then re-appearing pays one AttachConsole again
        (this is correct: it might literally be a new process with
        the same numeric pid after the OS reused the slot)."""
        patched.set_console({1234: 0xAA})
        patched.set_walk({0xAA: 0x11})

        adapter.group([_view(1234)])
        adapter.group([])  # pid leaves
        adapter.group([_view(1234)])  # pid back

        assert patched.get_console_info.call_count == 2


# ── Singleton-grouping invariant ──────────────────────────────────────

class TestSingletonGrouping:
    """Lock in the post-bug-fix contract: every view becomes its own
    SessionGroup, regardless of cwd or any wt-window relationship.

    This is the regression guard for the bug where two `claude` sessions
    launched from the same project root in two tabs of the same WT window
    were silently merged into one card, and the inactive tab's click
    routed to its sibling. See group() docstring for the WinUI3 / UIA
    rationale that forced singleton-only grouping on Windows."""

    def test_each_view_is_its_own_group(self, adapter, patched):
        """Two live views with the same cwd → two distinct groups."""
        patched.set_console({1234: 0xAA, 5678: 0xBB})

        groups = adapter.group([_view(1234, "C:\\proj"), _view(5678, "C:\\proj")])

        assert len(groups) == 2
        assert {g.group_id for g in groups} == {"wt:1234", "wt:5678"}
        # Each group holds exactly one view.
        for g in groups:
            assert len(g.views) == 1

    def test_same_cwd_does_not_merge_regression(self, adapter, patched):
        """Regression: dev + dev2 in two tabs of the same WT window,
        both cd'd to the same project. Pre-fix this collapsed into a
        single 2-view group with title_hint set; click on inactive
        tab routed to active tab via sibling fallback. Now: each
        gets its own card, each focus click routes to its own pid."""
        patched.set_console({100: 0xA0, 200: 0xB0})

        groups = adapter.group([
            _view(100, "D:\\coding projects\\claude-island"),
            _view(200, "D:\\coding projects\\claude-island"),
        ])

        # Two views, two groups, no merging.
        assert len(groups) == 2
        all_pids = {v.pid for g in groups for v in g.views}
        assert all_pids == {100, 200}
        # No multi-view group with merged title hint.
        for g in groups:
            assert g.title_hint is None
            assert len(g.views) == 1

    def test_orphan_dropped_live_kept_as_singleton(self, adapter, patched):
        """Orphan filter is preserved by singleton refactor: a pid
        whose AttachConsole fails is dropped; surviving pids each
        become their own group."""
        patched.set_console({100: 0xA0, 200: None, 300: 0xC0})

        groups = adapter.group([_view(100), _view(200), _view(300)])

        kept_pids = {v.pid for g in groups for v in g.views}
        assert kept_pids == {100, 300}
        # Each survivor is its own singleton.
        assert len(groups) == 2


# ── Phase 4 (resume-offline): LAUNCH capability ──────────────────────────

class TestWindowsTerminalLaunch:
    """Verify the @capability(LAUNCH) launch method on WindowsTerminalAdapter.

    All tests mock subprocess.Popen so wt.exe never actually spawns —
    keeps the suite cross-platform and CI-friendly."""

    def test_launch_advertised_in_capabilities(self):
        """The @capability decorator + _CapabilityProvider mixin should
        add LAUNCH to the class-level capabilities frozenset."""
        from claude_island.core.capabilities import Capability
        assert Capability.LAUNCH in WindowsTerminalAdapter.capabilities

    def test_launch_calls_wt_exe_with_correct_argv(self):
        from claude_island.core.capabilities import SpawnResult
        adapter = WindowsTerminalAdapter()

        with mock.patch(
            "claude_island.platform_.terminals.windows_terminal.shutil.which",
            return_value="C:\\Windows\\System32\\wt.exe",
        ), mock.patch(
            "claude_island.platform_.terminals.windows_terminal.subprocess.Popen",
        ) as mock_popen:
            mock_popen.return_value.pid = 9999

            result = adapter.launch(
                cwd=Path("D:/proj with space/foo"),
                command=("claude", "--resume", "u1", "--dangerously-skip-permissions"),
            )

        assert isinstance(result, SpawnResult)
        assert result.terminal_pid == 9999
        assert result.terminal_name == adapter.name

        # Argv must be wt.exe -d <cwd> -- cmd.exe /k claude --resume <uuid> [flags]
        # cmd.exe wrapper is REQUIRED, not a convenience: WT spawns the
        # new tab via CreateProcessW which doesn't walk PATHEXT, so the
        # bare "claude" (which is "claude.cmd" on npm installs) raises
        # ERROR_FILE_NOT_FOUND (0x80070002) the moment Resume is clicked.
        # cmd.exe walks PATHEXT and resolves it.
        call_args = mock_popen.call_args
        argv = call_args[0][0]
        assert argv[0] == "wt.exe"
        assert argv[1] == "-d"
        assert argv[2] == "D:\\proj with space\\foo"  # str(Path) on Windows-style
        assert argv[3] == "--"
        assert argv[4] == "cmd.exe"
        assert argv[5] == "/k"
        assert argv[6:] == [
            "claude", "--resume", "u1", "--dangerously-skip-permissions",
        ]

    def test_launch_uses_slash_k_not_slash_c(self):
        """``/k`` (keep window) is intentional — claude crashing must
        leave the error visible. ``/c`` would close the window the
        instant claude exits and hide the diagnostic. Pinned so a
        future "cleanup" PR doesn't silently flip it back to ``/c``."""
        adapter = WindowsTerminalAdapter()
        with mock.patch(
            "claude_island.platform_.terminals.windows_terminal.shutil.which",
            return_value="C:\\Windows\\System32\\wt.exe",
        ), mock.patch(
            "claude_island.platform_.terminals.windows_terminal.subprocess.Popen",
        ) as mock_popen:
            mock_popen.return_value.pid = 1
            adapter.launch(cwd=Path("D:/x"), command=("claude",))
        argv = mock_popen.call_args[0][0]
        assert "/k" in argv
        assert "/c" not in argv

    def test_launch_raises_when_wt_exe_missing(self):
        from claude_island.core.capabilities import LauncherSpawnError
        adapter = WindowsTerminalAdapter()
        with mock.patch(
            "claude_island.platform_.terminals.windows_terminal.shutil.which",
            return_value=None,
        ):
            with pytest.raises(LauncherSpawnError, match="not found"):
                adapter.launch(cwd=Path("D:/x"), command=("claude",))

    def test_launch_wraps_oserror_as_launcher_spawn_error(self):
        from claude_island.core.capabilities import LauncherSpawnError
        adapter = WindowsTerminalAdapter()
        with mock.patch(
            "claude_island.platform_.terminals.windows_terminal.shutil.which",
            return_value="C:/wt.exe",
        ), mock.patch(
            "claude_island.platform_.terminals.windows_terminal.subprocess.Popen",
            side_effect=OSError("permission denied"),
        ):
            with pytest.raises(LauncherSpawnError, match="permission denied"):
                adapter.launch(cwd=Path("D:/x"), command=("claude",))
