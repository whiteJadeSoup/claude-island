"""Encoding-safe stderr writer.

Default ``sys.stderr`` on non-UTF-8 Windows consoles (Chinese GBK /
cp936, Japanese cp932, Western cp1252, etc.) raises UnicodeEncodeError
when ``print`` is asked to emit characters outside that codec.

Several call sites in this codebase route exception messages or Qt
warning lines through stderr. Any one of them tripping over an emoji
or a CJK character would leave the calling context in a bad state —
particularly the Qt message handler installed by __main__, where an
exception inside the handler leaves Qt's diagnostic pipeline in an
undefined state on the next emit.

Lives in core because it has no UI / OS dependencies — it's pure
Python textmode IO with a robust fallback. Imported by __main__ (Qt
message filter) and by anything else that prints to stderr in paths
that may include user-supplied text.
"""
from __future__ import annotations

import sys


def safe_stderr_write(text: str) -> None:
    """Write ``text + "\\n"`` to ``sys.stderr``, never raising.

    Strategy:
      1. Native write — preserves emoji on UTF-8 consoles (modern
         Win Terminal, macOS, Linux).
      2. On UnicodeEncodeError, re-encode with errors='replace' so
         non-encodable chars become '?' rather than crashing.
      3. On any other write/flush failure (closed stderr, OSError),
         silently return — better silent than propagating an
         exception out of e.g. a Qt message handler callback."""
    enc = getattr(sys.stderr, "encoding", None) or "utf-8"
    try:
        sys.stderr.write(text + "\n")
    except UnicodeEncodeError:
        safe = text.encode(enc, errors="replace").decode(enc, errors="replace")
        try:
            sys.stderr.write(safe + "\n")
        except Exception:
            return
    except Exception:
        return
    try:
        sys.stderr.flush()
    except Exception:
        pass
