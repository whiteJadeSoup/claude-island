"""Tests for UsageRegistry — particularly the batch write path (B4)."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from claude_island.core.usage_registry import UsageRegistry


def _entry(input_tokens: int = 100, output_tokens: int = 50) -> dict:
    # Use "now" so rows fall inside the daily / weekly / monthly windows
    # that get_totals filters by.
    return {
        "timestamp": datetime.now(timezone.utc),
        "project_path": "proj",
        "session_uuid": "sess",
        "model": "claude-sonnet-4-5",
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cache_creation_tokens": 0,
        "cache_read_tokens": 0,
    }


@pytest.fixture
def registry(tmp_path):
    reg = UsageRegistry(db_path=tmp_path / "u.db")
    yield reg
    reg.close()


def _row_count(reg: UsageRegistry) -> int:
    with reg._lock:
        return reg._conn.execute("SELECT COUNT(*) FROM usage_records").fetchone()[0]


# --------------------------------------------------------------------------
# B4: batch insert + single emit
# --------------------------------------------------------------------------

def test_record_many_inserts_all_rows_in_one_transaction(registry):
    entries = [_entry(input_tokens=i) for i in range(50)]
    registry.record_many(entries)
    assert _row_count(registry) == 50


def test_record_many_emits_totals_changed_exactly_once(registry):
    received = []
    registry.totals_changed.subscribe(received.append)

    entries = [_entry(input_tokens=i) for i in range(100)]
    registry.record_many(entries)

    assert len(received) == 1


def test_record_many_with_empty_list_is_noop(registry):
    received = []
    registry.totals_changed.subscribe(received.append)

    registry.record_many([])

    assert _row_count(registry) == 0
    assert received == []  # no transaction, no emit


def test_record_single_still_works_via_record_many_wrapper(registry):
    """Backward-compat: record() with kwargs must still insert one row and
    emit once."""
    received = []
    registry.totals_changed.subscribe(received.append)

    registry.record(
        timestamp=datetime.now(timezone.utc),
        project_path="p",
        session_uuid="s",
        model="claude-sonnet-4-5",
        input_tokens=100,
        output_tokens=50,
        cache_creation_tokens=0,
        cache_read_tokens=0,
    )

    assert _row_count(registry) == 1
    assert len(received) == 1


def test_record_many_rolls_back_on_partial_failure(registry):
    """A bad entry mid-batch should not leave half the batch committed.
    SQLite's default rollback semantics on executemany failure ensures this.
    """
    entries = [_entry(input_tokens=i) for i in range(5)]
    # Force a type error mid-batch: input_tokens as a string the binding rejects.
    entries[3]["input_tokens"] = object()  # not adaptable to SQLite

    with pytest.raises(Exception):
        registry.record_many(entries)

    # Either zero rows committed, or all five — definitely not "first 3".
    n = _row_count(registry)
    assert n in (0, 5), f"partial commit observed: {n} rows"


def test_get_totals_aggregates_across_batch_records(registry):
    """Sanity: totals computation works correctly on batch-inserted rows."""
    entries = [_entry(input_tokens=100, output_tokens=50) for _ in range(10)]
    registry.record_many(entries)

    totals = registry.get_totals("daily")
    assert totals.input_tokens == 1000
    assert totals.output_tokens == 500


# --------------------------------------------------------------------------
# S2: per-file atomicity (records + offset in one transaction)
# --------------------------------------------------------------------------

def test_record_many_with_advance_offset_writes_both(registry):
    """Both INSERT rows and offset UPSERT must be visible after the call."""
    entries = [_entry(input_tokens=10) for _ in range(3)]
    registry.record_many(entries, advance_offset=("/path/to/x.jsonl", 1234))

    assert _row_count(registry) == 3
    assert registry.get_offset("/path/to/x.jsonl") == 1234


def test_record_many_with_advance_offset_only_no_rows(registry):
    """An empty batch with advance_offset still writes the offset (used by
    the truncation-reset path or on a chunk that had no parseable rows)."""
    received = []
    registry.totals_changed.subscribe(received.append)

    registry.record_many([], advance_offset=("/path/to/y.jsonl", 0))

    assert _row_count(registry) == 0
    assert registry.get_offset("/path/to/y.jsonl") == 0
    # No emit when no rows were inserted.
    assert received == []


def test_record_many_atomic_rollback_on_partial_failure(registry):
    """If the UPSERT for offset somehow raises after the INSERTs, the
    whole transaction must roll back. We trigger a rollback by passing
    a non-stringifiable file path to the offset binding."""
    entries = [_entry(input_tokens=10) for _ in range(5)]

    with pytest.raises(Exception):
        registry.record_many(
            entries,
            advance_offset=(object(), 42),  # bad binding for TEXT column
        )

    # Records must NOT be committed because the offset write failed
    # in the same transaction.
    assert _row_count(registry) == 0


def test_record_many_advance_offset_overwrites_previous(registry):
    """UPSERT semantics: same file_path keeps last write."""
    registry.record_many([], advance_offset=("/path/to/z.jsonl", 100))
    registry.record_many([], advance_offset=("/path/to/z.jsonl", 500))
    assert registry.get_offset("/path/to/z.jsonl") == 500


# --------------------------------------------------------------------------
# S1: schema cleanup + migration
# --------------------------------------------------------------------------

def _column_names(reg: UsageRegistry, table: str) -> list[str]:
    rows = reg._conn.execute(f"PRAGMA table_info({table})").fetchall()
    return [r[1] for r in rows]


def _index_names(reg: UsageRegistry, table: str) -> list[str]:
    rows = reg._conn.execute(f"PRAGMA index_list({table})").fetchall()
    return [r[1] for r in rows]


def test_fresh_db_has_no_cost_usd_column(registry):
    """New databases never get the legacy column."""
    assert "cost_usd" not in _column_names(registry, "usage_records")


def test_fresh_db_has_composite_index_only(registry):
    """New databases get the composite (timestamp, model) index, not the
    obsolete timestamp-only index."""
    indexes = _index_names(registry, "usage_records")
    assert "idx_usage_timestamp_model" in indexes
    assert "idx_usage_timestamp" not in indexes


def test_migration_drops_cost_usd_from_legacy_db(tmp_path):
    """User upgraded from a pre-S1 build: open with the legacy schema in
    place, then re-open with the new code — cost_usd column should be gone."""
    import sqlite3
    db_path = tmp_path / "legacy.db"

    # Stand up the OLD schema by hand.
    legacy_schema = """
    CREATE TABLE usage_records (
        id                    INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp             TEXT    NOT NULL,
        project_path          TEXT    NOT NULL,
        session_uuid          TEXT    NOT NULL,
        model                 TEXT    NOT NULL,
        input_tokens          INTEGER NOT NULL,
        output_tokens         INTEGER NOT NULL,
        cache_creation_tokens INTEGER NOT NULL DEFAULT 0,
        cache_read_tokens     INTEGER NOT NULL DEFAULT 0,
        cost_usd              REAL
    );
    CREATE INDEX idx_usage_timestamp ON usage_records(timestamp);
    CREATE TABLE parse_offsets (
        file_path      TEXT    PRIMARY KEY,
        byte_offset    INTEGER NOT NULL,
        last_parsed_at TEXT    NOT NULL
    );
    """
    legacy_conn = sqlite3.connect(str(db_path))
    legacy_conn.executescript(legacy_schema)
    legacy_conn.execute(
        "INSERT INTO usage_records (timestamp, project_path, session_uuid, "
        "model, input_tokens, output_tokens, cost_usd) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (datetime.now(timezone.utc).isoformat(), "p", "s",
         "claude-sonnet-4-5", 100, 50, 0.99),
    )
    legacy_conn.commit()
    legacy_conn.close()

    # Open via the new code — migration runs.
    reg = UsageRegistry(db_path=db_path)
    try:
        # Legacy data must survive.
        assert _row_count(reg) == 1
        # cost_usd column must be gone.
        assert "cost_usd" not in _column_names(reg, "usage_records")
        # Composite index present, old one gone.
        indexes = _index_names(reg, "usage_records")
        assert "idx_usage_timestamp_model" in indexes
        assert "idx_usage_timestamp" not in indexes
        # get_totals still works (recomputes from token columns).
        totals = reg.get_totals("daily")
        assert totals.input_tokens == 100
    finally:
        reg.close()


def test_migration_is_idempotent(tmp_path):
    """Re-opening an already-migrated DB should run cleanly (the
    'no such column' / 'no such index' errors get swallowed)."""
    db_path = tmp_path / "twice.db"
    UsageRegistry(db_path=db_path).close()
    # Second open: migrations re-run on an already-clean schema.
    reg = UsageRegistry(db_path=db_path)
    try:
        assert "cost_usd" not in _column_names(reg, "usage_records")
    finally:
        reg.close()
