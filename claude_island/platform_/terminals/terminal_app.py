"""macOS Terminal.app adapter — tab-level focus via AppleScript.

Sits between iterm2 (priority=100) and generic-mac (priority=0) at
priority=50: claims sessions whose process tree contains Terminal.app
so the user gets tab-precision focus instead of "raise the app and
hope you land on the right tab" that generic-mac provides.

What this adapter buys over generic-mac
---------------------------------------
* FOCUS lands on the *correct* Terminal tab. Without this, multi-tab
  Terminal sessions would all dispatch to "frontmost Terminal" via
  generic-mac, leaving the user with whichever tab Terminal had last
  selected — frequently not the one they clicked on.
* group_id keyed on (window_id, tty) gives stable card identity
  across snapshots, the same way iterm2 keys on (window_id, tab_idx).

What this adapter does NOT do
-----------------------------
* No split-pane handling: Terminal.app doesn't support panes. Each
  tab has exactly one tty / one session, so groups are effectively
  singletons (one card per tab).
* No LAUNCH: generic_mac.launch already spawns Terminal.app via
  AppleScript and works fine — duplicating the path here would be
  ceremony with no benefit. If a future feature wants Terminal-
  specific launch enhancements (custom profile, Plan-L title via
  ``custom title`` of the new tab), it can graduate here.

Permission model
----------------
First call to ``osascript`` triggers macOS's "claude-island wants to
control Terminal" prompt. If declined, all enum / focus calls return
non-zero and the adapter degrades gracefully:
* ``group()`` → singleton groups stamped TerminalApp/APP.
* ``focus()`` → falls back to the same UI-ancestor frontmost path
  generic_mac uses, so the click still raises Terminal even when
  per-tab AppleScript is blocked.

Failure modes seen in the wild
------------------------------
* Terminal.app running with zero windows can hit ``-1712 AppleEvent
  timed out`` on every command (verified). The adapter's 3 s
  timeout + singleton-fallback handles this; the user just loses
  tab precision until Terminal regains responsiveness.
* Newly launched Terminal that hasn't yet shown a window: same
  -1712 path. Same fallback.
"""
from __future__ import annotations

import shlex
import subprocess
from dataclasses import replace
from typing import ClassVar

from claude_island.core.capabilities import (
    Capability,
    FocusGranularity,
    _CapabilityProvider,
    capability,
)
from claude_island.core.models import Session
from claude_island.core.snapshot import SessionGroup, SessionView
from claude_island.platform_.terminals import adapter
from claude_island.platform_.terminals._macos_common import (
    find_ui_app_ancestor,
    frontmost_app,
)
from claude_island.platform_.terminals.iterm2 import _escape_applescript_string
from claude_island.platform_.terminals.protocols import TerminalAdapter

# psutil reports Terminal.app's process name as plain ``Terminal``
# (verified on macOS 14 — the binary is at
# /System/Applications/Utilities/Terminal.app/Contents/MacOS/Terminal,
# basename is what psutil returns). Exact-match (not substring) on
# ``.lower()`` so something accidentally named ``terminal-notifier``
# or ``my-terminal-helper`` doesn't trip the claim.
_TERMINAL_ANCESTOR_NAMES = frozenset({"terminal"})

# How far up the parent chain we walk looking for Terminal.app. Real
# chains top out at ~4 hops (claude → -zsh → login → Terminal).
# Matching iterm2's depth keeps the two adapters consistent.
_MAX_ANCESTOR_DEPTH = 10

# osascript timeout. Generous enough for cold-start AppleScript
# dispatch to a Terminal that's been idle, but bounded so a misbehaving
# Terminal can't freeze the snapshot pipeline. Same value as iterm2.
_OSASCRIPT_TIMEOUT_S = 3.0

# Field separator for the enum output. Pipe is safe — neither window
# ids (integers) nor macOS tty paths contain it.
_ENUM_SEP = "|"

# Enumeration AppleScript. Emits one ``window_id|tty`` line per tab.
# Terminal.app has no per-tab id beyond list position, so we key on
# tty (which IS stable per-tab) instead.
_ENUM_SCRIPT = """\
tell application "Terminal"
    set out to ""
    repeat with w in windows
        set wid to id of w
        repeat with t in tabs of w
            set out to out & wid & "|" & (tty of t) & linefeed
        end repeat
    end repeat
    return out
end tell
"""

# Focus AppleScript: locate the tab whose tty matches, select it,
# raise its window inside Terminal's z-order, then bring Terminal
# itself to the OS front.
#
# ``set frontmost of w to true`` is load-bearing for the multi-window
# case: ``activate`` only raises the app, not a specific window
# inside it. Without setting frontmost on the target window first,
# the user's last-frontmost Terminal window stays on top and the
# selected tab remains hidden.
_FOCUS_SCRIPT_TEMPLATE = """\
tell application "Terminal"
    repeat with w in windows
        repeat with t in tabs of w
            if tty of t is "{tty}" then
                set selected of t to true
                set frontmost of w to true
                activate
                return "ok"
            end if
        end repeat
    end repeat
    return "miss"
end tell
"""


@adapter("terminal-app", priority=50, platform="mac")
class TerminalAppAdapter(_CapabilityProvider):
    """Adapter for claude sessions running inside macOS Terminal.app.

    Tab-precision focus + (window, tty) grouping. Sits at priority=50
    so iterm2 (priority=100) claims iTerm2 sessions first; sessions
    whose ancestry doesn't contain Terminal fall through to
    generic-mac (priority=0).
    """

    name: ClassVar[str] = ""  # set by @adapter
    _priority: int = 0

    # ── can_handle ──────────────────────────────────────────────────────

    def can_handle(self, session: Session) -> bool:
        """True when Terminal.app appears in the session's ancestor
        chain. Walks up to ``_MAX_ANCESTOR_DEPTH`` parents looking for
        a process whose lowercased name is exactly ``terminal``."""
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
                    if p.name().lower() in _TERMINAL_ANCESTOR_NAMES:
                        return True
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    pass
                proc = p
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            return False
        return False

    # ── group ───────────────────────────────────────────────────────────

    def group(self, views: list[SessionView]) -> list[SessionGroup]:
        """Bucket views by (window_id, tty) via tty matching.

        Each Terminal.app tab has exactly one tty (no splits), so the
        bucket is effectively per-tab. Views whose tty isn't in
        Terminal's tree (race / closed mid-tick / process reparented
        away) become singletons stamped with this adapter so click
        retries the AppleScript and falls back gracefully."""
        try:
            import psutil
        except ImportError:
            return _singletons(views, self.name)

        tty_to_coords = self._enumerate_tabs()
        if tty_to_coords is None:
            # AppleScript failed (Terminal.app misbehaving, permission
            # denied, timeout). Each view becomes a singleton; FOCUS
            # at click time still tries the per-tab AppleScript and,
            # on miss, falls back to UI-ancestor frontmost.
            return _singletons(views, self.name)

        view_ttys: dict[int, str | None] = {}
        for v in views:
            try:
                view_ttys[v.pid] = psutil.Process(v.session.pid).terminal()
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                view_ttys[v.pid] = None

        buckets: dict[tuple, list[SessionView]] = {}
        for v in views:
            tty = view_ttys[v.pid]
            coords = tty_to_coords.get(tty) if tty else None
            if coords is None:
                key: tuple = ("singleton", v.pid)
            else:
                key = ("terminal", coords[0], tty)  # (window_id, tty)
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
            if key[0] == "terminal":
                gid = f"terminal-app:{key[1]}:{key[2]}"
                title_hint = ", ".join(
                    sorted({v.project_basename for v in stamped})[:2]
                )
            else:
                gid = f"terminal-app:singleton:{key[1]}"
                title_hint = None
            result.append(SessionGroup(
                group_id=gid, title_hint=title_hint or None,
                adapter_id=self.name, views=tuple(stamped),
            ))
        return result

    # ── FOCUS ────────────────────────────────────────────────────────────

    @capability(Capability.FOCUS)
    def focus(self, view: SessionView, *, siblings: list[int] = ()) -> bool:
        """Select the target tab and raise its window.

        Falls back to UI-ancestor frontmost on every miss path
        (psutil failure, no controlling tty, AppleScript error,
        ``"miss"`` return) — same pattern as iterm2 so a click is
        never silently ignored."""
        del siblings  # Terminal exposes per-tab tty directly; no fallback needed
        try:
            import psutil
        except ImportError:
            return _focus_app_fallback(view)
        try:
            tty = psutil.Process(view.session.pid).terminal()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            return _focus_app_fallback(view)
        if tty and _focus_by_tty(tty):
            return True
        return _focus_app_fallback(view)

    # ── internal: osascript enumeration ─────────────────────────────────

    def _enumerate_tabs(self) -> dict[str, tuple[int]] | None:
        """Run the enumeration AppleScript; return ``{tty:
        (window_id,)}``. The single-element tuple keeps the shape
        symmetric with iterm2's ``(window_id, tab_idx)``.

        Returns None on any AppleScript failure so callers can fall
        back to singleton grouping."""
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

def _parse_enum_output(text: str) -> dict[str, tuple[int]]:
    """Parse ``window_id|tty`` lines into ``{tty: (window_id,)}``.

    Tolerant of trailing whitespace, blank lines, and malformed rows
    (skipped, never raises). Last row wins on duplicate ttys.
    """
    out: dict[str, tuple[int]] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        parts = line.split(_ENUM_SEP)
        if len(parts) != 2:
            continue
        wid_s, tty = parts[0].strip(), parts[1].strip()
        try:
            wid = int(wid_s)
        except ValueError:
            continue
        if not tty:
            continue
        out[tty] = (wid,)
    return out


def _focus_by_tty(tty: str) -> bool:
    """Run the focus AppleScript for the given tty. Returns True iff
    osascript completed AND the script reported "ok" (Terminal.app
    found a tab matching the tty and selected/activated it)."""
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


def _focus_app_fallback(view: SessionView) -> bool:
    """Raise the host UI app to the front when per-tab focus fails.
    Same shape as iterm2's fallback so behaviour is consistent across
    the two macOS terminal adapters."""
    ui_pid = find_ui_app_ancestor(view.session.pid)
    if ui_pid is None:
        return False
    return frontmost_app(ui_pid)


def _singletons(views: list[SessionView], adapter_name: str) -> list[SessionGroup]:
    """Fallback grouping when enumeration fails or psutil is missing.
    One singleton group per view, stamped with the adapter identity
    so dispatch routes back here for FOCUS attempts (which then retry
    the AppleScript or fall through to UI-ancestor frontmost)."""
    result: list[SessionGroup] = []
    for v in views:
        stamped = replace(
            v,
            adapter_id=adapter_name,
            focus_granularity=FocusGranularity.PANE,
            capabilities=TerminalAppAdapter.capabilities,
        )
        result.append(SessionGroup(
            group_id=f"terminal-app:singleton:{stamped.pid}",
            title_hint=None,
            adapter_id=adapter_name,
            views=(stamped,),
        ))
    return result
