from __future__ import annotations

from typing import Callable, Generic, TypeVar

T = TypeVar("T")


class Event(Generic[T]):
    """Single-emitter / multi-subscriber observer.

    Threading contract:
    - ``subscribe`` and the returned unsubscribe callable are NOT thread-safe.
      They mutate ``_handlers`` without a lock, relying only on CPython's
      GIL for individual list operations. Register all subscribers at app
      startup, before any background thread starts emitting; do not
      subscribe / unsubscribe at runtime.
    - ``emit`` is safe from any thread: it takes a snapshot via
      ``list(self._handlers)`` (a single GIL-protected slice copy) and then
      invokes handlers on the caller's thread. Handlers must do their own
      thread-marshalling if they touch UI state — see ``ui.qt_bridge.QtBridge``.

    Handlers are invoked in subscribe order. An exception from one handler
    propagates and skips the rest of the handlers in that emit; subscribers
    that need isolation should wrap their slot body in try/except.
    """

    def __init__(self) -> None:
        self._handlers: list[Callable[[T], None]] = []

    def subscribe(self, handler: Callable[[T], None]) -> Callable[[], None]:
        """Register a handler. Returns an unsubscribe callable.

        Call this only at app startup, before any thread that may call
        ``emit`` begins running. Runtime subscription has no lock and can
        race with concurrent emits / other subscribes.
        """
        self._handlers.append(handler)
        return lambda: self._handlers.remove(handler)

    def emit(self, payload: T) -> None:
        """Synchronously call every handler with ``payload``.

        Safe to call from any thread. Handlers run on the caller's thread.
        """
        for h in list(self._handlers):
            h(payload)
