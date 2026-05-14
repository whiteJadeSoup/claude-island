"""Tests for NotifyBackend implementations.

Covers:
  T2.6 — MacOsNotifyBackend invokes osascript with proper args (mocked
         subprocess; we never actually call osascript in CI to avoid
         triggering the system notification permission prompt)
  T2.7 — WindowsNotifyBackend falls back to QSystemTrayIcon when winrt
         is unavailable
  T4.1 — Both backends satisfy the NotifyBackend Protocol
  Noop — records calls + always returns True
"""
from __future__ import annotations

import subprocess
import sys
from typing import Any
from unittest import mock

import pytest

from claude_island.platform_.notify import (
    MacOsNotifyBackend,
    NoopNotifyBackend,
    NotifyBackend,
    NotifyKindHint,
    WindowsNotifyBackend,
)


# ── Protocol conformance (G4) ────────────────────────────────────────


class TestProtocolConformance:
    def test_macos_satisfies_protocol(self):
        b = MacOsNotifyBackend()
        assert isinstance(b, NotifyBackend)

    def test_windows_satisfies_protocol(self):
        b = WindowsNotifyBackend()
        assert isinstance(b, NotifyBackend)

    def test_noop_satisfies_protocol(self):
        b = NoopNotifyBackend()
        assert isinstance(b, NotifyBackend)


# ── NoopNotifyBackend ────────────────────────────────────────────────


class TestNoopBackend:
    def test_post_returns_true(self):
        b = NoopNotifyBackend()
        assert b.post(title="t", body="b") is True

    def test_records_calls(self):
        b = NoopNotifyBackend()
        b.post(title="hi", body="world")
        b.post(title="x", body="y", kind=NotifyKindHint.WARN)
        calls = b.posted_calls
        assert len(calls) == 2
        assert calls[0] == ("hi", "world", NotifyKindHint.INFO)
        assert calls[1] == ("x", "y", NotifyKindHint.WARN)

    def test_clear(self):
        b = NoopNotifyBackend()
        b.post(title="hi", body="world")
        b.clear()
        assert b.posted_calls == []


# ── MacOsNotifyBackend ───────────────────────────────────────────────


class TestMacOsBackend:
    def test_invokes_osascript_with_title_and_body(self):
        b = MacOsNotifyBackend()
        with mock.patch(
            "subprocess.run",
            return_value=mock.Mock(returncode=0, stderr=b""),
        ) as run:
            ok = b.post(title="claude-island", body="turn complete")
        assert ok is True
        assert run.call_count == 1
        argv = run.call_args[0][0]
        assert argv[0] == "/usr/bin/osascript"
        assert argv[1] == "-e"
        script = argv[2]
        assert 'display notification "turn complete"' in script
        assert 'with title "claude-island"' in script

    def test_warn_kind_includes_sound(self):
        b = MacOsNotifyBackend()
        with mock.patch(
            "subprocess.run",
            return_value=mock.Mock(returncode=0, stderr=b""),
        ) as run:
            b.post(title="t", body="b", kind=NotifyKindHint.WARN)
        script = run.call_args[0][0][2]
        assert 'sound name "Glass"' in script

    def test_info_kind_no_sound(self):
        b = MacOsNotifyBackend()
        with mock.patch(
            "subprocess.run",
            return_value=mock.Mock(returncode=0, stderr=b""),
        ) as run:
            b.post(title="t", body="b", kind=NotifyKindHint.INFO)
        script = run.call_args[0][0][2]
        assert "sound name" not in script

    def test_returns_false_on_nonzero_exit(self):
        b = MacOsNotifyBackend()
        with mock.patch(
            "subprocess.run",
            return_value=mock.Mock(returncode=1, stderr=b"some error"),
        ):
            assert b.post(title="t", body="b") is False

    def test_returns_false_on_timeout(self):
        b = MacOsNotifyBackend()
        with mock.patch(
            "subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd=["x"], timeout=3),
        ):
            assert b.post(title="t", body="b") is False

    def test_returns_false_on_oserror(self):
        b = MacOsNotifyBackend()
        with mock.patch(
            "subprocess.run", side_effect=FileNotFoundError("no osascript"),
        ):
            assert b.post(title="t", body="b") is False

    def test_failure_logged_only_once(self, caplog):
        b = MacOsNotifyBackend()
        with mock.patch(
            "subprocess.run",
            return_value=mock.Mock(returncode=1, stderr=b"err"),
        ):
            for _ in range(5):
                b.post(title="t", body="b")
        warnings = [r for r in caplog.records if r.levelname == "WARNING"]
        assert len(warnings) == 1

    def test_escapes_quotes_in_title(self):
        b = MacOsNotifyBackend()
        with mock.patch(
            "subprocess.run",
            return_value=mock.Mock(returncode=0, stderr=b""),
        ) as run:
            b.post(title='Has "quotes"', body="b")
        script = run.call_args[0][0][2]
        # Quotes should be escaped, not raw
        assert 'with title "Has \\"quotes\\""' in script

    def test_strips_newlines_in_body(self):
        # Newlines would terminate the AppleScript literal — replaced with space.
        b = MacOsNotifyBackend()
        with mock.patch(
            "subprocess.run",
            return_value=mock.Mock(returncode=0, stderr=b""),
        ) as run:
            b.post(title="t", body="line1\nline2")
        script = run.call_args[0][0][2]
        assert "\n" not in script.split("display notification ")[1].split('"')[1]


# ── WindowsNotifyBackend ─────────────────────────────────────────────


class _FakeTrayIcon:
    """Minimal stand-in for QSystemTrayIcon — records showMessage calls."""
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, int, int]] = []
    def showMessage(self, title: str, body: str, icon: int, msec: int) -> None:
        self.calls.append((title, body, icon, msec))


class TestWindowsBackend:
    def test_falls_back_to_tray_when_winrt_import_fails(self):
        # Simulate winrt unavailable: the lazy import in _try_winrt_post
        # raises ImportError → backend marks unusable + uses tray.
        tray = _FakeTrayIcon()
        b = WindowsNotifyBackend(tray_icon=tray)
        # Patch the import to fail
        with mock.patch.dict(sys.modules, {"winsdk.windows.data.xml.dom": None}):
            # Subsequent first post should try winrt, fail, fall back to tray
            ok = b.post(title="t", body="b")
        # Even if winrt path fails for an unrelated reason in the import,
        # tray fallback should keep ok=True.
        # Note: depending on test environment winsdk might be missing
        # entirely, in which case the import-style mock above is moot —
        # the lazy import simply fails. Either way we want tray called.
        assert ok is True
        assert len(tray.calls) == 1
        assert tray.calls[0][0] == "t"
        assert tray.calls[0][1] == "b"

    def test_returns_false_when_no_tray_and_no_winrt(self):
        # Test environment likely has no winsdk; without tray, post fails.
        b = WindowsNotifyBackend(tray_icon=None)
        ok = b.post(title="t", body="b")
        # The first call probes winrt (fails on non-Windows / no winsdk),
        # then tries tray (None) → False.
        assert ok is False

    def test_tray_kind_maps_to_icon_value(self):
        tray = _FakeTrayIcon()
        b = WindowsNotifyBackend(tray_icon=tray)
        # Force winrt unusable first.
        b._winrt_usable = False
        b.post(title="t", body="b", kind=NotifyKindHint.INFO)
        b.post(title="t", body="b", kind=NotifyKindHint.WARN)
        b.post(title="t", body="b", kind=NotifyKindHint.ERROR)
        assert tray.calls[0][2] == 1   # Information
        assert tray.calls[1][2] == 2   # Warning
        assert tray.calls[2][2] == 3   # Critical

    def test_failure_logged_only_once(self, caplog):
        b = WindowsNotifyBackend(tray_icon=None)
        for _ in range(5):
            b.post(title="t", body="b")
        # Two distinct failure keys: "winrt" and "tray". Each logs ONCE total.
        warnings = [r for r in caplog.records if r.levelname == "WARNING"]
        assert len(warnings) <= 2  # could be 1 (winrt) + 1 (tray)

    def test_marks_winrt_unusable_after_first_import_failure(self):
        b = WindowsNotifyBackend(tray_icon=_FakeTrayIcon())
        b.post(title="t", body="b")
        # After first call (winrt import will fail in CI), further calls
        # should skip winrt entirely.
        # NOTE: this relies on the CI not having winsdk; fine for our setup.
        assert b._winrt_usable is False
