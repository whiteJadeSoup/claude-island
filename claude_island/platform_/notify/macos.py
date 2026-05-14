"""macOS notification backend via ``osascript display notification``.

Why osascript and not pyobjc / UNUserNotificationCenter:
  - We don't ship a bundled .app yet (claude-island runs as
    ``python -m claude_island``). UNUserNotificationCenter requires a
    valid app bundle and notification entitlement; without one, calls
    are silently dropped.
  - osascript works on any unbundled Python process — first call may
    prompt "Script Editor wants to send notifications", which the
    user OKs once and the consent persists.
  - Documented v1 limitation: NotificationCenter rate-limits unbundled
    notifications (~1 per 2 s) and the icon shows "Script Editor".
    Acceptable for v1; v2 ships a real app bundle.

Failure modes:
  - osascript not found: impossible on macOS, but treated as a backend
    failure (logged once, returns False)
  - osascript exit non-zero: same
  - osascript timeout (>3 s): killed; logged once
"""
from __future__ import annotations

import logging
import shlex
import subprocess
import threading

from .protocols import NotifyKindHint

log = logging.getLogger(__name__)

# Bounded so a stuck osascript can't freeze the dispatcher. Notifications
# are best-effort; we'd rather lose one than block the Qt main thread.
_OSASCRIPT_TIMEOUT_S = 3.0

# AppleScript ``display notification`` accepts only the soundName
# parameter for sound — limited to system sound names. Default is silent.
# Mirrors macOS NotificationCenter's behavior: WARN/ERROR play; INFO
# stays silent so users don't get blasted on every Stop hook.
_SOUND_BY_KIND: dict[NotifyKindHint, str | None] = {
    NotifyKindHint.INFO: None,
    NotifyKindHint.WARN: "Glass",
    NotifyKindHint.ERROR: "Sosumi",
}


class MacOsNotifyBackend:
    """osascript-based notification poster.

    Single-method backend; safe to call from any thread. State is the
    "have we logged this failure yet" flag (so a permanently-broken
    osascript path doesn't spam logs at every event).
    """

    def __init__(self) -> None:
        self._failure_logged = False
        self._failure_lock = threading.Lock()

    def post(
        self,
        *,
        title: str,
        body: str,
        kind: NotifyKindHint = NotifyKindHint.INFO,
    ) -> bool:
        # AppleScript string literals: escape \" and \\
        title_esc = _applescript_escape(title)
        body_esc = _applescript_escape(body)
        sound = _SOUND_BY_KIND.get(kind)
        if sound:
            script = (
                f'display notification "{body_esc}" '
                f'with title "{title_esc}" sound name "{sound}"'
            )
        else:
            script = (
                f'display notification "{body_esc}" '
                f'with title "{title_esc}"'
            )
        try:
            result = subprocess.run(
                ["/usr/bin/osascript", "-e", script],
                capture_output=True,
                timeout=_OSASCRIPT_TIMEOUT_S,
            )
        except (OSError, subprocess.TimeoutExpired) as e:
            self._log_failure_once("osascript subprocess failed: %r", e)
            return False
        if result.returncode != 0:
            stderr = result.stderr.decode("utf-8", errors="replace").strip()
            self._log_failure_once(
                "osascript exit %d: %s",
                result.returncode, stderr[:200],
            )
            return False
        return True

    # ── internal ────────────────────────────────────────────────────────

    def _log_failure_once(self, msg: str, *args) -> None:
        with self._failure_lock:
            already = self._failure_logged
            self._failure_logged = True
        if not already:
            log.warning("MacOsNotifyBackend: " + msg, *args)


def _applescript_escape(s: str) -> str:
    """Escape a string for safe interpolation into an AppleScript
    double-quoted literal. Backslash and double-quote are the only
    chars that matter inside ``"..."``. Newlines are stripped (would
    end the literal); osascript wouldn't render them anyway.
    """
    return s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")
