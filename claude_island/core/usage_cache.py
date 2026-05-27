"""Persistent cache for ``UsageRegistry`` + ``JsonlParser`` state.

Why
---
Every boot, ``start_backfill_pool`` re-parses the entire JSONL corpus
under ``~/.claude/projects/`` — 446 files / 562 MB / ~27K records on
the maintainer's setup. Bench (commit ``2b13f32`` metrics + manual
profiling 2026-05-26):

    Python+Qt floor       : ~1000 ms
    JSONL backfill (4 wkr): ~2000 ms
    Total perceived boot  : ~3000 ms

Backfill is bounded by JSON-decode throughput (~14 MB/s per core).
It can't go meaningfully faster without changing the algorithm. The
only way to drop boot time is to *skip* the backfill — which we can
do safely if we've already parsed those bytes in a previous run.

This module persists the registry's in-memory state to disk at flush
time and restores it on boot. Next boot:

    1. Restore ``_records``, ``_seen_message_ids``, ``_offsets``,
       ``_session_meta`` from the cache.
    2. Run backfill normally. For every file whose offset is at EOF
       (the common case for an immediately-after-shutdown boot),
       ``_parse_incremental`` reads an empty chunk and returns
       without doing any JSON work.
    3. Files that GREW since shutdown get parsed incrementally;
       brand-new files get parsed from byte 0; truncated files reset
       offset 0 (the existing parser handles all three).

Net effect: a returning user's boot drops from ~3000 ms to roughly
the floor (~1100 ms cache-load included).

Design
------
* Format: gzipped JSON. Stdlib-only (``json`` + ``gzip``), human-
  inspectable (``gunzip -c | jq``), survives Python-version upgrades.
  At ~7 MB raw / 1-2 MB gzipped it's well under the cost ceiling
  where binary formats (pickle / sqlite) would start to matter.

* Location: ``platformdirs.user_data_dir(_APP_NAME)/usage_cache.json.gz``.
  Matches the existing quota cache path the providers package uses —
  same OS-appropriate convention, lives alongside the rest of the
  app's managed state rather than under user-facing
  ``~/.claude-island/`` (which is for config files the user edits).

* Schema versioning: a top-level ``"version"`` int. On any mismatch,
  the cache is silently ignored and the full backfill runs. Adding
  a field to ``UsageRecord`` bumps ``CACHE_VERSION``; legacy caches
  become inert without crashing the load path.

* Atomicity: tmp file + ``os.replace`` (POSIX rename is atomic on
  the same filesystem). A crash mid-write leaves the previous cache
  intact rather than producing a half-written file that would fail
  to load.

* Corruption recovery: every exception in ``load_cache`` returns
  ``None`` (and logs at WARNING). The cache is a perf optimization,
  not a source of truth — JSONL on disk remains the canonical record,
  and a missing cache just means the next boot does a full parse.

What's NOT persisted
--------------------
* ``UsageRegistry._by_uuid`` — derivable from ``_records`` in O(n).
  Rebuilt by replaying records through ``record_many`` semantics
  during apply. Smaller cache + impossible-to-drift invariant.
"""
from __future__ import annotations

import gzip
import json
import logging
import os
from collections import OrderedDict
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from claude_island.core.metrics import metrics as _metrics
from claude_island.core.models import UsageRecord


log = logging.getLogger(__name__)


# Bump when the on-disk schema (UsageRecord fields, top-level keys)
# changes incompatibly. The load path uses this to detect stale
# caches written by a prior app version and silently ignore them.
CACHE_VERSION = 1


def cache_path(data_dir: Path) -> Path:
    """Resolve the cache file path inside the platform's data dir."""
    return data_dir / "usage_cache.json.gz"


def _record_to_dict(r: UsageRecord) -> dict[str, Any]:
    d = asdict(r)
    # asdict serialises datetime as a datetime — JSON can't carry that.
    d["timestamp"] = r.timestamp.isoformat()
    return d


def _dict_to_record(d: dict[str, Any]) -> UsageRecord:
    return UsageRecord(
        timestamp=datetime.fromisoformat(d["timestamp"]),
        project_path=d["project_path"],
        session_uuid=d["session_uuid"],
        model=d["model"],
        input_tokens=d["input_tokens"],
        output_tokens=d["output_tokens"],
        cache_creation_tokens=d["cache_creation_tokens"],
        cache_read_tokens=d["cache_read_tokens"],
        message_id=d.get("message_id"),
        is_sidechain=d.get("is_sidechain", False),
    )


def _meta_to_jsonable(meta: dict[str, dict]) -> dict[str, dict]:
    """Convert ``_session_meta`` (datetime values included) to a
    JSON-serialisable dict. last_activity / started_at are datetimes;
    everything else is already JSON-safe."""
    out: dict[str, dict] = {}
    for uuid, m in meta.items():
        rec: dict[str, Any] = {}
        for k, v in m.items():
            if isinstance(v, datetime):
                rec[k] = {"__dt__": v.isoformat()}
            else:
                rec[k] = v
        out[uuid] = rec
    return out


def _meta_from_jsonable(meta: dict[str, dict]) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for uuid, m in meta.items():
        rec: dict[str, Any] = {}
        for k, v in m.items():
            if isinstance(v, dict) and "__dt__" in v:
                rec[k] = datetime.fromisoformat(v["__dt__"])
            else:
                rec[k] = v
        out[uuid] = rec
    return out


def save_cache(
    *,
    records: list[UsageRecord],
    seen_message_ids: OrderedDict[str, None],
    offsets: dict[str, int],
    session_meta: dict[str, dict],
    path: Path,
) -> None:
    """Write the registry + parser state to ``path`` atomically.

    Never raises — the cache is best-effort. On any IOError we log
    a WARNING and increment ``usage.cache.save_error`` so production
    can see if the save path is broken (disk full, permission denied,
    etc). The next boot falls back to a full parse, which is correct
    if slow.
    """
    import time as _time
    t0 = _time.perf_counter()
    try:
        payload = {
            "version": CACHE_VERSION,
            "saved_at": datetime.now(timezone.utc).isoformat(),
            "usage": {
                "records": [_record_to_dict(r) for r in records],
                # OrderedDict preserves insertion order; persist as a
                # plain list so FIFO ordering survives the round-trip
                # (the cap's eviction depends on insert order).
                "seen_message_ids": list(seen_message_ids.keys()),
            },
            "jsonl": {
                "offsets": dict(offsets),
                "session_meta": _meta_to_jsonable(session_meta),
            },
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        # gzip the JSON inline — at ~7 MB raw the file is uncomfortable
        # to hold in editor backup dirs / Time Machine; gzipped it's
        # comparable to a single screenshot.
        with gzip.open(tmp, "wt", encoding="utf-8") as fh:
            json.dump(payload, fh, separators=(",", ":"))
        os.replace(tmp, path)
        _metrics.observe(
            "usage.cache.save_ms", (_time.perf_counter() - t0) * 1000.0,
        )
        _metrics.incr("usage.cache.save")
    except OSError as e:
        log.warning("usage cache save failed: %s", e)
        _metrics.incr("usage.cache.save_error")


class CacheData:
    """Container for the restored state. Plain object (not a frozen
    dataclass) so ``apply_cache`` can yield references into it without
    extra copies.
    """
    __slots__ = ("records", "seen_message_ids", "offsets", "session_meta")

    def __init__(
        self,
        records: list[UsageRecord],
        seen_message_ids: list[str],
        offsets: dict[str, int],
        session_meta: dict[str, dict],
    ):
        self.records = records
        self.seen_message_ids = seen_message_ids
        self.offsets = offsets
        self.session_meta = session_meta


def load_cache(path: Path) -> CacheData | None:
    """Read + parse the cache file. Returns None when missing,
    corrupted, or schema-mismatched — caller treats None as "do a
    full parse from scratch".

    Time-cost telemetry is recorded under ``usage.cache.load_ms``
    only on success; misses don't pollute the timing histogram.
    """
    import time as _time
    if not path.exists():
        _metrics.incr("usage.cache.miss")
        return None
    t0 = _time.perf_counter()
    try:
        with gzip.open(path, "rt", encoding="utf-8") as fh:
            payload = json.load(fh)
        if payload.get("version") != CACHE_VERSION:
            log.info(
                "usage cache version mismatch (file=%s, expected=%s); "
                "ignoring cache and full-parsing",
                payload.get("version"), CACHE_VERSION,
            )
            _metrics.incr("usage.cache.version_mismatch")
            return None
        usage = payload.get("usage", {})
        jsonl = payload.get("jsonl", {})
        data = CacheData(
            records=[_dict_to_record(d) for d in usage.get("records", [])],
            seen_message_ids=list(usage.get("seen_message_ids", [])),
            offsets=dict(jsonl.get("offsets", {})),
            session_meta=_meta_from_jsonable(jsonl.get("session_meta", {})),
        )
        _metrics.observe(
            "usage.cache.load_ms", (_time.perf_counter() - t0) * 1000.0,
        )
        _metrics.incr("usage.cache.load")
        _metrics.incr("usage.cache.records_restored", n=len(data.records))
        return data
    except (OSError, ValueError, KeyError, gzip.BadGzipFile) as e:
        log.warning(
            "usage cache load failed (%s); falling back to full parse", e,
        )
        _metrics.incr("usage.cache.load_error")
        return None


def apply_cache(
    data: CacheData,
    *,
    registry,
    parser,
) -> None:
    """Hydrate ``UsageRegistry`` and ``JsonlParser`` from ``data``.

    Mutates both in place. Rebuilds ``_by_uuid`` from records (faster
    than persisting it and guarantees the invariant
    ``sum(len(v) for v in _by_uuid.values()) == len(_records)`` holds
    by construction). Restores ``_seen_message_ids`` in the order it
    was saved so FIFO eviction continues to drop the oldest entries.
    """
    # Direct attribute writes are OK — we're the boot path, no other
    # thread can be reading these yet (Snapshotter / backfill pool /
    # hook server all start AFTER apply_cache returns).
    registry._records = data.records
    registry._seen_message_ids = OrderedDict.fromkeys(data.seen_message_ids)
    registry._by_uuid = {}
    for r in data.records:
        registry._by_uuid.setdefault(r.session_uuid, []).append(r)
    parser._offsets = data.offsets
    parser._session_meta = data.session_meta
