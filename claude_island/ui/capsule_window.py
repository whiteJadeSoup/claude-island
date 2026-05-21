from __future__ import annotations

import sys
from datetime import datetime, timezone
from typing import NamedTuple

from PySide6.QtCore import (
    QEasingCurve, QPoint, QPropertyAnimation, Qt, QTimer,
)
from PySide6.QtGui import QColor, QPainter, QPainterPath
from PySide6.QtWidgets import (
    QApplication,
    QLabel,
    QMenu,
    QWidget,
)

from claude_island.core.snapshot import SessionView, WorldSnapshot
from .fonts import UI_FONT_FAMILY, UI_FONT_STACK
from .lab_palette import Color as _C, FontStack as _F
from .controller import IslandController
from .expanded_window import _RowStatusGlyph
from .window_position import load_position as _load_saved_position
from .window_position import save_position as _save_position


def _qcolor_from_hex(hex_str: str, alpha: int = 255) -> QColor:
    """``"#0e0e10"`` → ``QColor(14, 14, 16, alpha)``.

    Single helper so the capsule (which paints with QColor objects, not
    QSS strings) can consume the same hex tokens as the QSS surfaces.
    Keeps both worlds in sync without re-declaring colours by RGB tuple.
    """
    h = hex_str.lstrip("#")
    return QColor(int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16), alpha)

# Two widths: normal (no quota mini-bar) vs warning (quota mini-bar
# appended on the right). Switching between them happens on every
# refresh that crosses the warn threshold; the resize is unobtrusive
# at this scale and re-centring keeps the pill anchored to the top.
_CAPSULE_W = 200
_CAPSULE_W_WITH_QUOTA = 290
_CAPSULE_H = 36
_DOT_W = 18
_DOT_H = 18
# Hover-expanded dot: the affordance for "you can click me to open the
# panel even with zero sessions". Stays small enough not to scream at
# the user — this is the quiet-state visual. ~28 px is the smallest
# round target the eye unambiguously reads as "tappable".
_DOT_HOVER_W = 28
_DOT_HOVER_H = 28
_TOP_MARGIN = 8

# Long-press → unlock free-drag (2D + edge snap).
# 500 ms is the iOS / Android conventional long-press threshold —
# long enough to not fire on impatient clicks, short enough to feel
# responsive. AssistiveTouch and Chat Heads both sit in this range.
_LONG_PRESS_MS = 500
# Snap animation: 200 ms with OutBack easing reproduces the "spring
# to edge" bounce Messenger Chat Heads uses on release. Shorter feels
# abrupt, longer feels sluggish.
_SNAP_DURATION_MS = 200
# Opacity while in free-drag mode. 0.7 gives the user a clear signal
# they've entered a different mode (compared to default 1.0) without
# making the pill so faint it's hard to track during the drag.
_FREE_DRAG_OPACITY = 0.7

# Edge-idle half-hide: when the capsule is docked at a non-top edge
# (bottom/left/right), it shrinks to a thin strip and fades so the
# user's working area stays uncluttered. Hover restores it. Mirrors
# AssistiveTouch idle opacity (40%) tuned a bit brighter for desktop
# where the user's eye is further from the screen.
_IDLE_W = 60         # narrower than _DOT_LEFT_PAD + dot + label slot
_IDLE_OPACITY = 0.6  # faint enough to disappear, visible enough to find
# Edges that trigger half-hide. Top is always full-presence (it's
# the home position).
_IDLE_EDGES = ("bottom", "left", "right")

_DOT_LEFT_PAD = 12  # px from pill's left edge to dot's left edge
_DOT_LABEL_W = 14   # px reserved for the "●" glyph
_TEXT_LEFT = _DOT_LEFT_PAD + _DOT_LABEL_W + 4  # 4px gap between dot and text

# Cost is rendered in its own fixed right-side slot so a long session
# name can never push it off the pill. 56 px fits "$999.99" — three
# digits + decimals + symbol — at the body weight + size used by
# _STYLE_LABEL with a few px of slack. The slot is only allocated
# when cost > 0; days with no spend give the space back to the name
# region. 8 px gap keeps the two regions visually distinct without
# wasting width.
_COST_SLOT_W = 56
_NAME_COST_GAP = 8

# Mini quota progress bar dimensions. 56 px is roughly the same density
# as the iOS battery widget — recognisable as a progress indicator
# without competing with the text for attention.
_QUOTA_BAR_W = 56
_QUOTA_BAR_H = 6
_QUOTA_RIGHT_PAD = 12  # px from pill's right edge to bar's right edge

# Threshold % at which the mini quota bar starts surfacing on the
# capsule. Below the threshold the bar is hidden so the pill doesn't
# carry a green-only "everything's fine" indicator that adds noise
# without information. Once the user is approaching the rate-limit
# cliff the indicator becomes useful.
# Re-exported from core/quota_palette so the capsule and the expanded
# panel always agree on warn / critical boundaries — without a single
# source two surfaces can give the user contradictory severity
# readings for the same snapshot %.
from claude_island.core.quota_palette import (
    WARN_PCT as _QUOTA_WARN_THRESHOLD,
    CRITICAL_PCT as _QUOTA_CRITICAL_THRESHOLD,
    quota_bar_color as _quota_bar_color,
    quota_severity as _quota_severity,
)

# Multi-running carousel cadence. 3 s lands at the sweet spot: a
# casual glance reads a name in ~0.5–1 s, so 3 s gives the user
# enough time to read each name twice without the pill feeling
# either rushed or stale. Faster (≤2 s) reads as glitching; slower
# (≥5 s) makes the carousel feel pointless on busy machines where
# running sessions come and go in the same window.
_ROTATE_INTERVAL_MS = 3000

# Heuristic for "this session is currently doing something". An active
# Claude Code session writes a JSONL row at least every few seconds
# while the model is streaming or the tool loop is iterating; once the
# user has stopped typing and the model is done, writes pause. 30 s
# gives a comfortable margin for one tool round-trip without flapping
# the breathing animation off mid-burst.
_ACTIVE_THRESHOLD_SECONDS = 30

# (Breathing constants removed — the dot's pulse animation moved
# inside _RowStatusGlyph and runs as the equalizer-bar wave there.
# Capsule no longer owns a QPropertyAnimation directly.)

# v3 lab-console: all text in mono.  Primary text is paper-tinted (warm
# white), cost is paper-dim so the name reads as the primary anchor.
_STYLE_LABEL = (
    f"color: {_C.paper}; font-size: 12px; font-family: {_F.mono_stack};"
)
_STYLE_COST = (
    f"color: {_C.paper_dim}; font-size: 12px; font-family: {_F.mono_stack};"
)
# Equalizer-bar colours.  Kept aligned with the row glyph so the same
# wave shows up identically in the pill and the panel.  Values sourced
# from lab_palette so a future palette tweak propagates in one edit.
_DOT_RUNNING_COLOR = _C.phosphor
_DOT_IDLE_COLOR    = _C.paper_faint
# Capsule body background — v3 matte near-black.
_BG_COLOR          = _qcolor_from_hex(_C.ink, 230)
# Warn / critical washes — still amber / carmine in spirit, but tinted
# darker so they read as "the body has been stained, not painted" against
# the v3 ink baseline.  The row chip's amber/red stay vibrant on hover;
# the pill wash is the ambient cue.
_BG_COLOR_WARN     = _qcolor_from_hex(_C.amber_dim,    230)
_BG_COLOR_CRITICAL = _qcolor_from_hex(_C.red_warm_dim, 230)
# v3 dot is the smallest persistent surface — render as a near-black
# "stamp" rather than a grey pill so it sits as a deliberate desk
# object (matches prototype-v3.html's .dot baseline) instead of looking
# like a faded badge.  The rim stroke (drawn in paintEvent) supplies
# the v3 "tally tick" — the dot's only chrome.
_DOT_COLOR         = _qcolor_from_hex(_C.ink, 230)
# Rim stroke around the dot — 1 px of rule_bright reads as the stamp's
# edge, not as a focus indicator.
_DOT_RIM_COLOR     = _qcolor_from_hex(_C.rule_bright, 220)
# Urgent rim — kicks in when there are queued decisions.  red_warm
# matches the row strip + glyph wave when waiting_approval.
_DOT_RIM_URGENT    = _qcolor_from_hex(_C.red_warm, 220)
# Mini quota bar track.  Paper-faint at low alpha — visible against the
# ink wash, never bright enough to compete with the filled portion.
_QUOTA_BAR_TRACK = _qcolor_from_hex(_C.paper_faint, 60)
# BG wash colours keyed by severity name.  Dark variants of the bar
# palette so the wash stays a contextual cue rather than competing with
# the bar for attention.  The ok variant uses the same ink as _BG_COLOR
# — no quota wash below the warn band.
_BG_BY_SEVERITY: dict[str, QColor] = {
    "ok":       _BG_COLOR,
    "warn":     _BG_COLOR_WARN,
    "critical": _BG_COLOR_CRITICAL,
}
_STYLE_QUOTA_PCT = (
    f"color: {_C.amber}; font-size: 11px; font-weight: 600; "
    f"font-family: {_F.mono_stack};"
)
_STYLE_QUOTA_PCT_CRITICAL = (
    f"color: {_C.red_warm}; font-size: 11px; font-weight: 600; "
    f"font-family: {_F.mono_stack};"
)


def _pos_visible_on_any_screen(x: int, y: int, w: int, h: int) -> bool:
    """True iff a window of size (w, h) at (x, y) overlaps any
    currently-connected screen.

    Used to validate a persisted position from disk: a saved (x, y)
    that lived on a now-disconnected monitor would land off-screen,
    so the loader falls back to the default centred position. Uses
    QScreen.geometry() (not availableGeometry) — we want the raw
    screen bounds, not the menubar/dock-excluded region, so a
    capsule sitting in the menubar slot on macOS still counts as
    visible.

    Cross-platform: QScreen API returns identical structures on
    Windows / macOS / Linux."""
    from PySide6.QtCore import QRect
    rect = QRect(x, y, w, h)
    for screen in QApplication.screens():
        if screen.geometry().intersects(rect):
            return True
    return False


# Single source of truth lives in core/formatting.py — capsule and
# expanded both alias the canonical impl so dedup and rendering stay
# in lock-step (changing the bands changes both at once).
from claude_island.core.formatting import fmt_money as _fmt_money


class CapsuleData(NamedTuple):
    """Pre-resolved view-model the capsule actually renders.

    Output of ``CapsuleWindow.compute(snap)``. Used as both the
    ``distinct_until_changed`` key (NamedTuple gets structural eq for
    free) AND the ``render`` input — same value flows both places, so
    dedup precision is automatically equal to render precision.

    Field choice = exactly what the capsule's render code reads. Adding
    a field here means "capsule actually displays this"; not adding it
    means "capsule doesn't care, micro-changes don't trigger re-render".

    Quantisation:
      * ``cost_str`` is the formatted output (``"$54"`` etc), not the
        raw float — micro-cost ticks within a price band don't change
        the string and therefore don't change the dedup key.
      * ``quota_pct`` is ``int`` (truncated), not float — sub-percent
        wobble doesn't change the key.
    """

    flat_count: int                      # total session count
    running_names: tuple[str, ...]       # names of running sessions (carousel feed)
    cost_str: str                        # "" if cost <= 0, else _fmt_money(cost)
    quota_pct: int | None                # None if no quota, else 0..100 truncated
    awaiting_count: int = 0              # v4c: pending decisions count
    active_name: str = ""                # v4c: most-prominent live session name
    # v4c: phase of the active session, used to colour the equalizer
    # wave so the capsule reads the same hue as the corresponding row
    # in the expanded list ("review" thinking → purple wave both in
    # the row and on the pill).  Stored as the string ``value`` so
    # CapsuleData stays hashable + structurally comparable for
    # ``distinct_until_changed``.  Empty string when no active session.
    active_phase: str = ""

# Right-click menu — v3 surface tints + mono font.  No rounded corners
# on items (v3 reserves rounded for the wax-stamp / red shock visuals
# we explicitly removed); the menu reads as a flat lab-bench panel.
_STYLE_MENU = f"""
    QMenu {{
        background: {_C.surface};
        color: {_C.paper};
        border: 1px solid {_C.rule};
        padding: 4px;
        font-size: 12px;
        font-family: {_F.mono_stack};
    }}
    QMenu::item {{ padding: 6px 18px; border-radius: 0; }}
    QMenu::item:selected {{ background: {_C.surface_hi}; }}
    QMenu::separator {{ height: 1px; background: {_C.rule}; margin: 4px 6px; }}
"""


class CapsuleWindow(QWidget):
    """Frameless, always-on-top pill anchored to the top-centre of the screen.

    Clicking toggles the expanded panel via the controller.
    Resizes to a small dot when there are no active sessions.
    """

    def __init__(self, controller: IslandController) -> None:
        super().__init__()
        self._controller = controller
        self._is_dot = True
        # Once the user picks "Hide" from the right-click menu the capsule
        # stays gone until the next process restart — there is no tray icon
        # to bring it back, so all auto-show paths must respect this flag.
        self._hidden_by_user = False
        # Latest computed view-model. Populated by render(data); read
        # by _compose_label_text, _refresh_active_state, _paint_quota_bar.
        # Empty default so any path called before the first render
        # doesn't AttributeError.
        self._data: CapsuleData = CapsuleData(
            flat_count=0, running_names=(), cost_str="", quota_pct=None,
            awaiting_count=0, active_name="",
        )
        # Multi-running carousel: when ≥2 sessions are running, cycle
        # the pill text through their names every _ROTATE_INTERVAL_MS
        # so the user can see WHICH sessions are live (the count form
        # "2 sessions" tells you how many but not which). Index resets
        # every render(snap) when the running set changes; the timer
        # itself starts on first render with ≥2 running and stops when
        # the count drops back to ≤1.
        self._rotation_names: list[str] = []
        self._rotation_index: int = 0
        self._rotation_timer = QTimer(self)
        self._rotation_timer.setInterval(_ROTATE_INTERVAL_MS)
        self._rotation_timer.timeout.connect(self._on_rotate_tick)

        # ── Drag state (horizontal drag along the top) ────────────────
        # User can press-and-hold the pill, then drag it left/right to
        # reposition it horizontally along the top edge. The X coord
        # is persisted so the position survives restarts. Y is locked
        # to the top margin here — long-press promotion to free-drag
        # (below) unlocks Y.
        #
        # Drag-vs-click discrimination uses
        # QApplication.startDragDistance() (system-tunable, defaults to
        # ~10 px on win/mac/linux) so a sloppy click doesn't reposition
        # the window and a deliberate drag doesn't accidentally toggle
        # the panel.
        #
        # _drag_origin_global: mouse-down global QPoint. None when not
        #   currently in a press-hold cycle.
        # _drag_origin_window: capsule's pos() captured at mouse-down.
        # _is_dragging: True only after the cursor has moved past the
        #   click-distance threshold; used to decide click-vs-drag at
        #   release time.
        # _persisted_pos: (x, y) restored from disk at construction.
        #   None ⇒ no saved position (first run, or save file deleted).
        self._drag_origin_global: QPoint | None = None
        self._drag_origin_window: QPoint | None = None
        self._is_dragging: bool = False
        self._persisted_pos: tuple[int, int] | None = _load_saved_position()

        # ── Target geometry (authoritative position-and-size record) ──
        # Updated by ``_set_target_geometry`` (the single entry point
        # for capsule geometry changes) and by ``moveEvent`` (catches
        # user drag, which goes through ``self.move()``).
        #
        # Why this exists: ``controller.state_changed`` fans out to
        # *both* this widget and the ExpandedWindow. When the user
        # clicks to expand, the capsule's slot runs first and calls
        # setGeometry to switch dot→pill (18×18 → 200×36, sometimes
        # the X also shifts); then the panel's slot runs and needs
        # to anchor below the capsule. Reading ``self.frameGeometry()``
        # at that moment is unsafe — Qt's reported frame can lag the
        # most recent setGeometry call by an event-loop tick on macOS,
        # so the panel sees stale (dot-sized) geometry and positions
        # itself overlapping the actually-rendered pill. ``target_geometry``
        # is updated synchronously inside setGeometry's caller, so the
        # panel always reads what the capsule *just decided* to be,
        # regardless of when the OS finishes the resize.
        from PySide6.QtCore import QRect as _QRect  # local — not at file top to keep import set minimal
        self._target_geom: _QRect | None = None

        # ── Free-drag state (long-press to unlock 2D drag + edge snap) ──
        # A press-and-hold of _LONG_PRESS_MS unlocks "free drag mode"
        # (Y axis unlocked, capsule renders semi-transparent so the
        # user sees they've entered a different mode). On release the
        # capsule animates to the nearest of the 4 screen edges
        # (top / bottom / left / right) at the centre of the edge —
        # never floats in the middle. This mirrors AssistiveTouch /
        # Chat Heads "anchor to edge" semantics so the user's eye
        # always knows where the capsule lives.
        #
        # The long-press timer races with the click-distance threshold:
        # if the user moves the cursor past startDragDistance BEFORE
        # the timer fires, we fall through to horizontal-drag mode
        # (Y locked) — fast small adjustments shouldn't have to wait
        # 0.5 s. The timer is cancelled in that branch.
        #
        # _long_press_timer: single-shot QTimer started on mouse-down.
        # _is_free_drag: True after _LONG_PRESS_MS expires AND the
        #   user is still holding. Drives mouseMove (Y-unlock) and
        #   mouseRelease (snap-to-edge).
        # _snap_anim: held reference to the active QPropertyAnimation;
        #   without this Python GCs the animation mid-flight and the
        #   capsule jumps to the end position with no transition.
        self._long_press_timer = QTimer(self)
        self._long_press_timer.setSingleShot(True)
        self._long_press_timer.setInterval(_LONG_PRESS_MS)
        self._long_press_timer.timeout.connect(self._on_long_press)
        self._is_free_drag: bool = False
        self._snap_anim: "QPropertyAnimation | None" = None

        # ── Docked-edge idle state (hide-on-non-top-edge + hover) ──
        # After a free-drag-snap to bottom/left/right the capsule
        # half-hides (narrow + 0.6 opacity) so it stops competing
        # with whatever's behind it. enterEvent restores full size
        # (hover-out); leaveEvent collapses back to idle.
        #
        # _docked_edge: "top" / "bottom" / "left" / "right" / None.
        #   None ⇒ home position, no idle behaviour.
        # _is_idle: True when the half-hide is currently applied.
        #   Drives _apply_capsule's geometry decision so a render(snap)
        #   tick during idle doesn't accidentally restore full width.
        self._docked_edge: str | None = None
        self._is_idle: bool = False

        self._setup_window()

        # Status glyph — same widget the panel rows use, dropped into
        # the pill's left slot. Three states (idle/running/high-cost)
        # but only idle + running fire here (high-cost reads as "not
        # currently producing turns" from the capsule's perspective).
        # When running, the equalizer bars wave; when idle, a single
        # static dot.
        # v4c (2026-05-21): capsule overrides the default bar sizes —
        # the pill is much narrower than a row, so 18 px wide / 11 px
        # tall bars would dwarf the surrounding text.  Small bars
        # (~10 px wide × 8 px tall) read as a discreet "live" cue.
        self._dot_label = _RowStatusGlyph(
            self, bar_w=2, bar_gap=1, bar_count=4, wave_h=8,
        )

        # Three-region layout (see _apply_capsule): [dot] [name] [cost].
        # ``_label`` holds the elided session name (left-aligned in its
        # slot); ``_cost_label`` holds the cost string in a fixed
        # right-side slot (right-aligned). Splitting cost into its own
        # widget guarantees a long name can never push the cost off
        # the pill — name and cost live in independent slots.
        self._label = QLabel("", self)
        self._label.setAlignment(
            Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
        )
        self._label.setStyleSheet(_STYLE_LABEL)
        self._cost_label = QLabel("", self)
        self._cost_label.setAlignment(
            Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight,
        )
        self._cost_label.setStyleSheet(_STYLE_COST)

        # True when the equalizer glyph is animating; flipped by
        # _refresh_active_state alongside the glyph state change.
        self._is_breathing = False

        controller.state_changed.connect(self._on_state_changed)
        self._apply_dot()

    # ------------------------------------------------------------------
    # Window setup
    # ------------------------------------------------------------------

    def _setup_window(self) -> None:
        # Qt.Tool maps to NSPanel on macOS, which silently refuses to
        # paint a WA_TranslucentBackground surface — the capsule reports
        # isVisible=True yet nothing reaches the screen. Drop the flag
        # on darwin; the capsule still floats (StaysOnTopHint) and stays
        # frameless. The cosmetic trade-off (it can take focus / show in
        # Cmd+Tab briefly) beats being invisible. Windows is unaffected.
        flags = (
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
        )
        if sys.platform != "darwin":
            flags |= Qt.WindowType.Tool
        self.setWindowFlags(flags)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)

    def _center_top(self, w: int, h: int) -> None:
        """Position the capsule for the given (w, h).

        Honours the user's persisted drag position when one exists
        AND lands on a currently-connected screen; otherwise falls
        back to top-centre on the primary screen. Called from
        _apply_dot / _apply_capsule on every size-affecting state
        change so the user's chosen X survives those transitions.

        Multi-monitor: a persisted position from a now-disconnected
        monitor would land off-screen; ``_pos_visible_on_any_screen``
        guards that. Cross-platform (uses Qt screen geometry).
        """
        if self._persisted_pos is not None:
            x, y = self._persisted_pos
            x = self._clamp_x(x, w)
            if _pos_visible_on_any_screen(x, y, w, h):
                self._set_target_geometry(x, y, w, h)
                return
        # Default — primary-screen top centre, original behaviour.
        screen = QApplication.primaryScreen()
        geom = screen.geometry()
        x = geom.center().x() - w // 2
        self._set_target_geometry(x, geom.top() + _TOP_MARGIN, w, h)

    def _set_target_geometry(self, x: int, y: int, w: int, h: int) -> None:
        """Single entry point for capsule geometry changes.

        Records the new ``(x, y, w, h)`` as the authoritative
        ``_target_geom`` *before* delegating to ``setGeometry``.
        Sibling windows (ExpandedWindow) read this via
        ``target_geometry()`` instead of ``frameGeometry()``, so they
        see the latest intended geometry even when ``state_changed``
        fans out to both windows and Qt's frame report hasn't caught
        up to the most recent ``setGeometry`` call yet.
        """
        from PySide6.QtCore import QRect
        self._target_geom = QRect(x, y, w, h)
        self.setGeometry(x, y, w, h)

    def target_geometry(self):  # -> QRect
        """Authoritative current geometry of the capsule.

        Returns the most recently applied target geometry rather than
        ``frameGeometry()``, which on macOS can return a one-tick-stale
        value during the ``state_changed`` signal fanout (capsule
        resizes, then expanded panel anchors below — the second slot
        must see the new size, not the old one).

        Falls back to ``frameGeometry()`` only before any
        ``_set_target_geometry`` call has happened (extremely
        narrow window during __init__).
        """
        return self._target_geom if self._target_geom is not None else self.frameGeometry()

    def moveEvent(self, event) -> None:  # type: ignore[override]
        # Keep ``_target_geom`` in sync when the user drags the capsule —
        # drag handlers go through ``self.move()`` (position-only), which
        # fires moveEvent. setGeometry-driven changes also fire moveEvent
        # but ``_set_target_geometry`` already populated ``_target_geom``
        # with the correct rect; this is idempotent.
        super().moveEvent(event)
        from PySide6.QtCore import QRect
        g = self.geometry()
        self._target_geom = QRect(g.x(), g.y(), g.width(), g.height())

    def _clamp_x(self, x: int, w: int) -> int:
        """Clamp x so the capsule (width w) stays within the union
        of all connected screens' horizontal extents. Allows the
        capsule to live on any monitor in a multi-monitor setup
        without restricting which one."""
        screens = QApplication.screens()
        if not screens:
            return x
        leftmost = min(s.geometry().left() for s in screens)
        rightmost = max(s.geometry().right() for s in screens)
        return max(leftmost, min(rightmost - w + 1, x))

    def _top_y_for_x(self, centre_x: int) -> int:
        """Return the appropriate top-margin Y for a capsule whose
        horizontal centre lands at ``centre_x``.

        Iterates connected screens and returns ``geom.top() +
        _TOP_MARGIN`` of the first screen whose horizontal range
        contains ``centre_x``. Used by horizontal drag so that
        dragging from one monitor to another lands the capsule on
        THAT monitor's actual top edge.

        Why this matters: without this lookup, a horizontal drag
        keeps Y locked to the drag-origin's Y (= the source screen's
        top + _TOP_MARGIN). On a multi-monitor setup where the
        target screen has a different ``geom.top()`` (different
        height, vertical alignment in display arrangement), the
        capsule floats either above or below the target screen's
        top — visibly broken.

        Falls back to the primary screen if ``centre_x`` lands in a
        desktop gap (mismatched-height monitors arranged so part of
        their X union is one-screen-only). The fallback keeps the
        capsule visible rather than letting it drift off-screen."""
        for screen in QApplication.screens():
            g = screen.geometry()
            if g.left() <= centre_x <= g.right():
                return g.top() + _TOP_MARGIN
        return QApplication.primaryScreen().geometry().top() + _TOP_MARGIN

    # ------------------------------------------------------------------
    # State handlers
    # ------------------------------------------------------------------

    def _on_state_changed(self, state: str) -> None:
        if self._hidden_by_user:
            return
        # Three controller states collapse to two capsule visuals: 'dot' is
        # the minimal presence; both 'collapsed' and 'expanded' show the
        # full pill (the expanded panel is a separate window stacked below).
        if state == "dot":
            self._apply_dot()
        else:
            self._apply_capsule()

    def _apply_dot(self, *, hover: bool = False) -> None:
        """Render the quiet "no active sessions" indicator.

        The dot is clickable: pressing it opens the expanded panel
        (controller transition ``user_expand`` on source=``dot``) so
        the user can still reach Recents / Spend / Quota when no
        live claude sessions exist. The hover-grow affordance signals
        "this is a target" without making the resting visual loud.

        ``hover`` flips between the resting (~18 px) and hovered (~28
        px) size. ``enterEvent`` / ``leaveEvent`` call this with
        ``hover=True``/``False`` to drive the grow/shrink.
        """
        self._is_dot = True
        w = _DOT_HOVER_W if hover else _DOT_W
        h = _DOT_HOVER_H if hover else _DOT_H
        self._center_top(w, h)
        self._label.hide()
        self._cost_label.hide()
        self._dot_label.hide()
        # Glyph state goes IDLE while in dot mode — equalizer animation
        # would be wasted CPU on an invisible widget.
        self._dot_label.set_state(
            _RowStatusGlyph.STATE_IDLE, dot_color=_DOT_IDLE_COLOR,
        )
        self._is_breathing = False
        # Cursor flips to PointingHand so the user knows it's clickable.
        # Set unconditionally here (cheap, idempotent) rather than only
        # at construction; future _apply_capsule transitions also need
        # the pointer cursor and they call setCursor themselves.
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.update()
        self.show()

    def _should_show_quota_bar(self) -> bool:
        """True when the latest data's 5h % crossed the warning
        threshold. Below the threshold the indicator is hidden —
        a green-only "you're at 12 %" reading would be noise."""
        if self._data.quota_pct is None:
            return False
        return self._data.quota_pct >= _QUOTA_WARN_THRESHOLD

    # ------------------------------------------------------------------
    # Sole entry point — render(snap)
    # ------------------------------------------------------------------

    def compute(self, snap: WorldSnapshot) -> CapsuleData:
        """Project a ``WorldSnapshot`` into the capsule's view-model.

        Reads ONLY the snapshot fields the capsule actually displays:
        session count, names of running sessions, today cost, quota
        percentage. Everything else (last_activity, is_high_cost,
        latest_model, status_word, ...) is intentionally ignored —
        when those fields change without affecting the four above,
        ``distinct_until_changed`` on the returned tuple correctly
        skips the no-op render.

        Quantisation:
          * cost: passes through ``_fmt_money`` so micro-ticks within
            a price band don't change the dedup key.
          * quota: truncated to ``int`` so sub-percent wobble doesn't
            change the dedup key.

        This is what makes F4's per-surface dedup work — the function
        body itself declares the capsule's interface to the snapshot.
        """
        flat = tuple(v for g in snap.session_groups for v in g.views)
        running_names = tuple(v.name for v in flat if v.is_running)
        cost_str = (
            _fmt_money(snap.today_cost_usd) if snap.today_cost_usd > 0 else ""
        )
        quota_pct = (
            int(snap.quota.five_hour_pct) if snap.quota is not None else None
        )
        # v4c: awaiting consent count + most-prominent active name.
        # awaiting_count drives the badge ("[1 awaiting]") on the
        # capsule's right cluster.  active_name is the first name in
        # priority order: waiting > running > any.  Carried so the
        # capsule headline reads "vibe-ipad · awaiting consent" instead
        # of the generic "3 sessions".
        awaiting = len(snap.pending_decisions or ())
        # Active name + phase resolution.
        #   1. Any session whose phase says "waiting_approval"
        #   2. First running session (highest-priority view by compose)
        #   3. First flat name (idle / ended snapshots)
        # The phase tracks the same priority chain so the pill's wave
        # tints match the same row's wave in the expanded panel.
        active = ""
        active_phase = ""
        if snap.pending_decisions:
            # Use the session name carried by the first pending decision
            # so the capsule names the same session the user will see
            # in the awaiting-consent row.
            active = snap.pending_decisions[0].session_name
            # Find that session in flat to read its phase — pending
            # implies WAITING_APPROVAL but we trust the resolved phase.
            for v in flat:
                if v.name == active:
                    active_phase = getattr(v.phase, "value", "")
                    break
        if not active and running_names:
            active = running_names[0]
            # First running session — pull its phase from the same flat
            # view used for the name (matches `is_running` filter above).
            for v in flat:
                if v.is_running and v.name == active:
                    active_phase = getattr(v.phase, "value", "")
                    break
        if not active and flat:
            active = flat[0].name
            active_phase = getattr(flat[0].phase, "value", "")
        return CapsuleData(
            flat_count=len(flat),
            running_names=running_names,
            cost_str=cost_str,
            quota_pct=quota_pct,
            awaiting_count=awaiting,
            active_name=active,
            active_phase=active_phase,
        )

    def render(self, data: CapsuleData) -> None:
        """Render the capsule from a pre-computed ``CapsuleData``.

        Pure widget mutation — no policy. Subscribes to the world
        observable through ``ops.map(compute) → distinct_until_changed
        → render``, so this is only called when the data tuple
        actually changed (per F4)."""
        if self._hidden_by_user:
            return

        self._data = data
        # Sync the multi-running carousel with the new running set.
        # Must happen before _apply_capsule so the label text and
        # timer state reflect the same data.
        self._update_rotation_state()

        if self._is_dot:
            return

        # Capsule mode: re-apply (label + width + active state).
        self._apply_capsule()

    def _apply_capsule(self) -> None:
        """Lay out + show the capsule pill for the current snapshot.

        Three jobs:
          - lay out three regions: [dot] [name (elided)] [cost (slot)]
          - resize the pill (wider when the quota mini-bar is shown)
          - sync the equalizer-glyph running state

        Layout algebra (no quota):

            ┌──────────────────────────────────────────────────────┐
            │ [dot] [        name (elided middle)        ] [cost ] │
            └──────────────────────────────────────────────────────┘
              12px        flex                    8px gap   56px

        When ``cost_str`` is empty, the cost slot collapses and its
        space is donated back to the name region. When the quota
        mini-bar is showing, an extra trailing slot is reserved on
        the right of cost for the bar + percent caption.

        Idle exception: when the capsule is collapsed at a non-top
        edge (idle state), skip the geometry reset so a render tick
        during idle doesn't bounce the capsule back to full width.
        Label / dot updates still happen — the user will see them
        next time they hover-out."""
        self._is_dot = False
        showing_quota = self._should_show_quota_bar()
        cost_text = self._compose_cost_for_label()
        self._cost_label.setText(cost_text)

        if self._is_idle:
            # Idle layout is owned by _enter_idle; only refresh the
            # active state (dot colour) and skip the geometry pass.
            # Tooltip + name still want to be in sync for when the
            # user hovers out and the pill expands.
            self._label.setText(self._compose_name_for_label(width_px=0))
            self._refresh_active_state()
            self.update()
            self.show()
            return

        target_w = _CAPSULE_W_WITH_QUOTA if showing_quota else _CAPSULE_W
        self._center_top(target_w, _CAPSULE_H)
        self._dot_label.setGeometry(_DOT_LEFT_PAD, 0, _DOT_LABEL_W, _CAPSULE_H)

        quota_slot_w = (
            _QUOTA_RIGHT_PAD + _QUOTA_BAR_W + 36 if showing_quota else 0
        )
        # Right-edge anchor for the cost slot. Without quota, leave a
        # symmetric ``_DOT_LEFT_PAD`` so the pill looks balanced; with
        # quota, the bar + caption already occupy that space.
        right_edge_pad = _DOT_LEFT_PAD if not showing_quota else 0
        cost_slot_w = _COST_SLOT_W if cost_text else 0
        cost_slot_x = target_w - quota_slot_w - right_edge_pad - cost_slot_w
        self._cost_label.setGeometry(
            cost_slot_x, 0, cost_slot_w, _CAPSULE_H,
        )

        # Name region runs from text-left up to the cost slot, with
        # an 8 px gap so the two reads as separate columns. When the
        # cost slot is collapsed (cost == 0) the name takes the full
        # width back.
        name_right = cost_slot_x - (_NAME_COST_GAP if cost_slot_w else 0)
        name_w = max(0, name_right - _TEXT_LEFT)
        self._label.setGeometry(_TEXT_LEFT, 0, name_w, _CAPSULE_H)
        # Elide the name AGAINST the actual region width — QLabel's
        # built-in eliding is only end-elide and we want middle so
        # both prefix ("Sync ...") and suffix ("master branch") of
        # commit-message-style names stay recognisable.
        self._label.setText(self._compose_name_for_label(width_px=name_w))

        self._dot_label.show()
        self._label.show()
        if cost_text:
            self._cost_label.show()
        else:
            self._cost_label.hide()
        self._refresh_active_state()
        self.setToolTip(self._compose_tooltip())
        self.update()
        self.show()

    def _compose_name_for_label(self, *, width_px: int) -> str:
        """Compose the name-region text for the current snapshot,
        elided to fit ``width_px`` using ``Qt.ElideMiddle``.

        Three modes (same as before; only the cost suffix moved out):
          * 0 running ⇒ "N sessions" count form. Always fits, never
            elided in practice; passing ``width_px=0`` skips the elide
            pass entirely (used by the idle path that doesn't have
            a meaningful render width yet).
          * 1 running ⇒ that session's name.
          * ≥2 running ⇒ the carousel-current name.

        Middle-elide preserves both the head and tail of long names.
        For commit-message-style strings the head carries the verb
        ("Sync ...") and the tail carries the object ("... master
        branch") — both are useful identifiers; an end-elide would
        drop the tail entirely and leave the user with a generic
        prefix. ``QFontMetrics.elidedText`` returns the original
        string unchanged when it already fits, so short names pay
        no overhead and tests asserting full names in 1-running
        scenarios keep passing."""
        # v4c: when a decision is awaiting consent, the capsule headline
        # names that session so the user sees "vibe-ipad · awaiting
        # consent · 6 active" rather than the generic "6 sessions" /
        # carousel rotation.  Falls back to the v3 behaviour when no
        # decisions are pending.
        if self._data.awaiting_count > 0 and self._data.active_name:
            n = self._data.awaiting_count
            suffix = "awaiting consent"
            count_str = "" if self._data.flat_count == 0 else (
                f" · {self._data.flat_count} active"
            )
            full = f"{self._data.active_name} · {suffix}{count_str}"
            if n > 1:
                # Surface the multi-pending case in the headline so the
                # user reads "3 awaiting" without having to expand.
                full = (
                    f"{self._data.active_name} · {n} awaiting consent"
                    f"{count_str}"
                )
        elif self._rotation_names:
            idx = self._rotation_index % len(self._rotation_names)
            full = self._rotation_names[idx]
        else:
            count = self._data.flat_count
            noun = "session" if count == 1 else "sessions"
            full = f"{count} {noun}"
        if width_px <= 0:
            return full
        from PySide6.QtGui import QFontMetrics
        fm = QFontMetrics(self._label.font())
        return fm.elidedText(full, Qt.TextElideMode.ElideMiddle, width_px)

    def _compose_cost_for_label(self) -> str:
        """Cost slot text — empty string suppresses the slot entirely
        in _apply_capsule. ``compute()`` already returns "" when
        today's cost is ≤ 0, so no additional check is needed."""
        return self._data.cost_str

    def _compose_tooltip(self) -> str:
        """Multi-line tooltip with the FULL session name(s) + cost.

        This is the "tooltip on truncation" pattern the macOS
        NSStatusItem docs and PatternFly / Carbon UX guides
        independently recommend: when space forces ellipsis, give
        users a hover-revealed channel to recover the dropped info.

        Carousel users also benefit — the full list of running
        sessions appears at once instead of waiting for the rotation
        to land on each name.

        Tooltip body:
          * 0 running: ``"N sessions  ·  $X.YZ today"`` (identical
            to the visible label since both fit)
          * 1 running: ``"<full name>\\n$X.YZ today"`` (full name
            unredacted by elide)
          * ≥2 running: ``"Running:\\n  • <name1>\\n  • <name2>...\\n
            $X.YZ today"`` (all names listed)
        """
        cost_line = (
            f"{self._data.cost_str} today" if self._data.cost_str else ""
        )
        names = self._data.running_names
        if not names:
            count = self._data.flat_count
            noun = "session" if count == 1 else "sessions"
            return (
                f"{count} {noun}\n{cost_line}" if cost_line
                else f"{count} {noun}"
            )
        if len(names) == 1:
            return f"{names[0]}\n{cost_line}" if cost_line else names[0]
        joined = "\n".join(f"  • {n}" for n in names)
        body = f"Running:\n{joined}"
        return f"{body}\n{cost_line}" if cost_line else body

    def _on_rotate_tick(self) -> None:
        """Carousel timer hook: advance to the next running name and
        repaint the label. No-op when the rotation list isn't worth
        rotating (length ≤ 1).

        Re-elides against the current name region width — the cost
        slot stays put across rotations, so re-running the elide pass
        is the only thing that needs to happen."""
        if len(self._rotation_names) <= 1:
            return
        self._rotation_index = (self._rotation_index + 1) % len(self._rotation_names)
        if not self._is_dot:
            self._label.setText(
                self._compose_name_for_label(width_px=self._label.width()),
            )
            # Tooltip lists ALL names so its content doesn't change
            # across carousel rotations — but if the user hovered
            # away and back, refreshing here keeps the visible name
            # and the tooltip's first-listed name visually aligned.
            self.setToolTip(self._compose_tooltip())

    def _update_rotation_state(self) -> None:
        """Sync ``_rotation_names`` with the current running sessions
        and start / stop the timer accordingly.

        Index reset rule: only reset to 0 when the *set* of running
        names changes. Without this, every render tick would jerk the
        carousel back to position 0 and make the pill text flash.
        The dedup pipeline already filters spurious re-renders, so
        any render that reaches us is a real change worth reacting to."""
        new_names = list(self._data.running_names)
        if new_names != self._rotation_names:
            self._rotation_names = new_names
            self._rotation_index = 0

        if len(self._rotation_names) >= 2:
            if not self._rotation_timer.isActive():
                self._rotation_timer.start()
        else:
            if self._rotation_timer.isActive():
                self._rotation_timer.stop()

    def _refresh_active_state(self) -> None:
        """Sync the equalizer-glyph state with whether any session is
        running (per the latest data). Idempotent — set_state is a
        no-op when the target state matches.

        v4c: bar colour follows the active session's phase via
        ``Color.for_phase`` so the capsule wave matches the matching
        row's wave in the expanded list (review thinking → purple
        wave in both surfaces).
        """
        has_active = bool(self._data.running_names)
        if has_active:
            self._dot_label.set_state(
                _RowStatusGlyph.STATE_RUNNING,
                bar_color=self._phase_wave_color(),
            )
            self._is_breathing = True
        else:
            self._dot_label.set_state(
                _RowStatusGlyph.STATE_IDLE,
                dot_color=_DOT_IDLE_COLOR,
            )
            self._is_breathing = False

    def _phase_wave_color(self) -> str:
        """Resolve the equalizer-bar colour from the active session's
        phase.  Falls back to the green phosphor token (v3's old
        hardcoded value) when phase is empty / unrecognised."""
        from claude_island.core.session_phase import SessionPhase
        phase_val = self._data.active_phase
        if not phase_val:
            return _DOT_RUNNING_COLOR
        try:
            phase = SessionPhase(phase_val)
        except ValueError:
            return _DOT_RUNNING_COLOR
        return _C.for_phase(phase)

    # ------------------------------------------------------------------
    # Paint + events
    # ------------------------------------------------------------------

    def paintEvent(self, event: object) -> None:  # type: ignore[override]
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        path = QPainterPath()
        r = self.height() / 2

        # Background colour reflects quota severity. Dispatched on
        # ``quota_severity()`` from the shared core palette so this
        # surface and the panel's TODAY card always agree on what
        # band a given pct lands in. Below the warn band (and in
        # dot mode) we use the standard dark grey so normal operation
        # looks unobtrusive.
        pct = self._data.quota_pct
        if self._is_dot:
            color = _DOT_COLOR
        elif pct is not None:
            color = _BG_BY_SEVERITY[_quota_severity(pct)]
        else:
            color = _BG_COLOR

        path.addRoundedRect(0, 0, self.width(), self.height(), r, r)
        painter.fillPath(path, color)

        # v3 dot rim — 1 px stroke around the dot so it reads as a
        # stamp on the desk rather than a flat blob.  Urgent variant
        # (red_warm) is wired up but not currently emitted — the
        # CapsuleData view-model doesn't carry pending-decision count
        # yet; that's a separate slice once the field lands upstream.
        if self._is_dot:
            from PySide6.QtCore import QRectF
            from PySide6.QtGui import QPen
            pen = QPen(_DOT_RIM_COLOR)
            pen.setWidth(1)
            pen.setCosmetic(True)
            painter.setPen(pen)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            # inset by half a pixel so the stroke renders inside the
            # widget rect (the AA path otherwise gets clipped on one side)
            painter.drawRoundedRect(
                QRectF(0.5, 0.5, self.width() - 1, self.height() - 1),
                r - 0.5, r - 0.5,
            )

        # Mini quota bar — drawn directly on the pill (no QWidget
        # overhead) for two reasons: it shares lifecycle with the pill
        # background, and a child widget here would have to fight the
        # frameless / translucent setup for layering. Skipped entirely
        # when below threshold or the cache has never been populated.
        if not self._is_dot and self._should_show_quota_bar():
            self._paint_quota_bar(painter)

    def _paint_quota_bar(self, painter: QPainter) -> None:
        """Draw the right-side mini quota progress + "78%" caption."""
        pct = max(0, min(100, self._data.quota_pct or 0))
        critical = pct >= _QUOTA_CRITICAL_THRESHOLD

        # Layout: [bar] gap [pct text]  flush right against _QUOTA_RIGHT_PAD.
        bar_x = self.width() - _QUOTA_RIGHT_PAD - 36 - _QUOTA_BAR_W
        bar_y = (self.height() - _QUOTA_BAR_H) // 2

        # Track
        track_path = QPainterPath()
        track_path.addRoundedRect(
            bar_x, bar_y, _QUOTA_BAR_W, _QUOTA_BAR_H,
            _QUOTA_BAR_H / 2, _QUOTA_BAR_H / 2,
        )
        painter.fillPath(track_path, _QUOTA_BAR_TRACK)

        # Fill — width = pct * bar_w / 100. Colour is resolved through
        # the shared core palette so the panel's TODAY card and this
        # mini-bar always agree on what colour a given pct should be.
        fill_w = max(1, int(_QUOTA_BAR_W * pct / 100))
        fill_path = QPainterPath()
        fill_path.addRoundedRect(
            bar_x, bar_y, fill_w, _QUOTA_BAR_H,
            _QUOTA_BAR_H / 2, _QUOTA_BAR_H / 2,
        )
        painter.fillPath(fill_path, QColor(_quota_bar_color(pct)))

        # "78%" caption to the right of the bar. Native QPainter draw
        # so we don't have to manage another QLabel + opacity effect.
        from PySide6.QtGui import QFont
        text = f"{int(pct)}%"
        font = QFont(UI_FONT_FAMILY, 9, QFont.Weight.DemiBold)
        painter.setFont(font)
        text_color = (
            QColor(254, 226, 226) if critical else QColor(253, 230, 138)
        )
        painter.setPen(text_color)
        text_x = bar_x + _QUOTA_BAR_W + 4
        text_y = self.height() // 2 + 4  # rough vertical centre for QFont
        painter.drawText(text_x, text_y, text)

    def mousePressEvent(self, event) -> None:  # type: ignore[override]
        if event.button() == Qt.MouseButton.RightButton:
            self._show_context_menu(event.globalPosition().toPoint())
            return
        if event.button() == Qt.MouseButton.LeftButton:
            # Capture drag origin but DON'T toggle yet — we don't
            # know if this is a click, a horizontal drag, or a free
            # drag until mouseRelease.
            self._drag_origin_global = event.globalPosition().toPoint()
            self._drag_origin_window = self.pos()
            self._is_dragging = False
            self._is_free_drag = False
            # Start the long-press race: if the user holds without
            # moving for _LONG_PRESS_MS, we promote to free-drag.
            # If they move past startDragDistance first, the timer
            # is cancelled in mouseMoveEvent and we go horizontal.
            self._long_press_timer.start()

    def mouseMoveEvent(self, event) -> None:  # type: ignore[override]
        # No left-button press recorded ⇒ ignore (e.g. mouse wandered
        # over the pill without pressing). Qt only delivers move
        # events when a button is held unless setMouseTracking is on,
        # so this is mostly defensive.
        if self._drag_origin_global is None or self._drag_origin_window is None:
            return
        delta = event.globalPosition().toPoint() - self._drag_origin_global
        if not self._is_dragging:
            # Tolerance: system-defined click slop. Below this we
            # still treat the gesture as a click; above it we commit
            # to a drag and stop bubbling toggle on release.
            if delta.manhattanLength() < QApplication.startDragDistance():
                return
            self._is_dragging = True
            # User moved before the long-press fired ⇒ horizontal
            # drag mode. Cancel the timer so it doesn't promote
            # mid-drag (which would jarringly change Y axis lock
            # behaviour).
            if not self._is_free_drag:
                self._long_press_timer.stop()
        if self._is_free_drag:
            # Free drag: both axes follow the cursor.
            new_x = self._clamp_x(
                self._drag_origin_window.x() + delta.x(), self.width(),
            )
            new_y = self._drag_origin_window.y() + delta.y()
            self.move(new_x, new_y)
        else:
            # Horizontal drag: Y tracks each screen's top. Crossing
            # into a different monitor whose geom.top() isn't equal
            # to the drag-origin's screen recomputes Y so the capsule
            # lands on THAT monitor's top edge — without this, the
            # capsule floats mid-air on a taller / vertically-offset
            # secondary display.
            new_x = self._clamp_x(
                self._drag_origin_window.x() + delta.x(), self.width(),
            )
            new_y = self._top_y_for_x(new_x + self.width() // 2)
            self.move(new_x, new_y)

    def mouseReleaseEvent(self, event) -> None:  # type: ignore[override]
        if event.button() != Qt.MouseButton.LeftButton:
            return
        was_dragging = self._is_dragging
        was_free_drag = self._is_free_drag
        # Reset drag state BEFORE branching so a re-entrant signal
        # (e.g. toggle_expanded routes back here) sees a clean state.
        self._drag_origin_global = None
        self._drag_origin_window = None
        self._is_dragging = False
        self._is_free_drag = False
        # Always cancel the long-press timer on release — without
        # this a quick press-release would still promote to free-
        # drag _LONG_PRESS_MS later.
        self._long_press_timer.stop()
        if not was_dragging:
            # Pure click — original behaviour.
            self._controller.toggle_expanded()
            return
        if was_free_drag:
            # Restore opacity from the free-drag visual cue, then
            # snap to the nearest screen edge with a spring animation.
            # Position persistence happens after the animation finishes
            # so we save the snapped target, not the release-point.
            self.setWindowOpacity(1.0)
            self._snap_to_nearest_edge()
            return
        # Horizontal drag completed — persist immediately (no animation).
        pos = self.pos()
        self._persisted_pos = (pos.x(), pos.y())
        _save_position(pos.x(), pos.y())

    # ── Long-press → free drag promotion ────────────────────────────

    def _on_long_press(self) -> None:
        """Fired by ``_long_press_timer`` after _LONG_PRESS_MS of held-
        still mouse-down. Promotes the active press into "free drag
        mode" so subsequent mouseMove events are 2D rather than X-only,
        and applies a visual cue (opacity drop) so the user knows the
        mode flipped without releasing first.

        No-op if the press was already released or already promoted to
        a horizontal drag — both branches stop the timer to avoid
        firing here in stale state."""
        if self._drag_origin_global is None:
            return
        if self._is_dragging and not self._is_free_drag:
            # Already committed to horizontal-drag — too late to
            # promote (would feel like the rules changed mid-gesture).
            return
        self._is_free_drag = True
        self._is_dragging = True  # locks out the click-on-release path
        self.setWindowOpacity(_FREE_DRAG_OPACITY)

    # ── Edge snap ───────────────────────────────────────────────────

    def _snap_to_nearest_edge(self) -> None:
        """Animate to the nearest of the 4 screen edges of the screen
        the capsule currently lives on.

        Center-of-capsule is compared against each edge; the smallest
        distance wins. Anchor points are:
          top    → centre's X, top + _TOP_MARGIN
          bottom → centre's X, bottom - height - _TOP_MARGIN
          left   → left + _TOP_MARGIN, current Y
          right  → right - width - _TOP_MARGIN, current Y

        Multi-monitor: ``QApplication.screenAt(self.pos())`` resolves
        which screen the capsule is on, so dragging to a second
        monitor and releasing snaps to THAT monitor's edges, not the
        primary's. Falls back to the primary screen if pos() lands in
        a desktop gap (rare on real hardware).

        Side effect: sets ``self._docked_edge`` to the chosen edge so
        the post-animation hook (``_on_snap_finished``) knows whether
        to collapse to idle (non-top edges) or stay full-presence
        (top edge = home)."""
        screen = QApplication.screenAt(self.pos())
        if screen is None:
            screen = QApplication.primaryScreen()
        geom = screen.geometry()
        cx = self.x() + self.width() // 2
        cy = self.y() + self.height() // 2
        dist_top = cy - geom.top()
        dist_bottom = geom.bottom() - cy
        dist_left = cx - geom.left()
        dist_right = geom.right() - cx
        nearest = min(dist_top, dist_bottom, dist_left, dist_right)
        if nearest == dist_top:
            target = QPoint(self.x(), geom.top() + _TOP_MARGIN)
            self._docked_edge = "top"
        elif nearest == dist_bottom:
            target = QPoint(
                self.x(), geom.bottom() - self.height() - _TOP_MARGIN,
            )
            self._docked_edge = "bottom"
        elif nearest == dist_left:
            target = QPoint(geom.left() + _TOP_MARGIN, self.y())
            self._docked_edge = "left"
        else:
            target = QPoint(
                geom.right() - self.width() - _TOP_MARGIN, self.y(),
            )
            self._docked_edge = "right"
        # Final clamp so the snap target itself is within bounds (an
        # off-screen cy could otherwise pick a target right at the
        # screen edge that bleeds onto the next monitor).
        target.setX(self._clamp_x(target.x(), self.width()))
        self._animate_to(target)

    def _animate_to(self, target: QPoint) -> None:
        """Spring-bounce the capsule from current pos to ``target``.

        ``OutBack`` easing reproduces the Chat Heads / iOS spring
        feel — overshoots slightly then settles. Held in
        ``self._snap_anim`` so Python's GC doesn't reap the object
        mid-flight (which would silently kill the animation and jump
        to the end position with no transition).

        Position persistence fires from the ``finished`` signal so
        we save the actual landing point (not the release-point)."""
        anim = QPropertyAnimation(self, b"pos", self)
        anim.setDuration(_SNAP_DURATION_MS)
        anim.setStartValue(self.pos())
        anim.setEndValue(target)
        anim.setEasingCurve(QEasingCurve.Type.OutBack)
        anim.finished.connect(self._on_snap_finished)
        self._snap_anim = anim
        anim.start()

    def _on_snap_finished(self) -> None:
        """Persist the post-snap position so the next launch restores
        the snapped (edge-anchored) coordinates rather than the user's
        release point in the middle of the screen.

        If the snap target is a non-top edge, also collapse the
        capsule to its idle half-hidden form so it stops competing
        with the user's working area."""
        pos = self.pos()
        self._persisted_pos = (pos.x(), pos.y())
        _save_position(pos.x(), pos.y())
        self._snap_anim = None
        if self._docked_edge in _IDLE_EDGES:
            self._enter_idle()

    # ── Edge idle ────────────────────────────────────────────────────

    def _enter_idle(self) -> None:
        """Collapse the capsule to its idle half-hidden form: thin
        strip + reduced opacity. Hides the text label so just the
        status glyph and pill outline remain visible.

        No-op if already idle (idempotent — entered automatically on
        snap-finish AND on every leaveEvent, so the guard prevents
        double-shrink artefacts)."""
        if self._is_idle or self._is_dot:
            return
        self._is_idle = True
        # Width shrinks; height stays the same so the dot stays
        # vertically centred and the docked-edge anchor is preserved.
        # Position is NOT changed — _snap_to_nearest_edge already
        # placed the capsule's full-width frame at the edge anchor;
        # shrinking from the left edge keeps the visual anchor on
        # whichever side the user docked at (left edge: shrinks
        # rightwards, right edge: still anchored to right because
        # we recompute x for that side).
        new_x = self.x()
        if self._docked_edge == "right":
            # Right-edge dock: keep the right edge of the capsule
            # anchored, so shrinking the width pulls the left edge
            # rightwards (towards the visible edge).
            new_x = self.x() + (self.width() - _IDLE_W)
        self._set_target_geometry(new_x, self.y(), _IDLE_W, _CAPSULE_H)
        self.setWindowOpacity(_IDLE_OPACITY)
        self._label.hide()
        self._cost_label.hide()

    def _exit_idle(self) -> None:
        """Restore the capsule from idle to its full-width form on
        hover. _apply_capsule decides the actual width based on
        snapshot state (with/without quota mini-bar)."""
        if not self._is_idle or self._is_dot:
            return
        self._is_idle = False
        self.setWindowOpacity(1.0)
        self._label.show()
        # _apply_capsule below decides cost_label visibility based on
        # whether the snapshot's cost is non-zero, so don't unilaterally
        # show it here — let the layout pass be authoritative.
        # Re-run the layout pass so the capsule expands back to its
        # snapshot-driven width. _apply_capsule reads _is_idle (False
        # now) and picks the normal _CAPSULE_W / _CAPSULE_W_WITH_QUOTA.
        # Adjust position so a right-edge expansion grows leftwards
        # (keeps the visible edge anchored) instead of overflowing
        # off-screen.
        if self._docked_edge == "right":
            target_w = _CAPSULE_W_WITH_QUOTA if self._should_show_quota_bar() else _CAPSULE_W
            new_x = self.x() - (target_w - _IDLE_W)
            self.move(new_x, self.y())
        self._apply_capsule()

    def enterEvent(self, event) -> None:  # type: ignore[override]
        """Mouse entered the capsule's hit-area.

        Three behaviours, picked by current state:
          * dot   → grow to the hover size so the click target reads
            as "tappable" (resting size is intentionally quiet).
          * docked idle (capsule, off-top edge) → restore full size
            so the user can read the label.
          * normal capsule on top edge → no-op.
        """
        if self._is_dot:
            self._apply_dot(hover=True)
        elif self._is_idle:
            self._exit_idle()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:  # type: ignore[override]
        """Mouse left the capsule.

        Mirrors enterEvent: shrink the dot back to resting; or
        re-enter docked-idle for non-top edges. Drag-in-progress is
        protected against in the idle path."""
        if self._is_dot:
            self._apply_dot(hover=False)
        elif (
            self._docked_edge in _IDLE_EDGES
            and not self._is_idle
            and self._drag_origin_global is None
        ):
            self._enter_idle()
        super().leaveEvent(event)

    def _go_home(self) -> None:
        """Reset to the default top-centre position. Clears the
        docked-edge state, the persisted position, and the idle
        flag — capsule returns to the same place a fresh first-run
        install would put it.

        Triggered from the right-click menu's "Reset position"
        item. Keeps the persisted file but with the centred
        coordinates so the next launch also lands centred (rather
        than restoring whatever weird corner the user dragged to
        before resetting)."""
        self._docked_edge = None
        self._is_idle = False
        self._persisted_pos = None
        self.setWindowOpacity(1.0)
        self._label.show()
        # cost_label visibility decided by _apply_capsule below.
        # Re-apply the capsule layout: reads _persisted_pos (now
        # None) and falls back to primary-screen-top-centre.
        if self._is_dot:
            self._apply_dot()
        else:
            self._apply_capsule()

    def _show_context_menu(self, global_pos: QPoint) -> None:
        menu = QMenu(self)
        menu.setStyleSheet(_STYLE_MENU)
        # "Reset position" is only useful after the user has dragged
        # the capsule away from the home (top-centre) position. The
        # action is always shown for discoverability — clicking when
        # already at home is a harmless re-application of the centred
        # geometry.
        menu.addAction("Reset position", self._go_home)
        menu.addAction("Hide until restart", self._hide_until_restart)
        menu.addSeparator()
        menu.addAction("Quit ClaudeIsland", QApplication.instance().quit)
        menu.exec(global_pos)

    def _hide_until_restart(self) -> None:
        self._hidden_by_user = True
        # Collapse first so the expanded panel also disappears via its own
        # state_changed handler.
        if self._controller.state == "expanded":
            self._controller.toggle_expanded()
        self.hide()
