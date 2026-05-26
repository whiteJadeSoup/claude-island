from __future__ import annotations

import concurrent.futures
import json
import re
import threading
from datetime import datetime, timezone
from pathlib import Path

from .models import UsageRecord
from .usage_registry import UsageRegistry

# Pre-compiled: trim fractional seconds to ≤6 digits so datetime.fromisoformat
# accepts the string. Claude Code today uses ms (3 digits), but a future
# precision bump to ns would otherwise silently drop every line.
_FRACTIONAL_OVERFLOW = re.compile(r"(\.\d{6})\d+")


# ─────────────────────────────────────────────────────────────────────
# Subagent path classification — purely structural, no file I/O.
#
# Claude Code's storage convention (sessionStorage.ts):
#
#   <projects_dir>/<slug>/<sessionId>.jsonl                   ← main session
#   <projects_dir>/<slug>/<sessionId>/                        ← session-derived dir
#       └ subagents/agent-<aid>.jsonl                         ← subagent
#       └ subagents/workflows/<runId>/agent-<aid>.jsonl       ← workflow subagent
#       └ subagents/agent-<aid>.meta.json                     ← sidecar (.meta, ignored)
#       └ remote-agents/remote-agent-<tid>.meta.json          ← remote (.meta, ignored)
#
# Two helpers below are the single source of truth for "is this a
# subagent transcript?" and "what's its parent session?". The parent
# uuid is always the second segment (parts[1]) — both for direct
# subagents (parts ≥ 4) and workflow subagents (parts ≥ 6) — because
# the <sessionId> directory anchors everything below it.
# ─────────────────────────────────────────────────────────────────────

def _project_slug(file_path: Path, projects_dir: Path) -> str | None:
    """Return the top-level slug (Claude Code's project hash) for a
    transcript, regardless of how deeply it's nested.

    Returns None if ``file_path`` isn't under ``projects_dir`` — in
    that case the parser bails out rather than guess. rglob shouldn't
    produce such files in practice, but a misconfigured projects_dir
    or a symlink leak would, and silent misattribution of cost is
    worse than a no-op."""
    try:
        rel = file_path.relative_to(projects_dir)
    except ValueError:
        return None
    parts = rel.parts
    if not parts:
        return None
    return parts[0]


def _subagent_parent_uuid(file_path: Path, projects_dir: Path) -> str | None:
    """Parent session uuid if ``file_path`` is a subagent transcript;
    None if it's a main session (or anything else we don't recognise).

    Detection is path-only: the third segment (parts[2]) must be the
    literal ``subagents`` — the directory Claude Code uses to anchor
    every subagent file regardless of workflow nesting depth. The
    second segment (parts[1]) is then the parent ``<sessionId>``."""
    try:
        rel = file_path.relative_to(projects_dir)
    except ValueError:
        return None
    parts = rel.parts
    # main session: <slug>/<uuid>.jsonl  →  len 2
    # subagent:     <slug>/<sid>/subagents/agent-<aid>.jsonl  →  len ≥ 4
    if len(parts) >= 4 and parts[2] == "subagents":
        return parts[1]
    return None


def _is_main_session_file(file_path: Path, projects_dir: Path) -> bool:
    """True iff ``file_path`` is a main-session transcript (the kind
    addressable as a session in HISTORY).

    Equivalent to ``_subagent_parent_uuid(...) is None`` AND
    ``len(rel.parts) == 2``. The double-check rejects any future
    `<slug>/<sid>/foo.jsonl` placement that isn't a subagent but
    also isn't a top-level session — e.g. an experimental Claude Code
    feature dumping derived data alongside subagents/."""
    try:
        rel = file_path.relative_to(projects_dir)
    except ValueError:
        return False
    return len(rel.parts) == 2


class JsonlParser:
    """Incrementally parses Claude Code JSONL session files.

    Only reads bytes beyond the per-process in-memory offset so repeated
    calls are O(new bytes). Thread-safe: a lock prevents two callers
    from racing on the same file.

    Expected JSONL line shape (assistant turns):
        {"type": "assistant", "message": {"model": "...", "usage": {...}},
         "timestamp": "2025-01-01T00:00:00.000Z"}

    Per-session ``last_activity`` is maintained on ``_session_meta[uuid]``
    (uuid-keyed, so two sessions sharing a cwd never alias). UI consumers
    pull it via ``compose_session_view`` -> ``get_session_metadata(uuid)``;
    snapshotter wakes happen through ``UsageRegistry.totals_changed``
    after ``record_many`` runs, so there's no separate activity event.

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

        Subagent transcripts (``<sid>/subagents/agent-*.jsonl`` and
        their ``workflows/<runId>/`` children) are filtered — they are
        not addressable as standalone sessions. Without this filter
        every subagent shows up in HISTORY as a broken pseudo-session
        and any cleanup keyed on the returned set would mis-classify
        ``agent-<aid>`` as a "live" uuid.
        """
        try:
            return {
                p.stem for p in self._projects_dir.rglob("*.jsonl")
                if _is_main_session_file(p, self._projects_dir)
            }
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

        Both main-session and subagent transcripts are parsed — subagent
        cost has to flow through ``_parse_incremental`` so it gets
        recorded against the parent session. The "is this a subagent?"
        decision happens inside the parser, not here at enumeration.
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
        # Time the full parse path so future-us can see boot replay vs
        # tail-watching cost separately. Counters fire only when we
        # actually consumed bytes (early returns below don't count).
        import time as _time
        from claude_island.core.metrics import metrics as _metrics
        _t0 = _time.perf_counter()

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

        _metrics.incr("jsonl.file.parsed")
        _metrics.incr("jsonl.bytes.parsed", n=len(chunk))

        # Path-level identity. For a main session the layout is
        #   <projects_dir>/<slug>/<sessionId>.jsonl
        # so file.stem is the uuid and file.parent.name is the slug.
        # For a subagent it's
        #   <projects_dir>/<slug>/<parent-sid>/subagents/[workflows/<runId>/]agent-<aid>.jsonl
        # — we route its cost/activity to the parent's uuid so it rolls
        # up into the parent session's totals (and the sidechain count).
        # ``project_path`` is always the top-level slug regardless of
        # nesting depth; without this fix subagents would emit activity
        # under "subagents" / "<runId>" and SessionRegistry's per-project
        # join would drop the bump on the floor.
        parent_uuid = _subagent_parent_uuid(file_path, self._projects_dir)
        is_subagent = parent_uuid is not None
        slug = _project_slug(file_path, self._projects_dir)
        if slug is None:
            # File outside our projects_dir — defensive, rglob shouldn't
            # produce these, but a misconfigured projects_dir could.
            return
        if is_subagent:
            session_uuid = parent_uuid          # type: ignore[assignment]
        else:
            session_uuid = file_path.stem
        project_path = slug

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
        # Subagents get a throwaway local meta — any ai-title / git-branch
        # / cwd written by a subagent's transcript MUST NOT pollute the
        # parent's _session_meta entry. The parent owns its own metadata.
        # Using a discardable dict (rather than `if meta is not None`
        # guards on every write below) keeps the existing parse loop
        # untouched — writes just don't survive past this function.
        if is_subagent:
            meta: dict = {}
        else:
            meta = self._session_meta.setdefault(session_uuid, {})
        # earliest_ts → started_at (first user prompt time)
        # latest_ts   → last_activity (used by DormantSessionSource to sort
        #               offline sessions by recency without re-stat'ing files)
        earliest_ts: datetime | None = None
        latest_ts: datetime | None = None

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
            elif row_type == "permission-mode":
                # Dedicated permission-mode flip row written by Claude Code
                # when the user toggles modes mid-session (Shift+Tab).
                pm = entry.get("permissionMode")
                if isinstance(pm, str) and pm:
                    meta["permission_mode"] = pm
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
            # cwd appears on every non-meta row; first-wins (cwd doesn't
            # change within a session — it was the process's working dir
            # when claude was launched). Storing it lets DormantSessionSource
            # answer "where do I cd to before claude --resume <uuid>?"
            # without re-decoding the hashed parent dir name.
            if "cwd" not in meta:
                cwd_v = entry.get("cwd")
                if isinstance(cwd_v, str) and cwd_v:
                    meta["cwd"] = cwd_v
            # permissionMode also rides on regular user/assistant rows,
            # not just dedicated permission-mode flip rows. Latest-wins so
            # the value reflects what the user had set when they last
            # interacted — that's the mode we want to restore on resume.
            pm_inline = entry.get("permissionMode")
            if isinstance(pm_inline, str) and pm_inline:
                meta["permission_mode"] = pm_inline
            # turn / sidechain counts live in UsageRegistry — there
            # they're computed from unique message.id, dedupping the
            # N-rows-per-response duplication we already handle there.

            ts = _parse_ts(entry)
            usage, model = _extract_usage(entry)

            # Track earliest + latest timestamp across every row (not just
            # rows with usage). The first row is a "type": "user" prompt
            # which carries the session start time; the last row is whatever
            # Claude wrote last (could be a system or summary row, not
            # necessarily an assistant turn).
            if ts is not None:
                if earliest_ts is None or ts < earliest_ts:
                    earliest_ts = ts
                if latest_ts is None or ts > latest_ts:
                    latest_ts = ts

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
                    # Any record sourced from a subagent file is, by
                    # definition, a sidechain of the parent session —
                    # forced True regardless of the row's own isSidechain
                    # flag (which Claude Code may or may not stamp on
                    # rows inside agent-*.jsonl). This keeps the parent's
                    # sidechain_count accurate when the popup renders.
                    is_sidechain=is_subagent or bool(entry.get("isSidechain")),
                ))
                if last_activity is None or ts > last_activity:
                    last_activity = ts

        # Persist earliest_ts → started_at (earliest-wins; the detail
        # popup uses this when ~/.claude/sessions/<pid>.json is absent).
        if earliest_ts is not None:
            existing_start = meta.get("started_at")
            if existing_start is None or earliest_ts < existing_start:
                meta["started_at"] = earliest_ts
        # Persist latest_ts → last_activity (latest-wins; DormantSessionSource
        # uses this for "sort offline sessions by recency"; cheaper than
        # re-stat'ing each .jsonl + handles transcripts that were appended
        # to since the last parse without re-shipping all contents).
        if latest_ts is not None:
            existing_last = meta.get("last_activity")
            if existing_last is None or latest_ts > existing_last:
                meta["last_activity"] = latest_ts

        # Subagent activity must bump the PARENT's persistent
        # last_activity so the parent session appears "active" while a
        # subagent is mid-run. The throwaway ``meta`` above correctly
        # absorbs subagent-specific pollution (ai_title, last_prompt,
        # permission_mode, cwd, ...); last_activity is the one field
        # that legitimately belongs on the parent's metadata.
        if is_subagent and latest_ts is not None:
            parent_meta = self._session_meta.setdefault(session_uuid, {})
            existing = parent_meta.get("last_activity")
            if existing is None or latest_ts > existing:
                parent_meta["last_activity"] = latest_ts

        # Advance offset and emit records. Order is important: advance
        # offset BEFORE the registry call so a watchdog event firing
        # again for this same file (a re-fire on the same write) sees
        # the new offset and reads zero bytes. The registry call may
        # itself synchronously fan out to subscribers (the UI bridge),
        # which we don't want to do twice.
        self._offsets[path_str] = new_offset - tail_len
        self._usage.record_many(batch)

        _metrics.observe(
            "jsonl.parse.duration_ms",
            (_time.perf_counter() - _t0) * 1000.0,
        )


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
