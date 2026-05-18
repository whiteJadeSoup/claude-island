"""Single source of truth for v4c "GitHub list" visual tokens.

Why this module exists: v4c's redesign (see ``design/2026-05-island-redesign/
prototype-v4c-github.html``) settles on a GitHub Primer-style palette
with Tailwind-tinted phase colours and sans-led typography (mono only
for numbers / code / paths).  Spreading those values across
``capsule_window.py``, ``expanded_window.py``, ``recents_drawer.py`` would
guarantee drift the first time someone tweaks a colour without grepping
every surface.

History:
  - v3 (lab console): dark + warm + all-mono + amber accent.
    Rejected by the user: "整体风格不喜欢，希望简洁明了."
  - v4c (current): GitHub Primer dark default, Tailwind 400-family phase
    tints, sans typography. Same Color.* token names as v3 so existing
    callers keep working — only values move.

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
# Colour tokens
# ---------------------------------------------------------------------------
#
# Values mirror prototype-v4c-github.html's :root block.  The HTML
# prototype is the source of truth for visual intent — when a token
# here is changed, the prototype must move in lockstep so design + impl
# don't drift.
#
# Token NAMES are preserved verbatim from the v3 vocabulary so every
# existing call site (capsule_window.py, expanded_window.py, etc.)
# continues to compile.  Only the VALUES move from "warm dark" (v3) to
# "GitHub Primer dark + Tailwind accents" (v4c).
@dataclass(frozen=True, slots=True)
class _ColorTokens:
    # ── surfaces (GitHub Primer dark) ──
    ink:          str = "#0d1117"   # var(--canvas)        — panel bg
    surface:      str = "#151b23"   # var(--canvas-sub)    — card / row bg
    surface_hi:   str = "#21262d"   # var(--canvas-emph)   — hover / pressed
    surface_warm: str = "#161b22"   # var(--row-hover)     — kept as alias for legacy callers

    # ── rules / dividers ──
    rule:         str = "#30363d"   # var(--border)
    rule_bright:  str = "#3d444d"   # one step lighter than rule
    rule_active:  str = "#656c76"   # focus / selected outline

    # ── type tints (GitHub Primer light-on-dark) ──
    paper:        str = "#f0f6fc"   # primary text
    paper_dim:    str = "#9198a1"   # secondary
    paper_faint:  str = "#6e7681"   # tertiary / placeholders
    paper_deep:   str = "#3d444d"   # ended / muted

    # ── status tints (Tailwind 400-family — warmer, more "product UI") ──
    # Token names preserved (amber / phosphor / red_warm) but values
    # are now Tailwind's friendly palette:
    amber:        str = "#a371f7"   # purple-400 — thinking (was warm amber)
    amber_dim:    str = "#4493f8"   # blue-400  — compacting (was darker amber)
    phosphor:     str = "#3fb950"   # green-500 — tool_use / live
    phosphor_dim: str = "#1f6431"   # green-700 — wave fallback / outline
    red_warm:     str = "#db6d28"   # orange-500 — waiting (friendly, not screaming red)
    red_warm_dim: str = "#bc4c00"   # orange-600 — outline / pressed

    # ── extras new in v4c (callers may pick these up incrementally) ──
    accent:       str = "#4493f8"   # GitHub blue — primary action buttons
    success:      str = "#3fb950"   # quota OK band
    danger:       str = "#f85149"   # high-cost cost tier

    def for_phase(self, phase: SessionPhase) -> str:
        """Map a SessionPhase to its dominant tint.

        Mirrors prototype-v4c-github.html's ``.row[data-phase="..."] .ico``
        rule.  Callers paint the row strip / wave / phase label in this
        colour; status text inherits the same tint.

        Phase → Tailwind tint mapping:
          IDLE             → rule grey
          THINKING         → purple-400 (deliberative)
          TOOL_USE         → green-500   (executing — production action)
          WAITING_APPROVAL → orange-500  (friendly attention, not panic)
          COMPACTING       → blue-400    (housekeeping)
          ENDED            → paper_deep (dimmest visible — "present but inert")
        """
        return {
            SessionPhase.IDLE:             self.rule_active,
            SessionPhase.THINKING:         self.amber,         # purple-400 in v4c
            SessionPhase.TOOL_USE:         self.phosphor,      # green-500
            SessionPhase.WAITING_APPROVAL: self.red_warm,      # orange-500
            SessionPhase.COMPACTING:       self.amber_dim,     # blue-400 in v4c
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
WAVE_BAR_COUNT = 5            # five 1.5px bars
WAVE_PERIOD_MS = 1200         # full loop, linear easing
WAVE_MIN_PCT = 0.20           # min bar height (proportion of widget)
WAVE_MAX_PCT = 1.00           # max bar height
# delay schedule = period / N for N bars; the wave then reads as
# "travelling left → right" rather than "all bars in sync".
WAVE_DELAY_STEP_MS = WAVE_PERIOD_MS // WAVE_BAR_COUNT
