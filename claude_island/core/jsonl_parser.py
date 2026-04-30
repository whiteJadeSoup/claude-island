from __future__ import annotations

import json
import threading
from datetime import datetime
from pathlib import Path
from typing import Callable

from .events import Event
from .usage_registry import UsageRegistry


class JsonlParser:
    """Incrementally parses Claude Code JSONL session files.

    Only reads bytes beyond the stored offset so repeated calls are O(new bytes).
    Thread-safe: a lock prevents two callers from racing on the same file.

    Expected JSONL line shape (assistant turns):
        {"type": "assistant", "message": {"model": "...", "usage": {...}},
         "timestamp": "2025-01-01T00:00:00.000Z"}
    """

    def __init__(
        self,
        *,
        usage_registry: UsageRegistry,
        claude_projects_dir: Path,
    ) -> None:
        self.activity_updated: Event[tuple[Path, datetime]] = Event()
        self._usage = usage_registry
        self._projects_dir = claude_projects_dir
        self._lock = threading.Lock()

    def parse_file(self, file_path: Path) -> None:
        """Parse new bytes from *file_path*. Safe to call from any thread."""
        with self._lock:
            self._parse_incremental(file_path)

    def backfill_all(self) -> None:
        """Parse every existing JSONL under the projects dir from stored offsets.

        Intended to run once at startup in a background thread; may take tens
        of seconds on large history.
        """
        for jsonl_file in self._projects_dir.rglob("*.jsonl"):
            with self._lock:
                self._parse_incremental(jsonl_file)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _parse_incremental(self, file_path: Path) -> None:
        path_str = str(file_path)
        offset = self._usage.get_offset(path_str)

        try:
            with open(file_path, "rb") as fh:
                fh.seek(offset)
                chunk = fh.read()
                new_offset = fh.tell()
        except OSError:
            return

        if not chunk:
            return

        # session_uuid = filename stem; project_path = parent dir name
        session_uuid = file_path.stem
        project_path = file_path.parent.name  # hashed project id

        last_activity: datetime | None = None

        for raw_line in chunk.split(b"\n"):
            raw_line = raw_line.strip()
            if not raw_line:
                continue
            try:
                entry: dict = json.loads(raw_line)
            except json.JSONDecodeError:
                continue

            ts = _parse_ts(entry)
            usage, model = _extract_usage(entry)

            if usage and ts:
                self._usage.record(
                    timestamp=ts,
                    project_path=project_path,
                    session_uuid=session_uuid,
                    model=model,
                    input_tokens=usage.get("input_tokens", 0),
                    output_tokens=usage.get("output_tokens", 0),
                    cache_creation_tokens=usage.get("cache_creation_input_tokens", 0),
                    cache_read_tokens=usage.get("cache_read_input_tokens", 0),
                )
                if last_activity is None or ts > last_activity:
                    last_activity = ts

        self._usage.set_offset(path_str, new_offset)

        if last_activity is not None:
            self.activity_updated.emit((file_path, last_activity))


def _parse_ts(entry: dict) -> datetime | None:
    ts_str = entry.get("timestamp")
    if not isinstance(ts_str, str):
        return None
    try:
        return datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
    except ValueError:
        return None


def _extract_usage(entry: dict) -> tuple[dict, str]:
    """Return (usage_dict, model_name). Falls back to empty dict / empty string."""
    message = entry.get("message") or {}

    # usage may be at top level or nested inside message
    usage: dict = entry.get("usage") or message.get("usage") or {}
    model: str = entry.get("model") or message.get("model") or ""

    return usage, model
