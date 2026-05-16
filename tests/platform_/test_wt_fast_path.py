"""Unit tests for WT focus fast-path module.

Strategy: monkeypatch the platform-specific symbols (`_pythoncom`,
`_win32_console`, `_wt_uia`) so the tests run on any OS. The dep
probe (`_ensure_deps`) is bypassed by setting `_HAS_DEPS = True`
manually plus injecting fake module objects.

Caveat noted in design decision doc: real COM apartment behavior
under a Windows STA is NOT exercised here — these tests verify
control flow only. Verified-on-Windows assertions are marked
``TODO(windows-verify)``.
"""
from __future__ import annotations

import logging
import threading
from unittest import mock

import pytest

from claude_island.platform_.terminals import _wt_fast_path as wfp


# ─────────────────────────────────────────────────────────────────────
# Fakes
# ─────────────────────────────────────────────────────────────────────


class _FakePythoncom:
    """Stand-in for pythoncom module. CoInitializeEx is recorded."""

    COINIT_APARTMENTTHREADED = 0x2

    def __init__(self, *, raise_on_init: Exception | None = None) -> None:
        self.coinitex_calls: list[int] = []
        self.coinit_calls = 0
        self._raise = raise_on_init

    def CoInitializeEx(self, flag):
        self.coinitex_calls.append(flag)
        if self._raise is not None:
            raise self._raise

    def CoInitialize(self):
        self.coinit_calls += 1


class _FakeWtUia:
    """Stand-in for wt_uia module — records calls + returns staged values."""

    def __init__(self) -> None:
        self.select_calls: list[tuple[int, str]] = []
        self.select_results: list[bool] = []  # default: all False
        self.wait_calls: list[tuple[int, str, int]] = []
        self.wait_results: list[bool] = []
        self.list_calls: list[int] = []
        self.list_results: list[set[str]] = []

    def select_tab_by_title(self, hwnd, title):
        self.select_calls.append((hwnd, title))
        if self.select_results:
            return self.select_results.pop(0)
        return False

    def wait_for_tab_name(self, hwnd, name, *, timeout_ms=80, poll_ms=10):
        self.wait_calls.append((hwnd, name, timeout_ms))
        if self.wait_results:
            return self.wait_results.pop(0)
        return False

    def list_ci_tab_names(self, hwnd):
        self.list_calls.append(hwnd)
        if self.list_results:
            return self.list_results.pop(0)
        return set()


class _FakeWin32Console:
    def __init__(self) -> None:
        self.set_calls: list[tuple[int, str]] = []
        self.set_results: list[bool] = []

    def set_console_title(self, pid, title):
        self.set_calls.append((pid, title))
        if self.set_results:
            return self.set_results.pop(0)
        return True


# ─────────────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────────────


@pytest.fixture
def patch_deps(monkeypatch):
    """Install fake deps + mark _HAS_DEPS True so try_schedule runs."""
    fake_pythoncom = _FakePythoncom()
    fake_console = _FakeWin32Console()
    fake_uia = _FakeWtUia()

    monkeypatch.setattr(wfp, "_HAS_DEPS", True)
    monkeypatch.setattr(wfp, "_pythoncom", fake_pythoncom)
    monkeypatch.setattr(wfp, "_win32_console", fake_console)
    monkeypatch.setattr(wfp, "_wt_uia", fake_uia)
    monkeypatch.setattr(wfp, "_COINIT_APARTMENTTHREADED", 0x2)
    monkeypatch.setattr(wfp, "_worker_singleton", None)
    # Reset thread-local COM flag so tests can re-init.
    if hasattr(wfp._thread_local, "com_initialized"):
        monkeypatch.delattr(wfp._thread_local, "com_initialized")

    yield fake_pythoncom, fake_console, fake_uia

    # Cleanup worker pool.
    if wfp._worker_singleton is not None:
        try:
            wfp._worker_singleton.shutdown(timeout_ms=2000)
        except Exception:
            pass


# ─────────────────────────────────────────────────────────────────────
# _ensure_deps gating
# ─────────────────────────────────────────────────────────────────────


class TestDepsGate:
    def test_non_windows_returns_false(self, monkeypatch):
        # _HAS_DEPS not set → _ensure_deps probes sys.platform == "win32"
        monkeypatch.setattr(wfp, "_HAS_DEPS", None)
        monkeypatch.setattr("sys.platform", "darwin")
        assert wfp._ensure_deps() is False

    def test_cached_false_short_circuits(self, monkeypatch):
        monkeypatch.setattr(wfp, "_HAS_DEPS", False)
        # Even though we set platform to win32, cached False wins.
        monkeypatch.setattr("sys.platform", "win32")
        assert wfp._ensure_deps() is False

    def test_try_schedule_returns_false_without_deps(self, monkeypatch):
        monkeypatch.setattr(wfp, "_HAS_DEPS", False)
        result = wfp.try_schedule(
            pid=123, wt_hwnd=999, expected_title="ci:abc",
            sibling_sentinels=(),
        )
        assert result is False


# ─────────────────────────────────────────────────────────────────────
# _WtFocusTask constructor invariants
# ─────────────────────────────────────────────────────────────────────


class TestTaskInvariants:
    def test_zero_wt_hwnd_raises(self):
        with pytest.raises(ValueError, match="wt_hwnd must be positive"):
            wfp._WtFocusTask(
                pid=1, wt_hwnd=0, expected_title="ci:x",
                sibling_sentinels=(),
            )

    def test_negative_wt_hwnd_raises(self):
        with pytest.raises(ValueError, match="wt_hwnd must be positive"):
            wfp._WtFocusTask(
                pid=1, wt_hwnd=-5, expected_title="ci:x",
                sibling_sentinels=(),
            )

    def test_no_expected_no_siblings_raises(self):
        with pytest.raises(ValueError, match="expected_title or sibling_sentinels"):
            wfp._WtFocusTask(
                pid=1, wt_hwnd=999, expected_title=None,
                sibling_sentinels=(),
            )

    def test_only_sibling_sentinels_constructs(self):
        t = wfp._WtFocusTask(
            pid=1, wt_hwnd=999, expected_title=None,
            sibling_sentinels=("ci:abc", "ci:def"),
        )
        assert t.expected_title is None
        assert t.sibling_sentinels == ("ci:abc", "ci:def")

    def test_only_expected_constructs(self):
        t = wfp._WtFocusTask(
            pid=1, wt_hwnd=999, expected_title="ci:abc",
            sibling_sentinels=(),
        )
        assert t.expected_title == "ci:abc"

    def test_created_at_recorded(self):
        import time
        before = time.monotonic()
        t = wfp._WtFocusTask(
            pid=1, wt_hwnd=999, expected_title="ci:x",
            sibling_sentinels=(),
        )
        after = time.monotonic()
        assert before <= t.created_at <= after


# ─────────────────────────────────────────────────────────────────────
# try_schedule decision tree
# ─────────────────────────────────────────────────────────────────────


class TestTrySchedule:
    def test_with_expected_title_accepts(self, patch_deps):
        result = wfp.try_schedule(
            pid=123, wt_hwnd=999, expected_title="ci:abc",
            sibling_sentinels=(),
        )
        assert result is True
        # Drain.
        wfp.get_worker()._pool.waitForDone(2000)

    def test_with_only_siblings_accepts(self, patch_deps):
        result = wfp.try_schedule(
            pid=123, wt_hwnd=999, expected_title=None,
            sibling_sentinels=("ci:sib",),
        )
        assert result is True
        wfp.get_worker()._pool.waitForDone(2000)

    def test_invalid_args_rejected_caught(self, patch_deps, caplog):
        """Bad wt_hwnd shouldn't propagate; try_schedule returns False."""
        with caplog.at_level(logging.WARNING):
            result = wfp.try_schedule(
                pid=123, wt_hwnd=0, expected_title="ci:x",
                sibling_sentinels=(),
            )
        assert result is False
        assert any("construction failed" in r.message for r in caplog.records)


# ─────────────────────────────────────────────────────────────────────
# Task run() decision chain
# ─────────────────────────────────────────────────────────────────────


class TestTaskRunChain:
    def _build_task(self, **kwargs):
        defaults = dict(
            pid=100, wt_hwnd=999, expected_title="ci:target",
            sibling_sentinels=(),
        )
        defaults.update(kwargs)
        return wfp._WtFocusTask(**defaults)

    def test_select_succeeds_on_first_try(self, patch_deps):
        _, _, fake_uia = patch_deps
        fake_uia.select_results = [True]
        t = self._build_task()
        t.run()
        # Only one select call (the expected_title direct match).
        assert fake_uia.select_calls == [(999, "ci:target")]
        assert fake_uia.wait_calls == []

    def test_title_drift_set_title_wait_select(self, patch_deps):
        _, fake_console, fake_uia = patch_deps
        # First select misses; set_console succeeds; wait succeeds; second select hits.
        fake_uia.select_results = [False, True]
        fake_uia.wait_results = [True]
        fake_console.set_results = [True]
        t = self._build_task()
        t.run()
        assert fake_console.set_calls == [(100, "ci:target")]
        assert fake_uia.wait_calls == [(999, "ci:target", 80)]
        assert fake_uia.select_calls == [
            (999, "ci:target"), (999, "ci:target"),
        ]

    def test_title_drift_skipped_for_placeholder_pid(self, patch_deps):
        """pid<=0 (placeholder) skips set_console_title since
        AttachConsole needs a real pid."""
        _, fake_console, fake_uia = patch_deps
        fake_uia.select_results = [False]
        t = self._build_task(pid=-1)
        t.run()
        # set_console_title must NOT be called.
        assert fake_console.set_calls == []
        # Only the initial select.
        assert fake_uia.select_calls == [(999, "ci:target")]

    def test_falls_back_to_sibling_sentinels(self, patch_deps):
        """First select misses; title-drift recovery misses; siblings tried."""
        _, fake_console, fake_uia = patch_deps
        # initial expected miss; set+wait succeeds but second select misses;
        # then first sibling select misses, second sibling hits.
        fake_uia.select_results = [False, False, False, True]
        fake_uia.wait_results = [True]
        fake_console.set_results = [True]
        t = self._build_task(sibling_sentinels=("ci:sib1", "ci:sib2"))
        t.run()
        # 4 selects: expected, expected-after-retry, sib1, sib2
        assert [c[1] for c in fake_uia.select_calls] == [
            "ci:target", "ci:target", "ci:sib1", "ci:sib2",
        ]

    def test_siblings_skip_when_equal_to_expected(self, patch_deps):
        _, fake_console, fake_uia = patch_deps
        # All selects miss to force the sibling iteration.
        fake_uia.select_results = [False, False, False]
        fake_uia.wait_results = [True]
        fake_console.set_results = [True]
        t = self._build_task(
            sibling_sentinels=("ci:target", "ci:other"),
        )
        # Smart-guess + diagnostic require windows_terminal symbols;
        # patch them to no-op so the test exits cleanly.
        with (
            mock.patch(
                "claude_island.platform_.terminals.windows_terminal._try_smart_guess_select",
                return_value=False,
            ),
            mock.patch(
                "claude_island.platform_.terminals.windows_terminal._emit_suppress_title_diagnostic",
                return_value=None,
            ),
        ):
            t.run()
        # The "ci:target" sibling matches expected_title → skipped.
        # Only "ci:other" attempted in sibling loop.
        sib_attempted = [c[1] for c in fake_uia.select_calls if c[1] not in ("ci:target",)]
        assert sib_attempted == ["ci:other"]

    def test_smart_guess_invoked_when_everything_misses(self, patch_deps):
        _, fake_console, fake_uia = patch_deps
        fake_uia.select_results = [False, False, False]
        fake_uia.wait_results = [False]  # title never propagated
        fake_console.set_results = [True]
        t = self._build_task(sibling_sentinels=("ci:sib1",))

        smart_guess = mock.Mock(return_value=True)
        diagnostic = mock.Mock(return_value=None)
        with (
            mock.patch(
                "claude_island.platform_.terminals.windows_terminal._try_smart_guess_select",
                smart_guess,
            ),
            mock.patch(
                "claude_island.platform_.terminals.windows_terminal._emit_suppress_title_diagnostic",
                diagnostic,
            ),
        ):
            t.run()
        smart_guess.assert_called_once()
        # Diagnostic should NOT fire when smart_guess succeeded.
        diagnostic.assert_not_called()

    def test_diagnostic_emitted_when_all_strategies_miss(self, patch_deps, caplog):
        _, fake_console, fake_uia = patch_deps
        fake_uia.select_results = [False, False, False]
        fake_uia.wait_results = [False]
        fake_console.set_results = [True]
        t = self._build_task()

        with (
            mock.patch(
                "claude_island.platform_.terminals.windows_terminal._try_smart_guess_select",
                return_value=False,
            ),
            mock.patch(
                "claude_island.platform_.terminals.windows_terminal._emit_suppress_title_diagnostic",
            ) as diagnostic,
            caplog.at_level(logging.INFO),
        ):
            t.run()
        diagnostic.assert_called_once_with("target")  # strip "ci:" prefix
        assert any("pane select miss" in r.message for r in caplog.records)


# ─────────────────────────────────────────────────────────────────────
# COM apartment initialization
# ─────────────────────────────────────────────────────────────────────


class TestComInit:
    def test_run_calls_coinitex_first(self, patch_deps):
        fake_pythoncom, _, fake_uia = patch_deps
        fake_uia.select_results = [True]
        t = wfp._WtFocusTask(
            pid=1, wt_hwnd=999, expected_title="ci:abc",
            sibling_sentinels=(),
        )
        t.run()
        # CoInitializeEx called once with COINIT_APARTMENTTHREADED.
        assert fake_pythoncom.coinitex_calls == [0x2]

    def test_repeat_run_same_thread_inits_once(self, patch_deps):
        """Thread-local guard means re-running on the same thread
        doesn't re-init."""
        fake_pythoncom, _, fake_uia = patch_deps
        fake_uia.select_results = [True, True]
        t1 = wfp._WtFocusTask(
            pid=1, wt_hwnd=999, expected_title="ci:a",
            sibling_sentinels=(),
        )
        t2 = wfp._WtFocusTask(
            pid=2, wt_hwnd=999, expected_title="ci:b",
            sibling_sentinels=(),
        )
        t1.run()
        t2.run()
        # Single thread, so single init.
        assert len(fake_pythoncom.coinitex_calls) == 1

    def test_coinitex_failure_falls_back_to_coinit(self, patch_deps, monkeypatch):
        """RPC_E_CHANGED_MODE recovery uses plain CoInitialize."""
        fake_pythoncom = _FakePythoncom(
            raise_on_init=RuntimeError("RPC_E_CHANGED_MODE"),
        )
        monkeypatch.setattr(wfp, "_pythoncom", fake_pythoncom)
        # Reset thread-local.
        if hasattr(wfp._thread_local, "com_initialized"):
            monkeypatch.delattr(wfp._thread_local, "com_initialized")

        fake_uia = patch_deps[2]
        fake_uia.select_results = [True]
        t = wfp._WtFocusTask(
            pid=1, wt_hwnd=999, expected_title="ci:x",
            sibling_sentinels=(),
        )
        t.run()
        assert fake_pythoncom.coinitex_calls == [0x2]
        assert fake_pythoncom.coinit_calls == 1


# ─────────────────────────────────────────────────────────────────────
# prewarm()
# ─────────────────────────────────────────────────────────────────────


class TestPrewarm:
    def test_prewarm_initialises_com(self, patch_deps):
        fake_pythoncom = patch_deps[0]
        wfp.prewarm()
        wfp.get_worker()._pool.waitForDone(2000)
        assert fake_pythoncom.coinitex_calls == [0x2]

    def test_prewarm_noop_without_deps(self, monkeypatch):
        monkeypatch.setattr(wfp, "_HAS_DEPS", False)
        # Should not raise.
        wfp.prewarm()


# ─────────────────────────────────────────────────────────────────────
# Task exception swallowing
# ─────────────────────────────────────────────────────────────────────


class TestTaskExceptionSafety:
    def test_run_swallows_exceptions(self, patch_deps, caplog):
        t = wfp._WtFocusTask(
            pid=1, wt_hwnd=999, expected_title="ci:x",
            sibling_sentinels=(),
        )
        with (
            mock.patch.object(t, "_run_impl", side_effect=RuntimeError("boom")),
            caplog.at_level(logging.WARNING),
        ):
            t.run()
        assert any("_WtFocusTask raised" in r.message for r in caplog.records)
