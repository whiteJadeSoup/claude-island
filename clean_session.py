#!/usr/bin/env python3
"""Remove thinking blocks from a Claude Code session JSONL file.

Usage:
    python clean_session.py <session-id-or-name-or-project-path>

Examples:
    python clean_session.py 1adbe247-f557-49be-8ed4-2ad65b89aea7
    python clean_session.py cc-learning
    python clean_session.py "D:\\Learning\\cc"
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path


def _resolve(identifier: str) -> Path | None:
    """Find the .jsonl file for the given identifier.

    Resolution order:
    1. UUID (full or 6+ char prefix) → projects/*/<uuid>.jsonl
    2. Session name from sessions/*.json (live/running sessions only)
    3. Project path → find matching projects/ dir
    """
    base = Path.home() / ".claude"
    sessions_dir = base / "sessions"
    projects_dir = base / "projects"

    # Step 1: direct UUID lookup in projects/. sessions/*.json only contains
    # live/running sessions (keyed by PID), so historical UUIDs aren't there —
    # they live as projects/<slug>/<uuid>.jsonl on disk.
    if len(identifier) >= 6 and all(c in "0123456789abcdef-" for c in identifier.lower()):
        ident_lower = identifier.lower()
        for proj_dir in projects_dir.iterdir():
            if not proj_dir.is_dir():
                continue
            for jsonl in proj_dir.glob("*.jsonl"):
                if jsonl.stem.lower().startswith(ident_lower):
                    return jsonl

    # Helper: derive projects/ slug from a cwd string
    def _slug(cwd: str) -> str:
        drive, _, rest = cwd.partition(":")
        drive = drive.upper()
        # Normalise all path separators and spaces to single hyphens, then
        # collapse consecutive hyphens.  Claude Code uses drive--segments
        # without any separator between drive and the first segment.
        backslash = chr(92)
        rest = rest.replace(backslash, " ").replace("/", " ").replace("-", " ")
        while "  " in rest:
            rest = rest.replace("  ", " ")
        rest = rest.strip().replace(" ", "-")
        while "--" in rest:
            rest = rest.replace("--", "-")
        slug = f"{drive}--{rest}" if drive else rest
        return slug

    # Step 2: session name lookup in sessions/ (live sessions only — sessions/
    # entries disappear once a session exits, so name lookup only works while
    # the named session is still running).
    if sessions_dir.exists():
        identifier_lower = identifier.lower()
        for f in sessions_dir.glob("*.json"):
            try:
                obj = json.loads(f.read_text(encoding="utf-8"))
            except Exception:
                continue

            sid = obj.get("sessionId", "")
            name = obj.get("name", "")
            cwd = obj.get("cwd", "")

            if name.lower() == identifier_lower or sid == identifier:
                slug = _slug(cwd)
                jsonl = projects_dir / slug / f"{sid}.jsonl"
                if jsonl.exists():
                    return jsonl

    # Step 3: project path
    proj_path = Path(identifier).resolve()
    if not str(proj_path).startswith(str(Path.home())):
        # Not under home — still try the projects/ dirs
        pass
    input_slug = _slug(identifier)
    for proj_dir in projects_dir.iterdir():
        if not proj_dir.is_dir():
            continue
        if _slug(proj_dir.name) == input_slug:
            # Found the matching project dir — find the newest .jsonl
            candidates = sorted(proj_dir.glob("????????-????-????-????-????????????.jsonl"))
            if candidates:
                return candidates[-1]  # most recently modified

    return None


def _strip_thinking(obj: dict) -> bool:
    """Recursively drop every 'thinking' type block. Returns True if changed."""
    changed = False
    if isinstance(obj, dict):
        for key in list(obj.keys()):
            val = obj[key]
            if isinstance(val, list):
                filtered: list = []
                for item in val:
                    if isinstance(item, dict) and item.get("type") == "thinking":
                        changed = True
                    else:
                        if isinstance(item, dict) and _strip_thinking(item):
                            changed = True
                        filtered.append(item)
                obj[key] = filtered
            elif isinstance(val, dict):
                if _strip_thinking(val):
                    changed = True
    return changed


def clean(identifier: str) -> int:
    path = _resolve(identifier)
    if path is None or not path.exists():
        print(f"[clean] Cannot find session: {identifier!r}", file=sys.stderr)
        sys.exit(1)

    print(f"File: {path}")
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()
    total = cleaned = parse_errors = 0
    for i, line in enumerate(lines):
        stripped = line.strip()
        if not stripped:
            continue
        total += 1
        try:
            obj = json.loads(stripped)
        except json.JSONDecodeError:
            parse_errors += 1
            continue
        if _strip_thinking(obj):
            lines[i] = json.dumps(obj, ensure_ascii=False)
            cleaned += 1

    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"Cleaned {cleaned}/{total} entries, {parse_errors} parse errors skipped")
    return cleaned


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(__doc__)
        sys.exit(1)
    clean(sys.argv[1])