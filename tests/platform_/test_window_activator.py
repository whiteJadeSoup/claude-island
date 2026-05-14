"""Tests for the GW_OWNER walk in window_activator.

The console-state dance itself (AttachConsole / FreeConsole / pythonw
safety) lives in test_win32_console.py — that logic was extracted to
win32_console so the scanner can share it.

The legacy ``WindowActivator`` class that used to live alongside these
helpers was removed in 2026-05; its click-time entry point is now
``WindowsTerminalAdapter._activate_windows`` in
``terminals/windows_terminal.py``, covered by
``test_windows_terminal_adapter.py``.
"""
from __future__ import annotations

import sys
from unittest.mock import MagicMock, patch

import pytest

from claude_island.platform_.window_activator import _ancestor_pids


# ── _ancestor_pids defensive behaviour (cross-platform: pure Python) ──


def test_ancestor_pids_placeholder_pid_returns_empty():
    """PLACEHOLDER_PID (-1) inserted by HookSessionBridge must not blow
    up _ancestor_pids — psutil raises ValueError on negative pid which
    would propagate up to focus() and silently no-op user clicks
    (Bug A from live-run testing 2026-05-13)."""
    assert _ancestor_pids(-1) == []


def test_ancestor_pids_zero_pid_returns_empty():
    """pid=0 is the System Idle Process on Windows; treat as invalid."""
    assert _ancestor_pids(0) == []


def test_ancestor_pids_nonexistent_pid_returns_empty():
    """High pid that doesn't exist → psutil.NoSuchProcess → []."""
    # Use a very large pid unlikely to exist
    result = _ancestor_pids(2**30)
    assert result == []


pytestmark = pytest.mark.skipif(
    sys.platform != "win32", reason="Windows-only API surface"
)


def _make_win32gui_stub(visible_title: str | None = None):
    """Stub out win32gui so the GW_OWNER walk has something to terminate on."""
    stub = MagicMock()
    if visible_title:
        stub.IsWindowVisible.return_value = True
        stub.GetWindowText.return_value = visible_title
    else:
        stub.IsWindowVisible.return_value = False
        stub.GetWindow.return_value = 0
        stub.GetParent.return_value = 0
    return stub


# ---------------------------------------------------------------------------
# W1: _resolve_console_window returns (host_hwnd, title) on success
# ---------------------------------------------------------------------------

def test_resolve_returns_tuple_with_title():
    """get_console_info gives us (conpty_hwnd, title); the GW_OWNER walk
    finds a visible host. _resolve_console_window must return that tuple."""
    from claude_island.platform_.window_activator import _resolve_console_window

    win32gui = _make_win32gui_stub(visible_title="WT Window")

    with patch(
        "claude_island.platform_.window_activator.win32_console.get_console_info",
        return_value=(12345, "my tab"),
    ):
        result = _resolve_console_window(pid=999, win32gui=win32gui)

    assert result == (12345, "my tab")


# ---------------------------------------------------------------------------
# W2: _resolve_console_window returns None when get_console_info returns None
# ---------------------------------------------------------------------------

def test_resolve_returns_none_when_console_info_unavailable():
    from claude_island.platform_.window_activator import _resolve_console_window

    with patch(
        "claude_island.platform_.window_activator.win32_console.get_console_info",
        return_value=None,
    ):
        result = _resolve_console_window(pid=999, win32gui=_make_win32gui_stub())
    assert result is None
