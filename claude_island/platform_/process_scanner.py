from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import psutil

from claude_island.core.models import Session

# Claude Code is a Node.js CLI; on Windows it may appear as node.exe wrapping
# the claude script, or as a bundled "claude.exe".
_DIRECT_NAMES = {"claude", "claude.exe"}
_NODE_NAMES = {"node", "node.exe"}


class ProcessScanner:
    """Enumerates running Claude Code processes using psutil.

    Uses the process working directory as the project path — Claude Code is
    always started from the project root, so cwd is the authoritative path.

    Two-pass enumeration for cost: the cheap first pass requests only
    ``["pid", "name", "create_time"]`` from every process on the system —
    these are O(1) reads from psutil's per-process snapshot. Only processes
    named ``node[.exe]`` need a follow-up ``cmdline()`` call to confirm
    they're hosting Claude Code; ``claude[.exe]`` is a direct name hit.
    Pulling cmdline eagerly across all processes (the previous behaviour)
    triggered a per-process NtQueryInformationProcess on Windows that
    measurably contributed to scan-tick CPU on busy machines (500+ procs).
    """

    def scan(self) -> list[Session]:
        sessions: list[Session] = []
        for proc in psutil.process_iter(["pid", "name", "create_time"]):
            try:
                info = proc.info
                name = (info.get("name") or "").lower()

                if name in _DIRECT_NAMES:
                    session = self._build(proc, info)
                    if session:
                        sessions.append(session)
                    continue

                if name not in _NODE_NAMES:
                    continue

                # Node process — only now pay the cmdline cost.
                try:
                    cmdline = proc.cmdline()
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    continue
                if not any("claude" in arg.lower() for arg in cmdline):
                    continue
                session = self._build(proc, info)
                if session:
                    sessions.append(session)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        return sessions

    @staticmethod
    def _build(proc: psutil.Process, info: dict) -> Session | None:
        try:
            project_path = Path(proc.cwd())
        except (psutil.AccessDenied, psutil.NoSuchProcess):
            project_path = Path("unknown")

        create_time = datetime.fromtimestamp(
            info.get("create_time") or 0.0, tz=timezone.utc
        )
        return Session(
            pid=info["pid"],
            project_path=project_path,
            session_uuid="",    # resolved later by JsonlParser activity events
            window_handle=None, # resolved by WindowActivator on demand
            last_activity=create_time,
        )
