"""Persistent custom session names.

Users can rename a Claude Code session via the right-click detail
popup; the override is stored here keyed by the session's transcript
UUID (the JSONL filename stem) — a strict per-session identifier so
renaming one session never bleeds into others in the same project.

Storage: ``~/.claude-island/session_names.json``::

    {
      "1172b95b-4e6a-...": "frontend refactor",
      ...
    }

The override is **claude-island only** — it does not change the
underlying Windows Terminal tab title (that comes from Claude Code's
conPTY writes and is not addressable from outside the process).
Click-to-activate keeps working because the activator routes by pid +
``GetConsoleTitleW`` output, neither of which depends on this file.

Trade-off note: an earlier design also wrote a per-project fallback
key so the rename would survive Claude Code's ``/clear`` /
``/resume`` minting a fresh sessionId for the same pid. That fallback
was removed because it caused renames to bleed across sibling
sessions sharing a project_path — a worse UX than occasionally
"losing" a rename on session rotation.

API mirrors ``platform_/providers/__init__.py`` (atomic write, lazy
path resolution for tests, errors silenced as warnings) so it reads
as the same family of helpers.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

# Resolved at call time (not module-load time) so tests can monkeypatch
# the module attribute. Same pattern as PROVIDER_CONFIG_PATH in
# claude_island.platform_.providers.
SESSION_NAMES_PATH = Path.home() / ".claude-island" / "session_names.json"


def _read(path: Path | None = None) -> dict[str, str]:
    """Parse session_names.json into a {uuid: name} dict.

    Returns ``{}`` on any read / parse failure (missing file, malformed
    JSON, wrong shape) so callers can do a plain ``.get(uuid)`` without
    try/except sprinkles. Non-string values are filtered out so a
    corrupted entry doesn't crash the UI.
    """
    if path is None:
        path = SESSION_NAMES_PATH
    try:
        text = path.read_text(encoding="utf-8")
        data = json.loads(text)
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {k: v for k, v in data.items() if isinstance(k, str) and isinstance(v, str)}


def _write(names: dict[str, str], path: Path | None = None) -> None:
    """Atomic write of session_names.json (tmp + os.replace).

    Failures print a warning to stderr but never raise — losing the
    write means the rename doesn't persist across restarts, which is
    annoying but not a crash worth bubbling up. The in-process state
    is unaffected.
    """
    if path is None:
        path = SESSION_NAMES_PATH
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(names, indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)
    except OSError as e:
        print(f"[claude-island] session_names.json write failed: {e}", file=sys.stderr)


def get_session_name(uuid: str) -> str | None:
    """Return the user's custom name for ``uuid``, or ``None`` if unset.

    Per-session lookup only — there's no project-level fallback (an
    earlier design caused renames to bleed across sibling sessions in
    the same directory). Returned strings are guaranteed non-empty
    (an empty value in the file is treated as "unset" so the
    empty-as-delete sentinel doesn't leak through).
    """
    if not uuid:
        return None
    name = _read().get(uuid)
    return name if name else None


def set_session_name(uuid: str, name: str) -> None:
    """Persist ``name`` as the display override for ``uuid``.

    Empty / whitespace-only ``name`` deletes the entry — that's the
    "go back to the auto-detected name" gesture, exposed in the UI as
    saving a blank field. Saves are merge-style: other sessions'
    overrides are preserved.

    No-op when ``uuid`` is empty (the detail popup may pass an empty
    uuid for sessions whose transcript hasn't been resolved yet; we'd
    rather quietly skip than corrupt the file with a "" key).
    """
    if not uuid:
        return
    cleaned = (name or "").strip()
    names = _read()
    if not cleaned:
        if uuid in names:
            names.pop(uuid)
            _write(names)
        return
    if names.get(uuid) == cleaned:
        return  # idempotent — skip the write if nothing changed
    names[uuid] = cleaned
    _write(names)


def delete_session_name(uuid: str) -> None:
    """Drop ``uuid`` from the override map. Convenience wrapper around
    :func:`set_session_name` with an empty value — clearer at the
    call site than ``set_session_name(uuid, "")``."""
    set_session_name(uuid, "")


def gc_session_names(known_uuids: set[str]) -> None:
    """Drop entries whose session_uuid no longer corresponds to a known
    transcript. Called periodically so renamed-then-closed sessions
    don't accumulate forever in the override file.

    Pure data hygiene — never raises, no-op when the file is missing.
    A ``known_uuids`` of ``set()`` would wipe everything, so callers
    must populate it before invoking — the obvious safety guard
    against running gc before the JSONL parser has indexed anything.
    """
    if not known_uuids:
        return
    names = _read()
    pruned = {uuid: n for uuid, n in names.items() if uuid in known_uuids}
    if pruned == names:
        return
    _write(pruned)
