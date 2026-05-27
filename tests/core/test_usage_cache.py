"""Tests for the persistence cache."""
from __future__ import annotations

import gzip
import json
from collections import OrderedDict
from datetime import datetime, timezone
from pathlib import Path

import pytest

from claude_island.core.jsonl_parser import JsonlParser
from claude_island.core.metrics import metrics
from claude_island.core.models import UsageRecord
from claude_island.core.usage_cache import (
    CACHE_VERSION,
    apply_cache,
    cache_path,
    load_cache,
    save_cache,
)
from claude_island.core.usage_registry import UsageRegistry


def _t(i: int) -> datetime:
    return datetime(2026, 5, 26, 0, 0, 0, tzinfo=timezone.utc).replace(
        microsecond=i,
    )


def _record(i: int, *, uuid: str = "u") -> UsageRecord:
    return UsageRecord(
        timestamp=_t(i),
        project_path="proj",
        session_uuid=uuid,
        model="claude-sonnet-4-6",
        input_tokens=10, output_tokens=5,
        cache_creation_tokens=2, cache_read_tokens=1,
        message_id=f"msg-{i}",
    )


@pytest.fixture
def cache_file(tmp_path: Path) -> Path:
    return cache_path(tmp_path)


# ── round-trip ──────────────────────────────────────────────────────────


def test_round_trip_preserves_records(cache_file: Path):
    """Records survive save→load exactly — fields, types, count."""
    records = [_record(i) for i in range(5)]
    save_cache(
        records=records,
        seen_message_ids=OrderedDict.fromkeys(f"msg-{i}" for i in range(5)),
        offsets={},
        session_meta={},
        path=cache_file,
    )
    data = load_cache(cache_file)
    assert data is not None
    assert len(data.records) == 5
    assert all(isinstance(r.timestamp, datetime) for r in data.records)
    assert data.records[0] == records[0]


def test_round_trip_preserves_seen_message_ids_order(cache_file: Path):
    """OrderedDict FIFO order MUST survive — the cap's eviction policy
    drops the oldest entry, so insertion order being preserved is the
    correctness invariant the cap relies on."""
    ids = [f"msg-{i}" for i in range(10)]
    save_cache(
        records=[],
        seen_message_ids=OrderedDict.fromkeys(ids),
        offsets={},
        session_meta={},
        path=cache_file,
    )
    data = load_cache(cache_file)
    assert data is not None
    assert data.seen_message_ids == ids


def test_round_trip_preserves_offsets_and_meta(cache_file: Path):
    """Offsets keyed by string path; meta has datetime values that must
    survive the JSON round-trip."""
    save_cache(
        records=[],
        seen_message_ids=OrderedDict(),
        offsets={"/foo/a.jsonl": 1234, "/foo/b.jsonl": 5678},
        session_meta={
            "u1": {
                "cwd": "/foo",
                "ai_title": "test",
                "last_activity": _t(99),
            },
        },
        path=cache_file,
    )
    data = load_cache(cache_file)
    assert data is not None
    assert data.offsets == {"/foo/a.jsonl": 1234, "/foo/b.jsonl": 5678}
    meta = data.session_meta["u1"]
    assert meta["cwd"] == "/foo"
    assert meta["ai_title"] == "test"
    assert isinstance(meta["last_activity"], datetime)
    assert meta["last_activity"] == _t(99)


# ── miss / corruption / version-mismatch ────────────────────────────────


def test_load_returns_none_when_file_missing(cache_file: Path):
    """First-time-user case: no cache file at all → None, full parse
    runs. Counter ``usage.cache.miss`` increments."""
    assert load_cache(cache_file) is None
    assert metrics.snapshot().counters.get("usage.cache.miss") == 1


def test_load_returns_none_on_corrupt_gzip(cache_file: Path):
    """Disk fault / interrupted prior save → corrupted gzip → swallow
    and re-parse from scratch. Counter ``usage.cache.load_error``
    increments."""
    cache_file.parent.mkdir(parents=True, exist_ok=True)
    cache_file.write_bytes(b"not a gzip file")
    assert load_cache(cache_file) is None
    assert metrics.snapshot().counters.get("usage.cache.load_error") == 1


def test_load_returns_none_on_corrupt_json_inside_gzip(cache_file: Path):
    cache_file.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(cache_file, "wt", encoding="utf-8") as fh:
        fh.write("{this is not valid json")
    assert load_cache(cache_file) is None
    assert metrics.snapshot().counters.get("usage.cache.load_error") == 1


def test_load_returns_none_on_version_mismatch(cache_file: Path):
    """Cache from a prior app version → schema may have changed →
    silently discard and re-parse. version_mismatch counter increments
    (distinct from load_error so we can tell schema-bumps from real
    corruption in production telemetry)."""
    cache_file.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(cache_file, "wt", encoding="utf-8") as fh:
        json.dump({"version": CACHE_VERSION + 999, "usage": {"records": []}}, fh)
    assert load_cache(cache_file) is None
    assert metrics.snapshot().counters.get("usage.cache.version_mismatch") == 1


# ── apply_cache ─────────────────────────────────────────────────────────


def test_apply_cache_hydrates_registry_records_and_by_uuid_index(
    cache_file: Path,
):
    """After apply, the registry behaves identically to one populated
    via record_many — including the inverted ``_by_uuid`` index that
    apply rebuilds (not persisted) from the records list."""
    records = [_record(i, uuid=f"u-{i%3}") for i in range(6)]
    save_cache(
        records=records,
        seen_message_ids=OrderedDict.fromkeys(r.message_id for r in records),
        offsets={},
        session_meta={},
        path=cache_file,
    )
    data = load_cache(cache_file)
    assert data is not None

    reg = UsageRegistry()
    parser = JsonlParser(usage_registry=reg, claude_projects_dir=Path("/x"))
    apply_cache(data, registry=reg, parser=parser)

    assert len(reg._records) == 6
    # by_uuid invariant: sum of inverted-index lists == records length
    assert sum(len(v) for v in reg._by_uuid.values()) == len(reg._records)
    # 3 distinct uuids (i%3) → 3 buckets, 2 records each
    assert set(reg._by_uuid) == {"u-0", "u-1", "u-2"}
    assert all(len(v) == 2 for v in reg._by_uuid.values())


def test_apply_cache_dedup_still_catches_replayed_message_ids(
    cache_file: Path,
):
    """After restore, a JSONL row that was already counted (msg id
    present in restored seen-set) must NOT double-count. This is the
    correctness invariant that justifies persisting _seen_message_ids
    at all — without it, the next backfill would re-add every record."""
    records = [_record(i) for i in range(3)]
    save_cache(
        records=records,
        seen_message_ids=OrderedDict.fromkeys(r.message_id for r in records),
        offsets={},
        session_meta={},
        path=cache_file,
    )
    data = load_cache(cache_file)
    assert data is not None
    reg = UsageRegistry()
    parser = JsonlParser(usage_registry=reg, claude_projects_dir=Path("/x"))
    apply_cache(data, registry=reg, parser=parser)

    # Re-submit the same records — every one should be dropped.
    reg.record_many(records)
    assert len(reg._records) == 3  # unchanged
    # All 3 were detected as duplicates.
    assert metrics.snapshot().counters.get("usage.record.deduped") == 3


def test_apply_cache_hydrates_parser_offsets_and_meta(cache_file: Path):
    offsets = {"/a/foo.jsonl": 100, "/a/bar.jsonl": 200}
    meta = {"u1": {"cwd": "/a", "last_activity": _t(5)}}
    save_cache(
        records=[],
        seen_message_ids=OrderedDict(),
        offsets=offsets,
        session_meta=meta,
        path=cache_file,
    )
    data = load_cache(cache_file)
    assert data is not None
    reg = UsageRegistry()
    parser = JsonlParser(usage_registry=reg, claude_projects_dir=Path("/x"))
    apply_cache(data, registry=reg, parser=parser)
    assert parser._offsets == offsets
    assert parser._session_meta == meta


# ── metrics ─────────────────────────────────────────────────────────────


def test_save_increments_save_metric_and_timing(cache_file: Path):
    save_cache(
        records=[_record(0)],
        seen_message_ids=OrderedDict.fromkeys(["msg-0"]),
        offsets={},
        session_meta={},
        path=cache_file,
    )
    snap = metrics.snapshot()
    assert snap.counters.get("usage.cache.save") == 1
    assert snap.timings.get("usage.cache.save_ms") is not None
    assert snap.timings["usage.cache.save_ms"].n == 1


def test_load_increments_load_metric_records_restored_and_timing(
    cache_file: Path,
):
    save_cache(
        records=[_record(i) for i in range(7)],
        seen_message_ids=OrderedDict.fromkeys(f"msg-{i}" for i in range(7)),
        offsets={},
        session_meta={},
        path=cache_file,
    )
    metrics.reset_for_testing()  # isolate from save metrics

    data = load_cache(cache_file)
    assert data is not None
    snap = metrics.snapshot()
    assert snap.counters.get("usage.cache.load") == 1
    assert snap.counters.get("usage.cache.records_restored") == 7
    assert snap.timings.get("usage.cache.load_ms") is not None


# ── atomicity ───────────────────────────────────────────────────────────


def test_save_uses_atomic_replace_not_partial_write(cache_file: Path):
    """The tmp+rename pattern must leave the prior cache intact if
    the rename never happens. Smoke-test by verifying a second save
    fully replaces the first one (not appending)."""
    save_cache(
        records=[_record(0)],
        seen_message_ids=OrderedDict.fromkeys(["msg-0"]),
        offsets={},
        session_meta={},
        path=cache_file,
    )
    first_size = cache_file.stat().st_size

    # Second save with more records — file should be larger.
    save_cache(
        records=[_record(i) for i in range(50)],
        seen_message_ids=OrderedDict.fromkeys(f"msg-{i}" for i in range(50)),
        offsets={},
        session_meta={},
        path=cache_file,
    )
    second_size = cache_file.stat().st_size
    assert second_size > first_size

    data = load_cache(cache_file)
    assert data is not None
    assert len(data.records) == 50  # not 51 (no append)
