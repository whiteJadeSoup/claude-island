"""Windows notification backend with two-tier strategy:

Tier 1 (preferred): WinRT Toast notifications via ``winsdk`` (or
``winrt`` in older docs). Native Windows 10/11 toast UI; supports
icon, sound, urgency.

Tier 2 (fallback): ``QSystemTrayIcon.showMessage`` from PySide6. Always
available since we already depend on Qt; renders as a balloon
notification. Quality lower (no app-specific icon by default) but
always works.

Selection happens at construct time: try to import winsdk; if that
fails or ToastNotifier raises on first call, mark winrt-unusable and
permanently route through tray. The tray surface is bound at
constructor time (caller passes a QSystemTrayIcon) so we don't depend
on PySide6 at module-import in a non-Qt context (tests).

Cross-platform-safety: this module imports nothing OS-specific at
module load. winsdk import is lazy in post(). PySide6 import is the
caller's responsibility (passed via constructor).
"""
from __future__ import annotations

import logging
import threading
from typing import Any

from .protocols import NotifyKindHint

log = logging.getLogger(__name__)


class WindowsNotifyBackend:
    """Posts via WinRT Toast first; falls back to QSystemTrayIcon.

    Construct with an optional ``tray_icon`` (a ``QSystemTrayIcon``
    instance — typed as ``Any`` to keep this module Qt-import-free at
    module load). When tray_icon is None and winrt is unavailable,
    post() returns False (notification dropped, logged once).

    Thread-safe; relies on internal locks for state mutation.
    """

    def __init__(
        self,
        *,
        tray_icon: Any | None = None,
        app_id: str = "claude-island",
    ) -> None:
        self._tray_icon = tray_icon
        self._app_id = app_id
        # WinRT availability is decided lazily on first post() so we
        # don't pay the import cost at boot time. Three states:
        #   None  → unprobed
        #   True  → confirmed working last call
        #   False → permanently disabled (this process)
        self._winrt_usable: bool | None = None
        self._winrt_module = None
        self._lock = threading.Lock()
        self._failure_logged: dict[str, bool] = {"winrt": False, "tray": False}

    def post(
        self,
        *,
        title: str,
        body: str,
        kind: NotifyKindHint = NotifyKindHint.INFO,
    ) -> bool:
        # Try WinRT toast first.
        if self._try_winrt_post(title=title, body=body, kind=kind):
            return True
        # Fall through to tray.
        return self._try_tray_post(title=title, body=body, kind=kind)

    # ── WinRT toast path ────────────────────────────────────────────────

    def _try_winrt_post(self, *, title: str, body: str, kind: NotifyKindHint) -> bool:
        with self._lock:
            usable = self._winrt_usable
        if usable is False:
            return False
        if usable is None and not self._init_winrt():
            # Initialization failed; mark unusable + skip.
            return False
        # By here, _winrt_module is loaded.
        try:
            self._winrt_module.show_toast(
                app_id=self._app_id, title=title, body=body, kind=kind.value,
            )
            return True
        except Exception as e:
            self._log_failure_once("winrt", "winrt show_toast failed: %r", e)
            with self._lock:
                self._winrt_usable = False
            return False

    def _init_winrt(self) -> bool:
        """Probe winrt availability lazily. Returns True iff a usable
        toast helper module is loaded into ``self._winrt_module``."""
        try:
            from . import _winrt_helper  # noqa: F401
            self._winrt_module = _winrt_helper
        except ImportError as e:
            self._log_failure_once(
                "winrt", "winsdk not installed (toast unavailable): %r", e,
            )
            with self._lock:
                self._winrt_usable = False
            return False
        with self._lock:
            self._winrt_usable = True
        return True

    # ── Tray fallback path ─────────────────────────────────────────────

    def _try_tray_post(self, *, title: str, body: str, kind: NotifyKindHint) -> bool:
        if self._tray_icon is None:
            self._log_failure_once(
                "tray",
                "no tray icon provided; notification dropped",
            )
            return False
        try:
            # QSystemTrayIcon.MessageIcon enum values:
            #   NoIcon=0, Information=1, Warning=2, Critical=3
            icon_value = {
                NotifyKindHint.INFO: 1,
                NotifyKindHint.WARN: 2,
                NotifyKindHint.ERROR: 3,
            }[kind]
            self._tray_icon.showMessage(title, body, icon_value, 5000)
            return True
        except Exception as e:
            self._log_failure_once("tray", "tray showMessage failed: %r", e)
            return False

    # ── helpers ────────────────────────────────────────────────────────

    def _log_failure_once(self, key: str, msg: str, *args) -> None:
        with self._lock:
            already = self._failure_logged.get(key, False)
            self._failure_logged[key] = True
        if not already:
            log.warning("WindowsNotifyBackend(%s): " + msg, key, *args)
