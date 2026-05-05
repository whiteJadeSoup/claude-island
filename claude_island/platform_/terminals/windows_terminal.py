"""Windows Terminal adapter — AttachConsole orphan filter + per-tab focus.

Handles claude.exe / node.exe (claude) sessions launched inside
Windows Terminal. Emits one singleton SessionGroup per session — no
multi-view grouping. WinUI3 lazy-loads inactive tab subtrees, so a
conpty in an inactive tab has no TermControl in the UIA tree we
could match against; the previous (wt_hwnd, cwd) approximation
silently merged unrelated tabs that happened to share a project
directory. See ``group`` docstring for the full rationale.

FOCUS capability: SetForegroundWindow on the WT window + UIA
Select on the tab whose TabItem.Name matches the session's console
title (set by claude itself, e.g. "claude-island-dev").

Helpers it leans on:
- ``platform_/win32_console.py`` for AttachConsole-driven title reads
- ``platform_/wt_uia.py`` for UIA tab discovery + selection
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import ClassVar

from claude_island.core.capabilities import (
    Capability,
    FocusGranularity,
    LauncherSpawnError,
    SpawnResult,
    _CapabilityProvider,
    capability,
)
from claude_island.core.models import Session
from claude_island.core.snapshot import SessionGroup, SessionView
from claude_island.platform_.terminals import adapter
from claude_island.platform_.terminals.protocols import TerminalAdapter

# Internal Win32 helpers — import within methods so non-Windows import
# of this module (via __init__.py adapter registry) doesn't trigger
# ImportError. The @adapter decorator skips instantiation on non-win
# platforms anyway.

_WT_CLASS_PREFIX = "CASCADIA_HOSTING_WINDOW_CLASS"
_MAX_ANCESTOR_DEPTH = 10


@adapter("windows-terminal", priority=100, platform="win")
class WindowsTerminalAdapter(_CapabilityProvider):
    """Adapter for claude sessions running inside Windows Terminal."""

    name: ClassVar[str] = ""  # set by @adapter
    _priority: int = 0

    def __init__(self) -> None:
        super().__init__()
        # pid → conpty_hwnd. Used by group() as the orphan-filter probe
        # cache: a non-zero conpty_hwnd means AttachConsole succeeded
        # last tick, so the pid still has a live console — skip the
        # ~3 ms AttachConsole syscall (which holds a process-global lock)
        # on every wake after the first. The conPTY hwnd is allocated
        # by the OS at process start and freed when the pid dies, so
        # it cannot change for the lifetime of pid.
        #
        # Negative results (orphan / race) are NOT cached — a brief
        # race between process_scanner accepting a pid and our group()
        # call finding its conPTY would otherwise permanently hide the
        # session until process_scanner re-emits. Re-probing each tick
        # costs one extra AttachConsole per orphan, and orphans are
        # already filtered upstream in process_scanner, so this is
        # rarely exercised.
        #
        # Single-threaded access: group() only runs on the snapshotter's
        # worker thread (reactivex EventLoopScheduler), so no lock.
        self._conpty_cache: dict[int, int] = {}

    # ── can_handle ──────────────────────────────────────────────────────

    def can_handle(self, session: Session) -> bool:
        """True when the session's process ancestry includes
        WindowsTerminal.exe or a conpty host that traces there.

        Walks ancestors via psutil. On Windows every claude session is
        inside *some* terminal (WT, conhost, or a bundled app); we
        only claim WT-hosted sessions, generic_windows claims the rest."""
        import psutil
        try:
            proc = psutil.Process(session.pid)
            ancestors: list[psutil.Process] = []
            for _ in range(_MAX_ANCESTOR_DEPTH):
                p = proc.parent()
                if p is None: break
                ancestors.append(p)
                proc = p
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            return False
        return any(
            a.name().lower() in {"windowsterminal.exe", "wt.exe"}
            for a in ancestors
        )

    # ── group ───────────────────────────────────────────────────────────

    def group(self, views: list[SessionView]) -> list[SessionGroup]:
        """Drop orphans, stamp adapter identity, emit one singleton
        SessionGroup per view.

        Why one-per-view (not tab-level grouping):
        WT's UIA tree only fully populates the *currently active* tab —
        WinUI3 lazy-loads inactive tab subtrees, so a conpty in an
        inactive tab has no TermControl in the UIA tree we could match
        against. The previous (wt_hwnd, normalized_cwd) approximation
        merged any two tabs in the same WT window that shared a cwd
        (the common case: two ``claude`` sessions launched from the
        same project root) into one card; clicks on the inactive one
        silently routed to the active sibling via the title fallback.
        See WT issues #5694, #1351 + WinUI3 issue #8719 for why a
        proper conpty→TabItem mapping is not feasible here.

        Per-view work:
          1. AttachConsole probe (cached) to drop orphans (no console).
          2. Stamp adapter identity, emit one SessionGroup per view.

        Drag-tab correctness still works without group() doing anything:
        ``_activate_windows`` re-runs ``walk_to_visible_host`` on every
        focus click, so dragging a tab to another WT window is reflected
        on the next click without group() needing to recompute.
        """
        from dataclasses import replace
        from claude_island.platform_ import win32_console

        # GC: drop cache entries for pids that left views (process
        # exited or was reassigned to a different adapter). Done before
        # the loop so the cache stays bounded by the live session count.
        alive_pids = {v.session.pid for v in views}
        if self._conpty_cache:
            self._conpty_cache = {
                p: h for p, h in self._conpty_cache.items() if p in alive_pids
            }

        kept: list[SessionView] = []
        for v in views:
            pid = v.session.pid
            conpty_hwnd = self._conpty_cache.get(pid)
            if conpty_hwnd is None:
                info = win32_console.get_console_info(pid)
                if info is None:
                    continue
                conpty_hwnd = info[0]
                if not conpty_hwnd:
                    continue
                self._conpty_cache[pid] = conpty_hwnd
            kept.append(v)

        # Tripwire: every view filtered → likely a race with our own
        # console state. Keep originals so the user still sees rows
        # (rather than a blank list).
        if not kept:
            kept = list(views)

        result: list[SessionGroup] = []
        for v in kept:
            stamped = replace(
                v,
                adapter_id=self.name,
                focus_granularity=FocusGranularity.TAB,
                capabilities=type(self).capabilities,
            )
            result.append(SessionGroup(
                group_id=f"wt:{stamped.pid}",
                title_hint=None,
                adapter_id=self.name,
                views=(stamped,),
            ))
        return result

    # ── FOCUS ────────────────────────────────────────────────────────────

    @capability(Capability.FOCUS)
    def focus(self, view: SessionView, *, siblings: list[int] = ()) -> bool:
        """Bring the WT window to foreground + select the matching tab.

        ``siblings`` is the list of pids of other sessions in the same
        SessionGroup (i.e. other panes in the same WT window+project).
        Used as a fallback when the clicked row is an inactive split
        pane: WT's UIA TabItem.Name only exposes the active pane's
        title, so a click on an inactive pane has no matching tab title
        — we then try each sibling's title in turn, and one of them
        IS the active pane (whose title DOES match a tab). Without
        this fallback, clicking an inactive pane only foregrounds the
        WT window without switching tabs (the regression originally
        fixed in commit 7daa451)."""
        return _activate_windows(view.session.pid, list(siblings))

    # ── LAUNCH ───────────────────────────────────────────────────────────

    @capability(Capability.LAUNCH)
    def launch(self, *, cwd: Path, command: tuple[str, ...]) -> SpawnResult:
        """Spawn a new wt.exe window in ``cwd`` running ``command``.

        Used by RecentsDrawer's Resume click handler — the dormant
        session has no live SessionView (that's the whole point), so
        unlike FOCUS this method takes raw cwd + command rather than
        a view.

        wt.exe argv: ``-d <cwd>`` sets initial directory, ``--`` ends
        wt's own option parsing, then the command runs in the new tab.
        Using DETACHED_PROCESS so the new window is independent of
        claude-island — closing the island doesn't kill the resumed
        claude session, and vice versa.

        ``cmd.exe /k`` wrapper — *required*, not a convenience: WT
        spawns the new tab's process via ``CreateProcessW``, which
        does NOT walk PATHEXT to find ``.cmd`` / ``.bat`` extensions.
        npm-installed ``claude`` is actually ``claude.cmd`` on disk;
        without the wrapper WT raises ``ERROR_FILE_NOT_FOUND``
        (0x80070002) the moment the user clicks Resume. cmd.exe DOES
        walk PATHEXT, so the lookup succeeds. ``/k`` keeps the window
        alive after claude exits so the user can read any error
        message — ``/c`` would close it instantly on failure and
        hide all diagnostic output.

        Raises LauncherSpawnError if wt.exe isn't installed (Windows 10
        without the Store-shipped Terminal app) or if the spawn itself
        fails. Caller (RecentsDrawer) catches and toasts."""
        if shutil.which("wt.exe") is None:
            raise LauncherSpawnError(
                "Windows Terminal (wt.exe) not found. Install from the "
                "Microsoft Store: https://aka.ms/terminal"
            )
        argv = ["wt.exe", "-d", str(cwd), "--", "cmd.exe", "/k", *command]
        try:
            proc = subprocess.Popen(
                argv,
                creationflags=subprocess.DETACHED_PROCESS,
                close_fds=True,
            )
        except (OSError, FileNotFoundError) as e:
            raise LauncherSpawnError(f"wt.exe spawn failed: {e}") from e
        return SpawnResult(
            terminal_name=self.name,
            terminal_pid=proc.pid,
            started_at=datetime.now(timezone.utc),
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _activate_windows(pid: int, sibling_pids: list[int] | None = None) -> bool:
    """Resolve console window → UIA tab select (with sibling fallback)
    → SetForegroundWindow.

    Sibling fallback handles the inactive split-pane case: when the
    clicked row is an inactive pane, its own console title doesn't
    appear in any TabItem.Name — WT only exposes the active pane's
    title in the tab strip. Walking sibling pids tries each sibling's
    console title; one of them IS the active pane in the same tab
    and its title DOES match.
    """
    try:
        import win32con
        import win32gui
        import win32process
    except ImportError:
        print(
            "[claude-island] pywin32 not installed; cannot activate windows. "
            "Run: pip install pywin32",
            file=sys.stderr,
        )
        return False

    resolved = _resolve_console_window(pid, win32gui)
    hwnd: int | None = None
    if resolved is not None:
        hwnd, title = resolved
        from claude_island.platform_ import wt_uia
        if not wt_uia.select_tab_by_title(hwnd, title) and sibling_pids:
            # Fallback for inactive split-pane clicks — try each sibling's
            # console title; one of them is the active pane and its title
            # matches a tab. Reuses the AttachConsole helper from
            # window_activator so the dance lives in one place.
            from claude_island.platform_.window_activator import _select_tab_via_siblings
            _select_tab_via_siblings(hwnd, sibling_pids)
    else:
        from claude_island.platform_.window_activator import _ancestor_pids, _find_window_for_pids
        candidate_pids = _ancestor_pids(pid)
        if candidate_pids:
            hwnd = _find_window_for_pids(candidate_pids, win32gui, win32process)

    if hwnd is None:
        return False
    from claude_island.platform_.window_activator import _force_foreground
    return _force_foreground(hwnd, win32con, win32gui, win32process)


def _resolve_console_window(pid: int, win32gui) -> tuple[int, str] | None:
    from claude_island.platform_ import win32_console
    from claude_island.platform_.window_activator import walk_to_visible_host
    info = win32_console.get_console_info(pid)
    if info is None:
        return None
    console_hwnd, console_title = info
    if not console_hwnd:
        return None
    host = walk_to_visible_host(console_hwnd, win32gui)
    if host is None:
        return None
    return (host, console_title)
