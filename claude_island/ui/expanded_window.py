from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from claude_island.ui.fonts import MONO_FONT_STACK, UI_FONT_STACK
from claude_island.ui.lab_palette import Color as _LabColor, FontStack
from claude_island.ui.tooltip_style import TOOLTIP_QSS

from PySide6.QtCore import (
    QEasingCurve,
    QEvent,
    QObject,
    QPropertyAnimation,
    Qt,
    QTimer,
    QVariantAnimation,
    Signal,
)
from PySide6.QtGui import QColor, QFont, QPainter, QPainterPath
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
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
    DEFAULT_MODEL_COLOR,
    ModelTotals,
    QuotaSnapshot,
    Session,
    SessionDetails,
    UsageTotals,
    resolve_model_color,
    resolve_model_short_name,
)
from claude_island.core.capabilities import Capability
from claude_island.core.snapshot import SessionView, WorldSnapshot
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

        # elide=False: wordWrap is set below for the long-form path,
        # and the short-form (8-char UUID prefix) is well under panel
        # width — eliding would conflict with both.
        self._uuid_label = mk_label(shown, elide=False)
        self._uuid_label.setStyleSheet(
            f"color: #e8e8e8; font-size: 11px; font-family: {MONO_FONT_STACK};"
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
            self._glyph_label = mk_label("⧉", elide=False)
            self._glyph_label.setStyleSheet("color: #6b7280; font-size: 11px;")
            self._glyph_label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
            layout.addWidget(self._glyph_label)

        layout.addStretch()

        self._copied_label = mk_label("Copied", elide=False)
        self._copied_label.setStyleSheet(
            f"color: #4ade80; font-size: 11px; font-family: {MONO_FONT_STACK};"
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
        """Mark ``widget`` as hover-only and hide it now.

        ``setRetainSizeWhenHidden(True)`` is critical: without it Qt
        drops the hidden widget from layout sizing, so the sibling
        label gets full width at rest and loses ~20 px on hover —
        triggering wordwrap reflow that grows the row taller and
        pushes everything below it down (visible as the whole panel
        "stretching" and the Resume button jumping). With the flag
        set, the slot is reserved at all times; hover only flips
        visibility, never the layout."""
        if widget is None:
            return
        self._reveal.append(widget)
        sp = widget.sizePolicy()
        sp.setRetainSizeWhenHidden(True)
        widget.setSizePolicy(sp)
        widget.hide()

    def enterEvent(self, event) -> None:  # type: ignore[override]
        for w in self._reveal:
            w.show()
        return super().enterEvent(event)

    def leaveEvent(self, event) -> None:  # type: ignore[override]
        for w in self._reveal:
            w.hide()
        return super().leaveEvent(event)


_PANEL_W = 400
# Visible gap (in px) between the capsule's bottom and the panel's top.
# 6 px ≈ 12 physical px on Retina — small but clearly perceived.
# The historical "6 px gap collapses to 0 visible px" bug was *not*
# about this value being too small — it was about the panel reading
# the wrong capsule geometry across the controller's state_changed
# signal fanout (see ``_position`` and CapsuleWindow.target_geometry
# for the fix). Once that race is gone, 6 px is enough.
_GAP = 6

# Bound the sessions list so a heavy user with 20+ sessions doesn't
# get a panel taller than the screen. Computed as an *exact* multiple
# of (row + gap) so the visible boundary always lands on a row edge.
#
# 5 standalone rows × _ROW_HEIGHT(52) + 4 gaps × _GROUP_GAP(8) = 292 px.
# Dropped from 6 to 5 visible rows when row height grew from 36→52 px
# (P0.3 status row); a 6-row 52 px area would push the panel ~360 px
# tall, eating into the USAGE block visibility on small monitors.
_SESSION_SCROLL_VISIBLE_ROWS = 5


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

_STYLE_PANEL = f"""
    ExpandedWindow {{
        color: white;
        font-family: {UI_FONT_STACK};
    }}
""" + TOOLTIP_QSS
# QToolTip QSS appended above — Qt's stylesheet resolution shadows the
# app-level rule when a widget calls self.setStyleSheet, so we have
# to include it here too. Single source: claude_island.ui.tooltip_style.
# Bare top-level properties + selector blocks in one stylesheet make
# Qt fail to parse the entire sheet (silent "Could not parse" warning,
# and the QToolTip block goes ignored — system default white tooltip
# would render the "Add a quota provider" hint as white-on-white).
# Wrapping the bare props under the ExpandedWindow selector forces a
# strict parse so the QToolTip override actually applies.
# v4c: card-section titles ("TODAY" / "SPEND" / "QUOTA") use paper_dim
# at 10 px with the same wide letter-spacing as before.  Routed through
# lab_palette so a future palette tweak propagates here.
_STYLE_TITLE = (
    f"color: {_LabColor.paper_dim}; font-size: 10px; "
    f"letter-spacing: 1px; font-weight: 600;"
)
_STYLE_SEP = f"background: {_LabColor.rule};"

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

# v3: panel surfaces resolve through lab_palette so the panel ↔ capsule
# ↔ recents trio share a vocabulary.  Hover/pressed tones inherit one
# step lighter than the rest position, matching the prototype's
# surface / surface_hi pair.  Group cards keep a slight warmth (the
# surface_warm token, the only place in lab_palette where warmth exists)
# so two adjacent cards still read as distinct containers vs row.
_BG_SINGLE          = _LabColor.surface         # was "#1e1e1e"
_BG_GROUP           = _LabColor.surface_warm    # was "#181818"
_BG_HOVER_SINGLE    = _LabColor.surface_hi      # was "#2a2a2a"
_BG_HOVER_IN_GROUP  = _LabColor.surface_hi      # was "#222222" — collapse the two
_BG_PRESSED         = _LabColor.surface_hi      # was "#333333"

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
# Same-project session groups are bracketed by a thin outline (1 px
# rounded border, no fill) around the rows. Dim grey so the outline
# reads as "metadata frame" not as content — sits at the "last 10 %"
# tier of visual hierarchy that dividers and outlines belong to.
# Uniform across all groups (no per-group hue) — group identity is
# conveyed by which rows the outline contains, not by colour.
_GROUP_OUTLINE_COLOR = _LabColor.rule  # v3 — was "#3a3a3a"
# Inner padding inside the group outline so rows don't touch the
# border. 4 px on all sides keeps the rows breathing without
# inflating the group's footprint much.
_GROUP_OUTLINE_PAD_PX = 4


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
    stable (it is — adapter chain emits SessionGroups in deterministic
    insertion order based on bucket key), so
    cards keep their colour across refreshes unless a group above
    them appears / disappears. Worth the trade vs the previous
    "absolutely stable but visually broken" hash."""
    return _GROUP_BG_PALETTE[idx % len(_GROUP_BG_PALETTE)]


class HoverRow(QPushButton):
    """Session row button. Three paint layers stacked on top of each
    other: base background → optional running accent (left edge) →
    optional hover accent (left edge). The running accent animates
    its alpha on a 1.4 s sine cycle (Spotify "Now Playing" / Apple
    Music sidebar pattern).

    High-cost used to live on the row's right edge as a static yellow
    bar — moved to colour-coding the cost label itself instead. The
    bar was too easy to miss against narrow row geometry; a yellow
    "$132" reads as expensive on first glance without a separate
    legend.
    """

    _ACCENT_W = 3       # px wide
    _ACCENT_INSET = 4   # px from top/bottom edges (so bar < row height)
    _RUNNING_W = 4      # the running bar is one px wider so it stands
                         # out next to the hover bar without overlap

    # Bright green that reads "alive". Same hex as the capsule's active
    # dot so the colour story is unified across surfaces.  Now sourced
    # from lab_palette so a future palette tweak doesn't drift between
    # row and pill.  ``set_phase`` rebinds this per-instance based on
    # the live SessionPhase.
    _RUNNING_COLOR = "#22c55e"

    def __init__(self, base_bg: str, parent_card: "QFrame | None" = None, **kwargs):
        super().__init__(**kwargs)
        self._base_bg = base_bg
        self._parent_card = parent_card
        self._hovered = False
        self._running = False
        self._running_alpha = 0.0  # 0..1; driven by _running_anim
        # Per-instance pulse colour — defaults to the class-level green
        # so legacy code paths that only call ``set_running(True)`` get
        # exactly the same paint output as before.  ``set_phase`` rebinds
        # this so the pulse tint matches view.phase (amber for thinking,
        # phosphor for tool_use, red-warm for waiting_approval, etc).
        self._pulse_color = self._RUNNING_COLOR
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

        # Running pulse animation. QVariantAnimation drives _running_alpha
        # via valueChanged callback (no Qt-Property metaclass dance) and
        # we call update() to repaint each tick. 1.4 s round-trip with
        # an InOutSine curve: slow enough not to be distracting, fast
        # enough to clearly read as "live".
        from PySide6.QtCore import QVariantAnimation
        self._running_anim = QVariantAnimation(self)
        self._running_anim.setDuration(1400)
        self._running_anim.setStartValue(0.30)
        self._running_anim.setKeyValueAt(0.5, 1.0)
        self._running_anim.setEndValue(0.30)
        self._running_anim.setEasingCurve(QEasingCurve.Type.InOutSine)
        self._running_anim.setLoopCount(-1)
        self._running_anim.valueChanged.connect(self._on_running_alpha)

    def _on_running_alpha(self, v: float) -> None:
        self._running_alpha = float(v)
        self.update()

    def set_running(self, running: bool) -> None:
        """Toggle the persistent left-edge pulse. Idempotent — calling
        with the same value twice is a no-op.

        Kept as the legacy entry point; v3 callers should prefer
        ``set_phase`` so the pulse tint matches the session's live phase.
        ``set_running(True)`` keeps the historical green colour by NOT
        touching ``_pulse_color`` — bool callers get the same pixels."""
        if self._running == running:
            return
        self._running = running
        if running:
            self._running_anim.start()
        else:
            self._running_anim.stop()
            self._running_alpha = 0.0
            self.update()

    def set_phase(self, phase: "SessionPhase") -> None:
        """v3 entry point: drive the left-edge pulse colour AND on/off
        state from a SessionPhase.

        Phase → behaviour:
          IDLE / ENDED         → pulse off (no left-edge bar)
          THINKING             → amber pulse
          TOOL_USE             → phosphor (green) pulse
          WAITING_APPROVAL     → red-warm pulse
          COMPACTING           → amber-dim pulse

        Falls back to ``set_running(True)`` semantics for any phase value
        we don't recognise, so a future SessionPhase addition still
        renders something visible rather than silently going inert.
        """
        from claude_island.core.session_phase import SessionPhase as _SP
        from claude_island.ui.lab_palette import Color as _LC
        if phase in (_SP.IDLE, _SP.ENDED):
            self.set_running(False)
            return
        # Rebind the pulse tint BEFORE flipping running on, so the first
        # paint after the animation starts already uses the new colour.
        try:
            self._pulse_color = _LC.for_phase(phase)
        except KeyError:
            self._pulse_color = self._RUNNING_COLOR
        # Force a repaint even if _running was already True (a phase
        # transition from THINKING → TOOL_USE doesn't change _running
        # but does change the colour).
        was_running = self._running
        self._running = True
        if not was_running:
            self._running_anim.start()
        self.update()

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
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)

        # Running pulse first (drawn behind the hover bar so a hover
        # over an active row reads as "hover on something running"
        # rather than the hover overwriting the live indicator).
        # ``_pulse_color`` defaults to _RUNNING_COLOR for legacy
        # set_running(True) callers; v3 set_phase callers rebind it to
        # the phase-specific tint before the animation starts.
        if self._running:
            color = QColor(self._pulse_color)
            color.setAlphaF(self._running_alpha)
            painter.setBrush(color)
            x = 0
            y = self._ACCENT_INSET
            w = self._RUNNING_W
            h = self.height() - 2 * self._ACCENT_INSET
            painter.drawRoundedRect(x, y, w, h, w / 2, w / 2)

        if self._hovered:
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


_DOT_GREEN = "#4ade80"    # < 1h since last activity
_DOT_YELLOW = "#facc15"   # < 24h
_DOT_GRAY = "#52525b"     # ≥ 24h
# Brighter, more saturated green for the running state — distinct from
# _DOT_GREEN (freshness) so the pulsing animation reads as "live now"
# rather than "recent". Tested at 12 px and 14 px font sizes.
#
# v3: sourced from lab_palette so the glyph and the capsule's mini-wave
# can't drift — both now resolve to Color.phosphor.  Behaviour-equivalent
# default for legacy ``set_state(STATE_RUNNING)`` callers that don't
# pass a bar_color.
_DOT_RUNNING = _LabColor.phosphor


class _RowStatusGlyph(QWidget):
    """Bi-state glyph painted in the row's leftmost slot:

    - ``running`` → 3 vertical bars that wave like an audio equalizer
      (Spotify "Now Playing" pattern). Each bar's height is animated
      between 28 % and 100 % of the widget height with a phase offset
      so the bars never sync up — that's what reads as "live" rather
      than "loading".
    - ``idle`` → single static colour-coded dot (green / yellow /
      grey based on activity recency, picked by the caller).

    High-cost is NOT a glyph state — it lives on the row's right
    edge as a static yellow bar (HoverRow.set_high_cost), so the
    leftmost slot stays a pure liveness channel. Combining both
    signals on this widget caused user confusion ("why is this ⚡
    instead of EQ when it's also running?")."""

    # 5 narrow bars (1.5 px each) read as a traveling wave; 3 bars at
    # period/3 phase offset reads as "all bouncing in sync" because
    # the eye doesn't get enough samples to perceive the wavefront.
    # Width still ~10 px total to fit the dot slot.
    _BAR_W = 1
    _BAR_GAP = 1
    _BAR_COUNT = 5
    # 1200 ms full wave traversal — slower than the previous 900 ms
    # bounce because the wave needs more time per cycle to read as
    # "scrolling left to right" rather than "all bars wiggling".
    _PERIOD_MS = 1200
    # Wave amplitude bounds. The wave height function is computed in
    # paintEvent via sin() — these bound the output of that math.
    _MIN_PCT = 0.20
    _MAX_PCT = 1.00
    _DEFAULT_W = _BAR_W * _BAR_COUNT + _BAR_GAP * (_BAR_COUNT - 1)
    # Slot width floor matches the dot label's 12 px so swapping the
    # status glyph in/out of the row layout doesn't shift other widgets.
    _MIN_SLOT_W = 12

    STATE_IDLE = "idle"
    STATE_RUNNING = "running"

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.setFixedWidth(max(self._DEFAULT_W, self._MIN_SLOT_W))
        self._state = self.STATE_IDLE
        self._dot_color = _DOT_GRAY
        # Bar colour for the RUNNING state.
        self._bar_color = _DOT_RUNNING
        # Whether to paint anything at all in IDLE state. True → draw
        # the static dot (capsule pill needs SOMETHING in this slot
        # so the pill never looks empty when no session is active).
        # False → IDLE state is a no-op paint (row mode — idle rows
        # leave the leftmost slot visually empty so RUNNING rows
        # stand out by being the only rows with anything there;
        # widget is still 12 px wide so layout stays aligned).
        self._idle_visible = True
        # Single "phase" 0..1 drives all bars — paintEvent computes
        # each bar's height from a sin() of (phase + bar_offset). One
        # animation, N bars; the wave traveling across is just sin
        # math, no per-bar animation objects. (B-002 review note:
        # this is also the cheaper path — 1 anim instead of N.)
        self._phase = 0.0
        self._anim = QVariantAnimation(self)
        self._anim.setDuration(self._PERIOD_MS)
        self._anim.setStartValue(0.0)
        self._anim.setEndValue(1.0)
        # Linear so the wave scrolls at constant speed — InOutSine
        # would make the wave appear to slow down at the loop edges,
        # breaking the illusion of continuous left-to-right motion.
        self._anim.setEasingCurve(QEasingCurve.Type.Linear)
        self._anim.setLoopCount(-1)
        self._anim.valueChanged.connect(self._on_phase)

    def _on_phase(self, value: float) -> None:
        self._phase = float(value)
        self.update()

    # Backwards-compat alias for tests that look up the old per-bar
    # animation list. Single shared animation now lives in self._anim.
    @property
    def _anims(self) -> list[QVariantAnimation]:
        return [self._anim]

    def set_state(
        self,
        state: str,
        *,
        dot_color: str | None = None,
        bar_color: str | None = None,
    ) -> None:
        """Switch between IDLE and RUNNING.

        - ``dot_color`` only affects IDLE rendering (freshness colour).
        - ``bar_color`` only affects RUNNING rendering — kept for
          callers that want a different bar tint, but the current
          design uses the standard green for all running rows
          (high-cost moved to a separate channel on the row's right
          edge, so we no longer overload the bar colour to combine
          signals).

        Idempotent — calling with the same state is a no-op so this
        is safe to call from every refresh tick. Only state
        transitions start / stop the animations."""
        # Anything other than RUNNING collapses to IDLE — defensive
        # so a typo'd or future-unknown state string doesn't crash.
        if state != self.STATE_RUNNING:
            state = self.STATE_IDLE
        if dot_color is not None:
            self._dot_color = dot_color
        if bar_color is not None:
            self._bar_color = bar_color
        if state == self._state:
            self.update()  # picks up colour changes even if state same
            return
        prev = self._state
        self._state = state
        if state == self.STATE_RUNNING and prev != self.STATE_RUNNING:
            # Single shared phase animation — paintEvent reads
            # (phase + bar_index/N) into sin() to compute each bar's
            # height. The wave traveling left-to-right falls out of
            # the math; no per-bar animation staggering needed.
            self._anim.start()
        elif prev == self.STATE_RUNNING and state != self.STATE_RUNNING:
            self._anim.stop()
        self.update()

    def set_phase(
        self,
        phase: "SessionPhase",
        *,
        idle_dot_color: str | None = None,
    ) -> None:
        """v3 entry point: drive both the wave-vs-dot state AND the
        bar's tint from a SessionPhase.

        Phase → behaviour:
          IDLE / ENDED         → static dot (uses idle_dot_color, default grey)
          THINKING             → amber wave
          TOOL_USE             → phosphor wave
          WAITING_APPROVAL     → red-warm wave
          COMPACTING           → amber-dim wave

        Underneath this just dispatches to ``set_state`` with the
        appropriate bar_color — so legacy bool callers and v3 phase
        callers compete for the same single-source-of-truth animation.
        """
        from claude_island.core.session_phase import SessionPhase as _SP
        if phase in (_SP.IDLE, _SP.ENDED):
            self.set_state(self.STATE_IDLE, dot_color=idle_dot_color)
            return
        try:
            bar = _LabColor.for_phase(phase)
        except KeyError:
            bar = _DOT_RUNNING
        self.set_state(self.STATE_RUNNING, bar_color=bar)

    def state(self) -> str:
        """Currently-displayed state (one of STATE_*)."""
        return self._state

    def set_idle_visible(self, visible: bool) -> None:
        """Toggle whether the IDLE state paints anything.

        Default True (capsule pill needs the dot so the slot never
        reads empty). Pass False from the panel rows so an idle row's
        leftmost slot stays visually empty — that way RUNNING rows
        are the only rows with anything in that slot, which is the
        whole point of "scheme 2" (running stands out by exclusion)."""
        if self._idle_visible == visible:
            return
        self._idle_visible = visible
        if self._state == self.STATE_IDLE:
            self.update()

    def paintEvent(self, event) -> None:  # type: ignore[override]
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)

        wgt_w = self.width()
        wgt_h = self.height()
        cx = wgt_w // 2
        cy = wgt_h // 2

        if self._state == self.STATE_RUNNING:
            painter.setBrush(QColor(self._bar_color))
            # Traveling-wave equalizer: each bar's height is a sin()
            # of (phase + i / N). The phase scrolls 0→1 over the
            # period, so the wavefront moves left-to-right by exactly
            # one wavelength per cycle. Bars centered vertically (top
            # + bottom grow together) so the wave reads as a true
            # waveform, not as bars rising off a floor.
            total_w = (
                self._BAR_W * self._BAR_COUNT
                + self._BAR_GAP * (self._BAR_COUNT - 1)
            )
            x0 = (wgt_w - total_w) // 2
            usable_h = max(8, wgt_h - 4)  # 2 px breathing room top/bottom
            import math
            amplitude_range = self._MAX_PCT - self._MIN_PCT
            for i in range(self._BAR_COUNT):
                # sin returns -1..1; map to MIN_PCT..MAX_PCT.
                # Two wavelengths visible across the bar set so the
                # wave reads as denser / more obviously moving.
                t = self._phase + (i / self._BAR_COUNT) * 2.0
                normalized = (math.sin(t * 2 * math.pi) + 1) / 2  # 0..1
                fraction = self._MIN_PCT + normalized * amplitude_range
                bar_h = max(2, int(usable_h * fraction))
                bx = x0 + i * (self._BAR_W + self._BAR_GAP)
                by = cy - bar_h // 2
                painter.drawRoundedRect(
                    bx, by, self._BAR_W, bar_h,
                    self._BAR_W / 2, self._BAR_W / 2,
                )
        else:
            # IDLE — single dot, colour picked by caller. Skipped
            # entirely when set_idle_visible(False) was called (row
            # mode); draws the dot for capsule mode where the pill
            # needs a constant visual anchor.
            if not self._idle_visible:
                return
            painter.setBrush(QColor(self._dot_color))
            r = 3
            painter.drawEllipse(cx - r, cy - r, r * 2, r * 2)

# Two-line row: top = dot + name + cost, bottom = model chip + status.
# 52 px holds the 13 px name plus the 11 px status row with breathing
# room top/bottom — anything shorter clipped descenders on g/y/p.
# v4c: rows grew from 52 → 68 px to fit a third line for the cwd path.
# GitHub Actions list density — the cwd is too useful to elide off the
# row entirely (it disambiguates two sessions named "master" living in
# different repos) and too long to inline next to the name.
_ROW_HEIGHT = 68
_ROW_PAD_H = 12

# Activity heuristic for the row status text. Same threshold as the
# capsule's breathing animation so "running" / "idle" reads consistently
# between the pill and the panel rows.
_ROW_ACTIVE_THRESHOLD_SECONDS = 30


class _ElasticRichLabel(QLabel):
    """A QLabel for RichText whose ``minimumSizeHint`` width is always 0.

    Default QLabel computes ``minimumSizeHint`` from the rendered
    content width — for RichText (HTML strings with ``<span>`` colour
    segments) that's the full one-line width of the rendered HTML. When
    such a label sits inside a layout whose parent has
    ``setFixedWidth(_PANEL_W=400)``, Qt resolves the conflict by
    forcing the parent's effective minimum width up to fit the
    child — producing the QWindowsWindow::setGeometry mintrack=480
    warning every layout pass.

    ``setSizePolicy(Ignored, ...)`` alone doesn't help: the size policy
    governs space *allocation*, while ``minimumSizeHint`` governs the
    layout's minimum-width *propagation*. We need to neuter the
    width-propagation explicitly. Returning width=0 tells Qt "I'm fine
    with whatever width the parent gives me" — the rendered text
    elides naturally if the parent is narrower than the content.
    """

    from PySide6.QtCore import QSize as _QSize

    def minimumSizeHint(self) -> "QSize":  # type: ignore[override]
        from PySide6.QtCore import QSize
        h = super().minimumSizeHint().height()
        return QSize(0, h)


# Plain-text alias — same minimumSizeHint=0 trick. Used for QLabels
# whose content can be longer than _PANEL_W (project names, status
# strings, spend amounts). Renames the intent so call sites read as
# "I'm not rich text, I just might get long" instead of misleadingly
# implying RichText-only use. Both names point at the same class.
_ElasticLabel = _ElasticRichLabel


class _ElidingLabel(QLabel):
    """Plain-text QLabel that visually elides with ``…`` when its text
    exceeds the allocated width, AND reports
    ``minimumSizeHint().width() = 0`` so a long string can't push the
    parent layout's minimum width past _PANEL_W.

    Pairs the layout-stability trick of `_ElasticRichLabel` with the
    visual elision users actually expect — default QLabel hard-clips
    on overflow, which looks broken (text just gets cut at the widget
    boundary mid-character). Use for labels in the main panel whose
    content is user-supplied / variable length: session row name and
    status fields, primarily.

    Important: ``text()`` returns the *full* (un-elided) string, not
    the visible elided form. This keeps callers that compare
    ``label.text()`` against a source-of-truth string (see
    `_update_row`) and tests that assert on label content working
    without surprises. The elision happens only at paint time.

    Auto-tooltip: when the visible text gets elided, the tooltip is
    automatically set to the full text (so hovering reveals what was
    truncated). Cleared back to "" when no truncation. A custom
    tooltip set via ``setToolTip(...)`` from the call site is
    respected — we only touch the tooltip when its current value
    matches what we last auto-set (tracked in ``_auto_tooltip``).
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Source-of-truth text. Diverges from QLabel's internal text
        # whenever elision kicks in (which sets the *visible* shorter
        # form via super().setText).
        self._full_text: str = super().text() or ""
        # The last tooltip value WE set via _sync_tooltip. Lets us
        # detect "user has set a custom tooltip since" — if the
        # current toolTip() differs from this, we leave it alone.
        self._auto_tooltip: str = ""

    def setText(self, text: str) -> None:  # type: ignore[override]
        # Dedupe on the *full* text — _update_row calls setText every
        # tick, and the visible (super().text()) form is the elided
        # variant which won't match the new full string. Comparing
        # against _full_text avoids re-eliding when nothing changed.
        if text == self._full_text:
            return
        self._full_text = text or ""
        self._apply_elision()

    def text(self) -> str:  # type: ignore[override]
        return self._full_text

    def resizeEvent(self, event) -> None:  # type: ignore[override]
        super().resizeEvent(event)
        self._apply_elision()

    def minimumSizeHint(self) -> "QSize":  # type: ignore[override]
        from PySide6.QtCore import QSize
        h = super().minimumSizeHint().height()
        return QSize(0, h)

    def sizeHint(self) -> "QSize":  # type: ignore[override]
        # Always report sizeHint based on the FULL text, NOT the
        # currently-displayed (possibly elided) form. Without this
        # override, QLabel.sizeHint reads its internal text — which
        # `_apply_elision` has already replaced with the shorter
        # elided string. Result: layout queries sizeHint, gets the
        # shrunken value, allocates less width, resizeEvent triggers
        # another elision pass, sizeHint shrinks further, ... a
        # feedback loop that collapses the label to ~9 chars (e.g.
        # "active now" → "active n…" → "active …" → "act…").
        # Reporting full-text width breaks the loop: layout always
        # *prefers* to give us full width; if there isn't enough,
        # we elide gracefully — but next layout pass still asks for
        # full width, so the allocation doesn't keep shrinking.
        from PySide6.QtCore import QSize
        from PySide6.QtGui import QFontMetrics
        if not self._full_text:
            return super().sizeHint()
        metrics = QFontMetrics(self.font())
        w = metrics.horizontalAdvance(self._full_text)
        h = super().sizeHint().height()
        return QSize(w, h)

    def _apply_elision(self) -> None:
        from PySide6.QtCore import Qt as _Qt
        from PySide6.QtGui import QFontMetrics
        full = self._full_text
        w = self.width()
        if w <= 0:
            # Pre-layout (constructor / before first show): no width
            # yet, so leave QLabel's internal text as the full string
            # and let the first resizeEvent re-elide once the layout
            # assigns a real width.
            if super().text() != full:
                super().setText(full)
            self._sync_tooltip(elided=False)
            return
        metrics = QFontMetrics(self.font())
        elided = metrics.elidedText(full, _Qt.TextElideMode.ElideRight, w)
        if super().text() != elided:
            super().setText(elided)
        self._sync_tooltip(elided=(elided != full))

    def _sync_tooltip(self, *, elided: bool) -> None:
        # Hover-reveal: when elided, surface the full text via tooltip
        # so users can see what was truncated. When NOT elided, clear
        # so the tooltip doesn't redundantly echo the visible string.
        # Respects user-set tooltips: if the current toolTip() differs
        # from what we last auto-set, the call site has installed its
        # own (e.g. tokens model row tooltips listing every raw model
        # id) — leave it alone.
        desired = self._full_text if elided else ""
        current = self.toolTip()
        if current != self._auto_tooltip:
            # User has overridden — don't touch. We'll never auto-set
            # again on this widget unless the user clears their custom
            # tooltip back to our last auto value.
            return
        if current != desired:
            self.setToolTip(desired)
            self._auto_tooltip = desired


def mk_label(
    text: str = "",
    *,
    elide: bool = True,
    rich: bool = False,
    object_name: str | None = None,
    style: str | None = None,
) -> QLabel:
    """Factory for panel labels. Default ``elide=True`` returns an
    `_ElidingLabel` so any new call site is safe-by-default against
    the QWindowsWindow::setGeometry mintrack=N>panel-width bug class
    (see `_ElidingLabel` docstring for the full mechanism).

    Pass ``elide=False`` for fixed-string captions ("TODAY" / "SPEND"
    / "QUOTA" etc.) that are guaranteed shorter than the panel — no
    layout-stability concern there, and a plain QLabel is cheaper at
    paint time.

    Pass ``rich=True`` for HTML content (uses `_ElasticRichLabel`,
    which neuters width-propagation but does NOT elide — eliding HTML
    would lose `<span>` tags).

    Two convenience kwargs (`object_name`, `style`) collapse the
    common "construct + setObjectName + setStyleSheet" three-liner
    into a single call so migrating bare ``QLabel(...)`` sites stays
    a one-line edit.

    Note: when both ``rich`` and ``elide`` are True, ``rich`` wins —
    `elide` defaults to True so users writing ``mk_label(html, rich=True)``
    shouldn't have to also write ``elide=False`` to silence an error.
    Eliding HTML loses ``<span>`` tags, so RichText labels skip elision
    by design."""
    if rich:
        lbl: QLabel = _ElasticRichLabel(text)
    elif elide:
        lbl = _ElidingLabel(text)
    else:
        lbl = QLabel(text)
    if object_name is not None:
        lbl.setObjectName(object_name)
    if style is not None:
        lbl.setStyleSheet(style)
    return lbl


# Cumulative-spend threshold past which a session's row dot flips to
# the high-cost ⚡ marker. The user explicitly chose to keep this
# warning on the card only (NOT the capsule) so the pill stays calm
# while individual rows still flag outliers. Number is intentionally
# round-ish — fine-grained tuning via a settings panel can come later.
_HIGH_COST_USD_THRESHOLD = 50.0

# Backwards-compat aliases for tests / call sites that referenced the
# inline P0.3 helpers. The actual lookups now live in core.models, fed
# by per-provider register_model_colors / register_model_short_names
# calls at import time. Keep the names so external imports continue
# to resolve; both delegate to the registry-aware core implementations.
_MODEL_COLOR_FALLBACK = DEFAULT_MODEL_COLOR
_resolve_model_short_name = resolve_model_short_name
_resolve_model_color = resolve_model_color

# v4c: standalone session rows are flat table rows now, not standalone
# cards.  No card-style background / radius; just a transparent surface
# with a bottom hairline so the list reads as a continuous table.
# Hover (handled by HoverRow.paintEvent) overlays a left-edge accent
# without touching the background, so the table feel survives hover.
_STYLE_SINGLE_ROW = f"""
    QPushButton {{
        background: transparent;
        border: none;
        border-bottom: 1px solid {_LabColor.rule};
        border-radius: 0;
        text-align: left;
    }}
    QPushButton:hover {{ background: {_LabColor.surface_hi}; }}
    QPushButton:pressed {{ background: {_LabColor.surface}; }}
"""
# Group card (multiple sessions sharing a cwd) keeps its rounded
# container, but the rows inside it now use a dashed inner divider —
# the card frame is the chrome, the rows are list items.
_STYLE_GROUP_CARD = f"""
    QFrame#group_card {{
        background: {_BG_GROUP};
        border: 1px solid {_LabColor.rule};
        border-radius: 6px;
    }}
"""
_STYLE_GROUP_ROW = f"""
    QPushButton {{
        background: transparent;
        border: none;
        border-bottom: 1px dashed {_LabColor.rule};
        border-radius: 0;
        text-align: left;
    }}
    QPushButton:hover {{ background: {_LabColor.surface_hi}; }}
    QPushButton:pressed {{ background: {_LabColor.surface}; }}
"""
# Separator between rows of the same group. With the group sitting on
# #181818, a #2a2a2a hairline is just barely visible — enough to read
# as "two distinct rows" without competing with the dot/name typography.
_STYLE_GROUP_ROW_SEP = (
    f"background: {_LabColor.rule}; margin-left: 12px; margin-right: 12px;"
)
# Px gap between top-level entries (cards / standalone rows). Bigger than
# the in-group row spacing so "next card" reads as a different chunk.
_GROUP_GAP = 8

_STYLE_DOT = "color: {color}; font-size: 11px;"
# v4c: row name is the primary identity, sans-serif, paper-bright.
# Cwd / meta still use mono in their own labels (kept separate).
_STYLE_NAME = (
    f"color: {_LabColor.paper}; font-size: 13px; font-weight: 600;"
)
_STYLE_AGE = f"color: {_LabColor.paper_faint}; font-size: 11px;"
# Cost label — three tiers mirror the v4c row colour ladder:
# default (paper), high (orange), legacy (yellow for severe).
# The "high" tier is what flips when cumulative spend crosses the alert
# threshold; the value being expensive IS the warning, no separate icon.
_STYLE_COST_DEFAULT = f"color: {_LabColor.paper}; font-size: 11px; font-weight: 500;"
_STYLE_COST_HIGH = (
    f"color: {_LabColor.red_warm}; font-size: 11px; font-weight: 600;"
)
# Small coloured pill label used in the row's status line. Background
# is the model's hue at 18 % alpha so the chip reads as "tinted" against
# the row bg without overpowering the name typography. Border shares
# the same hue at higher alpha for legibility.
_STYLE_MODEL_CHIP = (
    "QLabel {{"
    " color: {color};"
    " background: rgba(255, 255, 255, 0);"
    " border: 1px solid {color};"
    " border-radius: 6px;"
    " padding: 0px 6px;"
    " font-size: 10px;"
    " font-weight: 600;"
    "}}"
)
# Empty / unset chip: transparent border + transparent text so an empty
# QLabel doesn't paint a pill-shaped void on rows where the model isn't
# resolved yet. Layout still reserves zero width (empty text) — the
# crucial property is that the widget stays visible so subsequent
# ``setText`` updates trigger a normal relayout. See `_update_row` for
# why this matters (was masked by a hide()/show() pattern that left the
# chip permanently collapsed after the first JSONL backfill cycle).
_STYLE_MODEL_CHIP_EMPTY = (
    "QLabel {"
    " color: transparent;"
    " background: transparent;"
    " border: 1px solid transparent;"
    " padding: 0px;"
    "}"
)
# Status line typography: same dimmer grey as _STYLE_AGE so the text
# settles into the secondary tier; size matched to chip height.
_STYLE_STATUS = f"color: {_LabColor.paper_dim}; font-size: 10px;"
# v4c: cwd label sits between top (name + status + cost) and bottom
# (model chip).  Monospace so paths read cleanly; paper_faint so they
# settle into the secondary tier without distracting from the name.
_STYLE_CWD = (
    f"color: {_LabColor.paper_faint}; font-size: 10px; "
    f"font-family: {FontStack.mono_stack};"
)


def _row_tooltip(view: "SessionView") -> str:
    """Compose the hover tooltip for a session row.

    Mirrors open-vibe-island's session-detail summary: shows last
    prompt + last assistant response + terminal context when those
    fields are populated by the hook pipeline. Returns "" when there's
    nothing interesting to show (caller treats "" as "no tooltip").

    Format:
        Prompt: explain the new hook pipeline
        Last response: We refactored compose_session_view...
        Terminal: Windows Terminal (pane b2d0e4f0)
    """
    last_prompt = getattr(view, "last_prompt", None) or ""
    last_resp = getattr(view, "last_assistant_message", None) or ""
    jt = getattr(view, "jump_target", None)

    lines: list[str] = []
    if last_prompt:
        # Truncate display — the underlying SessionLiveState already
        # has 200-char cap from the hook boundary, but defensive
        # truncate here so future relaxation doesn't blow up the tooltip.
        clipped = last_prompt if len(last_prompt) <= 180 else last_prompt[:177] + "…"
        lines.append(f"Prompt: {clipped}")
    if last_resp:
        clipped = last_resp if len(last_resp) <= 180 else last_resp[:177] + "…"
        lines.append(f"Last response: {clipped}")
    if jt is not None:
        ta = (jt.terminal_app or "").strip()
        guid = (jt.wt_session_guid or "").strip()
        if ta:
            if guid:
                # Show first 8 chars of guid — full is too long for tooltip
                lines.append(f"Terminal: {ta} (pane {guid[:8]})")
            else:
                lines.append(f"Terminal: {ta}")
    return "\n".join(lines)


def _row_status_text(
    view: "SessionView",
) -> str:
    """Compose the bottom-line text for a row.

    Open-vibe-island parity (2026-05-14): when hook data is present we
    show what Claude is actually *doing right now* (Bash, Read, etc.)
    instead of just the last_activity age. This mirrors their
    ``AgentSession.summary`` which renders things like "Running Bash"
    or "Needs approval".

    Phase-to-text mapping:
      * TOOL_USE + current_tool → ``"Running {tool}"``
      * WAITING_APPROVAL → ``"Needs approval"``  (+ tool name when known)
      * THINKING → ``"Thinking..."``
      * COMPACTING → ``"Compacting context"``
      * ENDED → ``"Ended"``
      * IDLE / unknown → ``"active <relative>"`` (legacy text)

    The "active" prefix on the legacy text disambiguates this from
    the popup's "Created <relative>" line.
    """
    # Lazy import to avoid making this module depend on session_phase
    # at import time (UI tests construct SessionView via _degraded_view
    # which doesn't always go through phase resolution).
    from claude_island.core.session_phase import SessionPhase

    phase = getattr(view, "phase", None)
    if phase == SessionPhase.TOOL_USE:
        tool = (getattr(view, "current_tool", None) or "").strip()
        return f"Running {tool}" if tool else "Running tool"
    if phase == SessionPhase.WAITING_APPROVAL:
        return "Needs approval"
    if phase == SessionPhase.THINKING:
        return "Thinking…"
    if phase == SessionPhase.COMPACTING:
        return "Compacting context"
    if phase == SessionPhase.ENDED:
        return "Ended"
    # IDLE or any unrecognized value falls back to the legacy text.
    return f"active {_fmt_started(view.last_activity)}"
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
# Single dropdown selector — replaced the 5-tab pill strip in P1.2.
# Compact (matches the old pill height); arrow indicator on the right
# makes it scan as "selectable" without needing extra label.
_STYLE_PERIOD_COMBO = """
    QComboBox {
        color: #c9c9c9;
        background: transparent;
        border: 1px solid #333;
        border-radius: 8px;
        padding: 2px 8px;
        font-size: 10px;
        min-width: 80px;
    }
    QComboBox:hover { color: #e8e8e8; border-color: #555; }
    QComboBox::drop-down {
        border: none;
        width: 16px;
    }
    QComboBox::down-arrow {
        image: none;
    }
    QComboBox QAbstractItemView {
        background: #1e1e1e;
        color: #e0e0e0;
        border: 1px solid #333;
        selection-background-color: #2e2e2e;
        outline: none;
    }
"""
# "▸ details" disclosure for the SPEND token breakdown.
# Borderless link-styled button — reads as a hyperlink so the user
# expects a click-to-expand interaction without any other affordance.
_STYLE_DETAILS_DISCLOSURE = """
    QPushButton {
        color: #9ca3af;
        background: transparent;
        border: none;
        font-size: 10px;
        text-align: left;
        padding: 2px 0;
    }
    QPushButton:hover { color: #e8e8e8; }
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
# SPEND uses the same bg as the other cards (TODAY / SESSIONS / QUOTA).
# Was {_BG_SINGLE} (#1e1e1e) — that lighter shade was a holdover from
# when SPEND had a different role; in the unified card system every
# card shares one bg so the grid reads as one family.
_STYLE_USAGE_PERIOD_CARD = f"""
    QFrame#usage_period_card {{
        background: {_BG_GROUP};
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

# Summary card's quota progress bar style. ``__CHUNK__`` is a marker
# that ``_refresh_summary_card`` rewrites to the threshold colour
# (green/amber/red) at refresh time — Qt stylesheets don't support
# variable substitution out of the box, and the alternative (per-state
# CSS classes) is more code for the same effect.
_STYLE_SUMMARY_PROGRESS = (
    "QProgressBar {"
    " background: #2a2a2a;"
    " border: none;"
    " border-radius: 3px;"
    "}"
    "QProgressBar::chunk {"
    " background: __CHUNK__;"
    " border-radius: 3px;"
    "}"
)
# Per-model breakdown: bumped from #6b7280 to #9ca3af (matches the
# rest of the secondary text in the panel) so the "where did the $
# go" line is actually scannable instead of a dim afterthought.
_STYLE_USAGE_MODEL = "color: #9ca3af; font-size: 11px;"
_STYLE_USAGE_PERIOD_NAME = "color: #c9c9c9; font-size: 12px;"
_STYLE_USAGE_PERIOD_TOTAL = "color: #f5f5f5; font-size: 13px; font-weight: 500;"
_STYLE_USAGE_TOKEN_ROW = "color: #9ca3af; font-size: 11px;"

# Quota progress-bar colour thresholds — re-exported from the shared
# core palette so this surface and the capsule mini-bar can't drift
# apart on what counts as warn vs critical.
#
# ``_BAR_*`` are imported under their local names so the parity test
# in ``tests/core/test_quota_palette.py`` can grep for them and assert
# this module hasn't quietly redefined the colours locally.
from claude_island.core.quota_palette import (
    BAR_GREEN as _BAR_GREEN,
    BAR_AMBER as _BAR_YELLOW,
    BAR_RED   as _BAR_RED,
    BAR_STALE as _BAR_STALE,
    quota_bar_color as _quota_bar_color_core,
)

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


def _phase_icon_char(phase) -> str:
    """v4c: single-character glyph painted in the row's leftmost slot.

    Mirrors prototype-v4c-github.html's .row[data-phase=...] .ico
    selectors.  Returns Unicode characters that all major macOS /
    Windows / Linux fonts render — no SVG or icon font dependency.
    """
    from claude_island.core.session_phase import SessionPhase as _SP
    return {
        _SP.IDLE:             "○",
        _SP.THINKING:         "◐",
        _SP.TOOL_USE:         "▶",
        _SP.WAITING_APPROVAL: "!",
        _SP.COMPACTING:       "⇕",
        _SP.ENDED:            "✓",
    }.get(phase, "·")


def _phase_inline_text(view) -> str:
    """v4c: phase-tinted status text that sits inline next to the
    session name on the top row.

    Format follows prototype-v4c-github.html's .row .status-text:
      THINKING         → "· thinking · turn N"     (turn count if known)
      TOOL_USE         → "· tool_use · ToolName"   (current tool)
      WAITING_APPROVAL → "· awaiting consent · Xs elapsed"
      COMPACTING       → "· compacting"
      IDLE             → ""   (the bottom-row "active Xm ago" already
                                 carries this; no need to duplicate)
      ENDED            → "· ended"
    """
    from claude_island.core.session_phase import SessionPhase as _SP
    phase = getattr(view, "phase", _SP.IDLE)
    if phase == _SP.THINKING:
        return "· thinking"
    if phase == _SP.TOOL_USE:
        tool = getattr(view, "current_tool", None)
        return f"· tool_use · {tool}" if tool else "· tool_use"
    if phase == _SP.WAITING_APPROVAL:
        return "· awaiting consent"
    if phase == _SP.COMPACTING:
        return "· compacting"
    if phase == _SP.ENDED:
        return "· ended"
    return ""


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


def _short_uuid(uuid: str) -> str:
    """First 8 chars of a UUID, or "—" when missing.
    Matches the convention used by `git log --oneline` for commits."""
    if not uuid:
        return "—"
    return uuid[:8]


def _transcript_path_for_display(project_path: Path, session_uuid: str) -> Path:
    """``~/.claude/projects/<hash>/<uuid>.jsonl`` for the popup's
    Transcript row. Single source of truth for the path the user sees;
    the OS backend recomputes the same path when actually opening the
    file. Two computations stay in sync because both call
    ``core.models.project_hash`` on the same cwd."""
    from claude_island.core.models import project_hash as _ph
    return Path.home() / ".claude" / "projects" / _ph(project_path) / f"{session_uuid}.jsonl"


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


# Re-exported from core/formatting.py so this module's many call sites
# (popup, row labels, summary card) keep using the local name without
# churn. The canonical implementation lives in core/ because per-surface
# compute selectors (F4 dedup) need it too.
from claude_island.core.formatting import fmt_started as _fmt_started


def _view_render_signature(v: "SessionView") -> tuple:
    """The per-view fields that drive dedup for ExpandedWindow.

    Used by ``ExpandedWindow.compute`` (F4). Each field is either a
    primitive whose change we want to react to (``is_running``,
    ``latest_model``, ...), or a UI text string produced by the
    same formatter the row painters use (``_fmt_started`` for
    last_activity, ``_fmt_money`` for cost). Microsecond ticks
    within a quantisation window produce the same string and
    therefore the same tuple — dedup skips the no-op render.

    Adding a field here means "the panel actually displays it";
    not adding it means "micro-changes don't trigger re-render".
    Free-standing function (not a SessionView method) so it stays
    in the UI layer where the formatting decisions belong.
    """
    return (
        v.pid,
        v.name,
        v.is_running,
        v.is_high_cost,
        v.latest_model,
        v.status_word,
        _fmt_started(v.last_activity),
        _fmt_money(v.cost_usd),
    )


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

    Wraps ``core.quota_palette.quota_bar_color`` so callers in this
    module can pass ``stale`` positionally (the core API requires it
    as a kwarg)."""
    return _quota_bar_color_core(pct, stale=stale)


# Same re-export pattern as _fmt_started above — canonical impl in core.
from claude_island.core.formatting import fmt_money as _fmt_money



from claude_island.core.snapshot import SessionGroup


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
        view: SessionView,
        parent: QWidget | None = None,
        *,
        on_rename: "Callable[[str, str], bool] | None" = None,
        on_open_folder: "Callable[[], bool] | None" = None,
        on_open_transcript: "Callable[[], bool] | None" = None,
        on_strip_thinking: "Callable[[], bool] | None" = None,
        # Bidirectional Hooks v1 (G8): per-session "Review prompts" toggle.
        # When set, the popup renders a checkbox row reflecting current
        # review-mode state and lets the user toggle it. None ⇒ feature
        # disabled (no checkbox shown). The wiring layer injects:
        #   get_review_mode  → SessionPermissionCache.is_review
        #   set_review_mode  → SessionPermissionCache.set_review
        get_review_mode: "Callable[[str], bool] | None" = None,
        set_review_mode: "Callable[[str, bool], None] | None" = None,
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
        # SessionView is the pre-resolved snapshot the list rows render
        # off, so the popup's "active <ago>" subtitle uses the same
        # last_activity (per-uuid meta-merged) as the row — no drift.
        self._view = view
        # All three callbacks are caller-supplied closures that route
        # to the dispatcher in production. Each returns bool — True on
        # success, False on missing capability / failure. The popup
        # itself holds zero platform knowledge. ``_cb`` suffix avoids
        # name collisions with same-named slot methods (_on_open_folder
        # is also a Qt slot below).
        self._on_rename = on_rename
        self._on_open_folder_cb = on_open_folder
        self._on_open_transcript_cb = on_open_transcript
        self._on_strip_thinking_cb = on_strip_thinking
        self._get_review_mode = get_review_mode
        self._set_review_mode = set_review_mode
        # Inline rename state — only populated while the user is editing.
        self._name_label: QLabel | None = None
        self._name_edit: "QLineEdit | None" = None
        self._edit_btn: QPushButton | None = None
        # LAST PROMPT section — built lazily in _build_prompt_section
        # via the shared LastPromptSection widget. The widget owns its
        # own collapse state; the popup just keeps a handle so showEvent
        # can call refit() once the widget tree is realised.
        self._prompt_section: "LastPromptSection | None" = None

        # Footer action buttons — assigned in _build_footer; some are
        # None when the corresponding action isn't available (e.g.
        # _repair_btn when there's no transcript uuid).
        self._copy_id_btn: QPushButton | None = None
        self._open_folder_btn: QPushButton | None = None
        self._repair_btn: QPushButton | None = None
        # Backwards-compat alias kept until tests are updated; will be
        # the same widget as _repair_btn.
        self._repair_icon: QPushButton | None = None
        # Status line shared by all footer actions. elide=False because
        # setWordWrap(True) below is the chosen overflow strategy.
        self._repair_status: QLabel = mk_label("", elide=False)
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
        review_section = self._build_review_section()
        if review_section is not None:
            root.addWidget(self._divider())
            root.addWidget(review_section)
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
            f"    font-family: {UI_FONT_STACK};"
            "}"
            "QLabel:hover, QFrame:hover, QWidget:hover {"
            "    background: transparent;"
            "}"
            "QPushButton:hover {"
            "    background: transparent;"
            "}"
            + TOOLTIP_QSS
        )
        self.adjustSize()

    # ------------------------------------------------------------------
    # Size-hint clamp — defense in depth for setFixedWidth(_PANEL_W).
    # Same rationale as ExpandedWindow.minimumSizeHint — see there.
    # ------------------------------------------------------------------
    def minimumSizeHint(self) -> "QSize":  # type: ignore[override]
        from PySide6.QtCore import QSize
        h = super().minimumSizeHint().height()
        return QSize(_PANEL_W, h)

    def sizeHint(self) -> "QSize":  # type: ignore[override]
        from PySide6.QtCore import QSize
        h = super().sizeHint().height()
        return QSize(_PANEL_W, h)

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
        # Italic subtitle = "what Claude would have called this session
        # if the user hadn't customized the name". Two-tier resolution:
        #
        #   1. Prefer ``ai_title`` (the AI-generated descriptive title
        #      from the JSONL ai-title row) — works whether or not the
        #      user renamed, so unmodified sessions still surface their
        #      AI title underneath the auto name.
        #   2. Else fall back to ``original_name`` (Claude Code's auto
        #      name from sessions/<pid>.json), or the project basename
        #      if that's missing too. The basename leg catches sessions
        #      whose state file was never written — without it, those
        #      renamed sessions would show no italic at all because
        #      there's no anchor to display.
        #
        # Tier 2 deliberately excludes ai_title even though it'd be
        # available — tier 1 has already filtered the case where it
        # could be a useful subtitle; reaching tier 2 means ai_title
        # is either missing or already echoes the title.
        if d and d.ai_title and d.ai_title != title:
            subtitle_ai = d.ai_title
        else:
            fallback = (
                (d.original_name if d and d.original_name else None)
                or (self._view.project_path.name or None)
            )
            subtitle_ai = (
                fallback if fallback and fallback != title else None
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
        name = mk_label(title)
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
            # Tests reach for ``_repair_icon``; production reads
            # ``_repair_btn``. Same widget, two attribute names —
            # cheaper than renaming through every test assertion.
            self._repair_icon = self._repair_btn
            head.addWidget(self._repair_btn)
        layout.addLayout(head)

        # Line 2: dim "active <relative> · v<version>" subtitle.
        # The "active" tag intentionally MATCHES the list-row time so
        # the popup and the row carry the same number for the same
        # field. The labelled "Created <abs> · <rel>" row in the meta
        # section below is the canonical place for the creation time;
        # the unlabelled top-of-popup time is now strictly the
        # last-activity recency. Either part of the subtitle can be
        # missing — only show the dot separator when both sides exist.
        sub_parts: list[str] = []
        if self._view.last_activity is not None:
            sub_parts.append(f"active {_fmt_started(self._view.last_activity)}")
        if d and d.cc_version:
            sub_parts.append(f"v{d.cc_version}")
        if sub_parts:
            sub = mk_label(" · ".join(sub_parts))
            sub.setStyleSheet("color: #6b7280; font-size: 11px;")
            layout.addWidget(sub)

        # Line 3 (optional): AI-generated title shown only when it
        # differs from the displayed name (so we don't echo the same
        # string twice).
        if subtitle_ai:
            # elide=False: setWordWrap(True) below is the chosen
            # overflow strategy. _ElidingLabel + wordWrap conflict —
            # eliding forces single-line, which prevents the label
            # from reporting the multi-line height it needs, shrinking
            # the header section.
            ai = mk_label(subtitle_ai, elide=False)
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
        k = mk_label("ID", elide=False)
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
            v = mk_label("—", elide=False)
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
        pk = mk_label("Path", elide=False)
        pk.setStyleSheet("color: #6b7280; font-size: 11px;")
        pk.setFixedWidth(54)
        pk.setAlignment(Qt.AlignmentFlag.AlignTop)
        pk.setToolTip("Project path")
        path_h.addWidget(pk)
        # Path value is clickable to open folder (same as the ↗ button
        # revealed on hover — clicking the text should open, not copy).
        # elide=False: setWordWrap(True) below is the chosen overflow
        # strategy for long paths (eliding would lose path tail info).
        pv = mk_label(str(self._view.project_path), elide=False)
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

        # Transcript row — displays the .jsonl path and opens it with
        # the user's default app for the extension. Suppressed when no
        # session uuid is resolved (file path would be ill-formed —
        # ``<project_dir>/.jsonl`` — and clicking would no-op anyway).
        if sess_uuid:
            transcript_path = _transcript_path_for_display(
                self._view.project_path, sess_uuid,
            )
            tr_row = _HoverRevealRow()
            tr_h = QHBoxLayout(tr_row)
            tr_h.setContentsMargins(0, 0, 0, 0)
            tr_h.setSpacing(4)
            tk = mk_label("Transcript", elide=False)
            tk.setStyleSheet("color: #6b7280; font-size: 11px;")
            tk.setFixedWidth(54)
            tk.setAlignment(Qt.AlignmentFlag.AlignTop)
            tk.setToolTip("Session transcript file (.jsonl)")
            tr_h.addWidget(tk)
            tv = mk_label(str(transcript_path), elide=False)
            tv.setStyleSheet("color: #e8e8e8; font-size: 12px;")
            tv.setWordWrap(True)
            tv.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
            tv.setCursor(Qt.CursorShape.PointingHandCursor)
            tv.setToolTip("Open transcript in default app")
            tv.mousePressEvent = lambda _: self._on_open_transcript()
            tr_h.addWidget(tv, 1)
            tr_open = QPushButton("↗")
            tr_open.setStyleSheet(_STYLE_TEXT_LINK)
            tr_open.setCursor(Qt.CursorShape.PointingHandCursor)
            tr_open.setToolTip("Open transcript in default app")
            tr_open.setFixedWidth(16)
            tr_open.clicked.connect(self._on_open_transcript)
            tr_h.addWidget(tr_open)
            tr_row.register_reveal(tr_open)
            layout.addWidget(tr_row)

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
        title_lbl = mk_label("TOKENS", elide=False)
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
            tail = mk_label(" · ".join(extras))
            tail.setStyleSheet("color: #c9c9c9; font-size: 11px;")
            tail.setAlignment(Qt.AlignmentFlag.AlignRight)
            head.addWidget(tail)
        layout.addLayout(head)

        if not rows:
            empty = mk_label(
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
        with the input / output / cache-write / cache-read token
        breakdown. Mirrors the SPEND card layout so the two surfaces
        share visual language. Terms are spelled out (no ``cw``/``cr``
        abbreviations) so the user doesn't need a glossary."""
        row = QWidget()
        v = QVBoxLayout(row)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(1)

        top = QHBoxLayout()
        top.setSpacing(6)

        name = mk_label(r.label)
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

        cost = mk_label(_fmt_money(r.cost_usd), elide=False)
        cost.setStyleSheet("color: #e8e8e8; font-size: 12px;")
        cost.setFixedWidth(54)
        cost.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        top.addWidget(cost)
        v.addLayout(top)

        # Sub-line: dim token breakdown. "cache w / cache r" instead
        # of "cache write / cache read" — the longer form pushed the
        # line past the popup's fixed width and got truncated on the
        # right edge. The short form keeps "cache" so the meaning is
        # still obvious without a glossary.
        parts = [
            f"input {_fmt_tokens(r.input_tokens)}",
            f"output {_fmt_tokens(r.output_tokens)}",
        ]
        if r.cache_creation_tokens or r.cache_read_tokens:
            parts.append(f"cache w {_fmt_tokens(r.cache_creation_tokens)}")
            parts.append(f"cache r {_fmt_tokens(r.cache_read_tokens)}")
        # elide=False: setWordWrap(True) below is the chosen strategy.
        tokens = mk_label("  " + " · ".join(parts), elide=False)
        tokens.setStyleSheet(_STYLE_AGE)
        tokens.setWordWrap(True)
        # NO setSizePolicy(Expanding, …) — see _build_spend_model_row's
        # token_lbl for the same defensive note. wordWrap + Expanding
        # made Qt demand a 480 px minimum width and froze the layout
        # against the popup's fixed width.
        v.addWidget(tokens)
        return row

    def _build_review_section(self) -> QWidget | None:
        """Per-session "Review prompts before send" toggle (G8).

        Returns None when the wiring layer didn't inject the
        review-mode getter/setter — the toggle is hidden entirely so
        users in tests / unwired paths see exactly today's UI.
        """
        if self._get_review_mode is None or self._set_review_mode is None:
            return None
        sess_uuid = self._effective_uuid()
        if not sess_uuid:
            return None
        from PySide6.QtWidgets import QCheckBox, QLabel
        wrap = QFrame()
        wrap.setStyleSheet("background: transparent;")
        layout = QVBoxLayout(wrap)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)
        title = QLabel("PROMPT REVIEW")
        title.setStyleSheet(
            "QLabel { color: #6b7280; font-size: 10px; font-weight: 600; "
            "letter-spacing: 0.5px; }"
        )
        layout.addWidget(title)
        cb = QCheckBox("Review prompts before sending to Claude")
        cb.setStyleSheet(
            f"QCheckBox {{ color: #cdd2d8; font-size: 12px; "
            f"font-family: {UI_FONT_STACK}; }}"
        )
        try:
            cb.setChecked(bool(self._get_review_mode(sess_uuid)))
        except Exception:
            cb.setChecked(False)
        # Snap the closure to a captured uuid so subsequent toggles after
        # a popup-reuse don't accidentally apply to a different session.
        captured_uuid = sess_uuid
        cb.toggled.connect(
            lambda enabled: self._on_review_toggled(captured_uuid, enabled)
        )
        layout.addWidget(cb)
        hint = QLabel(
            "When ON, claude-island shows a card on every prompt for "
            "Allow / Block / Inject context."
        )
        hint.setStyleSheet(
            "QLabel { color: #6b7280; font-size: 10px; padding-top: 2px; }"
        )
        hint.setWordWrap(True)
        layout.addWidget(hint)
        return wrap

    def _on_review_toggled(self, sess_uuid: str, enabled: bool) -> None:
        """Persist the toggle change. The setter is the single place where
        SessionPermissionCache.set_review is called from the UI."""
        if self._set_review_mode is None:
            return
        try:
            self._set_review_mode(sess_uuid, enabled)
        except Exception:
            # Swallow — the next click will retry. Failure here is
            # cosmetic (UI checkbox state already changed); the worst
            # case is a single missed event the user can re-trigger.
            pass

    def _build_prompt_section(self) -> QWidget | None:
        """LAST PROMPT — delegates to the shared LastPromptSection widget
        so SessionDetailPopup and RecentsDrawer's preview pane stay
        visually identical. Returns None when no prompt is recorded
        so the caller can skip the divider above it too."""
        d = self._details
        if not d or not d.last_prompt:
            return None
        from claude_island.ui.last_prompt_section import LastPromptSection
        # Inner width = popup width minus root margins (14 left + 14 right)
        # minus a small safety pad so the ellipsis doesn't touch the edge.
        section = LastPromptSection(
            d.last_prompt,
            available_width=max(40, _PANEL_W - 28 - 4),
        )
        # Re-fit popup geometry whenever the section grows / shrinks so
        # the popup doesn't clip the expanded QTextEdit.
        section.expansion_changed.connect(lambda _expanded: self.adjustSize())
        self._prompt_section = section
        return section


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
        path = self._view.project_path
        if self._on_open_folder_cb is None:
            self._show_status("Open-folder action is not wired", color="#ef4444")
            return
        if self._on_open_folder_cb():
            self._show_status(f"✓ Opened {path}", color="#9ca3af")
        else:
            self._show_status(f"❌ Failed to open {path}", color="#ef4444")

    def _on_open_transcript(self) -> None:
        if self._on_open_transcript_cb is None:
            self._show_status("Open-transcript action is not wired", color="#ef4444")
            return
        if self._on_open_transcript_cb():
            self._show_status("✓ Opened transcript", color="#9ca3af")
        else:
            self._show_status("❌ Failed to open transcript", color="#ef4444")

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
        # Pop the edit reference into a local FIRST and clear self._name_edit
        # before any layout / Qt calls. If a Qt operation below raises, the
        # popup state stays consistent ("not editing") instead of leaving a
        # dangling reference to a half-deleted widget that the next
        # _enter_rename_mode would skip past.
        edit = self._name_edit
        self._name_edit = None
        idx = self._name_head_layout.indexOf(edit)
        if idx >= 0:
            self._name_head_layout.removeWidget(edit)
        edit.deleteLater()
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
            ok = self._on_rename(sess_uuid, new_name)
        except Exception as exc:
            self._show_status(f"Rename failed: {exc}", color="#ef4444")
            self._exit_rename_mode()
            return
        if not ok:
            self._show_status("Rename failed (action returned False)", color="#ef4444")
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

    def _on_strip_thinking(self) -> None:
        """Delegate to the injected callback (which routes through the
        dispatcher → AppBackend.reset_thinking).

        The callback returns bool only — granular count/error info is
        intentionally not surfaced because the .bak file beside the
        original is the durable evidence the user can inspect to
        confirm what changed."""
        if self._on_strip_thinking_cb is None:
            self._show_status("Strip-thinking action is not wired", color="#ef4444")
            return
        if self._on_strip_thinking_cb():
            self._show_status(
                "✓ Stripped thinking blocks (.bak saved). "
                "Exit (Ctrl+C) and re-enter the session — Claude can "
                "now continue from this transcript.",
                color="#4ade80",
            )
            # Disable so a panicked re-click doesn't write a second
            # backup over an already-clean file.
            if self._repair_btn is not None:
                self._repair_btn.setEnabled(False)
                self._repair_btn.setText("Done")
        else:
            self._show_status(
                "❌ Failed to strip thinking blocks. Transcript may be "
                "missing, locked, or the session has no recorded uuid.",
                color="#ef4444",
            )

    def _show_status(self, text: str, *, color: str) -> None:
        self._repair_status.setText(text)
        self._repair_status.setStyleSheet(f"color: {color}; font-size: 11px;")
        self._repair_status.show()
        self.adjustSize()

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
        # widget tree is realised — pre-show QFontMetrics under-reports
        # CJK glyph widths and would skip the toggle for prompts that
        # do need eliding once rendered. The section's own showEvent
        # also calls refit, but a forwarded call here keeps the order
        # consistent with the bar-width refresh above.
        if self._prompt_section is not None:
            self._prompt_section.refit()

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
        dot = mk_label("·", elide=False)
        dot.setStyleSheet("color: #4a4a4a; font-size: 11px;")
        return dot

    def _kv_row(self, key: str, value: str) -> QWidget:
        """Two-column ``Key   value`` row. Key is fixed-width gray,
        value is white and wraps if long."""
        row = QWidget()
        h = QHBoxLayout(row)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(8)
        # k: setFixedWidth(54) below caps width, elide=False is fine.
        k = mk_label(key, elide=False)
        k.setStyleSheet("color: #6b7280; font-size: 11px;")
        k.setFixedWidth(54)
        k.setAlignment(Qt.AlignmentFlag.AlignTop)
        h.addWidget(k)
        # v: setWordWrap(True) is the chosen overflow strategy.
        v = mk_label(value, elide=False)
        v.setStyleSheet("color: #e8e8e8; font-size: 12px;")
        v.setWordWrap(True)
        h.addWidget(v, 1)
        return row

    def _title_text(self) -> str:
        d = self._details
        return (
            (d.name if d and d.name else None)
            or (d.ai_title if d and d.ai_title else None)
            or self._view.project_path.name
            or str(self._view.project_path)
        )

    def _effective_uuid(self) -> str:
        """The session_uuid the composer actually used. Falls back to
        the Session's own session_uuid field (often empty when coming
        straight from ProcessScanner)."""
        if self._details and self._details.effective_uuid:
            return self._details.effective_uuid
        return self._view.session_uuid or ""


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
            f"    font-family: {UI_FONT_STACK};"
            "}"
            "QLabel { color: #e8e8e8; }"
            + TOOLTIP_QSS
        )

        root = QVBoxLayout(self)
        root.setContentsMargins(16, 16, 16, 14)
        root.setSpacing(10)
        self._build_body(root)
        self.adjustSize()

    # Size-hint clamp — defense in depth for setFixedWidth(self._DIALOG_W).
    # Same rationale as ExpandedWindow.minimumSizeHint — see there.
    def minimumSizeHint(self) -> "QSize":  # type: ignore[override]
        from PySide6.QtCore import QSize
        h = super().minimumSizeHint().height()
        return QSize(self._DIALOG_W, h)

    def sizeHint(self) -> "QSize":  # type: ignore[override]
        from PySide6.QtCore import QSize
        h = super().sizeHint().height()
        return QSize(self._DIALOG_W, h)

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
        title = mk_label("Add provider", elide=False)
        title.setStyleSheet("color: #e8e8e8; font-size: 13px; font-weight: 500;")
        root.addWidget(title)

        # Empty state: nothing left to add. Skip the radio + form
        # entirely and offer a single Close button.
        if not self._configurable:
            msg = mk_label(
                "All available providers are already configured.\n\n"
                "Edit ~/.claude-island/providers.json directly to update "
                "tokens or add a custom provider.",
                elide=False,  # setWordWrap(True) below is the strategy
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
        # elide=False: setWordWrap(True) below is the strategy.
        self._status = mk_label("", elide=False)
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
                # elide=False: setWordWrap(True) below is the strategy.
                help_lbl = mk_label(val, elide=False)
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
            # setFixedWidth(80) below caps width, elide=False is fine.
            label = mk_label(key, elide=False)
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

    User actions (row click → FOCUS, popup buttons → REVEAL_CWD / RENAME /
    RESET_THINKING) all flow through the injected ``dispatch`` callable.
    The default no-op makes the UI safe to construct in tests without
    wiring the full platform stack; production injects
    ``TerminalDispatcher.dispatch`` from __main__.py. The UI itself never
    imports platform code — capability routing is the dispatcher's job.
    """

    def __init__(
        self,
        capsule: QWidget,
        controller: IslandController,
        get_usage_totals: Callable[[str], UsageTotals],
        on_refresh_clicked: Callable[[], None] | None = None,
        get_session_details: Callable[[Session], SessionDetails] | None = None,
        available_providers: list[str] | None = None,
        selected_provider: str | None = None,
        on_provider_selected: Callable[[str], None] | None = None,
        get_totals_by_model: Callable[[str], "tuple[ModelTotals, ...]"] | None = None,
        get_quota_snapshot: Callable[[], "QuotaSnapshot | None"] | None = None,
        on_provider_config_changed: Callable[[], None] | None = None,
        dispatch: "Callable[..., bool] | None" = None,
        # Provider settings hooks — injected so the UI never imports
        # platform_.providers directly (import-linter contract: ui must
        # not depend on platform). All three are None-able for tests.
        list_configurable_providers: "Callable[[], list[tuple[str, dict]]] | None" = None,
        save_provider_settings: "Callable[[str, dict], None] | None" = None,
        delete_provider_settings: "Callable[[str], None] | None" = None,
        # Bidirectional Hooks v1 (2026-05-14): callbacks injected by the
        # wiring layer for the ApprovalCard / PromptReviewCard list. None
        # ⇒ the cards are still rendered (so existing UI tests don't break)
        # but the click is a no-op (registry resolve never called). In
        # production, ``resolve_decision`` points at
        # ``PendingDecisionRegistry.resolve``.
        resolve_decision: "Callable[[str, object], bool] | None" = None,
        # G8 per-session "Review prompts before send" toggle: getter +
        # setter passed straight through to SessionDetailPopup so the
        # checkbox row reflects + persists state. Both default None ⇒
        # popup hides the row entirely (matches v0 behavior + tests).
        get_review_mode: "Callable[[str], bool] | None" = None,
        set_review_mode: "Callable[[str, bool], None] | None" = None,
    ) -> None:
        super().__init__()
        self._capsule = capsule
        self._controller = controller
        self._get_usage_totals = get_usage_totals
        # Per-period per-model breakdown for the SPEND card's model
        # row. None → row stays empty (test setup). When wired in
        # __main__, points at usage_registry.get_totals_by_model.
        self._get_totals_by_model = get_totals_by_model
        # Per-provider quota snapshot for the QUOTA card. None → bars
        # stay hidden (single-provider case OR test setup). When wired,
        # the closure reads the panel's currently-selected provider so
        # tab clicks immediately re-fetch the right provider's quota.
        self._get_quota_snapshot = get_quota_snapshot
        # Optional manual-refresh hook. Wired in __main__ to bypass the
        # provider's TTL and force an immediate fetch — gives the user
        # an out when the auto-refresh hasn't caught the latest state
        # yet (cache TTL is 5 min, heartbeat is 60 s, so worst case
        # the displayed % can lag 5 min behind reality).
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
        # Three provider-settings callbacks injected by the wiring layer
        # so this widget never touches platform_.providers directly.
        # When None (test setup), the corresponding feature degrades:
        # - list_configurable: empty list → + button stays hidden
        # - save_provider_settings: dialog Save becomes a no-op
        # - delete_provider_settings: context-menu Delete becomes a no-op
        self._list_configurable_providers = list_configurable_providers
        self._save_provider_settings = save_provider_settings
        self._delete_provider_settings = delete_provider_settings
        # Bidirectional Hooks v1 — see ctor docstring above for None semantics.
        self._resolve_decision = resolve_decision
        self._get_review_mode = get_review_mode
        self._set_review_mode = set_review_mode
        self._provider_btns: dict[str, QPushButton] = {}
        # Hold a reference to the active add-provider dialog so Qt's
        # GC doesn't tear it down before the user can interact with it.
        self._add_provider_dialog: "_AddProviderDialog | None" = None
        # Diff-based row update: keep widget references keyed by pid so that
        # session ticks (every ~10s) don't tear down rows the user might be
        # hovering. The placeholder widget (no sessions) is tracked separately
        # — its presence is mutually exclusive with any row.
        self._rows: dict[int, QPushButton] = {}
        # Per-group_id card frame cache. Multi-view groups render as a
        # bordered QFrame containing flat row buttons; without this
        # cache every snap rebuilt the QFrame and re-parsed its
        # stylesheet. Cache contains only multi-view groups —
        # single-view groups return the row directly with no card
        # wrapper. Eviction mirrors _gc_rows: any group_id absent
        # from the current snap is dropped after each render.
        self._cards: dict[str, QFrame] = {}
        self._placeholder: QLabel | None = None
        # Last session snapshot — populated by _render_sessions, read
        # by user-triggered re-renders (rename) so we don't have to
        # wait for the next scan tick.
        self._latest_sessions: list[Session] = []
        # Last full WorldSnapshot — populated by render(snap). Read
        # by _on_state_changed when the user expands the panel so
        # the first frame is the latest world state, not whatever
        # we last rendered before being collapsed.
        self._latest_snap: WorldSnapshot | None = None
        # Capability dispatcher (injected). Returns bool — True when
        # the action was accepted/completed, False on missing capability,
        # missing target, or backend failure. Default no-op so unwired
        # tests that never trigger an action don't need to construct a
        # full dispatcher; production wires this to TerminalDispatcher.
        self._dispatch: "Callable[..., bool]" = dispatch or (
            lambda *args, **kwargs: False
        )

        # Sibling-window references for the auto-hide whitelist (see
        # ``event``). When the panel loses focus, we close UNLESS the
        # focus went to one of "our" related windows. Detail popups
        # are tracked here as they're created in _show_detail_popup;
        # recents drawer is injected via set_recents_drawer at boot.
        self._active_detail_popup: "QWidget | None" = None
        self._recents_drawer_window: "QWidget | None" = None

        self._setup_window()
        self._build_ui()

        # Esc closes the panel — same handle the capsule click toggles.
        # ``WindowShortcut`` (the QShortcut default) means it only fires
        # while THIS window has focus, so it doesn't conflict with the
        # recents drawer's own Esc shortcut: whichever window owns
        # focus is the one that closes.
        from PySide6.QtGui import QShortcut, QKeySequence
        QShortcut(
            QKeySequence(Qt.Key.Key_Escape), self, self._close_via_esc,
        )

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

    def event(self, e: QEvent) -> bool:  # type: ignore[override]
        """Auto-hide when focus moves to a non-related window.

        ``WindowDeactivate`` fires when our window loses keyboard
        focus to ANY other window. We want to close in that case
        UNLESS focus went to a "satellite" of ours — capsule, recents
        drawer, or the currently-open detail popup — in which case
        the user is still navigating our app and the panel should stay.

        The check runs on the next event-loop tick (``singleShot(0)``)
        because Qt updates ``QApplication.activeWindow()`` AFTER
        delivering ``WindowDeactivate``; reading it inside the handler
        would still return us.
        """
        if e.type() == QEvent.Type.WindowDeactivate:
            QTimer.singleShot(0, self._maybe_hide_on_deactivate)
        return super().event(e)

    # ── Auto-hide helpers ──────────────────────────────────────────────

    def _close_via_esc(self) -> None:
        """Esc handler — same teardown as a click on the capsule."""
        if self._controller.state == "expanded":
            self._controller.toggle_expanded()

    def _satellite_windows(self) -> set:
        """Set of top-level windows that count as "still our app" for
        the auto-hide whitelist. Built fresh each call because the
        detail popup comes and goes; capsule + recents drawer are
        stable but cheap to re-collect."""
        sats: set = set()
        if self._capsule is not None:
            sats.add(self._capsule)
        if self._recents_drawer_window is not None:
            sats.add(self._recents_drawer_window)
        if self._active_detail_popup is not None:
            sats.add(self._active_detail_popup)
        return sats

    def _maybe_hide_on_deactivate(self) -> None:
        """Deferred half of the WindowDeactivate handler. Runs after
        Qt has updated ``QApplication.activeWindow()``; closes the
        panel only when the new active window is not one of ours."""
        if not self.isVisible():
            return  # already torn down by another path (esc, capsule click)
        if self._controller.state != "expanded":
            return  # state machine already moved on
        new_active = QApplication.activeWindow()
        if new_active in self._satellite_windows():
            return  # focus went to a sibling we control — stay open
        self._controller.toggle_expanded()

    # ------------------------------------------------------------------
    # Window setup
    # ------------------------------------------------------------------

    def _setup_window(self) -> None:
        # Qt.Tool dropped on macOS — see CapsuleWindow._setup_window
        # for the NSPanel WA_TranslucentBackground rendering bug.
        flags = (
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
        )
        if sys.platform != "darwin":
            flags |= Qt.WindowType.Tool
        self.setWindowFlags(flags)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        # Intentionally NOT setting WA_ShowWithoutActivating: that attribute
        # sets WS_EX_NOACTIVATE, which makes WM_MOUSEACTIVATE return
        # MA_NOACTIVATE — clicks deliver but never make us the foreground
        # process, so SetForegroundWindow on the target terminal then fails
        # the "calling process must be foreground" rule.
        self.setFixedWidth(_PANEL_W)
        self.hide()

    # ------------------------------------------------------------------
    # Size-hint clamp — defense in depth for setFixedWidth(_PANEL_W).
    # See module-level _ElidingLabel docstring for the full bug class.
    # ------------------------------------------------------------------
    # setFixedWidth is a *request* to the layout system, not a command:
    # if a child widget's minimumSizeHint().width() exceeds _PANEL_W,
    # the layout overrides setFixedWidth and the window grows. By
    # clamping our own minimumSizeHint / sizeHint here, the window
    # itself never reports more than _PANEL_W to Qt's layout solver
    # or to the OS — the QWindowsWindow::setGeometry mintrack=N>320
    # warning can no longer fire from this widget regardless of which
    # child misbehaves. Belt-and-suspenders for mk_label() defaults.
    def minimumSizeHint(self) -> "QSize":  # type: ignore[override]
        from PySide6.QtCore import QSize
        h = super().minimumSizeHint().height()
        return QSize(_PANEL_W, h)

    def sizeHint(self) -> "QSize":  # type: ignore[override]
        from PySide6.QtCore import QSize
        h = super().sizeHint().height()
        return QSize(_PANEL_W, h)

    def _position(self) -> None:
        # Anchor the panel below the capsule. Capsule stays visible as
        # the header; panel renders the body below it.
        #
        # Read the capsule's **target_geometry** (its authoritative
        # position-and-size record) rather than ``frameGeometry()``.
        # The controller's ``state_changed`` signal fans out to the
        # capsule first (which calls setGeometry to switch dot→pill)
        # and then to this slot. On macOS, ``frameGeometry()`` can
        # still report the pre-resize bounds at this point, which
        # would put the panel's top above the capsule's actually-
        # rendered bottom edge — that's the visible-overlap bug.
        # ``target_geometry()`` is set synchronously inside the
        # capsule's own setGeometry caller, so it always reflects
        # what the capsule just decided to be.
        #
        # Use ``y() + height()`` (exclusive) instead of ``bottom()``
        # (inclusive: top + height − 1) so the math stays consistent
        # with _GAP semantics.
        #
        # ``_clamp_to_screen`` keeps the panel on-screen even on a
        # capsule that was dragged to an off-screen monitor and is
        # falling back to a default position.
        # Duck-type: real CapsuleWindow exposes target_geometry(); tests
        # inject a plain QWidget as a stand-in satellite, which only has
        # frameGeometry(). Falling back keeps the test fakes simple.
        get_geom = getattr(self._capsule, "target_geometry", None)
        cap = get_geom() if callable(get_geom) else self._capsule.frameGeometry()
        x = cap.center().x() - _PANEL_W // 2
        y = cap.y() + cap.height() + _GAP
        x, y = self._clamp_to_screen(cap, x, y)
        self.move(x, y)

    def _clamp_to_screen(self, cap: "QRect", x: int, y: int) -> tuple[int, int]:
        """Clamp ``(x, y)`` so the panel stays inside the screen that
        contains the capsule. Picks the screen by capsule center —
        works for negative-Y monitors and odd multi-monitor layouts.

        Y-clamp invariant: the panel's top must stay at or below
        ``cap.y() + cap.height() + _GAP``. If clamping into the screen
        would push the panel up into the capsule (panel too tall for
        the remaining space below the capsule), we keep it anchored
        just below the capsule and accept that the bottom may extend
        off-screen — the inner scroll area handles overflow. Visual
        overlap with the capsule is the worse outcome here; off-screen
        bottom is fine.

        Caller passes ``cap`` from ``CapsuleWindow.target_geometry()``
        (not ``frameGeometry``) so cap.height() here is the freshly
        applied capsule height, not whatever Qt's last frame report
        said.
        """
        from PySide6.QtGui import QGuiApplication
        center = cap.center()
        screen = QGuiApplication.screenAt(center) or QGuiApplication.primaryScreen()
        if screen is None:
            return x, y
        avail = screen.availableGeometry()
        w = _PANEL_W
        h = self.sizeHint().height() or self.height()
        # Horizontal: keep panel fully on-screen if it fits, else align left.
        if w <= avail.width():
            x = max(avail.left(), min(x, avail.right() - w + 1))
        else:
            x = avail.left()
        # Vertical: clamp into the screen, but never above the anchor
        # (cap.y() + cap.height() + _GAP). The max(min_y, ...) at the
        # bottom is what prevents the tall-panel-on-short-screen case
        # from pushing the panel up into the capsule.
        min_y = cap.y() + cap.height() + _GAP
        if h <= avail.height():
            y = max(avail.top(), min(y, avail.bottom() - h + 1))
        else:
            y = avail.top()
        y = max(min_y, y)
        return x, y

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        """Four-zone vertical layout:
            0. Focus summary card (today's $ + 5h quota bar) — Tier 1
            1. Bounded-scroll sessions list (max ~5 visible rows) — Tier 2
            2. SPEND card (cross-provider, period-selectable) — Tier 3
            3. QUOTA card (provider-specific, 5h + weekly bars) — Tier 3
        Summary at the top is the "glanceable" tier — big $ + a single
        progress bar so the user gets the headline numbers without
        reading anything below. SPEND + QUOTA stay as the detail tier
        for power-user inspection.
        """
        root = QVBoxLayout(self)
        root.setContentsMargins(14, 14, 14, 14)
        root.setSpacing(8)

        # ── Focus summary (Tier 1: at-a-glance headline) ────────────
        # Big "today" dollar value paired with a single 5h quota bar —
        # the two numbers a user most often opens the panel to check.
        # Placed above CLAUDE SESSIONS so a quick peek gets the answer
        # without scanning the session list at all.
        self._summary_card = self._build_summary_card()
        root.addWidget(self._summary_card)

        # ── Sessions header (with count badge + recents chip) ───────
        # Count badge makes overflow discoverable: when the list scrolls,
        # the user sees "· 14" and knows there's more below the fold.
        # The chip on the right surfaces dormant sessions (offline
        # sessions on disk with no live process) — click opens the
        # RecentsDrawer. Hidden when there are zero dormant sessions.
        # v4c: sessions title reads "N sessions · M awaiting consent"
        # instead of the all-caps "CLAUDE SESSIONS · N" — matches the
        # prototype-v4c-github.html card-head layout the user picked.
        # The label uses normal-case sans (not the v3 small-caps title
        # style) so it reads as content, not chrome.
        self._sessions_title = mk_label("0 sessions", elide=False)
        self._sessions_title.setStyleSheet(
            f"color: {_LabColor.paper}; font-size: 12.5px; font-weight: 600;"
        )
        # v4c Recents chip — rounded pill with a phosphor (green) dot
        # signalling "dormant sessions exist".  Uses lab_palette so the
        # tones stay in lock-step with everything else.
        self._recents_chip = QPushButton("● Recents · 0")
        self._recents_chip.setCursor(Qt.CursorShape.PointingHandCursor)
        self._recents_chip.setStyleSheet(
            f"QPushButton {{"
            f"  background: {_LabColor.ink};"
            f"  color: {_LabColor.paper};"
            f"  border: 1px solid {_LabColor.rule};"
            f"  border-radius: 100px;"
            f"  padding: 3px 9px;"
            f"  font-size: 11px;"
            f"  font-weight: 500;"
            f"  text-align: left;"
            f"}}"
            f"QPushButton:hover {{"
            f"  background: {_LabColor.surface_hi};"
            f"  border-color: {_LabColor.rule_bright};"
            f"}}"
        )
        self._recents_chip.setFlat(True)
        self._recents_chip.setVisible(False)
        # Toggle slot — wired from __main__.py via set_recents_toggle().
        self._recents_toggle: Callable[[], None] | None = None
        self._recents_chip.clicked.connect(self._on_recents_chip_clicked)
        # ── Pending decisions section (Bidirectional Hooks v1) ─────
        # Sits ABOVE the sessions header so any approval / question
        # card the user must act on is the first thing visible when
        # the panel expands. v2 (2026-05) renders the queue as a
        # stack of cards — only the head is interactive — to force
        # serial resolution; see ui/decisions_stack.
        from claude_island.ui.decisions_stack import StackedDecisionsPanel
        self._pending_panel = StackedDecisionsPanel(
            on_resolve=self._on_decision_resolved,
            on_focus_terminal=self._on_focus_terminal_for_decision,
        )
        root.addWidget(self._pending_panel)
        # Legacy alias — some older test scaffolding peeked at
        # ``_pending_container``. Point it at the new panel so those
        # tests keep working while v2 settles in. Remove once all
        # callers are updated.
        self._pending_container = self._pending_panel

        sessions_header = QHBoxLayout()
        sessions_header.setContentsMargins(0, 0, 0, 0)
        sessions_header.setSpacing(6)
        sessions_header.addWidget(self._sessions_title)
        sessions_header.addStretch(1)
        sessions_header.addWidget(self._recents_chip)
        root.addLayout(sessions_header)

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

        # No "USAGE" parent label any more — SPEND and QUOTA each
        # carry their own card-internal title in the unified design
        # so the parent wrapper became redundant chrome. The cards
        # are visually grouped by being adjacent + same colour + same
        # padding, which is enough.
        # No separator either — the consistent 8 px card-to-card gap
        # already does the job a 1 px line used to do, with less ink.

        # ── SPEND card: period selector + total + breakdown + I/O ──
        # v4c: SPEND card is hidden by default — its headline data
        # (today's cost / per-class tokens) is now in the TODAY summary
        # card's hero number + the stats strip below it.  Keeping the
        # widget tree in place (via setVisible(False)) preserves all
        # downstream refresh hooks (_refresh_spend_card / spend_period
        # selector / per-model rows) so callers / tests that reach
        # through `self._spend_card.findChild(...)` keep compiling.
        # A future slice can remove the build entirely after audit.
        self._spend_card = self._build_spend_card()
        self._spend_card.setVisible(False)
        root.addWidget(self._spend_card)

        # ── QUOTA card: provider tabs + 5h + weekly bars ────────────
        # v4c: hide the large QUOTA card.  Its data lives in two places:
        #   1. TODAY summary card carries the headline 5h percent + bar
        #   2. New quota_footer (added below) carries "QUOTA · Anthropic
        #      [+] · 5h N% · Weekly N% · ↻" as a single horizontal line
        # Original quota_card stays in the tree (setVisible(False)) so
        # provider tabs / refresh hooks / per-provider configs all keep
        # compiling — the footer is a sibling view of the same data.
        self._quota_card = self._build_quota_card()
        self._quota_card.setVisible(False)
        root.addWidget(self._quota_card)
        self._quota_footer = self._build_quota_footer()
        root.addWidget(self._quota_footer)

        self.setStyleSheet(_STYLE_PANEL)

    # ------------------------------------------------------------------
    # Sole entry point — render(snap)
    # ------------------------------------------------------------------

    def compute(self, snap: WorldSnapshot) -> tuple:
        """Project a ``WorldSnapshot`` into the panel's dedup key.

        Returns a tuple whose components match what the panel actually
        displays. ``ops.distinct_until_changed(key_mapper=compute)``
        in the wiring layer skips no-op renders by comparing these
        tuples.

        Why this surface keeps ``render(snap)`` instead of switching
        to ``render(data)``: row construction needs the live
        ``SessionView`` instances (to wire siblings, dispatch FOCUS
        clicks, etc.), so the data flow has to carry the raw groups
        through. We split responsibilities: ``compute`` describes
        what the panel reads (drives dedup), ``render`` does widget
        mutation (still reads ``snap`` directly).

        Per-view fields are quantised through the same UI text
        formatters the row code uses (``_fmt_started`` / ``_fmt_money``)
        so dedup precision matches display precision automatically.
        Microsecond ``last_activity`` ticks within a "5m ago" window
        produce identical strings → identical tuple → render skipped.

        IMPORTANT: do NOT include the raw ``snap.quota`` dataclass —
        ``QuotaSnapshot.fetched_at`` updates on every quota refresh
        (~every 5 min) without changing any displayed value, which
        would make dedup miss → spurious re-render → visible panel
        flash. Project to only the fields actually painted: 5h pct +
        reset time, 7d pct + reset time, stale flag, provider tag.
        """
        if snap.quota is None:
            quota_key: tuple = ()
        else:
            q = snap.quota
            quota_key = (
                q.five_hour_pct, q.five_hour_resets_at,
                q.seven_day_pct, q.seven_day_resets_at,
                q.is_stale, q.provider,
            )
        return (
            tuple(
                (g.group_id, tuple(_view_render_signature(v) for v in g.views))
                for g in snap.session_groups
            ),
            _fmt_money(snap.today_cost_usd),
            quota_key,
            snap.available_providers,
            snap.selected_provider,
            # History chip count — without these in the key, a new
            # dormant transcript wouldn't refresh the "🗂 N" badge until
            # something else triggered a render.
            len(snap.dormant_sessions),
            len(snap.launching_sessions),
            # Bidirectional Hooks v1: pending decisions drive both the
            # inline-under-row card and the orphan-container card. Without
            # (id, session_uuid) in the dedup key, a PermissionRequest
            # arriving when the rest of the world is quiet (idle session,
            # no quota tick, no cost change) would be filtered out by
            # distinct_until_changed and the approval card would never
            # appear → user couldn't decide → 598 s wait timeout → defer.
            # Fixed in code review A-001.
            tuple((d.id, d.session_uuid) for d in snap.pending_decisions),
        )

    def render(self, snap: WorldSnapshot) -> None:
        """Render the panel from a single ``WorldSnapshot``.

        Sessions are consumed from ``snap.session_groups`` — the adapter
        chain has already grouped them. UI just renders one card per
        group, one row per view.

        Pending decisions are routed entirely to the
        :class:`StackedDecisionsPanel` at the top of the panel. v1's
        bucketing into ``matched`` (inline-under-row) and ``orphans``
        (global container) was deleted in v2 because the inline path
        sat inside ``_session_scroll`` whose ``setFixedHeight`` clipped
        approval-card buttons out of view; routing every decision to
        the dedicated top panel sidesteps the clip entirely.
        """
        self._latest_snap = snap
        self._pending_panel.render(snap.pending_decisions)
        self._render_session_groups(snap.session_groups)
        self._render_cards()
        # Update history chip from dormant + launching counts (resume-offline
        # feature). Hide entirely when zero — keeps the header clean for
        # users who never closed terminals while island was running.
        self.update_recents_count(
            len(snap.dormant_sessions) + len(snap.launching_sessions)
        )

    # ── Pending-decision rendering (Bidirectional Hooks v2) ─────────
    #
    # The legacy `_render_pending_decisions` / `_get_or_create_decision_card`
    # / `_update_pending_overflow_label` chain was removed in v2 — the
    # ``StackedDecisionsPanel`` at the top of the panel owns the whole
    # queue rendering now. The fields `_decision_cards`, `_pending_layout`,
    # `_PENDING_VISIBLE_CAP`, `_pending_overflow_label` no longer exist;
    # callers should drive the stack via ``self._pending_panel.render(...)``
    # (already wired in ``render(snap)``).

    def _on_decision_resolved(self, decision_id: str, decision: object) -> None:
        """Card click handler — routes to the injected resolve callback.

        No-op when ``resolve_decision`` is not wired (default for tests).
        Production wires this to ``PendingDecisionRegistry.resolve``,
        which sets the threading.Event the HookServer thread is waiting
        on, unblocking it to write the directive in the HTTP response.
        """
        if self._resolve_decision is None:
            return
        try:
            self._resolve_decision(decision_id, decision)
        except Exception:
            # Silent swallow — no logger module-wide here. Failure mode:
            # registry resolve raises ⇒ user click does nothing visible
            # ⇒ the hook server thread hits its 600 s wait timeout ⇒
            # Claude proceeds via fail-open (defer). Acceptable.
            pass

    def _on_focus_terminal_for_decision(self, session_uuid: str) -> None:
        """QuestionCard option-pick hook — focus the terminal of the
        session the question came from so the user can press the
        matching digit key without switching apps first.

        v1 stub: wiring to ``WindowActivatorProtocol`` is the seam,
        plumbed from ``__main__.py`` in a follow-up (the activator
        isn't held by this widget today). The decision resolves
        correctly regardless; only the convenience focus is a no-op
        until wired.
        """
        del session_uuid   # intentional no-op for now; seam preserved

    # ── History chip integration ────────────────────────────────────────

    def set_recents_toggle(self, toggle: Callable[[], None]) -> None:
        """Wire the Recents chip click → RecentsDrawer.toggle. Called
        once at boot from __main__.py after the drawer is constructed.
        Kept as a setter (not a ctor arg) because ExpandedWindow already
        has a long ctor and recents is an optional/late-bound feature."""
        self._recents_toggle = toggle

    def set_recents_drawer(self, drawer: QWidget) -> None:
        """Inject the RecentsDrawer instance so the auto-hide-on-focus-
        loss check can recognise it as a sibling and stay open while the
        user is interacting with it. Optional — when unwired, the drawer
        will count as "outside" and clicking into it will collapse the
        panel."""
        self._recents_drawer_window = drawer

    def _sessions_title_text(self, total_views: int) -> str:
        """Compose the v4c session-list header text.

        Formats:
          0 sessions
          6 sessions
          6 sessions · 1 awaiting consent
          6 sessions · 3 awaiting consent

        ``awaiting consent`` is the v4c label for any PendingDecisionView
        currently in the registry — the panel already renders these via
        ``StackedDecisionsPanel`` above; the title carries the count so
        the user reads "yes there's work to do" before scrolling.
        """
        pending = 0
        snap = getattr(self, "_latest_snap", None)
        if snap is not None:
            pending = len(getattr(snap, "pending_decisions", ()) or ())
        base = f"{total_views} sessions" if total_views != 1 else "1 session"
        if pending == 0:
            return base
        suffix = "1 awaiting consent" if pending == 1 else f"{pending} awaiting consent"
        return f"{base} · {suffix}"

    def update_recents_count(self, n: int) -> None:
        """Refresh the chip's "● Recents · N" label.

        v4c: chip stays visible at n == 0 too — its presence advertises
        the ⌘J entry-point even when there are no dormant sessions yet,
        and tucking it on a "● Recents · 0" label is less surprising
        than a chip that pops in and out.  The dot tints by content:
        phosphor when there's something to resume, paper_faint when not.
        """
        if n <= 0:
            # Faint dot when nothing dormant — chip stays so ⌘J is
            # discoverable as a permanent affordance.
            text = "● Recents · 0"
            self._recents_chip.setStyleSheet(
                self._recents_chip.styleSheet().replace(
                    f"color: {_LabColor.paper};",
                    f"color: {_LabColor.paper_dim};",
                )
            )
        else:
            text = f"● Recents · {n}"
        self._recents_chip.setText(text)
        self._recents_chip.setVisible(True)

    def _on_recents_chip_clicked(self) -> None:
        if self._recents_toggle is not None:
            self._recents_toggle()

    # ------------------------------------------------------------------
    # Internal render helpers (called by render(snap))
    # ------------------------------------------------------------------

    def _render_sessions(self, sessions: "list[Session]") -> None:
        """Test seeder: wrap each Session into a SessionView in its
        own singleton group, then call the real renderer.

        Production code never reaches this — render(snap) goes straight
        to ``_render_session_groups`` with adapter-built groups. Tests
        use this to seed the panel with raw Sessions without spinning
        up the full Snapshotter + dispatcher pipeline."""
        from dataclasses import replace as _replace
        from claude_island.core.snapshot import (
            SessionGroup as _SG,
            _degraded_view as _dv,
        )
        from claude_island.core.capabilities import (
            Capability as _Cap,
            FocusGranularity as _FG,
        )
        groups: list[_SG] = []
        for s in sessions:
            v = _replace(
                _dv(s),
                adapter_id="test",
                focus_granularity=_FG.APP,
                capabilities=frozenset({_Cap.FOCUS}),
            )
            groups.append(_SG(
                group_id=f"test:{s.pid}",
                title_hint=None,
                adapter_id="test",
                views=(v,),
            ))
        self._render_session_groups(tuple(groups))

    def _render_session_groups(
        self,
        groups: tuple["SessionGroup", ...],
    ) -> None:
        """Render pre-grouped sessions from the adapter chain.

        Each ``SessionGroup`` becomes one card; each ``SessionView`` in
        the group becomes a row inside that card. Grouping decisions
        live entirely in the adapter — UI just presents them.

        Row widgets are cached by pid to preserve hover/pressed state
        across ticks. Cards are rebuilt every refresh.

        Fast path: when the group structure (group_ids in order, view
        pids in order) is identical to the previous render, skip the
        layout teardown + rebuild entirely — only update each row's
        content in place via ``_update_row``. Cached in
        ``_last_struct_sig``.

        v2: pending decisions used to be inlined as wrapper widgets
        below their matching group here; they now live exclusively
        in the top-of-panel ``StackedDecisionsPanel``, so the wrapper
        logic and ``pending_decisions`` parameter are gone."""
        total_views = sum(len(g.views) for g in groups)
        self._latest_sessions = list(range(total_views))  # sentinel

        new_struct_sig = tuple(
            (g.group_id, tuple(v.pid for v in g.views))
            for g in groups
        )
        prev_sig = getattr(self, "_last_struct_sig", None)
        if (
            new_struct_sig == prev_sig
            and groups
            and self._session_box.count() > 0
        ):
            for group in groups:
                for view in group.views:
                    btn = self._rows.get(view.pid)
                    if btn is not None:
                        self._update_row(btn, view)
            # Title text + count rebuild remains safe (a setText() that
            # ends up identical is a no-op for Qt).
            self._sessions_title.setText(self._sessions_title_text(total_views))
            return

        self._last_struct_sig = new_struct_sig
        self._clear_session_layout()
        self._sessions_title.setText(self._sessions_title_text(total_views))
        # History chip count is updated separately via update_recents_count;
        # render() in __main__'s subscription wires the count from
        # snap.dormant_sessions, not from session_groups.

        if not groups:
            self._show_placeholder()
            self._gc_rows(set())
            self.adjustSize()
            self._position()
            return

        self._hide_placeholder()

        needed_pids: set[int] = set()
        needed_group_ids: set[str] = set()
        multi_idx = 0
        for group in groups:
            palette_idx = multi_idx if len(group.views) > 1 else None
            group_widget = self._make_group_widget(
                group, palette_idx=palette_idx,
            )
            self._session_box.addWidget(group_widget)
            if palette_idx is not None:
                multi_idx += 1
            for v in group.views:
                needed_pids.add(v.pid)
            # Only multi-view groups create a cached card; single-view
            # groups return their row directly with no card wrapper.
            if len(group.views) > 1:
                needed_group_ids.add(group.group_id)

        self._gc_rows(needed_pids)
        self._gc_cards(needed_group_ids)

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

    def _render_cards(self) -> None:
        """Refresh summary + SPEND + QUOTA cards.

        Pulls token / quota data from the injected getters
        (registries / quota providers). Called by render(snap) on
        every snapshotter tick so cards stay in sync with the row
        list.

        Calls ``adjustSize`` after the refresh so the panel grows /
        shrinks to match the new SPEND content (e.g. switching from
        ``5H`` to ``Today`` adds rows for models the 5h window didn't
        spend on). Without this call the panel kept its previous size
        and Qt squeezed the sessions scroll area to make room — the
        sessions section would visibly shrink when the user did
        nothing but switch a SPEND tab."""
        self._refresh_summary_card()
        self._refresh_spend_card()
        self._refresh_quota_card()
        # v4c footer mirrors the same data the (hidden) quota card uses.
        # Kept defensive — _build_quota_footer ran with no errors but
        # individual snap fields might be missing on cold start.
        if hasattr(self, "_quota_footer"):
            try:
                self._refresh_quota_footer()
            except Exception as exc:
                import sys as _sys
                print(f"[claude-island] quota footer refresh failed: {exc}",
                      file=_sys.stderr)
        self.adjustSize()
        self._position()

    def _on_manual_refresh(self) -> None:
        """User clicked the ↻ button. Force a quota fetch (bypassing
        the provider's TTL) and immediately redraw the cards.

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
        self._render_cards()

    # ------------------------------------------------------------------
    # USAGE: SPEND card (period-selectable, cross-provider)
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # Focus summary (Tier 1 headline)
    # ------------------------------------------------------------------

    def _build_summary_card(self) -> QFrame:
        """Top focus summary — the panel's "what's the headline?" tier.

        Layout::

            ┌──────────────────────────────────────────┐
            │ TODAY                          $86.42    │  big $
            │ Anthropic · resets in 4h 47m             │  subtitle
            │ ▓▓▓▓▓▓▓░░░░░░░░░░  78% of 5h limit       │  bar
            └──────────────────────────────────────────┘

        Today's $ is the most-asked question; the 5h bar warns when
        the user is approaching the rate-limit cliff. Both numbers
        already exist in the SPEND/QUOTA cards below, but those cards
        are tier-3 detail; the summary lifts the headline up so the
        eye lands on it first.
        """
        card = QFrame()
        card.setObjectName("summary_card")
        card.setStyleSheet(_STYLE_USAGE_SESSION_CARD.replace(
            "usage_session_card", "summary_card"
        ))

        layout = QVBoxLayout(card)
        # Same padding as SPEND / QUOTA cards (12, 10) — the unified
        # card system means every card uses the same insets so adjacent
        # cards align edge-to-edge in the grid.
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(6)

        # Top row: "TODAY" caption (left) + big $ amount (right).
        top = QHBoxLayout()
        top.setSpacing(8)
        today_label = mk_label("TODAY", elide=False)
        today_label.setStyleSheet(_STYLE_TITLE)
        today_label.setAlignment(
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
        )
        top.addWidget(today_label)
        top.addStretch()

        # Headline number — bumped 4 px over the SPEND card's amount so
        # the visual hierarchy reads "summary > detail". _ElasticLabel
        # because at 24 px font a high-spend value (e.g. "$12,345.67")
        # can otherwise propagate width up to the panel and trip the
        # QWindowsWindow::setGeometry mintrack=480 warning.
        self._summary_amount = _ElasticLabel("—")
        self._summary_amount.setStyleSheet(
            "color: #f5f5f5; font-size: 24px; font-weight: 600;"
        )
        self._summary_amount.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )
        top.addWidget(self._summary_amount)
        layout.addLayout(top)

        # Subtitle — provider name + reset countdown. Hidden when no
        # provider is configured (e.g. empty providers.json). Uses
        # _ElasticRichLabel even though it's plain text: the natural
        # rendered width (~319 px for "Anthropic · resets in expired")
        # is a hair under _PANEL_W, but a longer countdown string can
        # tip it over and trigger the QWindowsWindow::setGeometry
        # warning. Eliding via the Elastic widget keeps the panel
        # honest at 320 px no matter what the subtitle says.
        self._summary_subtitle = _ElasticRichLabel("")
        self._summary_subtitle.setStyleSheet(_STYLE_USAGE_RESET)
        self._summary_subtitle.setSizePolicy(
            QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred,
        )
        layout.addWidget(self._summary_subtitle)

        # 5h quota progress bar. Reused QProgressBar (consistent with
        # the QUOTA card's bars) so styling stays in one place.
        self._summary_quota_bar = QProgressBar()
        self._summary_quota_bar.setRange(0, 100)
        self._summary_quota_bar.setTextVisible(False)
        self._summary_quota_bar.setFixedHeight(6)
        # Default chunk colour at construction = neutral grey; the real
        # threshold colour gets swapped in by _refresh_summary_card on
        # the first refresh tick once a quota snapshot lands.
        self._summary_quota_bar.setStyleSheet(
            _STYLE_SUMMARY_PROGRESS.replace("__CHUNK__", "#4a4a4a")
        )

        # v4c: bar + percent caption on the SAME row — "▓▓▓░░░  62% of 5h"
        # mirrors prototype-v4c-github.html's bar-row group, keeping the
        # signal compact (one visual line carrying fill + label).
        bar_row = QHBoxLayout()
        bar_row.setContentsMargins(0, 0, 0, 0)
        bar_row.setSpacing(10)
        bar_row.addWidget(self._summary_quota_bar, 1)
        self._summary_caption = mk_label("", elide=False)
        self._summary_caption.setStyleSheet(_STYLE_USAGE_PCT)
        self._summary_caption.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )
        bar_row.addWidget(self._summary_caption)
        layout.addLayout(bar_row)

        # ── v4c stats strip ─────────────────────────────────────────
        # Single inline row at the bottom of the TODAY card carrying
        # three quick KPIs: tokens / cache / hit-rate.  Mirrors the
        # prototype-v4c-github.html stats strip — keeps secondary
        # numbers visible without taking up a whole new card.
        #
        # Data source: usage_registry.get_totals("today") returns the
        # same UsageTotals object _refresh_summary_card already reads
        # for today's cost — no new fetch / no new fields.  Request
        # count isn't shown because it doesn't exist on UsageTotals
        # yet; adding it would be a new feature outside this UI slice.
        self._summary_stats = _ElasticRichLabel("")
        self._summary_stats.setStyleSheet(
            f"color: {_LabColor.paper_faint}; font-size: 11px; "
            f"font-family: {FontStack.mono_stack};"
        )
        self._summary_stats.setSizePolicy(
            QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred,
        )
        layout.addWidget(self._summary_stats)

        return card

    def _refresh_summary_card(self) -> None:
        """Repopulate the summary card from usage_registry + quota
        snapshot. Same defensive pattern as the SPEND/QUOTA refreshers
        — getter exceptions are logged and the card degrades to a "—"
        amount or hides the bar rather than crashing the panel."""
        # Today's spend + stats strip — both pull from the SAME
        # UsageTotals object, fetched once and stashed on the closure.
        # The strip is rendered HERE (before the quota-snapshot check
        # below) so stats stay visible even when quota fetch fails —
        # the two data sources are independent (usage_registry vs.
        # provider engine), so one failing shouldn't blank the other.
        today_totals = None
        if self._get_usage_totals is not None:
            try:
                today_totals = self._get_usage_totals("today")
                self._summary_amount.setText(_fmt_money(today_totals.cost_usd))
            except Exception as exc:
                import sys as _sys
                print(f"[claude-island] summary today fetch failed: {exc}",
                      file=_sys.stderr)
                self._summary_amount.setText("—")
                today_totals = None
        else:
            self._summary_amount.setText("—")

        # Stats strip (tokens / cache / hit) — derived from the same
        # totals.  Empty when totals fetch failed or wasn't wired.
        self._render_summary_stats(today_totals)

        # Quota snapshot for the 5h bar + reset countdown.
        snap = None
        if self._get_quota_snapshot is not None:
            try:
                snap = self._get_quota_snapshot()
            except Exception as exc:
                import sys as _sys
                print(f"[claude-island] summary quota fetch failed: {exc}",
                      file=_sys.stderr)

        provider_label = (self.selected_provider_name() or "anthropic").title()
        if snap is None:
            # v4c: quota fetch failed (most often: OAuth expired, no
            # network, rate-limit on the quota endpoint itself).  Keep
            # the bar + caption slots visible to preserve the layout —
            # a missing row makes the card look "broken".  Render an
            # empty bar in paper_faint + the same "quota unavailable"
            # subtitle.
            self._summary_subtitle.setText(f"{provider_label} · quota unavailable")
            self._summary_quota_bar.setValue(0)
            self._summary_quota_bar.setStyleSheet(
                _STYLE_SUMMARY_PROGRESS.replace("__CHUNK__", _LabColor.rule_bright)
            )
            self._summary_quota_bar.show()
            self._summary_caption.setStyleSheet(
                f"color: {_LabColor.paper_faint}; font-size: 11px;"
            )
            self._summary_caption.setText("quota unavailable · sign in or hit ↻")
            self._summary_caption.show()
            return

        reset = _fmt_reset(snap.five_hour_resets_at)
        self._summary_subtitle.setText(f"{provider_label} · resets in {reset}")
        # Bar value — clamp to 0..100 just in case the provider returns
        # something odd (we've seen 100.5 when the API rounds).
        pct = max(0.0, min(100.0, float(snap.five_hour_pct)))
        # Round (NOT truncate) before bucketing — the QUOTA card uses
        # int(round(...)) too, and a parallel int(...) here landed
        # 84.6 in summary's "84 → yellow" bucket while QUOTA put it
        # in "85 → red". The bucketing pct is also what feeds the
        # progress bar so both numbers stay in lock-step.
        bucket_pct = int(round(pct))
        self._summary_quota_bar.setValue(bucket_pct)
        # Threshold colour — delegate to the same _quota_color helper
        # the QUOTA card uses, so the same percentage NEVER renders in
        # one colour up here and a different colour down there.
        chunk_color = _quota_color(bucket_pct, stale=snap.is_stale)
        self._summary_quota_bar.setStyleSheet(
            _STYLE_SUMMARY_PROGRESS.replace("__CHUNK__", chunk_color)
        )
        self._summary_quota_bar.show()
        # Caption text mirrors the chunk colour so the eye gets the
        # same signal whether it lands on the bar fill or on the
        # number — was fixed grey before, which buried the warning
        # state at >60 % when the bar started flashing yellow.
        self._summary_caption.setStyleSheet(
            f"color: {chunk_color}; font-size: 11px;"
        )
        stale_marker = " ⚠" if snap.is_stale else ""
        # Show the same rounded bucket the colour was picked from so
        # the user never sees "84 % → red" or "85 % → yellow" — bar
        # value, caption, and chunk colour all read from bucket_pct.
        # v4c: caption sits on the SAME row as the bar, right-aligned.
        # Shorter copy ("62% of 5h" not "62% of 5h limit") to fit
        # without crowding the bar fill.
        self._summary_caption.setText(
            f"<b>{bucket_pct}%</b> "
            f"<span style='color: {_LabColor.paper_faint};'>of 5h</span>"
            f"{stale_marker}"
        )
        self._summary_caption.show()

    def _render_summary_stats(self, today: "UsageTotals | None") -> None:
        """Paint the v4c stats strip ("210K tokens · 128.6M cache ·
        99.5% hit") from a UsageTotals payload.  Empty string when
        the payload is None — both "totals fetch failed" and "no
        usage_registry wired" cases land here.

        Pulled out as a small helper so the stats label can be
        recomputed independently of the quota-snapshot fetch (those
        two data sources are independent: usage_registry feeds stats,
        provider engine feeds the bar)."""
        if today is None:
            self._summary_stats.setText("")
            return
        try:
            total_tok = today.input_tokens + today.output_tokens
            cache_tok = today.cache_creation_tokens + today.cache_read_tokens
            # Cache hit rate: reads ÷ (reads + creations).  100% when
            # there are no creations (everything was a hit); 0% when
            # nothing was cached.  denom == 0 (cold-start day with
            # zero cache traffic both directions) prints "—" rather
            # than dividing.
            denom = today.cache_read_tokens + today.cache_creation_tokens
            hit_str = f"{today.cache_read_tokens / denom * 100:.1f}%" if denom > 0 else "—"

            # HTML so a single QLabel.setText keeps stats coloured
            # without us recreating QLabel children every refresh.
            col_num   = _LabColor.paper
            col_label = _LabColor.paper_faint
            col_sep   = _LabColor.rule_bright
            # v4c: 4-stat strip — reqs first (number of API calls)
            # then tokens / cache / hit.  Mirrors prototype layout:
            # "257 reqs · 210K tokens · 128.9M cache · 99.5% hit"
            reqs = getattr(today, "request_count", 0)
            parts = [
                f"<span style='color:{col_num}'><b>{_fmt_tokens(reqs)}</b></span> "
                f"<span style='color:{col_label}'>reqs</span>",
                f"<span style='color:{col_num}'><b>{_fmt_tokens(total_tok)}</b></span> "
                f"<span style='color:{col_label}'>tokens</span>",
                f"<span style='color:{col_num}'><b>{_fmt_tokens(cache_tok)}</b></span> "
                f"<span style='color:{col_label}'>cache</span>",
                f"<span style='color:{col_num}'><b>{hit_str}</b></span> "
                f"<span style='color:{col_label}'>hit</span>",
            ]
            sep = f" <span style='color:{col_sep}'>·</span> "
            self._summary_stats.setText(sep.join(parts))
        except Exception as exc:
            import sys as _sys
            print(f"[claude-island] summary stats render failed: {exc}",
                  file=_sys.stderr)
            self._summary_stats.setText("")

    def _build_spend_card(self) -> QFrame:
        """Single SPEND card driven by a window dropdown (5h / Today /
        Last 7 days / Last 30 days). Each row shows one model's bar
        plus its aggregate token count; the per-model in/out/cache
        breakdown is folded behind a "details" disclosure to keep the
        default density readable.

           ┌──────────────────────────────────────────┐
           │ SPEND                       ⌄ 5h         │
           │ $86.42 · 1.2M tokens                     │
           │ Opus  ████████  $85    1.2M tokens       │
           │ Haiku ▓░░░░░░░  $0.77   19K tokens       │
           │ ▸ details                                │
           └──────────────────────────────────────────┘

        Period dropdown (instead of a tab strip) keeps the row above
        the amount narrow and predictable — the previous 5-tab strip
        was clipping the trailing "Monthly" tab on default panel
        widths. "Daily" was dropped because it overlaps semantically
        with "Today"; the remaining four windows cover all common
        questions ("right now / today / week / month").
        """
        card = QFrame()
        card.setObjectName("usage_spend_card")
        card.setStyleSheet(_STYLE_USAGE_PERIOD_CARD)

        layout = QVBoxLayout(card)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(6)

        # Header row: SPEND label (left) + period dropdown (right)
        hdr = QHBoxLayout()
        hdr.setSpacing(8)
        spend_label = mk_label("SPEND", elide=False)
        spend_label.setStyleSheet(_STYLE_TITLE)
        hdr.addWidget(spend_label)
        hdr.addStretch()

        self._period_combo = QComboBox()
        self._period_combo.setStyleSheet(_STYLE_PERIOD_COMBO)
        self._period_combo.setCursor(Qt.CursorShape.PointingHandCursor)
        # (label, key) pairs. Order matters for the dropdown — most
        # actionable window first ("right now") then expanding outward.
        self._period_combo_items: list[tuple[str, str]] = [
            ("5h window",   "5h"),
            ("Today",       "today"),
            ("Last 7 days", "weekly"),
            ("Last 30 days", "monthly"),
        ]
        for label, key in self._period_combo_items:
            self._period_combo.addItem(label, key)
        # Pre-select the saved period
        for idx, (_label, key) in enumerate(self._period_combo_items):
            if key == self._period:
                self._period_combo.setCurrentIndex(idx)
                break
        self._period_combo.currentIndexChanged.connect(self._on_period_combo_changed)
        hdr.addWidget(self._period_combo)
        layout.addLayout(hdr)

        # Headline $ + total token count, on one row.
        # ``$86.42 · 1.2M tokens`` — the dot separator visually couples
        # them as "two facets of the same window" (cost vs scale).
        # _ElasticLabel: at 20 px font the combined money+tokens string
        # can run past _PANEL_W and otherwise propagates the panel's
        # effective minimum width past 320 (mintrack=480 warning).
        self._spend_amount = _ElasticLabel("—")
        self._spend_amount.setStyleSheet(_STYLE_USAGE_AMOUNT)
        layout.addWidget(self._spend_amount)

        # Per-model proportional bar container. Each row:
        #   [short name] [======bar======] [$XX.XX] [tokens]
        # Token detail sub-line is hidden by default; the disclosure
        # below toggles all sub-lines together.
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

        # No ▸ details disclosure here on purpose — the previous
        # toggle made the panel grow taller (and on some systems the
        # token sub-line briefly forced a width recalc), and the user
        # explicitly asked for the SPEND card to keep a stable size
        # across all interactions. Per-row breakdowns now live as a
        # tooltip on the bar row — hover to see "input X · output Y
        # · cache w A · cache r B" without a single pixel of layout
        # movement. The same data is also available on right-click of
        # the corresponding session row in the SessionDetailPopup.
        return card

    def _on_period_combo_changed(self, index: int) -> None:
        """QComboBox handler — extract the period key stored on the
        item and forward to ``_on_period`` (the real period-change
        path that tests and the refresh logic both call directly)."""
        if 0 <= index < len(self._period_combo_items):
            _label, key = self._period_combo_items[index]
            self._on_period(key)

    def _build_spend_model_row(self) -> QWidget:
        """One model row: name · bar · cost · aggregate-tokens.

        The detailed in/out/cw/cr breakdown is exposed as a tooltip on
        the row (hover to read) rather than an inline expandable
        sub-line — the disclosure pattern was making the panel grow /
        shrink on every click, which the user explicitly rejected.
        Tooltip keeps the data accessible without any layout impact.

        Proportional bar is a QFrame with a fixed-height colored child
        whose width is set as a percentage of the container. Width is
        updated via `_refresh_spend_card` whenever totals change.
        """
        row = QWidget()
        row.setStyleSheet("background: transparent;")

        # Two-line vertical: top = name+bar+cost+tokens, bottom = detail line
        v = QVBoxLayout(row)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(1)

        # Top line: [name] [bar-area] [cost] [aggregate-tokens]
        top = QHBoxLayout()
        top.setSpacing(6)

        # setFixedWidth(76) below caps width, elide=False is fine.
        name_lbl = mk_label("", elide=False)
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

        cost_lbl = mk_label("", elide=False)
        cost_lbl.setStyleSheet("color: #e8e8e8; font-size: 12px;")
        cost_lbl.setFixedWidth(62)
        cost_lbl.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        top.addWidget(cost_lbl)

        # Aggregate token count to the right of cost. Dimmer than cost
        # so the dollar number remains the dominant value on the row;
        # tokens are the "scale context" answer ("how big was the
        # workload that produced this $?"). Drop the literal word
        # "tokens" — at 320 px panel width the suffix pushed values
        # like "100.7M tokens" past the column boundary and clipped
        # the trailing "ns". The unit column is unambiguous at the
        # row level (it's always tokens), and the headline above
        # ("$86 · 1.2M tokens") names the unit once for the whole card.
        agg_lbl = mk_label("", elide=False)
        agg_lbl.setStyleSheet("color: #9ca3af; font-size: 11px;")
        agg_lbl.setFixedWidth(56)
        agg_lbl.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        top.addWidget(agg_lbl)

        v.addLayout(top)

        # Store on the row widget for later access by _show_spend_row.
        # Per-model breakdown (in/out/cw/cr) is exposed via row.setToolTip
        # in _show_spend_row — the disclosure pattern moved off the
        # layout (every click resized the panel) onto hover.
        row._spend_name = name_lbl
        row._spend_bar_fill = bar_fill
        row._spend_cost = cost_lbl
        row._spend_aggregate = agg_lbl
        row._spend_bar_track = bar_track

        return row

    def _refresh_spend_card(self) -> None:
        t = self._get_usage_totals(self._period)
        # Headline now combines $ and total tokens — the "$86 · 1.2M
        # tokens" pair lets the user feel the cost-per-token signal
        # (Opus-heavy days vs Haiku-heavy days look obviously different).
        # Tokens are suppressed when the period has no spend at all.
        total_tokens = (
            t.input_tokens + t.output_tokens
            + t.cache_creation_tokens + t.cache_read_tokens
        )
        if t.cost_usd > 0 and total_tokens > 0:
            self._spend_amount.setText(
                f"{_fmt_money(t.cost_usd)} · {_fmt_tokens(total_tokens)} tokens"
            )
        else:
            self._spend_amount.setText(_fmt_money(t.cost_usd))

        # Per-model proportional bars. Only rendered when the wiring
        # layer provides get_totals_by_model — tests that don't pass it
        # see the SPEND card without per-model rows, no warnings raised.
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

        name_lbl.setText(label)
        # Tooltip on the name carries the un-truncated model id so users
        # can hover to see the full name when _fmt_model_label collapsed
        # it (e.g. "deepseek-v4-pro" → "deepseek-v4-…").
        name_lbl.setToolTip(full_name if full_name else label)
        cost_lbl.setText(_fmt_money(cost))

        # Aggregate token count on the top line. No "tokens" suffix
        # (see _build_spend_model_row): the column is always tokens
        # and the headline above the rows already names the unit.
        agg_lbl: QLabel | None = getattr(row, "_spend_aggregate", None)
        total_tokens = tokens_in + tokens_out + cache_w + cache_r
        if agg_lbl is not None:
            agg_lbl.setText(_fmt_tokens(total_tokens))

        # Detail breakdown is exposed as a row-level tooltip — hover
        # to see "input X · output Y · cache w A · cache r B" without
        # any layout impact. Replaces the previous inline disclosure
        # which forced the panel to grow / shrink on every click.
        # Cache portion is omitted when both write and read are 0 so
        # the tooltip stays scannable for non-cache models.
        parts = [
            f"input {_fmt_tokens(tokens_in)}",
            f"output {_fmt_tokens(tokens_out)}",
        ]
        if cache_w or cache_r:
            parts.append(f"cache w {_fmt_tokens(cache_w)}")
            parts.append(f"cache r {_fmt_tokens(cache_r)}")
        row.setToolTip(" · ".join(parts))

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
    # v4c QUOTA footer — horizontal inline row replacing the big
    # quota card at the bottom of the panel.  Mirrors prototype-v4c-
    # github.html's footer band:
    #
    #   QUOTA  [Anthropic] [+]   5h 62% · 2h 14m  │  Weekly 41% · 4d 22h   ↻
    #
    # ``_refresh_quota_footer`` is called from the same refresh tick
    # the big card uses, so both surfaces stay in lock-step.
    # ------------------------------------------------------------------

    def _build_quota_footer(self) -> QWidget:
        """Build the inline quota footer.  Reads provider name + 5h /
        weekly figures from snap.quota at refresh time; refresh button
        forwards to the same on_refresh_clicked callback the v3 card
        already uses."""
        bar = QFrame()
        bar.setObjectName("quota_footer")
        bar.setStyleSheet(
            f"QFrame#quota_footer {{"
            f"  background: {_LabColor.surface};"
            f"  border-top: 1px solid {_LabColor.rule};"
            f"  border-radius: 0 0 6px 6px;"
            f"}}"
        )
        lay = QHBoxLayout(bar)
        lay.setContentsMargins(14, 8, 10, 8)
        lay.setSpacing(10)

        # "QUOTA" caption (paper_dim uppercase).
        cap = QLabel("QUOTA")
        cap.setStyleSheet(
            f"color: {_LabColor.paper_dim}; "
            f"font-size: 10px; font-weight: 600; letter-spacing: 0.08em;"
        )
        lay.addWidget(cap)

        # Provider chip — current selected provider.  Click TBD (v4c
        # spec is "chip + [+] for switching", but provider switching
        # already lives in the hidden quota_card's tab strip; the
        # footer chip is read-only for now).  Updated by
        # _refresh_quota_footer to reflect the active provider name.
        self._quota_footer_chip = QPushButton("Anthropic")
        self._quota_footer_chip.setCursor(Qt.CursorShape.PointingHandCursor)
        self._quota_footer_chip.setStyleSheet(
            f"QPushButton {{"
            f"  background: {_LabColor.ink};"
            f"  color: {_LabColor.accent};"
            f"  border: 1px solid {_LabColor.accent};"
            f"  border-radius: 100px;"
            f"  padding: 2px 9px;"
            f"  font-size: 11px;"
            f"  font-weight: 500;"
            f"}}"
            f"QPushButton:hover {{"
            f"  background: {_LabColor.surface_hi};"
            f"}}"
        )
        lay.addWidget(self._quota_footer_chip)

        # "+" placeholder — future "add provider" affordance.  Sized to
        # match chip height (dashed border = "potential / inactive").
        self._quota_footer_add = QPushButton("+")
        self._quota_footer_add.setCursor(Qt.CursorShape.PointingHandCursor)
        self._quota_footer_add.setFixedSize(20, 20)
        self._quota_footer_add.setStyleSheet(
            f"QPushButton {{"
            f"  background: transparent;"
            f"  color: {_LabColor.paper_faint};"
            f"  border: 1px dashed {_LabColor.rule_bright};"
            f"  border-radius: 100px;"
            f"  font-size: 12px;"
            f"  padding: 0;"
            f"}}"
            f"QPushButton:hover {{"
            f"  color: {_LabColor.paper};"
            f"  border-color: {_LabColor.rule_active};"
            f"}}"
        )
        self._quota_footer_add.setToolTip("Add another provider (coming soon)")
        lay.addWidget(self._quota_footer_add)

        # 5h figure ("5h 62% · 2h 14m") — mono, dim label + bright bold
        # number, separator dot.  Single label keeps refresh cheap.
        self._quota_footer_5h = QLabel("—")
        self._quota_footer_5h.setStyleSheet(
            f"color: {_LabColor.paper_dim}; font-size: 11px; "
            f"font-family: {FontStack.mono_stack};"
        )
        self._quota_footer_5h.setTextFormat(Qt.TextFormat.RichText)
        lay.addWidget(self._quota_footer_5h)

        # vertical separator before Weekly
        sep = QLabel("│")
        sep.setStyleSheet(
            f"color: {_LabColor.rule_bright}; font-size: 11px;"
        )
        lay.addWidget(sep)

        # Weekly figure — same shape as 5h.  Snap doesn't carry a
        # weekly progress today; we surface "Weekly —" until provider
        # backends learn weekly reporting.
        self._quota_footer_weekly = QLabel(
            f"<span style='color: {_LabColor.paper_dim}'>Weekly</span> "
            f"<span style='color: {_LabColor.paper_faint}'>—</span>"
        )
        self._quota_footer_weekly.setStyleSheet(
            f"font-size: 11px; font-family: {FontStack.mono_stack};"
        )
        self._quota_footer_weekly.setTextFormat(Qt.TextFormat.RichText)
        lay.addWidget(self._quota_footer_weekly)

        lay.addStretch(1)

        # Refresh button — forwards to the existing
        # ``_on_refresh_clicked`` callback so both quota surfaces
        # (hidden card + visible footer) share one network handler.
        self._quota_footer_refresh = QPushButton("↻")
        self._quota_footer_refresh.setCursor(Qt.CursorShape.PointingHandCursor)
        self._quota_footer_refresh.setFixedSize(24, 24)
        self._quota_footer_refresh.setStyleSheet(
            f"QPushButton {{"
            f"  background: transparent;"
            f"  color: {_LabColor.paper_dim};"
            f"  border: 1px solid {_LabColor.rule};"
            f"  border-radius: 100px;"
            f"  font-size: 13px;"
            f"  padding: 0;"
            f"}}"
            f"QPushButton:hover {{"
            f"  color: {_LabColor.paper};"
            f"  background: {_LabColor.surface_hi};"
            f"}}"
        )
        self._quota_footer_refresh.setToolTip("Refresh quota now")
        self._quota_footer_refresh.clicked.connect(self._on_quota_footer_refresh)
        lay.addWidget(self._quota_footer_refresh)

        return bar

    def _on_quota_footer_refresh(self) -> None:
        """Forward footer refresh clicks to the existing handler."""
        if self._on_refresh_clicked is not None:
            try:
                self._on_refresh_clicked()
            except Exception as exc:
                import sys as _sys
                print(f"[claude-island] quota footer refresh failed: {exc}",
                      file=_sys.stderr)

    def _refresh_quota_footer(self) -> None:
        """Repopulate the footer from the current quota snapshot.
        Defensive — fall back to "—" labels when snapshot or
        per-class fields are missing rather than crashing."""
        # Provider chip — name comes from selected_provider_name (same
        # source the v3 card uses).
        provider = (self.selected_provider_name() or "anthropic").title()
        self._quota_footer_chip.setText(provider)

        snap = None
        if self._get_quota_snapshot is not None:
            try:
                snap = self._get_quota_snapshot()
            except Exception as exc:
                import sys as _sys
                print(f"[claude-island] quota footer fetch failed: {exc}",
                      file=_sys.stderr)

        if snap is None:
            self._quota_footer_5h.setText(
                f"<span style='color: {_LabColor.paper_dim}'>5h</span> "
                f"<span style='color: {_LabColor.paper_faint}'>—</span>"
            )
            return

        pct_5h = max(0.0, min(100.0, float(snap.five_hour_pct)))
        bucket = int(round(pct_5h))
        col = _quota_color(bucket, stale=snap.is_stale)
        reset = _fmt_reset(snap.five_hour_resets_at)
        self._quota_footer_5h.setText(
            f"<span style='color: {_LabColor.paper_dim}'>5h</span> "
            f"<span style='color: {col}; font-weight: 600'>{bucket}%</span>"
            f"<span style='color: {_LabColor.rule_bright}'> · </span>"
            f"<span style='color: {_LabColor.paper_dim}'>{reset}</span>"
        )

        # Weekly — UsageSnapshot may carry a seven_day_pct field; fall
        # back to em-dash when absent.
        wk_pct = getattr(snap, "seven_day_pct", None)
        wk_reset = getattr(snap, "seven_day_resets_at", None)
        if wk_pct is not None and wk_reset is not None:
            wk_bucket = int(round(max(0.0, min(100.0, float(wk_pct)))))
            wk_col = _quota_color(wk_bucket, stale=snap.is_stale)
            self._quota_footer_weekly.setText(
                f"<span style='color: {_LabColor.paper_dim}'>Weekly</span> "
                f"<span style='color: {wk_col}; font-weight: 600'>{wk_bucket}%</span>"
                f"<span style='color: {_LabColor.rule_bright}'> · </span>"
                f"<span style='color: {_LabColor.paper_dim}'>{_fmt_reset(wk_reset)}</span>"
            )
        else:
            self._quota_footer_weekly.setText(
                f"<span style='color: {_LabColor.paper_dim}'>Weekly</span> "
                f"<span style='color: {_LabColor.paper_faint}'>—</span>"
            )

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

        # QUOTA section title — restored for symmetry with the SPEND
        # card's "SPEND" title. The unified card system carries one
        # consistent uppercase label per card so the grid reads as a
        # set of equal-weight panes, not "some have titles, some
        # don't". The status dot is still hidden (color of the inline
        # 5h text already conveys live/closed window state).
        section_title = mk_label("QUOTA", elide=False)
        section_title.setStyleSheet(_STYLE_TITLE)
        layout.addWidget(section_title)

        # Header: tab strip + refresh button (no leading status dot).
        # The dot was carrying "is the 5h window still open?" — same
        # info now lives in the colour of the inline 5h line below.
        quota_hdr = QHBoxLayout()
        quota_hdr.setSpacing(8)
        self._quota_dot = mk_label("●", elide=False)
        self._quota_dot.setStyleSheet(_STYLE_DOT.format(color=_DOT_GRAY))
        self._quota_dot.hide()

        # Tab strip lives in its own sub-layout so set_available_providers()
        # can wipe + rebuild it without disturbing the dot or the refresh
        # button on either side. Earlier the rebuild walked quota_hdr
        # directly and ate the refresh button as collateral damage.
        self._tab_strip_layout = QHBoxLayout()
        self._tab_strip_layout.setSpacing(8)
        self._tab_strip_layout.setContentsMargins(0, 0, 0, 0)
        # Reserved header label for the no-providers fallback branch
        # (see _refresh_quota_card → no providers configured): shown
        # only when the tab strip can't be populated, otherwise hidden.
        self._quota_hdr = mk_label("QUOTA", elide=False)
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

        # 5h + Weekly compressed onto a single rich-text line —
        # ``5h 53% · 3h 5m  │  Weekly 57% · 55m``. Each half can take
        # its own threshold colour because the label uses HTML in
        # setText. Falls back to a single line of ~ 30 chars at 11 px
        # font, which fits within the 320 px panel width with margin
        # to spare. Hidden when no quota snapshot.
        #
        # Critical: ``setSizePolicy(Ignored, Preferred)`` is required.
        # RichText QLabels report ``minimumSizeHint().width()`` as the
        # rendered HTML width — observed at 429 px for the typical
        # warning-state string. Default Preferred policy would let
        # that propagate up to the parent card, then to the panel,
        # which has ``setFixedWidth(_PANEL_W=400)`` set. Qt resolves
        # the conflict by overriding the fixed-width to fit the
        # child, producing the QWindowsWindow::setGeometry mintrack
        # warning every layout pass. Ignored h-policy tells Qt to use
        # whatever width the parent allocates, which is what we want
        # for an inline status line.
        self._quota_inline = _ElasticRichLabel("")
        self._quota_inline.setStyleSheet("font-size: 11px;")
        self._quota_inline.setTextFormat(Qt.TextFormat.RichText)
        self._quota_inline.setSizePolicy(
            QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred,
        )
        self._quota_inline.hide()
        layout.addWidget(self._quota_inline)

        # "Unavailable" hint, hidden at rest. Shown by _refresh_quota_card
        # when the provider's fetch returns None — gives the user a
        # diagnostic instead of an empty card. Wraps because the tip
        # references an environment variable / file path that can be
        # longer than the card width.
        # elide=False: setWordWrap(True) below is the strategy.
        self._quota_unavailable = mk_label("", elide=False)
        self._quota_unavailable.setStyleSheet(
            "color: #9ca3af; font-size: 11px; padding: 4px 0 0 0;"
        )
        self._quota_unavailable.setWordWrap(True)
        self._quota_unavailable.hide()
        layout.addWidget(self._quota_unavailable)

        # "Auto-refresh paused" hint, hidden at rest. Shown when the
        # provider's circuit-breaker has tripped (auto fetch stopped
        # issuing HTTP after enough consecutive failures). Tells the
        # user the bars they see are frozen and that ↻ is the recovery
        # path. Distinct from ``_quota_unavailable`` — that one fires
        # when we have NO data at all; this one fires when data exists
        # but auto-refresh is on strike.
        self._quota_paused_hint = mk_label("", elide=False)
        self._quota_paused_hint.setStyleSheet(
            "color: #f59e0b; font-size: 11px; padding: 4px 0 0 0;"
        )
        self._quota_paused_hint.setWordWrap(True)
        self._quota_paused_hint.hide()
        layout.addWidget(self._quota_paused_hint)

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
            self._quota_inline.hide()
            self._quota_paused_hint.hide()
            self._show_quota_unavailable_hint()
            return

        # Snapshot is available — make sure the unavailable hint is
        # gone (would otherwise persist across a recover-from-failure
        # transition and visually compete with the now-real bars).
        self._quota_unavailable.hide()
        self._refresh_quota_paused_hint(snap)

        # Live dot: green when 5h window is still open
        active = snap.five_hour_resets_at > datetime.now(timezone.utc)
        self._quota_dot.setStyleSheet(
            _STYLE_DOT.format(color=_DOT_GREEN if active else _DOT_GRAY)
        )

        # 5h + Weekly compressed onto one rich-text line. Each half
        # carries its own threshold colour (5h often warns first since
        # that's the tighter window). Format: "5h 53% · 3h 5m │
        # Weekly 57% · 55m" — abbreviations chosen to fit the 320 px
        # panel: drop "used", drop "resets" (the time alone reads as
        # "until reset" once the user knows the convention).
        five_pct = max(0, min(100, int(round(snap.five_hour_pct))))
        weekly_pct = max(0, min(100, int(round(snap.seven_day_pct))))
        stale_marker = " ⚠" if snap.is_stale else ""
        five_color = _quota_color(five_pct, stale=snap.is_stale)
        weekly_color = _quota_color(weekly_pct, stale=snap.is_stale)
        five_reset = _fmt_reset(snap.five_hour_resets_at)
        weekly_reset = _fmt_reset(snap.seven_day_resets_at)
        # Strip leading "in " from the reset countdowns (the format
        # helper sometimes emits "in 3h 5m" / "3h 5m" depending on
        # window state) so the compact line stays uniform.
        five_reset_short = five_reset.removeprefix("in ")
        weekly_reset_short = weekly_reset.removeprefix("in ")
        self._quota_inline.setText(
            f"<span style='color:{five_color}'>5h {five_pct}%{stale_marker} · {five_reset_short}</span>"
            f"<span style='color:#4b5563'>  │  </span>"
            f"<span style='color:{weekly_color}'>Weekly {weekly_pct}%{stale_marker} · {weekly_reset_short}</span>"
        )
        self._quota_inline.show()

    def _refresh_quota_paused_hint(self, snap) -> None:
        """Show / hide the "auto-refresh paused" hint based on the
        snapshot's circuit-breaker state. Called from
        ``_refresh_quota_card`` when ``snap`` is non-None — the no-data
        branch hides the hint outright since the unavailable copy
        already explains what to do.

        Copy: surfaces the consecutive-failure count so the user knows
        WHY auto stopped, and mentions the ↻ as the recovery path.
        Distinct yellow tone (#f59e0b) so it reads as a warning rather
        than the dimmer-grey "unavailable" message — bars below are
        still real data, just frozen."""
        if not snap.is_auto_refresh_paused:
            self._quota_paused_hint.hide()
            return
        n = snap.consecutive_failures
        self._quota_paused_hint.setText(
            f"Auto-refresh paused after {n} consecutive failures. "
            f"Click ↻ to retry."
        )
        self._quota_paused_hint.show()

    def _show_quota_unavailable_hint(self) -> None:
        """Surface a provider-specific tip explaining why the bars are
        empty. Each tip points at the most likely fix path so the user
        doesn't have to grep stderr for ``[claude-island] quota fetch
        failed`` to figure out what went wrong.

        Picks the tip text from the currently-selected provider name —
        matches what the tab strip shows so the message reads as "this
        provider isn't returning data" rather than a generic alert.
        """
        provider = self.selected_provider_name() or "anthropic"
        tips = {
            "anthropic": (
                "Quota unavailable. Sign in to Claude Code "
                "(re-creates ~/.claude/.credentials.json), or hit ↻ "
                "after a network blip. Errors print to the terminal "
                "the app was launched from."
            ),
            "minimax": (
                "Quota unavailable. Paste a MiniMax Coding-Plan key "
                "into the Minimax tab via +, or check that "
                "ANTHROPIC_AUTH_TOKEN is set in the launching shell."
            ),
            "zhipu": (
                "Quota unavailable. Paste a Z.AI key into the Zhipu "
                "tab via +, or set ZHIPU_API_KEY in the launching "
                "shell. Errors print to the terminal."
            ),
        }
        self._quota_unavailable.setText(tips.get(
            provider,
            f"Quota unavailable for {provider}. "
            "Check the provider config and hit ↻ to retry.",
        ))
        self._quota_unavailable.show()

    def _on_state_changed(self, state: str) -> None:
        if state == "expanded":
            if self._latest_snap is not None:
                self.render(self._latest_snap)
            else:
                self._render_cards()
            self._position()
            self.show()
            self.raise_()
            # Explicitly take foreground so subsequent SetForegroundWindow
            # calls from row clicks are allowed by Win32.
            self.activateWindow()
        else:
            self.hide()

    def _on_period(self, period: str) -> None:
        """Switch the SPEND time window. Single entry point used by
        both the dropdown handler (``_on_period_combo_changed``) and
        any direct programmatic caller (tests, future hotkey bindings).

        Syncs the dropdown's selection to ``period`` so the visible
        state stays consistent regardless of who triggered the change.
        """
        self._period = period
        combo = getattr(self, "_period_combo", None)
        if combo is not None:
            for idx, (_label, key) in enumerate(self._period_combo_items):
                if key == period:
                    if combo.currentIndex() != idx:
                        # blockSignals so syncing the index doesn't
                        # re-trigger _on_period_combo_changed → infinite
                        # recursion via _on_period.
                        combo.blockSignals(True)
                        combo.setCurrentIndex(idx)
                        combo.blockSignals(False)
                    break
        self._render_cards()

    # ------------------------------------------------------------------
    # Multi-provider tab handlers + getter
    # ------------------------------------------------------------------

    def selected_provider_name(self) -> str | None:
        """Read the currently-active provider tab.

        The wiring layer reads this from inside its quota-snapshot
        closure to decide which provider to fetch from."""
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
        self._render_cards()

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
        and the dialog's choice list.

        Source of truth lives in platform_/providers; the wiring layer
        passes us a callable so this widget never imports platform code
        (import-linter contract). When the callback is None (tests), the
        list is empty and the + button stays hidden so single-provider
        users don't see an add UI they can't act on.
        """
        if self._list_configurable_providers is None:
            return []
        try:
            full = self._list_configurable_providers()
        except Exception:
            return []
        return [
            (name, cfg) for (name, cfg) in full
            if name not in self._available_providers
        ]

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
        if self._save_provider_settings is not None:
            self._save_provider_settings(name, fields)
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
        if self._delete_provider_settings is not None:
            self._delete_provider_settings(name)
        if self._on_provider_config_changed is not None:
            try:
                self._on_provider_config_changed()
            except Exception as exc:
                import sys as _sys
                print(f"[claude-island] provider-config-changed callback failed: {exc}",
                      file=_sys.stderr)

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
        self._render_cards()

    # ------------------------------------------------------------------
    # Session row factory
    # ------------------------------------------------------------------

    def _make_row(self, view: SessionView, parent_card: QFrame | None = None) -> HoverRow:
        """Build a click-target row with a v4c three-line layout:

        ::

            ● name                                $cost
              ~/path/to/cwd
              [Model] · running · 5m ago

        v4c GitHub-list density: cwd path on its own row (mono) so the
        user can disambiguate two same-named sessions in different repos
        without hovering for a tooltip.  Status text on the top line so
        the phase reads next to the name (the way the prototype rendered
        it).  Model chip + relative-time on the bottom row, same as
        before — kept on its own line so chip + status stay one block.

        Hover feedback is supplied by HoverRow's left-edge accent bar.
        Right-click still routes to SessionDetailPopup.
        """
        btn = HoverRow(base_bg=_BG_SINGLE, parent_card=parent_card)
        btn.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        btn.setFixedHeight(_ROW_HEIGHT)
        btn.setProperty("_session", view)
        btn.setProperty("_siblings", [])

        outer = QVBoxLayout(btn)
        outer.setContentsMargins(_ROW_PAD_H, 6, _ROW_PAD_H, 6)
        outer.setSpacing(2)

        # ---- top row: dot + name + cost ---------------------------------
        top = QHBoxLayout()
        top.setContentsMargins(0, 0, 0, 0)
        top.setSpacing(8)

        # v4c: phase icon — 16 px QLabel that paints a single character
        # glyph (◐ ▶ ! ⇕ ○ ✓) coloured by SessionPhase.  Sits in the
        # row's leftmost slot.  ``_update_row`` rebinds the text +
        # colour every refresh from view.phase.
        phase_icon = QLabel("")
        phase_icon.setObjectName("phase_icon")
        phase_icon.setFixedWidth(16)
        phase_icon.setAlignment(Qt.AlignmentFlag.AlignCenter | Qt.AlignmentFlag.AlignVCenter)
        phase_icon.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        btn._phase_icon = phase_icon
        top.addWidget(phase_icon)

        # Status glyph — the 5-bar wave that signals "this session is
        # actively producing tokens".  v4c moves it from the leftmost
        # slot (where v3 put it) to a position adjacent to the cost
        # number on the top row — same right-side spot where the
        # prototype paints it next to "1.4k tk/min".  Stays mounted on
        # the row object as ``btn._status_glyph`` so existing
        # set_phase / capabilities dispatch hooks keep working.
        status_glyph = _RowStatusGlyph(btn)
        status_glyph.setFixedHeight(_ROW_HEIGHT - 12)
        status_glyph.set_idle_visible(False)
        btn._status_glyph = status_glyph

        # _ElidingLabel: project names / AI titles are user-supplied
        # text. Two reasons we need the eliding variant here:
        #   1. minimumSizeHint=0 stops a long name from propagating
        #      its full width up the layout chain to ExpandedWindow,
        #      which would override setFixedWidth(_PANEL_W=400) and
        #      produce the QWindowsWindow::setGeometry mintrack=480
        #      warning. setSizePolicy(Expanding, …) alone won't do
        #      it — see _ElasticRichLabel docstring.
        #   2. Visual elision (`…`) at paint time so an overflowing
        #      name reads cleanly instead of getting hard-clipped
        #      mid-character at the row boundary.
        name_label = _ElidingLabel()
        name_label.setObjectName("name_label")
        name_label.setStyleSheet(_STYLE_NAME)
        name_label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        name_label.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        top.addWidget(name_label, 1)

        # v4c: status text inline next to the name on the top row
        # ("thinking · turn 3", "tool_use · Bash · 1.2s", etc.).
        # Phase-tinted; populated by _update_row from view.phase.
        # The bottom row's status_label is kept for backwards compat
        # (older meta strings like "active 3m ago") — they live
        # alongside each other as two complementary signals.
        status_inline = QLabel("")
        status_inline.setObjectName("status_inline")
        status_inline.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        status_inline.setStyleSheet(
            f"color: {_LabColor.paper_dim}; font-size: 11px;"
        )
        top.addWidget(status_inline)

        # v4c: wave glyph immediately before the cost number — that's
        # where prototype-v4c-github.html paints "live signal next to
        # live number".  Glyph is the same instance bound above as
        # btn._status_glyph (so set_phase / set_running still flip it);
        # we just attach it to a different layout slot here than v3 did.
        top.addWidget(status_glyph)

        # Right-side meta slot. Shows cumulative session cost ("$XX.XX").
        # elide=False: cost strings are bounded short, eliding right-
        # aligned money would be visually broken ("$1,2…").
        meta_label = mk_label("", elide=False)
        meta_label.setObjectName("meta_label")
        meta_label.setStyleSheet(_STYLE_COST_DEFAULT)
        meta_label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        meta_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        top.addWidget(meta_label)

        outer.addLayout(top)

        # ---- middle row: cwd path (mono) --------------------------------
        # Indent matches the bottom row so cwd sits flush under the name,
        # not under the status glyph. Eliding left so the *tail* of the
        # path stays readable (basename matters more than ancestors).
        cwd_label = _ElidingLabel()
        cwd_label.setObjectName("cwd_label")
        cwd_label.setStyleSheet(_STYLE_CWD)
        cwd_label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        cwd_label.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        cwd_label.setContentsMargins(20, 0, 0, 0)
        outer.addWidget(cwd_label)

        # ---- bottom row: indent + model chip + status -------------------
        # Indent the bottom row so the model chip sits under the name,
        # not under the dot — visually couples the two lines as one block.
        bottom = QHBoxLayout()
        bottom.setContentsMargins(20, 0, 0, 0)  # 12 (dot width) + 8 (spacing)
        bottom.setSpacing(6)

        # Model chip text is short ("Sonnet" / "Opus" / "Haiku" / etc.);
        # elide=False is safe and avoids spurious eliding overhead.
        model_chip = mk_label("", elide=False)
        model_chip.setObjectName("model_chip")
        model_chip.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        model_chip.setStyleSheet(_STYLE_MODEL_CHIP.format(color=_MODEL_COLOR_FALLBACK))
        bottom.addWidget(model_chip)

        # _ElidingLabel: status text composes "active <relative> ·
        # v<version>" — usually short, but long version strings or a
        # localised relative-time phrase can exceed the row width on
        # narrow allocations. Same dual rationale as name_label:
        # neuter width-propagation + elide on overflow rather than
        # hard-clip.
        #
        # stretch=1 is load-bearing here. Without it, the bottom-row
        # layout is `[chip + status + addStretch()]` with all three
        # at default sizePolicy. When available width drops below
        # `chip.sizeHint + status.sizeHint`, Qt picks the most
        # compressible widget — status (because _ElidingLabel reports
        # minimumSize=0) — and squeezes it preferentially, eliding
        # text well before chip ever yields a pixel. Result: rows
        # whose status would comfortably fit displayed as "active
        # 8d a…" while chip stayed at its full sizeHint, even with
        # plenty of slack on the right (caught by user feedback;
        # see test_status_label_keeps_full_width_in_card).
        # stretch=1 on status (and dropping the trailing
        # addStretch) flips the priority: status absorbs both the
        # surplus AND deficit. Surplus → status renders wider but
        # text stays left-aligned (visually identical to before).
        # Deficit → status still elides first, which is the correct
        # behaviour ("active …" is more graceful than "Opu…").
        status_label = _ElidingLabel()
        status_label.setObjectName("status_label")
        status_label.setStyleSheet(_STYLE_STATUS)
        status_label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        bottom.addWidget(status_label, 1)

        outer.addLayout(bottom)

        self._update_row(btn, view)
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

    def _update_row(self, btn: QPushButton, view: SessionView) -> None:
        """Refresh dot / name / cost / model chip / status on every tick.

        Each label is updated only when its computed value differs from
        the current text (Qt repaints on setText regardless of whether
        the string changed) — keeps the row stable through the periodic
        refresh cadence."""
        details: SessionDetails | None = None
        if self._get_session_details is not None:
            try:
                details = self._get_session_details(view.session)
            except Exception as exc:
                # Detail composition is enrichment, not load-bearing —
                # never let a composer hiccup take down the row update.
                import sys as _sys
                print(f"[claude-island] session details failed: {exc}",
                      file=_sys.stderr)
        title = (
            (details.name if details and details.name else None)
            or (details.ai_title if details and details.ai_title else None)
            or view.project_path.name
            or str(view.project_path)
        )
        # "—" when details are unavailable rather than falling back to
        # an age string — keeps the meta slot semantically consistent
        # ("this column is always cost").
        meta_text = _fmt_money(details.cost_usd) if details is not None else "—"

        # Activity dot — green/yellow/grey based on freshness. Same
        # palette as the capsule so the user learns one mapping globally.
        # High-cost sessions take precedence: cumulative spend over the
        # alert threshold flips the dot to a yellow ⚡ glyph regardless
        # of recency, so the user can spot expensive sessions even
        # after they've gone idle.
        high_cost = (
            details is not None and details.cost_usd >= _HIGH_COST_USD_THRESHOLD
        )
        # "Currently running" detection. Read directly from
        # ``view.is_running`` (the @property derived from view.phase, which
        # itself is resolved by compose_session_view via the hook ↔
        # pid.json ↔ activity heuristic priority chain).
        #
        # This used to be a per-row priority chain over details.status —
        # but that BYPASSED compose_session_view's pid.json freshness
        # gate (Bug B fix). The capsule reads view.is_running too; using
        # the same field here keeps capsule and row in lockstep, so a
        # stale "busy" no longer makes the row show running while the
        # capsule (correctly) doesn't.
        running = view.is_running

        # Two independent signal channels:
        #   - Leftmost slot (glyph + running accent bar) → liveness.
        #   - Cost label colour                          → high-cost.
        # The cost label IS the high-cost warning — a yellow "$132"
        # is more direct than a separate icon or edge bar (YNAB /
        # Mint pattern: the expensive value highlights itself).
        glyph: _RowStatusGlyph | None = getattr(btn, "_status_glyph", None)
        if glyph is not None:
            # v3: drive wave-vs-dot AND the wave's tint from view.phase.
            # set_phase routes IDLE/ENDED to the dot path with the
            # freshness colour we'd pick anyway, and active phases to
            # the wave with the phase-specific tint.  Legacy bool
            # glyphs that don't have set_phase fall back to set_state.
            if hasattr(glyph, "set_phase"):
                glyph.set_phase(
                    view.phase,
                    idle_dot_color=_activity_color(view.last_activity),
                )
                glyph.setToolTip(
                    "Currently running — JSONL recently updated"
                    if running else ""
                )
            elif running:
                glyph.set_state(_RowStatusGlyph.STATE_RUNNING)
                glyph.setToolTip("Currently running — JSONL recently updated")
            else:
                glyph.set_state(
                    _RowStatusGlyph.STATE_IDLE,
                    dot_color=_activity_color(view.last_activity),
                )
                glyph.setToolTip("")

        # Left-edge accent bar — animates while phase is active.
        # v3: tint follows view.phase (amber for thinking, phosphor for
        # tool_use, red-warm for waiting_approval, etc).  set_phase
        # internally turns the animation off for IDLE/ENDED.  Fall back
        # to the legacy bool entry point on row classes that haven't
        # added set_phase (e.g. test stubs).
        if hasattr(btn, "set_phase"):
            btn.set_phase(view.phase)
        elif hasattr(btn, "set_running"):
            btn.set_running(running)

        name_label = btn.findChild(QLabel, "name_label")
        if name_label is not None and name_label.text() != title:
            name_label.setText(title)

        # v4c: phase icon + inline status text, driven by view.phase.
        phase_icon = btn.findChild(QLabel, "phase_icon")
        if phase_icon is not None:
            char = _phase_icon_char(view.phase)
            tint = _LabColor.for_phase(view.phase)
            phase_icon.setText(char)
            phase_icon.setStyleSheet(
                f"color: {tint}; font-size: 12px; font-weight: 600;"
            )

        status_inline = btn.findChild(QLabel, "status_inline")
        if status_inline is not None:
            inline_text = _phase_inline_text(view)
            tint = _LabColor.for_phase(view.phase)
            status_inline.setText(inline_text)
            status_inline.setStyleSheet(
                f"color: {tint}; font-size: 11px;"
            )

        # v4c: cwd label on the middle row.  Tilde-prefixed if the path
        # lives under the user's home so the row stays short for the
        # common case.  Empty paths (degraded SessionView) show "—" to
        # keep the row's three-line geometry stable across states.
        cwd_label = btn.findChild(QLabel, "cwd_label")
        if cwd_label is not None:
            try:
                import os as _os
                p = str(view.project_path) if view.project_path else ""
                home = str(_os.path.expanduser("~"))
                if p and p.startswith(home):
                    cwd_text = "~" + p[len(home):]
                else:
                    cwd_text = p or "—"
            except Exception:
                cwd_text = str(view.project_path) if view.project_path else "—"
            if cwd_label.text() != cwd_text:
                cwd_label.setText(cwd_text)

        meta_label = btn.findChild(QLabel, "meta_label")
        if meta_label is not None and meta_label.text() != meta_text:
            meta_label.setText(meta_text)

        # Cost label colour — yellow + bold when high-cost so the
        # number itself flags the warning without a separate icon.
        if meta_label is not None:
            target_cost_style = (
                _STYLE_COST_HIGH if high_cost else _STYLE_COST_DEFAULT
            )
            if meta_label.styleSheet() != target_cost_style:
                meta_label.setStyleSheet(target_cost_style)

        # Row-level tooltip + cursor communicate whether click-to-focus
        # is actually wired. By default no tooltip (high-cost is signalled
        # via the yellow cost label, not a hover-anywhere tooltip — the
        # latter covered rows below the cursor and reused the dollar
        # amount, NN/g "obvious or redundant" rule). When the adapter
        # has dropped FOCUS from this view's capabilities (typical for
        # tmux/screen sessions whose daemonized server breaks the chain
        # to a UI host), remove the pointer affordance and surface a
        # tooltip explaining the no-op rather than letting the user
        # click into the void.
        focus_supported = Capability.FOCUS in view.capabilities
        if focus_supported:
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            # Open-vibe-island parity (2026-05-14): when hook-captured
            # data is available, surface it as a hover tooltip — last
            # prompt, current tool, terminal app. This mirrors their
            # session row showing summary text + tool icon.
            btn.setToolTip(_row_tooltip(view))
        else:
            btn.setCursor(Qt.CursorShape.ArrowCursor)
            # FOCUS gets stripped at compose time when no UI app
            # ancestor is reachable. Three real-world causes feed this
            # path: (1) tmux/screen daemonization severs the chain,
            # (2) Privacy & Security ▶ Automation has revoked the
            # System Events permission, (3) osascript hit a transient
            # timeout. Hint at all three so the user can self-diagnose
            # rather than assume tmux. The exact failure reason is
            # logged at WARNING by ``_macos_common`` — see stderr.
            btn.setToolTip(
                "Click-to-focus unavailable for this session.\n"
                "Possible causes:\n"
                "  • tmux/screen daemonization (host terminal not in "
                "process tree)\n"
                "  • System Events automation permission denied\n"
                "    (System Settings ▶ Privacy & Security ▶ Automation)\n"
                "  • Transient osascript failure (retry in 30s)\n"
                "Right-click for session details."
            )

        # Model chip. Priority:
        #   1. ``latest_model`` — the model from the most recent
        #      UsageRecord. Reflects "what model is the session using
        #      *right now*", not "what model cost the most over the
        #      session's lifetime" (which was misleading for sessions
        #      that switched from an expensive to a cheap model).
        #      Updated by _build_session_details via
        #      usage_registry.get_latest_model on every refresh tick,
        #      so model switches propagate within a tick.
        #   2. ``per_model[0]`` — fallback for paths (some composers,
        #      tests) that don't populate latest_model.
        #   3. Empty string — chip hidden.
        model_id = ""
        if details is not None:
            if details.latest_model and not details.latest_model.startswith("<"):
                model_id = details.latest_model
            elif details.per_model:
                for m in details.per_model:
                    if not (m.model or "").startswith("<"):
                        model_id = m.model
                        break
        chip_label = btn.findChild(QLabel, "model_chip")
        if chip_label is not None:
            # NEVER use hide()/show() on this chip. The row is first
            # rendered while the JSONL backfill is still running for
            # sessions whose uuid was only just resolved (e.g. the
            # ``--resume <name>`` reverse-lookup path). On that first
            # tick ``model_id == ""`` — an earlier revision hid the
            # chip widget then, and Qt cached the resulting zero-width
            # layout slot. Once backfill completed and ``show()`` ran,
            # the chip was marked visible but its slot stayed
            # collapsed, leaving the chip permanently invisible.
            # Instead, drive the chip with text + style only and poke
            # the layout to recompute on every tick so the slot grows
            # from 0 to the chip's real sizeHint as soon as the model
            # arrives. Caught 2026-05-17 on cc-learning.
            if model_id:
                chip_text = _resolve_model_short_name(model_id)
                chip_color = _resolve_model_color(model_id)
                target_chip_style = _STYLE_MODEL_CHIP.format(color=chip_color)
            else:
                chip_text = ""
                target_chip_style = _STYLE_MODEL_CHIP_EMPTY
            if chip_label.text() != chip_text:
                chip_label.setText(chip_text)
            if chip_label.styleSheet() != target_chip_style:
                chip_label.setStyleSheet(target_chip_style)
            chip_label.adjustSize()
            chip_label.updateGeometry()
            parent = chip_label.parentWidget()
            if parent is not None:
                if parent.layout() is not None:
                    parent.layout().invalidate()
                    parent.layout().activate()
                parent.updateGeometry()

        status_label = btn.findChild(QLabel, "status_label")
        if status_label is not None:
            status_text = _row_status_text(view)
            if status_label.text() != status_text:
                status_label.setText(status_text)

        # NB: row tooltip is owned by the high-cost branch above —
        # don't blanket-clear it here or the warning gets wiped on
        # every refresh tick.

        btn.setProperty("_session", view)

    # ------------------------------------------------------------------
    # Card composition (same-tab grouping)
    # ------------------------------------------------------------------

    def _make_group_widget(
        self, group: "SessionGroup", *, palette_idx: int | None = None
    ) -> QWidget:
        """One group → one widget. Single-view groups render as a
        standalone rounded button; multi-view groups render as a
        rounded card with flat internal rows + thin separators.

        ``palette_idx`` is supplied by the caller (it counts multi-card
        position) so adjacent multi-cards never collide on tint.
        Singletons ignore it."""
        views = list(group.views)
        if len(views) == 1:
            row = self._get_or_create_row(views[0], views, in_card=False, card=None)
            row.setParent(None)
            return row
        return self._get_or_create_card(group, views, palette_idx=palette_idx or 0)

    def _get_or_create_card(
        self,
        group: "SessionGroup",
        views: list["SessionView"],
        *,
        palette_idx: int = 0,
    ) -> QFrame:
        """Render a session group as a flat list bracketed by a 1 px
        rounded outline (ghost-card pattern), reusing the QFrame
        across snaps when the same ``group_id`` reappears.

        Cache key is ``group.group_id`` (stable for one logical group
        — same WT window + cwd, same iTerm2 (window, tab), etc).
        On a hit we empty the inner layout (rows are detached, not
        destroyed; they survive in ``self._rows``) and refill with
        the current view set. setStyleSheet runs only on first
        creation, which is the costly part — Qt parses the CSS and
        polishes the frame on assignment."""
        del palette_idx  # outline colour is uniform now; kept for caller API compat
        card = self._cards.get(group.group_id)
        if card is None:
            card = QFrame()
            card.setObjectName("group_card")
            card._base_bg = _BG_GROUP
            card.setStyleSheet(
                "QFrame#group_card {"
                f" background: transparent;"
                f" border: 1px solid {_GROUP_OUTLINE_COLOR};"
                f" border-radius: 10px;"
                f" padding: {_GROUP_OUTLINE_PAD_PX}px;"
                "}"
            )
            layout = QVBoxLayout(card)
            layout.setContentsMargins(0, 0, 0, 0)
            layout.setSpacing(2)
            self._cards[group.group_id] = card
        else:
            # Empty the inner layout. Children are detached (setParent
            # None) so the cached row buttons in ``self._rows`` survive
            # — only the layout slot is recycled. Without setParent
            # before re-adding, Qt warns "QLayout: Attempting to add
            # QLayoutItem ... twice".
            inner = card.layout()
            while inner.count():
                item = inner.takeAt(0)
                w = item.widget()
                if w is not None:
                    w.setParent(None)
        layout = card.layout()
        for view in views:
            row = self._get_or_create_row(view, views, in_card=True, card=card)
            row.setParent(None)
            layout.addWidget(row)
        return card

    def _get_or_create_row(
        self,
        view: "SessionView",
        group: list["SessionView"],
        *,
        in_card: bool,
        card: QFrame | None,
    ) -> HoverRow:
        btn = self._rows.get(view.pid)
        if btn is None:
            btn = self._make_row(view, parent_card=card)
            self._rows[view.pid] = btn
        else:
            btn.set_parent_card(card)
        btn.setStyleSheet(_STYLE_GROUP_ROW if in_card else _STYLE_SINGLE_ROW)
        self._update_row(btn, view)
        siblings = [s for s in group if s.pid != view.pid]
        btn.setProperty("_siblings", siblings)
        return btn

    # ------------------------------------------------------------------
    # Layout housekeeping
    # ------------------------------------------------------------------

    def _clear_session_layout(self) -> None:
        """Remove every top-level item from session_box. Cached row
        buttons AND cached card frames are detached (kept alive in
        self._rows / self._cards for reuse); the placeholder is
        deleted (recreated on demand by _show_placeholder).

        B-003/A-004: when an item is a wrapper QWidget (the
        group + inline-decisions container built by
        _render_session_groups for matched-decision groups), it isn't
        in cached_rows or cached_cards, so the old code went straight
        to deleteLater() while the wrapper still owned cached children.
        That worked by accident-of-order because Qt's deferred-delete
        runs only after the synchronous rebuild loop re-parents the
        cached children OFF the wrapper (via setParent(None)). To
        remove the implicit dependency on rebuild ordering, we now
        explicitly detach any cached row/card/decision-card descendant
        before deleteLater().
        """
        cached_rows = set(self._rows.values())
        cached_cards = set(self._cards.values())
        while self._session_box.count():
            item = self._session_box.takeAt(0)
            widget = item.widget()
            if widget is None:
                continue
            if widget in cached_rows or widget in cached_cards:
                widget.setParent(None)
                continue
            if widget is self._placeholder:
                self._placeholder = None
            else:
                # Wrapper or other non-cached top-level: detach any
                # cached children so deleteLater doesn't cascade-destroy
                # them on the next event-loop tick.
                for child in widget.findChildren(QWidget):
                    if child in cached_rows or child in cached_cards:
                        child.setParent(None)
            widget.deleteLater()

    def _show_placeholder(self) -> None:
        # Drop any cached rows AND cards: there are no sessions to
        # back either, and the next non-empty render will recreate
        # whatever the new group set needs.
        for btn in self._rows.values():
            btn.deleteLater()
        self._rows.clear()
        for card in self._cards.values():
            card.deleteLater()
        self._cards.clear()
        if self._placeholder is None:
            self._placeholder = mk_label("No active sessions", elide=False)
            self._placeholder.setStyleSheet("color: #555; font-size: 12px;")
        self._session_box.addWidget(self._placeholder)

    def _hide_placeholder(self) -> None:
        if self._placeholder is not None:
            self._placeholder.deleteLater()
            self._placeholder = None

    def _gc_cards(self, needed_group_ids: set[str]) -> None:
        """Drop card frames whose group_id no longer appears in the
        latest groups tuple. Mirrors _gc_rows for the per-group_id
        QFrame cache.

        Cached row buttons inside the card are detached BEFORE the
        card is destroyed so they survive in self._rows for the next
        render — exactly the same pattern _clear_session_layout uses.
        Without this, deleteLater on the card would cascade-destroy
        any HoverRow buttons still parented inside it."""
        cached_rows = set(self._rows.values())
        for gid in list(self._cards.keys()):
            if gid not in needed_group_ids:
                card = self._cards.pop(gid)
                for child in card.findChildren(QPushButton):
                    if child in cached_rows:
                        child.setParent(None)
                card.deleteLater()

    def _gc_rows(self, needed_pids: set[int]) -> None:
        """Drop rows whose pid is no longer present.

        Animations are explicitly stopped before deleteLater() — without
        this, a QVariantAnimation's valueChanged could in theory fire
        once between the deleteLater event being posted and the actual
        teardown, with the callback running on a half-destroyed widget.
        Cheap defensive call; no observed crash but no reason to leave
        the door cracked."""
        for pid in list(self._rows.keys()):
            if pid not in needed_pids:
                btn = self._rows.pop(pid)
                # Stop the row-level pulse + the glyph's per-bar anims
                # before the widget enters the deferred-delete state.
                if hasattr(btn, "set_running"):
                    btn.set_running(False)
                glyph = getattr(btn, "_status_glyph", None)
                if glyph is not None:
                    glyph.set_state(_RowStatusGlyph.STATE_IDLE)
                btn.deleteLater()

    def _show_detail_popup(self, btn: QPushButton, local_pos) -> None:
        """Build a SessionDetailPopup with fresh data and pop it at the
        cursor. Called from the row's customContextMenuRequested signal.

        We compose details on demand (not from a cached value) so the
        popup always reflects the latest cost / status. Failures in the
        composer are logged and the popup still opens with whatever
        partial data is available — falling back to None when the
        composer is unwired or the lookup raises."""
        view: SessionView = btn.property("_session")
        if view is None:
            return
        details: SessionDetails | None = None
        if self._get_session_details is not None:
            try:
                details = self._get_session_details(view.session)
            except Exception as exc:
                import sys as _sys
                print(f"[claude-island] detail popup composer failed: {exc}",
                      file=_sys.stderr)
        # All capability actions flow through the injected dispatch.
        # Each callback closes over `view` so the popup itself never
        # holds capability/scope knowledge — it just calls the closure.
        # The popup's own try/except catches any escape (dispatcher is
        # supposed to swallow internally, but a defence-in-depth catch
        # at the popup boundary keeps the UI alive even if it doesn't).
        def _do_rename(_uuid: str, new_name: str) -> bool:
            return bool(self._dispatch(view, Capability.RENAME, new_name=new_name))

        def _do_open_folder() -> bool:
            return bool(self._dispatch(view, Capability.REVEAL_CWD))

        def _do_open_transcript() -> bool:
            return bool(self._dispatch(view, Capability.REVEAL_TRANSCRIPT))

        def _do_strip_thinking() -> bool:
            return bool(self._dispatch(view, Capability.RESET_THINKING))

        popup = SessionDetailPopup(
            details, view, parent=self,
            on_rename=_do_rename,
            on_open_folder=_do_open_folder,
            on_open_transcript=_do_open_transcript,
            on_strip_thinking=_do_strip_thinking,
            # G8 — without these the "Review prompts" checkbox row is
            # hidden and the entire UserPromptSubmit-intercept flow is
            # unreachable in production (regression caught in code review C-001).
            get_review_mode=self._get_review_mode,
            set_review_mode=self._set_review_mode,
        )
        # Force a layout activation BEFORE move + show so the popup's
        # sizeHint reflects the actual content height. Without this,
        # Qt's first sizeHint underestimates by a few px (QLabel
        # word-wrap heights aren't known until the label has its real
        # width), Qt issues setGeometry with the underestimated size,
        # then Windows clamps up to the real minimum and Qt logs the
        # benign "Unable to set geometry" warning. Activating the
        # layout up front lets sizeHint settle before show — no
        # warning, identical visual outcome.
        popup.ensurePolished()
        if popup.layout() is not None:
            popup.layout().activate()
        popup.adjustSize()
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

    def _on_row_clicked(self, view: "SessionView", siblings: list["SessionView"]) -> None:
        # Just activate the terminal — the panel auto-hides via
        # ``event``'s WindowDeactivate hook when the activation moves
        # focus to the terminal app. SetForegroundWindow's foreground-
        # transfer rule is still satisfied: we ARE the foreground
        # process at the moment of dispatch, before focus shifts.
        # Siblings are forwarded as full SessionViews; WindowsTerminal-
        # Adapter uses them for the inactive-split-pane case (when the
        # clicked row's sentinel isn't in any UIA TabItem.Name, fall
        # back to a same-cwd sibling's sentinel to switch the WT tab).
        # Other adapters accept-and-ignore.
        self._dispatch(view, Capability.FOCUS, siblings=siblings)

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
