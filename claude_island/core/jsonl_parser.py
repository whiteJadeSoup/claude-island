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

    activity_updated payload: (project_hash, timestamp).
    project_hash is the parent directory name of the JSONL file (Claude Code's
    per-project encoding of the cwd; see core.models.project_hash). This lets
    SessionRegistry join activity to scanned sessions by recomputing the hash
    of each session's cwd.
    """

    def __init__(
        self,
        *,
        usage_registry: UsageRegistry,
        claude_projects_dir: Path,
    ) -> None:
        self.activity_updated: Event[tuple[str, datetime]] = Event()
        self._usage = usage_registry
        self._projects_dir = claude_projects_dir
        self._lock = threading.Lock()
        # Cooperative cancellation for backfill_all. Set by request_stop()
        # at app shutdown so the daemon thread bails out before we close
        # the SQLite connection — otherwise record_many / set_offset would
        # raise sqlite3.ProgrammingError on a closed connection.
        self._stop_event = threading.Event()

    def request_stop(self) -> None:
        """Signal backfill_all to abort at the next file boundary.

        Idempotent. Used by the shutdown sequence in __main__.py: call this
        before joining the backfill thread and closing the usage registry.
        """
        self._stop_event.set()

    def parse_file(self, file_path: Path) -> None:
        """Parse new bytes from *file_path*. Safe to call from any thread."""
        with self._lock:
            self._parse_incremental(file_path)

    def backfill_all(self) -> None:
        """Parse every existing JSONL under the projects dir from stored offsets.

        Intended to run once at startup in a background thread; may take tens
        of seconds on large history. Checks the stop event between files so
        shutdown doesn't have to wait for the entire history to finish.
        """
        for jsonl_file in self._projects_dir.rglob("*.jsonl"):
            if self._stop_event.is_set():
                return
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

        # Tail-follow pattern: only commit offsets at fully-terminated line
        # boundaries. If the chunk ends mid-line (writer is mid-flush), the
        # trailing fragment must be left in the file — committing past it
        # would silently lose the rest of the line on the next read.
        parts = chunk.split(b"\n")
        if chunk.endswith(b"\n"):
            complete_lines, tail_len = parts, 0
        else:
            complete_lines, tail_len = parts[:-1], len(parts[-1])

        # Accumulate this chunk's usage rows and emit a single totals_changed
        # at the end (via record_many). Per-row record() in a tight loop
        # would flood the UI with N redundant SELECT-and-redraw passes,
        # which is observable during backfill_all over a large history.
        batch: list[dict] = []
        last_activity: datetime | None = None

        for raw_line in complete_lines:
            raw_line = raw_line.strip()
            if not raw_line:
                continue
            # Decode explicitly as UTF-8 (Claude Code writes UTF-8). Passing raw
            # bytes to json.loads triggers heuristic encoding detection, which
            # picks utf-32-be when a line happens to start with \x00\x00 and
            # then crashes the thread with UnicodeDecodeError on later bytes.
            try:
                entry: dict = json.loads(raw_line.decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue

            ts = _parse_ts(entry)
            usage, model = _extract_usage(entry)

            if usage and ts:
                batch.append({
                    "timestamp": ts,
                    "project_path": project_path,
                    "session_uuid": session_uuid,
                    "model": model,
                    "input_tokens": usage.get("input_tokens", 0),
                    "output_tokens": usage.get("output_tokens", 0),
                    "cache_creation_tokens": usage.get("cache_creation_input_tokens", 0),
                    "cache_read_tokens": usage.get("cache_read_input_tokens", 0),
                })
                if last_activity is None or ts > last_activity:
                    last_activity = ts

        if batch:
            self._usage.record_many(batch)
        self._usage.set_offset(path_str, new_offset - tail_len)

        if last_activity is not None:
            self.activity_updated.emit((project_path, last_activity))


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
