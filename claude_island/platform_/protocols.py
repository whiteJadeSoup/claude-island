from __future__ import annotations

from pathlib import Path
from typing import Callable, Protocol, runtime_checkable

from claude_island.core.models import Session


@runtime_checkable
class ProcessScannerProtocol(Protocol):
    def scan(self) -> list[Session]: ...


@runtime_checkable
class WindowActivatorProtocol(Protocol):
    def activate(self, session: Session) -> bool: ...


@runtime_checkable
class FileWatcherProtocol(Protocol):
    def watch(self, path: Path, callback: Callable[[Path], None]) -> None: ...
    def start(self) -> None: ...
    def stop(self) -> None: ...
