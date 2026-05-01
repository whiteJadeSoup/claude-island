from __future__ import annotations

import json
import re
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from .events import Event
from .usage_registry import UsageRegistry

# Pre-compiled: trim fractional seconds to ≤6 digits so datetime.fromisoformat
# accepts the string. Claude Code today uses ms (3 digits), but a future
# precision bump to ns would otherwise silently drop every line.
_FRACTIONAL_OVERFLOW = re.compile(r"(\.\d{6})\d+")


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
        stored_offset = self._usage.get_offset(path_str)

        try:
            # Detect truncation / rotation before seeking. seek(N) on a file
            # smaller than N succeeds silently but read() returns b'' and
            # tell() returns N — we'd then commit the same stale offset and
            # all future writes (until the file grows past N) would be lost.
            # Reset to 0 if the file shrank, treating it as a fresh file.
            file_size = file_path.stat().st_size
            offset = 0 if file_size < stored_offset else stored_offset

            with open(file_path, "rb") as fh:
                fh.seek(offset)
                chunk = fh.read()
                new_offset = fh.tell()
        except OSError:
            return

        # Persist the truncation reset even if the new file has nothing to
        # parse yet, so the stored offset reflects reality.
        if not chunk and offset != stored_offset:
            self._usage.set_offset(path_str, offset)
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

        # Atomic per-file: records + offset commit together. If the process
        # is killed mid-file, the whole transaction rolls back — no chance
        # of committed rows with an unadvanced offset (which would re-parse
        # them on next start, double-counting tokens).
        self._usage.record_many(
            batch,
            advance_offset=(path_str, new_offset - tail_len),
        )

        if last_activity is not None:
            self.activity_updated.emit((project_path, last_activity))


def _parse_ts(entry: dict) -> datetime | None:
    """Parse a Claude Code JSONL timestamp into a UTC tz-aware datetime.

    Two latent traps the normalisation closes:

    1. fromisoformat rejects 7+ fractional digits ('Invalid isoformat
       string'). Truncate to 6 — the same precision Python natively stores.

    2. A naive datetime (no 'Z', no offset) round-tripped through
       .isoformat() produces a string with no '+00:00' suffix. The DB
       stores it as TEXT and get_totals does a string >= comparison
       against `datetime.now(utc).isoformat()` which always has the
       suffix. Lex order: '...12:34:56' < '...12:34:56+00:00' (the
       suffix character '+' comes after EOS), so naive-stamped rows
       fall outside their time window and silently drop out of totals.
       Force every output to be UTC tz-aware so the stored string
       always carries the offset.
    """
    ts_str = entry.get("timestamp")
    if not isinstance(ts_str, str):
        return None
    normalised = _FRACTIONAL_OVERFLOW.sub(r"\1", ts_str.replace("Z", "+00:00"))
    try:
        ts = datetime.fromisoformat(normalised)
    except ValueError:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(timezone.utc)


def _extract_usage(entry: dict) -> tuple[dict, str]:
    """Return (usage_dict, model_name). Falls back to empty dict / empty string."""
    message = entry.get("message") or {}

    # usage may be at top level or nested inside message
    usage: dict = entry.get("usage") or message.get("usage") or {}
    model: str = entry.get("model") or message.get("model") or ""

    return usage, model
