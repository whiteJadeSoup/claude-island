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

import logging
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import ClassVar, Sequence

log = logging.getLogger(__name__)

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


def _is_wt_window(hwnd: int, win32gui_mod) -> bool:
    """Return True iff ``hwnd`` has the Windows Terminal window class.

    Used by the fast-path before calling ``SetForegroundWindow`` to
    defend against hwnd recycle — a stale ``conhost_hwnd`` from a
    JumpTarget whose original window has been closed might GW_OWNER-
    walk to a hwnd whose value has been reissued to an unrelated
    process. Without this check we'd raise the wrong window
    (review finding C-004 / D-5).

    Cheap: one win32gui.GetClassName syscall (~50 µs).
    """
    if hwnd <= 0:
        return False
    try:
        cls = win32gui_mod.GetClassName(hwnd)
    except Exception:
        return False
    return bool(cls and cls.startswith(_WT_CLASS_PREFIX))


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

    # ── can_handle ──────────────────────────────────────────────────────

    def can_handle(self, session: Session) -> bool:
        """True when the session's process ancestry includes
        WindowsTerminal.exe or a conpty host that traces there.

        Walks ancestors via psutil. On Windows every claude session is
        inside *some* terminal (WT, conhost, or a bundled app); we
        only claim WT-hosted sessions, generic_windows claims the rest.

        Returns False for placeholder sessions (pid<=0) — those are
        SessionRegistry entries created by HookSessionBridge before the
        scanner has confirmed a real pid; with no pid we cannot walk
        ancestors. Such sessions render via the singleton fallback group
        until the scanner catches up and pid becomes positive.
        """
        if session.pid <= 0:
            return False
        import psutil
        try:
            proc = psutil.Process(session.pid)
            ancestors: list[psutil.Process] = []
            for _ in range(_MAX_ANCESTOR_DEPTH):
                p = proc.parent()
                if p is None: break
                ancestors.append(p)
                proc = p
        except (psutil.NoSuchProcess, psutil.AccessDenied, ValueError):
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
        in it does best-effort tab select. User then uses WT's own
        Alt+arrow to focus the right pane.

        Sentinel title (Plan O reconcile):
        On first sight of a session, we read its console title; if
        it's not already our ``ci:{uuid}`` sentinel, we set it via
        SetConsoleTitleW. WT mirrors that into TabItem.Name (unless
        the tab was launched with ``--suppressApplicationTitle`` —
        Plan L — in which case the title was set at spawn). Either
        way, ``select_tab_by_title`` can find the target tab by its
        ``ci:{uuid32}`` Name.

        ``views`` are guaranteed to be claude sessions by upstream
        ``can_handle`` + the dispatcher chain — so SetConsoleTitleW
        only ever touches claude-attached consoles.

        Drag-tab correctness: a session moved to another WT window
        is re-bucketed on the next signature change (see
        ``_wt_window_signature``); the click-time path re-resolves
        fresh either way.
        """
        win32gui_mod = self._import_win32gui()
        kept = self._filter_orphans_and_reconcile(views, win32gui_mod)
        buckets, singletons = self._bucket_views(kept, win32gui_mod)
        buckets, singletons = self._demote_false_split_panes(
            buckets, singletons,
        )
        return self._stamp_groups(buckets, singletons)

    # ── group() phase helpers ──────────────────────────────────────────
    # Pure structural extraction (S-1 of the multi-agent review). Each
    # helper owns one phase of the pipeline and is independently
    # testable. Behaviour is byte-identical to the pre-extraction
    # implementation — see git history for the inline version.

    @staticmethod
    def _import_win32gui():
        """Return the win32gui module, or ``None`` on non-Windows.

        win32gui is needed for ``walk_to_visible_host`` and the
        WT-window-set signature. None on ImportError → all views fall
        through to singleton groups (we lose the window-grouping but
        rows still render)."""
        try:
            import win32gui  # type: ignore[import-not-found]
            return win32gui
        except ImportError:
            return None

    def _filter_orphans_and_reconcile(
        self, views: list[SessionView], win32gui_mod,
    ) -> list[SessionView]:
        """Phase 1: GC the per-pid caches, invalidate wt_hwnd cache on
        WT-window-set change, then for each view: AttachConsole probe
        (cached) to drop orphans + write sentinel title once on first
        sight. Returns the kept views (orphans dropped); applies the
        tripwire that restores the original list when every view was
        filtered (likely a race with our own console state)."""
        from claude_island.platform_ import win32_console
        from claude_island.platform_.wt_session_title import sentinel_title

        # GC: drop cache entries for pids that no longer exist at the
        # OS level.
        #
        # Why pid-existence and not "pid in this call's views":
        # ``dispatcher.group_sessions`` may invoke this adapter's
        # ``group()`` MULTIPLE times per snap — once per routing
        # bucket (jump_target-routed sessions in one bucket, legacy
        # can_handle-routed in another). The view-scoped GC that lived
        # here previously trimmed the caches to whichever bucket's
        # pids were in the current call, evicting the OTHER bucket's
        # still-alive pids. Every snap, both calls fired AttachConsole
        # afresh — and any transient AttachConsole failure under load
        # then dropped a row from the snap (the flicker bug observed
        # 2026-05-24, captured in flicker.log around 22:47:20: bucket
        # A = [cc-learning] and bucket B = [claude-island,
        # build-mini-cc] thrashed each other's cache).
        #
        # ``psutil.pid_exists`` is a cheap O(1)-ish syscall (Windows
        # PssCaptureSnapshot under the hood). Pid recycle is handled
        # at the call site by win32 handle validation
        # (``GetWindowText`` returning "" on dead conhost); a stale
        # cache value past pid recycle is recovered next probe.
        import psutil
        if self._conpty_cache:
            self._conpty_cache = {
                p: h for p, h in self._conpty_cache.items()
                if psutil.pid_exists(p)
            }
        if self._title_set_attempted:
            self._title_set_attempted = {
                p for p in self._title_set_attempted if psutil.pid_exists(p)
            }
        if self._wt_hwnd_cache:
            self._wt_hwnd_cache = {
                p: h for p, h in self._wt_hwnd_cache.items()
                if psutil.pid_exists(p)
            }

        # Invalidate the wt_hwnd cache on WT-window-set change.
        if win32gui_mod is not None:
            sig = _wt_window_signature(win32gui_mod)
            if sig != self._wt_window_signature:
                self._wt_hwnd_cache = {}
                self._wt_window_signature = sig

        kept: list[SessionView] = []
        for v in views:
            pid = v.session.pid
            # Placeholder pid (hook arrived before scanner) with a
            # jump_target shortcut: trust the hook capture, skip the
            # AttachConsole probe entirely (pid<=0 makes get_console_info
            # raise). conhost_hwnd will drive _bucket_views below. This
            # is the open-vibe-island fast path — the in-process hook
            # already proved the session is real.
            if pid <= 0:
                if v.jump_target is not None and v.jump_target.conhost_hwnd:
                    kept.append(v)
                # else: no identifying info, drop. Next wake will retry
                # when scanner has caught up.
                continue
            conpty_hwnd = self._conpty_cache.get(pid)
            if conpty_hwnd is None:
                info = win32_console.get_console_info(pid)
                if info is None:
                    continue  # orphan / no console — skip; next wake re-probes
                conpty_hwnd, current_title = info
                if not conpty_hwnd:
                    continue
                # conPTY survives for the pid lifetime — cache
                # unconditionally, even if the title-set below fails
                # (otherwise suppressApplicationTitle profiles trap us
                # in a permanent AttachConsole re-probe loop).
                self._conpty_cache[pid] = conpty_hwnd

                # Title set is best-effort, one attempt per pid.
                # EXACT-match against expected — prefix-match would
                # leave a stale ``ci:OLD_UUID`` in place under pid
                # recycle / multi-island scenarios.
                expected = sentinel_title(v.session_uuid)
                if (
                    expected
                    and current_title != expected
                    and pid not in self._title_set_attempted
                ):
                    self._title_set_attempted.add(pid)
                    win32_console.set_console_title(pid, expected)
            kept.append(v)

        # Tripwire: every view filtered → race with our own console
        # state. Keep originals so the user still sees rows.
        if not kept:
            kept = list(views)
        return kept

    def _bucket_views(
        self, kept: list[SessionView], win32gui_mod,
    ) -> tuple[dict[tuple, list[SessionView]], list[SessionView]]:
        """Phase 2: bucket views by ``(wt_hwnd, normalized_cwd)``.

        Same WT window AND same project → likely split panes of one
        tab → group. Different cwd → certainly different tabs (panes
        share cwd by construction). Worktree paths are normalised
        back to their parent repo since claude-code split-pane
        between main repo + worktree is a common workflow.

        Views whose wt_hwnd doesn't resolve become singletons. Returns
        ``(buckets, singletons)``; sentinel-presence demotion happens
        in the next phase."""
        from claude_island.core.snapshot import _normalize_project_path
        from claude_island.platform_ import window_activator

        buckets: dict[tuple, list[SessionView]] = {}
        singletons: list[SessionView] = []
        for v in kept:
            pid = v.session.pid
            wt_hwnd: int | None = None
            # Placeholder views: walk from jump_target.conhost_hwnd to
            # the WT host (no pid → no _conpty_cache entry → can't take
            # the legacy path). The conhost_hwnd was captured inside the
            # claude.exe process by the hook, so it's authoritative.
            if pid <= 0 and v.jump_target is not None and v.jump_target.conhost_hwnd:
                if win32gui_mod is not None:
                    wt_hwnd = window_activator.walk_to_visible_host(
                        v.jump_target.conhost_hwnd, win32gui_mod,
                    )
            else:
                wt_hwnd = self._wt_hwnd_cache.get(pid)
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
        return buckets, singletons

    def _demote_false_split_panes(
        self,
        buckets: dict[tuple, list[SessionView]],
        singletons: list[SessionView],
    ) -> tuple[dict[tuple, list[SessionView]], list[SessionView]]:
        """Phase 3: for each multi-view bucket, ask UIA which sentinels
        currently appear as TabItem.Name. If ALL bucket sentinels are
        present → these are separate tabs that happen to share cwd
        (each is its own active pane) → demote to singletons. If at
        least one is missing → some view is an inactive pane → keep
        grouped. Single UIA call per wt_hwnd (cached across buckets).

        Mutates the dicts in-place AND returns them, so callers can
        chain or inspect either way."""
        from claude_island.platform_ import wt_uia
        from claude_island.platform_.wt_session_title import sentinel_title

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
                key = (wt_hwnd, _cwd)
                del buckets[key]
                singletons.extend(batch)
        return buckets, singletons

    def _stamp_groups(
        self,
        buckets: dict[tuple, list[SessionView]],
        singletons: list[SessionView],
    ) -> list[SessionGroup]:
        """Phase 4: stamp adapter identity / granularity / capabilities
        on every view and emit one ``SessionGroup`` per bucket and per
        singleton. ``group_id`` carries the wt_hwnd + normalised cwd
        for buckets so the UI's per-group_id card cache survives wakes
        and worktree → parent path normalisation doesn't churn ids."""
        from dataclasses import replace

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
            # group_id must be unique. Placeholder pid (-1) is shared
            # across all hook-before-scanner views, so prefer session_uuid
            # which is always set by the hook. Pid fallback handles older
            # SessionView construction paths in tests.
            uid = stamped.session_uuid or str(stamped.pid)
            result.append(SessionGroup(
                group_id=f"wt:singleton:{uid}",
                title_hint=None,
                adapter_id=self.name,
                views=(stamped,),
            ))
        return result

    # ── FOCUS ────────────────────────────────────────────────────────────

    @capability(Capability.FOCUS)
    def focus(
        self, view: SessionView, *, siblings: Sequence[SessionView] = (),
    ) -> bool:
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

        ``siblings`` arrives as the full SessionViews from the same
        UI group (Q-3 of the multi-agent review: previously this was
        a list of pids requiring an adapter-side ``_view_cache`` to
        rehydrate. The cache mirrored a slice of WorldSnapshot and
        violated the 'single source of truth' design principle).
        """
        from claude_island.core.snapshot import _normalize_project_path
        from claude_island.platform_.wt_session_title import sentinel_title
        expected = sentinel_title(view.session_uuid)

        # Filter to same-cwd siblings only (they're the
        # likely-pane-mate candidates), compute their sentinels.
        # Normalize worktree paths back to their parent repo so e.g.
        # `D:\proj\.claude\worktrees\feat-x` matches `D:\proj` —
        # claude-code split panes between main repo and worktree are
        # very common (verified empirically: build-mini-cc + its
        # worktree share a WT split tab).
        my_cwd = _normalize_project_path(view.project_path)
        sib_sentinels: list[str] = []
        for sib in siblings:
            if sib.session.pid == view.session.pid:
                continue  # the clicked view itself, if caller didn't filter
            if _normalize_project_path(sib.project_path) != my_cwd:
                continue  # different project → almost certainly a different tab
            sib_sentinel = sentinel_title(sib.session_uuid)
            if sib_sentinel and sib_sentinel != expected:
                sib_sentinels.append(sib_sentinel)

        # Open-vibe-island pattern (2026-05-14): if the hook captured
        # a JumpTarget at SessionStart, pass its conhost_hwnd as a
        # shortcut so _activate_windows skips the ~50ms AttachConsole
        # round-trip. For real pids we validate host_pid match (defends
        # against pid recycle); for placeholder pids (hook arrived
        # before scanner, session.pid==-1) we trust the hook capture
        # unconditionally — that's the whole point of the hook stream.
        prehook_conhost: int = 0
        if view.jump_target is not None and view.jump_target.conhost_hwnd:
            if (
                view.session.pid <= 0
                or view.jump_target.host_pid == view.session.pid
            ):
                prehook_conhost = view.jump_target.conhost_hwnd

        sib_sentinels_tuple = tuple(sib_sentinels)

        # Fast path (2026-05): resolve wt_hwnd from prehook or
        # adapter cache, raise WT on the main thread, then defer the
        # UIA tab-select chain to the worker thread. See
        # ``design/2026-05-wt-focus-performance.md`` for the rationale.
        # Falls through to the legacy synchronous chain when:
        #   * fast-path module deps are missing (non-Win, no pywin32,
        #     no uiautomation, no pythoncom),
        #   * neither prehook nor cache yields a valid wt_hwnd,
        #   * the resolved hwnd is no longer a WT class window
        #     (defends against hwnd recycle — review finding C-004),
        #   * SetForegroundWindow fails (lets the legacy chain's
        #     AttachThreadInput / SwitchToThisWindow passes run).
        if self._try_fast_path(
            view=view,
            expected=expected,
            sib_sentinels=sib_sentinels_tuple,
            prehook_conhost=prehook_conhost,
        ):
            return True

        # Legacy fallback (unchanged behaviour).
        return _activate_windows(
            view.session.pid,
            expected_title=expected,
            sibling_sentinels=sib_sentinels_tuple,
            prehook_conhost_hwnd=prehook_conhost,
        )

    def _try_fast_path(
        self,
        *,
        view: SessionView,
        expected: str,
        sib_sentinels: tuple[str, ...],
        prehook_conhost: int,
    ) -> bool:
        """Fast-path orchestrator. Returns True iff the WT window was
        raised on the main thread AND the worker task was scheduled.

        Main-thread budget (G1):
          * wt_hwnd resolve (~1–5 ms)
          * GetClassName validation (~50 µs)
          * ``_force_foreground`` happy path (~5 ms warm)
          * Task submit (~50 µs)
          ≈ 6–11 ms total in the prehook-hit cohort.
        """
        try:
            import win32con
            import win32gui
            import win32process
        except ImportError:
            return False

        wt_hwnd = self._resolve_wt_hwnd_fast(
            view, prehook_conhost, win32gui,
        )
        if wt_hwnd is None or wt_hwnd <= 0:
            return False
        if not _is_wt_window(wt_hwnd, win32gui):
            return False

        from claude_island.platform_.window_activator import _force_foreground
        if not _force_foreground(wt_hwnd, win32con, win32gui, win32process):
            return False

        # Schedule async tab-select. If submission fails (deps missing,
        # backlog full, construction error) the window is still raised —
        # the user sees WT in front; the tab just stays where it was.
        from claude_island.platform_.terminals import _wt_fast_path
        _wt_fast_path.try_schedule(
            pid=view.session.pid,
            wt_hwnd=wt_hwnd,
            expected_title=expected,
            sibling_sentinels=sib_sentinels,
        )
        return True

    def _resolve_wt_hwnd_fast(
        self,
        view: SessionView,
        prehook_conhost: int,
        win32gui,
    ) -> int | None:
        """Resolve wt_hwnd without an AttachConsole round-trip.

        Resolution order (per review B-001 / Q-1):
          1. ``prehook_conhost_hwnd`` (hook-captured) → GW_OWNER walk
          2. ``self._wt_hwnd_cache`` (populated by group()) → direct hit
          3. None — caller falls back to the legacy synchronous chain
        """
        from claude_island.platform_.window_activator import walk_to_visible_host

        if prehook_conhost > 0:
            try:
                if win32gui.IsWindow(prehook_conhost):
                    host = walk_to_visible_host(prehook_conhost, win32gui)
                    if host is not None:
                        return host
            except Exception:
                pass

        if view.session.pid > 0:
            cached = self._wt_hwnd_cache.get(view.session.pid)
            if cached:
                try:
                    if win32gui.IsWindow(cached):
                        return cached
                except Exception:
                    pass

        return None

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
    prehook_conhost_hwnd: int = 0,
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

    # Open-vibe-island shortcut (2026-05-14): when the hook gave us a
    # captured conhost_hwnd at SessionStart, walk to wt_hwnd directly
    # from it. Saves the AttachConsole/FreeConsole round-trip
    # (~50ms) that _resolve_console_window pays via get_console_info.
    resolved = _resolve_console_window_fast(
        pid, win32gui, prehook_conhost_hwnd,
    )
    hwnd: int | None = None
    if resolved is not None:
        hwnd, current_title = resolved
        from claude_island.platform_ import win32_console, wt_uia

        # Click-time reconcile: race against Claude OSC overwrites.
        # WT's TabItem.Name can diverge from the kernel title because
        # Claude continuously writes OSC titles. We retry set+wait+select
        # several times in a tight loop to catch the brief window where
        # WT has just mirrored our sentinel before Claude OSCs again.
        #
        # WARNING: this entire helper runs on the Qt main thread
        # (called from the click handler). Total time budget capped
        # at ~150 ms (5 attempts × ~30 ms each) so a click never feels
        # unresponsive.
        #
        # Profile incompatibility: if the WT profile has
        # suppressApplicationTitle:true, NO amount of retry helps —
        # WT silently discards every kernel-set title. The fallback
        # block below gracefully degrades (no wrong-tab navigation).
        target_title = expected_title or current_title
        selected = False
        if expected_title:
            # Fast path: try select_tab_by_title(expected) directly.
            # Works in the common case (sentinel set correctly + WT
            # mirrored it to TabItem.Name). When called via the
            # JumpTarget shortcut the current_title isn't known, so we
            # rely on this attempt to detect whether WT has the title.
            if wt_uia.select_tab_by_title(hwnd, expected_title):
                selected = True
            elif current_title != expected_title:
                # Title drift / Claude OSC clobbered our sentinel.
                # Worth one re-set + wait + select. (When current_title
                # is "" — JumpTarget shortcut — we conservatively try
                # this path; one extra AttachConsole is fine since the
                # fast select above already failed.)
                win32_console.set_console_title(pid, expected_title)
                if wt_uia.wait_for_tab_name(
                    hwnd, expected_title, timeout_ms=80,
                ) and wt_uia.select_tab_by_title(hwnd, expected_title):
                    selected = True
            # else: current_title == expected_title (we knew it via
            # slow path) AND select failed → WT silently discarding
            # title (suppressApplicationTitle). No point re-setting.
        else:
            # No expected_title (degraded SessionView). Best we can do
            # is try current_title.
            if wt_uia.select_tab_by_title(hwnd, target_title):
                selected = True

        # Race-loser fallback (Bug 2026-05-14, mini-cc-opus-dev): when
        # Claude's OSC writes win the race against our SetConsoleTitleW,
        # WT mirrors Claude's title to TabItem.Name (just not our
        # sentinel). ``current_title`` was just read from the kernel
        # for THIS pid, so it IS what WT shows for this conpty's tab.
        # One cheap UIA select before falling to the smart_guess
        # heuristic. Almost always hits because WT happily mirrors
        # Claude's titles — only ours lose the race.
        #
        # Confirmed by scripts/probe_focus.py: pid 82508 had
        # current_title='⠂ Claude Code' which WAS in the WT window's
        # tab_titles list. Sentinel select would've kept missing forever
        # because Claude rewrites its OSC every internal turn.
        #
        # Caveat: when N tabs share the same kernel title (e.g. several
        # Claude sessions all currently on '⠂ Claude Code'),
        # select_tab_by_title picks the first match — which may be a
        # sibling rather than our exact target. Still strictly better
        # than the previous behaviour (no navigation at all): the user
        # lands on a Claude tab in the same WT window instead of
        # whatever was previously frontmost.
        if (
            not selected
            and expected_title
            and current_title
            and current_title != expected_title
            and wt_uia.select_tab_by_title(hwnd, current_title)
        ):
            selected = True

        if not selected:
            # The target's sentinel isn't visible as a TabItem.Name.
            # Three distinct scenarios produce this:
            #   (a) Inactive pane of a split tab — sentinel is set on
            #       the inactive pane's conpty but UIA only surfaces
            #       the ACTIVE pane's title.
            #   (b) Claude OSC overwrote our sentinel after the retry
            #       loop above couldn't win the race in 150ms.
            #   (c) WT profile has suppressApplicationTitle:true and
            #       silently drops every kernel-set title.
            #
            # Strategy: same-cwd sibling fallback first (works for
            # (a) + the (b)/(c) inactive-pane sub-case), then a
            # "smart guess" using the tab list.
            visible_ci_tabs = wt_uia.list_ci_tab_names(hwnd)

            # Step 1: same-cwd siblings → likely pane-mates in the
            # target's tab. Same-cwd + same-WT-window is a strong "same
            # tab, different pane" signal — selecting any sibling lands
            # the user on the right tab; they finish with Alt+arrow.
            # Tried in BOTH the "≤1 ci:* visible" and ">1 ci:* visible"
            # cases (2026-05-17 fix): an earlier revision only tried
            # siblings when ≤1 ci:* was visible, which abandoned the
            # right answer when the target was a sub-pane in a tab
            # whose sibling pane still has its sentinel mirrored by WT.
            # Concrete trigger: build-mini-cc shares cwd + WT window
            # with mini-cc-opus-dev as panes of one tab. Click on
            # build-mini-cc → siblings = (mini-cc-opus-dev,) →
            # selecting mini-cc-opus-dev's sentinel lands the tab,
            # user uses Alt+arrow to switch panes.
            for sib_name in sibling_sentinels:
                if sib_name and sib_name != target_title and \
                        wt_uia.select_tab_by_title(hwnd, sib_name):
                    selected = True
                    break

            # Step 2: when siblings didn't resolve AND multiple ci:*
            # are visible (case (b)/(c) territory), try smart_guess —
            # enumerate all TabItems, find ones whose Name is NOT a
            # known sentinel (= candidates for our suppressed target).
            # If EXACTLY one candidate exists, Select it. 0 or 2+ →
            # abstain (foreground-only + diagnostic).
            #
            # The earlier ``content_match`` approach cycled visibly
            # through candidates reading each tab's terminal text.
            # User correctly identified this as bad UX (2026-05-13).
            # Per open-vibe-island's architecture: hooks should
            # capture terminal-identifying info at SessionStart and
            # click uses it directly. For WT specifically, no
            # outside-process API maps conpty hwnd → UIA TabItem
            # reliably (verified 2026-05-17 via UIA probe — TabItem's
            # ProcessId is WT's own pid, NativeWindowHandle is 0,
            # AutomationId is empty), so for ambiguous cases we
            # surface a clear diagnostic and fall back to
            # force_foreground.
            if not selected and len(visible_ci_tabs) > 1:
                known = set(visible_ci_tabs) | set(sibling_sentinels)
                # Our own expected_title belongs in candidates too —
                # remove only OTHER sentinels.
                if expected_title:
                    known.discard(expected_title)
                if not _try_smart_guess_select(hwnd, exclude_names=known):
                    if expected_title:
                        _emit_suppress_title_diagnostic(
                            expected_title.removeprefix("ci:"),
                        )
    else:
        from claude_island.platform_.window_activator import _ancestor_pids, _find_window_for_pids
        candidate_pids = _ancestor_pids(pid)
        if candidate_pids:
            hwnd = _find_window_for_pids(candidate_pids, win32gui, win32process)

    if hwnd is None:
        return False
    from claude_island.platform_.window_activator import _force_foreground
    return _force_foreground(hwnd, win32con, win32gui, win32process)


def _try_smart_guess_select(hwnd: int, *, exclude_names: set[str]) -> bool:
    """Smart guess for the suppressApplicationTitle case (Bug C deep
    fix, 2026-05-13).

    When our sentinel can't be matched via TabItem.Name (because the
    target's WT profile suppresses application titles), enumerate all
    TabItems and identify "candidates": tabs whose Name is NOT a known
    other-session sentinel. If EXACTLY one candidate exists, it's
    almost certainly our target — select it.

    If 0 candidates or 2+ candidates exist, abstain: a wrong-tab
    selection is worse than no-op (user can still see the WT window
    via the subsequent _force_foreground call). The caller is
    expected to emit a one-time diagnostic explaining WT's profile
    limitation.

    Returns True if a candidate was selected, False otherwise.
    """
    try:
        import uiautomation as auto
        root = auto.ControlFromHandle(hwnd)
        if root is None:
            return False
        tab_control = root.TabControl(searchDepth=10)
        if not tab_control.Exists(0.1):
            return False

        # Find the ListControl wrapping all TabItems (WinUI3 TabView).
        list_ctrl = None
        for c in tab_control.GetChildren():
            if getattr(c, "ControlTypeName", "") == "ListControl":
                list_ctrl = c
                break
        if list_ctrl is None:
            return False

        candidates = []
        for item in list_ctrl.GetChildren():
            if getattr(item, "ControlTypeName", "") != "TabItemControl":
                continue
            name = getattr(item, "Name", "") or ""
            if name in exclude_names:
                continue
            candidates.append(item)

        if len(candidates) != 1:
            return False

        candidate = candidates[0]
        try:
            sel = candidate.GetSelectionItemPattern()
            if sel is None:
                return False
            sel.Select()
            return True
        except Exception:
            return False
    except Exception as exc:
        log.debug("_try_smart_guess_select failed: %s", exc)
        return False


# Module-level: emit the suppressed-title diagnostic at most once per
# process lifetime. Spam to stderr on every click would be obnoxious;
# the user only needs to see it once to know what's going on.
_suppress_title_warning_emitted = False


def _emit_suppress_title_diagnostic(target_uuid: str) -> None:
    """Write a one-time stderr message explaining why click couldn't
    navigate when WT's profile suppresses application titles.

    Detection heuristic: we got here because multiple TabItems with
    names NOT in our sentinel set existed (smart_guess abstained).
    The classic cause is ``suppressApplicationTitle: true`` in one or
    more WT profiles. Surface that diagnosis to the user so they
    understand the click no-op and have a clear path to fix it
    (edit WT settings).

    Idempotent — only emits once per process. Subsequent calls no-op
    so the user's stderr isn't spammed by repeated clicks.
    """
    global _suppress_title_warning_emitted
    if _suppress_title_warning_emitted:
        return
    _suppress_title_warning_emitted = True

    # Probe WT's settings.json to confirm or deny our suspicion.
    settings_path = (
        Path.home()
        / "AppData/Local/Packages"
        / "Microsoft.WindowsTerminal_8wekyb3d8bbwe"
        / "LocalState" / "settings.json"
    )
    suppressed_profiles: list[str] = []
    try:
        import json
        text = settings_path.read_text(encoding="utf-8")
        data = json.loads(text)
        profiles = data.get("profiles", {})
        if isinstance(profiles, dict):
            for plist in (profiles.get("list", []), profiles.get("defaults", {})):
                if isinstance(plist, list):
                    for p in plist:
                        if isinstance(p, dict) and p.get("suppressApplicationTitle"):
                            suppressed_profiles.append(
                                p.get("name", "?") or "?"
                            )
                elif isinstance(plist, dict):
                    if plist.get("suppressApplicationTitle"):
                        suppressed_profiles.append("<defaults>")
    except (OSError, ValueError, KeyError):
        pass

    # Tone: this is a one-time informational hint, not an error. WT
    # was brought to foreground successfully; only the tab-switch
    # couldn't auto-target because of a WT profile limitation outside
    # our control. Phrase it accordingly so the user doesn't think
    # something crashed.
    if suppressed_profiles:
        print(
            f"[claude-island] note: tab auto-switch unavailable — your WT "
            f"profile(s) {suppressed_profiles} have `suppressApplicationTitle: "
            f"true`, which prevents external tab identification. To enable "
            f"click-to-tab, set that option to false in WT settings.json. "
            f"Sessions started via the Resume drawer (claude-island spawns "
            f"the WT tab) are unaffected and navigate cleanly.",
            file=sys.stderr,
        )
    else:
        print(
            f"[claude-island] note: tab auto-switch unavailable for this "
            f"session — Claude's terminal title overrides our sentinel before "
            f"WT mirrors it. WT window is in foreground; click the tab once "
            f"manually. Future sessions started via the Resume drawer skip "
            f"this limitation.",
            file=sys.stderr,
        )


def _resolve_console_window(pid: int, win32gui) -> tuple[int, str] | None:
    # Placeholder pid (hook arrived before scanner) cannot be resolved
    # via get_console_info — that path needs a real OS handle. Caller
    # is expected to take the fast path (prehook_conhost_hwnd) instead;
    # this guard just prevents a crash if the fast path also missed.
    if pid <= 0:
        return None
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


def _resolve_console_window_fast(
    pid: int,
    win32gui,
    prehook_conhost_hwnd: int,
) -> tuple[int, str] | None:
    """Open-vibe-island JumpTarget shortcut (2026-05-14).

    When ``prehook_conhost_hwnd`` is non-zero, the hook captured the
    conhost hwnd at SessionStart and shipped it via JumpTarget. We
    walk straight to the WT host without an AttachConsole round-trip.

    Trade-off: we can't read the current console title without
    AttachConsole (verified: ``GetWindowText`` on conhost returns ""
    even when the kernel title is set). So we return ``""`` for
    title — the caller's tab-select chain handles this by trying
    select_tab_by_title(expected) optimistically before deciding
    whether to enter the slow set+wait+select branch.

    Falls through to the slow ``_resolve_console_window`` when:
      * prehook_conhost_hwnd is 0 (no hook coverage / capture failed)
      * the hwnd is no longer valid (pid recycle / exit)
      * the walk from the pre-hook hwnd fails

    Returns ``(wt_host_hwnd, console_title_or_empty)``.
    """
    from claude_island.platform_.window_activator import walk_to_visible_host

    if prehook_conhost_hwnd > 0:
        try:
            if not win32gui.IsWindow(prehook_conhost_hwnd):
                return _resolve_console_window(pid, win32gui)
        except Exception:
            return _resolve_console_window(pid, win32gui)

        host = walk_to_visible_host(prehook_conhost_hwnd, win32gui)
        if host is not None:
            return (host, "")  # Title unknown — caller handles.

    return _resolve_console_window(pid, win32gui)
