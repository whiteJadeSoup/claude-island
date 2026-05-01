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


# --------------------------------------------------------------------------
# W1-W5: 5h session window detection
# --------------------------------------------------------------------------
from datetime import timedelta


def _entry_at(when: datetime, *, model: str = "claude-sonnet-4-5",
              input_tokens: int = 100, output_tokens: int = 50) -> dict:
    return {
        "timestamp": when,
        "project_path": "proj",
        "session_uuid": "sess",
        "model": model,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cache_creation_tokens": 0,
        "cache_read_tokens": 0,
    }


def test_session_window_empty_db_returns_nones(registry):
    """W1: zero records → SessionUsage with None timestamps and empty totals."""
    su = registry.get_session_window()
    assert su.start_time is None
    assert su.end_time is None
    assert su.by_model == ()
    assert su.total_cost_usd == 0.0
    assert su.quota is None


def test_session_window_single_recent_record(registry):
    """W2: one record 1h ago → start_time at that record, end_time +5h."""
    one_hour_ago = datetime.now(timezone.utc) - timedelta(hours=1)
    registry.record_many([_entry_at(one_hour_ago)])

    su = registry.get_session_window()
    assert su.start_time is not None
    # Compare to seconds-precision; ISO round-trip preserves microseconds
    assert abs((su.start_time - one_hour_ago).total_seconds()) < 1
    assert (su.end_time - su.start_time) == timedelta(hours=5)
    assert len(su.by_model) == 1
    assert su.by_model[0].input_tokens == 100


def test_session_window_block_break_after_long_gap(registry):
    """W3: 6h-old record + 30min-old record → 5.5h gap breaks the block,
    so session_start = the 30min-old record (only it counts)."""
    now = datetime.now(timezone.utc)
    registry.record_many([
        _entry_at(now - timedelta(hours=6), input_tokens=999),
        _entry_at(now - timedelta(minutes=30), input_tokens=42),
    ])

    su = registry.get_session_window()
    # Window starts at the most-recent block-start = the 30-min-ago record
    assert abs((su.start_time - (now - timedelta(minutes=30))).total_seconds()) < 1
    # Only the 30-min row counts, not the 6h-ago one
    assert len(su.by_model) == 1
    assert su.by_model[0].input_tokens == 42


def test_session_window_continuous_block(registry):
    """W4: 4h-old record + 1h-old record (only 3h apart, < 5h gap)
    → both belong to the same block; session_start = 4h-old record."""
    now = datetime.now(timezone.utc)
    registry.record_many([
        _entry_at(now - timedelta(hours=4), input_tokens=10),
        _entry_at(now - timedelta(hours=1), input_tokens=20),
    ])

    su = registry.get_session_window()
    assert abs((su.start_time - (now - timedelta(hours=4))).total_seconds()) < 1
    # Both rows aggregated under the same model
    assert len(su.by_model) == 1
    assert su.by_model[0].input_tokens == 30


def test_session_window_all_old_returns_expired_block(registry):
    """W5: every record is ≥ 5h old → still returns the last block, but
    end_time is in the past (caller decides how to render 'expired')."""
    now = datetime.now(timezone.utc)
    registry.record_many([
        _entry_at(now - timedelta(hours=10)),
        _entry_at(now - timedelta(hours=8)),
    ])

    su = registry.get_session_window()
    # Most-recent block-start is the 10h record (the 8h one is within
    # 5h of it, same block — start = 10h ago).
    assert abs((su.start_time - (now - timedelta(hours=10))).total_seconds()) < 1
    assert su.end_time < now      # block already expired


# --------------------------------------------------------------------------
# M1-M2: per-model breakdown
# --------------------------------------------------------------------------

def test_totals_by_model_returns_each_model_priced(registry):
    """M1: rows from multiple models → tuple ordered by cost desc, each
    ModelTotals priced via the model's PRICING entry."""
    now = datetime.now(timezone.utc)
    registry.record_many([
        _entry_at(now, model="claude-sonnet-4-5",
                  input_tokens=1_000_000, output_tokens=0),  # $3 input
        _entry_at(now, model="claude-haiku-4-5",
                  input_tokens=1_000_000, output_tokens=0),  # $1 input
    ])

    rows = registry.get_totals_by_model("today")
    # Two distinct models, sorted by cost descending
    assert len(rows) == 2
    assert rows[0].cost_usd > rows[1].cost_usd
    assert "sonnet" in rows[0].model.lower()
    assert "haiku" in rows[1].model.lower()
    # Sonnet input rate = $3/Mtok → 1M input = $3
    assert abs(rows[0].cost_usd - 3.0) < 1e-6


def test_totals_by_model_empty_returns_empty_tuple(registry):
    """M2: empty DB → empty tuple, no exception."""
    assert registry.get_totals_by_model("today") == ()
