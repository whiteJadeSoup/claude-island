"""OS-native notification backends for Stop / TurnComplete events (G2).

Selection happens in ``__main__`` per ``sys.platform``:
  darwin → ``MacOsNotifyBackend`` (osascript display notification)
  win32  → ``WindowsNotifyBackend`` (winrt Toast → QSystemTrayIcon fallback)
  other  → ``NoopNotifyBackend``   (silent; tests + Linux fallback)

All backends satisfy ``NotifyBackend`` Protocol (in protocols.py). The
NotificationDispatcher in the UI layer is OS-agnostic; switching is one
line in __main__.
"""
from __future__ import annotations

from .protocols import NotifyBackend, NotifyKindHint
from .noop import NoopNotifyBackend
from .macos import MacOsNotifyBackend
from .windows import WindowsNotifyBackend

__all__ = [
    "NotifyBackend",
    "NotifyKindHint",
    "NoopNotifyBackend",
    "MacOsNotifyBackend",
    "WindowsNotifyBackend",
]
