"""Lightweight in-process metrics — counters + timings + frozen snapshot.

Why this exists
---------------
Perf changes that don't ship instrumentation can't be validated. The
2026-05-26 ``a96a26b`` perf commit (later reverted) shipped a snapshot
cache claiming a perf win, but with zero cache-hit/miss visibility a
reviewer had no way to confirm — and the regression bugs it carried
went undetected in production telemetry too, because there was no
production telemetry. This module is the minimum primitive that lets
future perf changes be measured before, during, and after.

Design constraints
------------------
* **Pure-Python, stdlib-only.** Lives in ``core/`` and obeys the
  no-UI / no-platform import rule. No prometheus, no opentelemetry —
  those are heavy and pull in network deps; we just need a few hundred
  bytes of counters under a single lock.
* **Thread-safe by default.** Hot-path callers run on the Snapshotter
  worker thread, the HookServer request threads, the JsonlParser
  backfill pool, and the Qt main thread. ``incr`` / ``observe`` lock
  internally; callers don't need to coordinate.
* **Frozen snapshot for dedup.** ``snapshot()`` returns an immutable
  ``MetricsSnapshot``; UI surfaces can subscribe to a periodic
  publish + ``distinct_until_changed`` without writing custom
  comparators, same pattern the rest of the project uses.
* **Lightweight by design — bounded memory.** Each timing keeps n /
  sum_ms / max_ms (constant per name); we deliberately do *not* hold
  histograms or sample lists. The trade-off: no p50/p99 from this
  layer. If a hotspot ever needs percentiles, swap in a windowed
  reservoir at THAT name (not globally).
* **Single source of truth.** Module-level ``metrics`` singleton —
  exactly the same pattern as ``world`` in ``snapshot.py``. Tests
  isolate via ``reset_for_testing()`` called from an autouse fixture.

Naming convention
-----------------
``<subsystem>.<noun>.<verb>`` for counters
(e.g. ``snap.build.count``, ``usage.record.added``).

``<subsystem>.<noun>.duration_ms`` for timings
(e.g. ``snap.build.duration_ms``, ``jsonl.parse.duration_ms``).

Pick the lowest cardinality that still answers the question. Avoid
unbounded-tag patterns like ``hook.event.<random_uuid>`` — that's how
metrics libraries blow up memory in production.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping


@dataclass(frozen=True, slots=True)
class TimingStats:
    """Read-only snapshot of one timing channel.

    ``avg_ms`` is computed at snapshot time from ``sum_ms / n`` rather
    than stored — it stays consistent with the underlying counters and
    avoids float drift across many small increments. ``n == 0`` ⇒
    ``avg_ms == 0.0`` (no division-by-zero leak through to consumers)."""

    n: int
    sum_ms: float
    max_ms: float

    @property
    def avg_ms(self) -> float:
        return self.sum_ms / self.n if self.n > 0 else 0.0


@dataclass(frozen=True, slots=True)
class MetricsSnapshot:
    """Immutable point-in-time view of every counter + timing.

    Frozen + slot'd so ``__eq__`` is structural and works as a dedup
    key, matching the project's snapshot conventions in
    ``core.snapshot``.

    ``counters`` and ``timings`` are ``MappingProxyType`` views over
    fresh dict copies — callers get stable iteration without holding
    the underlying Metrics lock, and cannot mutate the registry by
    accident.
    """

    counters: Mapping[str, int]
    timings: Mapping[str, TimingStats]

    def format_summary(self) -> str:
        """Multi-line human-readable dump for ``--doctor`` / log
        summary. Sorted by name so successive dumps diff cleanly."""
        lines: list[str] = []
        if self.counters:
            lines.append("counters:")
            for k in sorted(self.counters):
                lines.append(f"  {k:<40s} {self.counters[k]:>12d}")
        if self.timings:
            lines.append("timings:")
            for k in sorted(self.timings):
                t = self.timings[k]
                lines.append(
                    f"  {k:<40s} n={t.n:>8d}  "
                    f"avg={t.avg_ms:>8.2f}ms  max={t.max_ms:>8.2f}ms"
                )
        return "\n".join(lines) if lines else "(no metrics recorded)"


class Metrics:
    """Thread-safe counter + timing registry. Lazy creation per name.

    Single global instance is ``metrics`` at module bottom; that's the
    one production code should use. Constructor is public only so
    tests can build isolated registries when they don't want to share
    the global.
    """

    def __init__(self) -> None:
        self._counters: dict[str, int] = {}
        # Per-name (n, sum_ms, max_ms) tuples stored as a mutable list
        # so increments are in-place (avoiding tuple-realloc per call).
        self._timings: dict[str, list] = {}
        self._lock = threading.Lock()

    def incr(self, name: str, n: int = 1) -> None:
        """Add ``n`` to the named counter. Lazy-creates the entry."""
        with self._lock:
            self._counters[name] = self._counters.get(name, 0) + n

    def observe(self, name: str, value_ms: float) -> None:
        """Record one timing sample (in milliseconds). Updates n, sum,
        and max for the named timing channel.

        Why milliseconds (not seconds): every consumer reads these as
        latency strings, all expressed in ms. Storing in ms means
        the human-readable summary doesn't have to multiply by 1000
        per line.
        """
        with self._lock:
            entry = self._timings.get(name)
            if entry is None:
                self._timings[name] = [1, value_ms, value_ms]
            else:
                entry[0] += 1
                entry[1] += value_ms
                if value_ms > entry[2]:
                    entry[2] = value_ms

    def snapshot(self) -> MetricsSnapshot:
        """Build an immutable snapshot of the current state.

        Takes the lock briefly to copy the underlying dicts, then
        releases it before constructing TimingStats / MappingProxyType
        wrappers. The view returned is safe to iterate without
        coordination — the registry can keep accumulating while the
        caller reads.
        """
        with self._lock:
            counters_copy = dict(self._counters)
            timings_copy = {
                k: TimingStats(n=v[0], sum_ms=v[1], max_ms=v[2])
                for k, v in self._timings.items()
            }
        return MetricsSnapshot(
            counters=MappingProxyType(counters_copy),
            timings=MappingProxyType(timings_copy),
        )

    def reset_for_testing(self) -> None:
        """Wipe all counters + timings. Called by the autouse fixture
        in ``tests/conftest.py`` so test order doesn't matter.

        Production code MUST NOT call this — there's no use case for
        resetting metrics mid-run, and any caller doing so will silently
        corrupt the periodic-summary deltas a doctor / log consumer
        is computing."""
        with self._lock:
            self._counters.clear()
            self._timings.clear()


# Module-level singleton. Importers do
# ``from claude_island.core.metrics import metrics`` and use
# ``metrics.incr(...)`` / ``metrics.observe(...)`` / ``metrics.snapshot()``.
metrics = Metrics()
