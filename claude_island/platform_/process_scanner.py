from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import psutil

from claude_island.core.models import Session
from claude_island.platform_ import win32_console, wt_uia

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

    Orphan filter: a "live" Claude session must be currently rendered as
    a visible Windows Terminal tab. We collect every WT TabItem.Name once
    per scan via UIA, then for each candidate claude.exe we pull its
    console title via AttachConsole+GetConsoleTitleW; if the title isn't
    in the live-titles set, the process exists but has no visible host
    pane — it's an orphan, dropped from the result.

    Why title-set rather than process-tree liveness: the parent
    powershell.exe of an orphan claude.exe is often still alive (only
    its conPTY pipe to WT was closed). Process-tree judges miss that
    case entirely. UIA tab titles are the ground truth for "what WT is
    actually rendering right now".

    Fail-open at three levels: (a) UIA unavailable / no WT → keep all;
    (b) per-pid console read fails → keep that session; (c) sanity check
    — if every session would be filtered (e.g. user manually renamed all
    WT tabs so no titles match), return everything unchanged.
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

        return _filter_orphans(sessions)

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


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------

def _filter_orphans(sessions: list[Session]) -> list[Session]:
    """Drop sessions whose console title is not currently rendered as a
    visible WT tab.

    Three fail-open exits, in order:
    1. UIA returned ``None`` (no WT, library missing, enumeration fail) →
       skip filter, return all. We can't tell.
    2. UIA returned an empty set → same; treat as "unknown" not "no tabs".
    3. After filtering, every session was dropped while we had >0 raw
       sessions → the judge is probably wrong (user renamed every tab,
       or our class-name filter missed a WT variant). Return originals.
    """
    if not sessions:
        return sessions

    live_titles = wt_uia.collect_wt_tab_titles()
    if not live_titles:  # None or empty → fail-open
        return sessions

    kept: list[Session] = []
    for s in sessions:
        info = win32_console.get_console_info(s.pid)
        if info is None:
            # Per-pid fail-open: couldn't read this title, don't filter it.
            kept.append(s)
            continue
        _, title = info
        if title and title in live_titles:
            kept.append(s)
        # else: orphan — the title doesn't match any visible WT tab, drop.

    if not kept:
        # Sanity tripwire: rather than wipe the list silently, return the
        # raw sessions and let the user see them. Better stale than blank.
        return sessions
    return kept
