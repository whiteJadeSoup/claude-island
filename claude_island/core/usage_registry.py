from __future__ import annotations

import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .events import Event
from .models import PRICING, DEFAULT_PRICING, PricingTable, UsageTotals

# INTEGER PRIMARY KEY (without AUTOINCREMENT) is automatically the ROWID
# alias on SQLite — same monotonic behaviour without the sqlite_sequence
# overhead. cost_usd is intentionally absent: get_totals recomputes cost
# from token columns + live PRICING on every read, so storing the cost
# was redundant and could disagree with display when prices change.
# Composite (timestamp, model) lets get_totals' "WHERE timestamp >= ?
# GROUP BY model" stream from the index without a sort step.
_SCHEMA = """
CREATE TABLE IF NOT EXISTS usage_records (
    id                    INTEGER PRIMARY KEY,
    timestamp             TEXT    NOT NULL,
    project_path          TEXT    NOT NULL,
    session_uuid          TEXT    NOT NULL,
    model                 TEXT    NOT NULL,
    input_tokens          INTEGER NOT NULL,
    output_tokens         INTEGER NOT NULL,
    cache_creation_tokens INTEGER NOT NULL DEFAULT 0,
    cache_read_tokens     INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_usage_timestamp_model
    ON usage_records(timestamp, model);

CREATE TABLE IF NOT EXISTS parse_offsets (
    file_path      TEXT    PRIMARY KEY,
    byte_offset    INTEGER NOT NULL,
    last_parsed_at TEXT    NOT NULL
);
"""

# Migration applied on every open. Each step is idempotent so re-running
# is safe; uses PRAGMA introspection to detect old layouts. Steps:
# - Drop legacy cost_usd column (SQLite 3.35+, ships with Python 3.11+).
# - Drop the timestamp-only index in favour of the composite one.
# - The legacy AUTOINCREMENT can't be dropped without rebuilding the table;
#   leave the historical id column as-is and rely on new tables having
#   plain INTEGER PRIMARY KEY going forward.
_MIGRATIONS = [
    "ALTER TABLE usage_records DROP COLUMN cost_usd",
    "DROP INDEX IF EXISTS idx_usage_timestamp",
]

# Rolling windows, not calendar periods — "monthly" is the trailing 30 days,
# not the current calendar month. Avoids month-boundary edge cases (28 vs 29
# vs 30 vs 31 days, year wraps) at the cost of a small UI label ambiguity
# ("Monthly" reads as either "this month" or "last 30d" depending on user).
_PERIOD_DELTA: dict[str, timedelta] = {
    "daily":   timedelta(days=1),
    "weekly":  timedelta(weeks=1),
    "monthly": timedelta(days=30),
}


def _apply_migrations(conn: sqlite3.Connection) -> None:
    """Run idempotent schema migrations on every open.

    Each statement either succeeds or fails for a known reason (column
    already gone, index already missing). Unknown failures propagate so
    we don't silently mask schema corruption. Called once during __init__
    after CREATE TABLE IF NOT EXISTS, so brand-new databases see this as
    a series of no-ops.
    """
    for stmt in _MIGRATIONS:
        try:
            conn.execute(stmt)
        except sqlite3.OperationalError as e:
            msg = str(e).lower()
            # "no such column" / "no such index" → migration already applied.
            if "no such column" in msg or "no such index" in msg:
                continue
            raise


def _resolve_pricing(model: str) -> PricingTable:
    # Substring match on lowercased model id so we survive Anthropic's
    # version-suffixing convention ("claude-3-5-sonnet-20241022" → "sonnet").
    # Iteration order is the dict's insertion order (haiku, sonnet, opus);
    # since these tokens don't appear in each other's names, order is safe.
    # Unknown / empty model silently falls back to DEFAULT_PRICING (sonnet
    # rates) — preferable to crashing, but means an unknown future family
    # gets priced as sonnet until the table is updated.
    lower = model.lower()
    for key, pricing in PRICING.items():
        if key in lower:
            return pricing
    return DEFAULT_PRICING


class UsageRegistry:
    """SQLite-backed store for per-turn token usage and incremental parse offsets.

    Thread-safe: a single lock serialises all DB writes so the JSONL parser
    thread and the main thread cannot interleave partial writes.
    """

    def __init__(self, *, db_path: Path) -> None:
        self.totals_changed: Event[None] = Event()
        self._db_path = db_path
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.executescript(_SCHEMA)
        _apply_migrations(self._conn)
        self._conn.commit()
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Write path
    # ------------------------------------------------------------------

    def record(
        self,
        *,
        timestamp: datetime,
        project_path: str,
        session_uuid: str,
        model: str,
        input_tokens: int,
        output_tokens: int,
        cache_creation_tokens: int,
        cache_read_tokens: int,
    ) -> None:
        """Insert a single usage record. Convenience wrapper around record_many.

        Prefer record_many for batch ingestion (e.g. backfill_all parsing
        thousands of historical turns) — record() emits totals_changed once
        per call, which floods the UI with redundant SELECT-and-redraw work
        when called in a tight loop.
        """
        self.record_many([{
            "timestamp": timestamp,
            "project_path": project_path,
            "session_uuid": session_uuid,
            "model": model,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cache_creation_tokens": cache_creation_tokens,
            "cache_read_tokens": cache_read_tokens,
        }])

    def record_many(
        self,
        entries: list[dict],
        *,
        advance_offset: tuple[str, int] | None = None,
    ) -> None:
        """Batch-insert usage records under a single transaction and emit
        totals_changed exactly once at the end.

        Each entry dict must carry the same keys as record()'s kwargs.
        Empty input is a no-op (no transaction, no emit) — but if
        ``advance_offset`` is set, the offset is still written so callers
        can checkpoint progress on a file with no parseable rows.

        ``advance_offset=(file_path, byte_offset)``: write the parse-offset
        UPSERT inside the same transaction as the record INSERTs. If the
        process is killed mid-file (e.g. user quits during backfill), the
        whole transaction rolls back atomically — no scenario where rows
        are committed but the offset is not (which would cause double-
        counting on the next start) or vice versa (which would cause data
        loss). This is the per-file durability guarantee S2 calls for.
        """
        # cost_usd is intentionally NOT stored — get_totals recomputes from
        # token columns + live PRICING so price-table updates retroactively
        # apply. Storing it would create a dual source of truth; the column
        # was dropped from the schema in S1.
        rows = [
            (
                e["timestamp"].isoformat(),
                e["project_path"],
                e["session_uuid"],
                e["model"],
                e["input_tokens"],
                e["output_tokens"],
                e["cache_creation_tokens"],
                e["cache_read_tokens"],
            )
            for e in entries
        ]

        if not rows and advance_offset is None:
            return

        now_iso = datetime.now(timezone.utc).isoformat()
        with self._lock:
            try:
                if rows:
                    self._conn.executemany(
                        """
                        INSERT INTO usage_records
                            (timestamp, project_path, session_uuid, model,
                             input_tokens, output_tokens,
                             cache_creation_tokens, cache_read_tokens)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        rows,
                    )
                if advance_offset is not None:
                    file_path, byte_offset = advance_offset
                    self._conn.execute(
                        """
                        INSERT INTO parse_offsets (file_path, byte_offset, last_parsed_at)
                        VALUES (?, ?, ?)
                        ON CONFLICT(file_path) DO UPDATE SET
                            byte_offset    = excluded.byte_offset,
                            last_parsed_at = excluded.last_parsed_at
                        """,
                        (file_path, byte_offset, now_iso),
                    )
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

        if rows:
            self.totals_changed.emit(None)

    # ------------------------------------------------------------------
    # Read path
    # ------------------------------------------------------------------

    def get_totals(self, period: str) -> UsageTotals:
        # Aggregate per model so each (model, token-class) pair is priced with
        # its own rate. Cost is recomputed on read rather than read from the
        # stored cost_usd column — this also auto-corrects records written
        # under stale pricing tables (e.g. old Opus rates).
        delta = _PERIOD_DELTA.get(period, timedelta(days=1))
        since = (datetime.now(timezone.utc) - delta).isoformat()
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT
                    model,
                    COALESCE(SUM(input_tokens),          0),
                    COALESCE(SUM(output_tokens),         0),
                    COALESCE(SUM(cache_creation_tokens), 0),
                    COALESCE(SUM(cache_read_tokens),     0)
                FROM usage_records
                WHERE timestamp >= ?
                GROUP BY model
                """,
                (since,),
            ).fetchall()

        totals = UsageTotals(period=period)
        for model, in_tok, out_tok, cw_tok, cr_tok in rows:
            p = _resolve_pricing(model)
            totals.input_tokens          += in_tok
            totals.output_tokens         += out_tok
            totals.cache_creation_tokens += cw_tok
            totals.cache_read_tokens     += cr_tok
            totals.input_cost            += in_tok / 1_000_000 * p.input_per_mtok
            totals.output_cost           += out_tok / 1_000_000 * p.output_per_mtok
            totals.cache_creation_cost   += cw_tok / 1_000_000 * p.input_per_mtok * 1.25
            totals.cache_read_cost       += cr_tok / 1_000_000 * p.input_per_mtok * 0.1
        return totals

    # ------------------------------------------------------------------
    # Incremental parse offsets
    # ------------------------------------------------------------------

    def get_offset(self, file_path: str) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT byte_offset FROM parse_offsets WHERE file_path = ?",
                (file_path,),
            ).fetchone()
        return row[0] if row else 0

    def set_offset(self, file_path: str, byte_offset: int) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO parse_offsets (file_path, byte_offset, last_parsed_at)
                VALUES (?, ?, ?)
                ON CONFLICT(file_path) DO UPDATE SET
                    byte_offset    = excluded.byte_offset,
                    last_parsed_at = excluded.last_parsed_at
                """,
                (file_path, byte_offset, now),
            )
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()
