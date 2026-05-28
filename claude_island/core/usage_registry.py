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

from reactivex.subject import Subject

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
# AnthropicProvider's /api/oauth/usage when the caller passes it in.
_SESSION_WINDOW_HOURS = 5

# Rolling windows, not calendar periods — "monthly" is the trailing
# 30 days, not the current calendar month. Avoids month-boundary edge
# cases at the cost of a small UI-label ambiguity.
#
# "5h" is a rolling spend window (last 5 hours), distinct from the
# provider-specific quota window. The two often overlap but conceptually
# differ: spend = "what did I run in the last 5 hours" (cross-provider,
# JSONL-derived); quota = "how much of THIS provider's 5h bucket is
# used" (provider-specific, anchored to that provider's reset clock).
# Putting both in the period selector lets the user compare 5h spend
# alongside daily/weekly/monthly without conflating with quota.
_PERIOD_DELTA: dict[str, timedelta] = {
    "5h":      timedelta(hours=5),
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
    """Map an API model id to its pricing entry.

    Length-descending substring match so the most-specific key wins:
    "MiniMax-M2.7-highspeed" matches its own entry before the shorter
    "MiniMax-M2.7", which matches before the family token "sonnet" /
    "opus" / "haiku" (which appear in dirty Anthropic ids like
    ``claude-3-5-sonnet-20241022``).

    Comparison is case-insensitive on both sides so the table can keep
    readable mixed-case keys ("MiniMax-M2.7") while still matching
    lowercased model ids.

    Unknown / empty model → DEFAULT_PRICING (Sonnet rates). Preferable
    to crashing — an unknown future family gets priced as Sonnet until
    the table is updated. The cost will be visibly off for non-Sonnet
    families (MiniMax under-priced ~10×, etc), so add new entries
    promptly when a new family appears.
    """
    lower = model.lower()
    for key, pricing in sorted(PRICING.items(), key=lambda kv: -len(kv[0])):
        if key.lower() in lower:
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
            + agg["cw"] / 1_000_000 * p.cw_rate()
            + agg["cr"] / 1_000_000 * p.cr_rate()
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
        # Reactivex Subject — on_next(None) synchronously notifies
        # subscribers on the calling thread.
        self.totals_changed: Subject[None] = Subject()
        self._records: list[UsageRecord] = []
        # Dedup keyed by Anthropic ``message.id``. One API response is
        # spread across N JSONL lines (one per content block: text +
        # each tool_use), and every one of those lines repeats the same
        # ``usage`` payload. Without this set, a response with 5 blocks
        # is counted 5×. Records whose message_id is None bypass dedup.
        self._seen_message_ids: set[str] = set()
        # Per-uuid inverted index. Built incrementally inside
        # ``record_many`` (post-dedup) so per-session queries
        # (``get_session_summary`` / ``get_session_per_model`` /
        # ``get_latest_model``) read O(N_uuid) instead of O(N_records).
        # Holds *references* to the same UsageRecord objects in
        # ``_records`` — no value copy, just a second list of
        # references; memory cost ≈ 8 bytes × records.
        #
        # INVARIANT: ``sum(len(v) for v in _by_uuid.values()) ==
        # len(_records)`` after any record_many call. Enforced by
        # ``test_by_uuid_index_invariant_after_record_many`` and a
        # private ``_assert_index_invariant`` helper used by tests.
        # Append-only: uuids are stable for the lifetime of a session
        # (set when the JSONL file is created, never renamed), so we
        # never need to move/remove entries — purely additive.
        self._by_uuid: dict[str, list[UsageRecord]] = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Write path
    # ------------------------------------------------------------------

    def record_many(self, records: Iterable[UsageRecord]) -> None:
        """Append a batch of UsageRecords, dropping duplicates of any
        ``message.id`` we have already accepted. Emits
        ``totals_changed`` once at the end (and only if at least one
        record actually made it past dedup, so a batch that's 100 %
        duplicates is a no-op for the UI).

        Records with message_id=None bypass dedup — these come from
        legacy transcript rows that don't expose the API id; better to
        risk a rare over-count than drop them.
        """
        batch_in = list(records)
        if not batch_in:
            return
        with self._lock:
            kept: list[UsageRecord] = []
            for r in batch_in:
                mid = r.message_id
                if mid is None:
                    kept.append(r)
                    self._by_uuid.setdefault(r.session_uuid, []).append(r)
                    continue
                if mid in self._seen_message_ids:
                    continue
                self._seen_message_ids.add(mid)
                kept.append(r)
                # Index AFTER dedup decision so duplicates don't double-
                # count in per-session queries either. Critical: this
                # line and the kept.append above must stay paired —
                # whatever lands in _records MUST also land in _by_uuid.
                self._by_uuid.setdefault(r.session_uuid, []).append(r)
            if not kept:
                return
            self._records.extend(kept)
        self.totals_changed.on_next(None)

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

        v4c (2026-05): also reports ``request_count`` — the number of
        UsageRecord rows in the window.  One row = one assistant
        message = one Claude API request, so this is the count the
        TODAY card surfaces as "N reqs".
        """
        since = _period_cutoff(period)
        records = self._records_since(since)
        per_model = _aggregate_by_model(records)

        totals = UsageTotals(period=period, request_count=len(records))
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
            totals.cache_creation_cost += m.cache_creation_tokens / 1_000_000 * p.cw_rate()
            totals.cache_read_cost     += m.cache_read_tokens / 1_000_000 * p.cr_rate()
        return totals

    def get_totals_by_model(self, period: str) -> tuple[ModelTotals, ...]:
        """Same time window as get_totals but keep the model dimension.
        Used by the UI's session-card breakdown line ("Sonnet $2.54 ·
        Haiku $0.13"). Sorted by cost descending.
        """
        since = _period_cutoff(period)
        return _aggregate_by_model(self._records_since(since))

    def get_session_summary(self, session_uuid: str) -> tuple[float, int, int]:
        """Aggregate over a single Claude Code session (transcript file).

        Returns ``(total_cost_usd, turn_count, sidechain_count)`` —
        used by the hover tooltip to show how much this specific
        session has consumed across its lifetime. Iterates the
        in-memory record list once; cheap at the user's scale.

        ``turn_count`` counts records that are NOT subagent (i.e. the
        main session's assistant turns). ``sidechain_count`` is the
        number of subagent invocations.
        """
        cost = 0.0
        turns = 0
        sides = 0
        with self._lock:
            # Read from the per-uuid inverted index so we touch only
            # this session's records, not the full _records list.
            for r in self._by_uuid.get(session_uuid, ()):
                p = _resolve_pricing(r.model)
                cost += (
                    r.input_tokens / 1_000_000 * p.input_per_mtok
                    + r.output_tokens / 1_000_000 * p.output_per_mtok
                    + r.cache_creation_tokens / 1_000_000 * p.cw_rate()
                    + r.cache_read_tokens / 1_000_000 * p.cr_rate()
                )
                if r.is_sidechain:
                    sides += 1
                else:
                    turns += 1
        return cost, turns, sides

    def get_session_token_rate(
        self,
        session_uuid: str,
        *,
        window_s: int = 60,
    ) -> int | None:
        """v4c Phase 3b: return the session's token-per-minute rate
        over the last ``window_s`` seconds, or None when there's no
        usage in the window.

        Formula: ``(sum_tokens_in_window / window_s) * 60``.  Tokens
        counted = input + output (cache tokens excluded — they're a
        prompt-level cost, not a "generating right now" signal).

        Returns None when:
          - no UsageRecord for ``session_uuid`` (session has never
            produced a turn)
          - all records are older than ``window_s`` (session went idle)

        Lock-protected scan of the per-uuid inverted index — cheap at
        the user's scale (≤ a few hundred records per session).
        """
        if not session_uuid:
            return None
        cutoff = datetime.now(timezone.utc) - timedelta(seconds=window_s)
        total_tokens = 0
        with self._lock:
            for r in self._by_uuid.get(session_uuid, ()):
                if r.timestamp < cutoff:
                    continue
                total_tokens += r.input_tokens + r.output_tokens
        if total_tokens <= 0:
            return None
        # Rate over the FULL window — yields stable values even when
        # the most recent record landed at t=window-1s (instead of
        # extrapolating from a tiny span).  Reads as "average over
        # last minute" rather than "instantaneous rate at last turn".
        return int(total_tokens * 60 / window_s)

    def get_sidechain_totals(self, period: str) -> tuple[int, float]:
        """Return (sidechain_request_count, sidechain_cost_usd) for the period.

        Counts only UsageRecord rows where ``is_sidechain=True`` — i.e. records
        that originated from a subagent transcript.  Used by ``spendDetail()``
        to populate the "↳ incl. N subagent reqs · $X" line on the TODAY card.
        Cost is recomputed from tokens × pricing (same formula as get_totals)
        so price-table updates retroactively apply.
        """
        since = _period_cutoff(period)
        records = self._records_since(since)
        count = 0
        cost = 0.0
        for r in records:
            if not r.is_sidechain:
                continue
            count += 1
            p = _resolve_pricing(r.model)
            cost += (
                r.input_tokens / 1_000_000 * p.input_per_mtok
                + r.output_tokens / 1_000_000 * p.output_per_mtok
                + r.cache_creation_tokens / 1_000_000 * p.cw_rate()
                + r.cache_read_tokens / 1_000_000 * p.cr_rate()
            )
        return count, cost

    def get_session_per_model(self, session_uuid: str) -> tuple[ModelTotals, ...]:
        """Per-model aggregation for a single transcript file.

        Same shape as :meth:`get_totals_by_model` but filtered by
        ``session_uuid`` instead of a rolling time period. Used by the
        right-click detail popup's TOKENS section to show one row per
        model with its own cost + token breakdown. Empty tuple when no
        records exist for that uuid.
        """
        with self._lock:
            # Snapshot a list copy so the caller can iterate without
            # holding the lock; reading from the inverted index gives
            # us O(N_uuid) instead of scanning the full _records list.
            rs = list(self._by_uuid.get(session_uuid, ()))
        return _aggregate_by_model(rs)

    def get_latest_model(self, session_uuid: str) -> str | None:
        """Return the model id from the most recent UsageRecord for
        ``session_uuid``. Used by the row chip to show "what model is
        this session *currently* using" rather than "what model has
        the highest cumulative cost over the session's lifetime"
        (which was misleading for sessions that had switched models
        mid-lifecycle — the old expensive model outranked the cheap
        current one)."""
        with self._lock:
            best_ts = None
            best_model = None
            # Inverted-index read — only this session's records, not
            # the whole list.
            for r in self._by_uuid.get(session_uuid, ()):
                # Skip synthetic records — /compact summaries etc
                # would bias the chip toward "<synthetic>".
                if (r.model or "").startswith("<"):
                    continue
                if best_ts is None or r.timestamp > best_ts:
                    best_ts = r.timestamp
                    best_model = r.model
        return best_model

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

        ``quota`` is left as None — the ProviderEngine lives in the
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
