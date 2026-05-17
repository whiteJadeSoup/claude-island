"""Probe: for each running claude.exe pid, find the JSONL file Claude is
actively writing to — by listing the project's JSONL dir and picking the
file with mtime >= pid.startedAt.

This is the heuristic needed when ``--resume <name>`` (not UUID) is used:
the cmdline has no UUID to recover, but the JSONL the process keeps
writing to is unambiguous evidence of which uuid is "real" for that pid.
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

import psutil


def _project_dir_for_cwd(projects_root: Path, cwd: Path) -> Path | None:
    """Claude's project directory naming: drop drive colon, replace
    path separators AND spaces with ``-``. Match case-insensitively
    (Windows volume casing varies across sessions).

    Returns the matched directory or None on miss / ambiguity.

    Examples:
      ``D:\\Learning\\cc``                  → ``D--Learning-cc``
      ``D:\\coding projects\\build-mini-cc`` → ``D--coding-projects-build-mini-cc``
    """
    raw = str(cwd)
    # Drive letter normalisation: ``D:\foo`` / ``D:/foo`` → ``D--foo``.
    if len(raw) >= 2 and raw[1] == ":":
        raw = raw[0] + "--" + raw[2:].lstrip("\\/")
    name = (
        raw.replace("\\", "-")
        .replace("/", "-")
        .replace(" ", "-")
    )
    while "---" in name:
        name = name.replace("---", "--")
    target = name.lower()
    for d in projects_root.iterdir():
        if d.is_dir() and d.name.lower() == target:
            return d
    return None


def find_active_jsonl_uuid(pid: int, cwd: Path) -> str | None:
    try:
        proc = psutil.Process(pid)
        started_at = datetime.fromtimestamp(proc.create_time())
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return None
    projects_root = Path.home() / ".claude" / "projects"
    proj_dir = _project_dir_for_cwd(projects_root, cwd)
    if proj_dir is None:
        return None
    candidates: list[tuple[Path, datetime]] = []
    for f in proj_dir.glob("*.jsonl"):
        try:
            mtime = datetime.fromtimestamp(f.stat().st_mtime)
        except OSError:
            continue
        if mtime >= started_at:
            candidates.append((f, mtime))
    if not candidates:
        return None
    # Most-recent first; if multiple, prefer largest (the actively-
    # written-to one will tend to dominate).
    candidates.sort(key=lambda x: (x[1], x[0].stat().st_size), reverse=True)
    return candidates[0][0].stem


def main() -> None:
    targets = [
        (69248, r"D:\Learning\cc", "cc-learning"),
        (97372, r"D:\coding projects\build-mini-cc", "build-mini-cc"),
        (99872, r"D:\coding projects\build-mini-cc", "mini-cc-opus-dev"),
        (113200, r"D:\coding projects\claude-island", "claude-island"),
    ]
    for pid, cwd, label in targets:
        uuid = find_active_jsonl_uuid(pid, Path(cwd))
        print(f"{label:25s} pid={pid:>6}  active JSONL uuid = {uuid}")


if __name__ == "__main__":
    main()
