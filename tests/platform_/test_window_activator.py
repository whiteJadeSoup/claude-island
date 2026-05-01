"""Tests for window_activator: the GW_OWNER walk on top of the shared
win32_console.get_console_info, plus the activate flow that wires
tab selection in front of foreground.

The console-state dance itself (AttachConsole / FreeConsole / pythonw
safety) lives in test_win32_console.py — that logic was extracted to
win32_console so the scanner can share it.
"""
from __future__ import annotations

import sys
from unittest.mock import MagicMock, patch

import pytest

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


# ---------------------------------------------------------------------------
# W3: _activate_windows calls wt_uia.select_tab_by_title when resolve succeeds
# ---------------------------------------------------------------------------

def test_activate_calls_wt_uia_select_when_resolved():
    """When _resolve_console_window returns (hwnd, title),
    wt_uia.select_tab_by_title must be called with those exact args."""
    from claude_island.platform_.window_activator import WindowActivator

    with (
        patch(
            "claude_island.platform_.window_activator._resolve_console_window",
            return_value=(5678, "my-tab"),
        ),
        patch(
            "claude_island.platform_.window_activator.wt_uia"
        ) as mock_wt_uia,
        patch(
            "claude_island.platform_.window_activator._force_foreground",
            return_value=True,
        ),
        patch.dict(
            "sys.modules",
            {
                "win32con": MagicMock(),
                "win32gui": MagicMock(),
                "win32process": MagicMock(),
            },
        ),
    ):
        result = WindowActivator()._activate_windows(pid=999)

    mock_wt_uia.select_tab_by_title.assert_called_once_with(5678, "my-tab")
    assert result is True


# ---------------------------------------------------------------------------
# W4: _activate_windows skips tab selection on the ancestor-walk fallback
# ---------------------------------------------------------------------------

def test_activate_skips_tab_select_on_fallback():
    """When _resolve_console_window returns None (ancestor-walk fallback),
    wt_uia.select_tab_by_title must NOT be called, but _force_foreground
    still runs against the fallback HWND."""
    from claude_island.platform_.window_activator import WindowActivator

    with (
        patch(
            "claude_island.platform_.window_activator._resolve_console_window",
            return_value=None,
        ),
        patch(
            "claude_island.platform_.window_activator._ancestor_pids",
            return_value=[999, 1],
        ),
        patch(
            "claude_island.platform_.window_activator._find_window_for_pids",
            return_value=1111,
        ),
        patch(
            "claude_island.platform_.window_activator.wt_uia"
        ) as mock_wt_uia,
        patch(
            "claude_island.platform_.window_activator._force_foreground",
            return_value=True,
        ),
        patch.dict(
            "sys.modules",
            {
                "win32con": MagicMock(),
                "win32gui": MagicMock(),
                "win32process": MagicMock(),
            },
        ),
    ):
        result = WindowActivator()._activate_windows(pid=999)

    mock_wt_uia.select_tab_by_title.assert_not_called()
    assert result is True
