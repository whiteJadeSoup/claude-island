"""Tests for win32_console.get_console_info — the lock-protected
AttachConsole / GetConsoleTitleW dance shared by ProcessScanner and
WindowActivator.

The Q3 pythonw-safety invariant lives here too: when our process never
had a console at entry (windowed launch), we MUST NOT call
AttachConsole(ATTACH_PARENT_PROCESS) on the way out — that call silently
fails and leaves us console-less for the rest of the run.
"""
from __future__ import annotations

import sys
from unittest.mock import MagicMock, patch

import pytest

# Windows-only API surface (ctypes.windll.kernel32).
pytestmark = pytest.mark.skipif(
    sys.platform != "win32", reason="Windows-only API surface"
)


@pytest.fixture
def mock_kernel32():
    """Patch ctypes.windll.kernel32 with a MagicMock so we can intercept
    the GetConsoleWindow / FreeConsole / AttachConsole sequence."""
    import ctypes
    fake_kernel32 = MagicMock()
    fake_windll = MagicMock(kernel32=fake_kernel32)
    with patch.object(ctypes, "windll", fake_windll):
        yield fake_kernel32


# ---------------------------------------------------------------------------
# Q3: pythonw / no-parent-console safety (lives here after the dance was
# moved out of window_activator into win32_console).
# ---------------------------------------------------------------------------

def test_no_attach_parent_when_we_had_no_console(mock_kernel32):
    """When GetConsoleWindow returns 0 at entry (pythonw / windowed .exe),
    we must NOT call AttachConsole(ATTACH_PARENT_PROCESS) on the way out."""
    from claude_island.platform_.win32_console import get_console_info

    # First call: our pre-existing console state. Second: target's.
    mock_kernel32.GetConsoleWindow.side_effect = [0, 12345]
    mock_kernel32.AttachConsole.return_value = 1

    get_console_info(pid=999)

    attach_calls = [
        call.args[0] for call in mock_kernel32.AttachConsole.call_args_list
    ]
    assert 999 in attach_calls, "AttachConsole(target_pid) should run"
    assert 0xFFFFFFFF not in attach_calls, (
        "AttachConsole(ATTACH_PARENT_PROCESS) called despite no original console"
    )


def test_attach_parent_called_when_we_had_a_console(mock_kernel32):
    """Normal CLI launch path: we had a console, so the dance must
    restore it on exit."""
    from claude_island.platform_.win32_console import get_console_info

    mock_kernel32.GetConsoleWindow.side_effect = [99999, 12345]
    mock_kernel32.AttachConsole.return_value = 1

    get_console_info(pid=999)

    attach_calls = [
        call.args[0] for call in mock_kernel32.AttachConsole.call_args_list
    ]
    assert 0xFFFFFFFF in attach_calls, (
        "AttachConsole(ATTACH_PARENT_PROCESS) must run to restore our console"
    )


def test_returns_none_when_attach_fails(mock_kernel32):
    """If AttachConsole(target) fails (target has no console, access
    denied), return None gracefully."""
    from claude_island.platform_.win32_console import get_console_info

    mock_kernel32.GetConsoleWindow.return_value = 0
    mock_kernel32.AttachConsole.return_value = 0

    assert get_console_info(pid=999) is None


def test_returns_tuple_with_title_on_success(mock_kernel32):
    """Happy path: returns (conpty_hwnd, console_title)."""
    from claude_island.platform_.win32_console import get_console_info

    mock_kernel32.GetConsoleWindow.side_effect = [0, 12345]
    mock_kernel32.AttachConsole.return_value = 1

    def _set_title(buf, _size):
        buf.value = "my tab"
        return len("my tab")

    mock_kernel32.GetConsoleTitleW.side_effect = _set_title

    result = get_console_info(pid=999)
    assert result == (12345, "my tab")


def test_returns_none_when_console_window_is_zero_after_attach(mock_kernel32):
    """AttachConsole says success but GetConsoleWindow returns 0 (no
    console actually associated). Caller should see None, not (0, '')."""
    from claude_island.platform_.win32_console import get_console_info

    # Pre-existing: 0; after attach: 0 (anomalous).
    mock_kernel32.GetConsoleWindow.side_effect = [0, 0]
    mock_kernel32.AttachConsole.return_value = 1

    assert get_console_info(pid=999) is None
