from __future__ import annotations

import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .events import Event
from .models import PRICING, DEFAULT_PRICING, PricingTable, UsageTotals

_SCHEMA = """
CREATE TABLE IF NOT EXISTS usage_records (
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
CREATE INDEX IF NOT EXISTS idx_usage_timestamp ON usage_records(timestamp);

CREATE TABLE IF NOT EXISTS parse_offsets (
    file_path      TEXT    PRIMARY KEY,
    byte_offset    INTEGER NOT NULL,
    last_parsed_at TEXT    NOT NULL
);
"""

_PERIOD_DELTA: dict[str, timedelta] = {
    "daily":   timedelta(days=1),
    "weekly":  timedelta(weeks=1),
    "monthly": timedelta(days=30),
}


def _resolve_pricing(model: str) -> PricingTable:
    lower = model.lower()
    for key, pricing in PRICING.items():
        if key in lower:
            return pricing
    return DEFAULT_PRICING


def _compute_cost(
    input_tokens: int,
    output_tokens: int,
    cache_creation_tokens: int,
    cache_read_tokens: int,
    pricing: PricingTable,
) -> float:
    return (
        input_tokens / 1_000_000 * pricing.input_per_mtok
        + output_tokens / 1_000_000 * pricing.output_per_mtok
        + cache_creation_tokens / 1_000_000 * pricing.input_per_mtok * 1.25
        + cache_read_tokens / 1_000_000 * pricing.input_per_mtok * 0.1
    )


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
        pricing = _resolve_pricing(model)
        cost = _compute_cost(
            input_tokens, output_tokens,
            cache_creation_tokens, cache_read_tokens,
            pricing,
        )
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO usage_records
                    (timestamp, project_path, session_uuid, model,
                     input_tokens, output_tokens,
                     cache_creation_tokens, cache_read_tokens, cost_usd)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    timestamp.isoformat(), project_path, session_uuid, model,
                    input_tokens, output_tokens,
                    cache_creation_tokens, cache_read_tokens, cost,
                ),
            )
            self._conn.commit()
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
