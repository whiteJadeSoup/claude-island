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
from claude_island.platform_.terminals import adapter
from claude_island.platform_.terminals._macos_common import (
    focus_host_app,
    prewarm_ui_pid_cache,
)
from claude_island.platform_.terminals.protocols import TerminalAdapter

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
# System Events' ``set frontmost of process "iTerm2" to true`` runs
# with higher privilege via the accessibility API and bypasses that
# rule, so the target window is actually surfaced. Verified live: the
# AppKit warning disappears and the user-perceived "click does
# nothing" symptom resolves.
#
# ``select w`` is still load-bearing for the multi-window case:
# raising iTerm2 to the OS foreground doesn't pick which iTerm2
# window inside the app is on top — without ``select w`` the target
# pane lives in whatever window happens to be frontmost in iTerm2's
# own z-order, which can leave the user staring at a different
# window with the right pane hidden behind it.
_FOCUS_SCRIPT_TEMPLATE = """\
tell application "System Events"
    set frontmost of process "iTerm2" to true
end tell
tell application "iTerm"
    repeat with w in windows
        repeat with t in tabs of w
            repeat with s in sessions of t
                if tty of s is "{tty}" then
                    select s
                    select t
                    select w
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

        # tty per view
        view_ttys: dict[int, str | None] = {}
        for v in views:
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
        fallback. Selecting the right pane + tab + activating the
        app is one round-trip.

        Falls back to "raise iTerm2 to front" (no pane precision) on
        any tty-match miss: psutil failure, no controlling terminal,
        AppleScript error, or "miss" return. Common when the session
        runs in tmux inside iTerm2 (the claude pid's tty is the tmux
        pty, not in iTerm2's session tree) or when AppleScript
        enumeration permission is denied for iTerm but not for System
        Events. Without this fallback, those clicks were silent
        no-ops; with it, the user at least gets the app raised.
        """
        del siblings
        try:
            import psutil
        except ImportError:
            return focus_host_app(view.session.pid)
        try:
            tty = psutil.Process(view.session.pid).terminal()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            return focus_host_app(view.session.pid)
        if tty and _focus_by_tty(tty):
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


def _focus_by_tty(tty: str) -> bool:
    """Run the focus AppleScript for the given tty. Returns True iff
    osascript completed AND the script reported "ok" (i.e. iTerm2
    found a session matching the tty and selected/activated it)."""
    script = _FOCUS_SCRIPT_TEMPLATE.format(tty=_escape_applescript_string(tty))
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
