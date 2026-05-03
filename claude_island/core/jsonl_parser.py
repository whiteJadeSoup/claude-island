from __future__ import annotations

import concurrent.futures
import json
import re
import threading
from datetime import datetime, timezone
from pathlib import Path

from reactivex.subject import Subject

from .models import UsageRecord
from .usage_registry import UsageRegistry

# Pre-compiled: trim fractional seconds to ≤6 digits so datetime.fromisoformat
# accepts the string. Claude Code today uses ms (3 digits), but a future
# precision bump to ns would otherwise silently drop every line.
_FRACTIONAL_OVERFLOW = re.compile(r"(\.\d{6})\d+")


class JsonlParser:
    """Incrementally parses Claude Code JSONL session files.

    Only reads bytes beyond the per-process in-memory offset so repeated
    calls are O(new bytes). Thread-safe: a lock prevents two callers
    from racing on the same file.

    Expected JSONL line shape (assistant turns):
        {"type": "assistant", "message": {"model": "...", "usage": {...}},
         "timestamp": "2025-01-01T00:00:00.000Z"}

    activity_updated payload: (project_hash, timestamp).
    project_hash is the parent directory name of the JSONL file (Claude Code's
    per-project encoding of the cwd; see core.models.project_hash). This lets
    SessionRegistry join activity to scanned sessions by recomputing the hash
    of each session's cwd.

    Offset tracking (post-DB-removal): byte offsets live in a process-
    local ``_offsets`` dict. They reset to 0 on every restart, so
    ``backfill_all`` re-parses every transcript at startup. At the
    user's data scale (~10⁵ rows / ~5 MB total) this is sub-second
    and runs in a daemon thread anyway, so the UI is responsive
    immediately while history fills in.
    """

    def __init__(
        self,
        *,
        usage_registry: UsageRegistry,
        claude_projects_dir: Path,
    ) -> None:
        # Reactivex Subject (was Event[T] pre-Phase G2). API-equivalent:
        # ``subscribe(cb)`` registers, ``on_next(payload)`` emits.
        # Synchronously dispatches to subscribers on the calling thread.
        self.activity_updated: Subject[tuple[str, datetime]] = Subject()
        self._usage = usage_registry
        self._projects_dir = claude_projects_dir
        # Per-file locks replace the old single global lock so backfill
        # can parse multiple files concurrently via ThreadPoolExecutor.
        # The dict itself is protected by _file_locks_lock; each value is
        # a per-path_str Lock that serialises parse_file (watchdog) and
        # backfill workers on the same file.
        self._file_locks: dict[str, threading.Lock] = {}
        self._file_locks_lock = threading.Lock()
        self._offsets: dict[str, int] = {}
        self._session_meta: dict[str, dict] = {}
        self._stop_event = threading.Event()

    def get_session_metadata(self, session_uuid: str) -> dict:
        """Snapshot of per-session metadata extracted from the JSONL.

        Returns a dict with optional keys ``ai_title`` / ``last_prompt`` /
        ``git_branch`` / ``version``. Empty dict when the session UUID
        is unknown (transcript not yet parsed). Returned dict is a
        shallow copy so callers can read freely without locking.
        """
        return dict(self._session_meta.get(session_uuid, {}))

    def request_stop(self) -> None:
        """Signal backfill_all to abort at the next file boundary.

        Idempotent. Used by the shutdown sequence in __main__.py.
        """
        self._stop_event.set()
        executor = getattr(self, "_backfill_executor", None)
        if executor is not None:
            executor.shutdown(wait=False, cancel_futures=True)
            self._backfill_executor = None

    def known_session_uuids(self) -> set[str]:
        """Return every session_uuid the parser knows about — i.e.
        every transcript filename stem under the projects dir.

        Used by the periodic ``session_names`` cleanup so renamed-and-
        then-deleted sessions don't leave stale override entries
        accumulating in ``~/.claude-island/session_names.json``.

        Implemented as a fresh ``rglob`` rather than reading
        ``self._session_meta`` so the answer reflects what's on disk
        right now (the meta dict only sees files the parser actually
        touched). Cheap — same scan ``backfill_all`` already does.
        Errors during enumeration return an empty set so the caller
        skips its mutation, which is the safe default for a gc.
        """
        try:
            return {p.stem for p in self._projects_dir.rglob("*.jsonl")}
        except OSError:
            return set()

    def _get_file_lock(self, path_str: str) -> threading.Lock:
        """Return (or create) the per-file lock for *path_str*.

        The dict of locks is itself protected — callers can safely invoke
        this from any thread without a surrounding lock.
        """
        with self._file_locks_lock:
            lock = self._file_locks.get(path_str)
            if lock is None:
                lock = threading.Lock()
                self._file_locks[path_str] = lock
            return lock

    def parse_file(self, file_path: Path) -> None:
        """Parse new bytes from *file_path*. Safe to call from any thread."""
        with self._get_file_lock(str(file_path)):
            self._parse_incremental(file_path)

    def backfill_all(self) -> None:
        """Parse every existing JSONL under the projects dir from offset 0.

        Intended to run once at startup in a background thread. Checks
        the stop event between files so shutdown doesn't have to wait
        for the entire history to finish.
        """
        for jsonl_file in self._projects_dir.rglob("*.jsonl"):
            if self._stop_event.is_set():
                return
            with self._get_file_lock(str(jsonl_file)):
                self._parse_incremental(jsonl_file)

    def start_backfill_pool(self, max_workers: int = 4) -> None:
        """Launch a thread-pool backfill that parses all JSONL files in
        parallel. Returns immediately — workers run on daemon threads.

        Files are dispatched to *max_workers* threads. Each thread acquires
        the per-file lock before parsing, so concurrent workers processing
        different files never contend; only a watchdog event on the same
        file will serialise (correctly) with the backfill worker.

        The pool's work finishes silently in the background. The caller
        (``__main__.py``) can optionally ``join()`` the executor if it
        needs a clean shutdown, but since all threads are daemon threads
        the process exits without waiting.
        """
        from concurrent.futures import ThreadPoolExecutor
        files = list(self._projects_dir.rglob("*.jsonl"))
        if not files:
            return
        executor = ThreadPoolExecutor(max_workers=max_workers)
        for f in files:
            if self._stop_event.is_set():
                break
            executor.submit(self._parse_file_backfill, f)
        # Hold a reference so the executor isn't GC'd before workers finish.
        self._backfill_executor: ThreadPoolExecutor | None = executor

    def _parse_file_backfill(self, file_path: Path) -> None:
        """Per-file backfill worker — called from the thread pool."""
        if self._stop_event.is_set():
            return
        with self._get_file_lock(str(file_path)):
            self._parse_incremental(file_path)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _parse_incremental(self, file_path: Path) -> None:
        path_str = str(file_path)
        stored_offset = self._offsets.get(path_str, 0)

        try:
            # Detect truncation / rotation before seeking. seek(N) on a
            # file smaller than N succeeds silently but read() returns
            # b'' and tell() returns N — we'd then commit the same
            # stale offset and all future writes (until the file grows
            # past N) would be lost. Reset to 0 if the file shrank.
            file_size = file_path.stat().st_size
            offset = 0 if file_size < stored_offset else stored_offset

            with open(file_path, "rb") as fh:
                fh.seek(offset)
                chunk = fh.read()
                new_offset = fh.tell()
        except OSError:
            return

        # Persist the truncation reset even if the new file has nothing
        # to parse yet, so the next call doesn't re-skip past the truncation.
        if not chunk and offset != stored_offset:
            self._offsets[path_str] = offset
            return

        if not chunk:
            return

        # session_uuid = filename stem; project_path = parent dir name
        session_uuid = file_path.stem
        project_path = file_path.parent.name  # hashed project id

        # Tail-follow pattern: only commit offsets at fully-terminated
        # line boundaries. If the chunk ends mid-line (writer is
        # mid-flush) the trailing fragment must be left in the file —
        # advancing past it would silently drop the rest of the line on
        # the next read.
        parts = chunk.split(b"\n")
        if chunk.endswith(b"\n"):
            complete_lines, tail_len = parts, 0
        else:
            complete_lines, tail_len = parts[:-1], len(parts[-1])

        batch: list[UsageRecord] = []
        last_activity: datetime | None = None
        meta = self._session_meta.setdefault(session_uuid, {})
        # Track the earliest timestamp so the detail popup can show the
        # session start time even when ~/.claude/sessions/<pid>.json is
        # absent (MiniMax sessions don't write that file).
        earliest_ts: datetime | None = None

        for raw_line in complete_lines:
            raw_line = raw_line.strip()
            if not raw_line:
                continue
            try:
                entry: dict = json.loads(raw_line.decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue

            # Capture per-session metadata before we filter by usage —
            # ai-title / last-prompt / permission-mode rows have no
            # usage but carry the session info we want for the tooltip.
            row_type = entry.get("type")
            if row_type == "ai-title":
                t = entry.get("aiTitle")
                if isinstance(t, str) and t.strip():
                    meta["ai_title"] = t.strip()
            elif row_type == "last-prompt":
                p = entry.get("lastPrompt")
                if isinstance(p, str) and p.strip():
                    meta["last_prompt"] = p.strip()
            # Branch + version live on most rows; latest-wins is fine
            # since they don't change within one session except for
            # the rare git-checkout-mid-session case (then the latest
            # write reflects the user's current branch).
            br = entry.get("gitBranch")
            if isinstance(br, str) and br:
                meta["git_branch"] = br
            ver = entry.get("version")
            if isinstance(ver, str) and ver:
                meta["version"] = ver
            # turn / sidechain counts live in UsageRegistry — there
            # they're computed from unique message.id, dedupping the
            # N-rows-per-response duplication we already handle there.

            ts = _parse_ts(entry)
            usage, model = _extract_usage(entry)

            # Track the earliest timestamp across all entries in this file.
            # The first entry of a transcript is always a "type": "user" row
            # with the session start time, so min(timestamps) ≈ session start.
            if ts is not None and (earliest_ts is None or ts < earliest_ts):
                earliest_ts = ts

            if usage and ts:
                # Pull the API ``message.id`` for dedup. Claude Code
                # writes the same response usage across N JSONL rows
                # (one per content block); UsageRegistry uses this id
                # to count the response exactly once. None is OK —
                # legacy rows without an id bypass dedup.
                msg_block = entry.get("message") or {}
                message_id = msg_block.get("id") if isinstance(msg_block, dict) else None
                batch.append(UsageRecord(
                    timestamp=ts,
                    project_path=project_path,
                    session_uuid=session_uuid,
                    model=model,
                    input_tokens=usage.get("input_tokens", 0),
                    output_tokens=usage.get("output_tokens", 0),
                    cache_creation_tokens=usage.get("cache_creation_input_tokens", 0),
                    cache_read_tokens=usage.get("cache_read_input_tokens", 0),
                    message_id=message_id if isinstance(message_id, str) else None,
                    is_sidechain=bool(entry.get("isSidechain")),
                ))
                if last_activity is None or ts > last_activity:
                    last_activity = ts

        # Persist the earliest timestamp so the detail popup can show
        # "Created" even when ~/.claude/sessions/<pid>.json is absent
        # (MiniMax sessions don't write that file). latest-wins for other
        # fields; earliest-wins for started_at.
        if earliest_ts is not None:
            existing = meta.get("started_at")
            if existing is None or earliest_ts < existing:
                meta["started_at"] = earliest_ts

        # Advance offset and emit records. Order is important: advance
        # offset BEFORE the registry call so a watchdog event firing
        # again for this same file (a re-fire on the same write) sees
        # the new offset and reads zero bytes. The registry call may
        # itself synchronously fan out to subscribers (the UI bridge),
        # which we don't want to do twice.
        self._offsets[path_str] = new_offset - tail_len
        self._usage.record_many(batch)

        if last_activity is not None:
            self.activity_updated.on_next((project_path, last_activity))


def _parse_ts(entry: dict) -> datetime | None:
    """Parse a Claude Code JSONL timestamp into a UTC tz-aware datetime.

    Two latent traps the normalisation closes:

    1. fromisoformat rejects 7+ fractional digits ('Invalid isoformat
       string'). Truncate to 6 — the same precision Python natively stores.

    2. A naive datetime (no 'Z', no offset) compared to tz-aware UTC
       raises TypeError. Force every output to be UTC tz-aware so the
       comparison in UsageRegistry filters works.
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
