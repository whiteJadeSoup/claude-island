"""FD-level stderr noise filter for known-harmless OS log spam.

Some macOS system logs (Input Method Kit, font CoreText fallbacks)
write straight to file descriptor 2 via ``NSLog`` / ``os_log``,
bypassing both Python's ``sys.stderr`` and Qt's ``qInstallMessageHandler``.
That makes them impossible to suppress at either layer.

This module hijacks FD 2 on import: the original FD is duplicated for
later writes, then a pipe takes its place so every byte that any C
library writes to stderr lands in our reader thread first. Lines
matching a known-noise regex are dropped; everything else is forwarded
unchanged. The reader is daemonised so it dies with the process — no
shutdown wiring needed.

Activated only on macOS (Linux / Windows have their own log channels
and don't emit these specific messages). Lives in core because it has
no Qt / PySide dependency — it's pure stdlib FD plumbing.
"""
from __future__ import annotations

import atexit
import os
import re
import sys
import threading
import time

# Patterns of stderr lines we drop. Each pattern matches a known-harmless
# macOS system log line that adds noise without informational value.
#
# - IMKCFRunLoopWakeUpReliable: Input Method Kit warning emitted when the
#   IME mach port handshake takes a tick longer than expected. Apple's
#   own apps emit it too; documented as cosmetic on Sonoma+.
# - "in CoreText" font fallbacks: emitted when CT walks the cascade list;
#   harmless and triggered by any non-Latin glyph rendering.
_NOISE_PATTERNS = [
    re.compile(rb"error messaging the mach port for IMKCFRunLoopWakeUpReliable"),
    re.compile(rb"CoreText note:.*?fallback"),
]

_installed = False
_install_lock = threading.Lock()


def install() -> None:
    """Hijack FD 2 with a noise-filtering pipe. Idempotent.

    Must be called before any C library writes to stderr — practically
    that means very early in ``__main__.py``, before Qt or pyobjc are
    imported. Calling later still works but won't catch lines that
    were already written.
    """
    global _installed
    if sys.platform != "darwin":
        return
    with _install_lock:
        if _installed:
            return
        _installed = True

    # Save the real stderr FD so we can write survivors to it.
    real_stderr_fd = os.dup(2)
    real_stderr_writer = os.fdopen(real_stderr_fd, "wb", buffering=0)

    read_fd, write_fd = os.pipe()
    os.dup2(write_fd, 2)
    os.close(write_fd)
    # Re-open Python's sys.stderr against the new FD 2 so
    # print(..., file=sys.stderr) still flows through the filter
    # rather than holding a stale reference to the original FD.
    sys.stderr = os.fdopen(2, "w", buffering=1, errors="backslashreplace")

    def _pump() -> None:
        # Buffered line reads keep partial writes intact: a C library
        # that writes a header then a body in two write() calls still
        # produces one filterable line.
        with os.fdopen(read_fd, "rb", buffering=0) as src:
            buf = b""
            while True:
                chunk = src.read(4096)
                if not chunk:
                    return
                buf += chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    if not any(p.search(line) for p in _NOISE_PATTERNS):
                        try:
                            real_stderr_writer.write(line + b"\n")
                        except OSError:
                            return

    pump_thread = threading.Thread(
        target=_pump, daemon=True, name="stderr-noise-filter",
    )
    pump_thread.start()

    def _drain_at_exit() -> None:
        """Give the pump thread a brief window to flush remaining
        bytes after sys.stderr is flushed but before the process dies.
        Without this, writes that landed in the pipe in the last few
        ms before exit (typical: shutdown messages, atexit-registered
        prints) are lost. 50 ms is more than enough for a healthy
        pipe and short enough that quit feels instant.
        """
        try:
            sys.stderr.flush()
        except Exception:
            pass
        time.sleep(0.05)

    atexit.register(_drain_at_exit)
