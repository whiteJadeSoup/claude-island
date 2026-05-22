"""Single source of truth for v4c visual tokens.

Why this module exists: v4c's redesign (see ``design/2026-05-island-redesign/
prototype-v4c-github.html``) settles on a low-contrast pastel palette
with sans-led typography (mono only for numbers / code / paths).
Spreading those values across ``capsule_window.py``,
``expanded_window.py``, ``recents_drawer.py`` would guarantee drift the
first time someone tweaks a colour without grepping every surface.

History:
  - v3 (lab console): dark + warm + all-mono + amber accent.
    Rejected by the user: "整体风格不喜欢，希望简洁明了."
  - v4c interim (GitHub Primer + Tailwind 400-family): high-saturation
    phase colours competed with each other; pure-black bg + bright
    white text was harsh for long sessions.  Rejected 2026-05-22.
  - v4c (current): **Catppuccin Mocha** — pastel palette with a
    warm-dark base.  Token names preserved verbatim from prior
    versions so every existing call site continues to compile.
    See ``design/2026-05-island-redesign/color-palette-research.html``
    for the migration rationale + side-by-side mockup.

What lives here:
  - colour tokens (``Color``) — surfaces, ink, rules, accents, phase tints
  - typography tokens (``FontStack``) — UI sans + monospace stacks
  - wave animation parameters (centralised so capsule + row can't drift)

What does NOT live here:
  - widget-level QSS strings (those compose tokens; composition belongs
    next to the widget so a reader sees layout + colour together)

Read order for new surfaces: import ``Color`` + ``FontStack``, compose
the QSS string at the call site, never inline hex literals.
"""
from __future__ import annotations

import sys
from dataclasses import dataclass

from claude_island.core.session_phase import SessionPhase


# ---------------------------------------------------------------------------
# Colour tokens — Catppuccin Mocha
# ---------------------------------------------------------------------------
#
# Values map onto the Catppuccin Mocha palette (catppuccin.com).  Token
# names are kept verbatim from prior versions so every existing call
# site continues to compile — only values move.  The Catppuccin name
# for each value is included in the comment so a reader can cross-
# reference the upstream palette without grepping.
#
# Reference: https://github.com/catppuccin/catppuccin/blob/main/docs/style-guide.md
@dataclass(frozen=True, slots=True)
class _ColorTokens:
    # ── surfaces (Catppuccin Mocha — warm-dark with a slight purple cast) ──
    ink:          str = "#1e1e2e"   # base    — panel bg (lifts pure-black harshness)
    surface:      str = "#181825"   # mantle  — card / row bg (one step deeper than base)
    surface_hi:   str = "#313244"   # surface0 — hover / pressed
    surface_warm: str = "#181825"   # mantle  — legacy alias for row hover

    # ── rules / dividers ──
    rule:         str = "#45475a"   # surface1
    rule_bright:  str = "#585b70"   # surface2
    rule_active:  str = "#7f849c"   # overlay1 — focus / selected outline

    # ── type tints ──
    paper:        str = "#cdd6f4"   # text     — primary (drops contrast 14.2:1 → 10.7:1)
    paper_dim:    str = "#a6adc8"   # subtext0 — secondary
    paper_faint:  str = "#7f849c"   # overlay1 — tertiary / placeholders
    paper_deep:   str = "#585b70"   # surface2 — ended / muted

    # ── status tints — pastel, so 4 phase colours can coexist without competing ──
    amber:        str = "#cba6f7"   # mauve    — thinking (was saturated purple)
    amber_dim:    str = "#74c7ec"   # sapphire — compacting (clearly cooler than mauve)
    phosphor:     str = "#a6e3a1"   # green    — tool_use / live (sage, not traffic-light)
    phosphor_dim: str = "#40a02b"   # green deep (Mocha extension) — wave fallback
    red_warm:     str = "#fab387"   # peach    — waiting (friendlier than burnt orange)
    red_warm_dim: str = "#ef9f76"   # peach deep — outline / pressed

    # ── action / status extras ──
    accent:       str = "#89b4fa"   # blue     — primary action buttons
    success:      str = "#a6e3a1"   # green    — quota OK band
    danger:       str = "#ef4444"   # red      — true red (Tailwind red-500) for the
                                    #            high-cost tier; user wanted "红色"
                                    #            not "粉红", so we step outside the
                                    #            Mocha pastel set for this one alarm.

    def for_phase(self, phase: SessionPhase) -> str:
        """Map a SessionPhase to its dominant tint.

        Phase → Catppuccin Mocha mapping (pastel set so multiple
        phases on screen at once don't compete for attention):
          IDLE             → overlay1 grey
          THINKING         → mauve     (deliberative)
          TOOL_USE         → green     (executing — production action)
          WAITING_APPROVAL → peach     (friendly attention, not panic)
          COMPACTING       → sapphire  (housekeeping; cool but distinct from mauve)
          ENDED            → surface2  (dimmest visible — "present but inert")
        """
        return {
            SessionPhase.IDLE:             self.rule_active,
            SessionPhase.THINKING:         self.amber,        # mauve
            SessionPhase.TOOL_USE:         self.phosphor,     # green
            SessionPhase.WAITING_APPROVAL: self.red_warm,     # peach
            SessionPhase.COMPACTING:       self.amber_dim,    # sapphire
            SessionPhase.ENDED:            self.paper_deep,
        }[phase]


Color = _ColorTokens()


# ---------------------------------------------------------------------------
# Typography
# ---------------------------------------------------------------------------
#
# v4c reverses v3's "all-mono" stance: sans for UI chrome, mono only for
# numbers / code / cwd paths.  Reads as a modern product dashboard
# (Linear / Vercel / GitHub Actions) rather than a terminal panel.
#
# Family choice is platform-aware for the same reason fonts.py does it:
# naming a family Qt doesn't have in its database triggers a slow alias
# resolution warning on first paint.  Falls back gracefully to the
# system's native sans / mono so on any platform we get sensible
# rendering even without explicit installs.
if sys.platform == "darwin":
    _FALLBACK_SANS = "-apple-system, BlinkMacSystemFont, 'SF Pro Text', 'Helvetica Neue'"
    _FALLBACK_MONO = "Menlo"
elif sys.platform == "win32":
    _FALLBACK_SANS = "'Segoe UI', system-ui"
    _FALLBACK_MONO = "Consolas"
else:
    _FALLBACK_SANS = "'Cantarell', 'Ubuntu', 'DejaVu Sans'"
    _FALLBACK_MONO = "DejaVu Sans Mono"


@dataclass(frozen=True, slots=True)
class _FontStack:
    # Sans-serif stack for UI chrome (titles, labels, button text).
    # Falls back to the platform's native UI sans if a preferred face
    # isn't installed.
    sans_stack: str = f"{_FALLBACK_SANS}, sans-serif"
    # Monospace stack for numbers, code blocks, and cwd paths.
    mono_stack: str = f"'JetBrains Mono', '{_FALLBACK_MONO}', monospace"
    # First-choice family names — used when constructing QFont objects
    # directly (where Qt expects a single string and walks the database
    # itself).
    sans_first:    str = _FALLBACK_SANS.split(",")[0].strip().strip("'")
    sans_fallback: str = _FALLBACK_SANS.split(",")[0].strip().strip("'")
    mono_first:    str = "JetBrains Mono"
    mono_fallback: str = _FALLBACK_MONO


FontStack = _FontStack()


# ---------------------------------------------------------------------------
# Wave animation parameters
# ---------------------------------------------------------------------------
#
# Mirrors prototype-v4c-github.html's wave + the existing
# _RowStatusGlyph in expanded_window.py.  Centralised so the capsule's
# mini-wave and the row's wave can't drift.
# v4c: 4 bars per prototype-v4c-github.html `.row .wave i:nth-child(1..4)`
# — reads as a piano-key equalizer rather than a 5-bar VU meter.
# Animation delays at 0 / 300 / 600 / 900 ms per the prototype's
# nth-child schedule, so the bars cascade left-to-right with an
# offset-quarter phase each.
WAVE_BAR_COUNT = 4            # four narrow piano-key bars
WAVE_PERIOD_MS = 1200         # full loop, linear easing
WAVE_MIN_PCT = 0.20           # min bar height (proportion of widget)
WAVE_MAX_PCT = 1.00           # max bar height
# delay schedule = period / N for N bars; the wave then reads as
# "travelling left → right" rather than "all bars in sync".
WAVE_DELAY_STEP_MS = WAVE_PERIOD_MS // WAVE_BAR_COUNT
