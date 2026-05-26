"""Tests for core/formatting.py — UI text formatters used both by
render code and by per-surface compute selectors (F4).

Two callers depend on these returning *exactly* the same string for
the same input + current time bucket. These tests pin down each
quantisation boundary so a future tweak to "now" / band thresholds
shows up here AND triggers a renamed test (helps keep the dedup
contract in sync with the display contract).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from claude_island.core.formatting import fmt_money, fmt_started


# ── fmt_started ────────────────────────────────────────────────────────

class TestFmtStarted:

    def test_none_returns_em_dash(self):
        assert fmt_started(None) == "—"

    def test_under_5s_returns_now(self):
        """The "<5s → now" boundary is the dedup window for active
        sessions: any last_activity tick within the last 5s
        produces the same string, so dedup correctly skips no-op
        renders. Microsecond JSONL writes hit this case."""
        now = datetime.now(timezone.utc)
        assert fmt_started(now) == "now"
        assert fmt_started(now - timedelta(seconds=4)) == "now"

    def test_5s_crosses_into_seconds_string(self):
        """At exactly 5s the formatter switches to "Ns ago". This
        is the boundary that does trigger a re-render: dedup will
        see "now" → "5s ago" and let the snap through."""
        now = datetime.now(timezone.utc)
        result = fmt_started(now - timedelta(seconds=6))
        assert result.endswith("s ago")

    def test_seconds_format(self):
        now = datetime.now(timezone.utc)
        assert fmt_started(now - timedelta(seconds=30)).endswith("s ago")

    def test_minutes_format(self):
        now = datetime.now(timezone.utc)
        result = fmt_started(now - timedelta(minutes=5, seconds=10))
        assert result == "5m ago"

    def test_hours_format(self):
        now = datetime.now(timezone.utc)
        result = fmt_started(now - timedelta(hours=2, minutes=30))
        assert result == "2h 30m ago"

    def test_days_format(self):
        now = datetime.now(timezone.utc)
        result = fmt_started(now - timedelta(days=3, hours=5))
        assert result == "3d ago"

    def test_microsecond_jitter_within_now_window_unchanged(self):
        """The F4 invariant: micro-changes within the same band
        produce the same string. This is what enables dedup.

        Margin is 4.5s (not 4.999999s) because there's a real wall-clock
        gap between capturing ``now`` here and the fmt_started call's
        own ``datetime.now()`` reading. With 4.999999s, even a 2µs
        gap pushes the elapsed delta over the 5s boundary and the band
        flips to "5s ago". 4.5s leaves comfortable headroom while still
        proving the invariant.
        """
        now = datetime.now(timezone.utc)
        a = fmt_started(now - timedelta(microseconds=100))
        b = fmt_started(now - timedelta(seconds=4, microseconds=500_000))
        assert a == "now"
        assert b == "now"


# ── fmt_money ──────────────────────────────────────────────────────────

class TestFmtMoney:

    def test_zero_uses_milli_band(self):
        """< $0.01 uses 3-decimal precision so a brand-new session
        with $0.001 cumulative cost still shows a non-zero indicator."""
        assert fmt_money(0.0) == "$0.000"
        assert fmt_money(0.005) == "$0.005"

    def test_small_band_two_decimals(self):
        """< $10 keeps cents."""
        assert fmt_money(0.01) == "$0.01"
        assert fmt_money(5.42) == "$5.42"
        assert fmt_money(9.99) == "$9.99"

    def test_medium_band_no_decimals(self):
        """< $1000 rounds to dollars."""
        assert fmt_money(10.0) == "$10"
        assert fmt_money(123.45) == "$123"
        assert fmt_money(999.4) == "$999"

    def test_large_band_kilo_suffix(self):
        """≥ $1000 collapses to "$X.XK"."""
        assert fmt_money(1000.0) == "$1.0K"
        assert fmt_money(1234.5) == "$1.2K"
        assert fmt_money(50_000.0) == "$50.0K"

    def test_band_boundaries_change_string(self):
        """Crossing a band MUST change the string — dedup must let
        these through so the user sees the format change."""
        assert fmt_money(9.99) != fmt_money(10.0)       # $9.99 → $10
        assert fmt_money(999.4) != fmt_money(1000.0)    # $999 → $1.0K

    def test_within_band_quantises_dedup_input(self):
        """The F4 invariant: changes within the same band leave the
        string unchanged. Ticking from $5.00 → $5.005 is invisible."""
        assert fmt_money(5.00) == fmt_money(5.0049)     # both round to "$5.00"
        assert fmt_money(123) == fmt_money(123.4)       # both "$123"
