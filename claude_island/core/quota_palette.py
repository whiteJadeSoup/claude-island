"""Single source of truth for 5h quota severity thresholds + colours.

Why this exists: pre-extraction the capsule pill (`ui/capsule_window.py`)
and the expanded panel's TODAY card (`ui/expanded_window.py`) each
defined their own thresholds AND their own bar colours. Capsule used
warn=70 / critical=90; panel used warn=60 / critical=85. At 86 % the
two surfaces gave **contradictory severity readings to the user**:
the capsule mini-bar showed amber ("warn"), the panel bar showed red
("critical"). Same snapshot value, different visual story.

This module collapses both axes into one place:

    pct → severity (ok | warn | critical) → bar colour (hex)

Every surface that renders quota state pulls from here. Adding a new
surface = ``from claude_island.core.quota_palette import quota_bar_color``
— no risk of drifting away from the canonical scheme.

Threshold choice (warn=70, critical=85): 90 % was too late on the 5 h
window (≈30 min headroom); 60 % was too early (alarm fatigue). 70 %
gives a comfortable amber band before things get tight; 85 % matches
how the panel already escalated to red and leaves a 15 % critical
window — long enough to defer big tasks, short enough to actually
notice.

Architecture: lives in ``core/`` because it has no Qt / no platform_
deps and is consumed by both render code and ``compute(snap)``
selectors (the F4 dedup path) — same rationale as ``core/formatting.py``.
"""
from __future__ import annotations

from typing import Literal

# Threshold percentages (inclusive lower bound). A reading of exactly
# WARN_PCT is already "warn"; exactly CRITICAL_PCT is "critical".
WARN_PCT = 70
CRITICAL_PCT = 85

# Bar fill colours per severity. Hex strings (not Qt QColor) so this
# module stays Qt-free — UI callers wrap with QColor() at the edge if
# they need the typed object.
BAR_GREEN = "#4ade80"   # Tailwind green-400 — "plenty of runway"
BAR_AMBER = "#facc15"   # Tailwind yellow-400 — "watch your step"
BAR_RED   = "#ef4444"   # Tailwind red-500 — "defer big tasks"
# Stale wins over any pct band: surfacing "I don't trust this" before
# "how full is it" prevents alarming (or reassuring) on cached data
# that may be 15+ minutes old.
BAR_STALE = "#6b7280"   # Tailwind gray-500


Severity = Literal["ok", "warn", "critical"]


def quota_severity(pct: float) -> Severity:
    """Bucket a 5 h quota percentage into one of three severity bands.

    Pure function, no I/O. Used by render code (to pick a colour) and
    by surface ``compute(snap)`` selectors (to put the severity into
    the dedup key so a green→amber transition triggers re-render even
    when the rounded pct number didn't change)."""
    if pct >= CRITICAL_PCT:
        return "critical"
    if pct >= WARN_PCT:
        return "warn"
    return "ok"


def quota_bar_color(pct: float, *, stale: bool = False) -> str:
    """Pick the progress-bar fill colour for a 5 h quota reading.

    Stale always wins — see module docstring. Otherwise dispatches on
    ``quota_severity(pct)`` so threshold and colour stay in lock-step.
    """
    if stale:
        return BAR_STALE
    return _SEVERITY_TO_BAR_COLOR[quota_severity(pct)]


_SEVERITY_TO_BAR_COLOR: dict[Severity, str] = {
    "ok":       BAR_GREEN,
    "warn":     BAR_AMBER,
    "critical": BAR_RED,
}
