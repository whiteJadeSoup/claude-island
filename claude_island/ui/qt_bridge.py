from __future__ import annotations

from typing import Callable, Generic, TypeVar

from PySide6.QtCore import QObject, Qt, Signal
from reactivex import Observable

T = TypeVar("T")


class QtBridge(QObject, Generic[T]):
    """Marshals reactivex Subject / Observable payloads from any thread
    onto the Qt main thread via QueuedConnection.

    Phase G2 transitional: only the controller wire still uses this.
    The WorldSnapshot pipeline uses WorldMarshaler instead (different
    Qt-Signal mechanism, same end effect). Phase G2.4 will replace
    the controller wire with a direct reactivex subscribe (Qt Signals
    are thread-safe so no marshaling needed) and delete this file.

    Usage:
        bridge = QtBridge(registry.sessions_changed)
        bridge.connect_to(controller.on_sessions_updated)
    """

    forwarded = Signal(object)

    def __init__(self, source: Observable[T]) -> None:
        super().__init__()
        # source.on_next(value) may be called from any thread; Signal.emit
        # is thread-safe — the QueuedConnection below does the marshalling.
        source.subscribe(self.forwarded.emit)

    def connect_to(self, slot: Callable[[T], None]) -> None:
        self.forwarded.connect(slot, Qt.ConnectionType.QueuedConnection)
