from __future__ import annotations

from typing import Callable, Generic, TypeVar

from PySide6.QtCore import QObject, Qt, Signal

from claude_island.core.events import Event

T = TypeVar("T")


class QtBridge(QObject, Generic[T]):
    """Sole file allowed to import both core Events and PySide6.

    Marshals core Event payloads from arbitrary threads into the Qt main thread
    via QueuedConnection. Multiple slots can be connected to the same bridge.

    Usage:
        bridge = QtBridge(registry.sessions_changed)
        bridge.connect_to(controller.on_sessions_updated)
        bridge.connect_to(expanded_window.refresh_sessions)
    """

    forwarded = Signal(object)

    def __init__(self, source: Event[T]) -> None:
        super().__init__()
        # source.emit() may be called from any thread; Signal.emit() is
        # thread-safe in Qt — the QueuedConnection below does the marshalling.
        source.subscribe(self.forwarded.emit)

    def connect_to(self, slot: Callable[[T], None]) -> None:
        self.forwarded.connect(slot, Qt.ConnectionType.QueuedConnection)
