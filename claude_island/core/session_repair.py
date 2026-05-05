"""Session-transcript repair: strip ``thinking`` blocks from JSONL.

Why this exists: Claude's extended-thinking responses contain blocks
like ``{"type": "thinking", "thinking": "...", "signature": "..."}``
that are bound to Anthropic's signing key. When the user routes a
session through a non-Anthropic provider (MiniMax / Kimi / etc) and
then routes back to Claude, those signatures become invalid and the
API rejects the next call with::

    400 invalid_request_error
    messages.X.content.0: Invalid `signature` in `thinking` block

The fix is to drop the thinking blocks from the historical transcript
so the next prompt's payload no longer includes them.

This module is the pure operation; the standalone
``scripts/clean_session.py`` script wraps it with identifier
resolution (UUID / name / path) for CLI use, and the GUI
(``SessionDetailPopup``) calls it directly with a known transcript
path.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path


def strip_thinking_blocks(jsonl_path: Path) -> int:
    """Remove every ``thinking``-typed block from a session JSONL.

    Atomic + reversible:
      1. Reads the original file.
      2. Filters thinking blocks out of every line.
      3. Writes the cleaned text to a sibling ``.tmp`` file, renames
         the original to ``<file>.bak.<unix-ts>``, then renames the
         tmp into place. Power-loss between steps leaves either the
         original (if the rename never happened) or the cleaned file
         alongside the .bak (if the rename completed).
      4. Returns the number of JSONL rows that had thinking blocks
         removed (zero when nothing needed cleaning — the .bak is
         still written for symmetry / discoverability).

    Raises:
        FileNotFoundError: when ``jsonl_path`` doesn't exist.
        OSError: on any underlying disk failure (passed through so the
            caller can surface it to the user).
    """
    if not jsonl_path.exists():
        raise FileNotFoundError(jsonl_path)

    text = jsonl_path.read_text(encoding="utf-8")
    cleaned_lines: list[str] = []
    cleaned_count = 0

    for raw_line in text.splitlines():
        stripped = raw_line.strip()
        if not stripped:
            cleaned_lines.append(raw_line)
            continue
        try:
            obj = json.loads(stripped)
        except json.JSONDecodeError:
            # Preserve unparseable lines verbatim — better to leave a
            # row we can't understand alone than corrupt the file.
            cleaned_lines.append(raw_line)
            continue
        if _strip_recursive(obj):
            cleaned_count += 1
            cleaned_lines.append(json.dumps(obj, ensure_ascii=False))
        else:
            cleaned_lines.append(raw_line)

    new_text = "\n".join(cleaned_lines) + ("\n" if text.endswith("\n") else "")

    # Backup with a timestamp so repeated repairs don't clobber each
    # other. Unix epoch is enough granularity — the user wouldn't run
    # this twice in the same second.
    backup_path = jsonl_path.with_suffix(jsonl_path.suffix + f".bak.{int(time.time())}")
    tmp_path = jsonl_path.with_suffix(jsonl_path.suffix + ".tmp")
    tmp_path.write_text(new_text, encoding="utf-8")
    os.replace(jsonl_path, backup_path)
    os.replace(tmp_path, jsonl_path)
    return cleaned_count


def _strip_recursive(obj: object) -> bool:
    """Drop every ``{"type": "thinking", ...}`` block in-place.

    Returns True if anything was removed. Walks any dict / list nested
    under the entry — Claude Code wraps content in slightly different
    shapes across CLI versions, and recursion handles all of them
    without us having to track the exact path.
    """
    changed = False
    if isinstance(obj, dict):
        for key in list(obj.keys()):
            val = obj[key]
            if isinstance(val, list):
                filtered: list = []
                for item in val:
                    if isinstance(item, dict) and item.get("type") == "thinking":
                        changed = True
                        continue
                    if isinstance(item, dict) and _strip_recursive(item):
                        changed = True
                    filtered.append(item)
                obj[key] = filtered
            elif isinstance(val, dict):
                if _strip_recursive(val):
                    changed = True
    return changed
