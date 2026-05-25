from __future__ import annotations

import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import psutil

from claude_island.core.models import Session
from claude_island.platform_ import win32_console

# Claude Code is a Node.js CLI; on Windows it may appear as node.exe wrapping
# the claude script, or as a bundled "claude.exe".
_DIRECT_NAMES = {"claude", "claude.exe"}
_NODE_NAMES = {"node", "node.exe"}
# The macOS installer (~/.local/share/claude/versions/<version>) names
# the binary after its version string, so psutil reports
# name="2.1.126" instead of "claude". Recognise version-like names as
# candidates and confirm via cmdline. Pattern is intentionally loose
# (X.Y.Z optionally followed by -beta, .rc1, etc.) to survive future
# version-string changes; the cmdline check rejects false positives.
_VERSION_LIKE = re.compile(r"^\d+\.\d+\.\d+(?:[.-]\S+)?$")

# psutil status values that mean "the process exists but isn't actually
# running Claude". Filtered out so the UI never shows a row for them.
#   STOPPED — SIGSTOP'd (typically Ctrl+Z without bg/fg). The session's
#     state file ~/.claude/sessions/<pid>.json freezes at whatever value
#     was last written (often "busy"), so without this filter the row
#     appears "active now" forever even though no work is happening.
#   ZOMBIE / DEAD — already exited, just hasn't been reaped. Short-lived
#     but worth dropping to avoid a flash row that vanishes next tick.
_INACTIVE_STATUSES = frozenset({
    psutil.STATUS_STOPPED,
    psutil.STATUS_ZOMBIE,
    psutil.STATUS_DEAD,
})


class ProcessScanner:
    """Enumerates running Claude Code processes using psutil.

    Uses the process working directory as the project path — Claude Code is
    always started from the project root, so cwd is the authoritative path.

    Lazy attr access for cost: ``psutil.process_iter()`` is iterated
    without an ``attrs=`` argument so psutil does not pre-read name +
    create_time for every process on the system. Inside the loop we
    read ``proc.name()`` first; only processes whose name matches one
    of our candidate patterns (claude[.exe], node[.exe], or a version-
    like binary on macOS) trigger ``proc.create_time()`` /
    ``proc.cmdline()`` / ``proc.cwd()``. On a machine with 570 running
    processes (typical) this collapses 1140 OpenProcess+attr syscalls
    down to ~7 — measured cold-start drop from 2852 ms to 18 ms.
    Pulling cmdline eagerly across all processes (the previous behaviour)
    triggered a per-process NtQueryInformationProcess on Windows that
    measurably contributed to scan-tick CPU on busy machines (500+ procs).

    Liveness filters (three layers):

    1. Status filter (cross-platform, in ``_build``): drop psutil
       statuses STOPPED / ZOMBIE / DEAD. STOPPED catches the common
       macOS case where a user hits Ctrl+Z without bg/fg — the process
       still appears in ``process_iter`` but its state file freezes,
       leaving the UI claiming the session is "busy / active now"
       indefinitely. See ``_INACTIVE_STATUSES`` for rationale.

    2. Worker filter (cross-platform, in the scan loop): for the
       ``node`` candidate path only, drop processes whose direct
       parent is itself a claude process. claude spawns node children
       for MCP servers / subagent helpers; they inherit the parent's
       cwd and may carry "claude" in argv but aren't sessions the
       user can interact with. See ``_is_claude_worker_child``.

    3. Orphan filter (Windows-only, in ``_filter_orphans``): a "live"
       Claude session must still have a console attached. Probe each
       claude.exe with AttachConsole+GetConsoleWindow; if it fails
       (target has no console at all) the process is detached — its
       conPTY pipe was severed when its WT pane closed, leaving the
       binary as a no-tty zombie. Drop those.

    Why AttachConsole-success rather than UIA tab-title matching:
    matching against TabItem.Name false-positives split panes — only the
    *active* pane's title is exposed in TabItem.Name; inactive panes are
    indistinguishable from genuine orphans by name alone. AttachConsole
    treats every still-attached process as live regardless of pane
    visibility, which is what we want.

    Fail-open with a sanity tripwire: if every session would be filtered
    (system-wide AttachConsole brokenness, race with shutdown, etc.),
    return the originals unchanged — better stale than blank.
    """

    def scan(self) -> list[Session]:
        return _filter_orphans(self.scan_fast())

    def scan_fast(self) -> list[Session]:
        """Same psutil enumeration as :meth:`scan` but without the
        ``_filter_orphans`` pass. Returns sessions immediately.

        Used at startup so the UI populates sessions in well under
        100 ms cold. A follow-up ``scan()`` call shortly after runs the
        full orphan filter. Per-session WT window discovery (the
        wt_hwnd that drives same-tab grouping) lives in
        ``WindowsTerminalAdapter.group()``; process_scanner only
        enumerates and orphan-filters.

        Pass ``psutil.process_iter()`` without an ``attrs=`` argument so
        psutil does not pre-read name + create_time for every process
        on the system — that O(N_total) eager fetch was the dominant
        cold-start cost on Windows (~2.85 s for ~570 procs). With lazy
        access we only pay name() per process; create_time() / cmdline()
        / cwd() are read solely for the ~handful of name-candidates.
        """
        sessions: list[Session] = []
        for proc in psutil.process_iter():
            try:
                try:
                    name = (proc.name() or "").lower()
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    continue

                if name in _DIRECT_NAMES:
                    session = self._build(proc)
                    if session:
                        sessions.append(session)
                    continue

                # Confirmation-required path: either a node host that
                # might be running claude, or a versioned binary on
                # macOS. Both need a cmdline() call to distinguish from
                # unrelated processes.
                if name not in _NODE_NAMES and not _VERSION_LIKE.match(name):
                    continue

                try:
                    cmdline = proc.cmdline()
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    continue
                if not cmdline:
                    continue

                # Match if "claude" appears anywhere in cmdline. For
                # node hosts that's the script path; for the macOS
                # versioned binary it's argv[0] (basename "claude").
                argv0_base = os.path.basename(cmdline[0]).lower()
                if argv0_base in _DIRECT_NAMES:
                    pass  # versioned-binary confirmation
                elif name in _NODE_NAMES and any(
                    "claude" in arg.lower() for arg in cmdline
                ):
                    # Node hosts have a worker-vs-session ambiguity that
                    # the versioned-binary path doesn't: claude itself
                    # spawns node children for MCP servers / subagent
                    # helpers. They inherit cwd and may carry "claude"
                    # in argv (when bundled inside the claude install).
                    # Disambiguate via parent: a worker's parent IS a
                    # claude process; an interactive launch's parent is
                    # a shell / login / IDE host.
                    if _is_claude_worker_child(proc):
                        continue
                else:
                    continue

                session = self._build(proc)
                if session:
                    sessions.append(session)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue

        return sessions

    @staticmethod
    def _build(proc: psutil.Process) -> Session | None:
        """Build a ``Session`` from a confirmed-candidate ``proc``.

        Reads pid / status / cwd / create_time lazily from the live
        process. Returns None if the process disappeared mid-read or
        is in an inactive status (STOPPED / ZOMBIE / DEAD)."""
        try:
            if proc.status() in _INACTIVE_STATUSES:
                return None
            pid = proc.pid
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            return None

        try:
            project_path = Path(proc.cwd())
        except (psutil.AccessDenied, psutil.NoSuchProcess):
            project_path = Path("unknown")

        try:
            ct_epoch = proc.create_time()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            ct_epoch = 0.0
        create_time = datetime.fromtimestamp(ct_epoch or 0.0, tz=timezone.utc)
        return Session(
            pid=pid,
            project_path=project_path,
            session_uuid="",    # resolved later by JsonlParser activity events
            last_activity=create_time,
        )


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------

def _is_claude_worker_child(proc: psutil.Process) -> bool:
    """True if ``proc``'s direct parent looks like a Claude process,
    suggesting ``proc`` is a worker (MCP server, subagent helper)
    spawned by an interactive session rather than its own session.

    Why direct-parent only: the immediate parent is the precise signal
    — claude spawns workers as direct children. Walking deeper would
    risk dropping legitimate launches whose ancestry happens to
    include a claude process (e.g., one claude session opening a
    terminal that opens another claude).

    Cheap: one ``parent()`` + one ``name()`` call. Parent ``cmdline()``
    is read only when the parent name is ambiguous (``node`` /
    version-like) and we need to confirm it's actually running claude.
    """
    try:
        parent = proc.parent()
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return False
    if parent is None:
        return False
    try:
        pname = parent.name().lower()
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return False
    if pname in _DIRECT_NAMES:
        return True
    if pname in _NODE_NAMES or _VERSION_LIKE.match(pname):
        try:
            pcmd = parent.cmdline()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            return False
        if not pcmd:
            return False
        # Same disambiguation rule the scan loop uses for the candidate
        # itself: argv0 basename is "claude", or (node + "claude" in
        # any arg). If parent matches this, parent is a claude session
        # and proc is its child worker.
        argv0_base = os.path.basename(pcmd[0]).lower()
        if argv0_base in _DIRECT_NAMES:
            return True
        if pname in _NODE_NAMES and any("claude" in a.lower() for a in pcmd):
            return True
    return False


def _filter_orphans(sessions: list[Session]) -> list[Session]:
    """Drop orphan sessions whose console pipe was severed.

    A single pass: ``get_console_info(pid)`` — if AttachConsole fails
    the process has no console attached (its conPTY pipe was severed
    when its WT pane closed) and we drop it.

    wt_hwnd discovery for same-tab grouping lives in
    ``WindowsTerminalAdapter.group()`` along with the rest of WT
    integration; process_scanner stays pure psutil + AttachConsole.

    Sanity tripwire: if every session would be filtered (system-wide
    AttachConsole brokenness, scan-thread race with our own console
    state, etc.), return the originals unchanged. Better stale than
    blank — the user can still see and manually triage from the list.

    Non-Windows shortcut: AttachConsole is Windows-only and
    ``get_console_info`` returns None for every pid on macOS / Linux.
    Without this guard every session would be dropped, and only the
    "all-filtered" tripwire would save us from an empty list — which is
    fragile (one accidental non-None makes other sessions disappear).
    """
    if sys.platform != "win32":
        return sessions
    if not sessions:
        return sessions

    kept: list[Session] = []
    for s in sessions:
        info = win32_console.get_console_info(s.pid)
        if info is None:
            # AttachConsole failed → no console attached → orphan.
            continue
        kept.append(s)

    if not kept:
        return sessions  # tripwire: don't wipe everything silently
    return kept
