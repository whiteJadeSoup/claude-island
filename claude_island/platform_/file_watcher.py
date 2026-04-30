from __future__ import annotations

from pathlib import Path
from typing import Callable

from watchdog.events import FileCreatedEvent, FileModifiedEvent, FileSystemEventHandler
from watchdog.observers import Observer


class _JssonlHandler(FileSystemEventHandler):
    def __init__(self, callback: Callable[[Path], None]) -> None:
        self._callback = callback

    def on_modified(self, event: FileModifiedEvent) -> None:  # type: ignore[override]
        if not event.is_directory and str(event.src_path).endswith(".jsonl"):
            self._callback(Path(str(event.src_path)))

    def on_created(self, event: FileCreatedEvent) -> None:  # type: ignore[override]
        if not event.is_directory and str(event.src_path).endswith(".jsonl"):
            self._callback(Path(str(event.src_path)))


class FileWatcher:
    """Watches the Claude projects directory for new/modified JSONL session files.

    Wraps watchdog's Observer so callers only see start/stop/watch.
    """

    def __init__(self) -> None:
        self._observer = Observer()

    def watch(self, path: Path, callback: Callable[[Path], None]) -> None:
        self._observer.schedule(_JssonlHandler(callback), str(path), recursive=True)

    def start(self) -> None:
        self._observer.start()

    def stop(self) -> None:
        self._observer.stop()
        self._observer.join()
