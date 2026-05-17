"""Tests for HoverRow.set_phase — v3's phase-driven left-edge pulse.

What this pins:
  · Inactive phases (IDLE / ENDED) stop the running animation.
  · Active phases bind the pulse colour from lab_palette.Color.for_phase
    BEFORE flipping the animation on, so the first painted frame already
    uses the new tint.
  · A phase transition between two active phases (e.g. THINKING → TOOL_USE)
    rebinds the colour without restarting the animation — otherwise the
    pulse would visibly stutter on every Claude tool call.
  · set_running(True/False) still works for legacy bool callers, leaving
    _pulse_color untouched so green-only callers get the same pixels.

These tests construct the HoverRow alone — no full window, no snapshot
pipeline — because the contract under test lives entirely on the widget.
"""
from __future__ import annotations

import pytest

from claude_island.core.session_phase import SessionPhase
from claude_island.ui.expanded_window import HoverRow
from claude_island.ui.lab_palette import Color


@pytest.fixture
def row(qtbot):
    r = HoverRow(base_bg="#16161a")
    qtbot.addWidget(r)
    return r


class TestSetPhase:
    def test_idle_stops_animation(self, row):
        # Start the row running, then drop to IDLE.
        row.set_phase(SessionPhase.THINKING)
        assert row._running is True
        row.set_phase(SessionPhase.IDLE)
        assert row._running is False
        assert row._running_alpha == 0.0

    def test_ended_stops_animation(self, row):
        row.set_phase(SessionPhase.TOOL_USE)
        assert row._running is True
        row.set_phase(SessionPhase.ENDED)
        assert row._running is False

    def test_thinking_uses_amber(self, row):
        row.set_phase(SessionPhase.THINKING)
        assert row._running is True
        assert row._pulse_color == Color.amber

    def test_tool_use_uses_phosphor(self, row):
        row.set_phase(SessionPhase.TOOL_USE)
        assert row._pulse_color == Color.phosphor

    def test_waiting_approval_uses_red_warm(self, row):
        row.set_phase(SessionPhase.WAITING_APPROVAL)
        assert row._pulse_color == Color.red_warm

    def test_compacting_uses_amber_dim(self, row):
        row.set_phase(SessionPhase.COMPACTING)
        assert row._pulse_color == Color.amber_dim

    def test_active_to_active_rebinds_colour_without_restart(self, row):
        """THINKING → TOOL_USE should swap the tint but keep the
        animation running.  Catches the regression where set_phase calls
        set_running(False) + set_running(True) and visibly stutters the
        pulse between phases."""
        row.set_phase(SessionPhase.THINKING)
        anim = row._running_anim
        assert anim.state() == anim.State.Running
        assert row._pulse_color == Color.amber

        row.set_phase(SessionPhase.TOOL_USE)
        # Tint moved …
        assert row._pulse_color == Color.phosphor
        # … but the animation kept spinning (didn't get torn down).
        assert row._running is True
        assert anim.state() == anim.State.Running

    def test_inactive_to_active_starts_animation(self, row):
        assert row._running is False
        row.set_phase(SessionPhase.THINKING)
        assert row._running is True
        assert row._running_anim.state() == row._running_anim.State.Running


class TestSetRunningBackwardsCompat:
    def test_set_running_true_keeps_default_green_colour(self, row):
        """Legacy callers that only pass a bool should get exactly the
        same pixels as before (bright green pulse).  v3's set_phase
        rebinds _pulse_color; set_running must not touch it so bool
        callers don't accidentally inherit a previous set_phase tint."""
        # Start with a phase that rebinds the colour …
        row.set_phase(SessionPhase.TOOL_USE)
        assert row._pulse_color == Color.phosphor
        # … then drop it via set_running(False).  set_running doesn't
        # reset _pulse_color (it's purely an on/off flag); the contract
        # is "if you only ever call set_running, you get the default".
        row.set_running(False)
        assert row._running is False
        # A fresh row that only ever sees set_running keeps the default:
        fresh = HoverRow(base_bg="#16161a")
        fresh.set_running(True)
        assert fresh._pulse_color == fresh._RUNNING_COLOR

    def test_set_running_idempotent(self, row):
        row.set_running(True)
        row.set_running(True)
        assert row._running is True
        row.set_running(False)
        row.set_running(False)
        assert row._running is False
