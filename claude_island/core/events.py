from __future__ import annotations

from typing import Callable, Generic, TypeVar

T = TypeVar("T")


class Event(Generic[T]):
    """Thread-safe observer. Handlers are called in subscribe order.

    Returns an unsubscribe callable so callers can clean up without
    keeping a reference to the handler itself.
    """

    def __init__(self) -> None:
        self._handlers: list[Callable[[T], None]] = []

    def subscribe(self, handler: Callable[[T], None]) -> Callable[[], None]:
        self._handlers.append(handler)
        return lambda: self._handlers.remove(handler)

    def emit(self, payload: T) -> None:
        for h in list(self._handlers):
            h(payload)
