"""Unit tests for LocalAppBackend + OsBackend — happy/edge/error paths.

Tests inject mock deps; no real filesystem or subprocess calls.
"""
from __future__ import annotations

import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

import pytest
from claude_island.core.capabilities import Capability
from claude_island.core.models import Session
from claude_island.core.snapshot import SessionView, _degraded_view
from claude_island.platform_.app_backend import LocalAppBackend


# ── Fixtures ──────────────────────────────────────────────────────────

@pytest.fixture
def view_with_uuid() -> SessionView:
    s = Session(pid=10, project_path=Path("/tmp/a"), session_uuid="abc-123",
                last_activity=datetime(2026,5,1,12,0,tzinfo=timezone.utc))
    return _degraded_view(s)


@pytest.fixture
def view_no_uuid() -> SessionView:
    s = Session(pid=10, project_path=Path("/tmp/a"), session_uuid="",
                last_activity=datetime(2026,5,1,12,0,tzinfo=timezone.utc))
    return _degraded_view(s)


# ── LocalAppBackend ───────────────────────────────────────────────────

class TestAppBackendRename:
    def test_happy_path(self, view_with_uuid):
        names = mock.Mock()
        names.set_session_name = mock.Mock()
        on_change = mock.Mock()
        backend = LocalAppBackend(
            names_store=names, claude_projects_dir=Path("/tmp"),
            on_change=on_change
        )
        result = backend.rename(view_with_uuid, new_name="  my session  ")
        assert result is True
        names.set_session_name.assert_called_once_with("abc-123", "my session")
        on_change.assert_called_once()

    def test_empty_uuid_returns_false(self, view_no_uuid):
        names = mock.Mock()
        on_change = mock.Mock()
        backend = LocalAppBackend(
            names_store=names, claude_projects_dir=Path("/tmp"),
            on_change=on_change
        )
        result = backend.rename(view_no_uuid, new_name="test")
        assert result is False
        names.set_session_name.assert_not_called()
        on_change.assert_not_called()

    def test_oserror_returns_false(self, view_with_uuid):
        names = mock.Mock()
        names.set_session_name = mock.Mock(side_effect=OSError("disk full"))
        on_change = mock.Mock()
        backend = LocalAppBackend(
            names_store=names, claude_projects_dir=Path("/tmp"),
            on_change=on_change
        )
        result = backend.rename(view_with_uuid, new_name="test")
        assert result is False
        on_change.assert_not_called()


class TestAppBackendResetThinking:
    def test_happy_path(self, view_with_uuid, tmp_path):
        from claude_island.core.models import project_hash
        slug = project_hash(view_with_uuid.project_path)
        jsonl = tmp_path / slug / "abc-123.jsonl"
        jsonl.parent.mkdir(parents=True)
        jsonl.write_text('{"type":"thinking","thinking":"x","signature":"s"}\n{"type":"assistant","message":{"content":[{"type":"text","text":"hi"}]}}\n')

        names = mock.Mock()
        on_change = mock.Mock()
        backend = LocalAppBackend(
            names_store=names, claude_projects_dir=tmp_path,
            on_change=on_change
        )
        result = backend.reset_thinking(view_with_uuid)
        assert result is True
        on_change.assert_called_once()
        # .bak file was written
        baks = list(tmp_path.rglob("*.bak.*"))
        assert len(baks) == 1

    def test_no_uuid_returns_false(self, view_no_uuid):
        backend = LocalAppBackend(
            names_store=mock.Mock(), claude_projects_dir=Path("/tmp"),
            on_change=mock.Mock()
        )
        assert backend.reset_thinking(view_no_uuid) is False

    def test_file_not_found_returns_false(self, view_with_uuid, tmp_path):
        backend = LocalAppBackend(
            names_store=mock.Mock(), claude_projects_dir=tmp_path,
            on_change=mock.Mock()
        )
        assert backend.reset_thinking(view_with_uuid) is False


# ── OS Backends (subprocess mocking) ──────────────────────────────────

class TestOsBackendRevealCwd:
    def test_macos_opens_path(self):
        from claude_island.platform_.os.macos import MacOsBackend
        from claude_island.core.snapshot import SessionView
        s = Session(pid=1, project_path=Path("/Users/test/foo"), session_uuid="",
                    last_activity=datetime(2026,5,1,12,0,tzinfo=timezone.utc))
        v = replace_view_with_caps(_degraded_view(s), {Capability.REVEAL_CWD})
        backend = MacOsBackend()
        with mock.patch("subprocess.run") as run:
            result = backend.reveal_cwd(v)
            assert result is True
            assert run.call_args[0][0] == ["open", "-R", str(Path("/Users/test/foo"))]

    def test_macos_oserror_caught(self):
        from claude_island.platform_.os.macos import MacOsBackend
        s = Session(pid=1, project_path=Path("/tmp"), session_uuid="",
                    last_activity=datetime(2026,5,1,12,0,tzinfo=timezone.utc))
        v = replace_view_with_caps(_degraded_view(s), {Capability.REVEAL_CWD})
        backend = MacOsBackend()
        with mock.patch("subprocess.run", side_effect=subprocess.TimeoutExpired(cmd=["x"], timeout=3)):
            assert backend.reveal_cwd(v) is False

    def test_windows_explorer_select(self):
        from claude_island.platform_.os.windows import WindowsOsBackend
        s = Session(pid=1, project_path=Path("C:\\Users\\test\\foo"), session_uuid="",
                    last_activity=datetime(2026,5,1,12,0,tzinfo=timezone.utc))
        v = replace_view_with_caps(_degraded_view(s), {Capability.REVEAL_CWD})
        backend = WindowsOsBackend()
        with mock.patch("subprocess.run") as run:
            result = backend.reveal_cwd(v)
            assert result is True
            assert "explorer" in run.call_args[0][0][0]
            assert "C:\\Users\\test\\foo" in run.call_args[0][0][1]


# ── helper ────────────────────────────────────────────────────────────

def replace_view_with_caps(view: SessionView, caps: set[Capability]) -> SessionView:
    from dataclasses import replace
    return replace(view, capabilities=view.capabilities | frozenset(caps))
