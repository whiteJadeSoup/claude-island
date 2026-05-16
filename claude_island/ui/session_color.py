"""Per-session accent colour — used by approval / question cards and
the peek slivers in StackedDecisionsPanel to give each session a
stable, distinguishable identity.

Distinct from ``_GROUP_BG_PALETTE`` in ``expanded_window.py``:
  * Group palette is a low-saturation **background** tint applied to
    multi-session group containers.
  * Session palette here is a high-saturation **accent** colour
    (left-edge bar, dot, badge) so the user can scan a stack of
    pending decisions and tell which session each one belongs to at
    a glance.

Assignment is by stable hash of ``session_uuid`` so the same session
gets the same colour across refreshes and across UI surfaces. Six
hues evenly spread around the wheel — collision possible past 6
sessions but the visual distinction is still strong enough to
disambiguate adjacent rows in the stack.
"""
from __future__ import annotations


# Six accent hues, ~60° apart on the wheel, all at similar saturation
# and lightness so no single colour visually overpowers the others.
# Ordered so adjacent palette indices (e.g. 0/1) read as distinctly
# different hues — the hash-based assignment can't avoid putting
# similarly-coloured sessions next to each other, but a wide hue gap
# between consecutive palette entries makes that the rare exception.
SESSION_ACCENT_PALETTE: tuple[str, ...] = (
    "#22c55e",   # green
    "#1d4ed8",   # blue
    "#a855f7",   # purple
    "#f59e0b",   # amber
    "#06b6d4",   # cyan
    "#ec4899",   # pink
)


def session_accent(session_uuid: str | None) -> str:
    """Stable colour pick for one session, by hash of ``session_uuid``.

    Returns the first palette entry for an empty / missing uuid so
    placeholder sessions don't crash callers; the colour just won't
    be unique among placeholders.
    """
    if not session_uuid:
        return SESSION_ACCENT_PALETTE[0]
    # ``hash()`` is process-randomised in Python 3.3+; we use a
    # deterministic alternative so the same uuid maps to the same
    # colour across process restarts — important for visual continuity
    # if the user closes and reopens the panel.
    return SESSION_ACCENT_PALETTE[
        _deterministic_hash(session_uuid) % len(SESSION_ACCENT_PALETTE)
    ]


def _deterministic_hash(s: str) -> int:
    """Tiny FNV-1a 32-bit. Not cryptographic; we just need stable %."""
    h = 0x811C9DC5
    for ch in s.encode("utf-8"):
        h ^= ch
        h = (h * 0x01000193) & 0xFFFFFFFF
    return h
