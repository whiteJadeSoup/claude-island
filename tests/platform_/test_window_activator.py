"""Tests for window_activator helpers (Q3 — pythonw-safe console handling)."""

from __future__ import annotations

import sys
from unittest.mock import MagicMock, patch

import pytest

# These tests are Windows-specific (the helper imports ctypes.windll.kernel32).
pytestmark = pytest.mark.skipif(
    sys.platform != "win32", reason="Windows-only API surface"
)


@pytest.fixture
def mock_kernel32():
    """Patch ctypes.windll.kernel32 with a MagicMock so we can intercept
    the GetConsoleWindow / FreeConsole / AttachConsole sequence and
    verify the dance respects pre-existing console state."""
    import ctypes
    real_windll = ctypes.windll
    fake_kernel32 = MagicMock()
    fake_windll = MagicMock(kernel32=fake_kernel32)
    with patch.object(ctypes, "windll", fake_windll):
        yield fake_kernel32


def _make_win32gui_stub(visible_title: str | None = None):
    """Stub out win32gui so the OWNER walk has something to terminate on."""
    stub = MagicMock()
    if visible_title:
        stub.IsWindowVisible.return_value = True
        stub.GetWindowText.return_value = visible_title
    else:
        # Force the owner walk to bail by returning None for owner & parent.
        stub.IsWindowVisible.return_value = False
        stub.GetWindow.return_value = 0
        stub.GetParent.return_value = 0
    return stub


# --------------------------------------------------------------------------
# Q3: pythonw / no-parent-console safety
# --------------------------------------------------------------------------

def test_no_attach_parent_when_we_had_no_console(mock_kernel32):
    """When GetConsoleWindow returns 0 at entry (pythonw / windowed .exe),
    we must NOT call AttachConsole(ATTACH_PARENT_PROCESS) on the way out.
    That call silently fails and leaves us console-less for the rest of
    the process — breaking later stderr diagnostics."""
    from claude_island.platform_.window_activator import _resolve_console_window

    # Original console: none. Target's console: a HWND.
    mock_kernel32.GetConsoleWindow.side_effect = [0, 12345]
    mock_kernel32.AttachConsole.return_value = 1  # AttachConsole(target) succeeds

    win32gui = _make_win32gui_stub(visible_title="Some Terminal")
    win32gui.IsWindowVisible.return_value = True

    _resolve_console_window(pid=999, win32gui=win32gui)

    # Verify FreeConsole was called and AttachConsole(target_pid) was called,
    # but AttachConsole(ATTACH_PARENT_PROCESS = 0xFFFFFFFF) was NOT.
    attach_calls = [
        call.args[0] for call in mock_kernel32.AttachConsole.call_args_list
    ]
    assert 999 in attach_calls, "AttachConsole(target_pid) should run"
    assert 0xFFFFFFFF not in attach_calls, (
        "AttachConsole(ATTACH_PARENT_PROCESS) called despite no original console"
    )


def test_attach_parent_called_when_we_had_a_console(mock_kernel32):
    """The normal path (started from PowerShell): we had a console at entry,
    so the dance must restore it on exit."""
    from claude_island.platform_.window_activator import _resolve_console_window

    # Original console present (any non-zero HWND); target's console too.
    mock_kernel32.GetConsoleWindow.side_effect = [99999, 12345]
    mock_kernel32.AttachConsole.return_value = 1

    win32gui = _make_win32gui_stub()
    win32gui.IsWindowVisible.return_value = True
    win32gui.GetWindowText.return_value = "title"

    _resolve_console_window(pid=999, win32gui=win32gui)

    attach_calls = [
        call.args[0] for call in mock_kernel32.AttachConsole.call_args_list
    ]
    assert 0xFFFFFFFF in attach_calls, (
        "AttachConsole(ATTACH_PARENT_PROCESS) must run to restore our console"
    )


def test_returns_none_when_attach_fails(mock_kernel32):
    """If AttachConsole(target) fails (target has no console, access denied),
    return None gracefully — caller falls back to ancestor-pid walk."""
    from claude_island.platform_.window_activator import _resolve_console_window

    mock_kernel32.GetConsoleWindow.return_value = 0
    mock_kernel32.AttachConsole.return_value = 0  # AttachConsole(target) fails

    win32gui = _make_win32gui_stub()
    result = _resolve_console_window(pid=999, win32gui=win32gui)
    assert result is None
