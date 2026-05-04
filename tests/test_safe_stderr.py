"""Regression test for the encoding-safe stderr write helper.

Why this test exists:
* Default ``sys.stderr`` on non-UTF-8 Windows consoles (Chinese GBK,
  Japanese cp932, Western cp1252, etc.) raises UnicodeEncodeError when
  ``print(text, file=sys.stderr)`` is asked to emit characters outside
  that codec.
* The Qt message handler, the render-error wrapper, and a few startup
  exception paths all route through this — a crash there would leave
  Qt in an undefined state on the next emit (silent diagnostic loss).
* The HistoryDrawer feature added widget labels with emoji (🗂 ⏳ 🛡)
  that increase the surface area for Qt internals to log strings
  containing those characters.

Strategy: monkeypatch sys.stderr with a TextIOWrapper that uses GBK +
strict — exactly the user's environment — then call safe_stderr_write
with emoji and assert it does not raise + the bytes that landed are a
GBK-encodable replacement.
"""
from __future__ import annotations

import io
import sys

from claude_island.core.safe_stderr import safe_stderr_write


def test_handles_gbk_emoji(monkeypatch):
    """Reproduce the user's Chinese-Windows env: stderr is GBK + strict.
    Writing emoji must not raise."""
    raw = io.BytesIO()
    fake = io.TextIOWrapper(raw, encoding="gbk", errors="strict", write_through=True)
    monkeypatch.setattr(sys, "stderr", fake)

    # Should NOT raise. The U+1F5C2 'card index dividers' emoji is what
    # the history chip uses; U+1F6E1 is the bypass shield.
    safe_stderr_write("history chip \U0001f5c2 launched 5 sessions, shield \U0001f6e1")

    output = raw.getvalue().decode("gbk")
    # Replacement chars (?) appear where emoji were — this is what we want
    # over a raised exception.
    assert "history chip" in output
    assert "launched 5 sessions" in output


def test_passes_through_ascii_unchanged(monkeypatch):
    """Plain ASCII goes through unchanged in any encoding."""
    raw = io.BytesIO()
    fake = io.TextIOWrapper(raw, encoding="gbk", errors="strict", write_through=True)
    monkeypatch.setattr(sys, "stderr", fake)

    safe_stderr_write("[claude-island] expanded.render(snap) raised: KeyError('foo')")
    output = raw.getvalue().decode("gbk")
    assert "[claude-island] expanded.render(snap) raised: KeyError('foo')" in output
    assert output.endswith("\n")


def test_preserves_emoji_on_utf8_console(monkeypatch):
    """Modern terminals (Windows Terminal, macOS, Linux) are UTF-8 —
    the helper must NOT lose emoji there."""
    raw = io.BytesIO()
    fake = io.TextIOWrapper(raw, encoding="utf-8", errors="strict", write_through=True)
    monkeypatch.setattr(sys, "stderr", fake)

    safe_stderr_write("history chip \U0001f5c2 ok")
    output = raw.getvalue().decode("utf-8")
    assert "\U0001f5c2" in output


def test_survives_broken_stderr(monkeypatch):
    """Even when sys.stderr.write itself blows up, the helper must
    return — never propagate the exception out of the message handler."""

    class _Broken:
        encoding = "utf-8"
        def write(self, _):
            raise OSError("disk full")
        def flush(self):
            raise OSError("disk full")
    monkeypatch.setattr(sys, "stderr", _Broken())
    safe_stderr_write("anything")  # must not raise


def test_survives_stderr_with_no_encoding_attr(monkeypatch):
    """Non-standard stderr replacements (custom logging proxies, etc.)
    sometimes lack an .encoding attr. Must fall back to utf-8."""

    class _NoEnc:
        def write(self, s):
            self.last = s
        def flush(self):
            pass
    sink = _NoEnc()
    monkeypatch.setattr(sys, "stderr", sink)
    safe_stderr_write("anything")
    assert "anything" in sink.last
