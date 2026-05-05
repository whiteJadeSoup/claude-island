"""Single source of truth for the QToolTip look across the app.

Why both QSS *and* palette: Qt's tooltip styling on macOS Fusion is
flaky in unpredictable ways. The QSS rule sometimes propagates from
the application-level stylesheet, sometimes from a widget-level
stylesheet, sometimes from neither — depending on subtle interactions
with widget flags (FramelessWindowHint, WA_TranslucentBackground)
and the platform style. The palette-based path (``QToolTip.setPalette``
with ToolTipBase / ToolTipText color roles) is the lower-level fallback
that works even when QSS doesn't reach the tooltip.

Strategy: belt and braces — set BOTH:
  1. ``TOOLTIP_QSS`` appended to every widget that calls
     ``self.setStyleSheet`` AND to ``app.setStyleSheet``. Carries
     the visual extras Qt can't express via palette: border, radius,
     padding, font-size.
  2. ``apply_tooltip_palette(app)`` called once on the QApplication.
     Sets ToolTipBase / ToolTipText globally so even tooltips that
     fall through every QSS path render in the dark colour scheme.

Anyone who wants to tweak the tooltip look edits THIS file —
nowhere else.
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
}
"""

# Tooltip background / foreground for the QPalette path. Same colour
# values as the QSS so the two layers don't visually diverge if both
# happen to apply to the same tooltip.
_TT_BG = "#1e1e1e"
_TT_FG = "#e8e8e8"


def apply_tooltip_palette(app) -> None:
    """Set ToolTipBase / ToolTipText on the QApplication palette.

    Belt-and-braces companion to ``TOOLTIP_QSS``: when QSS resolution
    fails to reach a particular tooltip (a known Qt quirk on macOS
    Fusion with frameless / translucent parents), the palette is
    consulted for the bg / fg colours. Without this fallback, those
    tooltips render in the system default colours regardless of how
    many QSS rules we sprinkle.

    Pass the QApplication instance from __main__ AFTER it is
    constructed but BEFORE the first widget is shown.
    """
    from PySide6.QtGui import QColor, QPalette
    palette = app.palette()
    palette.setColor(QPalette.ColorRole.ToolTipBase, QColor(_TT_BG))
    palette.setColor(QPalette.ColorRole.ToolTipText, QColor(_TT_FG))
    app.setPalette(palette)
