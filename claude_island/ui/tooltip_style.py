"""Single source of truth for the QToolTip QSS rule.

Why this module exists: Qt's stylesheet resolution for QToolTip is
NOT what you'd expect. When a widget calls ``self.setStyleSheet(...)``,
that widget's stylesheet effectively shadows the application-level
``app.setStyleSheet(...)`` for all tooltips popping inside that
widget's tree — even if the widget's own stylesheet has no QToolTip
rule. The result: tooltips on the capsule (no ``self.setStyleSheet``)
inherit the dark app-level rule and look correct, while tooltips on
the expanded panel / recents drawer / popups (which all DO call
``self.setStyleSheet``) silently fall back to the platform default
(translucent macOS-native, hard to read on dark UI).

The fix that actually holds across surfaces is to **append this QSS
to every per-widget stylesheet** in addition to setting it at app
level. Both paths reference the same constant, so the rule stays
identical and the user's hover-tooltip looks the same wherever they
land. Edit this string ONCE to change every tooltip in the app.

Usage:
    from claude_island.ui.tooltip_style import TOOLTIP_QSS
    self.setStyleSheet("MyWidget { ... }" + TOOLTIP_QSS)

Lives in the UI layer because it's a Qt-specific styling string with
no core / platform concept behind it.
"""
from __future__ import annotations

TOOLTIP_QSS = """
QToolTip {
    color: #e8e8e8;
    background-color: #1e1e1e;
    border: 1px solid #3a3a3a;
    padding: 6px 8px;
    border-radius: 4px;
    font-size: 12px;
    opacity: 240;
}
"""
