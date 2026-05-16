"""Integration tests for WindowsTerminalAdapter fast-path orchestration.

Tests the focus() integration that resolves wt_hwnd, validates it,
calls _force_foreground on main thread, then schedules the async worker.

Strategy: mock win32 modules at sys.modules so the adapter's lazy
imports succeed on non-Windows test hosts; mock _force_foreground +
_wt_fast_path.try_schedule at the seams to observe orchestration
behaviour without actually running UIA or COM.
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

import pytest

from claude_island.core.hook_events import JumpTarget
from claude_island.core.models import Session
from claude_island.core.snapshot import SessionView, _degraded_view
from claude_island.platform_.terminals.windows_terminal import (
    WindowsTerminalAdapter,
    _is_wt_window,
    _WT_CLASS_PREFIX,
)


# ─────────────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────────────


@pytest.fixture
def patch_win32(monkeypatch):
    """Mock win32 modules so the adapter's import-on-use succeeds."""
    win32gui = mock.MagicMock(name="win32gui")
    win32con = mock.MagicMock(name="win32con")
    win32process = mock.MagicMock(name="win32process")
    monkeypatch.setitem(sys.modules, "win32gui", win32gui)
    monkeypatch.setitem(sys.modules, "win32con", win32con)
    monkeypatch.setitem(sys.modules, "win32process", win32process)
    return win32gui, win32con, win32process


def _session(pid: int = 1234, cwd: str = "/proj") -> Session:
    return Session(
        pid=pid, project_path=Path(cwd), session_uuid="abc-uuid",
        last_activity=datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc),
    )


def _view(
    pid: int = 1234,
    cwd: str = "/proj",
    *,
    jump_target: JumpTarget | None = None,
) -> SessionView:
    from dataclasses import replace
    v = _degraded_view(_session(pid, cwd))
    if jump_target is not None:
        v = replace(v, jump_target=jump_target)
    return v


def _adapter() -> WindowsTerminalAdapter:
    a = WindowsTerminalAdapter()
    a.name = "windows-terminal"
    a._priority = 100
    return a


# ─────────────────────────────────────────────────────────────────────
# _is_wt_window classname check (C-004 / D-5)
# ─────────────────────────────────────────────────────────────────────


class TestIsWtWindow:
    def test_returns_true_for_wt_class(self):
        win32gui = mock.Mock()
        win32gui.GetClassName.return_value = _WT_CLASS_PREFIX
        assert _is_wt_window(123, win32gui) is True

    def test_returns_true_for_class_with_suffix(self):
        win32gui = mock.Mock()
        win32gui.GetClassName.return_value = _WT_CLASS_PREFIX + "_Variant"
        assert _is_wt_window(123, win32gui) is True

    def test_returns_false_for_non_wt_class(self):
        win32gui = mock.Mock()
        win32gui.GetClassName.return_value = "Chrome_WidgetWin_1"
        assert _is_wt_window(123, win32gui) is False

    def test_returns_false_for_zero_hwnd(self):
        win32gui = mock.Mock()
        assert _is_wt_window(0, win32gui) is False
        win32gui.GetClassName.assert_not_called()

    def test_returns_false_on_exception(self):
        win32gui = mock.Mock()
        win32gui.GetClassName.side_effect = RuntimeError("invalid hwnd")
        assert _is_wt_window(123, win32gui) is False

    def test_returns_false_on_empty_classname(self):
        win32gui = mock.Mock()
        win32gui.GetClassName.return_value = ""
        assert _is_wt_window(123, win32gui) is False


# ─────────────────────────────────────────────────────────────────────
# _resolve_wt_hwnd_fast prehook + cache (B-001 / Q-1)
# ─────────────────────────────────────────────────────────────────────


class TestResolveWtHwndFast:
    def test_prehook_hit_walks_to_visible_host(self, patch_win32):
        win32gui, _, _ = patch_win32
        win32gui.IsWindow.return_value = True

        adapter = _adapter()
        v = _view(pid=1234)
        with mock.patch(
            "claude_island.platform_.window_activator.walk_to_visible_host",
            return_value=8888,
        ) as walk:
            result = adapter._resolve_wt_hwnd_fast(v, 5555, win32gui)
        assert result == 8888
        walk.assert_called_once_with(5555, win32gui)

    def test_prehook_invalid_falls_to_cache(self, patch_win32):
        """Prehook hwnd no longer a valid window → check cache instead."""
        win32gui, _, _ = patch_win32
        # IsWindow: False for prehook, True for cache entry.
        win32gui.IsWindow.side_effect = lambda h: h == 7777

        adapter = _adapter()
        adapter._wt_hwnd_cache[1234] = 7777
        v = _view(pid=1234)
        with mock.patch(
            "claude_island.platform_.window_activator.walk_to_visible_host",
        ) as walk:
            result = adapter._resolve_wt_hwnd_fast(v, 5555, win32gui)
        # walk_to_visible_host not called because IsWindow(prehook)=False.
        walk.assert_not_called()
        assert result == 7777

    def test_no_prehook_uses_cache(self, patch_win32):
        """Without prehook_conhost_hwnd, cache lookup is the fast path."""
        win32gui, _, _ = patch_win32
        win32gui.IsWindow.return_value = True

        adapter = _adapter()
        adapter._wt_hwnd_cache[1234] = 7777
        v = _view(pid=1234)
        result = adapter._resolve_wt_hwnd_fast(v, 0, win32gui)
        assert result == 7777

    def test_no_prehook_no_cache_returns_none(self, patch_win32):
        win32gui, _, _ = patch_win32
        adapter = _adapter()
        v = _view(pid=1234)
        result = adapter._resolve_wt_hwnd_fast(v, 0, win32gui)
        assert result is None

    def test_placeholder_pid_skips_cache(self, patch_win32):
        """pid<=0 must not consult cache (cache is keyed by real pid)."""
        win32gui, _, _ = patch_win32
        adapter = _adapter()
        adapter._wt_hwnd_cache[-1] = 7777  # poisoned entry
        v = _view(pid=-1)
        result = adapter._resolve_wt_hwnd_fast(v, 0, win32gui)
        assert result is None

    def test_cache_with_invalid_hwnd_returns_none(self, patch_win32):
        win32gui, _, _ = patch_win32
        win32gui.IsWindow.return_value = False  # cached hwnd no longer valid

        adapter = _adapter()
        adapter._wt_hwnd_cache[1234] = 7777
        v = _view(pid=1234)
        result = adapter._resolve_wt_hwnd_fast(v, 0, win32gui)
        assert result is None


# ─────────────────────────────────────────────────────────────────────
# _try_fast_path orchestration
# ─────────────────────────────────────────────────────────────────────


class TestTryFastPathOrchestration:
    def test_happy_path_raises_window_and_schedules(self, patch_win32, monkeypatch):
        win32gui, win32con, win32process = patch_win32
        win32gui.IsWindow.return_value = True
        win32gui.GetClassName.return_value = _WT_CLASS_PREFIX

        adapter = _adapter()
        adapter._wt_hwnd_cache[1234] = 7777
        v = _view(pid=1234)

        force_foreground = mock.Mock(return_value=True)
        try_schedule = mock.Mock(return_value=True)
        monkeypatch.setattr(
            "claude_island.platform_.window_activator._force_foreground",
            force_foreground,
        )
        monkeypatch.setattr(
            "claude_island.platform_.terminals._wt_fast_path.try_schedule",
            try_schedule,
        )

        result = adapter._try_fast_path(
            view=v, expected="ci:abc-uuid", sib_sentinels=(),
            prehook_conhost=0,
        )
        assert result is True
        force_foreground.assert_called_once_with(7777, win32con, win32gui, win32process)
        try_schedule.assert_called_once()
        kwargs = try_schedule.call_args.kwargs
        assert kwargs == {
            "pid": 1234,
            "wt_hwnd": 7777,
            "expected_title": "ci:abc-uuid",
            "sibling_sentinels": (),
        }

    def test_no_wt_hwnd_returns_false(self, patch_win32, monkeypatch):
        """Empty cache + no prehook → fast-path declines."""
        win32gui, _, _ = patch_win32
        adapter = _adapter()
        v = _view(pid=1234)

        force_foreground = mock.Mock()
        try_schedule = mock.Mock()
        monkeypatch.setattr(
            "claude_island.platform_.window_activator._force_foreground",
            force_foreground,
        )
        monkeypatch.setattr(
            "claude_island.platform_.terminals._wt_fast_path.try_schedule",
            try_schedule,
        )

        result = adapter._try_fast_path(
            view=v, expected="ci:x", sib_sentinels=(),
            prehook_conhost=0,
        )
        assert result is False
        force_foreground.assert_not_called()
        try_schedule.assert_not_called()

    def test_wrong_class_returns_false(self, patch_win32, monkeypatch):
        """hwnd resolves but classname check fails → decline."""
        win32gui, _, _ = patch_win32
        win32gui.IsWindow.return_value = True
        win32gui.GetClassName.return_value = "NotAWtClass"

        adapter = _adapter()
        adapter._wt_hwnd_cache[1234] = 7777
        v = _view(pid=1234)

        force_foreground = mock.Mock()
        try_schedule = mock.Mock()
        monkeypatch.setattr(
            "claude_island.platform_.window_activator._force_foreground",
            force_foreground,
        )
        monkeypatch.setattr(
            "claude_island.platform_.terminals._wt_fast_path.try_schedule",
            try_schedule,
        )

        result = adapter._try_fast_path(
            view=v, expected="ci:x", sib_sentinels=(),
            prehook_conhost=0,
        )
        assert result is False
        force_foreground.assert_not_called()
        try_schedule.assert_not_called()

    def test_force_foreground_failure_returns_false(self, patch_win32, monkeypatch):
        """_force_foreground returns False → decline; legacy will retry."""
        win32gui, win32con, win32process = patch_win32
        win32gui.IsWindow.return_value = True
        win32gui.GetClassName.return_value = _WT_CLASS_PREFIX

        adapter = _adapter()
        adapter._wt_hwnd_cache[1234] = 7777
        v = _view(pid=1234)

        force_foreground = mock.Mock(return_value=False)
        try_schedule = mock.Mock()
        monkeypatch.setattr(
            "claude_island.platform_.window_activator._force_foreground",
            force_foreground,
        )
        monkeypatch.setattr(
            "claude_island.platform_.terminals._wt_fast_path.try_schedule",
            try_schedule,
        )

        result = adapter._try_fast_path(
            view=v, expected="ci:x", sib_sentinels=(),
            prehook_conhost=0,
        )
        assert result is False
        try_schedule.assert_not_called()

    def test_prehook_preferred_over_cache(self, patch_win32, monkeypatch):
        """Prehook is checked first even when cache has a different entry."""
        win32gui, win32con, win32process = patch_win32
        win32gui.IsWindow.return_value = True
        win32gui.GetClassName.return_value = _WT_CLASS_PREFIX

        adapter = _adapter()
        adapter._wt_hwnd_cache[1234] = 2222  # different from prehook walk result
        v = _view(pid=1234)

        force_foreground = mock.Mock(return_value=True)
        try_schedule = mock.Mock(return_value=True)
        monkeypatch.setattr(
            "claude_island.platform_.window_activator._force_foreground",
            force_foreground,
        )
        monkeypatch.setattr(
            "claude_island.platform_.window_activator.walk_to_visible_host",
            mock.Mock(return_value=8888),  # prehook walk result
        )
        monkeypatch.setattr(
            "claude_island.platform_.terminals._wt_fast_path.try_schedule",
            try_schedule,
        )

        adapter._try_fast_path(
            view=v, expected="ci:x", sib_sentinels=(),
            prehook_conhost=5555,
        )
        # Prehook walk result (8888) was used, not cache entry (2222).
        kwargs = try_schedule.call_args.kwargs
        assert kwargs["wt_hwnd"] == 8888

    def test_no_pywin32_returns_false(self, monkeypatch):
        """Without pywin32, fast-path bails (legacy path handles missing dep)."""
        adapter = _adapter()
        v = _view(pid=1234)

        # Hide win32* modules from import.
        for mod in ("win32gui", "win32con", "win32process"):
            monkeypatch.setitem(sys.modules, mod, None)

        result = adapter._try_fast_path(
            view=v, expected="ci:x", sib_sentinels=(),
            prehook_conhost=0,
        )
        assert result is False
