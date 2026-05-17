"""Smoke tests for the v3 lab-console design tokens.

These tests pin the **contract** the token module offers to the rest of
the UI layer, not the exact hex values (those will iterate during the v3
rollout; the contract — "every phase has a tint, FontStack exposes a
QSS-ready stack" — must not).

The expensive thing this catches is **API drift** across slices: if a
later slice renames Color.amber → Color.gold without grepping every
caller, this test breaks at the seam rather than at a random surface
in production.
"""
from __future__ import annotations

import pytest

from claude_island.core.session_phase import SessionPhase
from claude_island.ui.lab_palette import (
    Color,
    FontStack,
    WAVE_BAR_COUNT,
    WAVE_DELAY_STEP_MS,
    WAVE_MAX_PCT,
    WAVE_MIN_PCT,
    WAVE_PERIOD_MS,
)


class TestColorTokens:
    def test_all_named_tones_are_hex_strings(self):
        """Every token is a 7-char lowercase hex string. Pins the
        invariant that callers can drop tokens straight into QSS
        ``background: #...`` without worrying about colour-space
        conversion."""
        for name in (
            "ink", "surface", "surface_hi", "surface_warm",
            "rule", "rule_bright", "rule_active",
            "paper", "paper_dim", "paper_faint", "paper_deep",
            "amber", "amber_dim",
            "phosphor", "phosphor_dim",
            "red_warm", "red_warm_dim",
        ):
            v = getattr(Color, name)
            assert isinstance(v, str), f"{name} not a string: {v!r}"
            assert v.startswith("#") and len(v) == 7, f"{name}: {v!r}"
            assert v == v.lower(), f"{name} not lowercase: {v!r}"
            int(v[1:], 16)  # raises ValueError if not valid hex

    def test_for_phase_covers_every_session_phase(self):
        """Pin: every SessionPhase value resolves to *some* tint —
        no silent KeyError when a new phase is added without
        updating the mapping."""
        for phase in SessionPhase:
            tint = Color.for_phase(phase)
            assert isinstance(tint, str)
            assert tint.startswith("#")

    def test_for_phase_uses_distinct_tints_for_distinct_phases(self):
        """Two phases that conceptually mean different things must not
        collide — that would erase the phase signal from the UI.
        Tests every pair (n*(n-1)/2 comparisons; cheap for 6 phases)."""
        tints = {p: Color.for_phase(p) for p in SessionPhase}
        # IDLE and ENDED are allowed to be similar (both "inactive") but
        # not identical — the row must still distinguish them visually.
        seen: dict[str, SessionPhase] = {}
        for phase, tint in tints.items():
            assert tint not in seen, (
                f"{phase} shares tint {tint} with {seen[tint]} "
                "— phase signal collapses"
            )
            seen[tint] = phase

    def test_for_phase_thinking_uses_amber(self):
        """Pin the concrete mapping used by the prototype so a refactor
        of Color.for_phase that swaps the table can't silently flip
        thinking → green without a test failing."""
        assert Color.for_phase(SessionPhase.THINKING) == Color.amber

    def test_for_phase_tool_use_uses_phosphor(self):
        assert Color.for_phase(SessionPhase.TOOL_USE) == Color.phosphor

    def test_for_phase_waiting_uses_red_warm(self):
        assert Color.for_phase(SessionPhase.WAITING_APPROVAL) == Color.red_warm


class TestFontStack:
    def test_mono_stack_lists_jetbrains_mono_first(self):
        """JetBrains Mono is the preferred face — Qt walks the comma list
        left-to-right and stops at the first family present on the OS.
        Putting the fallback first would mask installed JetBrains Mono."""
        assert FontStack.mono_stack.startswith("'JetBrains Mono'")

    def test_mono_stack_ends_with_generic_monospace_keyword(self):
        """Final fallback is the CSS keyword ``monospace``, which Qt
        resolves to a guaranteed-present family on every platform.
        Without this a font-poor system would fall back to the proportional
        default font, breaking the whole v3 aesthetic."""
        assert FontStack.mono_stack.rstrip().endswith("monospace")

    def test_mono_first_is_non_empty(self):
        assert FontStack.mono_first == "JetBrains Mono"

    def test_mono_fallback_is_platform_appropriate(self):
        """Platform-aware fallback exists; exact value is platform-dependent
        (Menlo / Consolas / DejaVu Sans Mono).  All three are guaranteed
        to exist on their respective OSes — see fonts.py for the same
        pattern."""
        import sys
        if sys.platform == "darwin":
            assert FontStack.mono_fallback == "Menlo"
        elif sys.platform == "win32":
            assert FontStack.mono_fallback == "Consolas"
        else:
            assert FontStack.mono_fallback == "DejaVu Sans Mono"


class TestWaveParameters:
    def test_wave_period_matches_prototype_and_row_status_glyph(self):
        """1200 ms matches both the prototype's @keyframes wave and
        the existing _RowStatusGlyph._PERIOD_MS — pinned so a tweak
        in one place is forced to update the other."""
        assert WAVE_PERIOD_MS == 1200

    def test_wave_bar_count_is_five(self):
        """Five bars at 1/5 period offset reads as a travelling wave;
        three bars at 1/3 offset reads as 'all wiggling in sync'."""
        assert WAVE_BAR_COUNT == 5

    def test_wave_delay_step_evenly_divides_period(self):
        assert WAVE_DELAY_STEP_MS * WAVE_BAR_COUNT == WAVE_PERIOD_MS

    def test_wave_height_bounds_are_well_ordered(self):
        assert 0 < WAVE_MIN_PCT < WAVE_MAX_PCT <= 1.0
