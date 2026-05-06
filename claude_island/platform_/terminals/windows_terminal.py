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
        """Drop orphans, write the sentinel tab title for new sessions,
        stamp adapter identity, emit one singleton SessionGroup per view.

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

        Sentinel title (Plan O reconcile):
        On first sight of a session (cache miss), we read its console
        title; if it's not already our ``ci:{uuid}`` sentinel, we set
        it. This is what lets ``select_tab_by_title`` find the right
        tab on click — UIA mirrors console title to TabItem.Name, and
        a uuid-derived title is globally unique so name match is
        precise. Cache hits skip both probe and set: the session's
        title is assumed stable. claude only rewrites the title on
        topic-shift (rare); the click-time fallback in
        ``_activate_windows`` re-asserts the sentinel on demand.

        ``views`` are guaranteed to be claude sessions by upstream
        ``can_handle`` + the dispatcher chain — so SetConsoleTitleW
        only ever touches claude-attached consoles.

        Per-view work:
          1. AttachConsole probe (cached) to drop orphans (no console).
          2. On cache miss only: write sentinel title if drifted.
          3. Stamp adapter identity, emit one SessionGroup per view.

        Drag-tab correctness still works without group() doing anything:
        ``_activate_windows`` re-runs ``walk_to_visible_host`` on every
        focus click, so dragging a tab to another WT window is reflected
        on the next click without group() needing to recompute.
        """
        from dataclasses import replace
        from claude_island.platform_ import win32_console
        from claude_island.platform_.wt_session_title import (
            is_sentinel, sentinel_title,
        )

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
                conpty_hwnd, current_title = info
                if not conpty_hwnd:
                    continue
                # First sight of this session — establish the sentinel
                # title so click-time UIA Name match can find it. Skip
                # the set if we have no uuid (degraded view) or if the
                # title already matches our format (e.g. relaunched into
                # a tab whose previous incarnation we'd already labeled,
                # or this session was launched via Plan L which sets
                # the title at WT spawn time).
                #
                # Cache discipline: only memoise the conpty_hwnd AFTER
                # reconcile is done (set succeeded OR no set needed).
                # If set silently fails — typically a profile with
                # suppressApplicationTitle=true, but also transient WT
                # busy — we leave the cache empty so the next wake
                # re-probes the title and retries. AttachConsole is
                # ~3ms; retrying every wake on a silent-fail pid is
                # cheap insurance against permanently mislabeled tabs.
                expected = sentinel_title(v.session_uuid)
                set_ok = True
                if expected and not is_sentinel(current_title):
                    set_ok = win32_console.set_console_title(pid, expected)
                if set_ok:
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

        Passes ``expected_title = sentinel_title(view.session_uuid)`` to
        ``_activate_windows`` so the click-time path can re-assert our
        sentinel if claude or the user has clobbered it since group()
        last reconciled. ``siblings`` is preserved for backward
        compatibility but is always empty under singleton grouping —
        the inactive-split-pane fallback path no longer fires."""
        from claude_island.platform_.wt_session_title import sentinel_title
        expected = sentinel_title(view.session_uuid)
        return _activate_windows(
            view.session.pid,
            expected_title=expected,
            sibling_pids=list(siblings),
        )

    # ── LAUNCH ───────────────────────────────────────────────────────────

    @capability(Capability.LAUNCH)
    def launch(
        self,
        *,
        cwd: Path,
        command: tuple[str, ...],
        session_uuid: str | None = None,
    ) -> SpawnResult:
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

        Plan L (sentinel title at spawn time):
        When ``session_uuid`` is provided (Resume of a known dormant
        session), we add ``--title "ci:{uuid}" --suppressApplicationTitle``
        to lock the tab's title to our sentinel for life. WT's per-tab
        ``--suppressApplicationTitle`` flag tells WT to ignore any
        future OSC 0/2 / SetConsoleTitleW from the spawned process —
        so unlike Plan O (foreign sessions, reconciled lazily), Plan L
        tabs never need re-reconciling. Click-time UIA name match is
        always exact.

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
        # Build wt.exe argv. The Plan-L title-lock flags must come
        # *before* ``--`` (they configure the new-tab subcommand, not
        # the spawned process). When uuid is missing (defensive — every
        # Resume call should have one) we skip the flags and degrade
        # to the same Plan-O reconcile as a foreign session.
        from claude_island.platform_.wt_session_title import sentinel_title
        title = sentinel_title(session_uuid) if session_uuid else None

        argv: list[str] = ["wt.exe", "-d", str(cwd)]
        if title:
            argv += ["--title", title, "--suppressApplicationTitle"]
        argv += ["--", "cmd.exe", "/k", *command]
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

def _activate_windows(
    pid: int,
    *,
    expected_title: str | None = None,
    sibling_pids: list[int] | None = None,
) -> bool:
    """Resolve console window → (re-assert sentinel title if needed)
    → UIA tab select → SetForegroundWindow.

    ``expected_title``: the ``ci:{uuid}`` sentinel for this session.
    On click, we compare it against the current console title; if they
    differ (claude topic-shifted since group() last reconciled, or this
    is the very first click after the session appeared), we re-set the
    title via ``set_console_title`` and poll the UIA tree until WT
    mirrors the change into TabItem.Name (or 200ms times out — typical
    when the user's profile has ``suppressApplicationTitle: true``).

    Tab-select chain (3 fallbacks, increasingly imprecise):
      1. ``select_tab_by_title(expected)`` — exact match. Hits when the
         session is its tab's active pane (or sole pane).
      2. ``select_tab_by_title(current_title)`` if we didn't reconcile
         (no expected_title).
      3. ``select_any_ci_tab(hwnd)`` — pick any ``ci:*`` tab in this WT
         window. Hits when the click target is the *inactive pane* of a
         split tab — its TabItem.Name reflects the active sibling
         pane's sentinel, which is also a ``ci:*`` so it matches here.
         User then presses Alt+arrow to focus the right pane within
         the tab. See WT issue #5694 + WinUI3 lazy-load for why
         per-pane focus from outside is impossible.
      Last resort: plain ``_force_foreground`` so the user at least
      sees the WT window come up.

    ``sibling_pids``: kept for backward compatibility with the
    WindowActivator class path used by other adapters; WT's singleton
    grouping always passes an empty list, so the explicit
    sibling-walk is dead for WT clicks. Don't remove the parameter
    without also deleting the class method in ``window_activator.py``.

    Always returns whatever ``_force_foreground`` returns when we found
    a host hwnd — even if the tab select failed, raising the WT window
    is at least observable to the user (better than silently no-op).
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
        hwnd, current_title = resolved
        from claude_island.platform_ import win32_console, wt_uia

        # Click-time reconcile: claude may have rewritten the title via
        # OSC during a topic shift, or this session was just discovered
        # by the scanner and group()'s reconcile hasn't run yet. Re-set
        # then wait for WT's OSC pipeline to mirror it into TabItem.Name
        # before we issue the select.
        target_title = expected_title or current_title
        if expected_title and current_title != expected_title:
            win32_console.set_console_title(pid, expected_title)
            # Poll up to 200ms. If WT silently dropped our set
            # (suppressApplicationTitle profile), this returns False
            # and the select_tab_by_title below will also fail — we
            # still fall back to ci:* match and then plain foreground.
            wt_uia.wait_for_tab_name(hwnd, expected_title, timeout_ms=200)

        if not wt_uia.select_tab_by_title(hwnd, target_title):
            # Split-pane fallback: target is likely an inactive pane
            # whose sentinel doesn't appear in any TabItem.Name (WT
            # only mirrors the active pane's title). Settle for any
            # ci:* tab in this WT window — usually the sibling pane's
            # tab, which IS the right tab, just wrong pane.
            if not wt_uia.select_any_ci_tab(hwnd) and sibling_pids:
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
