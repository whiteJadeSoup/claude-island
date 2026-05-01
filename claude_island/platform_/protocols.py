from __future__ import annotations

from typing import Protocol, runtime_checkable

from claude_island.core.models import Session


@runtime_checkable
class ProcessScannerProtocol(Protocol):
    def scan(self) -> list[Session]: ...
