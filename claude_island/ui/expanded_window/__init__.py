"""Expanded panel — the floating window that opens below the capsule.

This package replaces the original 6958-line single-file
``ui/expanded_window.py``. Stage 1 of the split is a pure
no-op: the entire module content has moved into ``window.py``
unchanged, and this ``__init__`` re-exports every name that any
external module (production code or test) imported from the
old flat module.

Subsequent stages will extract self-contained pieces into sibling
modules (formatting helpers → ``format.py``, atomic widgets →
``widgets.py``, row composite → ``row.py``, the standalone composite
widgets → ``session_detail_popup.py`` and ``add_provider_dialog.py``).
At every stage this ``__init__`` keeps re-exporting the same surface
so external imports stay byte-identical with the original module.

Why a re-export shim instead of asking callers to update their
imports: the test suite (3441-line ``test_expanded_window.py`` + 6
sibling files) reaches into ~25 underscore-prefixed symbols
(``_CopyableIdLabel``, ``_STYLE_SEP`` etc). Forcing them all to
update with each extraction would turn a mechanical split into a
sprawling rewrite. The shim keeps the diff focused on movement,
not import-chasing.
"""
from __future__ import annotations

# Public API.
from .window import (
    ExpandedWindow,
    HoverRow,
    SessionDetailPopup,
)

# Re-export PySide6 names that tests monkeypatch against the
# expanded_window namespace (e.g. ``monkeypatch.setattr(
# "claude_island.ui.expanded_window.QApplication.activeWindow", ...)``).
# Worked under the flat-module layout because window.py's
# ``from PySide6.QtWidgets import QApplication`` populated the module
# attribute; preserved here so the same test paths keep working.
from PySide6.QtWidgets import QApplication  # noqa: F401

# Underscore-prefixed names that one or more external modules (prod
# or test) import directly. Enumerated by walking every
# ``from claude_island.ui.expanded_window import ...`` and
# ``from .expanded_window import ...`` statement in the repo. Adding
# a new external consumer of a private name requires adding it here
# too — until the underlying symbol has a clearer home in one of the
# extracted modules.
from .window import (  # noqa: F401  (re-exports)
    _AddProviderDialog,
    _BAR_GREEN,
    _BAR_RED,
    _BAR_STALE,
    _BAR_YELLOW,
    _BG_HOVER_SINGLE,
    _BG_PRESSED,
    _BG_SINGLE,
    _CopyableIdLabel,
    _ElidingLabel,
    _GROUP_OUTLINE_COLOR,
    _HoverRevealRow,
    _MODEL_COLOR_FALLBACK,
    _PANEL_W,
    _ROW_HEIGHT,
    _ROW_PAD_H,
    _RowStatusGlyph,
    _STYLE_AGE,
    _STYLE_COST_DEFAULT,
    _STYLE_COST_HIGH,
    _STYLE_NAME,
    _STYLE_SEP,
    _STYLE_TEXT_LINK,
    _STYLE_TITLE,
    _aggregate_per_model_for_display,
    _elide_path_segments,
    _fmt_money,
    _quota_color,
    _resolve_model_color,
    _resolve_model_short_name,
    _row_status_text,
    _transcript_path_for_display,
)

__all__ = [
    "ExpandedWindow",
    "HoverRow",
    "SessionDetailPopup",
]
