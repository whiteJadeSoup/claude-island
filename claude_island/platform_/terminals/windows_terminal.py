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

        # Sibling-pane cache for split-tab disambiguation. Updated on
        # every group() wake (passive, worker-thread); also refreshed
        # fire-and-forget at click time when the cache turns out
        # stale (active, background thread). See wt_pane_siblings.py
        # for the full design rationale.
        from claude_island.platform_.wt_pane_siblings import PaneSiblingTracker
        self._sibling_tracker = PaneSiblingTracker()

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
        from claude_island.platform_ import win32_console, window_activator
        from claude_island.platform_.wt_session_title import (
            is_sentinel, sentinel_title,
        )

        # win32gui needed for walk_to_visible_host (sibling-cache update
        # path). None on import error → we skip the sibling refresh but
        # don't crash the rest of group().
        win32gui_mod = None
        try:
            import win32gui as _w32g
            win32gui_mod = _w32g
        except ImportError:
            pass

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

        # Sibling-pane cache refresh: enumerate each WT window's
        # currently active tab and record which sentinels share it.
        # Click-time fallback uses these to land on the right tab when
        # the clicked session is an inactive pane (its sentinel isn't
        # in TabItem.Name; one of its siblings IS — see
        # wt_pane_siblings.py for the rationale). We dedup wt_hwnd
        # because all panes within one window resolve to the same
        # hwnd; one walk per window is enough.
        if win32gui_mod is not None:
            from claude_island.platform_.wt_pane_siblings import _dbg as _focus_dbg
            wt_hwnds: set[int] = set()
            for v in kept:
                conpty = self._conpty_cache.get(v.session.pid)
                if not conpty:
                    continue
                wt_hwnd = window_activator.walk_to_visible_host(
                    conpty, win32gui_mod,
                )
                if wt_hwnd:
                    wt_hwnds.add(wt_hwnd)
            _focus_dbg(
                f"group() wt_hwnds={[hex(h) for h in wt_hwnds]} "
                f"(kept={len(kept)} sessions)"
            )
            for wt_hwnd in wt_hwnds:
                self._sibling_tracker.update_from_active_tab(wt_hwnd)

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
        last reconciled.

        Also injects the cached sibling sentinels (sessions known to
        share *this* session's tab) — used as the second fallback when
        the click target is an inactive split-pane whose own sentinel
        doesn't appear in any TabItem.Name. The tracker is forwarded
        so ``_activate_windows`` can fire-and-forget a refresh when
        every fallback misses.

        ``siblings`` (pid list) is preserved for backward compatibility
        with the dispatcher signature; under singleton grouping it is
        always empty — the proper sibling info now flows through the
        sentinel cache."""
        from claude_island.platform_.wt_session_title import sentinel_title
        expected = sentinel_title(view.session_uuid)
        sib_sentinels: tuple[str, ...] = ()
        if expected:
            # Snapshot the cache at click time. siblings_of returns a
            # fresh copy, safe to iterate without holding the tracker
            # lock during UIA select calls.
            sib_sentinels = tuple(self._sibling_tracker.siblings_of(expected))
        return _activate_windows(
            view.session.pid,
            expected_title=expected,
            sibling_sentinels=sib_sentinels,
            sibling_pids=list(siblings),
            sibling_tracker=self._sibling_tracker,
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
    sibling_sentinels: tuple[str, ...] | list[str] = (),
    sibling_tracker: object | None = None,
    sibling_pids: list[int] | None = None,
) -> bool:
    """Resolve console window → (re-assert sentinel title if needed)
    → UIA tab select (with sibling-cache fallback) → SetForegroundWindow.

    ``expected_title``: the ``ci:{uuid}`` sentinel for this session.
    On click, we compare it against the current console title; if they
    differ (claude topic-shifted since group() last reconciled, or this
    is the very first click after the session appeared), we re-set the
    title via ``set_console_title`` and poll the UIA tree until WT
    mirrors the change into TabItem.Name (or 200ms times out — typical
    when the user's profile has ``suppressApplicationTitle: true``).

    ``sibling_sentinels``: pre-computed snapshot of the
    PaneSiblingTracker's cached siblings of expected_title. Used as
    the second fallback when select(expected) misses — the click target
    is likely an inactive pane in a split tab, and its sibling's
    sentinel IS in TabItem.Name (the sibling is the active pane).

    ``sibling_tracker``: PaneSiblingTracker instance. When all selects
    miss, we ``schedule_update`` (fire-and-forget on a background
    thread) so the cache is fresh for the next click. We do NOT block
    this click waiting for the refresh — Qt event loop must stay
    responsive.

    Tab-select chain (no further fallbacks beyond ``_force_foreground``):
      1. ``select_tab_by_title(expected)`` — exact match. Hits the
         active-pane / single-pane case.
      2. ``select_tab_by_title(sib)`` for each sibling sentinel — hits
         the inactive-pane-in-split-tab case via its active sibling.
      3. ``schedule_update(hwnd)`` fire-and-forget — repairs cache for
         the next click; this click does not retry (no Qt blocking).
      4. ``_force_foreground(hwnd)`` — at minimum brings WT to the
         foreground so the user gets visual feedback their click was
         received.

    ``sibling_pids``: kept for backward compatibility with the
    WindowActivator class path used by other adapters; WT's singleton
    grouping always passes an empty list. Don't remove the parameter
    without also deleting the class method in ``window_activator.py``.

    Returns whatever ``_force_foreground`` returns when we found a
    host hwnd — even if the tab select failed, raising the WT window
    is observable to the user.
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

    from claude_island.platform_.wt_pane_siblings import _dbg

    _dbg(
        f"_activate_windows(pid={pid}, expected={expected_title!r}, "
        f"siblings={list(sibling_sentinels)!r})"
    )
    resolved = _resolve_console_window(pid, win32gui)
    hwnd: int | None = None
    if resolved is not None:
        hwnd, current_title = resolved
        _dbg(f"  resolved → wt_hwnd={hex(hwnd)}, current_title={current_title!r}")
        from claude_island.platform_ import win32_console, wt_uia

        # Click-time reconcile: claude may have rewritten the title via
        # OSC during a topic shift, or this session was just discovered
        # by the scanner and group()'s reconcile hasn't run yet. Re-set
        # then wait for WT's OSC pipeline to mirror it into TabItem.Name
        # before we issue the select.
        target_title = expected_title or current_title
        if expected_title and current_title != expected_title:
            ok = win32_console.set_console_title(pid, expected_title)
            _dbg(f"  set_console_title → {ok}")
            # Poll up to 200ms. If WT silently dropped our set
            # (suppressApplicationTitle profile), this returns False
            # and the select_tab_by_title below will also fail — we
            # still fall back to siblings and then plain foreground.
            wait_ok = wt_uia.wait_for_tab_name(hwnd, expected_title, timeout_ms=200)
            _dbg(f"  wait_for_tab_name → {wait_ok}")

        # Step 1: try our own sentinel.
        hit = wt_uia.select_tab_by_title(hwnd, target_title)
        _dbg(f"  step1 select_tab_by_title({target_title!r}) → {hit}")

        # Step 2: try each cached sibling sentinel — covers
        # inactive-pane-in-split-tab.
        if not hit:
            for sib in sibling_sentinels:
                if sib and sib != target_title:
                    sib_hit = wt_uia.select_tab_by_title(hwnd, sib)
                    _dbg(f"  step2 select_tab_by_title({sib!r}) → {sib_hit}")
                    if sib_hit:
                        hit = True
                        break

        # Step 3: cache miss / stale. Fire async refresh for next
        # click; don't block this one. Tracker has duck-typed
        # ``schedule_update`` (object-typed parameter so this module
        # doesn't need to import the concrete class).
        if not hit and sibling_tracker is not None:
            _dbg("  step3 fire schedule_update (cache miss / stale)")
            try:
                sibling_tracker.schedule_update(hwnd)
            except Exception:
                pass  # tracker is best-effort
    else:
        from claude_island.platform_.window_activator import _ancestor_pids, _find_window_for_pids
        candidate_pids = _ancestor_pids(pid)
        if candidate_pids:
            hwnd = _find_window_for_pids(candidate_pids, win32gui, win32process)

    if hwnd is None:
        return False
    # Step 4: foreground regardless — visual feedback that click was
    # received, even if tab select failed.
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
