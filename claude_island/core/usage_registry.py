"""In-memory aggregator over UsageRecords parsed from Claude Code JSONL.

Source of truth is the JSONL transcripts at ``~/.claude/projects/``;
this registry just holds them in memory and answers aggregation
queries (``get_totals(period)`` / ``get_totals_by_model`` /
``get_session_window``) on demand. There is no on-disk derived store —
the in-memory list is rebuilt from JSONL on every process start, so
"is the cache out of sync with the JSONL?" is a question that simply
cannot exist.

Why no SQLite (we used to have it):
- The cache + offset-tracking machinery accumulated migration and
  dedup complexity (see git history — there were UNIQUE-index and
  re-parse-after-offset-rewind incidents that double-counted tokens).
- For Claude Code's per-user data scale (~100K rows/year × ~150 bytes
  in memory ≈ 15 MB) a Python list is plenty; queries iterate a few
  tens of thousands of records in <50 ms — well below the UI's 60 s
  refresh budget.
- JSONL stays the single source of truth: any disagreement between
  this tool and ``claude /status`` reduces to one of "did we read the
  same transcript?" rather than two layers of cache divergence.

Thread safety: a single lock serialises mutating writes
(``record_many``) and serialises reads against in-flight writes so
the iteration sees a consistent snapshot.
"""
from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone
from typing import Iterable

from .events import Event
from .models import (
    PRICING,
    DEFAULT_PRICING,
    ModelTotals,
    PricingTable,
    SessionUsage,
    UsageRecord,
    UsageTotals,
)

# Anthropic consumer-plan session window. Local-derived windows
# approximate this (rolling 5h); the real boundary comes from
# QuotaProvider's /api/oauth/usage when the caller passes it in.
_SESSION_WINDOW_HOURS = 5

# Rolling windows, not calendar periods — "monthly" is the trailing
# 30 days, not the current calendar month. Avoids month-boundary edge
# cases at the cost of a small UI-label ambiguity.
_PERIOD_DELTA: dict[str, timedelta] = {
    "daily":   timedelta(days=1),
    "weekly":  timedelta(weeks=1),
    "monthly": timedelta(days=30),
}


def _today_cutoff() -> datetime:
    """Midnight in the user's *local* timezone, returned as a UTC
    timestamp so it can compare with the records' UTC ``timestamp``.

    Why local instead of UTC: a user in UTC+8 sees "today" as the
    period since their 00:00 — which is UTC-8 of the previous calendar
    day. Filtering by UTC midnight would silently exclude everything
    they did before their local 08:00 (= UTC 00:00) and report
    "Today $0" until they crossed that line. Using their local
    midnight matches what they expect "today" to mean.
    """
    # ``datetime.now()`` is naive; .astimezone() attaches the system
    # local timezone; .replace() snaps to that day's local 00:00; a
    # second .astimezone(UTC) converts the result back to UTC for the
    # ``timestamp >= cutoff`` comparison against UTC-stamped records.
    local_midnight = (
        datetime.now()
        .astimezone()
        .replace(hour=0, minute=0, second=0, microsecond=0)
    )
    return local_midnight.astimezone(timezone.utc)


def _period_cutoff(period: str) -> datetime:
    """Earliest timestamp included in a period query."""
    if period == "today":
        return _today_cutoff()
    delta = _PERIOD_DELTA.get(period, timedelta(days=1))
    return datetime.now(timezone.utc) - delta


def _resolve_pricing(model: str) -> PricingTable:
    """Map an API model id to its pricing entry. Substring match so we
    survive Anthropic's version-suffixing (``claude-3-5-sonnet-20241022``
    → "sonnet"). Iteration order is the dict's insertion order
    (haiku, sonnet, opus); the family tokens don't appear in each
    other's names so the order is safe. Unknown / empty model falls
    back to DEFAULT_PRICING (Sonnet rates) — preferable to crashing,
    means an unknown future family gets priced as Sonnet until the
    table is updated.
    """
    lower = model.lower()
    for key, pricing in PRICING.items():
        if key in lower:
            return pricing
    return DEFAULT_PRICING


def _aggregate_by_model(
    records: Iterable[UsageRecord],
) -> tuple[ModelTotals, ...]:
    """Group an iterable of records by model and price each group.

    Cost formula: input + output use the model's direct rates; cache
    write is ×1.25 input, cache read is ×0.1 input. Returned tuple
    is sorted by cost descending so callers can show the top spender
    first (used for the per-model breakdown in the UI).
    """
    sums: dict[str, dict] = {}
    for r in records:
        agg = sums.setdefault(r.model, {
            "input": 0, "output": 0, "cw": 0, "cr": 0,
        })
        agg["input"] += r.input_tokens
        agg["output"] += r.output_tokens
        agg["cw"] += r.cache_creation_tokens
        agg["cr"] += r.cache_read_tokens

    out: list[ModelTotals] = []
    for model, agg in sums.items():
        p = _resolve_pricing(model)
        cost = (
            agg["input"] / 1_000_000 * p.input_per_mtok
            + agg["output"] / 1_000_000 * p.output_per_mtok
            + agg["cw"] / 1_000_000 * p.input_per_mtok * 1.25
            + agg["cr"] / 1_000_000 * p.input_per_mtok * 0.1
        )
        out.append(ModelTotals(
            model=model,
            input_tokens=agg["input"],
            output_tokens=agg["output"],
            cache_creation_tokens=agg["cw"],
            cache_read_tokens=agg["cr"],
            cost_usd=cost,
        ))
    out.sort(key=lambda m: m.cost_usd, reverse=True)
    return tuple(out)


class UsageRegistry:
    """Thread-safe in-memory store of UsageRecords + query helpers.

    The constructor takes no arguments — there is no DB path, no
    schema. ``record_many`` appends to the list; ``totals_changed``
    fires once per non-empty append so the UI knows to refresh.
    """

    def __init__(self) -> None:
        self.totals_changed: Event[None] = Event()
        self._records: list[UsageRecord] = []
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Write path
    # ------------------------------------------------------------------

    def record_many(self, records: Iterable[UsageRecord]) -> None:
        """Append a batch of UsageRecords. Emits ``totals_changed`` once
        at the end so the UI redraws once per batch, not once per row.
        Empty input is a no-op (no emit, no work)."""
        batch = list(records)
        if not batch:
            return
        with self._lock:
            self._records.extend(batch)
        self.totals_changed.emit(None)

    def record(self, record: UsageRecord) -> None:
        """Single-record convenience wrapper around record_many."""
        self.record_many([record])

    # ------------------------------------------------------------------
    # Read path: iterate the in-memory list under the lock so a write
    # cannot interleave a half-applied batch into our view.
    # ------------------------------------------------------------------

    def _records_since(self, since: datetime) -> list[UsageRecord]:
        with self._lock:
            return [r for r in self._records if r.timestamp >= since]

    def _records_in_window(
        self, since: datetime, until: datetime,
    ) -> list[UsageRecord]:
        with self._lock:
            return [
                r for r in self._records
                if since <= r.timestamp <= until
            ]

    def get_totals(self, period: str) -> UsageTotals:
        """Sum tokens + recompute cost for the given rolling period.
        Backward-compatible with the SQLite version's API: same
        ``period`` strings, same ``UsageTotals`` shape.
        """
        since = _period_cutoff(period)
        per_model = _aggregate_by_model(self._records_since(since))

        totals = UsageTotals(period=period)
        for m in per_model:
            totals.input_tokens          += m.input_tokens
            totals.output_tokens         += m.output_tokens
            totals.cache_creation_tokens += m.cache_creation_tokens
            totals.cache_read_tokens     += m.cache_read_tokens
            # Recover per-class cost from the aggregated totals using
            # the same formula as _aggregate_by_model so the line items
            # add up to ModelTotals.cost_usd exactly.
            p = _resolve_pricing(m.model)
            totals.input_cost          += m.input_tokens / 1_000_000 * p.input_per_mtok
            totals.output_cost         += m.output_tokens / 1_000_000 * p.output_per_mtok
            totals.cache_creation_cost += m.cache_creation_tokens / 1_000_000 * p.input_per_mtok * 1.25
            totals.cache_read_cost     += m.cache_read_tokens / 1_000_000 * p.input_per_mtok * 0.1
        return totals

    def get_totals_by_model(self, period: str) -> tuple[ModelTotals, ...]:
        """Same time window as get_totals but keep the model dimension.
        Used by the UI's session-card breakdown line ("Sonnet $2.54 ·
        Haiku $0.13"). Sorted by cost descending.
        """
        since = _period_cutoff(period)
        return _aggregate_by_model(self._records_since(since))

    def get_session_window(
        self,
        *,
        since: datetime | None = None,
        end_time: datetime | None = None,
    ) -> SessionUsage:
        """Return a SessionUsage for the current 5-hour Anthropic block.

        Window resolution:
        - If both ``since`` and ``end_time`` are provided (caller has
          a QuotaSnapshot), use them verbatim. This makes our local
          totals track Anthropic's authoritative window edges.
        - Otherwise approximate: ``since`` defaults to "earliest record
          in the last 5 h", ``end_time`` to ``since + 5h``. This is
          the closest local approximation of Anthropic's "first request
          in this block starts the 5h timer" rule. If the DB has no
          record in the last 5h, returns a SessionUsage with
          start_time=None (caller renders "no active session").

        ``quota`` is left as None — the QuotaProvider lives in the
        platform layer and core can't import it. The wiring layer
        (``__main__.py``) replaces this with a populated QuotaSnapshot
        when one is available.
        """
        now = datetime.now(timezone.utc)

        if since is None or end_time is None:
            # Local approximation: find the earliest record in the
            # last 5 hours and treat that as the block start.
            five_h_ago = now - timedelta(hours=_SESSION_WINDOW_HOURS)
            recent = self._records_since(five_h_ago)
            if not recent:
                return SessionUsage(
                    start_time=None, end_time=None,
                    by_model=(), total_cost_usd=0.0, quota=None,
                )
            block_start = min(r.timestamp for r in recent)
            block_end = block_start + timedelta(hours=_SESSION_WINDOW_HOURS)
        else:
            block_start = since
            block_end = end_time

        in_block = self._records_in_window(block_start, block_end)
        by_model = _aggregate_by_model(in_block)
        total = sum(m.cost_usd for m in by_model)

        return SessionUsage(
            start_time=block_start,
            end_time=block_end,
            by_model=by_model,
            total_cost_usd=total,
            quota=None,
        )
