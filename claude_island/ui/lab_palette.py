"""Single source of truth for the v3 "lab console" visual tokens.

Why this module exists: v3's redesign (see ``design/2026-05-island-redesign/
prototype-v3.html``) introduces a coherent dark palette + monospace
typography + phase-driven status tints.  Spreading those values across
``capsule_window.py``, ``expanded_window.py``, ``recents_drawer.py`` would
guarantee drift the first time someone tweaks a colour without grepping
every surface.

What lives here:
  - colour tokens (``Color``) — surfaces, ink, rules, accents, phase tints
  - typography tokens (``FontStack``) — display + monospace stacks that
    prefer JetBrains Mono when installed and fall back gracefully

What does NOT live here:
  - widget-level QSS strings (those compose tokens; the composition belongs
    next to the widget so a reader sees layout + colour together)
  - phase → tint dispatch (``Color.for_phase`` is the only convenience —
    pure mapping, no widget state; callers that need richer routing can
    keep it co-located with the widget)

Read order for new surfaces: import ``Color`` + ``FontStack``, compose
the QSS string at the call site, never inline hex literals.
"""
from __future__ import annotations

import sys
from dataclasses import dataclass

from claude_island.core.session_phase import SessionPhase


# ---------------------------------------------------------------------------
# Colour tokens
# ---------------------------------------------------------------------------
#
# Values are mirrored from prototype-v3.html's :root block.  The HTML
# prototype is the source of truth for visual intent — when a token here
# is changed, the prototype must move in lockstep so design + impl don't
# drift.  Comments below quote the prototype line so a reader can grep
# both at once.
@dataclass(frozen=True, slots=True)
class _ColorTokens:
    # surfaces — matte near-black, never pure #000
    ink:          str = "#0e0e10"   # var(--ink)
    surface:      str = "#16161a"   # var(--surface)
    surface_hi:   str = "#1d1d22"   # var(--surface-hi)
    surface_warm: str = "#1a1816"   # var(--surface-warm) — card body, slight warmth

    # rules / dividers
    rule:         str = "#2a2a31"   # var(--rule)
    rule_bright:  str = "#3d3d46"   # var(--rule-bright)
    rule_active:  str = "#5c5c66"   # var(--rule-active)

    # type tints (warm paper white → neutral grey → faint)
    paper:        str = "#e8e3d6"   # var(--paper) — primary text
    paper_dim:    str = "#98948a"   # var(--paper-dim) — secondary
    paper_faint:  str = "#5c5a55"   # var(--paper-faint) — tertiary / labels
    paper_deep:   str = "#36352f"   # var(--paper-deep) — ended row tint

    # status tints — sparingly applied
    amber:        str = "#d4a460"   # var(--amber) — thinking / counters / readings
    amber_dim:    str = "#8a6a3e"   # var(--amber-dim) — compacting
    phosphor:     str = "#6db580"   # var(--phosphor) — tool_use / live
    phosphor_dim: str = "#466652"   # var(--phosphor-dim) — wave fallback
    red_warm:     str = "#c46a55"   # var(--red-warm) — waiting (warm, not screaming)
    red_warm_dim: str = "#7a3a30"   # var(--red-warm-dim) — outline

    def for_phase(self, phase: SessionPhase) -> str:
        """Map a SessionPhase to its dominant tint.

        Mirrors prototype-v3.html's ``.row[data-phase="..."] .strip``
        rule.  Callers paint the row strip / wave / phase label in this
        colour; status text (e.g. "thinking · turn 3") inherits the
        same tint via the meta line's ``.phase`` span.

        ENDED returns paper_deep (the dimmest visible tone) so the row
        reads as "present but inert" — fully transparent would make
        the row disappear from the rule grid which is worse.
        """
        return {
            SessionPhase.IDLE:             self.rule,
            SessionPhase.THINKING:         self.amber,
            SessionPhase.TOOL_USE:         self.phosphor,
            SessionPhase.WAITING_APPROVAL: self.red_warm,
            SessionPhase.COMPACTING:       self.amber_dim,
            SessionPhase.ENDED:            self.paper_deep,
        }[phase]


Color = _ColorTokens()


# ---------------------------------------------------------------------------
# Typography
# ---------------------------------------------------------------------------
#
# v3 commits to "all monospace" — display hierarchy comes from size +
# weight + tint, not from pairing a serif with a sans.  Reasons in the
# prototype's top comment; not re-stating here.
#
# Family choice is platform-aware for the same reason ``fonts.py`` does
# it: naming a family Qt doesn't have in its database triggers a slow
# alias resolution warning on first paint.  ``JetBrains Mono`` is the
# preferred face when installed (many devs install it for terminals);
# the fallback chain ends at a guaranteed-present native mono.
if sys.platform == "darwin":
    _FALLBACK_MONO = "Menlo"
elif sys.platform == "win32":
    _FALLBACK_MONO = "Consolas"
else:
    _FALLBACK_MONO = "DejaVu Sans Mono"


@dataclass(frozen=True, slots=True)
class _FontStack:
    # The CSS-style stack used inside QSS strings.  Qt parses ``font-family``
    # exactly like CSS: comma-separated, first available family wins.
    mono_stack: str = (
        f"'JetBrains Mono', '{_FALLBACK_MONO}', monospace"
    )
    # First-choice family name only — used when constructing QFont objects
    # directly (where Qt expects a single string and walks the database
    # itself).  Empty if JetBrains Mono isn't installed; the QFont fallback
    # path handles that case.
    mono_first: str = "JetBrains Mono"
    mono_fallback: str = _FALLBACK_MONO


FontStack = _FontStack()


# ---------------------------------------------------------------------------
# Wave animation parameters
# ---------------------------------------------------------------------------
#
# Mirrors prototype-v3.html's @keyframes wave + animation-delay schedule,
# which itself mirrors the existing ``_RowStatusGlyph`` in expanded_window.py.
# Centralised so the capsule's mini-wave and the row's wave can't drift.
WAVE_BAR_COUNT = 5            # five 1.5px bars
WAVE_PERIOD_MS = 1200         # full loop, linear easing
WAVE_MIN_PCT = 0.20           # min bar height (proportion of widget)
WAVE_MAX_PCT = 1.00           # max bar height
# delay schedule = period / N for N bars; the wave then reads as
# "travelling left → right" rather than "all bars in sync".
WAVE_DELAY_STEP_MS = WAVE_PERIOD_MS // WAVE_BAR_COUNT
