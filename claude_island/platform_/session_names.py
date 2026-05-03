"""Persistent custom session names.

Users can rename a Claude Code session via the right-click detail
popup; the override is stored here under TWO keys so the rename
survives Claude Code's session rotation (``/clear``, ``/resume``,
``/compact`` all mint a fresh sessionId for the same pid):

  - ``<session_uuid>``  — the per-session key. Wins when the user
    has renamed THIS specific session and the sessionId is still the
    same as at rename time.
  - ``:project:<project_path>`` — the per-project fallback. Picks up
    when sessionId rotated and the per-session key no longer matches.
    Acts as the "I named this project" carry-over so subsequent
    sessions in the same dir inherit the rename.

Storage: ``~/.claude-island/session_names.json``::

    {
      "1172b95b-4e6a-...": "frontend refactor",   # session-uuid key
      ":project:/home/me/proj-a": "frontend",     # project key
      ...
    }

The override is **claude-island only** — it does not change the
underlying Windows Terminal tab title (that comes from Claude Code's
conPTY writes and is not addressable from outside the process).
Click-to-activate keeps working because the activator routes by pid +
``GetConsoleTitleW`` output, neither of which depends on this file.

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

# Prefix that distinguishes a project-path key from a session-uuid key.
# Session UUIDs are alphanumeric+dash (no colon); paths can contain ":"
# on Windows ("C:\..."), so the leading sentinel ":project:" must
# itself contain a colon — relying on "no colons in uuids" alone would
# false-positive on Windows drive letters.
_PROJECT_KEY_PREFIX = ":project:"


def _project_key(project_path: str) -> str:
    """Build the storage key for a project-level rename. Normalises
    nothing — callers are expected to feed ``str(session.project_path)``
    consistently so the key matches across reads/writes."""
    return f"{_PROJECT_KEY_PREFIX}{project_path}"


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


def get_session_name(uuid: str, project_path: str | None = None) -> str | None:
    """Return the user's custom name for this session, or ``None``.

    Tries the per-session key (``uuid``) first so an explicit per-
    session rename always wins. Falls back to the per-project key
    (``project_path``) when the session-key misses — that fallback is
    what makes the rename survive Claude Code's ``/clear``-style
    sessionId rotation. Empty values in the file are treated as
    "unset" so the empty-as-delete sentinel doesn't leak through.
    """
    names = _read()
    if uuid:
        name = names.get(uuid)
        if name:
            return name
    if project_path:
        name = names.get(_project_key(project_path))
        if name:
            return name
    return None


def set_session_name(uuid: str, name: str, project_path: str | None = None) -> None:
    """Persist ``name`` as the display override for this session.

    Writes BOTH the per-session key (``uuid``) and, when supplied, the
    per-project key (``project_path``). The dual write is the whole
    point of the design: the project key carries the rename across a
    sessionId rotation that would otherwise orphan the per-session
    entry. ``project_path`` is optional only because the platform
    layer can be exercised without it; the UI always supplies it.

    Empty / whitespace-only ``name`` deletes BOTH keys — the "restore
    default" gesture exposed as saving a blank field. Saves are
    merge-style: other sessions' overrides are preserved.

    No-op when both keys are empty/missing (e.g. the detail popup
    pre-uuid-resolution case).
    """
    cleaned = (name or "").strip()
    keys: list[str] = []
    if uuid:
        keys.append(uuid)
    if project_path:
        keys.append(_project_key(project_path))
    if not keys:
        return
    names = _read()
    changed = False
    if not cleaned:
        for k in keys:
            if k in names:
                names.pop(k)
                changed = True
        if changed:
            _write(names)
        return
    for k in keys:
        if names.get(k) != cleaned:
            names[k] = cleaned
            changed = True
    if changed:
        _write(names)


def delete_session_name(uuid: str, project_path: str | None = None) -> None:
    """Drop the override(s) for this session. Convenience wrapper
    around :func:`set_session_name` with an empty value — clearer at
    the call site than ``set_session_name(..., "")``."""
    set_session_name(uuid, "", project_path=project_path)


def gc_session_names(known_uuids: set[str]) -> None:
    """Drop session-uuid entries whose transcript no longer exists on
    disk. Project-key entries (``:project:<path>`` prefix) are always
    kept — they're stable per directory and we don't track which dirs
    are "still relevant".

    Pure data hygiene — never raises, no-op when the file is missing.
    A ``known_uuids`` of ``set()`` would wipe ALL session-uuid entries
    (since none would be "known"), so callers must populate it before
    invoking — the obvious safety guard against running gc before the
    JSONL parser has indexed anything.
    """
    if not known_uuids:
        return
    names = _read()
    pruned = {
        k: v for k, v in names.items()
        if k.startswith(_PROJECT_KEY_PREFIX) or k in known_uuids
    }
    if pruned == names:
        return
    _write(pruned)
