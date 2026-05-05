"""Pin the unified quota severity + colour contract.

These tests guard the property the module exists to enforce:

  * thresholds (warn=70, critical=85) bucket pcts the same way for
    every surface that calls in
  * stale data ALWAYS wins over the percent-based colour
  * the colour mapping has no holes (every severity → one colour)

Pre-extraction, capsule_window used 70/90 with one colour palette
and expanded_window used 60/85 with another. At 86 % the user saw
"warn" amber on one surface and "critical" red on the other for
the same snapshot value. This module collapses both axes into a
single dispatch — these tests pin that single dispatch so a future
refactor can't quietly recreate the drift.
"""
from __future__ import annotations

import pytest

from claude_island.core.quota_palette import (
    BAR_AMBER,
    BAR_GREEN,
    BAR_RED,
    BAR_STALE,
    CRITICAL_PCT,
    WARN_PCT,
    quota_bar_color,
    quota_severity,
)


class TestQuotaSeverity:
    """``quota_severity`` is the single function every surface routes
    through to decide which severity band a pct lands in."""

    @pytest.mark.parametrize("pct,expected", [
        (0.0,                "ok"),
        (10.0,               "ok"),
        (WARN_PCT - 1,       "ok"),
        (WARN_PCT - 0.001,   "ok"),
        (WARN_PCT,           "warn"),     # boundary inclusive
        (WARN_PCT + 5,       "warn"),
        (CRITICAL_PCT - 1,   "warn"),
        (CRITICAL_PCT,       "critical"), # boundary inclusive
        (CRITICAL_PCT + 5,   "critical"),
        (100.0,              "critical"),
        (200.0,              "critical"), # over-quota stays critical
    ])
    def test_severity_buckets(self, pct, expected):
        assert quota_severity(pct) == expected


class TestQuotaBarColor:
    """``quota_bar_color`` dispatches on severity + handles the stale
    short-circuit. This is the single source for what colour a 5h
    quota reading paints — capsule mini-bar + panel inline + summary
    progress all read from here."""

    @pytest.mark.parametrize("pct,expected", [
        (0.0,            BAR_GREEN),
        (WARN_PCT - 1,   BAR_GREEN),
        (WARN_PCT,       BAR_AMBER),
        (75.0,           BAR_AMBER),
        (CRITICAL_PCT - 1, BAR_AMBER),
        (CRITICAL_PCT,   BAR_RED),
        (100.0,          BAR_RED),
    ])
    def test_color_per_pct(self, pct, expected):
        assert quota_bar_color(pct) == expected

    def test_stale_overrides_any_pct(self):
        """The stale handler must short-circuit BEFORE the threshold
        dispatch — otherwise a stale 95 % would paint red and the
        user would react to a number we already know is untrustworthy
        (cache > 15 min old)."""
        for pct in (0.0, 50.0, WARN_PCT, CRITICAL_PCT, 100.0):
            assert quota_bar_color(pct, stale=True) == BAR_STALE, (
                f"stale={pct} returned the live colour instead of grey"
            )

    def test_stale_default_false(self):
        """Default kwarg keeps the threshold path active for callers
        that haven't been wired through the staleness signal yet."""
        assert quota_bar_color(95.0) == BAR_RED


class TestSurfaceParity:
    """The whole point of this module: capsule + panel must agree
    on what colour a given pct is. These tests catch any future
    drift where one surface re-defines its own thresholds locally."""

    def test_capsule_thresholds_match_core(self):
        """The capsule re-exports WARN/CRITICAL with the same names
        it's always used. Importing them from the capsule module
        must yield the same ints as the core source — otherwise
        someone re-defined the constants locally and the cross-
        surface contract is broken."""
        from claude_island.ui.capsule_window import (
            _QUOTA_CRITICAL_THRESHOLD,
            _QUOTA_WARN_THRESHOLD,
        )
        assert _QUOTA_WARN_THRESHOLD == WARN_PCT
        assert _QUOTA_CRITICAL_THRESHOLD == CRITICAL_PCT

    def test_expanded_window_uses_core_palette(self):
        """``_quota_color`` in ``expanded_window`` must produce the
        identical colour string as ``quota_bar_color`` from core for
        every input. Anything else means the panel has a private
        threshold ladder hiding behind the alias."""
        from claude_island.ui.expanded_window import _quota_color
        for pct in (0.0, 50.0, 70.0, 80.0, 85.0, 99.0):
            assert _quota_color(pct, stale=False) == quota_bar_color(pct)
            assert _quota_color(pct, stale=True) == BAR_STALE

    def test_expanded_window_legacy_aliases_match_core(self):
        """``_BAR_GREEN`` / ``_BAR_YELLOW`` / ``_BAR_RED`` / ``_BAR_STALE``
        in ``expanded_window`` must be the same hex strings as the
        core constants. Pinned because some test files imported them
        directly pre-extraction; if a future refactor forks them, the
        capsule and panel start diverging silently."""
        from claude_island.ui.expanded_window import (
            _BAR_GREEN as panel_green,
            _BAR_RED as panel_red,
            _BAR_STALE as panel_stale,
            _BAR_YELLOW as panel_yellow,
        )
        assert panel_green  == BAR_GREEN
        assert panel_yellow == BAR_AMBER
        assert panel_red    == BAR_RED
        assert panel_stale  == BAR_STALE
