"""Tests for the in-memory UsageRegistry.

Source of truth is JSONL transcripts; the registry just holds parsed
UsageRecords in memory and answers aggregation queries on demand. So
these tests have no DB / schema / migration concerns — every test
exercises pure Python list filtering, grouping, and pricing.

Coverage:
- B4 (carried over): record_many emits totals_changed exactly once
- T*: get_totals over each rolling period
- M1-M2: per-model breakdown
- W1-W5: 5h session-window edge cases
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from claude_island.core.models import UsageRecord
from claude_island.core.usage_registry import UsageRegistry


def _record(
    *,
    when: datetime | None = None,
    model: str = "claude-sonnet-4-5",
    input_tokens: int = 100,
    output_tokens: int = 50,
    cache_creation_tokens: int = 0,
    cache_read_tokens: int = 0,
    project_path: str = "proj",
    session_uuid: str = "sess",
    message_id: str | None = None,
) -> UsageRecord:
    return UsageRecord(
        timestamp=when or datetime.now(timezone.utc),
        project_path=project_path,
        session_uuid=session_uuid,
        model=model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_creation_tokens=cache_creation_tokens,
        cache_read_tokens=cache_read_tokens,
        message_id=message_id,
    )


@pytest.fixture
def registry():
    return UsageRegistry()


# --------------------------------------------------------------------------
# B4: write path emits exactly one totals_changed per batch
# --------------------------------------------------------------------------

def test_record_many_emits_totals_changed_exactly_once(registry):
    """A 50-record batch must trigger one redraw, not 50."""
    seen: list[None] = []
    registry.totals_changed.subscribe(lambda _: seen.append(None))
    registry.record_many([_record() for _ in range(50)])
    assert len(seen) == 1


def test_record_many_with_empty_batch_is_noop(registry):
    """Empty input → no emit, no growth in the in-memory list."""
    seen: list[None] = []
    registry.totals_changed.subscribe(lambda _: seen.append(None))
    registry.record_many([])
    assert seen == []
    assert registry._records == []


def test_dedup_drops_repeated_message_ids(registry):
    """One Anthropic API response is spread across N JSONL lines (one
    per content block: text + each tool_use), and every line repeats
    the same ``usage`` payload. The registry must keep only the first
    record per ``message.id``; otherwise an N-block response is billed
    N×. This was a real ~5× over-count on the test machine."""
    seen: list[None] = []
    registry.totals_changed.subscribe(lambda _: seen.append(None))

    # 5 records sharing one msg id (simulating a 5-block response)
    # plus 1 with a different msg id (a separate response).
    repeated = [
        _record(model="claude-opus-4-7", input_tokens=1, output_tokens=1000)
            for _ in range(5)
    ]
    repeated = [
        UsageRecord(**{**r.__dict__, "message_id": "msg_aaa"})
        for r in repeated
    ]
    other = UsageRecord(
        **{**_record(model="claude-opus-4-7", input_tokens=1,
                     output_tokens=2000).__dict__,
           "message_id": "msg_bbb"}
    )
    registry.record_many(repeated + [other])

    # Only 2 records survive: the first msg_aaa + the msg_bbb.
    assert len(registry._records) == 2
    assert registry._records[0].message_id == "msg_aaa"
    assert registry._records[1].message_id == "msg_bbb"
    # And totals_changed fired exactly once for the surviving batch.
    assert len(seen) == 1


def test_dedup_allows_records_without_message_id(registry):
    """Records with message_id=None bypass dedup — legacy transcript
    rows that don't expose the API id still count, even if duplicates
    happen to slip in."""
    legacy = [
        _record(model="claude-sonnet-4-5", input_tokens=10, output_tokens=20)
        for _ in range(3)
    ]  # all message_id=None by default
    registry.record_many(legacy)
    assert len(registry._records) == 3


def test_dedup_persists_across_batches(registry):
    """A duplicate message_id arriving in a later batch is also dropped —
    matters when JsonlParser flushes one batch per file and the same
    msg id appears in a sibling subagent transcript."""
    base = _record(model="claude-opus-4-7", input_tokens=1, output_tokens=1000)
    rec = UsageRecord(**{**base.__dict__, "message_id": "msg_xxx"})
    registry.record_many([rec])
    registry.record_many([rec])  # second batch: same msg id
    assert len(registry._records) == 1


def test_record_single_wraps_record_many(registry):
    """``record(rec)`` is a one-call helper."""
    seen: list[None] = []
    registry.totals_changed.subscribe(lambda _: seen.append(None))
    registry.record(_record())
    assert len(registry._records) == 1
    assert len(seen) == 1


# --------------------------------------------------------------------------
# F3: per-uuid inverted index (_by_uuid)
#
# These tests pin down the invariant that the inverted index stays in
# strict 1:1 correspondence with _records (after dedup), and that the
# per-session query methods now read from the index rather than scanning
# the full list. Existing tests above already cover the user-visible
# behaviour of those queries — these tests guard the implementation
# detail so a future refactor that breaks the index is caught early.
# --------------------------------------------------------------------------


def _assert_index_invariant(reg: UsageRegistry) -> None:
    """The fundamental invariant after every record_many call:
    everything that landed in _records also landed in _by_uuid (and
    nothing else did). Counts must match exactly — drift either way
    is a bug we need to catch immediately."""
    indexed = sum(len(v) for v in reg._by_uuid.values())
    assert indexed == len(reg._records), (
        f"_by_uuid drifted from _records: indexed={indexed}, "
        f"records={len(reg._records)}"
    )


def test_by_uuid_index_invariant_after_record_many(registry):
    """One batch with mixed uuids → index must mirror _records exactly."""
    registry.record_many([
        _record(session_uuid="A"), _record(session_uuid="A"),
        _record(session_uuid="B"),
    ])
    _assert_index_invariant(registry)
    assert len(registry._by_uuid["A"]) == 2
    assert len(registry._by_uuid["B"]) == 1


def test_by_uuid_index_skips_dedup_dropped_records(registry):
    """A record dropped by message_id dedup must NOT enter the index —
    otherwise per-session queries would re-introduce the double-count
    that dedup exists to prevent."""
    base = _record(session_uuid="A", model="claude-opus-4-7")
    rec = UsageRecord(**{**base.__dict__, "message_id": "msg_dup"})
    registry.record_many([rec, rec, rec])  # two duplicates of one id
    _assert_index_invariant(registry)
    assert len(registry._by_uuid["A"]) == 1


def test_by_uuid_index_persists_dedup_across_batches(registry):
    """Same dedup discipline across separate record_many calls — the
    second batch's duplicate must be filtered from BOTH _records AND
    _by_uuid."""
    base = _record(session_uuid="A")
    rec = UsageRecord(**{**base.__dict__, "message_id": "msg_xxx"})
    registry.record_many([rec])
    registry.record_many([rec])
    _assert_index_invariant(registry)
    assert len(registry._by_uuid["A"]) == 1


def test_by_uuid_index_records_without_message_id_are_indexed(registry):
    """Records with message_id=None bypass dedup (legacy transcript
    rows). They must still enter the index so per-session queries
    see them — otherwise legacy sessions would silently report $0."""
    legacy = [_record(session_uuid="L") for _ in range(3)]  # mid=None
    registry.record_many(legacy)
    _assert_index_invariant(registry)
    assert len(registry._by_uuid["L"]) == 3


def test_by_uuid_index_accumulates_across_record_many_calls(registry):
    """Multiple record_many calls for the same uuid append to the same
    bucket; the index never resets between batches."""
    registry.record_many([_record(session_uuid="A")])
    registry.record_many([_record(session_uuid="A"), _record(session_uuid="A")])
    _assert_index_invariant(registry)
    assert len(registry._by_uuid["A"]) == 3


def test_session_summary_unaffected_by_other_sessions_records(registry):
    """Inject 100 records under session B; querying A must traverse
    only A's bucket. We can't directly observe traversal cost in a
    unit test, but a behavioural proxy is: A's result equals what it
    would be if B's records didn't exist at all."""
    a_record = _record(session_uuid="A", input_tokens=1000, output_tokens=500)
    b_records = [
        _record(session_uuid="B", input_tokens=10_000, output_tokens=5_000)
        for _ in range(100)
    ]
    registry.record_many([a_record] + b_records)
    cost_a, turns_a, _ = registry.get_session_summary("A")
    assert turns_a == 1
    # Sonnet rates: 1000 in × $3/Mtok + 500 out × $15/Mtok = $0.0105
    assert cost_a == pytest.approx(1000 / 1_000_000 * 3 + 500 / 1_000_000 * 15)


def test_get_latest_model_unaffected_by_other_sessions(registry):
    """latest_model for session A must not be perturbed by session B's
    records, even if B's records are newer."""
    older = datetime(2026, 5, 1, 10, 0, tzinfo=timezone.utc)
    newer = datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc)
    registry.record_many([
        _record(session_uuid="A", model="claude-opus-4-7", when=older),
        _record(session_uuid="B", model="claude-sonnet-4-5", when=newer),
    ])
    # B's record is newer, but A's latest must still be opus.
    assert "opus" in registry.get_latest_model("A").lower()
    assert "sonnet" in registry.get_latest_model("B").lower()


def test_unknown_uuid_returns_empty_without_keyerror(registry):
    """Querying a uuid that has never been recorded must return empty
    results, not raise KeyError. This is the boot-time path: UI may
    query before any JSONL has been parsed."""
    registry.record_many([_record(session_uuid="A")])
    assert registry.get_session_summary("UNKNOWN") == (0.0, 0, 0)
    assert registry.get_session_per_model("UNKNOWN") == ()
    assert registry.get_latest_model("UNKNOWN") is None


def test_by_uuid_does_not_scan_full_records(registry, monkeypatch):
    """Behavioural proof that per-session queries read the inverted
    index, not _records: replace _records with a sentinel that would
    raise on iteration; queries should still succeed because they
    don't touch _records anymore."""
    registry.record_many([_record(session_uuid="A")])

    class _Explode:
        def __iter__(self):
            raise AssertionError("query touched _records instead of _by_uuid")

    monkeypatch.setattr(registry, "_records", _Explode())
    # All three per-uuid queries must complete without touching _records.
    registry.get_session_summary("A")
    registry.get_session_per_model("A")
    registry.get_latest_model("A")


# --------------------------------------------------------------------------
# T1-T2: get_totals over rolling periods
# --------------------------------------------------------------------------

def test_get_totals_today_aggregates_across_records(registry):
    registry.record_many([
        _record(input_tokens=10, output_tokens=20),
        _record(input_tokens=30, output_tokens=40),
    ])
    t = registry.get_totals("today")
    assert t.input_tokens == 40
    assert t.output_tokens == 60


def test_get_totals_excludes_records_outside_period(registry):
    """A record from 8 days ago must not show up in 'weekly'."""
    eight_days_ago = datetime.now(timezone.utc) - timedelta(days=8)
    registry.record_many([
        _record(when=eight_days_ago, input_tokens=999),
        _record(input_tokens=1),  # now
    ])
    t = registry.get_totals("weekly")
    assert t.input_tokens == 1


def test_get_totals_5h_window_excludes_older_records(registry):
    """The "5h" period is a rolling 5-hour window for cross-provider
    spend — distinct from the per-provider quota window. A record from
    6 hours ago must not appear; one from 4 hours ago must."""
    six_h_ago = datetime.now(timezone.utc) - timedelta(hours=6)
    four_h_ago = datetime.now(timezone.utc) - timedelta(hours=4)
    registry.record_many([
        _record(when=six_h_ago, input_tokens=999),   # too old
        _record(when=four_h_ago, input_tokens=42),   # in-window
    ])
    t = registry.get_totals("5h")
    assert t.input_tokens == 42


# --------------------------------------------------------------------------
# M1-M2: per-model breakdown
# --------------------------------------------------------------------------

def test_totals_by_model_returns_each_model_priced(registry):
    """Two distinct models → tuple ordered by cost desc, each priced
    via the model's PRICING entry."""
    registry.record_many([
        _record(model="claude-sonnet-4-5",
                input_tokens=1_000_000, output_tokens=0),  # $3 input
        _record(model="claude-haiku-4-5",
                input_tokens=1_000_000, output_tokens=0),  # $1 input
    ])
    rows = registry.get_totals_by_model("today")
    assert len(rows) == 2
    assert rows[0].cost_usd > rows[1].cost_usd
    assert "sonnet" in rows[0].model.lower()
    assert "haiku" in rows[1].model.lower()
    assert abs(rows[0].cost_usd - 3.0) < 1e-6


def test_totals_by_model_empty_returns_empty_tuple(registry):
    assert registry.get_totals_by_model("today") == ()


def test_session_summary_aggregates_one_sessions_records(registry):
    """get_session_summary returns (cost, turns, sidechain) for one
    transcript file. Records from other sessions are ignored."""
    registry.record_many([
        UsageRecord(**{**_record(model="claude-opus-4-7",
                                  input_tokens=1, output_tokens=1000).__dict__,
                       "session_uuid": "sess-a",
                       "message_id": "m1", "is_sidechain": False}),
        UsageRecord(**{**_record(model="claude-opus-4-7",
                                  input_tokens=2, output_tokens=2000).__dict__,
                       "session_uuid": "sess-a",
                       "message_id": "m2", "is_sidechain": True}),
        UsageRecord(**{**_record(model="claude-opus-4-7",
                                  input_tokens=99, output_tokens=99).__dict__,
                       "session_uuid": "sess-b",   # belongs to another session
                       "message_id": "m3"}),
    ])
    cost, turns, sides = registry.get_session_summary("sess-a")
    # 2 records for sess-a: 1 turn + 1 sidechain. cost is opus-priced.
    expected = (1/1e6*5 + 1000/1e6*25) + (2/1e6*5 + 2000/1e6*25)
    assert abs(cost - expected) < 1e-6
    assert turns == 1
    assert sides == 1


def test_session_summary_unknown_session_returns_zero(registry):
    cost, turns, sides = registry.get_session_summary("nope")
    assert (cost, turns, sides) == (0.0, 0, 0)


def test_session_per_model_splits_one_session_by_model(registry):
    """P1: one session with two models → tuple has ModelTotals for
    each. The detail popup uses this to render one row per model."""
    registry.record_many([
        UsageRecord(**{**_record(model="claude-sonnet-4-5",
                                  input_tokens=1_000_000, output_tokens=0).__dict__,
                       "session_uuid": "sess-x", "message_id": "m1"}),
        UsageRecord(**{**_record(model="claude-haiku-4-5",
                                  input_tokens=1_000_000, output_tokens=0).__dict__,
                       "session_uuid": "sess-x", "message_id": "m2"}),
        UsageRecord(**{**_record(model="claude-sonnet-4-5",
                                  input_tokens=99, output_tokens=99).__dict__,
                       "session_uuid": "other-sess", "message_id": "m3"}),
    ])
    rows = registry.get_session_per_model("sess-x")
    assert len(rows) == 2
    # Sorted by cost desc — Sonnet ($3 input rate) > Haiku ($1).
    assert "sonnet" in rows[0].model.lower()
    assert "haiku" in rows[1].model.lower()
    # The other-sess record must NOT appear here.
    assert all(r.input_tokens >= 1_000_000 for r in rows)


def test_session_per_model_unknown_session_returns_empty(registry):
    """P2: no records for that uuid → empty tuple."""
    assert registry.get_session_per_model("nope") == ()


def test_session_per_model_respects_message_id_dedup(registry):
    """P3: same msg.id ingested twice → still one logical record →
    per_model totals don't double-count."""
    base = _record(model="claude-opus-4-7", input_tokens=10, output_tokens=20)
    rec = UsageRecord(**{**base.__dict__,
                         "session_uuid": "uuid-y",
                         "message_id": "m-once"})
    registry.record_many([rec])
    registry.record_many([rec])  # duplicate batch
    rows = registry.get_session_per_model("uuid-y")
    assert len(rows) == 1
    assert rows[0].input_tokens == 10  # NOT 20


def test_opus_pricing_matches_anthropic_5_25_per_mtok(registry):
    """Regression for the bug a third-party tracker exposed: we had
    Opus at $15/$75 (legacy 3.x / 4.0/4.1 rates) but Anthropic dropped
    it to $5/$25 starting with Opus 4.5 (still current at 4.7). The
    real-data sample below was reverse-engineered from the third-party
    UI and matches Anthropic's official pricing page exactly.

    1 turn of Opus 4.7 with:
      input  21
      output 26,223
      cw     746,695   (×1.25 input rate)
      cr     11,406,712 (×0.10 input rate)
    Should price at $11.0259 (was $33.08 with the legacy rate).
    """
    registry.record_many([_record(
        model="claude-opus-4-7",
        input_tokens=21,
        output_tokens=26_223,
        cache_creation_tokens=746_695,
        cache_read_tokens=11_406_712,
    )])
    rows = registry.get_totals_by_model("today")
    assert len(rows) == 1
    # Tolerate tiny floating-point drift; the third-party showed $11.0258
    assert abs(rows[0].cost_usd - 11.0259) < 0.01


# --------------------------------------------------------------------------
# MiniMax pricing: regression for the "MiniMax-M2.7 priced as Sonnet"
# bug. Before the multi-provider PRICING table, _resolve_pricing fell
# through to DEFAULT (Sonnet $3/$15) for any non-Anthropic model id,
# inflating MiniMax cost ~10×. These tests pin the new behaviour.
# --------------------------------------------------------------------------

def test_minimax_m2_7_priced_at_paygo_rates(registry):
    """1M input + 1M output of MiniMax-M2.7 must price at $0.30 + $1.20
    = $1.50, not Sonnet's $3 + $15 = $18 (10× over). Cache_read is
    M2.7's documented $0.06/Mtok, distinct from the M2.5 $0.03 rate."""
    registry.record_many([_record(
        model="MiniMax-M2.7",
        input_tokens=1_000_000,
        output_tokens=1_000_000,
        cache_read_tokens=1_000_000,
    )])
    rows = registry.get_totals_by_model("today")
    assert len(rows) == 1
    # 0.30 + 1.20 + 0.06 (M2.7 cache_read) = 1.56
    assert abs(rows[0].cost_usd - 1.56) < 0.0001


def test_minimax_m2_5_uses_distinct_cache_read_rate(registry):
    """M2.5 cache_read is $0.03 (= 0.1 × input), M2.7 is $0.06. Make
    sure the right entry is picked even though input/output match."""
    registry.record_many([_record(
        model="MiniMax-M2.5",
        input_tokens=0,
        output_tokens=0,
        cache_read_tokens=1_000_000,
    )])
    rows = registry.get_totals_by_model("today")
    assert abs(rows[0].cost_usd - 0.03) < 0.0001


def test_minimax_highspeed_variant_doubles_io_keeps_cache(registry):
    """-highspeed SKUs are 2× input/output, identical cache rates.
    Length-descending lookup must pick the longer "-highspeed" key
    before the shorter "MiniMax-M2.7" parent entry."""
    registry.record_many([_record(
        model="MiniMax-M2.7-highspeed",
        input_tokens=1_000_000,
        output_tokens=1_000_000,
    )])
    rows = registry.get_totals_by_model("today")
    # 0.60 + 2.40 = 3.00
    assert abs(rows[0].cost_usd - 3.00) < 0.0001


def test_minimax_m_wildcard_falls_back_to_m2_7_rates(registry):
    """The MiniMax Coding-Plan API returns ``model_name: "MiniMax-M*"``
    as a literal wildcard. We treat it as M2.7 (the current flagship,
    only model named in MiniMax's own Claude Code setup docs)."""
    registry.record_many([_record(
        model="MiniMax-M*",
        input_tokens=1_000_000,
        cache_read_tokens=1_000_000,
    )])
    rows = registry.get_totals_by_model("today")
    # 0.30 (input) + 0.06 (M2.7 cache_read) = 0.36
    assert abs(rows[0].cost_usd - 0.36) < 0.0001


def test_resolve_pricing_length_descending_priority():
    """Direct test of _resolve_pricing: the most-specific key wins.
    Without length-descending iteration, "MiniMax-M2" would match
    "MiniMax-M2.7-highspeed" first (insertion order) and price the
    -highspeed SKU at parent-family rates."""
    from claude_island.core.usage_registry import _resolve_pricing
    p = _resolve_pricing("MiniMax-M2.7-highspeed")
    assert p.input_per_mtok == 0.60
    assert p.output_per_mtok == 2.40

    p = _resolve_pricing("MiniMax-M2.7")
    assert p.input_per_mtok == 0.30
    assert p.cache_read_per_mtok == 0.06

    # Anthropic dirty id still routes to the family token
    p = _resolve_pricing("claude-3-5-sonnet-20241022")
    assert p.input_per_mtok == 3.0


def test_deepseek_v4_pro_priced_at_promo_rates(registry):
    """DeepSeek V4 Pro is the proxy-via-Anthropic-format model exposed
    at api.deepseek.com/anthropic. Pricing matches the Model Details
    page (75% promo applied): $0.435 input, $0.87 output, $0.003625
    cache hit. Regression: before the deepseek.py provider module
    existed, this id fell through to Sonnet's $3 / $15 (~7× over)."""
    registry.record_many([_record(
        model="deepseek-v4-pro",
        input_tokens=1_000_000,
        output_tokens=1_000_000,
        cache_read_tokens=1_000_000,
    )])
    rows = registry.get_totals_by_model("today")
    # 0.435 (input) + 0.87 (output) + 0.003625 (cache_read) = 1.308625
    assert abs(rows[0].cost_usd - 1.308625) < 0.0001


def test_deepseek_v4_flash_priced_distinctly_from_pro(registry):
    """Flash and Pro are distinct keys in the registry. The longer
    "deepseek-v4-pro" key wins for that exact id; "deepseek-v4-flash"
    must NOT collide with it."""
    registry.record_many([_record(
        model="deepseek-v4-flash",
        input_tokens=1_000_000,
        output_tokens=1_000_000,
    )])
    rows = registry.get_totals_by_model("today")
    # 0.14 (input) + 0.28 (output) = 0.42
    assert abs(rows[0].cost_usd - 0.42) < 0.0001


def test_pricing_table_cache_rate_fallbacks():
    """When cache_read/write are None, fall back to Anthropic's
    standard ratios (1.25× / 0.1× input). When set, the explicit
    value wins. Both branches matter — Anthropic uses None (ratio),
    MiniMax-M2.7 uses explicit ($0.06)."""
    from claude_island.core.models import PricingTable
    p_anth = PricingTable(input_per_mtok=3.0, output_per_mtok=15.0)
    assert p_anth.cw_rate() == pytest.approx(3.75)   # 3.0 × 1.25
    assert p_anth.cr_rate() == pytest.approx(0.30)   # 3.0 × 0.1

    p_mm = PricingTable(0.30, 1.20, cache_read_per_mtok=0.06)
    assert p_mm.cr_rate() == pytest.approx(0.06)     # explicit wins
    assert p_mm.cw_rate() == pytest.approx(0.375)    # falls back to 0.30 × 1.25


# --------------------------------------------------------------------------
# W1-W5: 5h session window
# --------------------------------------------------------------------------

def test_session_window_empty_returns_nones(registry):
    """W1: zero records → SessionUsage with None timestamps."""
    su = registry.get_session_window()
    assert su.start_time is None
    assert su.end_time is None
    assert su.by_model == ()
    assert su.total_cost_usd == 0.0
    assert su.quota is None


def test_session_window_single_recent_record(registry):
    """W2: one record 1h ago → block_start at that record, end +5h."""
    one_h_ago = datetime.now(timezone.utc) - timedelta(hours=1)
    registry.record_many([_record(when=one_h_ago, input_tokens=42)])

    su = registry.get_session_window()
    assert su.start_time is not None
    assert abs((su.start_time - one_h_ago).total_seconds()) < 1
    assert (su.end_time - su.start_time) == timedelta(hours=5)
    assert len(su.by_model) == 1
    assert su.by_model[0].input_tokens == 42


def test_session_window_excludes_records_older_than_5h(registry):
    """W3 (this is the bug B fix): a record from 8h ago must NOT be
    counted in the current 5h block. The earlier "first block-start
    walking back through the entire DB if there's no 5h gap" algorithm
    pulled in records since the very first record ever, inflating the
    session $ by orders of magnitude on heavy users.

    Records inside the last 5h *do* all belong to the current block
    (Anthropic's rule: first request in a block opens its 5h timer,
    subsequent requests within that 5h count to the same block). So
    here both the 4h and 30-min records belong to the current block;
    only the 8h-old record is excluded.
    """
    now = datetime.now(timezone.utc)
    registry.record_many([
        _record(when=now - timedelta(hours=8),    input_tokens=999_999),  # excluded
        _record(when=now - timedelta(hours=4),    input_tokens=10),       # in block
        _record(when=now - timedelta(minutes=30), input_tokens=1),        # in block
    ])

    su = registry.get_session_window()
    # block_start = earliest record in last 5h = 4h ago.
    assert abs((su.start_time - (now - timedelta(hours=4))).total_seconds()) < 1
    assert len(su.by_model) == 1
    # 10 + 1, the 8h record is correctly excluded.
    assert su.by_model[0].input_tokens == 11


def test_session_window_continuous_block(registry):
    """W4: 4h-old + 1h-old → both fall inside the current 5h, both count."""
    now = datetime.now(timezone.utc)
    registry.record_many([
        _record(when=now - timedelta(hours=4), input_tokens=10),
        _record(when=now - timedelta(hours=1), input_tokens=20),
    ])

    su = registry.get_session_window()
    assert abs((su.start_time - (now - timedelta(hours=4))).total_seconds()) < 1
    assert su.by_model[0].input_tokens == 30


def test_session_window_no_recent_records_returns_no_session(registry):
    """W5: every record is ≥ 5h old → no active session."""
    now = datetime.now(timezone.utc)
    registry.record_many([
        _record(when=now - timedelta(hours=10)),
        _record(when=now - timedelta(hours=8)),
    ])
    su = registry.get_session_window()
    # Local approximation: no record in last 5h → empty session.
    assert su.start_time is None
    assert su.end_time is None


def test_session_window_explicit_bounds_anchor_to_endpoint(registry):
    """When the caller passes (since, end_time) — typically derived
    from QuotaSnapshot.five_hour_resets_at — the registry uses those
    bounds verbatim instead of the local approximation. This makes the
    local $ track Anthropic's authoritative window edges."""
    now = datetime.now(timezone.utc)
    # Caller's bounds: window started 30 min ago, ends 4.5h from now
    # (i.e. resets_at = now + 4.5h, since = resets_at - 5h).
    since = now - timedelta(minutes=30)
    end_time = since + timedelta(hours=5)

    registry.record_many([
        _record(when=now - timedelta(hours=2), input_tokens=999),  # outside
        _record(when=now - timedelta(minutes=5), input_tokens=7),  # inside
    ])
    su = registry.get_session_window(since=since, end_time=end_time)

    assert su.start_time == since
    assert su.end_time == end_time
    assert len(su.by_model) == 1
    assert su.by_model[0].input_tokens == 7


# ── _seen_message_ids cap (P1-a) ───────────────────────────────────────


def test_seen_message_ids_evicts_oldest_when_cap_exceeded(monkeypatch):
    """When _seen_message_ids reaches the cap, the FIFO-oldest entry
    falls off and ``usage.dedup.evict`` increments. Verified at a
    deliberately tiny cap so the test stays fast.

    The eviction property protects long-running island instances from
    unbounded growth — observed 27K msg ids after 5 months of use; without
    the cap a multi-year instance would reach hundreds of MB of dedup
    state. See SEEN_MESSAGE_IDS_CAP rationale in usage_registry.py.
    """
    from claude_island.core import metrics, usage_registry

    monkeypatch.setattr(usage_registry, "SEEN_MESSAGE_IDS_CAP", 3)
    reg = usage_registry.UsageRegistry()

    # Insert 5 unique msg ids into a 3-slot cap. msg-0 and msg-1 should
    # fall off; msg-2, msg-3, msg-4 should remain.
    reg.record_many([_record(message_id=f"msg-{i}") for i in range(5)])

    assert len(reg._seen_message_ids) == 3
    assert "msg-0" not in reg._seen_message_ids
    assert "msg-1" not in reg._seen_message_ids
    assert "msg-4" in reg._seen_message_ids

    # 2 evictions reported through the metrics primitive.
    assert metrics.metrics.snapshot().counters.get("usage.dedup.evict") == 2


def test_seen_message_ids_no_eviction_below_cap(monkeypatch):
    """Inserting fewer ids than the cap must never trigger an
    eviction (counter stays absent — lazy creation = no entry until
    something happens)."""
    from claude_island.core import metrics, usage_registry
    monkeypatch.setattr(usage_registry, "SEEN_MESSAGE_IDS_CAP", 100)
    reg = usage_registry.UsageRegistry()
    reg.record_many([_record(message_id=f"msg-{i}") for i in range(50)])
    assert "usage.dedup.evict" not in metrics.metrics.snapshot().counters


def test_seen_message_ids_dedup_still_works_post_eviction(monkeypatch):
    """The classic dedup contract (one msg id never double-counts) must
    survive the cap. As long as the duplicate arrives BEFORE the
    original gets evicted, dedup catches it — this is the in-window
    case the cap is sized to handle."""
    from claude_island.core import usage_registry
    monkeypatch.setattr(usage_registry, "SEEN_MESSAGE_IDS_CAP", 100)
    reg = usage_registry.UsageRegistry()
    reg.record_many([_record(message_id="msg-A")])
    # Duplicate arrives — cap is far from full, so dedup catches it.
    reg.record_many([_record(message_id="msg-A")])
    assert len(reg._records) == 1
