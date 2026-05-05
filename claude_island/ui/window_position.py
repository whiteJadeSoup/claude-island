"""Persistent capsule window position.

Saves the user's preferred capsule X/Y to disk so a drag survives
restarts. Storage shape and write discipline mirror
``platform_/session_names.py`` — atomic write via tmp + replace, in-
process lock for safety, errors silenced as warnings (window position
is best-effort; never crash the UI over a failed save).

Storage: ``~/.claude-island/window.json``::

    {
      "x": 1234,
      "y": 8
    }

Coordinates are in **global desktop space** (the QPoint returned by
``QWidget.pos()``). On multi-monitor setups this means the saved
position references one specific monitor's coordinate slot — if that
monitor is later disconnected, the load helper falls back to centred
on the primary screen rather than restoring an off-screen position.

Both axes are stored: horizontal-drag keeps Y at the top margin, but
the long-press free-drag mode lets the user reposition both axes —
the saved Y is what restores the user to whichever edge they docked at.

Cross-platform: paths use ``Path.home()`` (works on Windows / macOS /
Linux). The atomic-rename idiom uses ``os.replace`` which is the
standard cross-platform atomic file rename.
"""
from __future__ import annotations

import json
import os
import sys
import threading
from pathlib import Path

# Resolved at call time (not module-load time) so tests can monkeypatch
# this attribute to point at a tmp_path fixture. Mirrors the pattern
# used in platform_/session_names.py and platform_/providers/__init__.py.
WINDOW_POSITION_PATH = Path.home() / ".claude-island" / "window.json"

# Single-process serialisation. Save can be triggered by mouseRelease
# while a hypothetical future autosave-on-snapshot path could fire from
# a different thread; the lock keeps read-modify-write atomic. Cheap —
# capsule drag is human-paced, not a hot path.
_lock = threading.Lock()


def load_position() -> tuple[int, int] | None:
    """Read the saved (x, y) tuple. Returns None when the file is
    missing, malformed, or contains non-int values — caller falls
    back to its default (typically primary-screen-top-centre).

    Never raises. A corrupted window.json should not break startup —
    losing the saved position is recoverable; failing to launch is not.
    """
    try:
        text = WINDOW_POSITION_PATH.read_text(encoding="utf-8")
        data = json.loads(text)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    x = data.get("x")
    y = data.get("y")
    if not isinstance(x, int) or not isinstance(y, int):
        return None
    return (x, y)


def save_position(x: int, y: int) -> None:
    """Atomic write of the window position to ``WINDOW_POSITION_PATH``.

    Uses tmp file + ``os.replace`` so a crash mid-write leaves the
    previous file intact (or a fresh one — never half-written JSON
    that would fail load_position next start).

    Errors print to stderr but never raise — same discipline as
    ``platform_/session_names.py``. The user's drag intent is best-
    effort persistence; a save failure shouldn't crash the capsule
    or interrupt the next drag.
    """
    with _lock:
        try:
            WINDOW_POSITION_PATH.parent.mkdir(parents=True, exist_ok=True)
            tmp = WINDOW_POSITION_PATH.with_suffix(".json.tmp")
            tmp.write_text(
                json.dumps({"x": x, "y": y}),
                encoding="utf-8",
            )
            os.replace(tmp, WINDOW_POSITION_PATH)
        except OSError as exc:
            print(
                f"[claude-island] failed to save window position: {exc}",
                file=sys.stderr,
            )
