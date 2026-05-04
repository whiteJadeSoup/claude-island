from __future__ import annotations

from pathlib import Path
from typing import Callable

from watchdog.events import FileCreatedEvent, FileModifiedEvent, FileSystemEventHandler
from watchdog.observers import Observer


class _SuffixHandler(FileSystemEventHandler):
    """Forwards on_modified / on_created events whose path ends with
    ``suffix`` to a callback. Suffix-based filtering keeps the
    handler dumb (no need to know about specific Claude Code file
    layouts) — callers compose by binding their own suffix and
    callback at watch() time.

    Originally hard-coded to ``.jsonl`` for the projects/ tree;
    parameterised so the same FileWatcher can also drive the
    sessions/<pid>.json watcher (Bug fix: status-change latency).
    """

    def __init__(
        self,
        callback: Callable[[Path], None],
        *,
        suffix: str,
    ) -> None:
        self._callback = callback
        self._suffix = suffix

    def on_modified(self, event: FileModifiedEvent) -> None:  # type: ignore[override]
        if not event.is_directory and str(event.src_path).endswith(self._suffix):
            self._callback(Path(str(event.src_path)))

    def on_created(self, event: FileCreatedEvent) -> None:  # type: ignore[override]
        if not event.is_directory and str(event.src_path).endswith(self._suffix):
            self._callback(Path(str(event.src_path)))


class FileWatcher:
    """Watches one or more directories for new/modified files matching
    a suffix. Wraps watchdog's Observer so callers only see
    start/stop/watch.

    A single FileWatcher can drive multiple ``watch()`` calls — each
    schedules its own handler on the shared Observer thread, with
    independent suffix filters. Used both for projects/ (.jsonl) and
    sessions/ (.json status files).
    """

    def __init__(self) -> None:
        self._observer = Observer()

    def watch(
        self,
        path: Path,
        callback: Callable[[Path], None],
        *,
        suffix: str = ".jsonl",
    ) -> None:
        """Schedule ``callback(file_path)`` for every modify/create
        event under ``path`` (recursive) whose file name ends with
        ``suffix``. Defaults to ``.jsonl`` for backward compat with
        the projects/ caller; pass ``suffix=".json"`` for the
        sessions/ status-file caller."""
        self._observer.schedule(
            _SuffixHandler(callback, suffix=suffix), str(path), recursive=True,
        )

    def start(self) -> None:
        self._observer.start()

    def stop(self) -> None:
        # Idempotent: stop() on a never-started observer raises RuntimeError
        # in watchdog. Only stop if start() was actually called. Required so
        # __main__'s shutdown can be unconditional even when start() was
        # skipped (e.g. mkdir for the projects dir failed).
        if self._observer.is_alive():
            self._observer.stop()
            self._observer.join()
