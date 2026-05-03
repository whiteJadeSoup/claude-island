from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from PySide6.QtCore import (
    QEasingCurve,
    QEvent,
    QObject,
    QPropertyAnimation,
    Qt,
    QTimer,
    Signal,
)
from PySide6.QtGui import QColor, QPainter, QPainterPath
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMenu,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from claude_island.core.models import (
    ModelTotals,
    QuotaSnapshot,
    Session,
    SessionDetails,
    SessionUsage,
    UsageTotals,
)
from .controller import IslandController


class _CopyableIdLabel(QFrame):
    """A read-only click-to-copy UUID widget.

    Displays ``display_text`` (defaults to the full uuid) followed by a
    small clipboard glyph. On click, copies the *full* uuid and briefly
    flashes "Copied" for feedback. Display vs. copy values are split so
    the inspector can show a short 8-char prefix while still copying the
    whole 36-char uuid that ``claude --resume`` needs."""

    def __init__(
        self,
        uuid: str,
        parent: QWidget | None = None,
        *,
        display_text: str | None = None,
    ) -> None:
        super().__init__(parent)
        self._uuid = uuid
        shown = display_text if display_text is not None else uuid

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)

        self._uuid_label = QLabel(shown)
        self._uuid_label.setStyleSheet(
            "color: #e8e8e8; font-size: 11px; font-family: Consolas, monospace;"
        )
        # When showing the full uuid we want wrapping; the short form
        # never wraps so this is harmless either way.
        self._uuid_label.setWordWrap(display_text is None)
        self._uuid_label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        layout.addWidget(self._uuid_label)

        # Clipboard glyph — only when we're showing a shortened form
        # (full-uuid display already implies the whole thing is the
        # affordance, no need for an extra icon). Stored on self so
        # the hover-reveal container can show/hide it on enter/leave.
        self._glyph_label: QLabel | None = None
        if display_text is not None:
            self._glyph_label = QLabel("⧉")
            self._glyph_label.setStyleSheet("color: #6b7280; font-size: 11px;")
            self._glyph_label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
            layout.addWidget(self._glyph_label)

        layout.addStretch()

        self._copied_label = QLabel("Copied")
        self._copied_label.setStyleSheet(
            "color: #4ade80; font-size: 11px; font-family: Consolas, monospace;"
        )
        self._copied_label.hide()
        layout.addWidget(self._copied_label)

        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolTip("Click to copy session ID")

    def mousePressEvent(self, event) -> None:  # type: ignore[override]
        QApplication.clipboard().setText(self._uuid)
        self._copied_label.show()
        self._uuid_label.hide()
        QTimer.singleShot(1500, self._restore)

    def _restore(self) -> None:
        self._copied_label.hide()
        self._uuid_label.show()


class _ClickToCopyLabel(QLabel):
    """QLabel that copies its text to the clipboard on click and
    flashes "Copied" for 1.5s. Cursor changes to PointingHandCursor
    so the affordance is obvious. Use for inspector-row values where
    the user expects "click anywhere on the value to copy" (path,
    branch — anywhere the value is the affordance).

    ``copy_text`` overrides what gets put on the clipboard when the
    displayed text is shorter than the canonical value (e.g. a path
    that's been visually elided)."""

    def __init__(
        self,
        text: str,
        parent: QWidget | None = None,
        *,
        copy_text: str | None = None,
    ) -> None:
        super().__init__(text, parent)
        self._copy_text = copy_text if copy_text is not None else text
        self._original_text = text
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def mousePressEvent(self, event) -> None:  # type: ignore[override]
        QApplication.clipboard().setText(self._copy_text)
        self.setText("Copied")
        QTimer.singleShot(1500, self._restore)

    def _restore(self) -> None:
        self.setText(self._original_text)


class _HoverRevealRow(QFrame):
    """Container that hides registered ``reveal`` widgets at rest and
    shows them only when the mouse hovers anywhere over the row.

    Used by the SessionDetailPopup's ID and Path rows so the inline
    ``⧉`` (copy) and ``↗`` (open folder) glyphs only appear when the
    user is actually targeting the row — the static view stays clean
    (Notion / GitHub-list pattern). Click handlers on the revealed
    widgets remain active even while hidden, so they fire correctly
    after Qt shows them mid-hover.
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._reveal: list[QWidget] = []
        self.setAttribute(Qt.WidgetAttribute.WA_Hover, True)

    def register_reveal(self, widget: QWidget) -> None:
        """Mark ``widget`` as hover-only and hide it now."""
        if widget is None:
            return
        self._reveal.append(widget)
        widget.hide()

    def enterEvent(self, event) -> None:  # type: ignore[override]
        for w in self._reveal:
            w.show()
        return super().enterEvent(event)

    def leaveEvent(self, event) -> None:  # type: ignore[override]
        for w in self._reveal:
            w.hide()
        return super().leaveEvent(event)


_PANEL_W = 320
_GAP = 6  # px gap between capsule bottom and panel top

# Bound the sessions list so a heavy user with 20+ sessions doesn't
# get a panel taller than the screen. Computed as an *exact* multiple
# of (row + gap) so the visible boundary always lands on a row edge —
# the previous 196 px value chopped the last row in half.
#
# 6 standalone rows × _ROW_HEIGHT(36) + 5 gaps × _GROUP_GAP(8) = 256 px.
# Past 6 rows the user scrolls inside this region; the USAGE block
# stays anchored below.
_SESSION_SCROLL_VISIBLE_ROWS = 6


def _claude_projects_root() -> Path:
    """Where Claude Code stores per-project transcript dirs.

    Lives in a function (not a module-level constant) so tests can
    monkey-patch it to redirect at a tmp dir without having to swap
    out ``Path.home`` globally."""
    return Path.home() / ".claude" / "projects"


class _SmoothWheelScroller(QObject):
    """Animate ``QScrollArea`` wheel events instead of jumping.

    Default Qt behaviour scrolls instantly by ~3 lines per wheel tick,
    which feels jerky on a small bounded list — the user sees a
    teleport, not a scroll. This filter intercepts wheel events on
    the scroll area's viewport, computes the new target value, and
    runs a short eased animation to it. Successive wheel ticks cancel
    the previous animation and chain from the *current* value, so
    fast scrolling still feels responsive (no queued lag).

    Tuning:
      - ``duration``: 150ms — short enough to feel instantaneous,
        long enough to read as motion. Anything > 250ms feels laggy.
      - ``OutCubic`` easing: starts fast, decelerates to a stop —
        matches how scroll inertia feels in modern OS browsers.
      - ``delta * 0.5``: each 120-degree wheel tick translates to
        ~60 pixels of scroll, ≈ 2 row heights at our 28px row size.
    """

    def __init__(self, scroll_area: "QScrollArea") -> None:
        super().__init__(scroll_area)
        self._sb = scroll_area.verticalScrollBar()
        self._anim = QPropertyAnimation(self._sb, b"value", self)
        self._anim.setDuration(150)
        self._anim.setEasingCurve(QEasingCurve.Type.OutCubic)

    def eventFilter(self, obj, event):  # type: ignore[override]
        if event.type() == QEvent.Type.Wheel:
            delta = event.angleDelta().y()
            # Chain from the in-flight animation's end value when one is
            # running, so a flurry of ticks accumulates instead of
            # cancelling each other into a near-no-op.
            if self._anim.state() == QPropertyAnimation.State.Running:
                base = int(self._anim.endValue())
            else:
                base = self._sb.value()
            target = max(
                self._sb.minimum(),
                min(self._sb.maximum(), int(base - delta * 0.5)),
            )
            self._anim.stop()
            self._anim.setStartValue(self._sb.value())
            self._anim.setEndValue(target)
            self._anim.start()
            return True
        return super().eventFilter(obj, event)

_STYLE_PANEL = """
    ExpandedWindow {
        color: white;
        font-family: 'Segoe UI', sans-serif;
    }
    QToolTip {
        color: #e8e8e8;
        background-color: #1e1e1e;
        border: 1px solid #3a3a3a;
        padding: 6px 8px;
        border-radius: 4px;
        font-size: 12px;
    }
"""
# Bare top-level properties + selector blocks in one stylesheet make
# Qt fail to parse the entire sheet (silent "Could not parse" warning,
# and the QToolTip block goes ignored — system default white tooltip
# would render the "Add a quota provider" hint as white-on-white).
# Wrapping the bare props under the ExpandedWindow selector forces a
# strict parse so the QToolTip override actually applies.
_STYLE_TITLE = "color: #888; font-size: 10px; letter-spacing: 1px;"
_STYLE_SEP = "background: #2a2a2a;"

# --------------------------------------------------------------------------
# Session row + card visual language
# --------------------------------------------------------------------------
#
# Three elements have to read well at a glance:
# 1. activity dot (●) on the left — green / yellow / gray = recent / today /
#    older, so the user doesn't have to read the time text to tell which
#    sessions are "live"
# 2. group ↔ standalone — group cards sit on a darker background (#181818)
#    than standalone rows (#1e1e1e); the contrast tells the eye "these rows
#    are bracketed together" without explicit chrome
# 3. one row per line — name on the left, age on the right, no second line;
#    rows are 36px tall so the whole list scans top-to-bottom in one motion

_BG_SINGLE = "#1e1e1e"
_BG_GROUP = "#181818"          # darker → "this is a container, not a row"
_BG_HOVER_SINGLE = "#2a2a2a"
_BG_HOVER_IN_GROUP = "#222222"  # subtle hover that keeps the card identity
_BG_PRESSED = "#333333"

# Per-group accent palette. Multi-session groups (sessions sharing a
# WT tab) get a subtle hue tint so the user can tell two adjacent
# group cards apart at a glance — without a tint they all look like
# the same generic dark container. Singletons keep _BG_SINGLE.
#
# Colours are dark + low-saturation: each is roughly the same lightness
# as _BG_GROUP (#181818 / L≈9) with a slight hue shift, so the cards
# read as "the group container, just tinted" rather than as alarming
# coloured chrome. Group → palette index is hash(group_key) % len so
# the assignment is stable across refreshes.
_GROUP_BG_PALETTE = (
    "#1d2638",   # indigo
    "#2b1d38",   # violet
    "#1d3826",   # teal-green
    "#382d1d",   # amber
    "#1d3535",   # cyan
    "#381d22",   # rose
)


def _group_bg_color(idx: int) -> str:
    """Position-based palette assignment. Walking the visible groups
    top-down and incrementing ``idx`` on each multi-session card
    guarantees adjacent cards never collide on the same colour —
    which a hash-based scheme couldn't promise (the previous
    ``hash(key) % len`` mapping landed two distinct groups on the
    same red tint when their hashes happened to mod-equal).

    The position is stable as long as the session-sort order is
    stable (it is — deterministic via ``_session_sort_key``), so
    cards keep their colour across refreshes unless a group above
    them appears / disappears. Worth the trade vs the previous
    "absolutely stable but visually broken" hash."""
    return _GROUP_BG_PALETTE[idx % len(_GROUP_BG_PALETTE)]


class HoverRow(QPushButton):
    """Session row button. Hover feedback is a 3px left accent bar
    rather than a bg colour change — preserves the group's colour
    identity (VS Code / Slack / Linear pattern).

    Uses WA_Hover=True so Qt fires enter/leave natively. The accent
    bar is painted in paintEvent (no layout impact). Background is
    constant and identical to the row's resting bg.
    """

    _ACCENT_W = 3       # px wide
    _ACCENT_INSET = 4   # px from top/bottom edges (so bar < row height)

    def __init__(self, base_bg: str, parent_card: "QFrame | None" = None, **kwargs):
        super().__init__(**kwargs)
        self._base_bg = base_bg
        self._parent_card = parent_card
        self._hovered = False
        # Accent colour: brightened version of the group bg for in-card
        # rows (reinforces group identity); neutral grey for standalone.
        if parent_card is not None:
            card_bg = getattr(parent_card, "_base_bg", _BG_GROUP)
            self._accent_color = _lighten_bg(card_bg, shift=80)
        else:
            self._accent_color = "#9ca3af"
        # Enable Qt's native hover detection so enterEvent/leaveEvent fire.
        self.setAttribute(Qt.WidgetAttribute.WA_Hover, True)
        # Style is applied by _get_or_create_row via setStyleSheet; we
        # don't set it here because the row may be in_card or standalone.

    def set_parent_card(self, card: "QFrame | None") -> None:
        """Re-bind to a new card (or detach). Recomputes accent colour
        so a row that moves between groups picks up the new identity."""
        self._parent_card = card
        if card is not None:
            card_bg = getattr(card, "_base_bg", _BG_GROUP)
            self._accent_color = _lighten_bg(card_bg, shift=80)
        else:
            self._accent_color = "#9ca3af"
        if self._hovered:
            self.update()

    def enterEvent(self, event):
        self._hovered = True
        self.update()
        return super().enterEvent(event)

    def leaveEvent(self, event):
        self._hovered = False
        self.update()
        return super().leaveEvent(event)

    def paintEvent(self, event) -> None:
        super().paintEvent(event)
        if not self._hovered:
            return
        from PySide6.QtGui import QPainter, QColor
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(self._accent_color))
        # Left-edge rounded bar, inset top/bottom so it doesn't touch
        # the row corners (looks like a focus indicator, not a border).
        x = 0
        y = self._ACCENT_INSET
        w = self._ACCENT_W
        h = self.height() - 2 * self._ACCENT_INSET
        painter.drawRoundedRect(x, y, w, h, w / 2, w / 2)


def _lighten_bg(hex_color: str, shift: int = 18) -> str:
    """Shift a #RRGGBB colour lighter by ``shift`` per channel.

    Default shift (18) is a subtle hover feedback. Larger shifts
    (e.g. 80) produce visibly distinct accents — used by the row
    accent bar to stand out against the row's resting bg.
    """
    r = int(hex_color[1:3], 16)
    g = int(hex_color[3:5], 16)
    b = int(hex_color[5:7], 16)
    r = min(255, r + shift)
    g = min(255, g + shift)
    b = min(255, b + shift)
    return f"#{r:02x}{g:02x}{b:02x}"


_DOT_GREEN = "#4ade80"   # < 1h since last activity
_DOT_YELLOW = "#facc15"  # < 24h
_DOT_GRAY = "#52525b"    # ≥ 24h

_ROW_HEIGHT = 36
_ROW_PAD_H = 12

_STYLE_SINGLE_ROW = f"""
    QPushButton {{
        background: {_BG_SINGLE};
        border: none;
        border-radius: 8px;
        text-align: left;
    }}
    QPushButton:pressed {{ background: {_BG_PRESSED}; }}
"""
_STYLE_GROUP_CARD = f"""
    QFrame#group_card {{
        background: {_BG_GROUP};
        border-radius: 8px;
    }}
"""
_STYLE_GROUP_ROW = f"""
    QPushButton {{
        background: transparent;
        border: none;
        text-align: left;
    }}
    QPushButton:pressed {{ background: {_BG_PRESSED}; }}
"""
# Separator between rows of the same group. With the group sitting on
# #181818, a #2a2a2a hairline is just barely visible — enough to read
# as "two distinct rows" without competing with the dot/name typography.
_STYLE_GROUP_ROW_SEP = "background: #2a2a2a; margin-left: 12px; margin-right: 12px;"
# Px gap between top-level entries (cards / standalone rows). Bigger than
# the in-group row spacing so "next card" reads as a different chunk.
_GROUP_GAP = 8

_STYLE_DOT = "color: {color}; font-size: 11px;"
_STYLE_NAME = "color: #e8e8e8; font-size: 13px;"
_STYLE_AGE = "color: #6b7280; font-size: 11px;"
_STYLE_PERIOD_BTN = """
    QPushButton {
        color: #666;
        background: transparent;
        border: 1px solid #333;
        border-radius: 8px;
        padding: 2px 10px;
        font-size: 10px;
    }
    QPushButton:hover { color: #aaa; border-color: #555; }
    QPushButton:checked { color: white; border-color: #888; background: #2a2a2a; }
"""

# Variant of the pill style for the trailing "+" button that opens the
# add-provider dialog. Same shape so it reads as part of the tab strip,
# but a touch dimmer at rest and a touch brighter on hover so the affordance
# stays discoverable without competing with the actual tabs.
_STYLE_ADD_TAB_BTN = """
    QPushButton {
        color: #888;
        background: transparent;
        border: 1px dashed #444;
        border-radius: 8px;
        padding: 2px 8px;
        font-size: 12px;
        font-weight: bold;
    }
    QPushButton:hover { color: #e8e8e8; border-color: #888; border-style: solid; }
    QPushButton:pressed { background: #2a2a2a; }
"""

# Inputs in the add-provider dialog. Dark background, subtle border,
# accent on focus so the user knows which field is active.
_STYLE_DIALOG_INPUT = """
    QLineEdit {
        color: #e8e8e8;
        background: #1a1a1a;
        border: 1px solid #2a2a2a;
        border-radius: 4px;
        padding: 6px 8px;
        font-size: 12px;
    }
    QLineEdit:focus { border-color: #4a4a4a; }
"""

# Primary / secondary buttons in the add-provider dialog footer.
_STYLE_DIALOG_PRIMARY_BTN = """
    QPushButton {
        color: #e8e8e8;
        background: #2a3f5a;
        border: 1px solid #3a557a;
        border-radius: 6px;
        padding: 6px 14px;
        font-size: 12px;
    }
    QPushButton:hover { background: #335073; border-color: #4a6890; }
    QPushButton:pressed { background: #1f3046; }
    QPushButton:disabled { color: #666; background: #1f1f1f; border-color: #2a2a2a; }
"""

_STYLE_DIALOG_SECONDARY_BTN = """
    QPushButton {
        color: #c9c9c9;
        background: transparent;
        border: 1px solid #3a3a3a;
        border-radius: 6px;
        padding: 6px 14px;
        font-size: 12px;
    }
    QPushButton:hover { color: #e8e8e8; border-color: #4a4a4a; background: #2a2a2a; }
    QPushButton:pressed { background: #1f1f1f; }
"""

# --------------------------------------------------------------------------
# USAGE region typography + cards
# --------------------------------------------------------------------------
#
# Two stacked cards: the (highlighted) 5h-session card on top, the period
# card below. Same bg-contrast language as the session list (group bg
# darker than standalone) so both regions speak the same visual dialect.

_STYLE_USAGE_SESSION_CARD = f"""
    QFrame#usage_session_card {{
        background: {_BG_GROUP};
        border-radius: 8px;
    }}
"""
_STYLE_USAGE_PERIOD_CARD = f"""
    QFrame#usage_period_card {{
        background: {_BG_SINGLE};
        border-radius: 8px;
    }}
"""
_STYLE_USAGE_AMOUNT = "color: #f5f5f5; font-size: 20px; font-weight: 500;"
_STYLE_USAGE_HEADER = "color: #c9c9c9; font-size: 11px; letter-spacing: 0.5px;"
_STYLE_USAGE_RESET = "color: #9ca3af; font-size: 11px;"
_STYLE_USAGE_PCT = "color: #9ca3af; font-size: 11px;"
_STYLE_USAGE_PCT_STALE = "color: #facc15; font-size: 11px;"  # ⚠ tone
# Quota anchor: same visual weight as the $ amount above, so the
# "spend / quota" pair reads as two parallel headline numbers rather
# than "$22 (big) … 3% used (whisper)". Colour is set dynamically per
# threshold (green / yellow / red / gray) — only the size is fixed here.
_STYLE_USAGE_PCT_BIG = "font-size: 16px; font-weight: 500;"
# Per-model breakdown: bumped from #6b7280 to #9ca3af (matches the
# rest of the secondary text in the panel) so the "where did the $
# go" line is actually scannable instead of a dim afterthought.
_STYLE_USAGE_MODEL = "color: #9ca3af; font-size: 11px;"
_STYLE_USAGE_PERIOD_NAME = "color: #c9c9c9; font-size: 12px;"
_STYLE_USAGE_PERIOD_TOTAL = "color: #f5f5f5; font-size: 13px; font-weight: 500;"
_STYLE_USAGE_TOKEN_ROW = "color: #9ca3af; font-size: 11px;"

# Quota progress-bar colour thresholds. The bar's chunk colour escalates
# from green to yellow to red as the user's 5h quota fills, so the user
# can read "how worried should I be" at a glance without parsing the
# percentage text. Stale data (cache > 3×TTL) overrides any colour to
# gray — we don't want to alarm (or reassure) on a number we can't trust.
#
#   < 60%        green   — plenty of runway, large operations are fine
#   60–85%       yellow  — over half spent, start sizing requests
#   ≥ 85%        red     — only 15% headroom, defer big tasks past reset
#   stale (any%) gray    — endpoint quiet for >15 min, value is old
_BAR_GREEN  = "#4ade80"  # matches _DOT_GREEN
_BAR_YELLOW = "#facc15"  # matches _DOT_YELLOW
_BAR_RED    = "#ef4444"  # Tailwind red-500
_BAR_STALE  = "#6b7280"

_STYLE_REFRESH_BTN = """
    QPushButton {
        color: #888;
        background: transparent;
        border: 1px solid #333;
        border-radius: 10px;
        font-size: 12px;
        padding: 0;
    }
    QPushButton:hover   { color: #ddd; border-color: #555; }
    QPushButton:pressed { color: white; background: #2a2a2a; }
"""

# Repair-action button in SessionDetailPopup. Filled (not outline) so it
# reads as an actionable affordance, but coloured neutral grey instead
# of an alarming red — the action is corrective, not destructive (it
# writes a .bak first), and we don't want users to associate it with
# "danger zone" semantics. Disabled state kicks in after a successful
# run so a re-click can't write a second backup over a clean file.
_STYLE_REPAIR_BTN = """
    QPushButton {
        color: #e8e8e8;
        background: #2a2a2a;
        border: 1px solid #3a3a3a;
        border-radius: 6px;
        padding: 6px 12px;
        font-size: 12px;
    }
    QPushButton:hover    { background: #333333; border-color: #4a4a4a; }
    QPushButton:pressed  { background: #1f1f1f; }
    QPushButton:disabled { color: #666; background: #1f1f1f; border-color: #2a2a2a; }
"""

# Header icon button used by SessionDetailPopup's title row for the
# high-frequency actions (Copy ID, Open folder). Square hit target
# with a hover background — reads as a clickable affordance the user
# can target without reading text. Disabled colour shifts to green so
# a completed action ("Done"-state) stays legible.
_STYLE_HEADER_ICON = """
    QPushButton {
        color: #9ca3af;
        background: transparent;
        border: none;
        padding: 2px 6px;
        border-radius: 4px;
        font-size: 13px;
    }
    QPushButton:hover {
        background: #2a2a2a;
        color: #e8e8e8;
    }
    QPushButton:pressed { background: #1f1f1f; }
    QPushButton:disabled { color: #4ade80; background: transparent; }
"""

# Same shape as _STYLE_HEADER_ICON but hover tint shifts to amber and
# the disabled colour to green ("done"). Used for destructive-but-
# reversible actions (Reset thinking blocks) so the user gets a
# colour cue *before* clicking that this one mutates state.
_STYLE_HEADER_ICON_DANGER = """
    QPushButton {
        color: #9ca3af;
        background: transparent;
        border: none;
        padding: 2px 6px;
        border-radius: 4px;
        font-size: 13px;
    }
    QPushButton:hover {
        background: #2a1f15;
        color: #fb923c;
    }
    QPushButton:pressed { background: #1f1715; color: #f87171; }
    QPushButton:disabled { color: #4ade80; background: transparent; }
"""


# Subtle text-link button used by SessionDetailPopup's footer actions,
# the path "↗" reveal-in-explorer button, and the LAST PROMPT
# expand/collapse toggle. No background, gray text, hover underline —
# reads as a hyperlink, not a CTA, so multiple instances side-by-side
# don't compete for attention. Disabled colour shifts to green so a
# completed action still reads as "this finished" rather than "this is
# unavailable".
_STYLE_TEXT_LINK = """
    QPushButton {
        color: #6b7280;
        background: transparent;
        border: none;
        font-size: 11px;
        padding: 0;
        text-decoration: none;
    }
    QPushButton:hover {
        color: #9ca3af;
        text-decoration: underline;
    }
    QPushButton:pressed { color: #4ade80; }
    QPushButton:disabled { color: #4ade80; }
"""

_PROGRESS_BAR_TPL = """
    QProgressBar {{
        border: none;
        background: #2a2a2a;
        border-radius: 3px;
        height: 6px;
        text-align: center;
    }}
    QProgressBar::chunk {{
        background: {color};
        border-radius: 3px;
    }}
"""


def _fmt_tokens(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.0f}K"
    return str(n)


def _fmt_ago(dt: datetime) -> str:
    """Compact age label used on the right side of each row.
    No "ago" suffix — a single column of "5m / 19h / 6d" reads as a
    list of values, so the unit-only form is faster to compare."""
    delta = datetime.now(timezone.utc) - dt.astimezone(timezone.utc)
    s = int(delta.total_seconds())
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m"
    if s < 86400:
        return f"{s // 3600}h"
    return f"{s // 86400}d"


def _activity_color(dt: datetime) -> str:
    """Traffic-light dot color encoding how recent ``dt`` is.
    Thresholds are chosen so a daily user sees mostly green (active),
    yellow at the end of the day, and gray for stale sessions."""
    delta = datetime.now(timezone.utc) - dt.astimezone(timezone.utc)
    h = delta.total_seconds() / 3600.0
    if h < 1:
        return _DOT_GREEN
    if h < 24:
        return _DOT_YELLOW
    return _DOT_GRAY


def _fmt_reset(end_time: datetime | None) -> str:
    """Render the time-until-reset for the 5h session header.

    end_time None     → "—" (no DB activity yet)
    end_time in past  → "expired"
    < 60s remaining   → "in <Ns>" (special-case so the user sees the rollover)
    < 1h remaining    → "in Xm"
    otherwise         → "Xh Ym"
    """
    if end_time is None:
        return "—"
    now = datetime.now(timezone.utc)
    remaining = (end_time.astimezone(timezone.utc) - now).total_seconds()
    if remaining <= 0:
        return "expired"
    if remaining < 60:
        return f"in {int(remaining)}s"
    if remaining < 3600:
        return f"in {int(remaining // 60)}m"
    h = int(remaining // 3600)
    m = int((remaining % 3600) // 60)
    return f"{h}h {m}m"


def _fmt_model_label(model: str) -> str:
    """Friendly model label for the per-model breakdown line.

    Picks up the canonical family name (sonnet/haiku/opus) when the
    raw id contains it, otherwise truncates an unknown id so it
    doesn't overflow the row. Full id remains available via tooltip
    on the row label so the truncation never hides information.
    """
    lower = (model or "").lower()
    for known in ("haiku", "sonnet", "opus"):
        if known in lower:
            return known.capitalize()
    if not model:
        return "?"
    return model[:12] + ("…" if len(model) > 12 else "")


from dataclasses import dataclass


@dataclass(frozen=True)
class _DisplayModelRow:
    """Aggregated per-display-name model totals for the detail popup.

    The popup shows ONE row per display label (Opus / Sonnet / Haiku /
    truncated id), so multiple raw model ids that map to the same label
    (e.g. ``claude-opus-4-5`` and ``claude-opus-4-6``) get merged here
    rather than rendered as duplicate-looking rows.

    ``full_models`` carries every raw API id that contributed to this
    row so the UI can surface them via tooltip — important when the
    label was truncated (``"deepseek-v4-…"``) or when an Anthropic row
    spans multiple subversions (``"Opus"`` covering 4-5 / 4-6 / 4-7).
    """
    label: str
    cost_usd: float
    input_tokens: int
    output_tokens: int
    cache_creation_tokens: int
    cache_read_tokens: int
    full_models: tuple[str, ...] = ()


def _aggregate_per_model_for_display(
    per_model: tuple[ModelTotals, ...],
) -> tuple[_DisplayModelRow, ...]:
    """Group ``per_model`` rows by display label and drop placeholders.

    Drops entries with no cost AND no usage (catches Claude Code's
    ``<synthetic>`` placeholder, which carries zero everything and is
    pure visual noise). Sums the rest by ``_fmt_model_label`` so the
    user never sees two ``Opus`` rows just because the model id changed
    mid-session. Sorted by cost descending — the spender at the top.
    """
    buckets: dict[str, dict] = {}
    for m in per_model:
        if (
            m.cost_usd == 0
            and m.input_tokens == 0
            and m.output_tokens == 0
            and m.cache_creation_tokens == 0
            and m.cache_read_tokens == 0
        ):
            continue
        label = _fmt_model_label(m.model)
        agg = buckets.setdefault(label, {
            "cost": 0.0,
            "in": 0, "out": 0, "cw": 0, "cr": 0,
            "models": [],
        })
        agg["cost"] += m.cost_usd
        agg["in"]   += m.input_tokens
        agg["out"]  += m.output_tokens
        agg["cw"]   += m.cache_creation_tokens
        agg["cr"]   += m.cache_read_tokens
        if m.model and m.model not in agg["models"]:
            agg["models"].append(m.model)
    rows = [
        _DisplayModelRow(
            label=lbl,
            cost_usd=v["cost"],
            input_tokens=v["in"],
            output_tokens=v["out"],
            cache_creation_tokens=v["cw"],
            cache_read_tokens=v["cr"],
            full_models=tuple(v["models"]),
        )
        for lbl, v in buckets.items()
    ]
    rows.sort(key=lambda r: r.cost_usd, reverse=True)
    return tuple(rows)


def _open_in_explorer(path: Path) -> None:
    """Reveal ``path`` in the OS file manager.

    Best-effort — failures are logged to stderr but never raised so a
    missing folder / OS quirk can't crash the popup. Windows is the
    primary target; macOS branch covers laptop dev usage.
    """
    import os
    import subprocess
    import sys as _sys
    try:
        if _sys.platform == "win32":
            os.startfile(str(path))  # type: ignore[attr-defined]
        elif _sys.platform == "darwin":
            subprocess.run(["open", str(path)], check=False)
        else:
            subprocess.run(["xdg-open", str(path)], check=False)
    except Exception as exc:
        print(f"[claude-island] open_in_explorer failed: {exc}", file=_sys.stderr)


def _short_uuid(uuid: str) -> str:
    """First 8 chars of a UUID, or "—" when missing.
    Matches the convention used by `git log --oneline` for commits."""
    if not uuid:
        return "—"
    return uuid[:8]


# Per-model bar palette. Shared between the SPEND card (top-3 by cost
# with "others" in gray) and the detail popup's TOKENS section so the
# same model gets the same colour treatment in both places.
_MODEL_BAR_PALETTE = (
    "#4ade80",   # green
    "#60a5fa",   # blue
    "#c084fc",   # violet
    "#f87171",   # red
    "#fbbf24",   # amber
    "#34d399",   # emerald
)
_MODEL_BAR_OTHERS = "#6b7280"


def _collapse_prompt(text: str) -> str:
    """One-line preview of a prompt: take the first newline-terminated
    line, then truncate to 80 chars, appending ``…`` if any content was
    elided. Used by the detail popup's collapsed LAST PROMPT view."""
    if not text:
        return ""
    first = text.split("\n", 1)[0]
    elided = (first != text) or len(first) > 80
    if len(first) > 80:
        first = first[:79]
    return first + ("…" if elided else "")


def _fmt_started(dt: datetime | None) -> str:
    """Compact relative-time string ("1h 45m ago") for the popup's
    Created field. Dropped the "started " prefix — the field's own
    label ("Created") already supplies that context, and the prefix
    pushed the combined "datetime + relative" string past the value
    column's width on common popup sizes, cropping " ago)"."""
    if dt is None:
        return "—"
    delta = datetime.now(timezone.utc) - dt.astimezone(timezone.utc)
    s = int(delta.total_seconds())
    if s < 60:
        return f"{s}s ago"
    if s < 3600:
        return f"{s // 60}m ago"
    if s < 86400:
        return f"{s // 3600}h {(s % 3600) // 60}m ago"
    return f"{s // 86400}d ago"


def _escape_html(text: str) -> str:
    """Minimal HTML escape for tooltip-safe rendering of user content."""
    return (text.replace("&", "&amp;")
                .replace("<", "&lt;")
                .replace(">", "&gt;"))


def _fmt_local_dt(dt: datetime | None) -> str:
    """Local-timezone datetime for the popup's Created field.
    e.g. ``"2026-05-01 13:00"``. Returns "—" on None."""
    if dt is None:
        return "—"
    return dt.astimezone().strftime("%Y-%m-%d %H:%M")


def _quota_color(pct: float, stale: bool) -> str:
    """Pick the progress-bar / pct-text colour for a 5h quota reading.

    Stale data wins regardless of percentage — we surface "I don't
    trust this" before "how full is it". Thresholds documented at the
    _BAR_* constants above.
    """
    if stale:
        return _BAR_STALE
    if pct >= 85:
        return _BAR_RED
    if pct >= 60:
        return _BAR_YELLOW
    return _BAR_GREEN


def _fmt_money(amount: float) -> str:
    """Compact money formatting that switches precision by magnitude.

    < $0.01   → "$0.001" (preserve some signal)
    < $10     → "$1.23"
    < $1000   → "$123"
    otherwise → "$1.2K"
    """
    if amount < 0.01:
        return f"${amount:.3f}"
    if amount < 10:
        return f"${amount:.2f}"
    if amount < 1000:
        return f"${amount:.0f}"
    return f"${amount / 1000:.1f}K"


# --------------------------------------------------------------------------
# Same-tab grouping (PR2)
# --------------------------------------------------------------------------
#
# Two sessions are visually merged into one rounded card when their
# (window_handle, project_path) pair matches. window_handle is the WT
# main HWND populated by ProcessScanner; project_path is the cwd of the
# claude.exe at scan time. The pair stands in for "same WT tab" because
# WT's UIA tree doesn't expose pid→tab for inactive panes — and panes in
# one tab almost always share both wt_hwnd and cwd.
#
# Sessions whose window_handle is None (couldn't resolve to a WT host —
# pythonw, sandboxed shells, non-Windows) are always rendered standalone
# so they don't accidentally collapse together.

def _group_key(s: Session) -> tuple[int, str] | None:
    if s.window_handle is None:
        return None
    return (s.window_handle, _normalize_project_path(s.project_path))


def _normalize_project_path(path) -> str:
    """Collapse Claude Code worktree paths back to their parent project.

    Claude Code creates per-feature git worktrees under
    ``<repo>/.claude/worktrees/<branch-name>``. Users routinely run
    one claude session in the main repo and another in a worktree,
    side-by-side as split panes in the same WT tab. With raw cwds the
    grouping heuristic sees two different paths and fails to merge
    them. Normalising the worktree back to the repo root restores the
    "same tab" grouping (and, downstream, lets the activator find a
    sibling whose console title IS in the WT TabItem set, fixing
    click-to-switch on the inactive worktree pane).

    Non-worktree paths pass through unchanged.
    """
    parts = path.parts
    for i in range(len(parts) - 1):
        if parts[i] == ".claude" and parts[i + 1] == "worktrees":
            return str(Path(*parts[:i]))
    return str(path)


def _session_sort_key(s: Session) -> tuple:
    """Sort sessions so that members of the same group are adjacent.
    Ungroupable sessions (window_handle=None) sort to the end and stay
    in pid order so their position is stable across refreshes."""
    key = _group_key(s)
    if key is None:
        return (1, 0, "", s.pid)
    wt, path = key
    return (0, wt, path, s.pid)


def _consecutive_groups(sessions: list[Session]) -> list[list[Session]]:
    """Collapse consecutive same-key sessions into a sublist; ungroupable
    (None-key) sessions form singleton sublists each."""
    out: list[list[Session]] = []
    cur: list[Session] = []
    cur_key: object = object()  # sentinel that no real key matches
    for s in sessions:
        key = _group_key(s)
        if key is None:
            if cur:
                out.append(cur)
                cur = []
            out.append([s])
            cur_key = object()
        elif key == cur_key:
            cur.append(s)
        else:
            if cur:
                out.append(cur)
            cur = [s]
            cur_key = key
    if cur:
        out.append(cur)
    return out


class SessionDetailPopup(QFrame):
    """Frameless rounded popup that shows everything we know about
    one session — opened by right-clicking a session row.

    Layout is a flat "dense inspector" (Linear/Notion style): no inner
    sub-cards, sections are separated by 1px dividers, actions are
    text-link buttons in a footer row. Per-model rows are aggregated
    by display label (so two ``Opus`` raw ids collapse into one) and
    rendered with proportional bars matching the SPEND card's visual
    language.

    Uses ``Qt.WindowType.Popup`` so Qt closes us automatically when
    the user clicks anywhere outside, matching how a context menu
    behaves on every desktop platform.
    """

    def __init__(
        self,
        details: SessionDetails | None,
        fallback: Session,
        parent: QWidget | None = None,
        *,
        on_rename: "Callable[[str, str], None] | None" = None,
    ) -> None:
        super().__init__(parent)
        # Frameless + Popup = "I behave like a context menu":
        # no titlebar, on top, dismissed by clicks outside.
        self.setWindowFlags(
            Qt.WindowType.Popup
            | Qt.WindowType.FramelessWindowHint
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setFixedWidth(_PANEL_W)

        self._details = details
        self._fallback = fallback
        # on_rename: caller-supplied (uuid, new_name) -> None invoked when
        # the user commits a name change via the inline edit. None disables
        # the edit affordance entirely. The popup itself has no platform
        # dependency — it just hands the values back to the wiring layer.
        self._on_rename = on_rename
        # Inline rename state — only populated while the user is editing.
        self._name_label: QLabel | None = None
        self._name_edit: "QLineEdit | None" = None
        self._edit_btn: QPushButton | None = None
        # Prompt collapse state — toggled by the [展开] / [收起] link
        # in the LAST PROMPT section header. Default collapsed so the
        # popup stays compact until the user opts in.
        self._prompt_expanded: bool = False
        self._prompt_body: QLabel | None = None
        self._prompt_toggle: QPushButton | None = None

        # Footer action buttons — assigned in _build_footer; some are
        # None when the corresponding action isn't available (e.g.
        # _repair_btn when there's no transcript uuid).
        self._copy_id_btn: QPushButton | None = None
        self._open_folder_btn: QPushButton | None = None
        self._repair_btn: QPushButton | None = None
        # Backwards-compat alias kept until tests are updated; will be
        # the same widget as _repair_btn.
        self._repair_icon: QPushButton | None = None
        # Status line shared by all footer actions.
        self._repair_status: QLabel = QLabel("")
        self._repair_status.setStyleSheet("color: #9ca3af; font-size: 11px;")
        self._repair_status.setWordWrap(True)
        self._repair_status.hide()

        root = QVBoxLayout(self)
        root.setContentsMargins(14, 14, 14, 14)
        root.setSpacing(8)

        root.addWidget(self._build_header_section())
        root.addWidget(self._divider())
        root.addWidget(self._build_meta_section())
        root.addWidget(self._divider())
        root.addWidget(self._build_tokens_section())
        prompt_section = self._build_prompt_section()
        if prompt_section is not None:
            root.addWidget(self._divider())
            root.addWidget(prompt_section)
        # Status feedback line — hidden at rest, shown after any
        # action (copy / open / reset). Lives at the very bottom so
        # it doesn't shift the layout above when it appears.
        root.addWidget(self._repair_status)
        # Defensive stretch at the end. Any vertical surplus that the
        # layout calculates (e.g. when a child's sizeHint is larger
        # than its actual rendered size) gets absorbed here at the
        # bottom — never distributed to header / meta which would
        # otherwise pull subtitle and ID rows apart.
        root.addStretch(1)

        # Stylesheet — bare props go under the popup selector, plus
        # explicit QToolTip rule so the dark theme doesn't bleed white
        # text into white tooltip background on Windows.
        self.setStyleSheet(
            "SessionDetailPopup {"
            "    color: white;"
            "    font-family: 'Segoe UI', sans-serif;"
            "}"
            "QLabel:hover, QFrame:hover, QWidget:hover {"
            "    background: transparent;"
            "}"
            "QPushButton:hover {"
            "    background: transparent;"
            "}"
            "QToolTip {"
            "    color: #e8e8e8;"
            "    background-color: #1e1e1e;"
            "    border: 1px solid #3a3a3a;"
            "    padding: 6px 8px;"
            "    border-radius: 4px;"
            "    font-size: 12px;"
            "}"
        )
        self.adjustSize()

    # ------------------------------------------------------------------
    # paintEvent — translucent rounded body matching the main panel.
    # ------------------------------------------------------------------
    def paintEvent(self, event: object) -> None:  # type: ignore[override]
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        path = QPainterPath()
        path.addRoundedRect(0, 0, self.width(), self.height(), 16, 16)
        painter.fillPath(path, QColor(18, 18, 18, 240))

    # ------------------------------------------------------------------
    # Section builders — flat, no sub-card chrome.
    # ------------------------------------------------------------------

    def _build_header_section(self) -> QWidget:
        """Title + (busy/waiting status pill, only when non-default) +
        right-aligned high-frequency action icons (⧉ Copy ID, ↗ Open
        folder). The destructive ``Reset thinking blocks`` stays in the
        footer so it can't be accidentally clicked while the user is
        still scanning the popup."""
        d = self._details
        title = self._title_text()
        # Italic subtitle = "what this session would have been called
        # without the user's rename":
        #   - Prefer ai_title (the AI-generated title from the JSONL
        #     ai-title row) when it exists and differs from the title.
        #   - When Claude never wrote an ai-title row AND the user
        #     renamed, fall back to the Claude-Code-assigned
        #     ``original_name`` so the user can still see the
        #     "before-rename" context. The is-renamed check (name !=
        #     original_name) means we never surface state.name as
        #     subtitle for unmodified sessions — that'd just be noise.
        #   - Suppressed when no candidate differs from the title.
        was_renamed = bool(
            d and d.name and d.original_name and d.name != d.original_name
        )
        candidates: list[str | None] = [d.ai_title if d else None]
        if was_renamed:
            candidates.append(d.original_name)
        subtitle_ai = next(
            (c for c in candidates if c and c != title),
            None,
        )

        wrap = QWidget()
        # Refuse vertical stretch so layout surplus can't pull this
        # section taller than its content (root.addStretch absorbs
        # the surplus at the bottom of the popup instead).
        wrap.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)
        layout = QVBoxLayout(wrap)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)

        # Line 1: title (left) + right-side action icons.
        # Status pill removed — idle / waiting / busy carry low signal
        # for popup users (who came here to inspect, not monitor), and
        # the chip created visual noise + alignment fights against the
        # action icons.
        head = QHBoxLayout()
        head.setSpacing(6)
        name = QLabel(title)
        name.setStyleSheet(_STYLE_NAME)
        head.addWidget(name, 1)
        # Hold a reference so the inline-rename swap (label → QLineEdit
        # → label) can reach the widget. Same head layout slot is reused
        # so the popup geometry doesn't shift while editing.
        self._name_label = name
        self._name_head_layout = head

        # Right-side action icons. Order: ✎ Edit name, ⧉ Copy ID,
        # ↗ Open folder, ⟲ Reset thinking blocks. The first three are
        # safe & high-freq; the reset is destructive-but-reversible
        # (writes a .bak first) so it sits last and gets a red hover
        # tint as a soft warning. All only appear when applicable.
        sess_uuid = self._effective_uuid()
        # ✎ Edit shows only when the wiring layer wired on_rename AND
        # we have a session uuid to key the override on. Without uuid
        # the override has nowhere to land (sessions whose transcript
        # hasn't been resolved yet — rare but possible during the
        # ProcessScanner-fast → JsonlParser settle window).
        if sess_uuid and self._on_rename is not None:
            self._edit_btn = QPushButton("✎")
            self._edit_btn.setStyleSheet(_STYLE_HEADER_ICON)
            self._edit_btn.setCursor(Qt.CursorShape.PointingHandCursor)
            self._edit_btn.setToolTip(
                "Rename session (claude-island display only — does not "
                "change the terminal tab title)"
            )
            self._edit_btn.setFixedSize(24, 22)
            self._edit_btn.clicked.connect(self._enter_rename_mode)
            head.addWidget(self._edit_btn)
        if sess_uuid:
            self._copy_id_btn = QPushButton("⧉")
            self._copy_id_btn.setStyleSheet(_STYLE_HEADER_ICON)
            self._copy_id_btn.setCursor(Qt.CursorShape.PointingHandCursor)
            self._copy_id_btn.setToolTip("Copy session ID")
            self._copy_id_btn.setFixedSize(24, 22)
            self._copy_id_btn.clicked.connect(self._on_copy_id)
            head.addWidget(self._copy_id_btn)
        self._open_folder_btn = QPushButton("↗")
        self._open_folder_btn.setStyleSheet(_STYLE_HEADER_ICON)
        self._open_folder_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._open_folder_btn.setToolTip("Open project folder in file explorer")
        self._open_folder_btn.setFixedSize(24, 22)
        self._open_folder_btn.clicked.connect(self._on_open_folder)
        head.addWidget(self._open_folder_btn)
        if sess_uuid:
            self._repair_btn = QPushButton("⟲")
            self._repair_btn.setStyleSheet(_STYLE_HEADER_ICON_DANGER)
            self._repair_btn.setCursor(Qt.CursorShape.PointingHandCursor)
            self._repair_btn.setToolTip(
                "Reset thinking blocks — strip historical 'thinking' blocks\n"
                "from this transcript. Use after a Claude → MiniMax → Claude\n"
                "switch when the API rejects the next call with\n"
                "'Invalid signature in thinking block'.\n"
                "A timestamped .bak.<unix-ts> backup is saved automatically."
            )
            self._repair_btn.setFixedSize(24, 22)
            self._repair_btn.clicked.connect(self._on_strip_thinking)
            self._repair_icon = self._repair_btn  # back-compat alias
            head.addWidget(self._repair_btn)
        layout.addLayout(head)

        # Line 2: dim "<age> · v<version>" subtitle. Either part can be
        # missing — only show the dot separator when both sides exist.
        sub_parts: list[str] = []
        if d and d.started_at is not None:
            sub_parts.append(_fmt_started(d.started_at))
        if d and d.cc_version:
            sub_parts.append(f"v{d.cc_version}")
        if sub_parts:
            sub = QLabel(" · ".join(sub_parts))
            sub.setStyleSheet("color: #6b7280; font-size: 11px;")
            layout.addWidget(sub)

        # Line 3 (optional): AI-generated title shown only when it
        # differs from the displayed name (so we don't echo the same
        # string twice).
        if subtitle_ai:
            ai = QLabel(subtitle_ai)
            ai.setStyleSheet("color: #9ca3af; font-size: 11px; font-style: italic;")
            ai.setWordWrap(True)
            layout.addWidget(ai)

        return wrap

    def _build_meta_section(self) -> QWidget:
        """ID / Path / Branch / Created — flat 2-column layout.

        Branch row is suppressed when git_branch is empty OR equal to
        ``HEAD`` (Git's detached-HEAD placeholder is useless to display).
        Created row keeps the existing absolute+relative format.
        Version moved to the header subtitle.
        """
        d = self._details
        sess_uuid = self._effective_uuid()

        wrap = QWidget()
        wrap.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)
        layout = QVBoxLayout(wrap)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)

        # ID row — short 8-char display; ⧉ glyph reveals on row hover
        # (header has the same action always-visible too; the inline
        # affordance just makes near-cursor copying possible).
        id_row = _HoverRevealRow()
        id_h = QHBoxLayout(id_row)
        id_h.setContentsMargins(0, 0, 0, 0)
        id_h.setSpacing(8)
        k = QLabel("ID")
        k.setStyleSheet("color: #6b7280; font-size: 11px;")
        k.setFixedWidth(54)
        k.setAlignment(Qt.AlignmentFlag.AlignTop)
        k.setToolTip("Click to copy session ID")
        id_h.addWidget(k)
        if sess_uuid:
            copyable = _CopyableIdLabel(sess_uuid, display_text=_short_uuid(sess_uuid))
            id_h.addWidget(copyable, 1)
            id_row.register_reveal(copyable._glyph_label)
        else:
            v = QLabel("—")
            v.setStyleSheet("color: #e8e8e8; font-size: 12px;")
            id_h.addWidget(v, 1)
        layout.addWidget(id_row)

        # Path row — value text + ↗ open-in-explorer button that
        # reveals on row hover. Path text itself is non-clickable so a
        # quick mouse pass doesn't accidentally launch explorer.
        path_row = _HoverRevealRow()
        path_h = QHBoxLayout(path_row)
        path_h.setContentsMargins(0, 0, 0, 0)
        path_h.setSpacing(4)
        pk = QLabel("Path")
        pk.setStyleSheet("color: #6b7280; font-size: 11px;")
        pk.setFixedWidth(54)
        pk.setAlignment(Qt.AlignmentFlag.AlignTop)
        pk.setToolTip("Project path")
        path_h.addWidget(pk)
        # Path value is clickable to open folder (same as the ↗ button
        # revealed on hover — clicking the text should open, not copy).
        pv = QLabel(str(self._fallback.project_path))
        pv.setStyleSheet("color: #e8e8e8; font-size: 12px;")
        pv.setWordWrap(True)
        pv.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        pv.setCursor(Qt.CursorShape.PointingHandCursor)
        pv.setToolTip("Open project folder in file explorer")
        pv.mousePressEvent = lambda _: self._on_open_folder()
        path_h.addWidget(pv, 1)
        open_link = QPushButton("↗")
        open_link.setStyleSheet(_STYLE_TEXT_LINK)
        open_link.setCursor(Qt.CursorShape.PointingHandCursor)
        open_link.setToolTip("Open project folder in file explorer")
        open_link.setFixedWidth(16)
        open_link.clicked.connect(self._on_open_folder)
        path_h.addWidget(open_link)
        path_row.register_reveal(open_link)
        layout.addWidget(path_row)

        if d and d.git_branch and d.git_branch.upper() != "HEAD":
            layout.addWidget(self._kv_row("Branch", d.git_branch))
        if d and d.started_at is not None:
            layout.addWidget(self._kv_row(
                "Created",
                f"{_fmt_local_dt(d.started_at)} · {_fmt_started(d.started_at)}",
            ))
        return wrap

    def _build_tokens_section(self) -> QWidget:
        """TOKENS section — header carries cost+turns inline; below it
        a list of per-display-label rows with proportional bars."""
        d = self._details
        rows = (
            _aggregate_per_model_for_display(d.per_model)
            if d and d.per_model else ()
        )
        total_cost = sum(r.cost_usd for r in rows) if rows else (
            d.cost_usd if d else 0.0
        )

        wrap = QWidget()
        wrap.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)
        layout = QVBoxLayout(wrap)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)

        # Header: "TOKENS    $X · N turns"
        head = QHBoxLayout()
        title_lbl = QLabel("TOKENS")
        title_lbl.setStyleSheet(_STYLE_TITLE)
        head.addWidget(title_lbl)
        head.addStretch()
        if d:
            extras: list[str] = [_fmt_money(total_cost)]
            if d.turn_count:
                extras.append(
                    f"{d.turn_count} turn{'s' if d.turn_count != 1 else ''}"
                )
            if d.sidechain_count:
                extras.append(f"{d.sidechain_count} subagent")
            tail = QLabel(" · ".join(extras))
            tail.setStyleSheet("color: #c9c9c9; font-size: 11px;")
            tail.setAlignment(Qt.AlignmentFlag.AlignRight)
            head.addWidget(tail)
        layout.addLayout(head)

        if not rows:
            empty = QLabel(
                "No usage recorded yet." if d else "—"
            )
            empty.setStyleSheet(_STYLE_AGE)
            layout.addWidget(empty)
            return wrap

        # Per-model rows, sorted by cost descending. Bar width is
        # cost / total_cost of the displayed (non-zero) rows.
        max_cost = max(r.cost_usd for r in rows) or 1.0
        for idx, r in enumerate(rows):
            color = _MODEL_BAR_PALETTE[idx % len(_MODEL_BAR_PALETTE)]
            layout.addWidget(self._model_row(r, max_cost=max_cost, color=color))

        return wrap

    def _model_row(
        self,
        r: "_DisplayModelRow",
        *,
        max_cost: float,
        color: str,
    ) -> QWidget:
        """One model row: ``[name] [bar] [cost]`` then a dim sub-line
        with in/out/cw/cr tokens. Mirrors the SPEND card layout so the
        two surfaces share visual language."""
        row = QWidget()
        v = QVBoxLayout(row)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(1)

        top = QHBoxLayout()
        top.setSpacing(6)

        name = QLabel(r.label)
        name.setStyleSheet("color: #e8e8e8; font-size: 12px;")
        name.setFixedWidth(60)
        name.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        # Tooltip carries every raw API id that contributed to this
        # display label so users can see e.g. that "Opus" actually
        # spans "claude-opus-4-7" + legacy "claude-opus-4-5", or that
        # the truncated "deepseek-v4-…" is "deepseek-v4-pro" in full.
        if r.full_models:
            name.setToolTip("\n".join(r.full_models))
        top.addWidget(name)

        bar_track = QFrame()
        bar_track.setFixedHeight(6)
        bar_track.setStyleSheet("background: #2a2a2a; border-radius: 3px;")
        bar_track.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        bar_fill = QFrame(bar_track)
        bar_fill.setStyleSheet(f"background: {color}; border-radius: 3px;")
        bar_fill.setFixedHeight(6)
        # Geometry computed in showEvent / resizeEvent below — store
        # the desired pct on the row so we can recompute on layout
        # changes (popup grows when prompt expands).
        row._bar_track = bar_track
        row._bar_fill = bar_fill
        row._bar_pct = (r.cost_usd / max_cost) if max_cost > 0 else 0.0
        top.addWidget(bar_track, 1)

        cost = QLabel(_fmt_money(r.cost_usd))
        cost.setStyleSheet("color: #e8e8e8; font-size: 12px;")
        cost.setFixedWidth(54)
        cost.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        top.addWidget(cost)
        v.addLayout(top)

        # Sub-line: dim token breakdown on a single row.
        parts = [
            f"in {_fmt_tokens(r.input_tokens)}",
            f"out {_fmt_tokens(r.output_tokens)}",
        ]
        if r.cache_creation_tokens or r.cache_read_tokens:
            parts.append(f"cw {_fmt_tokens(r.cache_creation_tokens)}")
            parts.append(f"cr {_fmt_tokens(r.cache_read_tokens)}")
        tokens = QLabel("  " + " · ".join(parts))
        tokens.setStyleSheet(_STYLE_AGE)
        v.addWidget(tokens)
        return row

    def _build_prompt_section(self) -> QWidget | None:
        """LAST PROMPT — collapsed by default to a one-line preview;
        ``[展开]`` toggles to full text inside a height-capped scrollable
        text view. Returns None when no prompt is recorded so the
        divider above it can be skipped too."""
        d = self._details
        if not d or not d.last_prompt:
            return None
        full = d.last_prompt

        wrap = QWidget()
        wrap.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)
        layout = QVBoxLayout(wrap)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)

        head = QHBoxLayout()
        title_lbl = QLabel("LAST PROMPT")
        title_lbl.setStyleSheet(_STYLE_TITLE)
        head.addWidget(title_lbl)
        head.addStretch()
        # Always create the toggle but hide it initially. Visibility is
        # decided in _set_collapsed_prompt_text once we have a real
        # rendered width to compare against (QFontMetrics before show()
        # under-reports for CJK by ~40%, so any pre-show heuristic is
        # unreliable). The toggle just waits for the truth.
        self._prompt_toggle = QPushButton("[展开]")
        self._prompt_toggle.setStyleSheet(_STYLE_TEXT_LINK)
        self._prompt_toggle.setCursor(Qt.CursorShape.PointingHandCursor)
        self._prompt_toggle.clicked.connect(self._on_toggle_prompt)
        self._prompt_toggle.hide()
        head.addWidget(self._prompt_toggle)
        layout.addLayout(head)

        # Collapsed preview uses a QLabel — single line, no scroll needed.
        # Expanded view swaps in a read-only QTextEdit (see _on_toggle):
        #   - WrapAnywhere wraps API keys / URLs / no-space tokens, which
        #     QLabel.wordWrap=True silently fails to do (it only breaks
        #     at whitespace).
        #   - Capped maxHeight + ScrollBarAsNeeded keeps the popup from
        #     growing past the screen on huge prompts.
        #   - Read-only + selectable → user can copy parts of the prompt.
        # Collapsed preview: single-line QLabel.
        # IMPORTANT: word-wrap is OFF here because:
        #   - long URLs / API keys lack whitespace, and QLabel(wrap=True)
        #     reports minimumSizeHint = "longest unbreakable run" wide.
        #     Multiple long runs stack and push popup width past 320,
        #     producing the QWindowsWindow::setGeometry mintrack=480
        #     warning.
        #   - Single-line preview is the design intent anyway; the
        #     full text lives in the QTextEdit shown when expanded.
        # Text is elided to popup-inner-width via QFontMetrics so a
        # long preview shows the head + "…" instead of being clipped.
        self._prompt_body = QLabel()
        self._prompt_body.setStyleSheet("color: #c9c9c9; font-size: 12px;")
        self._prompt_body.setWordWrap(False)
        self._prompt_body.setSizePolicy(
            QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred
        )
        # Stash the raw collapsed string so showEvent / resizeEvent can
        # re-elide if the widget width changes.
        self._prompt_body._collapsed_text = _collapse_prompt(full)
        self._set_collapsed_prompt_text()
        # Holds the QTextEdit when expanded. Built lazily on first
        # toggle so the collapsed-only common case stays cheap.
        self._prompt_full_view: QTextEdit | None = None
        self._prompt_layout = layout
        layout.addWidget(self._prompt_body)

        return wrap

    def _set_collapsed_prompt_text(self) -> None:
        """Render the collapsed preview elided to fit popup-inner-width
        and update the [展开] toggle visibility based on whether elide
        actually trimmed anything (or the prompt is multi-line).

        Recomputed on every show / toggle-collapse so the ellipsis
        tracks any width changes and the toggle never desyncs from
        the actual content state."""
        if self._prompt_body is None:
            return
        raw = getattr(self._prompt_body, "_collapsed_text", "")
        from PySide6.QtGui import QFontMetrics
        fm = QFontMetrics(self._prompt_body.font())
        # Inner width = popup width minus root margins (14 left + 14 right)
        # minus a small safety pad so the ellipsis doesn't touch the edge.
        available = max(40, _PANEL_W - 28 - 4)
        elided = fm.elidedText(raw, Qt.TextElideMode.ElideRight, available)
        self._prompt_body.setText(elided)

        # Toggle visibility: show iff the user can't see the full prompt
        # in the collapsed state — either elide trimmed content or there's
        # additional content past the first line.
        full = self._details.last_prompt if self._details else ""
        truncated = (elided != raw) or ("\n" in full)
        if self._prompt_toggle is not None and not self._prompt_expanded:
            self._prompt_toggle.setVisible(truncated)


    # ------------------------------------------------------------------
    # Action handlers
    # ------------------------------------------------------------------

    def _on_copy_id(self) -> None:
        sess_uuid = self._effective_uuid()
        if not sess_uuid:
            return
        QApplication.clipboard().setText(sess_uuid)
        self._show_status(
            f"✓ Copied {_short_uuid(sess_uuid)}…",
            color="#4ade80",
        )

    def _on_open_folder(self) -> None:
        path = self._fallback.project_path
        _open_in_explorer(path)
        self._show_status(f"✓ Opened {path}", color="#9ca3af")

    # ------------------------------------------------------------------
    # Inline session rename
    # ------------------------------------------------------------------

    def _enter_rename_mode(self) -> None:
        """Swap the title QLabel for a QLineEdit so the user can type
        a new name. Enter commits, Esc cancels. The edit ✎ button
        hides while editing — its slot is taken by the inline field.

        Idempotent: a no-op if already editing or if the rename
        affordance was suppressed (no on_rename callback / no uuid).
        """
        if self._on_rename is None or self._name_label is None:
            return
        if self._name_edit is not None:
            return  # already in edit mode
        from PySide6.QtWidgets import QLineEdit

        edit = QLineEdit(self._name_label.text())
        edit.setStyleSheet(
            "QLineEdit {"
            "    color: #e8e8e8;"
            "    background: #1a1a1a;"
            "    border: 1px solid #4a4a4a;"
            "    border-radius: 4px;"
            "    padding: 2px 6px;"
            "    font-size: 13px;"
            "}"
            "QLineEdit:focus { border-color: #6b7280; }"
        )
        edit.setPlaceholderText("Display name (leave blank to restore default)")
        # Enter → commit; Esc → cancel. Both wire through dedicated
        # slots so the rename can have side effects (callback fires
        # only on commit) and Esc is unambiguous.
        edit.returnPressed.connect(self._commit_rename)
        # Esc handled via key filter on QLineEdit — easier than
        # subclassing for one keystroke.
        edit.installEventFilter(self)

        # Replace the label widget in-place so the head row geometry
        # stays put (no layout shift while typing).
        idx = self._name_head_layout.indexOf(self._name_label)
        if idx < 0:
            return
        self._name_head_layout.removeWidget(self._name_label)
        self._name_label.hide()
        self._name_head_layout.insertWidget(idx, edit, 1)
        self._name_edit = edit
        if self._edit_btn is not None:
            self._edit_btn.setEnabled(False)
        edit.setFocus()
        edit.selectAll()

    def _exit_rename_mode(self) -> None:
        """Tear down the QLineEdit and restore the QLabel. Caller is
        responsible for updating the label's text first if a save
        happened — _commit_rename does this; _cancel_rename leaves
        the original text untouched."""
        if self._name_edit is None or self._name_label is None:
            return
        idx = self._name_head_layout.indexOf(self._name_edit)
        if idx >= 0:
            self._name_head_layout.removeWidget(self._name_edit)
        self._name_edit.deleteLater()
        self._name_edit = None
        self._name_head_layout.insertWidget(idx if idx >= 0 else 0, self._name_label, 1)
        self._name_label.show()
        if self._edit_btn is not None:
            self._edit_btn.setEnabled(True)

    def _commit_rename(self) -> None:
        """Persist the new name via the on_rename callback, update the
        label text, and exit edit mode. Empty input clears the
        override (restores Claude Code's default name) — the platform
        helper translates "" into a delete.
        """
        if self._name_edit is None or self._on_rename is None:
            return
        new_name = self._name_edit.text().strip()
        sess_uuid = self._effective_uuid()
        if not sess_uuid:
            # Shouldn't happen — we only show the edit button when uuid
            # is non-empty — but guard so a race with session shutdown
            # doesn't blow up the popup.
            self._exit_rename_mode()
            return
        try:
            self._on_rename(sess_uuid, new_name)
        except Exception as exc:
            self._show_status(f"Rename failed: {exc}", color="#ef4444")
            self._exit_rename_mode()
            return
        # Update the label optimistically — the panel's refresh will
        # also re-render but the popup keeps showing the live value
        # until the user dismisses it.
        if self._name_label is not None:
            displayed = new_name if new_name else self._title_text()
            self._name_label.setText(displayed)
        self._exit_rename_mode()
        self._show_status("✓ Renamed", color="#4ade80")

    def _cancel_rename(self) -> None:
        """Exit edit mode without saving. Triggered by Esc."""
        self._exit_rename_mode()

    def eventFilter(self, obj: object, event: object) -> bool:  # type: ignore[override]
        """Catch Esc on the inline rename QLineEdit to cancel editing.

        QLineEdit doesn't expose an escapePressed signal, and
        subclassing for one key would be heavier than this filter.
        """
        from PySide6.QtCore import QEvent
        from PySide6.QtGui import QKeyEvent
        if (
            self._name_edit is not None
            and obj is self._name_edit
            and isinstance(event, QKeyEvent)
            and event.type() == QEvent.Type.KeyPress
            and event.key() == Qt.Key.Key_Escape
        ):
            self._cancel_rename()
            return True
        return super().eventFilter(obj, event)

    def _on_toggle_prompt(self) -> None:
        """Swap between the collapsed QLabel preview and an expanded
        scrollable QTextEdit so:
          - long unbroken tokens (API keys, URLs) wrap properly
            (QLabel only wraps at whitespace; QTextEdit can wrap anywhere)
          - the popup can't grow beyond a sensible max height
          - the user can select + copy parts of the full prompt
        """
        if self._prompt_body is None or self._prompt_toggle is None:
            return
        d = self._details
        if not d or not d.last_prompt:
            return
        self._prompt_expanded = not self._prompt_expanded

        if self._prompt_expanded:
            full = d.last_prompt
            # Hard ceiling so a 100k-char paste can't exhaust memory or
            # overwhelm Qt's text engine. A very long prompt this far
            # off the norm is more diagnostic than display anyway.
            if len(full) > 2000:
                full = full[:1997] + "…"
            if self._prompt_full_view is None:
                self._prompt_full_view = self._build_full_prompt_view()
                self._prompt_layout.addWidget(self._prompt_full_view)
            self._prompt_full_view.setPlainText(full)
            self._prompt_full_view.show()
            self._prompt_body.hide()
            self._prompt_toggle.setText("[收起]")
        else:
            if self._prompt_full_view is not None:
                self._prompt_full_view.hide()
            self._set_collapsed_prompt_text()  # re-elide in case width changed
            self._prompt_body.show()
            self._prompt_toggle.setText("[展开]")
        # Re-fit popup height to the new body.
        self.adjustSize()

    def _build_full_prompt_view(self) -> QTextEdit:
        """Read-only QTextEdit configured for the expanded LAST PROMPT
        view. Critical settings:

        * ``LineWrapMode.WidgetWidth`` — wrap to widget bounds rather
          than letting content drive the size. Without this the
          textedit's sizeHint follows the longest unbroken token (e.g.
          a long URL or API key) and pushes the whole popup wider than
          ``_PANEL_W``, which triggers Qt's ``setGeometry`` warning.
        * ``setMinimumWidth(0)`` + ``Ignored`` h-policy — explicitly
          let the parent layout decide the width, overriding the
          default minimumSizeHint that QTextEdit computes from content.
        * Transparent background + no frame — the textedit reads as
          part of the prompt section, not a sub-pane.
        """
        from PySide6.QtGui import QTextOption
        view = QTextEdit()
        view.setReadOnly(True)
        view.setFrameStyle(QFrame.Shape.NoFrame)
        view.setWordWrapMode(QTextOption.WrapMode.WrapAnywhere)
        view.setLineWrapMode(QTextEdit.LineWrapMode.WidgetWidth)
        view.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        view.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        view.setMinimumWidth(0)
        view.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        # Use a fixed height (not just maximumHeight) so QTextEdit's
        # sizeHint matches its actual on-screen height. With only
        # maximumHeight the sizeHint stays large (default content-driven
        # ~400px), popup.adjustSize() over-allocates, and the surplus
        # gets vertically distributed to header / meta — visible as
        # the gap between subtitle and ID row.
        # ~10 lines at 12px / 1.4 lh ≈ 168 → round up to 180.
        view.setFixedHeight(180)
        view.setStyleSheet(
            "QTextEdit {"
            "    color: #d4d4d4;"
            "    background: transparent;"
            "    border: none;"
            "    padding: 0;"
            "    font-size: 12px;"
            "}"
            "QTextEdit QScrollBar:vertical {"
            "    background: transparent;"
            "    width: 6px;"
            "    margin: 2px;"
            "}"
            "QTextEdit QScrollBar::handle:vertical {"
            "    background: #3a3a3a;"
            "    border-radius: 3px;"
            "    min-height: 20px;"
            "}"
            "QTextEdit QScrollBar::add-line:vertical,"
            "QTextEdit QScrollBar::sub-line:vertical {"
            "    height: 0;"
            "}"
        )
        return view

    def _on_strip_thinking(self) -> None:
        """Resolve the JSONL path from the session, run the repair, and
        report status. The repair function backs the original up to a
        ``.bak.<unix-ts>`` sibling — the button mentions this so a
        wary user knows there's an undo path."""
        from claude_island.core.models import project_hash
        from claude_island.core.session_repair import strip_thinking_blocks

        sess_uuid = self._effective_uuid()
        slug = project_hash(self._fallback.project_path)
        jsonl_path = (
            _claude_projects_root() / slug / f"{sess_uuid}.jsonl"
        )
        try:
            count = strip_thinking_blocks(jsonl_path)
        except FileNotFoundError:
            self._show_status(
                "❌ Transcript not found. The session may have been moved or "
                "started in a different project directory.",
                color="#ef4444",
            )
            return
        except OSError as e:
            self._show_status(
                f"❌ Repair failed: {e}. Original file is unchanged.",
                color="#ef4444",
            )
            return

        if count == 0:
            self._show_status(
                "No thinking blocks found — nothing to remove. "
                "(A backup was still saved.)",
                color="#9ca3af",
            )
        else:
            noun = "block" if count == 1 else "blocks"
            self._show_status(
                f"✓ Removed {count} thinking {noun}. "
                f"Exit (Ctrl+C) and re-enter the session — Claude can "
                f"now continue from this transcript.",
                color="#4ade80",
            )
        # Disable the button so a panicked re-click doesn't write a
        # second backup over an already-clean file.
        if self._repair_btn is not None:
            self._repair_btn.setEnabled(False)
            self._repair_btn.setText("Done")

    def _show_status(self, text: str, *, color: str) -> None:
        self._repair_status.setText(text)
        self._repair_status.setStyleSheet(f"color: {color}; font-size: 11px;")
        self._repair_status.show()
        self.adjustSize()

    # Back-compat name kept for tests that referenced the old method.
    _show_repair_status = _show_status

    # ------------------------------------------------------------------
    # Layout maintenance: bar fills must be sized after the layout has
    # computed each row's actual width. Done in resizeEvent + showEvent
    # so the bars are correct on first paint AND after the popup grows
    # (e.g. when the user expands LAST PROMPT).
    # ------------------------------------------------------------------

    def resizeEvent(self, event) -> None:  # type: ignore[override]
        super().resizeEvent(event)
        self._update_bar_widths()

    def showEvent(self, event) -> None:  # type: ignore[override]
        super().showEvent(event)
        self._update_bar_widths()
        # Re-compute prompt elide + toggle visibility now that the
        # widget tree is realised — pre-show QFontMetrics underreports
        # CJK glyph width and would skip the toggle for prompts that
        # actually do get elided once rendered.
        self._set_collapsed_prompt_text()

    def _update_bar_widths(self) -> None:
        # Walk the popup's QFrames and update any that carry the
        # _bar_track marker attached by `_model_row`.
        for w in self.findChildren(QWidget):
            track = getattr(w, "_bar_track", None)
            fill = getattr(w, "_bar_fill", None)
            pct = getattr(w, "_bar_pct", None)
            if track is None or fill is None or pct is None:
                continue
            tw = track.width()
            if tw <= 0:
                continue
            fw = max(2, int(tw * pct))
            fill.setGeometry(0, 0, fw, 6)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _divider(self) -> QFrame:
        sep = QFrame()
        sep.setFixedHeight(1)
        sep.setStyleSheet(_STYLE_SEP)
        return sep

    def _dot_separator(self) -> QLabel:
        dot = QLabel("·")
        dot.setStyleSheet("color: #4a4a4a; font-size: 11px;")
        return dot

    def _kv_row(self, key: str, value: str) -> QWidget:
        """Two-column ``Key   value`` row. Key is fixed-width gray,
        value is white and wraps if long."""
        row = QWidget()
        h = QHBoxLayout(row)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(8)
        k = QLabel(key)
        k.setStyleSheet("color: #6b7280; font-size: 11px;")
        k.setFixedWidth(54)
        k.setAlignment(Qt.AlignmentFlag.AlignTop)
        h.addWidget(k)
        v = QLabel(value)
        v.setStyleSheet("color: #e8e8e8; font-size: 12px;")
        v.setWordWrap(True)
        h.addWidget(v, 1)
        return row

    def _title_text(self) -> str:
        d = self._details
        return (
            (d.name if d and d.name else None)
            or (d.ai_title if d and d.ai_title else None)
            or self._fallback.project_path.name
            or str(self._fallback.project_path)
        )

    def _effective_uuid(self) -> str:
        """The session_uuid the composer actually used. Falls back to
        the Session's own session_uuid field (often empty when coming
        straight from ProcessScanner)."""
        if self._details and self._details.effective_uuid:
            return self._details.effective_uuid
        return self._fallback.session_uuid or ""


class _AddProviderDialog(QFrame):
    """Frameless rounded popup for adding a new quota provider in-app.

    Renders a radio strip of every provider that exposes a
    ``default_config()`` AND isn't already configured, plus a form
    auto-generated from the chosen provider's default block. On Save
    it invokes a caller-supplied ``on_save(name, fields)`` callback —
    the dialog itself does no I/O, so it stays pure-UI and tests can
    drive it without a filesystem.

    The dialog is fully declarative: it has zero hard-coded provider
    names. Adding a 5th provider in the future surfaces here automatically
    as long as the provider class has a ``default_config()`` classmethod.

    Usage::

        dlg = _AddProviderDialog(
            configurable=[("zhipu", ZhipuProvider.default_config())],
            on_save=lambda name, fields: ...,
            parent=self,
        )
        dlg.move(button.mapToGlobal(QPoint(0, button.height())))
        dlg.show()
    """

    _DIALOG_W = 360

    def __init__(
        self,
        configurable: list[tuple[str, dict]],
        on_save: Callable[[str, dict], None],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowFlags(
            Qt.WindowType.Popup
            | Qt.WindowType.FramelessWindowHint
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setFixedWidth(self._DIALOG_W)

        self._configurable = configurable
        self._on_save = on_save
        # Map provider name → list[(key_name, QLineEdit)] for read-back
        # at save time. Populated by _build_form_for().
        self._inputs: dict[str, list[tuple[str, "QLineEdit"]]] = {}
        # Map provider name → form QWidget so we can show/hide the
        # right one as the user picks a different radio.
        self._form_widgets: dict[str, QWidget] = {}
        # Currently visible provider's name; None when no providers
        # are configurable (empty state).
        self._active: str | None = None

        # Stylesheet matches SessionDetailPopup so the two floating
        # surfaces read as the same component family. Includes the
        # explicit QToolTip block for parity with the popup's fix
        # for white-on-white tooltip rendering on Windows.
        self.setStyleSheet(
            "_AddProviderDialog {"
            "    color: white;"
            "    font-family: 'Segoe UI', sans-serif;"
            "}"
            "QLabel { color: #e8e8e8; }"
            "QToolTip {"
            "    color: #e8e8e8;"
            "    background-color: #1e1e1e;"
            "    border: 1px solid #3a3a3a;"
            "    padding: 6px 8px;"
            "    border-radius: 4px;"
            "    font-size: 12px;"
            "}"
        )

        root = QVBoxLayout(self)
        root.setContentsMargins(16, 16, 16, 14)
        root.setSpacing(10)
        self._build_body(root)
        self.adjustSize()

    def paintEvent(self, event: object) -> None:  # type: ignore[override]
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        path = QPainterPath()
        path.addRoundedRect(0, 0, self.width(), self.height(), 12, 12)
        painter.fillPath(path, QColor(18, 18, 18, 245))

    # ------------------------------------------------------------------
    # Body
    # ------------------------------------------------------------------

    def _build_body(self, root: QVBoxLayout) -> None:
        title = QLabel("Add provider")
        title.setStyleSheet("color: #e8e8e8; font-size: 13px; font-weight: 500;")
        root.addWidget(title)

        # Empty state: nothing left to add. Skip the radio + form
        # entirely and offer a single Close button.
        if not self._configurable:
            msg = QLabel(
                "All available providers are already configured.\n\n"
                "Edit ~/.claude-island/providers.json directly to update "
                "tokens or add a custom provider."
            )
            msg.setStyleSheet("color: #c9c9c9; font-size: 12px;")
            msg.setWordWrap(True)
            root.addWidget(msg)

            close_row = QHBoxLayout()
            close_row.addStretch()
            close_btn = QPushButton("Close")
            close_btn.setStyleSheet(_STYLE_DIALOG_SECONDARY_BTN)
            close_btn.setCursor(Qt.CursorShape.PointingHandCursor)
            close_btn.clicked.connect(self.close)
            close_row.addWidget(close_btn)
            root.addLayout(close_row)
            return

        # Radio strip: one pill per configurable provider.
        radio_row = QHBoxLayout()
        radio_row.setSpacing(6)
        self._radio_btns: dict[str, QPushButton] = {}
        for name, _cfg in self._configurable:
            btn = QPushButton(name.capitalize())
            btn.setCheckable(True)
            btn.setStyleSheet(_STYLE_PERIOD_BTN)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.clicked.connect(lambda _checked, n=name: self._select_provider(n))
            self._radio_btns[name] = btn
            radio_row.addWidget(btn)
        radio_row.addStretch()
        root.addLayout(radio_row)

        # Forms — one per provider, only the active one visible.
        for name, cfg in self._configurable:
            form = self._build_form_for(name, cfg)
            form.hide()
            self._form_widgets[name] = form
            root.addWidget(form)

        # Status slot for save-time error messages.
        self._status = QLabel("")
        self._status.setStyleSheet("color: #ef4444; font-size: 11px;")
        self._status.setWordWrap(True)
        self._status.hide()
        root.addWidget(self._status)

        # Footer: Cancel / Save.
        footer = QHBoxLayout()
        footer.addStretch()
        cancel_btn = QPushButton("Cancel")
        cancel_btn.setStyleSheet(_STYLE_DIALOG_SECONDARY_BTN)
        cancel_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        cancel_btn.clicked.connect(self.close)
        footer.addWidget(cancel_btn)
        save_btn = QPushButton("Save")
        save_btn.setStyleSheet(_STYLE_DIALOG_PRIMARY_BTN)
        save_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        save_btn.clicked.connect(self._on_save_clicked)
        footer.addWidget(save_btn)
        self._save_btn = save_btn
        root.addLayout(footer)

        # Default-select the first provider so the dialog opens with
        # a usable form rather than blank.
        self._select_provider(self._configurable[0][0])

    def _build_form_for(self, name: str, cfg: dict) -> QWidget:
        """Generate a form from the provider's ``default_config()`` dict.

        Iteration order:
          1. ``_help`` (and other ``_``-prefixed keys) → dim wrap-text
             label above the inputs.
          2. ``auth_token`` → password QLineEdit, empty initial value
             (the seed config ships ``""``).
          3. Any other string key (e.g. ``base_url``) → text QLineEdit
             pre-filled with the seed value.
        """
        from PySide6.QtWidgets import QLineEdit

        wrap = QWidget()
        layout = QVBoxLayout(wrap)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        inputs: list[tuple[str, "QLineEdit"]] = []
        # Help text first. Bumped from 11px/#9ca3af to 12px/#c9c9c9 so
        # the multi-sentence guidance is comfortably readable on the
        # dark bg — earlier sizing made it feel like fine print rather
        # than the primary onboarding text it actually is. Top padding
        # gives it breathing room against the radio row above.
        for key, val in cfg.items():
            if key.startswith("_") and isinstance(val, str):
                help_lbl = QLabel(val)
                help_lbl.setStyleSheet(
                    "color: #c9c9c9; font-size: 12px; "
                    "padding: 4px 0 2px 0;"
                )
                help_lbl.setWordWrap(True)
                # Anchor the wrap width so heightForWidth works the
                # first time the form becomes visible. Without an
                # explicit minimum the wrapped label reports a single-
                # line sizeHint, the dialog adjusts to that, and the
                # bottom of the help text gets clipped. _DIALOG_W less
                # 2×16px outer margins is the actual usable width.
                help_lbl.setMinimumWidth(self._DIALOG_W - 32)
                help_lbl.setSizePolicy(
                    QSizePolicy.Policy.Preferred,
                    QSizePolicy.Policy.MinimumExpanding,
                )
                layout.addWidget(help_lbl)

        # Inputs: auth_token first (password), then any other string keys.
        for key in ("auth_token", *(k for k in cfg.keys() if k != "auth_token" and not k.startswith("_"))):
            if key not in cfg:
                continue
            val = cfg[key]
            if not isinstance(val, str):
                continue
            row = QHBoxLayout()
            row.setSpacing(8)
            label = QLabel(key)
            label.setStyleSheet("color: #6b7280; font-size: 11px;")
            label.setFixedWidth(80)
            row.addWidget(label)
            edit = QLineEdit()
            edit.setStyleSheet(_STYLE_DIALOG_INPUT)
            if key == "auth_token":
                edit.setEchoMode(QLineEdit.EchoMode.Password)
                edit.setPlaceholderText("paste API key here")
                # auth_token always starts empty regardless of seed.
            else:
                edit.setText(val)
            row.addWidget(edit, 1)
            layout.addLayout(row)
            inputs.append((key, edit))

        self._inputs[name] = inputs
        return wrap

    def _select_provider(self, name: str) -> None:
        """Switch the visible form + the checked radio to ``name``."""
        for k, btn in self._radio_btns.items():
            btn.setChecked(k == name)
        for k, form in self._form_widgets.items():
            form.setVisible(k == name)
        self._active = name
        # Hide any stale error from the previous provider.
        if hasattr(self, "_status"):
            self._status.hide()
        # Defer adjustSize: a word-wrapped QLabel inside a just-shown
        # form widget reports a 0-height sizeHint until the layout
        # engine has run one pass with the new visibility. Calling
        # adjustSize immediately captures the stale 0-height hint and
        # the help text gets clipped on the bottom (visible as the
        # "(api.z.ai, international)..." line being cut). singleShot(0)
        # punts to the next event-loop tick, after layout settles.
        QTimer.singleShot(0, self.adjustSize)

    def _on_save_clicked(self) -> None:
        if self._active is None:
            return
        fields: dict[str, str] = {}
        for key, edit in self._inputs.get(self._active, []):
            fields[key] = edit.text().strip()
        # Hard validation: auth_token is the one universally required field.
        # Any provider without it can't authenticate, so the tab is useless.
        if not fields.get("auth_token"):
            self._status.setText("auth_token is required.")
            self._status.show()
            self.adjustSize()
            return
        try:
            self._on_save(self._active, fields)
        except Exception as exc:
            self._status.setText(f"Save failed: {exc}")
            self._status.show()
            self.adjustSize()
            return
        self.close()


class ExpandedWindow(QWidget):
    """Floating panel that appears below the capsule when expanded.

    Shows the session list (clicking activates the terminal) and a usage
    summary with a period selector (Daily / Weekly / Monthly).

    ``session_activated`` is connected in __main__.py to WindowActivator.activate
    so the UI layer never imports platform code directly.
    """

    # Args: clicked Session, siblings (list[Session] of other group members,
    # may be empty). Siblings let the activator try sibling console titles
    # as fallback when the clicked row is an inactive split pane.
    session_activated: Signal = Signal(Session, list)

    def __init__(
        self,
        capsule: QWidget,
        controller: IslandController,
        get_usage_totals: Callable[[str], UsageTotals],
        get_session_usage: Callable[[], SessionUsage] | None = None,
        on_refresh_clicked: Callable[[], None] | None = None,
        get_session_details: Callable[[Session], SessionDetails] | None = None,
        available_providers: list[str] | None = None,
        selected_provider: str | None = None,
        on_provider_selected: Callable[[str], None] | None = None,
        get_totals_by_model: Callable[[str], "tuple[ModelTotals, ...]"] | None = None,
        get_quota_snapshot: Callable[[], "QuotaSnapshot | None"] | None = None,
        on_provider_config_changed: Callable[[], None] | None = None,
    ) -> None:
        super().__init__()
        self._capsule = capsule
        self._controller = controller
        self._get_usage_totals = get_usage_totals
        # Per-period per-model breakdown for the SPEND card's model
        # row. None → row stays empty (used in legacy tests). When
        # wired in __main__, points at usage_registry.get_totals_by_model.
        self._get_totals_by_model = get_totals_by_model
        # Per-provider quota snapshot for the QUOTA card. None → bars
        # stay hidden (single-provider case OR test setup). When wired,
        # the closure reads the panel's currently-selected provider so
        # tab clicks immediately re-fetch the right provider's quota.
        self._get_quota_snapshot = get_quota_snapshot
        # Legacy: SessionUsage-based combined quota+spend feed. Still
        # accepted so existing tests construct successfully, but the
        # new SPEND/QUOTA split reads from _get_totals_by_model and
        # _get_quota_snapshot directly. Will be removed once tests are
        # all on the new API.
        self._get_session_usage = get_session_usage
        # Optional manual-refresh hook. Wired in __main__ to bypass the
        # QuotaProvider's TTL and force an immediate fetch — gives the
        # user an out when the auto-refresh hasn't caught the latest
        # state yet (cache TTL is 5 min, heartbeat is 60 s, so worst
        # case the displayed % can lag 5 min behind reality).
        self._on_refresh_clicked = on_refresh_clicked
        # Per-session detail composer. Wired in __main__ to combine
        # JSONL metadata (aiTitle / gitBranch / lastPrompt / version),
        # process state (status / name / startedAt from
        # ~/.claude/sessions/<pid>.json), and aggregate usage (cost /
        # turn count / sidechain count) into one SessionDetails record
        # for the row's hover tooltip. None → tooltip falls back to a
        # minimal "<cwd>" string.
        self._get_session_details = get_session_details
        # Default period is "5h" — the most actionable window (matches
        # the quota's 5h block). Today / Daily / Weekly / Monthly stay
        # one click away when the user wants a longer view.
        self._period = "5h"
        # Multi-provider tab state. ``available_providers`` is the list
        # rendered as pill tabs at the top of the 5h-session card (only
        # rendered when ≥ 2 providers are present — single-provider
        # users see no tabs and the card looks identical to before).
        # ``selected_provider`` is the currently-active tab; the wiring
        # layer reads it via :meth:`selected_provider_name` to decide
        # which provider's quota to fetch. ``on_provider_selected`` is
        # invoked on tab click so the wiring layer can persist the
        # choice (to providers.json) without the UI knowing about disk.
        self._available_providers = list(available_providers or [])
        self._selected_provider = selected_provider
        self._on_provider_selected = on_provider_selected
        # Fired when the in-app + dialog persists a new provider's
        # credentials. The wiring layer re-runs detect() and pushes the
        # updated provider list back via :meth:`set_available_providers`.
        # None → the + button is hidden (no-restart add isn't wired).
        self._on_provider_config_changed = on_provider_config_changed
        self._provider_btns: dict[str, QPushButton] = {}
        # Hold a reference to the active add-provider dialog so Qt's
        # GC doesn't tear it down before the user can interact with it.
        self._add_provider_dialog: "_AddProviderDialog | None" = None
        # Diff-based row update: keep widget references keyed by pid so that
        # session ticks (every ~10s) don't tear down rows the user might be
        # hovering. The placeholder widget (no sessions) is tracked separately
        # — its presence is mutually exclusive with any row.
        self._rows: dict[int, QPushButton] = {}
        self._placeholder: QLabel | None = None
        # Last session snapshot — populated by refresh_sessions, read
        # by user-triggered re-renders (rename) so we don't have to
        # wait for the next scan tick.
        self._latest_sessions: list[Session] = []

        self._setup_window()
        self._build_ui()

        controller.state_changed.connect(self._on_state_changed)

    # ------------------------------------------------------------------
    # Event handling
    # ------------------------------------------------------------------

    def eventFilter(self, obj, event: QEvent) -> bool:  # type: ignore[override]
        """Pass-through for all events.

        Session row hover is handled by HoverRow.enterEvent/leaveEvent
        overrides, which use Qt's native WA_Hover mechanism. This filter
        exists only for potential future use (e.g., spend card rows).
        """
        return super().eventFilter(obj, event)

    # ------------------------------------------------------------------
    # Window setup
    # ------------------------------------------------------------------

    def _setup_window(self) -> None:
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        # Intentionally NOT setting WA_ShowWithoutActivating: that attribute
        # sets WS_EX_NOACTIVATE, which makes WM_MOUSEACTIVATE return
        # MA_NOACTIVATE — clicks deliver but never make us the foreground
        # process, so SetForegroundWindow on the target terminal then fails
        # the "calling process must be foreground" rule.
        self.setFixedWidth(_PANEL_W)
        self.hide()

    def _position(self) -> None:
        cap = self._capsule.frameGeometry()
        x = cap.center().x() - self.width() // 2
        y = cap.bottom() + _GAP
        self.move(x, y)

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        """Three-zone vertical layout:
            1. Bounded-scroll sessions list (max ~7 visible rows)
            2. SPEND card (cross-provider, period-selectable)
            3. QUOTA card (provider-specific, 5h + weekly bars)
        Spend and quota are intentionally siblings — they're different
        concepts (cumulative vs instantaneous) and grouping them in
        adjacent cards lets the eye scan one then the other without
        crossing through unrelated content.
        """
        root = QVBoxLayout(self)
        root.setContentsMargins(14, 14, 14, 14)
        root.setSpacing(8)

        # ── Sessions header (with count badge) ──────────────────────
        # Count badge makes overflow discoverable: when the list scrolls,
        # the user sees "· 14" and knows there's more below the fold.
        self._sessions_title = QLabel("CLAUDE SESSIONS")
        self._sessions_title.setStyleSheet(_STYLE_TITLE)
        root.addWidget(self._sessions_title)

        # ── Sessions scroll area ────────────────────────────────────
        # QScrollArea bounds the panel height regardless of session
        # count. Internal scrollbar (6px wide, dark) appears only on
        # overflow — invisible at 1-7 sessions.
        self._session_scroll = QScrollArea()
        self._session_scroll.setWidgetResizable(True)
        self._session_scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self._session_scroll.setVerticalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAsNeeded
        )
        # Exact multiple of (row + gap) so the visible boundary aligns
        # with a row edge — no half-cropped trailing row.
        self._session_scroll_max_h = (
            _ROW_HEIGHT * _SESSION_SCROLL_VISIBLE_ROWS
            + _GROUP_GAP * (_SESSION_SCROLL_VISIBLE_ROWS - 1)
        )
        self._session_scroll.setMaximumHeight(self._session_scroll_max_h)
        # Vertical Fixed sizePolicy keeps the scroll area at exactly
        # its set height. The default Expanding policy lets it absorb
        # any slack the panel hands it (panel.adjustSize() can grow
        # the panel for unrelated reasons — e.g. an extra SPEND row),
        # which made the sessions area visually "jump" to the max
        # whenever something else in the panel reflowed. Pinning the
        # vertical policy here means the only thing that changes the
        # scroll area's height is the explicit setFixedHeight() call
        # in refresh_sessions, which clamps to actual content.
        self._session_scroll.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed
        )
        self._session_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._session_scroll.setStyleSheet(
            "QScrollArea { background: transparent; border: none; }"
            "QScrollBar:vertical { background: transparent; width: 6px; margin: 0; }"
            "QScrollBar::handle:vertical { background: #3a3a3a; border-radius: 3px; min-height: 20px; }"
            "QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }"
        )
        self._session_container = QWidget()
        self._session_container.setStyleSheet("background: transparent;")
        self._session_box = QVBoxLayout(self._session_container)
        self._session_box.setSpacing(_GROUP_GAP)
        self._session_box.setContentsMargins(0, 0, 0, 0)
        self._session_scroll.setWidget(self._session_container)
        # Smooth-scroll: animate wheel-driven scroll position instead
        # of Qt's default per-tick teleport. Filter is parented to the
        # scroll area, so its lifetime tracks the panel's.
        self._smooth_scroller = _SmoothWheelScroller(self._session_scroll)
        self._session_scroll.viewport().installEventFilter(self._smooth_scroller)
        # Smaller singleStep so click-on-arrow / keyboard-arrow scrolls
        # are also fine-grained, not just the wheel path.
        self._session_scroll.verticalScrollBar().setSingleStep(8)
        root.addWidget(self._session_scroll)

        # ── Separator ───────────────────────────────────────────────
        sep = QFrame()
        sep.setFixedHeight(1)
        sep.setStyleSheet(_STYLE_SEP)
        root.addWidget(sep)

        # ── USAGE header (refresh button moved to QUOTA card) ───────
        usage_title = QLabel("USAGE")
        usage_title.setStyleSheet(_STYLE_TITLE)
        root.addWidget(usage_title)

        # ── SPEND card: period selector + total + breakdown + I/O ──
        self._spend_card = self._build_spend_card()
        root.addWidget(self._spend_card)

        # ── QUOTA card: provider tabs + 5h + weekly bars ────────────
        self._quota_card = self._build_quota_card()
        root.addWidget(self._quota_card)

        self.setStyleSheet(_STYLE_PANEL)

    # ------------------------------------------------------------------
    # Slots (called by QtBridge / signals)
    # ------------------------------------------------------------------

    def refresh_sessions(self, sessions: list[Session]) -> None:
        """Render sessions as a list of cards.

        Sessions sharing the same ``(window_handle, project_path)`` key
        are merged into one rounded card with thin internal separators —
        the iOS-Settings-style group. A session whose ``window_handle``
        is None (couldn't resolve to a WT host) is rendered as its own
        standalone rounded button. Single-session groups also render as
        standalone buttons (visually identical to the flat-list mode).

        Why ``(window_handle, project_path)`` rather than tab id: WT's
        UIA tree only exposes the *active* pane's title in
        TabItem.Name, so we cannot directly determine which tab an
        inactive split-pane belongs to. The wt_hwnd + cwd pair is a
        practical proxy: split panes inside one tab almost always
        share both. The known false-positive — same project opened in
        two separate tabs of the same WT window — is rare in practice.

        Row widgets are cached by pid to preserve hover/pressed state
        across the 10s scan tick. Cards are rebuilt every refresh
        (cheap; just QFrame + QVBoxLayout).
        """
        # Cache the latest list so user-triggered refreshes (e.g. after
        # a session rename) can re-render without waiting for the next
        # scan tick. The registry's emit is the only other path that
        # touches this list, and it always passes a complete snapshot,
        # so caching here is safe.
        self._latest_sessions = list(sessions)

        self._clear_session_layout()
        # Count badge in the header — makes overflow discoverable when
        # the list scrolls past the visible window.
        if sessions:
            self._sessions_title.setText(f"CLAUDE SESSIONS · {len(sessions)}")
        else:
            self._sessions_title.setText("CLAUDE SESSIONS")

        if not sessions:
            self._show_placeholder()
            self._gc_rows(set())
            self.adjustSize()
            self._position()
            return

        self._hide_placeholder()

        sorted_sessions = sorted(sessions, key=_session_sort_key)
        groups = _consecutive_groups(sorted_sessions)

        needed_pids: set[int] = set()
        # Multi-card groups are coloured by their position in the visible
        # list (not by hashing the group key). Walking with a counter
        # guarantees adjacent multi-cards never share a tint. Singletons
        # don't consume a palette slot.
        multi_idx = 0
        for group in groups:
            palette_idx = multi_idx if len(group) > 1 else None
            self._session_box.addWidget(
                self._make_group_widget(group, palette_idx=palette_idx)
            )
            if palette_idx is not None:
                multi_idx += 1
            for s in group:
                needed_pids.add(s.pid)

        self._gc_rows(needed_pids)

        # Lock the scroll area's height to the actual content size,
        # capped at the visible-row maximum. Without this, the scroll
        # area's default Expanding sizePolicy would let it absorb any
        # slack from the panel's adjustSize() (e.g. when an extra SPEND
        # row appears), making the sessions area "jump" to its max
        # height even when content is smaller. Pinning to content keeps
        # the panel layout stable across refreshes.
        self._update_session_scroll_height()
        # Schedule a second pass on the next event-loop tick so a
        # stale-sizeHint reading from the immediate call (Qt sometimes
        # returns 0 right after addWidget when the layout hasn't been
        # polished) gets corrected once the layout has settled.
        # Idempotent: same input → same output → no second resize if
        # the first call was already correct.
        QTimer.singleShot(0, self._update_session_scroll_height)

        self.adjustSize()
        self._position()

    def _update_session_scroll_height(self) -> None:
        """Resize the scroll area to match its content (capped at max).

        Forces a layout pass via ``activate()`` so the just-added
        widgets are measured before reading sizeHint — without this
        the immediate call after ``addWidget`` can return 0, which
        would collapse the scroll area to invisible (the visible bug
        was: header shows "CLAUDE SESSIONS · 7" but no rows render).

        Defensive: if sizeHint still reports 0 (widgets pending
        polishing, or no children at all), keep the previous height
        rather than shrinking to nothing. The deferred companion
        call in refresh_sessions retries after the layout settles.
        """
        self._session_box.activate()
        content_h = self._session_box.sizeHint().height()
        if content_h <= 0:
            return
        self._session_scroll.setFixedHeight(
            min(content_h, self._session_scroll_max_h)
        )

    def refresh_usage_bar(self, _: object = None) -> None:
        """Refresh both USAGE cards. Kept the legacy method name so the
        existing ``totals_changed`` signal wire-up in __main__.py
        continues to fire this on every DB change."""
        self._refresh_spend_card()
        self._refresh_quota_card()

    def _on_manual_refresh(self) -> None:
        """User clicked the ↻ button. Force a quota fetch (bypassing
        the QuotaProvider's TTL) and immediately redraw the cards.

        The fetch is synchronous on the Qt main thread — we live with
        the ~3s worst-case HTTP timeout (matches the rest of the
        provider) because the user is staring at the UI waiting for it
        to update; an async dance with a spinner is overkill here."""
        if self._on_refresh_clicked is not None:
            try:
                self._on_refresh_clicked()
            except Exception as exc:
                # Manual refresh must never crash the UI; the worst
                # case is "you press it and nothing changes".
                import sys as _sys
                print(f"[claude-island] manual refresh failed: {exc}",
                      file=_sys.stderr)
        self.refresh_usage_bar()

    # ------------------------------------------------------------------
    # USAGE: SPEND card (period-selectable, cross-provider)
    # ------------------------------------------------------------------

    def _build_spend_card(self) -> QFrame:
        """Single SPEND card driven by a 5-segment period selector
        (5H / Today / Daily / Weekly / Monthly).

           ┌─────────────────────────────────────┐
           │ SPEND                               │
           │ [5H][Today][Daily][Weekly][Monthly] │
           │ $27                                 │
           │ Opus ████████████        $25.00    │
           │   in 1.2M · out 48K                │
           │ Sonnet ███░░░░░░░░░░░  $1.04      │
           │   in 89K · out 12K                 │
           │ ─────────────────────────────────── │
           │ Input 18.7M · Output 684K           │
           │ Cache W 4.7M · Cache R 236.9M       │
           └─────────────────────────────────────┘

        Each model gets one proportional bar (width = share of total $).
        Top-3 shown; rest collapsed to "others (N)". Token sub-lines
        in 60%-opacity sit below each bar row. The proportional layout
        (YNAB / iOS Battery / macOS Storage style) lets the user read
        "where did the $ go" at a glance without parsing name+number
        pairs — the bar does the cognitive work.
        """
        card = QFrame()
        card.setObjectName("usage_spend_card")
        card.setStyleSheet(_STYLE_USAGE_PERIOD_CARD)

        layout = QVBoxLayout(card)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(6)

        spend_label = QLabel("SPEND")
        spend_label.setStyleSheet(_STYLE_TITLE)
        layout.addWidget(spend_label)

        # Period selector: 5 segments — at the upper bound of what
        # Apple HIG recommends for a segmented control (≤5).
        period_row = QHBoxLayout()
        period_row.setSpacing(6)
        self._period_btns: dict[str, QPushButton] = {}
        for label, key in [
            ("5H",      "5h"),
            ("Today",   "today"),
            ("Daily",   "daily"),
            ("Weekly",  "weekly"),
            ("Monthly", "monthly"),
        ]:
            btn = QPushButton(label)
            btn.setCheckable(True)
            btn.setStyleSheet(_STYLE_PERIOD_BTN)
            btn.setChecked(key == self._period)
            btn.clicked.connect(lambda _, k=key: self._on_period(k))
            period_row.addWidget(btn)
            self._period_btns[key] = btn
        period_row.addStretch()
        layout.addLayout(period_row)

        # Big $ anchor for the selected period
        self._spend_amount = QLabel("—")
        self._spend_amount.setStyleSheet(_STYLE_USAGE_AMOUNT)
        layout.addWidget(self._spend_amount)

        # Per-model proportional bar container. Each row:
        #   [short name] [======bar======] [$XX.XX]
        #   [sub-token line in 60% opacity]
        # Updated on every refresh; rows are created once here.
        self._spend_bar_container = QWidget()
        self._spend_bar_layout = QVBoxLayout(self._spend_bar_container)
        self._spend_bar_layout.setContentsMargins(0, 0, 0, 0)
        self._spend_bar_layout.setSpacing(4)
        self._spend_bar_rows: list[QWidget] = []
        for _ in range(4):  # top-3 + "others" row
            row = self._build_spend_model_row()
            self._spend_bar_layout.addWidget(row)
            self._spend_bar_rows.append(row)
        self._spend_bar_container.hide()
        layout.addWidget(self._spend_bar_container)

        return card

    def _build_spend_model_row(self) -> QWidget:
        """One model row: name · bar · cost, plus a token sub-line.

        Proportional bar is a QFrame with a fixed-height colored child
        whose width is set as a percentage of the container. Width is
        updated via `_refresh_spend_card` whenever totals change.
        """
        row = QWidget()
        row.setStyleSheet("background: transparent;")

        # Two-line vertical: top = name+bar+cost, bottom = token sub-line
        v = QVBoxLayout(row)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(1)

        # Top line: [name] [bar-area] [cost]
        top = QHBoxLayout()
        top.setSpacing(6)

        name_lbl = QLabel("")
        name_lbl.setStyleSheet("color: #e8e8e8; font-size: 12px;")
        name_lbl.setFixedWidth(76)
        name_lbl.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        top.addWidget(name_lbl)

        # Bar track (full width, grows with layout)
        bar_track = QFrame()
        bar_track.setFixedHeight(8)
        bar_track.setStyleSheet("background: #2a2a2a; border-radius: 4px;")
        bar_track.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        # The colored fill is a child frame inside the track
        bar_fill = QFrame()
        bar_fill.setObjectName("_spend_bar_fill")
        bar_fill.setFixedHeight(8)
        bar_fill.setStyleSheet("background: #4ade80; border-radius: 4px;")
        bar_layout = QHBoxLayout(bar_track)
        bar_layout.setContentsMargins(0, 0, 0, 0)
        bar_layout.setSpacing(0)
        bar_layout.addWidget(bar_fill)
        top.addWidget(bar_track, 1)

        cost_lbl = QLabel("")
        cost_lbl.setStyleSheet("color: #e8e8e8; font-size: 12px;")
        cost_lbl.setFixedWidth(62)
        cost_lbl.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        top.addWidget(cost_lbl)

        v.addLayout(top)

        # Token sub-line: "in X · out Y · cw A · cr B" all on one row
        # in dimmed colour. Cache portion is omitted when both cw/cr are 0.
        token_lbl = QLabel("")
        token_lbl.setStyleSheet("color: rgba(156,163,175, 0.6); font-size: 11px;")
        v.addWidget(token_lbl)

        # Store on the row widget for later access by _show_spend_row
        row._spend_name = name_lbl
        row._spend_bar_fill = bar_fill
        row._spend_cost = cost_lbl
        row._spend_tokens = token_lbl
        row._spend_bar_track = bar_track

        return row

    def _refresh_spend_card(self) -> None:
        t = self._get_usage_totals(self._period)
        self._spend_amount.setText(_fmt_money(t.cost_usd))

        # Per-model proportional bars. Only rendered when the wiring layer
        # provides get_totals_by_model. Falls silent for legacy tests.
        show_bars = (
            self._get_totals_by_model is not None
            and t.cost_usd > 0
        )
        if show_bars:
            try:
                rows = self._get_totals_by_model(self._period)
            except Exception as exc:
                import sys as _sys
                print(f"[claude-island] totals_by_model failed: {exc}", file=_sys.stderr)
                rows = ()
            self._populate_spend_bars(rows, t.cost_usd)
            self._spend_bar_container.show()
            # _show_spend_row already stored _bar_pct on each row.
            # Call _update_spend_bar_widths to set fill widths before the
            # first paint — otherwise bars are invisible (fill has 0 width
            # and setFixedWidth in resizeEvent hasn't fired yet).
            self._update_spend_bar_widths()
        else:
            self._spend_bar_container.hide()

    def _populate_spend_bars(
        self, model_rows: "tuple[ModelTotals, ...]", total_cost: float
    ) -> None:
        """Fill proportional bar rows from per-model totals.

        Top-3 rows get real data; the 4th row shows "others (N)" when
        there are more. Each bar's width = model_cost / total_cost.
        Rows beyond the top-3 are hidden.
        """
        PALETTE = _MODEL_BAR_PALETTE
        # Filter out models with zero cost — they shouldn't appear in the bar chart
        non_zero = [m for m in model_rows if m.cost_usd > 0]
        top = list(non_zero[:3])
        remainder = non_zero[3:]
        has_others = len(remainder) > 0

        # Pre-compute widths
        if total_cost <= 0:
            widths = []
        else:
            widths = [(m.cost_usd / total_cost, m) for m in top]

        # Others row: sum the rest
        if has_others:
            others_cost = sum(m.cost_usd for m in remainder)
            others_tokens_in = sum(m.input_tokens for m in remainder)
            others_tokens_out = sum(m.output_tokens for m in remainder)
            others_cw = sum(m.cache_creation_tokens for m in remainder)
            others_cr = sum(m.cache_read_tokens for m in remainder)
            if total_cost > 0:
                others_pct = others_cost / total_cost
            else:
                others_pct = 0
            widths.append((others_pct, None))  # marker for others row

        # Track which rows we've shown (for possible future "others" data)
        row_idx = 0
        for row_idx, row in enumerate(self._spend_bar_rows):
            self._hide_spend_row(row)

        # Fill top-3
        for idx, (pct, mt) in enumerate(widths[:-1] if has_others else widths):
            if mt is None:
                continue  # shouldn't happen
            row = self._spend_bar_rows[idx]
            self._show_spend_row(
                row,
                label=_fmt_model_label(mt.model),
                pct=min(pct, 1.0),
                cost=mt.cost_usd,
                tokens_in=mt.input_tokens,
                tokens_out=mt.output_tokens,
                cache_w=mt.cache_creation_tokens,
                cache_r=mt.cache_read_tokens,
                color=PALETTE[idx % len(PALETTE)],
                full_name=mt.model,
            )

        # Others row (always last)
        if has_others:
            others_row = self._spend_bar_rows[3]
            self._show_spend_row(
                others_row,
                label=f"others ({len(remainder)})",
                pct=min(widths[-1][0], 1.0),
                cost=others_cost,
                tokens_in=others_tokens_in,
                tokens_out=others_tokens_out,
                cache_w=others_cw,
                cache_r=others_cr,
                color=_MODEL_BAR_OTHERS,
            )

    def _show_spend_row(
        self,
        row: QWidget,
        label: str,
        pct: float,
        cost: float,
        tokens_in: int,
        tokens_out: int,
        color: str,
        cache_w: int = 0,
        cache_r: int = 0,
        full_name: str | None = None,
    ) -> None:
        name_lbl: QLabel = row._spend_name
        bar_fill: QFrame = row._spend_bar_fill
        cost_lbl: QLabel = row._spend_cost
        token_lbl: QLabel = row._spend_tokens

        name_lbl.setText(label)
        # Tooltip carries the un-truncated model id so users can hover
        # to see the full name when _fmt_model_label collapsed it
        # (e.g. "deepseek-v4-pro" → "deepseek-v4-…").
        name_lbl.setToolTip(full_name if full_name else label)
        cost_lbl.setText(_fmt_money(cost))
        parts = [
            f"in {_fmt_tokens(tokens_in)}",
            f"out {_fmt_tokens(tokens_out)}",
        ]
        if cache_w or cache_r:
            parts.append(f"cw {_fmt_tokens(cache_w)}")
            parts.append(f"cr {_fmt_tokens(cache_r)}")
        token_lbl.setText("  " + " · ".join(parts))
        bar_fill.setStyleSheet(f"background: {color}; border-radius: 4px;")
        # Store on the row for resizeEvent to compute widths after layout
        row._bar_pct = pct
        row._bar_color = color
        # Do NOT set bar_fill fixed width here — it must be done in
        # resizeEvent after the layout has computed bar_track's actual
        # width. Setting it here with a stale track_w locks bar_fill at
        # that width and the layout won't override it.
        row.show()

    def _hide_spend_row(self, row: QWidget) -> None:
        row.hide()

    # ------------------------------------------------------------------
    # USAGE: QUOTA card (provider-specific, 5h + weekly bars)
    # ------------------------------------------------------------------

    def _build_quota_card(self) -> QFrame:
        """Provider-specific quota card with two stacked bars (5h + weekly).

           ┌─────────────────────────────────────┐
           │ ● [Anthropic][Minimax]              │  switcher AS section header
           │ 5h     ▰▰▰▰▰▱▱▱  44% used            │
           │        resets in 3h 13m             │
           │ Weekly ▰▰▰▰▱▱▱▱  36% used            │
           │        resets in 22h 58m            │
           └─────────────────────────────────────┘

        Switcher placement follows Apple HIG: the tab strip lives
        inside the QUOTA region it scopes (replacing the static
        "{PROVIDER} QUOTA" label), so clicking a tab and seeing the
        bars below change happens in one visual group. With < 2
        providers the static label shows instead.
        """
        card = QFrame()
        card.setObjectName("usage_quota_card")
        card.setStyleSheet(_STYLE_USAGE_SESSION_CARD)

        layout = QVBoxLayout(card)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(4)

        # Section title — mirrors the SPEND header so the two halves
        # of the USAGE card share the same visual language. Earlier
        # the QUOTA region had no title; the dot+pills row alone read
        # as floating UI rather than a labelled section.
        section_title = QLabel("QUOTA")
        section_title.setStyleSheet(_STYLE_TITLE)
        layout.addWidget(section_title)

        # Header: dot + tab strip + refresh button
        quota_hdr = QHBoxLayout()
        quota_hdr.setSpacing(8)
        self._quota_dot = QLabel("●")
        self._quota_dot.setStyleSheet(_STYLE_DOT.format(color=_DOT_GRAY))
        quota_hdr.addWidget(self._quota_dot)

        # Tab strip lives in its own sub-layout so set_available_providers()
        # can wipe + rebuild it without disturbing the dot or the refresh
        # button on either side. Earlier the rebuild walked quota_hdr
        # directly and ate the refresh button as collateral damage.
        self._tab_strip_layout = QHBoxLayout()
        self._tab_strip_layout.setSpacing(8)
        self._tab_strip_layout.setContentsMargins(0, 0, 0, 0)
        # Always-present hidden label so legacy tests that grep for
        # "QUOTA" text in the widget tree keep passing. Shown only in
        # the no-providers fallback branch.
        self._quota_hdr = QLabel("QUOTA")
        self._build_provider_tab_strip()
        quota_hdr.addLayout(self._tab_strip_layout, 1)

        # Manual-refresh button — lives in quota_hdr (NOT the tab
        # strip sub-layout) so a tab-strip rebuild leaves it intact.
        self._refresh_btn = QPushButton("↻")
        self._refresh_btn.setStyleSheet(_STYLE_REFRESH_BTN)
        self._refresh_btn.setFixedSize(20, 20)
        self._refresh_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._refresh_btn.setToolTip("Refresh this provider's quota now")
        self._refresh_btn.clicked.connect(self._on_manual_refresh)
        quota_hdr.addWidget(self._refresh_btn)

        layout.addLayout(quota_hdr)

        # 5h + Weekly stacked bars
        row5, self._quota_bar_5h, self._quota_pct_5h, self._quota_reset_5h = (
            self._build_quota_row("5h")
        )
        layout.addWidget(row5)
        layout.addSpacing(2)
        row_week, self._quota_bar_week, self._quota_pct_week, self._quota_reset_week = (
            self._build_quota_row("Weekly")
        )
        layout.addWidget(row_week)

        return card

    def _refresh_quota_card(self) -> None:
        snap = None
        if self._get_quota_snapshot is not None:
            try:
                snap = self._get_quota_snapshot()
            except Exception as exc:
                import sys as _sys
                print(f"[claude-island] quota fetch failed: {exc}", file=_sys.stderr)

        if snap is None:
            self._quota_dot.setStyleSheet(_STYLE_DOT.format(color=_DOT_GRAY))
            self._hide_quota_row(self._quota_bar_5h, self._quota_pct_5h, self._quota_reset_5h)
            self._hide_quota_row(self._quota_bar_week, self._quota_pct_week, self._quota_reset_week)
            return

        # Provider name no longer prepended to the section title — the
        # section header is just "QUOTA" and the active pill in the
        # tab strip indicates which provider is selected. The old
        # ``ANTHROPIC QUOTA`` / ``MINIMAX QUOTA`` text fought with the
        # pill for the same role and confused users into thinking it
        # was a different region.

        # Live dot: green when 5h window is still open
        active = snap.five_hour_resets_at > datetime.now(timezone.utc)
        self._quota_dot.setStyleSheet(
            _STYLE_DOT.format(color=_DOT_GREEN if active else _DOT_GRAY)
        )

        self._render_quota_row(
            self._quota_bar_5h, self._quota_pct_5h, self._quota_reset_5h,
            pct=snap.five_hour_pct,
            resets_at=snap.five_hour_resets_at,
            stale=snap.is_stale,
        )
        self._render_quota_row(
            self._quota_bar_week, self._quota_pct_week, self._quota_reset_week,
            pct=snap.seven_day_pct,
            resets_at=snap.seven_day_resets_at,
            stale=snap.is_stale,
        )

    def _build_quota_row(self, label: str) -> tuple[QWidget, "QProgressBar", QLabel, QLabel]:
        """Compact two-line widget for one quota window:
            [label] ───── bar ─────  [N% used]
                                        resets in Xh Ym
        The progress-bar uses Expanding sizePolicy so it absorbs all
        available width once label + pct are laid out.
        """
        wrap = QWidget()
        v = QVBoxLayout(wrap)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(1)

        # Top line: label + bar + pct
        top = QHBoxLayout()
        top.setSpacing(8)
        lbl = QLabel(label)
        lbl.setStyleSheet("color: #9ca3af; font-size: 11px;")
        lbl.setFixedWidth(42)
        top.addWidget(lbl)
        bar = QProgressBar()
        bar.setRange(0, 100)
        bar.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        bar.setFixedHeight(8)
        bar.setTextVisible(False)
        bar.setStyleSheet(_PROGRESS_BAR_TPL.format(color=_BAR_GREEN))
        bar.hide()
        top.addWidget(bar, 1)
        pct = QLabel("")
        pct.setStyleSheet(_STYLE_USAGE_PCT_BIG)
        pct.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        pct.setFixedWidth(86)
        top.addWidget(pct)
        v.addLayout(top)

        # Bottom line: reset countdown, indented under the bar so it
        # reads "this is metadata about the bar above"
        reset = QLabel("")
        reset.setStyleSheet(_STYLE_USAGE_RESET)
        reset.setContentsMargins(50, 0, 0, 0)
        v.addWidget(reset)

        return wrap, bar, pct, reset

    def _render_quota_row(
        self,
        bar: "QProgressBar",
        pct_label: QLabel,
        reset_label: QLabel,
        *,
        pct: float,
        resets_at: datetime | None,
        stale: bool,
    ) -> None:
        """Fill one quota row (bar + pct + reset). Bar chunk and pct
        text share the threshold colour so the signal is consistent
        whether the eye lands on the bar or the number first."""
        clamped = max(0, min(100, int(round(pct))))
        color = _quota_color(clamped, stale=stale)
        stale_marker = " ⚠" if stale else ""
        bar.setValue(clamped)
        bar.setStyleSheet(_PROGRESS_BAR_TPL.format(color=color))
        bar.show()
        pct_label.setStyleSheet(f"color: {color}; {_STYLE_USAGE_PCT_BIG}")
        pct_label.setText(f"{clamped}% used{stale_marker}")
        reset_label.setText("resets " + _fmt_reset(resets_at))

    def _hide_quota_row(
        self,
        bar: "QProgressBar",
        pct_label: QLabel,
        reset_label: QLabel,
    ) -> None:
        bar.hide()
        pct_label.setText("")
        reset_label.setText("")

    def _on_state_changed(self, state: str) -> None:
        if state == "expanded":
            self.refresh_sessions(self._controller.sessions)
            self.refresh_usage_bar()
            self._position()
            self.show()
            self.raise_()
            # Explicitly take foreground so subsequent SetForegroundWindow
            # calls from row clicks are allowed by Win32.
            self.activateWindow()
        else:
            self.hide()

    def _on_period(self, period: str) -> None:
        self._period = period
        for key, btn in self._period_btns.items():
            btn.setChecked(key == period)
        self.refresh_usage_bar()

    # ------------------------------------------------------------------
    # Multi-provider tab handlers + getter
    # ------------------------------------------------------------------

    def selected_provider_name(self) -> str | None:
        """Read the currently-active provider tab.

        Wiring layer (``__main__.py``) calls this from inside its
        ``_build_session_usage`` closure to decide which provider's
        quota to fetch. Stored on the panel (rather than threaded
        through the get_session_usage signature) so tests using the
        legacy no-arg callable still work.
        """
        return self._selected_provider

    def _on_provider_clicked(self, name: str) -> None:
        """Switch tab. Updates checked state on every pill, notifies
        the wiring layer (so it can persist to providers.json), and
        forces a refresh so the user sees the new provider's quota
        immediately."""
        if name == self._selected_provider:
            # Re-clicking the active tab — keep it checked, no-op.
            self._provider_btns[name].setChecked(True)
            return
        self._selected_provider = name
        for k, btn in self._provider_btns.items():
            btn.setChecked(k == name)
        if self._on_provider_selected is not None:
            try:
                self._on_provider_selected(name)
            except Exception as exc:
                # Persistence failure must never crash the UI; the
                # in-process selection still works for this session.
                import sys as _sys
                print(f"[claude-island] provider-select callback failed: {exc}",
                      file=_sys.stderr)
        self.refresh_usage_bar()

    # ------------------------------------------------------------------
    # Provider tab strip — construction, in-place rebuild, and the
    # in-app + button that opens the add-provider dialog.
    # ------------------------------------------------------------------

    def _build_provider_tab_strip(self) -> None:
        """Populate ``self._tab_strip_layout`` with one pill per
        active provider plus a trailing ``+`` pill when at least one
        configurable provider is still un-added.

        Even single-provider state renders as a pill (e.g. just
        ``[Anthropic] [+]``) so the user always sees which provider
        the bars belong to AND the "selected" affordance. Earlier
        single-provider branch dropped to a static ``ANTHROPIC QUOTA``
        text label, which read as a section header rather than a
        current-selection indicator and confused users.

        Lives in its own sub-layout (separate from the refresh button
        in the parent ``quota_hdr``) so :meth:`_rebuild_provider_tab_strip`
        can wipe + rebuild without taking out the refresh button.

        The hidden ``QUOTA`` label is kept around as a no-op widget so
        existing tests that grep for the string keep passing."""
        layout = self._tab_strip_layout
        if self._available_providers:
            # Always pill-render — even single-provider state shows
            # one selected pill rather than degrading to a static label.
            for name in self._available_providers:
                btn = QPushButton(name.capitalize())
                btn.setCheckable(True)
                btn.setStyleSheet(_STYLE_PERIOD_BTN)
                btn.setChecked(name == self._selected_provider)
                btn.clicked.connect(lambda _, n=name: self._on_provider_clicked(n))
                # Right-click on non-Anthropic tabs offers Delete. Anthropic
                # is the always-available baseline (reads OAuth from
                # ~/.claude/.credentials.json), so deleting it would just
                # leave the strip blank — don't even hint at the action.
                # The wiring layer must be present (_on_provider_config_changed)
                # for the rebuild after delete to take effect; tests that
                # construct a panel without it skip the menu entirely.
                if name != "anthropic" and self._on_provider_config_changed is not None:
                    btn.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
                    btn.customContextMenuRequested.connect(
                        lambda pos, n=name, b=btn: self._show_provider_context_menu(n, b, pos)
                    )
                layout.addWidget(btn)
                self._provider_btns[name] = btn
            self._quota_hdr.setText("QUOTA")
            self._quota_hdr.hide()
        else:
            # Pathological zero-provider state — fall back to the static
            # label so the card still has a visible header in this row.
            self._quota_hdr.setText("QUOTA")
            self._quota_hdr.setStyleSheet(_STYLE_TITLE)
            self._quota_hdr.show()
            layout.addWidget(self._quota_hdr)

        # + button. Append before the stretch so it sits at the right
        # end of the pill row. Skip when no callback is wired (tests /
        # detached use), or when there's nothing left to add.
        self._add_provider_btn: QPushButton | None = None
        if self._on_provider_config_changed is not None and self._configurable_providers():
            self._add_provider_btn = QPushButton("+")
            self._add_provider_btn.setStyleSheet(_STYLE_ADD_TAB_BTN)
            self._add_provider_btn.setCursor(Qt.CursorShape.PointingHandCursor)
            self._add_provider_btn.setToolTip("Add a quota provider")
            self._add_provider_btn.setFixedHeight(20)
            self._add_provider_btn.clicked.connect(self._on_add_provider_clicked)
            layout.addWidget(self._add_provider_btn)

        layout.addStretch()

    def _rebuild_provider_tab_strip(self) -> None:
        """Tear down the existing pills + ``+`` + stretch and rebuild
        from the updated ``self._available_providers``. Used after the
        in-app add dialog persists a new provider so the tab appears
        without an app restart.

        Operates only on ``self._tab_strip_layout`` so the dot and
        refresh button (siblings in the parent quota_hdr) survive
        the rebuild — earlier the rebuild walked the parent layout
        and ate the refresh button as collateral damage."""
        layout = self._tab_strip_layout
        # Wipe everything in the sub-layout. self._quota_hdr is the
        # only widget we want to keep around (cached on self,
        # potentially re-added in the empty-providers branch); detach
        # without deleteLater so the next _build_provider_tab_strip
        # can re-show it if needed.
        while layout.count() > 0:
            item = layout.takeAt(0)
            if item is None:
                break
            w = item.widget()
            if w is None:
                continue  # spacer — already removed by takeAt
            if w is self._quota_hdr:
                w.setParent(None)  # keep alive for re-use
                continue
            w.setParent(None)
            w.deleteLater()
        self._provider_btns.clear()
        self._add_provider_btn = None
        self._build_provider_tab_strip()

    def _configurable_providers(self) -> list[tuple[str, dict]]:
        """List of (name, default_config_dict) for providers that
        expose ``default_config()`` and aren't already in
        ``_available_providers``. Drives both the + button visibility
        and the dialog's choice list."""
        from claude_island.platform_.providers import all_providers
        out: list[tuple[str, dict]] = []
        for name, cls in all_providers().items():
            if name in self._available_providers:
                continue
            cfg_fn = getattr(cls, "default_config", None)
            if cfg_fn is None:
                continue
            try:
                cfg = cfg_fn()
            except Exception:
                cfg = None
            if isinstance(cfg, dict):
                out.append((name, cfg))
        return out

    def _on_add_provider_clicked(self) -> None:
        """Open the add-provider dialog adjacent to the + button."""
        configurable = self._configurable_providers()
        dlg = _AddProviderDialog(
            configurable=configurable,
            on_save=self._on_dialog_save,
            parent=self,
        )
        # Position immediately below the + button. mapToGlobal handles
        # multi-monitor / DPI correctly.
        if self._add_provider_btn is not None:
            origin = self._add_provider_btn.mapToGlobal(
                self._add_provider_btn.rect().bottomLeft()
            )
            dlg.move(origin)
        dlg.show()
        # Hold a reference so Qt's GC doesn't tear it down.
        self._add_provider_dialog = dlg

    def _on_dialog_save(self, name: str, fields: dict) -> None:
        """Dialog Save callback — persist credentials, then ask the
        wiring layer to re-detect providers and push the new list back
        via :meth:`set_available_providers`."""
        from claude_island.platform_.providers import set_provider_settings
        set_provider_settings(name, fields)
        if self._on_provider_config_changed is not None:
            try:
                self._on_provider_config_changed()
            except Exception as exc:
                import sys as _sys
                print(f"[claude-island] provider-config-changed callback failed: {exc}",
                      file=_sys.stderr)

    def _show_provider_context_menu(
        self, name: str, anchor: QPushButton, pos: object
    ) -> None:
        """Pop a context menu under the right-clicked provider tab.

        Currently a single Delete action — keeps the menu tiny for
        what's effectively a one-click affordance. ``pos`` is the
        local-coordinate QPoint from the customContextMenuRequested
        signal; we map it through the anchor button so the menu
        appears under the cursor on a multi-monitor setup.
        """
        menu = QMenu(self)
        delete_action = menu.addAction(f"Delete {name.capitalize()}")
        delete_action.triggered.connect(
            lambda _checked=False, n=name: self._on_delete_provider_clicked(n)
        )
        menu.exec(anchor.mapToGlobal(pos))

    def _on_delete_provider_clicked(self, name: str) -> None:
        """Wipe the provider's block from providers.json and rebuild
        the tab strip via the same callback the add-dialog uses.

        Anthropic is filtered out at the call site (``_build_provider_tab_strip``
        only wires the menu for non-anthropic tabs), so this method
        doesn't re-check — keeping the guard in one place avoids the
        "is anthropic deletable?" rule drifting out of sync between
        the wiring and the action."""
        from claude_island.platform_.providers import delete_provider_settings
        delete_provider_settings(name)
        if self._on_provider_config_changed is not None:
            try:
                self._on_provider_config_changed()
            except Exception as exc:
                import sys as _sys
                print(f"[claude-island] provider-config-changed callback failed: {exc}",
                      file=_sys.stderr)

    def _on_session_renamed(self, sess_uuid: str, new_name: str) -> None:
        """Persist a custom session display name and re-render rows.

        Wired to ``SessionDetailPopup.on_rename``. Empty ``new_name``
        clears the override (restores the auto-detected name) — the
        platform helper handles the empty-as-delete sentinel.

        Strict per-session: the rename only affects THIS session. An
        earlier dual-key design also wrote a per-project entry so the
        rename would survive Claude Code's sessionId rotation, but
        that caused renames to bleed across sibling sessions sharing
        a project_path — worse than the rotation case.

        After persisting we re-call ``refresh_sessions`` with the
        cached snapshot so the row label reflects the new name without
        waiting for the next 10s scan tick. The composer
        ``_get_session_details`` is responsible for re-reading the
        override on each call (which it does, via the wiring layer's
        ``_build_session_details``)."""
        from claude_island.platform_ import session_names as _names
        _names.set_session_name(sess_uuid, new_name)
        # Re-render with the cached snapshot so the new name appears
        # immediately. _latest_sessions stays empty until the first
        # refresh_sessions call, but rename can only happen via the
        # popup which only opens after at least one row is shown — so
        # the cache is guaranteed populated at this point.
        if self._latest_sessions:
            self.refresh_sessions(self._latest_sessions)

    def set_available_providers(
        self, providers: list[str], selected: str | None = None
    ) -> None:
        """Update the panel's available-provider list and rebuild the
        QUOTA card's tab strip in place. Called by the wiring layer
        after the in-app add-provider dialog writes new credentials —
        gives users a no-restart path from "I just pasted a key" to
        "the new tab is live".

        ``selected`` overrides the current selection; when None and
        the existing selection is no longer valid, falls back to the
        first available."""
        self._available_providers = list(providers)
        if selected is not None:
            self._selected_provider = selected
        elif self._selected_provider not in providers and providers:
            self._selected_provider = providers[0]
        self._rebuild_provider_tab_strip()
        self.refresh_usage_bar()

    # ------------------------------------------------------------------
    # Session row factory
    # ------------------------------------------------------------------

    def _make_row(self, session: Session, parent_card: QFrame | None = None) -> HoverRow:
        """Build a click-target row with a 3-element horizontal layout:
        ``● name ............... cost``.

        The HoverRow supplies enter/leave hover transitions via native
        Qt hover detection (WA_Hover=True). A QHBoxLayout inside the
        button positions three QLabels (dot / name / meta). Each label
        has WA_TransparentForMouseEvents so clicks anywhere on the row
        fall through to the button.

        Right-click opens a SessionDetailPopup with the rich metadata
        (id / cwd / created / per-model tokens / last prompt). Left
        click activates the WT tab as before.
        """
        btn = HoverRow(base_bg=_BG_SINGLE, parent_card=parent_card)
        btn.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        btn.setFixedHeight(_ROW_HEIGHT)
        btn.setProperty("_session", session)
        btn.setProperty("_siblings", [])

        layout = QHBoxLayout(btn)
        layout.setContentsMargins(_ROW_PAD_H, 0, _ROW_PAD_H, 0)
        layout.setSpacing(8)

        name_label = QLabel()
        name_label.setObjectName("name_label")
        name_label.setStyleSheet(_STYLE_NAME)
        name_label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        name_label.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        layout.addWidget(name_label, 1)

        # Right-side meta slot. Shows cumulative session cost.
        meta_label = QLabel()
        meta_label.setObjectName("meta_label")
        meta_label.setStyleSheet(_STYLE_AGE)
        meta_label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        meta_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        layout.addWidget(meta_label)

        self._update_row(btn, session)
        btn.clicked.connect(lambda: self._on_row_clicked(
            btn.property("_session"),
            btn.property("_siblings") or [],
        ))
        # Right-click → detail popup at cursor. CustomContextMenu lets
        # us bypass Qt's default text-context-menu and route the event
        # to our handler, which opens the rich SessionDetailPopup.
        btn.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        btn.customContextMenuRequested.connect(
            lambda local_pos: self._show_detail_popup(btn, local_pos)
        )
        return btn

    def _update_row(self, btn: QPushButton, session: Session) -> None:
        """Refresh name and right-side cost on every refresh tick."""
        details: SessionDetails | None = None
        if self._get_session_details is not None:
            try:
                details = self._get_session_details(session)
            except Exception as exc:
                # Detail composition is enrichment, not load-bearing —
                # never let a composer hiccup take down the row update.
                import sys as _sys
                print(f"[claude-island] session details failed: {exc}",
                      file=_sys.stderr)
        title = (
            (details.name if details and details.name else None)
            or (details.ai_title if details and details.ai_title else None)
            or session.project_path.name
            or str(session.project_path)
        )
        # "—" when details are unavailable rather than falling back to
        # an age string — keeps the meta slot semantically consistent
        # ("this column is always cost").
        meta_text = _fmt_money(details.cost_usd) if details is not None else "—"

        name_label = btn.findChild(QLabel, "name_label")
        if name_label is not None and name_label.text() != title:
            name_label.setText(title)

        meta_label = btn.findChild(QLabel, "meta_label")
        if meta_label is not None and meta_label.text() != meta_text:
            meta_label.setText(meta_text)

        # Right-click triggers the rich popup; tooltip would compete
        # for the same surface, so it's gone. Explicit empty string
        # in case Qt cached a value from an earlier build of the row.
        btn.setToolTip("")

        btn.setProperty("_session", session)

    # ------------------------------------------------------------------
    # Card composition (PR2: same-tab grouping)
    # ------------------------------------------------------------------

    def _make_group_widget(
        self, group: list[Session], *, palette_idx: int | None = None
    ) -> QWidget:
        """One group → one widget. Single-session groups render as a
        standalone rounded button; multi-session groups render as a
        rounded card with flat internal rows + thin separators.

        ``palette_idx`` is supplied by the caller (it counts multi-card
        position) so adjacent multi-cards never collide on tint.
        Singletons ignore it."""
        if len(group) == 1:
            row = self._get_or_create_row(group[0], group, in_card=False, card=None)
            row.setParent(None)  # detach from any prior parent
            return row
        return self._make_multi_card(group, palette_idx=palette_idx or 0)

    def _make_multi_card(self, sessions: list[Session], *, palette_idx: int = 0) -> QFrame:
        card = QFrame()
        card.setObjectName("group_card")
        # Per-group hue tint indexed by visible position so adjacent
        # multi-cards always differ. The previous hash-based mapping
        # landed two unrelated groups on the same red tint when hashes
        # happened to collide mod 6.
        bg = _group_bg_color(palette_idx)
        card._base_bg = bg
        card.setStyleSheet(
            f"QFrame#group_card {{ background: {bg}; border-radius: 8px; }}"
        )
        # Card bg never changes on hover (left-accent-bar pattern), so
        # WA_Hover stays at the default — no OS overlay to suppress.
        layout = QVBoxLayout(card)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        for i, session in enumerate(sessions):
            if i > 0:
                sep = QFrame()
                sep.setFixedHeight(1)
                sep.setStyleSheet(_STYLE_GROUP_ROW_SEP)
                sep.setAttribute(Qt.WidgetAttribute.WA_Hover, False)
                layout.addWidget(sep)
            row = self._get_or_create_row(session, sessions, in_card=True, card=card)
            row.setParent(None)
            layout.addWidget(row)
        return card

    def _get_or_create_row(
        self,
        session: Session,
        group: list[Session],
        *,
        in_card: bool,
        card: QFrame | None,
    ) -> HoverRow:
        """Cached factory: same pid keeps the same HoverRow across
        refreshes (preserves hover state). Style is reapplied each call
        because a row can move between standalone (rounded) and in-card
        (flat) layouts as group membership changes.

        ``group`` is the full list of sessions in this row's group
        (including the row itself). Stored on the button so the click
        handler can pass siblings to the activator — needed for the
        inactive-pane case where the row's own console title doesn't
        appear in any TabItem.Name and we have to fall back to one of
        the siblings' titles to actually switch the WT tab.
        """
        btn = self._rows.get(session.pid)
        if btn is None:
            btn = self._make_row(session, parent_card=card)
            self._rows[session.pid] = btn
        else:
            # Row may have moved between standalone ↔ group — rebind so
            # the accent bar picks up the new card's group colour.
            btn.set_parent_card(card)
        btn.setStyleSheet(_STYLE_GROUP_ROW if in_card else _STYLE_SINGLE_ROW)
        self._update_row(btn, session)
        siblings = [s for s in group if s.pid != session.pid]
        btn.setProperty("_siblings", siblings)
        return btn

    # ------------------------------------------------------------------
    # Layout housekeeping
    # ------------------------------------------------------------------

    def _clear_session_layout(self) -> None:
        """Remove every top-level item from session_box. Cached row
        buttons are detached (kept alive in self._rows for reuse);
        cards and the placeholder are deleted."""
        cached = set(self._rows.values())
        while self._session_box.count():
            item = self._session_box.takeAt(0)
            widget = item.widget()
            if widget is None:
                continue
            if widget in cached:
                widget.setParent(None)
                continue
            # Card: detach any cached rows inside before deleting it,
            # so they survive for the next group composition.
            for child in widget.findChildren(QPushButton):
                if child in cached:
                    child.setParent(None)
            if widget is self._placeholder:
                self._placeholder = None
            widget.deleteLater()

    def _show_placeholder(self) -> None:
        # Drop any cached rows: there are no sessions to back them.
        for btn in self._rows.values():
            btn.deleteLater()
        self._rows.clear()
        if self._placeholder is None:
            self._placeholder = QLabel("No active sessions")
            self._placeholder.setStyleSheet("color: #555; font-size: 12px;")
        self._session_box.addWidget(self._placeholder)

    def _hide_placeholder(self) -> None:
        if self._placeholder is not None:
            self._placeholder.deleteLater()
            self._placeholder = None

    def _gc_rows(self, needed_pids: set[int]) -> None:
        for pid in list(self._rows.keys()):
            if pid not in needed_pids:
                self._rows.pop(pid).deleteLater()

    def _show_detail_popup(self, btn: QPushButton, local_pos) -> None:
        """Build a SessionDetailPopup with fresh data and pop it at the
        cursor. Called from the row's customContextMenuRequested signal.

        We compose details on demand (not from a cached value) so the
        popup always reflects the latest cost / status. Failures in the
        composer are logged and the popup still opens with whatever
        partial data is available — falling back to None when the
        composer is unwired or the lookup raises."""
        session = btn.property("_session")
        if session is None:
            return
        details: SessionDetails | None = None
        if self._get_session_details is not None:
            try:
                details = self._get_session_details(session)
            except Exception as exc:
                import sys as _sys
                print(f"[claude-island] detail popup composer failed: {exc}",
                      file=_sys.stderr)
        popup = SessionDetailPopup(
            details, session, parent=self,
            on_rename=self._on_session_renamed,
        )
        # Map the row-local right-click position to global screen
        # coordinates. ``btn.mapToGlobal`` does the right thing across
        # multi-monitor / DPI setups.
        popup.move(btn.mapToGlobal(local_pos))
        popup.show()
        # Hold a reference so Qt's GC doesn't tear the popup down
        # before the user gets to interact with it. Replacing the slot
        # on each open is fine — Qt's Popup flag closes the previous
        # instance when the new one shows.
        self._active_detail_popup = popup

    def _on_row_clicked(self, session: Session, siblings: list[Session]) -> None:
        # Activate first, then collapse — order matters: while our panel is
        # still on top (StaysOnTopHint) we are the foreground process, which
        # is the only state in which SetForegroundWindow is allowed to
        # surface another process's window.
        self.session_activated.emit(session, siblings)
        self._controller.toggle_expanded()

    def resizeEvent(self, event: object) -> None:  # type: ignore[override]
        """Recompute proportional bar fill widths after a layout resize.

        Also called manually from _refresh_spend_card after show() so the
        bars are correctly sized on first paint, not just on window resize.
        """
        super().resizeEvent(event)
        self._update_spend_bar_widths()

    def _update_spend_bar_widths(self) -> None:
        """Set each bar_fill to its proportional width of the bar_track.

        Called from resizeEvent (natural window resize) and also from
        _refresh_spend_card after the container is shown (first paint).
        """
        for row in self._spend_bar_rows:
            if row.isHidden():
                continue
            pct = getattr(row, "_bar_pct", 0)
            color = getattr(row, "_bar_color", "#4ade80")
            bar_track = row._spend_bar_track
            bar_fill = row._spend_bar_fill
            # geometry().width() gives the layout-allocated width.
            track_px = bar_track.geometry().width()
            bar_fill.setFixedWidth(int(pct * max(track_px, 1)))
            bar_fill.setStyleSheet(f"background: {color}; border-radius: 4px;")

    # ------------------------------------------------------------------
    # Paint
    # ------------------------------------------------------------------

    def paintEvent(self, event: object) -> None:  # type: ignore[override]
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        path = QPainterPath()
        path.addRoundedRect(0, 0, self.width(), self.height(), 16, 16)
        painter.fillPath(path, QColor(18, 18, 18, 240))
