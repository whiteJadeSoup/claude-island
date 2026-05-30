"""Unit tests for claude_island.core.metrics."""
from __future__ import annotations

import threading

import pytest

from claude_island.core.metrics import Metrics, MetricsSnapshot, TimingStats


@pytest.fixture
def m() -> Metrics:
    """Isolated registry per test — avoids the global singleton."""
    return Metrics()


# ── counters ────────────────────────────────────────────────────────────

def test_counter_lazy_creates_on_first_incr(m: Metrics):
    m.incr("snap.build.count")
    snap = m.snapshot()
    assert snap.counters["snap.build.count"] == 1


def test_counter_accumulates(m: Metrics):
    for _ in range(5):
        m.incr("usage.record.added")
    m.incr("usage.record.added", n=10)
    snap = m.snapshot()
    assert snap.counters["usage.record.added"] == 15


def test_counter_supports_negative_increment_for_decrement(m: Metrics):
    m.incr("queue.depth", n=3)
    m.incr("queue.depth", n=-1)
    assert m.snapshot().counters["queue.depth"] == 2


def test_multiple_counters_independent(m: Metrics):
    m.incr("a")
    m.incr("b", n=7)
    snap = m.snapshot()
    assert snap.counters["a"] == 1
    assert snap.counters["b"] == 7


# ── timings ─────────────────────────────────────────────────────────────

def test_timing_first_observation(m: Metrics):
    m.observe("snap.build.duration_ms", 12.5)
    snap = m.snapshot()
    t = snap.timings["snap.build.duration_ms"]
    assert t == TimingStats(n=1, sum_ms=12.5, max_ms=12.5)
    assert t.avg_ms == 12.5


def test_timing_accumulates_n_sum_max(m: Metrics):
    for v in [1.0, 3.0, 2.0, 8.0, 5.0]:
        m.observe("x", v)
    t = m.snapshot().timings["x"]
    assert t.n == 5
    assert t.sum_ms == 19.0
    assert t.max_ms == 8.0
    assert t.avg_ms == pytest.approx(3.8)


def test_timing_avg_safe_for_zero_n():
    # Empty TimingStats — produced by snapshot if no observations yet.
    t = TimingStats(n=0, sum_ms=0.0, max_ms=0.0)
    assert t.avg_ms == 0.0  # no ZeroDivisionError


# ── snapshot semantics ──────────────────────────────────────────────────

def test_snapshot_is_immutable(m: Metrics):
    m.incr("c")
    m.observe("t", 1.0)
    snap = m.snapshot()
    # MappingProxyType raises on mutation attempts
    with pytest.raises(TypeError):
        snap.counters["c"] = 99  # type: ignore[index]
    with pytest.raises(TypeError):
        snap.timings["t"] = TimingStats(n=999, sum_ms=0, max_ms=0)  # type: ignore[index]


def test_snapshot_isolated_from_subsequent_writes(m: Metrics):
    """The view returned should be stable; later incr/observe
    on the registry must not retroactively change the snapshot."""
    m.incr("c")
    snap = m.snapshot()
    m.incr("c", n=99)  # bump after snapshot
    assert snap.counters["c"] == 1
    assert m.snapshot().counters["c"] == 100


def test_snapshot_equality_enables_dedup(m: Metrics):
    """Frozen dataclass + dict equality means MetricsSnapshot can
    drive distinct_until_changed without a custom comparator."""
    m.incr("a"); m.observe("t", 3.0)
    s1 = m.snapshot()
    s2 = m.snapshot()
    assert s1 == s2
    m.incr("a")
    s3 = m.snapshot()
    assert s1 != s3


def test_format_summary_empty(m: Metrics):
    assert m.snapshot().format_summary() == "(no metrics recorded)"


def test_format_summary_sorted_for_diffability(m: Metrics):
    m.incr("z")
    m.incr("a")
    m.incr("m")
    out = m.snapshot().format_summary()
    # Names must appear alphabetically — successive doctor dumps diff
    # cleanly when the order is stable.
    a_idx = out.index("a ")
    m_idx = out.index("m ")
    z_idx = out.index("z ")
    assert a_idx < m_idx < z_idx


# ── thread safety ──────────────────────────────────────────────────────

def test_concurrent_incr_no_lost_updates(m: Metrics):
    """8 threads × 1000 increments → exact total. The lock must
    prevent the classic non-atomic ``x = x + 1`` lost-update race."""
    N_THREADS = 8
    N_PER = 1000

    def worker():
        for _ in range(N_PER):
            m.incr("hot")

    threads = [threading.Thread(target=worker) for _ in range(N_THREADS)]
    for t in threads: t.start()
    for t in threads: t.join()

    assert m.snapshot().counters["hot"] == N_THREADS * N_PER


def test_concurrent_observe_n_consistent_with_sum(m: Metrics):
    """If observe races, n / sum / max can drift apart. Verify each
    sample contributes to all three in lockstep."""
    N_THREADS = 4
    N_PER = 500
    SAMPLE_VALUE = 2.5

    def worker():
        for _ in range(N_PER):
            m.observe("t", SAMPLE_VALUE)

    threads = [threading.Thread(target=worker) for _ in range(N_THREADS)]
    for t in threads: t.start()
    for t in threads: t.join()

    t = m.snapshot().timings["t"]
    assert t.n == N_THREADS * N_PER
    assert t.sum_ms == pytest.approx(N_THREADS * N_PER * SAMPLE_VALUE)
    assert t.max_ms == SAMPLE_VALUE


# ── reset semantics ────────────────────────────────────────────────────

def test_reset_for_testing_wipes_state(m: Metrics):
    m.incr("c"); m.observe("t", 1.0)
    m.reset_for_testing()
    snap = m.snapshot()
    assert snap.counters == {}
    assert snap.timings == {}


def test_module_singleton_is_a_metrics():
    from claude_island.core.metrics import metrics as singleton
    assert isinstance(singleton, Metrics)
