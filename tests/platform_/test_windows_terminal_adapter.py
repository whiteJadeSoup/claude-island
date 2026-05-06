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

def _session(
    pid: int = 1234, cwd: str = "C:\\proj", session_uuid: str = "",
) -> Session:
    return Session(
        pid=pid, project_path=Path(cwd), session_uuid=session_uuid,
        last_activity=datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc),
    )


def _view(
    pid: int = 1234, cwd: str = "C:\\proj", session_uuid: str = "",
) -> SessionView:
    return _degraded_view(_session(pid, cwd, session_uuid))


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
    # Default: pretend SetConsoleTitleW always succeeds. Tests that
    # need to inspect the calls assert against this mock.
    set_console_title = mock.Mock(return_value=True)

    monkeypatch.setattr(
        "claude_island.platform_.win32_console.get_console_info",
        get_console_info,
    )
    monkeypatch.setattr(
        "claude_island.platform_.win32_console.set_console_title",
        set_console_title,
    )
    monkeypatch.setattr(
        "claude_island.platform_.window_activator.walk_to_visible_host",
        walk,
    )

    class _Bag:
        def __init__(self):
            self.get_console_info = get_console_info
            self.set_console_title = set_console_title
            self.walk = walk

        def set_console(
            self,
            pid_to_conpty: dict[int, int | None],
            *,
            title: str = "title",
        ):
            """Stage get_console_info(pid) → (conpty_hwnd, title) or None.

            Default title is non-sentinel so the reconcile path triggers
            (set_console_title gets called). Pass ``title="ci:..."`` to
            simulate a session whose tab is already labeled and reconcile
            should skip.
            """
            def _impl(pid):
                conpty = pid_to_conpty.get(pid)
                if conpty is None:
                    return None
                return (conpty, title)
            self.get_console_info.side_effect = _impl

        def set_console_per_pid(self, pid_to_info: dict[int, tuple[int, str] | None]):
            """Stage per-pid (conpty_hwnd, title) — for tests that want
            different titles per session."""
            def _impl(pid):
                return pid_to_info.get(pid)
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

        # AttachConsole is cached; only the first call probes.
        assert patched.get_console_info.call_count == 1
        # walk_to_visible_host now runs every group() to feed the
        # sibling tracker (one walk per cached pid per wake). So 2
        # ticks × 1 pid = 2 walks. This is independent of the
        # AttachConsole cache (walk reads the cached conpty_hwnd).
        assert patched.walk.call_count == 2

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


# ── Plan O sentinel reconcile ─────────────────────────────────────────

class TestSentinelReconcile:
    """group() establishes a unique 'ci:{uuid}' tab title on first
    sight of each session, so click-time UIA name match is precise.

    Reconcile only fires on cache miss (first sight of a pid) — cache
    hits skip the syscall. claude topic-shift recovery is the click-
    time _activate_windows path, not group()."""

    UUID = "a1b2c3d4-e5f6-7890-abcd-ef1234567890"
    EXPECTED = "ci:a1b2c3d4e5f67890abcdef1234567890"

    def test_first_call_writes_sentinel_when_title_drifts(self, adapter, patched):
        """Cache miss + non-sentinel current title → set_console_title
        called with the uuid-derived sentinel."""
        patched.set_console({1234: 0xAA}, title="Claude Code")

        adapter.group([_view(1234, session_uuid=self.UUID)])

        patched.set_console_title.assert_called_once_with(1234, self.EXPECTED)

    def test_first_call_skips_when_title_already_sentinel(self, adapter, patched):
        """Cache miss but title already = our sentinel (e.g. session
        was launched via Plan L which sets title at WT spawn time, or
        we labeled it on a previous run that didn't outlive the
        process). Skip the set_console_title syscall."""
        patched.set_console({1234: 0xAA}, title=self.EXPECTED)

        adapter.group([_view(1234, session_uuid=self.UUID)])

        patched.set_console_title.assert_not_called()

    def test_skips_when_session_uuid_empty(self, adapter, patched):
        """Degraded SessionView with no uuid (scanner caught process
        before its JSONL was parsed) → no stable identity → skip."""
        patched.set_console({1234: 0xAA}, title="Claude Code")

        adapter.group([_view(1234, session_uuid="")])

        patched.set_console_title.assert_not_called()

    def test_cache_hit_does_not_re_reconcile(self, adapter, patched):
        """Second tick on the same pid → cache hit → no AttachConsole,
        no SetConsoleTitleW. claude topic-shift recovery is handled by
        the click-time _activate_windows fallback, not by re-probing
        every wake."""
        patched.set_console({1234: 0xAA}, title="Claude Code")

        adapter.group([_view(1234, session_uuid=self.UUID)])
        adapter.group([_view(1234, session_uuid=self.UUID)])

        # First tick set, second tick must NOT.
        assert patched.set_console_title.call_count == 1

    def test_orphan_skips_reconcile(self, adapter, patched):
        """Orphan pid (AttachConsole fails) is dropped before reconcile
        — never SetConsoleTitleW on a pid we can't even attach to."""
        patched.set_console({1234: None}, title="ignored")

        adapter.group([_view(1234, session_uuid=self.UUID)])

        patched.set_console_title.assert_not_called()

    def test_failed_set_does_not_pollute_cache(self, adapter, patched):
        """Cache discipline: if set_console_title fails (silent fail
        under suppressApplicationTitle profile, transient WT busy),
        the conpty_hwnd MUST NOT be cached — otherwise the next wake's
        cache hit would skip the retry forever and the tab would stay
        un-labeled. AttachConsole on next wake costs ~3ms; cheap
        insurance vs permanently mislabeled tabs."""
        patched.set_console({1234: 0xAA}, title="Claude Code")
        patched.set_console_title.return_value = False  # silent fail

        adapter.group([_view(1234, session_uuid=self.UUID)])

        # set_console_title was attempted...
        patched.set_console_title.assert_called_once()
        # ...but cache must be empty so next tick retries.
        assert adapter._conpty_cache == {}

    def test_failed_set_retries_on_next_wake(self, adapter, patched):
        """Direct consequence of cache discipline: a failed set on
        tick 1 means tick 2 re-probes and re-attempts."""
        patched.set_console({1234: 0xAA}, title="Claude Code")
        patched.set_console_title.return_value = False

        adapter.group([_view(1234, session_uuid=self.UUID)])
        adapter.group([_view(1234, session_uuid=self.UUID)])

        # Both probe AND set called twice — no cache shortcut.
        assert patched.get_console_info.call_count == 2
        assert patched.set_console_title.call_count == 2

    def test_already_sentinel_caches_without_set(self, adapter, patched):
        """If title is already a sentinel (Plan-L launched, or we set
        it on a previous run that survived), no set is attempted but
        the cache IS populated — set_ok defaults to True when no set
        was needed."""
        patched.set_console({1234: 0xAA}, title=self.EXPECTED)

        adapter.group([_view(1234, session_uuid=self.UUID)])

        patched.set_console_title.assert_not_called()
        assert adapter._conpty_cache == {1234: 0xAA}

    def test_multi_pids_reconcile_independently(self, adapter, patched):
        """Two new sessions: each gets its own sentinel set. Different
        uuids → different sentinel titles."""
        uuid_a = "a" * 32
        uuid_b = "b" * 32
        patched.set_console_per_pid({
            100: (0xA0, "Claude Code"),
            200: (0xB0, "Claude Code"),
        })

        adapter.group([
            _view(100, session_uuid=uuid_a),
            _view(200, session_uuid=uuid_b),
        ])

        assert patched.set_console_title.call_count == 2
        calls = {
            (c.args[0], c.args[1])
            for c in patched.set_console_title.call_args_list
        }
        assert calls == {(100, f"ci:{uuid_a}"), (200, f"ci:{uuid_b}")}


# ── Sibling tracker integration ───────────────────────────────────────

class TestSiblingTrackerIntegration:
    """group() must drive the PaneSiblingTracker so the click-time
    fallback chain has fresh sibling info to work with.

    These tests replace the adapter's tracker with a Mock so we can
    assert the calls without running real UIA."""

    UUID = "a1b2c3d4" + "0" * 24

    def test_group_calls_update_for_each_unique_wt_hwnd(
        self, adapter, patched,
    ):
        """One walk_to_visible_host per distinct wt_hwnd; tracker
        update fires for each unique hwnd (not per-pid duplicates)."""
        # Two sessions in same WT window (same wt_hwnd).
        patched.set_console({100: 0xA0, 200: 0xB0})
        patched.set_walk({0xA0: 0x1111, 0xB0: 0x1111})  # same wt_hwnd

        tracker = mock.Mock()
        adapter._sibling_tracker = tracker

        adapter.group([
            _view(100, session_uuid="a" * 32),
            _view(200, session_uuid="b" * 32),
        ])

        # Both pids walked, but only one unique wt_hwnd → one update.
        tracker.update_from_active_tab.assert_called_once_with(0x1111)

    def test_group_update_called_per_distinct_wt_hwnd(
        self, adapter, patched,
    ):
        """Two WT windows → two updates (one per window)."""
        patched.set_console({100: 0xA0, 200: 0xB0})
        patched.set_walk({0xA0: 0x1111, 0xB0: 0x2222})  # different wt_hwnds

        tracker = mock.Mock()
        adapter._sibling_tracker = tracker

        adapter.group([
            _view(100, session_uuid="a" * 32),
            _view(200, session_uuid="b" * 32),
        ])

        assert tracker.update_from_active_tab.call_count == 2
        called_hwnds = {
            c.args[0] for c in tracker.update_from_active_tab.call_args_list
        }
        assert called_hwnds == {0x1111, 0x2222}

    def test_group_skips_update_when_walk_returns_none(
        self, adapter, patched,
    ):
        """If walk_to_visible_host can't resolve (orphan / WT gone),
        tracker is not called for that pid."""
        patched.set_console({100: 0xA0})
        patched.set_walk({0xA0: None})  # walk fails

        tracker = mock.Mock()
        adapter._sibling_tracker = tracker

        adapter.group([_view(100, session_uuid="a" * 32)])

        tracker.update_from_active_tab.assert_not_called()


class TestFocusPassesSiblingSentinels:
    """focus() must read the tracker's cache for the clicked view's
    sentinel and forward it to _activate_windows."""

    UUID = "a" * 32
    EXPECTED = f"ci:{UUID}"

    def test_focus_forwards_cached_siblings(self, monkeypatch):
        """tracker.siblings_of returns {ci:sib} → focus passes it
        through as sibling_sentinels."""
        from claude_island.core.capabilities import FocusGranularity
        from claude_island.platform_.terminals.windows_terminal import (
            WindowsTerminalAdapter,
        )

        adapter = WindowsTerminalAdapter()
        adapter.name = "windows-terminal"
        adapter._priority = 100

        # Stub the tracker.
        tracker = mock.Mock()
        tracker.siblings_of.return_value = {"ci:sib_uuid"}
        adapter._sibling_tracker = tracker

        # Stub _activate_windows to capture the call.
        captured: dict = {}
        def _stub_activate(pid, **kwargs):
            captured["pid"] = pid
            captured.update(kwargs)
            return True
        monkeypatch.setattr(
            "claude_island.platform_.terminals.windows_terminal._activate_windows",
            _stub_activate,
        )

        view = _view(999, session_uuid=self.UUID)
        # Stamp the FocusGranularity / adapter_id as group() would.
        from dataclasses import replace
        view = replace(
            view,
            adapter_id=adapter.name,
            focus_granularity=FocusGranularity.TAB,
        )

        adapter.focus(view)

        tracker.siblings_of.assert_called_once_with(self.EXPECTED)
        assert captured["pid"] == 999
        assert captured["expected_title"] == self.EXPECTED
        assert set(captured["sibling_sentinels"]) == {"ci:sib_uuid"}
        # tracker reference forwarded for fire-and-forget refresh.
        assert captured["sibling_tracker"] is tracker

    def test_focus_with_no_uuid_skips_sibling_lookup(self, monkeypatch):
        """Degraded view (empty uuid) → no sentinel → no sibling
        cache lookup."""
        from claude_island.platform_.terminals.windows_terminal import (
            WindowsTerminalAdapter,
        )

        adapter = WindowsTerminalAdapter()
        adapter.name = "windows-terminal"
        tracker = mock.Mock()
        adapter._sibling_tracker = tracker

        captured: dict = {}
        def _stub_activate(pid, **kwargs):
            captured.update(kwargs)
            return True
        monkeypatch.setattr(
            "claude_island.platform_.terminals.windows_terminal._activate_windows",
            _stub_activate,
        )

        view = _view(999, session_uuid="")
        adapter.focus(view)

        tracker.siblings_of.assert_not_called()
        assert captured["expected_title"] is None
        assert captured["sibling_sentinels"] == ()


# ── _activate_windows click-time reconcile ────────────────────────────

class TestActivateWindowsReconcile:
    """The click-time fallback in module-level _activate_windows: if the
    current console title doesn't match the expected sentinel (claude
    topic-shift since last group() reconcile, or first click on a
    just-discovered session), re-set it and wait for WT to mirror the
    OSC update into TabItem.Name before issuing select_tab_by_title.

    These tests exercise the module-level _activate_windows function in
    windows_terminal.py — not the older WindowActivator class method
    in window_activator.py (which is only kept around for legacy tests
    of generic_windows-style ancestor-walk activation)."""

    UUID = "a1b2c3d4-e5f6-7890-abcd-ef1234567890"
    EXPECTED = "ci:a1b2c3d4e5f67890abcdef1234567890"

    @pytest.fixture
    def patched_activate(self, monkeypatch):
        """Patch every Win32/UIA touchpoint inside _activate_windows."""
        # win32* modules — _activate_windows imports them lazily.
        win32con = mock.MagicMock(name="win32con")
        win32gui = mock.MagicMock(name="win32gui")
        win32process = mock.MagicMock(name="win32process")
        monkeypatch.setitem(__import__("sys").modules, "win32con", win32con)
        monkeypatch.setitem(__import__("sys").modules, "win32gui", win32gui)
        monkeypatch.setitem(__import__("sys").modules, "win32process", win32process)

        # The pieces we want to inspect.
        get_console_info = mock.Mock()
        set_console_title = mock.Mock(return_value=True)
        wait_for_tab_name = mock.Mock(return_value=True)
        select_tab_by_title = mock.Mock(return_value=True)
        force_foreground = mock.Mock(return_value=True)
        walk_to_visible_host = mock.Mock(return_value=0xCAFE)  # WT hwnd

        monkeypatch.setattr(
            "claude_island.platform_.win32_console.get_console_info",
            get_console_info,
        )
        monkeypatch.setattr(
            "claude_island.platform_.win32_console.set_console_title",
            set_console_title,
        )
        monkeypatch.setattr(
            "claude_island.platform_.wt_uia.wait_for_tab_name",
            wait_for_tab_name,
        )
        monkeypatch.setattr(
            "claude_island.platform_.wt_uia.select_tab_by_title",
            select_tab_by_title,
        )
        monkeypatch.setattr(
            "claude_island.platform_.window_activator._force_foreground",
            force_foreground,
        )
        monkeypatch.setattr(
            "claude_island.platform_.window_activator.walk_to_visible_host",
            walk_to_visible_host,
        )

        class _Bag:
            pass
        bag = _Bag()
        bag.get_console_info = get_console_info
        bag.set_console_title = set_console_title
        bag.wait_for_tab_name = wait_for_tab_name
        bag.select_tab_by_title = select_tab_by_title
        bag.force_foreground = force_foreground
        return bag

    def test_no_reconcile_when_current_title_matches_expected(
        self, patched_activate,
    ):
        """Common path: scanner already labeled the tab, current title
        equals expected. Skip set + poll, go straight to select."""
        from claude_island.platform_.terminals.windows_terminal import (
            _activate_windows,
        )

        patched_activate.get_console_info.return_value = (0xAA, self.EXPECTED)

        ok = _activate_windows(pid=999, expected_title=self.EXPECTED)

        assert ok is True
        patched_activate.set_console_title.assert_not_called()
        patched_activate.wait_for_tab_name.assert_not_called()
        patched_activate.select_tab_by_title.assert_called_once_with(
            0xCAFE, self.EXPECTED,
        )

    def test_reconcile_when_title_drifted(self, patched_activate):
        """claude topic-shifted: current title is "✳ memory", expected
        is sentinel. Re-set, poll, then select using expected."""
        from claude_island.platform_.terminals.windows_terminal import (
            _activate_windows,
        )

        patched_activate.get_console_info.return_value = (0xAA, "✳ memory")

        ok = _activate_windows(pid=999, expected_title=self.EXPECTED)

        assert ok is True
        patched_activate.set_console_title.assert_called_once_with(
            999, self.EXPECTED,
        )
        patched_activate.wait_for_tab_name.assert_called_once()
        patched_activate.select_tab_by_title.assert_called_once_with(
            0xCAFE, self.EXPECTED,
        )

    def test_force_foreground_called_even_when_select_fails(
        self, patched_activate,
    ):
        """suppressApplicationTitle profile: set_console_title silently
        no-ops, wait_for_tab_name times out, select_tab_by_title fails.
        Still call _force_foreground so user at least sees WT come up."""
        from claude_island.platform_.terminals.windows_terminal import (
            _activate_windows,
        )

        patched_activate.get_console_info.return_value = (0xAA, "Claude Code")
        patched_activate.wait_for_tab_name.return_value = False
        patched_activate.select_tab_by_title.return_value = False

        ok = _activate_windows(pid=999, expected_title=self.EXPECTED)

        # Returns whatever _force_foreground returns (True in this fixture).
        assert ok is True
        patched_activate.force_foreground.assert_called_once()

    def test_no_expected_title_falls_back_to_current(self, patched_activate):
        """Backward compat: caller didn't pass expected_title (degraded
        SessionView with no uuid) → use whatever current title is, no
        reconcile attempt."""
        from claude_island.platform_.terminals.windows_terminal import (
            _activate_windows,
        )

        patched_activate.get_console_info.return_value = (0xAA, "some title")

        _activate_windows(pid=999, expected_title=None)

        patched_activate.set_console_title.assert_not_called()
        patched_activate.select_tab_by_title.assert_called_once_with(
            0xCAFE, "some title",
        )

    def test_sibling_sentinel_tried_when_primary_select_fails(
        self, patched_activate,
    ):
        """Split-pane click: select(my sentinel) fails → try cached
        sibling sentinels in order → first one that hits wins."""
        from claude_island.platform_.terminals.windows_terminal import (
            _activate_windows,
        )

        patched_activate.get_console_info.return_value = (0xAA, self.EXPECTED)
        # Primary select fails; first sibling fails too; second hits.
        patched_activate.select_tab_by_title.side_effect = [
            False,  # primary
            False,  # sibling 1
            True,   # sibling 2
        ]

        ok = _activate_windows(
            pid=999,
            expected_title=self.EXPECTED,
            sibling_sentinels=("ci:sib1_uuid", "ci:sib2_uuid"),
        )

        assert ok is True
        # 3 calls: primary + 2 siblings.
        assert patched_activate.select_tab_by_title.call_count == 3
        calls = [c.args for c in patched_activate.select_tab_by_title.call_args_list]
        assert calls[0] == (0xCAFE, self.EXPECTED)
        assert calls[1] == (0xCAFE, "ci:sib1_uuid")
        assert calls[2] == (0xCAFE, "ci:sib2_uuid")
        patched_activate.force_foreground.assert_called_once()

    def test_sibling_sentinels_not_tried_when_primary_succeeds(
        self, patched_activate,
    ):
        """Fast path: primary select hits → siblings never tried."""
        from claude_island.platform_.terminals.windows_terminal import (
            _activate_windows,
        )

        patched_activate.get_console_info.return_value = (0xAA, self.EXPECTED)
        patched_activate.select_tab_by_title.return_value = True

        _activate_windows(
            pid=999,
            expected_title=self.EXPECTED,
            sibling_sentinels=("ci:sib_uuid",),
        )

        # Only the primary call.
        assert patched_activate.select_tab_by_title.call_count == 1

    def test_tracker_schedule_update_fires_on_full_miss(
        self, patched_activate,
    ):
        """All select calls miss → tracker.schedule_update is fired
        for next-click cache repair. force_foreground still called
        for visual feedback."""
        from claude_island.platform_.terminals.windows_terminal import (
            _activate_windows,
        )

        patched_activate.get_console_info.return_value = (0xAA, self.EXPECTED)
        patched_activate.select_tab_by_title.return_value = False

        tracker = mock.Mock()

        ok = _activate_windows(
            pid=999,
            expected_title=self.EXPECTED,
            sibling_sentinels=("ci:sib_uuid",),
            sibling_tracker=tracker,
        )

        assert ok is True  # force_foreground returned True
        tracker.schedule_update.assert_called_once_with(0xCAFE)
        patched_activate.force_foreground.assert_called_once()

    def test_tracker_not_fired_when_primary_hits(self, patched_activate):
        """Common path: primary hit → no schedule_update wasted."""
        from claude_island.platform_.terminals.windows_terminal import (
            _activate_windows,
        )

        patched_activate.get_console_info.return_value = (0xAA, self.EXPECTED)
        patched_activate.select_tab_by_title.return_value = True

        tracker = mock.Mock()

        _activate_windows(
            pid=999,
            expected_title=self.EXPECTED,
            sibling_tracker=tracker,
        )

        tracker.schedule_update.assert_not_called()

    def test_tracker_not_fired_when_sibling_hits(self, patched_activate):
        """A sibling hit means the cache was good — don't refresh."""
        from claude_island.platform_.terminals.windows_terminal import (
            _activate_windows,
        )

        patched_activate.get_console_info.return_value = (0xAA, self.EXPECTED)
        patched_activate.select_tab_by_title.side_effect = [False, True]

        tracker = mock.Mock()

        _activate_windows(
            pid=999,
            expected_title=self.EXPECTED,
            sibling_sentinels=("ci:sib_uuid",),
            sibling_tracker=tracker,
        )

        tracker.schedule_update.assert_not_called()

    def test_tracker_schedule_update_failure_does_not_raise(
        self, patched_activate,
    ):
        """If schedule_update raises (degraded tracker), _activate_windows
        must still return cleanly — tracker is best-effort."""
        from claude_island.platform_.terminals.windows_terminal import (
            _activate_windows,
        )

        patched_activate.get_console_info.return_value = (0xAA, self.EXPECTED)
        patched_activate.select_tab_by_title.return_value = False

        tracker = mock.Mock()
        tracker.schedule_update.side_effect = RuntimeError("tracker died")

        # Should not raise.
        _activate_windows(
            pid=999,
            expected_title=self.EXPECTED,
            sibling_tracker=tracker,
        )
        patched_activate.force_foreground.assert_called_once()


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

    def test_launch_with_session_uuid_adds_plan_l_flags(self):
        """Plan L: when session_uuid is provided (Resume of a known
        dormant session), argv must include --title and
        --suppressApplicationTitle BEFORE the ``--`` separator. This
        locks the WT tab title to ``ci:{uuid}`` for life so click-time
        UIA name match is always exact, with no Plan-O reconcile
        needed for tabs we spawned."""
        adapter = WindowsTerminalAdapter()
        uuid = "a1b2c3d4-e5f6-7890-abcd-ef1234567890"
        expected_title = "ci:a1b2c3d4e5f67890abcdef1234567890"

        with mock.patch(
            "claude_island.platform_.terminals.windows_terminal.shutil.which",
            return_value="C:/wt.exe",
        ), mock.patch(
            "claude_island.platform_.terminals.windows_terminal.subprocess.Popen",
        ) as mock_popen:
            mock_popen.return_value.pid = 1
            adapter.launch(
                cwd=Path("D:/x"),
                command=("claude", "--resume", uuid),
                session_uuid=uuid,
            )

        argv = mock_popen.call_args[0][0]
        # Plan-L flags must come before "--" (they configure the
        # new-tab subcommand, not the spawned process).
        assert "--title" in argv
        title_idx = argv.index("--title")
        assert argv[title_idx + 1] == expected_title
        assert "--suppressApplicationTitle" in argv
        # Both flags before the separator.
        sep_idx = argv.index("--")
        assert title_idx < sep_idx
        assert argv.index("--suppressApplicationTitle") < sep_idx

    def test_launch_without_session_uuid_omits_plan_l_flags(self):
        """Backward compat: caller didn't pass session_uuid (defensive
        — every Resume call should, but don't hard-fail). Skip
        Plan-L flags so the tab degrades to Plan-O reconcile (group()
        sentinel write on first sight)."""
        adapter = WindowsTerminalAdapter()
        with mock.patch(
            "claude_island.platform_.terminals.windows_terminal.shutil.which",
            return_value="C:/wt.exe",
        ), mock.patch(
            "claude_island.platform_.terminals.windows_terminal.subprocess.Popen",
        ) as mock_popen:
            mock_popen.return_value.pid = 1
            adapter.launch(cwd=Path("D:/x"), command=("claude",))

        argv = mock_popen.call_args[0][0]
        assert "--title" not in argv
        assert "--suppressApplicationTitle" not in argv
