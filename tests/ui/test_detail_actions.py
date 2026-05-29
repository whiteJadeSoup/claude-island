"""R7 Tests: WorldViewModel action slots (rename, copy-id, open-folder,
open-transcript, reset-thinking).

The VM is constructed with fake callbacks injected for each action; tests
assert that the slots forward correctly.

For copyId and openTranscript, the test asserts that the slots don't raise
when a QGuiApplication (or QCoreApplication) is present; clipboard is
guarded inside copyId already so it's a no-raise test on headless CI.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

from PySide6.QtCore import QCoreApplication

from claude_island.ui.world_view_model import WorldViewModel

# One shared app instance — QCoreApplication is sufficient for the slot
# dispatch tests.  copyId uses QGuiApplication.clipboard(); on an offscreen
# display the clipboard may be unavailable, so we guard with try/except inside
# the slot itself and verify here only that it doesn't raise.
_app = QCoreApplication.instance() or QCoreApplication([])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _vm(**kwargs) -> WorldViewModel:
    """Construct a WorldViewModel with the given kwargs and sensible defaults."""
    return WorldViewModel(**kwargs)


# ---------------------------------------------------------------------------
# renameSession — forwards (uuid, name) to rename_fn
# ---------------------------------------------------------------------------

def test_rename_session_forwards_uuid_and_name():
    calls = []
    vm = _vm(rename_fn=lambda uuid, name: calls.append((uuid, name)))
    vm.renameSession("uuid-1", "my cool session")
    assert calls == [("uuid-1", "my cool session")]


def test_rename_session_no_callback_is_noop():
    vm = _vm()
    vm.renameSession("uuid-1", "name")  # must not raise


# ---------------------------------------------------------------------------
# openFolder — forwards session_id to open_folder_fn
# ---------------------------------------------------------------------------

def test_open_folder_forwards_session_id():
    calls = []
    vm = _vm(open_folder_fn=lambda sid: calls.append(sid))
    vm.openFolder("uuid-2")
    assert calls == ["uuid-2"]


def test_open_folder_no_callback_is_noop():
    vm = _vm()
    vm.openFolder("uuid-2")  # must not raise


# ---------------------------------------------------------------------------
# resetThinking — forwards uuid to reset_thinking_fn
# ---------------------------------------------------------------------------

def test_reset_thinking_forwards_uuid():
    calls = []
    vm = _vm(reset_thinking_fn=lambda uuid: calls.append(uuid))
    vm.resetThinking("uuid-3")
    assert calls == ["uuid-3"]


def test_reset_thinking_no_callback_is_noop():
    vm = _vm()
    vm.resetThinking("uuid-3")  # must not raise


# ---------------------------------------------------------------------------
# copyId — doesn't raise; clipboard call is guarded inside the slot
# ---------------------------------------------------------------------------

def test_copy_id_does_not_raise_without_gui_app():
    """copyId must not propagate exceptions even when clipboard is unavailable."""
    vm = _vm()
    vm.copyId("some-uuid-string")  # no assert needed beyond no-raise


def test_copy_id_calls_clipboard_when_available():
    """When QGuiApplication.instance() returns a mock with a clipboard attribute,
    clipboard().setText is called with the given text.

    The slot guards on hasattr(app, 'clipboard') rather than isinstance so
    that MagicMock(spec=QGuiApplication) passes — PySide6's C-level isinstance
    rejects mocks regardless of spec."""
    mock_clipboard = MagicMock()
    mock_clipboard.setText = MagicMock()

    from PySide6.QtGui import QGuiApplication
    # spec=QGuiApplication gives the mock a 'clipboard' attribute,
    # which is all the slot's hasattr guard needs.
    mock_app = MagicMock(spec=QGuiApplication)
    mock_app.clipboard.return_value = mock_clipboard

    vm = _vm()
    with patch("PySide6.QtGui.QGuiApplication.instance", return_value=mock_app):
        vm.copyId("test-uuid-123")

    mock_clipboard.setText.assert_called_once_with("test-uuid-123")


# ---------------------------------------------------------------------------
# openTranscript — doesn't raise; opens URL via QDesktopServices
# ---------------------------------------------------------------------------

def test_open_transcript_does_not_raise_with_empty_path():
    """Empty path must be silently ignored (guarded inside the slot)."""
    vm = _vm()
    vm.openTranscript("")  # must not raise


def test_open_transcript_calls_desktop_services():
    """Non-empty path must call QDesktopServices.openUrl."""
    calls = []
    from PySide6.QtCore import QUrl
    with patch("PySide6.QtGui.QDesktopServices.openUrl") as mock_open:
        vm = _vm()
        vm.openTranscript("/some/path/session.jsonl")
        mock_open.assert_called_once()
        # Verify it received a QUrl (not a raw string)
        args = mock_open.call_args[0]
        assert len(args) == 1
        assert isinstance(args[0], QUrl)


def test_open_transcript_no_raise_on_arbitrary_path():
    """Any non-empty path must not raise even without a display."""
    vm = _vm()
    # Patch to avoid side effects in the test environment
    with patch("PySide6.QtGui.QDesktopServices.openUrl"):
        vm.openTranscript("C:/Users/user/.claude/projects/hash/uuid.jsonl")
