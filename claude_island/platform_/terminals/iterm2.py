"""iTerm2 terminal adapter — pane-level focus + (window, tab) grouping
via AppleScript / ``osascript``.

Why osascript and not the official ``iterm2`` PyPI package
----------------------------------------------------------
The ``iterm2`` package on PyPI is GPLv2+. Importing it would pull
this whole project under GPL — which we don't want for the same
reason claude-island stays MIT-friendly. AppleScript via
``osascript`` is a built-in macOS interface that talks the same
Apple Events under the hood; no extra deps, no license bleed.

iTerm2's AppleScript dictionary exposes:
  application "iTerm"
    windows / tabs / sessions      — the hierarchy
    id of window                    — stable int per WT window
    tty of session                  — e.g. ``/dev/ttys001``
    select session                  — make this pane the active one
    select tab                      — make this tab the active one
    activate                        — raise iTerm2 to foreground

Two AppleScript invocations:
  - Enumerate (in ``group``):  one round-trip per scan tick listing
    every ``window_id|tab_index|tty`` triple. Cached for the duration
    of one ``group()`` call.
  - Activate (in ``focus``):   one round-trip per click; finds the
    session whose tty matches and selects + activates it.

Permission model
----------------
First click triggers macOS's "‹app› wants to control iTerm" prompt
(System Settings ▶ Privacy & Security ▶ Automation). User clicks
allow; the consent persists. If declined, ``osascript`` exits non-zero
and we degrade — generic_mac then takes over for that session, so
the user still gets an "app to front" focus.

Failure modes
-------------
* iTerm2 not running             → ``osascript`` exits ≠ 0 → ``can_handle``
                                   never matches because no iTerm in
                                   ancestry; group never called.
* iTerm2 running but session
  spawned outside iTerm           → ``can_handle`` False (ancestor walk
                                   doesn't find iTerm), generic_mac
                                   takes it.
* tty mapping fails for a view    → that view goes to a singleton
                                   group, FOCUS still tries the tty
                                   match (returns False if no match).
"""
from __future__ import annotations

import logging
import shlex
import subprocess
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import ClassVar, Sequence

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
from claude_island.platform_.terminals import _iterm_fast_path, adapter
from claude_island.platform_.terminals._macos_common import (
    focus_host_app,
    prewarm_ui_pid_cache,
)
from claude_island.platform_.terminals.protocols import TerminalAdapter


log = logging.getLogger(__name__)

# Process-ancestry detection. iTerm2 spawns shells whose parents
# (eventually) reach the iTerm2.app helper; psutil exposes the chain.
_ITERM2_ANCESTOR_NAMES = frozenset({"iterm2", "iterm"})  # case-insensitive
_MAX_ANCESTOR_DEPTH = 10

# osascript timeout — long enough for cold-start AppleScript dispatch
# but short enough that an iTerm2 hang doesn't freeze the snapshotter.
_OSASCRIPT_TIMEOUT_S = 3.0

# Field separator for the enumeration output. Pipe is safe — neither
# window ids nor tty paths contain it on macOS.
_ENUM_SEP = "|"

# AppleScript that walks every iTerm window/tab/session and emits one
# ``window_id|tab_index|tty`` triple per line. Returned via stdout so
# Python parses the text rather than threading scriptable objects.
_ENUM_SCRIPT = """\
tell application "iTerm"
    set out to ""
    repeat with w in windows
        set wid to id of w
        set tabIdx to 0
        repeat with t in tabs of w
            set tabIdx to tabIdx + 1
            repeat with s in sessions of t
                set out to out & wid & "|" & tabIdx & "|" & (tty of s) & linefeed
            end repeat
        end repeat
    end repeat
    return out
end tell
"""

# AppleScript for FOCUS — finds the iTerm2 pane whose tty matches the
# clicked session, selects its window + tab + session, then brings
# iTerm2 to the front via System Events. Returns "ok" / "miss" via
# stdout so the Python side can distinguish "tty wasn't in iTerm2's
# tree" (return False) from osascript errors (subprocess returncode ≠ 0).
#
# Why ``System Events ▶ set frontmost`` instead of ``tell iTerm to
# activate``: claude-island's panel uses Qt.WindowStaysOnTopHint and
# is the macOS frontmost application at the moment the user clicks a
# row. ``[NSApp activate]`` (what iTerm2's ``activate`` ultimately
# calls) is governed by AppKit's "non-active app cannot order its
# windows above the active app's windows" rule — it logs ``Window …
# ordered front from a non-active application and may order beneath
# the active application's windows`` and visually does nothing. The
# user sees AppleScript return "ok" but no window switches.
#
# System Events' ``set frontmost of (... unix id is HOST_PID ...) to
# true`` runs with higher privilege via the accessibility API and
# bypasses that rule, so the target window is actually surfaced.
# Verified live: the AppKit warning disappears and the user-perceived
# "click does nothing" symptom resolves.
#
# Why target by ``unix id`` instead of ``process "iTerm2"``: a user
# can have two iTerm2 installations running side by side (e.g. the
# 3.6.9 in /Applications and the 3.6.10 in ~/Applications, both
# bundle id ``com.googlecode.iterm2``). ``first process whose name is
# "iTerm2"`` returns the first match — usually the older instance —
# regardless of which one hosts the clicked session, so clicks on
# sessions in the OTHER instance silently bring the wrong window to
# front. Resolving the host pid via ``find_iterm2_host_pid`` at click
# time and targeting it by unix id picks the correct instance.
#
# ``select w`` is still load-bearing for the multi-window case:
# raising iTerm2 to the OS foreground doesn't pick which iTerm2
# window inside the app is on top — without ``select w`` the target
# pane lives in whatever window happens to be frontmost in iTerm2's
# own z-order, which can leave the user staring at a different
# window with the right pane hidden behind it.
#
# ``set miniaturized of w to false`` is required before ``select w``:
# AppleScript's ``select`` brings a window forward in iTerm's z-order
# but does NOT deminiaturize a window that's been minimized to the
# Dock. Without this, clicking a session whose host window is in the
# Dock activates iTerm but leaves the window stuck in the Dock —
# user perceives "click did nothing". Idempotent: no-op when the
# window is already visible. Mirrored in ``_FOCUS_SCRIPT_BY_ID_TEMPLATE``
# and the fast-path handlers (``_iterm_fast_path._FOCUS_BY_*``).
_FOCUS_SCRIPT_TEMPLATE = """\
tell application "System Events"
    set frontmost of (first process whose unix id is {host_pid}) to true
end tell
tell application "iTerm"
    repeat with w in windows
        repeat with t in tabs of w
            repeat with s in sessions of t
                if tty of s is "{tty}" then
                    set miniaturized of w to false
                    select w
                    select t
                    select s
                    set index of w to 1
                    return "ok"
                end if
            end repeat
        end repeat
    end repeat
    return "miss"
end tell
"""

# Same shape as _FOCUS_SCRIPT_TEMPLATE but matches by stable session id
# (``id of s``) instead of tty. Used when the hook captured an
# ``iterm_session_id`` at SessionStart time — id matching is preferred
# because:
#   • tty can drift across reconnect / pane reuse / process restart;
#     session id is stable for the lifetime of the iTerm session.
#   • Avoids the click-time ``psutil.Process(pid).terminal()`` syscall
#     and the iTerm-3.6.9-vs-3.6.10 enumeration ambiguity (we ask the
#     specific iTerm instance addressed by host_pid for the session
#     with this id).
# Falls through to "miss" if the captured id has aged out (window
# closed / iTerm restarted); the caller then falls back to the
# tty-template path and ultimately to ``focus_host_app``.
_FOCUS_SCRIPT_BY_ID_TEMPLATE = """\
tell application "System Events"
    set frontmost of (first process whose unix id is {host_pid}) to true
end tell
tell application "iTerm"
    repeat with w in windows
        repeat with t in tabs of w
            repeat with s in sessions of t
                if (id of s as text) is "{session_id}" then
                    set miniaturized of w to false
                    select w
                    select t
                    select s
                    set index of w to 1
                    return "ok"
                end if
            end repeat
        end repeat
    end repeat
    return "miss"
end tell
"""


@adapter("iterm2", priority=100, platform="mac")
class ITerm2Adapter(_CapabilityProvider):
    """Adapter for claude sessions running inside iTerm2.

    Pane-level focus + (window, tab) grouping. Sits at priority=100
    so it claims sessions before generic_mac; sessions whose ancestry
    doesn't contain iTerm2 fall through to the chain's lower tiers.
    """

    name: ClassVar[str] = ""  # set by @adapter
    _priority: int = 0

    # ── can_handle ──────────────────────────────────────────────────────

    def can_handle(self, session: Session) -> bool:
        """True when iTerm2 appears in the session's ancestor chain.

        Walks ``psutil.Process(pid).parent()`` up to depth 10. iTerm2
        usually shows up as ``iTerm2`` (process name) within 2-3 hops
        for a shell session; deeper if there's a tmux / nested launcher.
        Cheap — the parent chain is local memory once psutil snapshot
        is in cache."""
        # Hook-bridge placeholder sessions (pid<=0) have no real process
        # to walk; can't prove iTerm2 ancestry → return False and let
        # jump_target routing place the view instead.
        if session.pid <= 0:
            return False
        try:
            import psutil
        except ImportError:
            return False
        try:
            proc = psutil.Process(session.pid)
            for _ in range(_MAX_ANCESTOR_DEPTH):
                p = proc.parent()
                if p is None:
                    break
                try:
                    if p.name().lower() in _ITERM2_ANCESTOR_NAMES:
                        return True
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    pass
                proc = p
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            return False
        return False

    # ── group ───────────────────────────────────────────────────────────

    def group(self, views: list[SessionView]) -> list[SessionGroup]:
        """Bucket views by (window_id, tab_index) via tty matching.

        One osascript call enumerates every iTerm2 pane's tty +
        owning (window_id, tab_index). For each view we resolve its
        tty via psutil, look up the (window, tab) coordinates, and
        bucket. Views whose tty isn't in the iTerm2 tree (e.g. the
        process was reparented away) become singleton groups so they
        still render — FOCUS will then return False for them, and
        the user can fall back to manually finding the tab.
        """
        # Prewarm the UI-pids cache while we're on the worker thread.
        # When a click later triggers focus_host_app on the Qt main
        # thread, the cache is warm and we save ~270 ms of osascript
        # round-trip per click. No-op when cache is fresh.
        prewarm_ui_pid_cache()
        try:
            import psutil
        except ImportError:
            return _singletons(views, self.name)

        tty_to_coords = self._enumerate_panes()
        if tty_to_coords is None:
            # Enumeration failed (osascript timeout / iTerm2 quit
            # mid-tick). Render each as singleton — caller's chain
            # has no other adapter able to claim these (we already
            # passed can_handle), so we must keep them visible.
            return _singletons(views, self.name)

        # tty per view. Placeholder pids (<=0) come from hook-bridged
        # sessions whose scanner snapshot hasn't landed yet — no real
        # process to ask for tty, fall through to singleton bucket.
        view_ttys: dict[int, str | None] = {}
        for v in views:
            if v.session.pid <= 0:
                view_ttys[v.pid] = None
                continue
            try:
                view_ttys[v.pid] = psutil.Process(v.session.pid).terminal()
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                view_ttys[v.pid] = None

        # Bucket
        buckets: dict[tuple, list[SessionView]] = {}
        for v in views:
            tty = view_ttys[v.pid]
            coords = tty_to_coords.get(tty) if tty else None
            if coords is None:
                key: tuple = ("singleton", v.pid)
            else:
                key = ("iterm2", coords[0], coords[1])  # (window_id, tab_idx)
            buckets.setdefault(key, []).append(v)

        result: list[SessionGroup] = []
        for key, batch in buckets.items():
            stamped = [
                replace(
                    v,
                    adapter_id=self.name,
                    focus_granularity=FocusGranularity.PANE,
                    capabilities=type(self).capabilities,
                )
                for v in batch
            ]
            if key[0] == "iterm2":
                gid = f"iterm2:{key[1]}:{key[2]}"
                title_hint = ", ".join(
                    sorted({v.project_basename for v in stamped})[:2]
                )
            else:
                gid = f"iterm2:singleton:{key[1]}"
                title_hint = None
            result.append(SessionGroup(
                group_id=gid, title_hint=title_hint or None,
                adapter_id=self.name, views=tuple(stamped),
            ))
        return result

    # ── FOCUS ────────────────────────────────────────────────────────────

    @capability(Capability.FOCUS)
    def focus(
        self, view: SessionView, *, siblings: Sequence[SessionView] = (),
    ) -> bool:
        """Activate the iTerm2 pane whose tty matches this session.

        ``siblings`` is accepted for kwargs uniformity with the WT
        adapter, but ignored — iTerm2 exposes pane-level identity
        directly via tty matching, so there's no need for a sibling
        fallback.

        Two-tier strategy
        -----------------
        1. **Fast path** (PyObjC + in-process AppKit): main thread
           calls ``NSRunningApplication.activate`` on the resolved iTerm
           host pid (~0.3 ms warm), then schedules a worker-thread
           ``NSAppleScript`` pane select. The host raise is what the
           user perceives — the panel's WindowDeactivate fires
           immediately and iTerm appears in front.
        2. **Legacy fallback** (subprocess osascript): preserved
           verbatim from the pre-fast-path implementation. Triggers
           when PyObjC is unavailable, host pid can't be resolved, or
           ``NSRunningApplication.activate`` reports failure.

        Returns ``True`` if either path put iTerm in front. The
        "True" semantics shifted slightly between paths — fast-path
        returns True after host raise (pane select is fire-and-forget),
        legacy returns True after a full subprocess round-trip
        including pane select. No caller distinguishes (dispatcher
        only logs on False; UI doesn't read the return value).
        """
        del siblings
        jt = view.jump_target

        # ── Fast path ──────────────────────────────────────────────
        host_pid = self._resolve_host_pid(view, jt)
        if host_pid is not None and host_pid > 0:
            # Prefer hook-captured iterm_session_id when present — it's
            # stable across tty drift and avoids the click-time psutil
            # syscall. Only fall back to a psutil tty lookup when the
            # hook didn't capture an id (older hook.py, capture race,
            # session that started outside iTerm).
            session_id: str | None = (
                jt.iterm_session_id
                if jt is not None and jt.iterm_session_id
                else None
            )
            tty: str | None = None if session_id else self._resolve_tty(view)
            try:
                if _iterm_fast_path.try_fast_path(
                    host_pid=host_pid,
                    session_id=session_id,
                    tty=tty,
                ):
                    return True
            except Exception as e:
                # Fast-path module should never raise — it has its own
                # exception boundary — but if it does, log and fall
                # through to legacy so the click is never silent.
                log.warning("iterm2 fast-path raised; falling back: %s", e)

        # ── Legacy fallback (unchanged behaviour) ──────────────────
        return self._legacy_focus(view, jt)

    def _resolve_host_pid(
        self, view: SessionView, jt: object | None,
    ) -> int | None:
        """Pick the iTerm2 host pid for ``view``.

        Prefers hook-captured ``jump_target.terminal_pid``; falls back
        to the runtime ancestor walk. Returns None when neither yields
        a valid pid (placeholder session, no iTerm ancestor).

        The hook-captured pid is **liveness-checked** before being
        trusted: macOS recycles pids, and the captured pid is frozen
        at SessionStart time. If iTerm restarted (or the user just
        kept the row alive across reboots), the captured pid may now
        belong to a *different* UI app — activating it would silently
        focus Slack / Mail / etc. instead of iTerm. We require the
        process to still exist AND its name to match the iTerm
        ancestor set; otherwise we fall through to the runtime walk
        which always derives the host from the live claude pid."""
        if jt is not None and getattr(jt, "terminal_pid", 0) > 0:
            pid = int(jt.terminal_pid)  # type: ignore[attr-defined]
            if _pid_is_iterm(pid):
                return pid
            log.info(
                "iterm2: jt.terminal_pid=%d no longer iTerm "
                "(recycled or app restart); falling back to runtime walk",
                pid,
            )
        if view.session.pid > 0:
            return _iterm_host_pid(view.session.pid)
        return None

    def _resolve_tty(self, view: SessionView) -> str | None:
        """psutil-based tty lookup, defensively swallowing all failures.

        Returns None for placeholder pids, dead processes, or psutil
        absence — caller treats None as "no tty signal available"."""
        if view.session.pid <= 0:
            return None
        try:
            import psutil
        except ImportError:
            return None
        try:
            return psutil.Process(view.session.pid).terminal()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            return None

    def _legacy_focus(self, view: SessionView, jt: object | None) -> bool:
        """Pre-fast-path implementation, preserved verbatim.

        Used when the PyObjC fast-path can't run (ImportError, host pid
        unresolvable, activate failure). Same subprocess osascript
        chain we shipped before fast-path: id-match → tty-match →
        ``focus_host_app`` (just raise the app, no pane precision)."""
        if jt is not None and getattr(jt, "iterm_session_id", "") and getattr(jt, "terminal_pid", 0) > 0:
            if _focus_by_session_id(
                jt.iterm_session_id,  # type: ignore[attr-defined]
                host_pid=jt.terminal_pid,  # type: ignore[attr-defined]
            ):
                return True
            # Captured id didn't resolve (iTerm restarted, session
            # closed). Fall through to the slow path which re-derives
            # everything from psutil.

        if view.session.pid <= 0:
            return focus_host_app(view.session.pid)
        try:
            import psutil
        except ImportError:
            return focus_host_app(view.session.pid)
        try:
            tty = psutil.Process(view.session.pid).terminal()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            return focus_host_app(view.session.pid)
        host_pid = (
            jt.terminal_pid  # type: ignore[attr-defined]
            if jt is not None and getattr(jt, "terminal_pid", 0) > 0
            else _iterm_host_pid(view.session.pid)
        )
        if host_pid is None:
            return focus_host_app(view.session.pid)
        if tty and _focus_by_tty(tty, host_pid=host_pid):
            return True
        return focus_host_app(view.session.pid)

    # ── LAUNCH ───────────────────────────────────────────────────────────

    @capability(Capability.LAUNCH)
    def launch(
        self,
        *,
        cwd: Path,
        command: tuple[str, ...],
        session_uuid: str | None = None,
    ) -> SpawnResult:
        """Spawn a new iTerm2 window in ``cwd`` running ``command``.

        Used by RecentsDrawer's Resume click — same contract as the
        Windows adapter: takes raw cwd + command (no SessionView).
        AppleScript creates a new window with the default profile,
        then writes ``cd <cwd> && <command>`` into the new session.

        ``session_uuid`` is accepted for kwargs uniformity with the
        WT adapter's Plan-L title-locking, but ignored on macOS —
        iTerm2's AppleScript dictionary already exposes per-session
        ``tty`` as a stable PID-independent identifier, so we don't
        need to inject one via the tab title.

        ``terminal_pid`` we report is the osascript pid (NOT iTerm2's —
        iTerm2 is a long-lived application; the spawn doesn't create
        a new app process). It's still a useful diagnostic anchor for
        the "couldn't detect new session" toast on timeout."""
        del session_uuid  # ignored — see docstring
        cmd_str = "cd " + shlex.quote(str(cwd)) + " && " + " ".join(
            shlex.quote(a) for a in command
        )
        # AppleScript uses double-quoted strings; escape backslashes
        # and double quotes in the user's command before interpolation.
        cmd_escaped = _escape_applescript_string(cmd_str)
        script = (
            'tell application "iTerm"\n'
            '  activate\n'
            '  create window with default profile\n'
            '  tell current session of current window\n'
            f'    write text "{cmd_escaped}"\n'
            '  end tell\n'
            'end tell\n'
        )
        try:
            proc = subprocess.Popen(
                ["osascript", "-e", script], close_fds=True,
            )
        except (OSError, FileNotFoundError) as e:
            raise LauncherSpawnError(f"osascript spawn failed: {e}") from e
        return SpawnResult(
            terminal_name=self.name,
            terminal_pid=proc.pid,
            started_at=datetime.now(timezone.utc),
        )

    # ── internal: osascript enumeration ─────────────────────────────────

    def _enumerate_panes(self) -> dict[str, tuple[int, int]] | None:
        """Run the enumeration AppleScript; return ``{tty:
        (window_id, tab_index)}``. Returns None on osascript failure
        (iTerm2 not running, AppleScript permission denied, parse
        error) so the caller can fall back to singleton grouping."""
        try:
            result = subprocess.run(
                ["osascript", "-e", _ENUM_SCRIPT],
                capture_output=True, timeout=_OSASCRIPT_TIMEOUT_S,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        if result.returncode != 0:
            return None
        return _parse_enum_output(result.stdout.decode("utf-8", errors="replace"))


# ── module-level helpers ──────────────────────────────────────────────────

def _parse_enum_output(text: str) -> dict[str, tuple[int, int]]:
    """Parse the enumeration script's ``window_id|tab_index|tty``
    lines into a ``{tty: (window_id, tab_index)}`` dict.

    Tolerant of trailing whitespace, blank lines, and malformed rows
    (which are skipped — never raises). Last row wins on duplicate
    ttys (which shouldn't happen but the dict semantic makes it safe).
    """
    out: dict[str, tuple[int, int]] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        parts = line.split(_ENUM_SEP)
        if len(parts) != 3:
            continue
        wid_s, tab_s, tty = parts[0].strip(), parts[1].strip(), parts[2].strip()
        try:
            wid = int(wid_s)
            tab = int(tab_s)
        except ValueError:
            continue
        if not tty:
            continue
        out[tty] = (wid, tab)
    return out


def _focus_by_session_id(session_id: str, *, host_pid: int) -> bool:
    """Run the focus AppleScript matching by iTerm session id. Returns
    True iff osascript completed AND the script reported "ok".

    Preferred over ``_focus_by_tty`` when the SessionStart hook
    captured ``iterm_session_id`` — id is stable across tty drift
    and avoids the click-time psutil terminal() lookup. ``host_pid``
    is the iTerm2 host pid (also from ``jump_target.terminal_pid``)
    used by the System Events frontmost call.

    A "miss" return (id not found in any window/tab/session of the
    addressed iTerm instance) means the captured id has aged out
    (iTerm restarted, the session window was closed). The caller
    falls back to ``_focus_by_tty`` to recover.
    """
    script = _FOCUS_SCRIPT_BY_ID_TEMPLATE.format(
        session_id=_escape_applescript_string(session_id),
        host_pid=int(host_pid),
    )
    try:
        result = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True, timeout=_OSASCRIPT_TIMEOUT_S,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    if result.returncode != 0:
        return False
    return result.stdout.decode("utf-8", errors="replace").strip() == "ok"


def _focus_by_tty(tty: str, *, host_pid: int) -> bool:
    """Run the focus AppleScript for the given tty. Returns True iff
    osascript completed AND the script reported "ok" (i.e. iTerm2
    found a session matching the tty and selected/activated it).

    ``host_pid`` is the pid of the specific iTerm2 instance that owns
    the session. Targeting by pid (not by process name) is required
    when two iTerm2 installations are running simultaneously — see
    ``_FOCUS_SCRIPT_TEMPLATE`` docstring for context."""
    script = _FOCUS_SCRIPT_TEMPLATE.format(
        tty=_escape_applescript_string(tty),
        host_pid=int(host_pid),
    )
    try:
        result = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True, timeout=_OSASCRIPT_TIMEOUT_S,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    if result.returncode != 0:
        return False
    return result.stdout.decode("utf-8", errors="replace").strip() == "ok"


def _pid_is_iterm(pid: int) -> bool:
    """True iff ``pid`` is alive AND its process name matches the iTerm
    ancestor set.

    Used by :meth:`ITerm2Adapter._resolve_host_pid` to validate
    hook-captured ``terminal_pid`` before trusting it. Without this,
    a recycled pid that now belongs to a non-iTerm UI app (Slack, etc.)
    silently steals focus when the user clicks the row.

    psutil-only — no AppleScript, so cheap (~0.1 ms) and safe to call
    from the Qt main thread at click time. False on any psutil failure
    (process gone, access denied, psutil missing): the caller falls
    through to the runtime ancestor walk, which always derives from
    the live claude pid."""
    if pid <= 0:
        return False
    try:
        import psutil
    except ImportError:
        # No psutil → caller should fall through to runtime walk
        # (which also bails without psutil). Match that behaviour by
        # refusing to trust the cached pid here.
        return False
    try:
        return psutil.Process(pid).name().lower() in _ITERM2_ANCESTOR_NAMES
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return False


def _iterm_host_pid(claude_pid: int) -> int | None:
    """Walk ``claude_pid``'s parent chain and return the pid of the
    first iTerm2-named ancestor, or ``None`` if no such ancestor is
    found within ``_MAX_ANCESTOR_DEPTH`` hops.

    Used at FOCUS time to disambiguate between multiple iTerm2
    installations running side by side — see ``_FOCUS_SCRIPT_TEMPLATE``.
    Returns None on any psutil failure so the caller can fall back to
    the generic host-app raise rather than silently no-op'ing.
    """
    if claude_pid <= 0:
        return None
    try:
        import psutil
    except ImportError:
        return None
    try:
        proc = psutil.Process(claude_pid)
        for _ in range(_MAX_ANCESTOR_DEPTH):
            p = proc.parent()
            if p is None:
                return None
            try:
                if p.name().lower() in _ITERM2_ANCESTOR_NAMES:
                    return p.pid
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
            proc = p
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return None
    return None


def _escape_applescript_string(s: str) -> str:
    """Escape a string for safe interpolation into an AppleScript
    double-quoted literal. Backslash and double-quote are the only
    chars that matter inside ``"..."`` — newlines mid-string would
    end the literal but ttys don't contain them."""
    return s.replace("\\", "\\\\").replace('"', '\\"')


def _singletons(views: list[SessionView], adapter_name: str) -> list[SessionGroup]:
    """Fallback grouping when enumeration fails or psutil is missing.
    One singleton group per view, stamped with the iTerm2 adapter
    identity so dispatch routes back here for FOCUS attempts (which
    will retry the AppleScript)."""
    result: list[SessionGroup] = []
    for v in views:
        stamped = replace(
            v,
            adapter_id=adapter_name,
            focus_granularity=FocusGranularity.PANE,
            capabilities=ITerm2Adapter.capabilities,
        )
        result.append(SessionGroup(
            group_id=f"iterm2:singleton:{stamped.pid}",
            title_hint=None,
            adapter_id=adapter_name,
            views=(stamped,),
        ))
    return result
