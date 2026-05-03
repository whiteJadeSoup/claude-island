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
        import json
        from claude_island.core.models import project_hash
        slug = project_hash(view_with_uuid.project_path)
        jsonl = tmp_path / slug / "abc-123.jsonl"
        jsonl.parent.mkdir(parents=True)
        # An assistant turn whose content[] mixes a thinking block (the
        # one to strip) and a text block (the one to keep). This is the
        # shape strip_thinking_blocks's recursive walker actually targets
        # — it drops thinking entries inside content arrays, leaving the
        # surrounding row intact.
        jsonl.write_text(json.dumps({
            "message": {"content": [
                {"type": "thinking", "thinking": "x", "signature": "abc"},
                {"type": "text", "text": "hello"},
            ]}
        }) + "\n", encoding="utf-8")

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
        # S-2 (review feedback): assert the actual contract — the
        # thinking block is gone from content[], the text block survives.
        # A no-op implementation that wrote a .bak without filtering
        # would have passed the old test, hiding correctness regressions.
        cleaned = json.loads(jsonl.read_text(encoding="utf-8").strip())
        types = [c.get("type") for c in cleaned["message"]["content"]]
        assert types == ["text"]

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
        completed = mock.Mock(returncode=0)
        with mock.patch("subprocess.run", return_value=completed) as run:
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
        # explorer.exe returns 1 even on success — see windows.py docstring;
        # we treat any completed run as OK, so returncode is irrelevant.
        with mock.patch("subprocess.run", return_value=mock.Mock(returncode=1)) as run:
            result = backend.reveal_cwd(v)
            assert result is True
            assert "explorer" in run.call_args[0][0][0]
            assert "C:\\Users\\test\\foo" in run.call_args[0][0][1]


class TestOsBackendCopyPath:
    """Pin the encoding contracts for clipboard writes — pbcopy reads
    UTF-8, clip.exe needs a UTF-16 BOM. Mock subprocess so the test
    runs anywhere without actually touching the host clipboard."""

    def test_macos_pbcopy_utf8(self):
        from claude_island.platform_.os.macos import MacOsBackend
        # Use a Path that round-trips identically across OSes — pytest
        # runs this test on Windows too (where Path("/x/y") becomes
        # WindowsPath("\\x\\y")).
        path_str = "/Users/test/项目"
        s = Session(pid=1, project_path=Path(path_str), session_uuid="",
                    last_activity=datetime(2026,5,1,12,0,tzinfo=timezone.utc))
        v = replace_view_with_caps(_degraded_view(s), {Capability.COPY_PATH})
        backend = MacOsBackend()
        with mock.patch("subprocess.run", return_value=mock.Mock(returncode=0)) as run:
            assert backend.copy_path(v) is True
            assert run.call_args[0][0] == ["pbcopy"]
            # Compare against str(Path(...)) so the assertion holds on
            # both POSIX (forward slashes) and Windows (backslashes).
            assert run.call_args.kwargs["input"] == str(Path(path_str)).encode("utf-8")

    def test_macos_returns_false_on_nonzero_exit(self):
        from claude_island.platform_.os.macos import MacOsBackend
        s = Session(pid=1, project_path=Path("/x"), session_uuid="",
                    last_activity=datetime(2026,5,1,12,0,tzinfo=timezone.utc))
        v = replace_view_with_caps(_degraded_view(s), {Capability.COPY_PATH})
        backend = MacOsBackend()
        with mock.patch("subprocess.run", return_value=mock.Mock(returncode=1)):
            assert backend.copy_path(v) is False

    def test_windows_clip_has_utf16_bom(self):
        """clip.exe identifies UTF-16 LE input via the \\xff\\xfe BOM
        prefix; without it the OEM codepage decoder mojibakes any
        non-ASCII character. Pin the BOM so a future "performance fix"
        that drops the prefix breaks this test loudly."""
        from claude_island.platform_.os.windows import WindowsOsBackend
        s = Session(pid=1, project_path=Path("D:/项目/test"), session_uuid="",
                    last_activity=datetime(2026,5,1,12,0,tzinfo=timezone.utc))
        v = replace_view_with_caps(_degraded_view(s), {Capability.COPY_PATH})
        backend = WindowsOsBackend()
        with mock.patch("subprocess.run", return_value=mock.Mock(returncode=0)) as run:
            assert backend.copy_path(v) is True
            payload: bytes = run.call_args.kwargs["input"]
            assert payload.startswith(b"\xff\xfe"), "clip.exe input is missing UTF-16-LE BOM"
            decoded = payload[2:].decode("utf-16-le")
            assert decoded == "D:\\项目\\test" or decoded == "D:/项目/test"

    def test_windows_returns_false_on_nonzero_exit(self):
        from claude_island.platform_.os.windows import WindowsOsBackend
        s = Session(pid=1, project_path=Path("C:/x"), session_uuid="",
                    last_activity=datetime(2026,5,1,12,0,tzinfo=timezone.utc))
        v = replace_view_with_caps(_degraded_view(s), {Capability.COPY_PATH})
        backend = WindowsOsBackend()
        with mock.patch("subprocess.run", return_value=mock.Mock(returncode=1)):
            assert backend.copy_path(v) is False


# ── helper ────────────────────────────────────────────────────────────

def replace_view_with_caps(view: SessionView, caps: set[Capability]) -> SessionView:
    from dataclasses import replace
    return replace(view, capabilities=view.capabilities | frozenset(caps))
