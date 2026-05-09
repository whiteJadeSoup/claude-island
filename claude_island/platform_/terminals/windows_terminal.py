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

# Internal Win32 helpers — import within methods so non-Windows import
# of this module (via __init__.py adapter registry) doesn't trigger
# ImportError. The @adapter decorator skips instantiation on non-win
# platforms anyway.

_MAX_ANCESTOR_DEPTH = 10

# WT main window class. Same on stable / preview / dev. Used as the
# coarse cache-invalidation signal for ``_wt_hwnd_cache`` — see
# ``_wt_window_signature`` below.
_WT_CLASS_PREFIX = "CASCADIA_HOSTING_WINDOW_CLASS"


def _wt_window_signature(win32gui_mod) -> int:
    """Hash the current set of WT top-level HWNDs.

    Cheap (~1 ms): one EnumWindows pass + a GetClassName per top-level
    window. Hash CHANGES when a WT window is created or destroyed —
    the only invalidation event that affects per-pid wt_hwnd
    correctness across wakes. (Tab-drag within an unchanged window
    set is the known blind spot — see ``_wt_hwnd_cache`` doc on
    ``WindowsTerminalAdapter``.)

    Returns 0 on any failure; callers treat 0 as "always invalidate"
    by comparing against the previous stored signature (also 0 when
    win32gui is missing or first-call), so caching simply degrades to
    no-op without bugging out.
    """
    hwnds: list[int] = []

    def _cb(hwnd: int, _arg: object) -> bool:
        try:
            cls = win32gui_mod.GetClassName(hwnd)
            if cls.startswith(_WT_CLASS_PREFIX):
                hwnds.append(hwnd)
        except Exception:
            pass
        return True

    try:
        win32gui_mod.EnumWindows(_cb, None)
    except Exception:
        return 0
    return hash(frozenset(hwnds))


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
        # it cannot change for the lifetime of pid → cached
        # unconditionally on first sight.
        #
        # Negative results (orphan / race — ``info is None``) are NOT
        # cached: process_scanner may accept a pid before its conPTY
        # is ready, and we want the next wake to re-probe rather than
        # permanently hide the session.
        #
        # Single-threaded access: group() only runs on the snapshotter's
        # worker thread (reactivex EventLoopScheduler), so no lock.
        self._conpty_cache: dict[int, int] = {}

        # pids that have already received a SetConsoleTitleW attempt
        # (success or silent-fail). Tracked SEPARATELY from
        # _conpty_cache so that a profile with
        # ``suppressApplicationTitle: true`` doesn't trap us in an
        # infinite re-probe loop: SetConsoleTitleW returns True even
        # when WT discards the OSC update, and even returning False
        # only means "syscall failed", not "we should retry forever".
        # One attempt per pid is enough — if WT really wanted the
        # title it would have updated TabItem.Name; if not, the
        # sentinel-presence detection in the bucketing phase is
        # fail-safe (an absent sentinel keeps the bucket grouped,
        # never permanently broken). GC'd alongside _conpty_cache.
        self._title_set_attempted: set[int] = set()

        # pid → wt_hwnd. Avoid running ``walk_to_visible_host`` (10
        # win32gui calls per pid) on every wake. Invalidated wholesale
        # whenever the WT-window-set signature changes (cheap
        # EnumWindows + class filter, ~1 ms per wake) — that captures
        # process restart and new-window cases. Tab-drag WITHIN the
        # existing WT window set is the known blind spot; it self-heals
        # on the next signature change and the click-time path
        # (``_resolve_console_window``) re-walks fresh anyway, so a
        # stale group attribution doesn't break click correctness.
        self._wt_hwnd_cache: dict[int, int] = {}
        self._wt_window_signature: int = 0

        # pid → SessionView. Populated in group() at every wake; read
        # in focus() to compute cwd-matched sibling sentinels for the
        # inactive-pane fallback. focus() runs on Qt main thread; the
        # write in group() runs on the snapshotter worker — but
        # dict assignment of full snapshot (`self._view_cache = {...}`)
        # is atomic at the GIL level (single bytecode op), so no lock
        # needed for this read/write pattern.
        self._view_cache: dict[int, SessionView] = {}

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
        """Drop orphans, reconcile sentinel title for new sessions,
        bucket views by wt_hwnd, emit one SessionGroup per WT window.

        Grouping by wt_hwnd (not singleton, not by-cwd):
        Empirical UIA dump (scripts/dump_wt_uia.py) showed that WT's
        UIA tree only exposes the *active pane* of the *active tab* —
        every other pane (inactive panes within the active tab AND
        anything in inactive tabs) is physically absent (WT calls
        ``_tabContent.Children().Clear()`` on every tab switch in
        TabManagement.cpp). So per-pane identification of inactive
        panes is impossible from outside. We trade pane-precision for
        UI grouping: same WT window → one card → click any session
        in it does best-effort tab select (works when the target is
        a tab's active pane / single-pane tab; falls back to plain
        foreground for inactive panes in a split tab). User then uses
        WT's own Alt+arrow to focus the right pane.

        Sentinel title (Plan O reconcile):
        On first sight of a session (cache miss), we read its console
        title; if it's not already our ``ci:{uuid}`` sentinel, we set
        it via SetConsoleTitleW. WT mirrors that into TabItem.Name
        (unless the tab was launched with ``--suppressApplicationTitle``
        — Plan L — in which case the title was set at spawn). Either
        way, ``select_tab_by_title`` can find the target tab by its
        ``ci:{uuid32}`` Name. Cache hits skip the syscall.

        ``views`` are guaranteed to be claude sessions by upstream
        ``can_handle`` + the dispatcher chain — so SetConsoleTitleW
        only ever touches claude-attached consoles.

        Per-view work:
          1. AttachConsole probe (cached) to drop orphans (no console).
          2. On cache miss only: write sentinel title if drifted.
          3. Resolve wt_hwnd via walk_to_visible_host — used as the
             group bucket key.
          4. Bucket views by wt_hwnd. Views whose wt_hwnd doesn't
             resolve become singleton groups (one card each).

        Drag-tab correctness: a session moved to another WT window
        will be re-bucketed on the next group() call, since
        walk_to_visible_host re-resolves on every wake.
        """
        from dataclasses import replace
        from claude_island.platform_ import win32_console, window_activator
        from claude_island.platform_.wt_session_title import sentinel_title

        # win32gui needed for walk_to_visible_host. None on ImportError
        # → all views fall through to singleton groups (we lose the
        # window-grouping but rows still render).
        win32gui_mod = None
        try:
            import win32gui as _w32g
            win32gui_mod = _w32g
        except ImportError:
            pass

        # GC: drop cache entries for pids that left views (process
        # exited or was reassigned to a different adapter). Done before
        # the loop so the cache stays bounded by the live session count.
        # All three caches GC'd together — a returning pid (recycled
        # by the OS, or re-discovered after a transient orphan window)
        # gets a fresh title-set attempt and a fresh wt_hwnd walk.
        alive_pids = {v.session.pid for v in views}
        if self._conpty_cache:
            self._conpty_cache = {
                p: h for p, h in self._conpty_cache.items() if p in alive_pids
            }
        if self._title_set_attempted:
            self._title_set_attempted &= alive_pids
        if self._wt_hwnd_cache:
            self._wt_hwnd_cache = {
                p: h for p, h in self._wt_hwnd_cache.items() if p in alive_pids
            }

        # Invalidate the wt_hwnd cache on WT-window-set change. Cheap
        # win32 sweep — drops the per-pid GW_OWNER walks below to a
        # cache hit in steady state.
        if win32gui_mod is not None:
            sig = _wt_window_signature(win32gui_mod)
            if sig != self._wt_window_signature:
                self._wt_hwnd_cache = {}
                self._wt_window_signature = sig

        kept: list[SessionView] = []
        for v in views:
            pid = v.session.pid
            conpty_hwnd = self._conpty_cache.get(pid)
            if conpty_hwnd is None:
                info = win32_console.get_console_info(pid)
                if info is None:
                    continue  # orphan / no console — skip; next wake re-probes
                conpty_hwnd, current_title = info
                if not conpty_hwnd:
                    continue
                # conPTY survives for the pid lifetime — cache
                # unconditionally, even if the title-set below fails.
                # Caching here protects suppressApplicationTitle profiles
                # from a permanent re-probe loop (5 Hz × 5 sessions =
                # 25 AttachConsole/sec under the global console lock).
                self._conpty_cache[pid] = conpty_hwnd

                # Title set is best-effort and tried at most once per pid.
                # We compare against the EXACT expected sentinel — using
                # ``is_sentinel`` (prefix match) was a bug: an inherited
                # ``ci:OLD_UUID`` (pid recycled, multiple island instances)
                # would falsely look "already labeled" and the wrong
                # sentinel would persist on the tab.
                #
                # If WT silently drops the OSC update
                # (suppressApplicationTitle profile), the sentinel never
                # reaches TabItem.Name. The bucketing phase's
                # sentinel-presence detection is fail-safe under that
                # condition: an absent sentinel keeps the bucket grouped
                # rather than demoting to singletons. Slightly imprecise
                # grouping is preferable to the previous infinite retry.
                expected = sentinel_title(v.session_uuid)
                if (
                    expected
                    and current_title != expected
                    and pid not in self._title_set_attempted
                ):
                    self._title_set_attempted.add(pid)
                    win32_console.set_console_title(pid, expected)
            kept.append(v)

        # Tripwire: every view filtered → likely a race with our own
        # console state. Keep originals so the user still sees rows
        # (rather than a blank list).
        if not kept:
            kept = list(views)

        # Bucket by (wt_hwnd, normalized_cwd). Same WT window AND same
        # project → likely split panes of one tab → group. Different
        # cwd → certainly different tabs (panes of one tab share cwd
        # by construction; they're started from `claude` in the same
        # working dir). Worktree paths are normalised back to their
        # parent repo (a subdirectory of the project), since
        # claude-code split-pane between main repo + worktree is a
        # common workflow.
        from claude_island.core.snapshot import _normalize_project_path
        from claude_island.platform_ import wt_uia
        buckets: dict[tuple, list[SessionView]] = {}
        singletons: list[SessionView] = []
        for v in kept:
            pid = v.session.pid
            wt_hwnd: int | None = self._wt_hwnd_cache.get(pid)
            if wt_hwnd is None and win32gui_mod is not None:
                conpty = self._conpty_cache.get(pid)
                if conpty:
                    wt_hwnd = window_activator.walk_to_visible_host(
                        conpty, win32gui_mod,
                    )
                    if wt_hwnd:
                        self._wt_hwnd_cache[pid] = wt_hwnd
            if wt_hwnd:
                key = (wt_hwnd, _normalize_project_path(v.project_path))
                buckets.setdefault(key, []).append(v)
            else:
                singletons.append(v)

        # Sentinel-presence detection: for each multi-view bucket, check
        # whether ALL views' sentinels appear in the wt_hwnd's
        # TabItem.Names. If yes → these are separate tabs that happen
        # to share cwd (each is its own active pane) → split into
        # singletons. If at least one is missing → that view is an
        # inactive pane → keep grouped (it's truly co-pane with the
        # active sibling whose sentinel IS in TabItem.Name).
        # Single UIA call per wt_hwnd (cached across buckets).
        tab_names_by_hwnd: dict[int, set[str]] = {}
        for (wt_hwnd, _cwd), batch in list(buckets.items()):
            if len(batch) <= 1:
                continue  # singleton bucket: nothing to detect
            if wt_hwnd not in tab_names_by_hwnd:
                tab_names_by_hwnd[wt_hwnd] = wt_uia.list_ci_tab_names(wt_hwnd)
            tab_names = tab_names_by_hwnd[wt_hwnd]
            sentinels = {sentinel_title(v.session_uuid) for v in batch}
            sentinels.discard(None)
            if sentinels and sentinels.issubset(tab_names):
                # All sessions are own-tab active panes — separate
                # tabs that happen to share cwd. Demote to singletons.
                key = (wt_hwnd, _cwd)
                del buckets[key]
                singletons.extend(batch)

        result: list[SessionGroup] = []
        for (wt_hwnd, cwd_norm), batch in buckets.items():
            stamped = tuple(
                replace(
                    v,
                    adapter_id=self.name,
                    focus_granularity=FocusGranularity.TAB,
                    capabilities=type(self).capabilities,
                )
                for v in batch
            )
            # group_id includes a hash-stable suffix based on cwd so
            # the UI's per-group_id card cache survives wakes. Use
            # the normalised cwd so worktree → parent renames don't
            # generate a new group_id mid-session.
            result.append(SessionGroup(
                group_id=f"wt:{wt_hwnd}:{cwd_norm}",
                title_hint=None,
                adapter_id=self.name,
                views=stamped,
            ))
        for v in singletons:
            stamped = replace(
                v,
                adapter_id=self.name,
                focus_granularity=FocusGranularity.TAB,
                capabilities=type(self).capabilities,
            )
            result.append(SessionGroup(
                group_id=f"wt:singleton:{stamped.pid}",
                title_hint=None,
                adapter_id=self.name,
                views=(stamped,),
            ))

        # Snapshot the view cache for click-time sibling lookup. Built
        # from `kept` (orphan-filtered, post-reconcile) so focus() can
        # translate sibling pids into SessionViews → cwd / session_uuid
        # → cwd-matched sibling sentinels for the inactive-pane fallback.
        # Atomic dict swap; no lock needed for the worker-write /
        # Qt-main-read pattern.
        self._view_cache = {v.session.pid: v for v in kept}
        return result

    # ── FOCUS ────────────────────────────────────────────────────────────

    @capability(Capability.FOCUS)
    def focus(self, view: SessionView, *, siblings: list[int] = ()) -> bool:
        """Bring the WT window to foreground + select the matching tab.

        Passes ``expected_title = sentinel_title(view.session_uuid)`` to
        ``_activate_windows`` so the click-time path can re-assert our
        sentinel if claude or the user has clobbered it since group()
        last reconciled.

        Inactive-pane fallback: when the click target is the inactive
        pane of a split tab, its sentinel doesn't appear in any
        TabItem.Name. We pre-compute the sentinels of *same-cwd
        siblings* (other claude sessions in the same WT window whose
        cwd matches view's cwd — a strong signal they're split panes
        of the same tab, since users usually split panes within one
        project) and pass them as a fallback sentinel list. One of
        them is likely the active pane of the click target's tab,
        and selecting its sentinel switches WT to the right tab.
        User then uses Alt+arrow to focus the right pane.

        We ignore the cross-cwd siblings (same wt_hwnd but different
        cwd) — those are almost certainly separate tabs, so trying
        their sentinels would just take us to the wrong tab.
        """
        from claude_island.core.snapshot import _normalize_project_path
        from claude_island.platform_.wt_session_title import sentinel_title
        expected = sentinel_title(view.session_uuid)

        # Translate sibling pids → SessionViews via the cache built in
        # group(). Filter to same-cwd siblings only (they're the
        # likely-pane-mate candidates), compute their sentinels.
        # Normalize worktree paths back to their parent repo so e.g.
        # `D:\proj\.claude\worktrees\feat-x` matches `D:\proj` —
        # claude-code split panes between main repo and worktree are
        # very common (verified empirically: build-mini-cc + its
        # worktree share a WT split tab).
        my_cwd = _normalize_project_path(view.project_path)
        sib_sentinels: list[str] = []
        for sib_pid in siblings:
            sib_view = self._view_cache.get(sib_pid)
            if sib_view is None:
                continue
            if _normalize_project_path(sib_view.project_path) != my_cwd:
                continue  # different project → almost certainly a different tab
            sib_sentinel = sentinel_title(sib_view.session_uuid)
            if sib_sentinel and sib_sentinel != expected:
                sib_sentinels.append(sib_sentinel)

        return _activate_windows(
            view.session.pid,
            expected_title=expected,
            sibling_sentinels=tuple(sib_sentinels),
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
    sibling_sentinels: tuple[str, ...] = (),
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

    ``sibling_sentinels``: same-cwd same-wt-window claude sessions'
    sentinels, pre-filtered by ``focus()``. Used as the inactive-pane
    fallback — see the chain below.

    Tab-select chain:
      1. ``select_tab_by_title(expected)`` — exact match. Hits the
         active-pane / single-pane case.
      2. If step 1 misses (inactive pane in split tab), try each
         sibling sentinel in order. Same-cwd siblings are very
         likely co-pane: WT users typically split-pane within one
         project, so a sibling that shares cwd is almost certainly
         the active pane of the click target's tab. Selecting its
         sentinel switches WT to the right tab; user uses Alt+arrow
         to focus the intended pane.
      3. ``_force_foreground(hwnd)`` — runs unconditionally after
         step 1/2 so the WT window itself always comes to foreground.

    Per-pane disambiguation of inactive split panes is impossible
    from outside WT (TabItem.Name reflects only the active pane;
    inactive panes have no UIA presence — see scripts/dump_wt_uia.py
    output and TerminalApp/TabManagement.cpp's _UpdatedSelectedTab).
    The cwd filter on sibling_sentinels prevents the click from
    landing on an unrelated tab (e.g. another project's session in
    the same WT window).

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

    resolved = _resolve_console_window(pid, win32gui)
    hwnd: int | None = None
    if resolved is not None:
        hwnd, current_title = resolved
        from claude_island.platform_ import win32_console, wt_uia

        # Click-time reconcile: claude may have rewritten the title
        # via OSC during a topic shift, or this session was just
        # discovered by the scanner and group()'s reconcile hasn't
        # run yet. Re-set then wait for WT's OSC pipeline to mirror
        # the change into TabItem.Name before we issue the select.
        #
        # WARNING: this entire helper runs on the Qt main thread
        # (called from the click handler). The wait_for_tab_name
        # busy-poll is the dominant blocking source — kept under
        # ~80 ms by its default cap (Q-2). If a future change adds a
        # longer-poll variant or removes the cap, the click freezes
        # the UI for that duration. Move the reconcile to a worker
        # thread before going past ~100 ms.
        target_title = expected_title or current_title
        if expected_title and current_title != expected_title:
            win32_console.set_console_title(pid, expected_title)
            # If WT silently dropped our set (suppressApplicationTitle
            # profile), wait_for_tab_name times out and select_tab_by_title
            # below also fails — we still fall back to plain foreground
            # at the end.
            wt_uia.wait_for_tab_name(hwnd, expected_title)

        if not wt_uia.select_tab_by_title(hwnd, target_title):
            # Inactive-pane fallback: try cwd-matched sibling
            # sentinels (caller-filtered to same-cwd same-wt-window).
            # One of them is likely the active pane of the click
            # target's tab — selecting its sentinel switches WT to
            # the right tab.
            for sib_name in sibling_sentinels:
                if sib_name and sib_name != target_title and \
                        wt_uia.select_tab_by_title(hwnd, sib_name):
                    break
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
