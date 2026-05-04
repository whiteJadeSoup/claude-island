from __future__ import annotations

from datetime import datetime, timezone
from typing import Callable, NamedTuple

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

from claude_island.core.models import QuotaSnapshot, Session, SessionDetails
from claude_island.core.snapshot import SessionView, WorldSnapshot
from .controller import IslandController
from .expanded_window import _RowStatusGlyph
from .window_position import load_position as _load_saved_position
from .window_position import save_position as _save_position

# Two widths: normal (no quota mini-bar) vs warning (quota mini-bar
# appended on the right). Switching between them happens on every
# refresh that crosses the warn threshold; the resize is unobtrusive
# at this scale and re-centring keeps the pill anchored to the top.
_CAPSULE_W = 200
_CAPSULE_W_WITH_QUOTA = 290
_CAPSULE_H = 36
_DOT_W = 12
_DOT_H = 12
_TOP_MARGIN = 8

# PR2 — long-press unlock + edge-snap
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

# PR3 — edge-idle half-hide
# When the capsule is docked at a non-top edge (bottom/left/right),
# it shrinks to a thin strip and fades so the user's working area
# stays uncluttered. Hover restores it. Mirrors AssistiveTouch idle
# opacity (40%) tuned a bit brighter for desktop where the user's
# eye is further from the screen.
_IDLE_W = 60         # narrower than _DOT_LEFT_PAD + dot + label slot
_IDLE_OPACITY = 0.6  # faint enough to disappear, visible enough to find
# Edges that trigger half-hide. Top is always full-presence (it's
# the home position).
_IDLE_EDGES = ("bottom", "left", "right")

_DOT_LEFT_PAD = 12  # px from pill's left edge to dot's left edge
_DOT_LABEL_W = 14   # px reserved for the "●" glyph
_TEXT_LEFT = _DOT_LEFT_PAD + _DOT_LABEL_W + 4  # 4px gap between dot and text

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
_QUOTA_WARN_THRESHOLD = 70
_QUOTA_CRITICAL_THRESHOLD = 90

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

_STYLE_LABEL = "color: white; font-size: 12px; font-family: 'Segoe UI', sans-serif;"
# Active = green equalizer bars; idle = static grey dot. Same colour
# mapping the panel rows use, just rendered through _RowStatusGlyph
# instead of a styled QLabel.
_DOT_RUNNING_COLOR = "#22c55e"
_DOT_IDLE_COLOR = "#6b7280"
_BG_COLOR = QColor(18, 18, 18, 230)
# Capsule background swap when quota crosses the critical threshold —
# amber that reads "warning, not failure" against the dark text. The
# RGB matches the row chip's amber so the warning vocabulary stays
# consistent across panel + pill.
_BG_COLOR_WARN = QColor(120, 53, 15, 230)
_BG_COLOR_CRITICAL = QColor(127, 29, 29, 230)
_DOT_COLOR = QColor(80, 80, 80, 200)
# Mini quota bar pen / brush colours. Threshold-driven, mirroring the
# summary card's progress bar palette so the user sees the same colour
# story in both places.
_QUOTA_BAR_TRACK = QColor(255, 255, 255, 40)
_QUOTA_BAR_FILL_WARN = QColor(250, 204, 21)     # amber
_QUOTA_BAR_FILL_CRITICAL = QColor(248, 113, 113)  # bright red
_STYLE_QUOTA_PCT = "color: #fde68a; font-size: 11px; font-weight: 600;"
_STYLE_QUOTA_PCT_CRITICAL = "color: #fee2e2; font-size: 11px; font-weight: 600;"


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

_STYLE_MENU = """
    QMenu {
        background: #1e1e1e;
        color: #e0e0e0;
        border: 1px solid #333;
        padding: 4px;
        font-size: 12px;
    }
    QMenu::item { padding: 6px 18px; border-radius: 4px; }
    QMenu::item:selected { background: #2e2e2e; }
    QMenu::separator { height: 1px; background: #333; margin: 4px 6px; }
"""


class CapsuleWindow(QWidget):
    """Frameless, always-on-top pill anchored to the top-centre of the screen.

    Clicking toggles the expanded panel via the controller.
    Resizes to a small dot when there are no active sessions.
    """

    def __init__(
        self,
        controller: IslandController,
        *,
        get_today_cost: Callable[[], float] | None = None,
        get_session_details: Callable[[Session], SessionDetails | None] | None = None,
        get_quota_snapshot: Callable[[], QuotaSnapshot | None] | None = None,
    ) -> None:
        super().__init__()
        self._controller = controller
        self._is_dot = True
        # Once the user picks "Hide" from the right-click menu the capsule
        # stays gone until the next process restart — there is no tray icon
        # to bring it back, so all auto-show paths must respect this flag.
        self._hidden_by_user = False
        # Constructor params kept for backwards compat with the wiring
        # layer's call signature; no longer read internally — the
        # capsule's data path is render(snap) only after Phase G2.2.
        # Will be removed once the wiring layer drops them too.
        self._get_today_cost = get_today_cost
        self._get_session_details = get_session_details
        self._get_quota_snapshot = get_quota_snapshot
        # Latest computed view-model. Populated by render(data); read
        # by _compose_label_text, _refresh_active_state, _paint_quota_bar.
        # Empty default so any path called before the first render
        # doesn't AttributeError.
        self._data: CapsuleData = CapsuleData(
            flat_count=0, running_names=(), cost_str="", quota_pct=None,
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
        # NOTE: previously self._cost_cache (float) and
        # self._quota_pct_cache (float | None) lived here as separate
        # paint caches. They have been folded into self._data.cost_str
        # and self._data.quota_pct (string + int, quantised to display
        # precision) so dedup keys and paint reads share one source.

        # ── Drag state (PR1: horizontal drag along the top) ────────────
        # User can press-and-hold the pill, then drag it left/right to
        # reposition it horizontally along the top edge. The X coord
        # is persisted so the position survives restarts. Y is locked
        # to top margin in PR1; PR2 will unlock Y via long-press.
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

        # ── Free-drag state (PR2: long-press to unlock 2D drag + edge snap) ──
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
        # the timer fires, we fall through to the PR1 horizontal-drag
        # mode (Y locked) — fast small adjustments shouldn't have to
        # wait 0.5 s. The timer is cancelled in that branch.
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

        # ── Docked-edge idle state (PR3: hide-on-non-top-edge + hover) ──
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
        # static dot. Running state previously was the dot's opacity
        # pulse — same visual story, more obvious indicator.
        self._dot_label = _RowStatusGlyph(self)

        self._label = QLabel("", self)
        self._label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._label.setStyleSheet(_STYLE_LABEL)

        # is_breathing is the legacy attribute name for "is the
        # equalizer currently animating" — kept so existing tests /
        # callers reading capsule._is_breathing keep working.
        self._is_breathing = False

        controller.state_changed.connect(self._on_state_changed)
        self._apply_dot()

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
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)

    def _center_top(self, w: int, h: int) -> None:
        """Position the capsule for the given (w, h).

        Honours the user's persisted drag position when one exists
        AND lands on a currently-connected screen; otherwise falls
        back to top-centre on the primary screen (the original
        behaviour). Called from _apply_dot / _apply_capsule on every
        size-affecting state change so PR1 must keep the user's
        chosen X across those transitions.

        Multi-monitor: a persisted position from a now-disconnected
        monitor would land off-screen; ``_pos_visible_on_any_screen``
        guards that. Cross-platform (uses Qt screen geometry).
        """
        if self._persisted_pos is not None:
            x, y = self._persisted_pos
            x = self._clamp_x(x, w)
            if _pos_visible_on_any_screen(x, y, w, h):
                self.setGeometry(x, y, w, h)
                return
        # Default — primary-screen top centre, original behaviour.
        screen = QApplication.primaryScreen()
        geom = screen.geometry()
        x = geom.center().x() - w // 2
        self.setGeometry(x, geom.top() + _TOP_MARGIN, w, h)

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

    def _apply_dot(self) -> None:
        self._is_dot = True
        self._center_top(_DOT_W, _DOT_H)
        self._label.hide()
        self._dot_label.hide()
        # Glyph state goes IDLE while in dot mode — equalizer animation
        # would be wasted CPU on an invisible widget.
        self._dot_label.set_state(
            _RowStatusGlyph.STATE_IDLE, dot_color=_DOT_IDLE_COLOR,
        )
        self._is_breathing = False
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
        return CapsuleData(
            flat_count=len(flat),
            running_names=running_names,
            cost_str=cost_str,
            quota_pct=quota_pct,
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
          - set the label text via _compose_label_text
          - resize the pill (wider when the quota mini-bar is shown)
          - sync the equalizer-glyph running state

        Idle exception: when the capsule is collapsed at a non-top
        edge (PR3 idle state), skip the geometry reset so a render
        tick during idle doesn't bounce the capsule back to full
        width. Label / dot updates still happen — the user will see
        them next time they hover-out."""
        self._is_dot = False
        self._label.setText(self._compose_label_text())
        showing_quota = self._should_show_quota_bar()
        if self._is_idle:
            # Idle layout is owned by _enter_idle; only refresh the
            # active state (dot colour) and skip the geometry pass.
            self._refresh_active_state()
            self.update()
            self.show()
            return
        target_w = _CAPSULE_W_WITH_QUOTA if showing_quota else _CAPSULE_W
        self._center_top(target_w, _CAPSULE_H)
        self._dot_label.setGeometry(_DOT_LEFT_PAD, 0, _DOT_LABEL_W, _CAPSULE_H)
        right_pad = (
            (_QUOTA_RIGHT_PAD + _QUOTA_BAR_W + 36)  # 36 px for "78%"
            if showing_quota
            else _DOT_LEFT_PAD + _DOT_LABEL_W      # symmetric blank
        )
        self._label.setGeometry(
            _TEXT_LEFT, 0, target_w - _TEXT_LEFT - right_pad, _CAPSULE_H,
        )
        self._dot_label.show()
        self._label.show()
        self._refresh_active_state()
        self.update()
        self.show()

    def _compose_label_text(self) -> str:
        """Compose pill text from the current data + carousel state.

        Three modes:
          * Exactly 1 running ⇒ show its name (no carousel).
          * ≥2 running ⇒ show the carousel-current name. The rotation
            timer ticks every ``_ROTATE_INTERVAL_MS``.
          * 0 running ⇒ "N sessions" count form. Carousel inactive.

        Cost suffix appended when present (compute already produced
        empty string for cost <= 0, so no extra check needed here)."""
        cost_suffix = f"  {self._data.cost_str}" if self._data.cost_str else ""

        if self._rotation_names:
            # ≥1 running session — render the carousel-current name.
            idx = self._rotation_index % len(self._rotation_names)
            return f"{self._rotation_names[idx]}{cost_suffix}"

        # 0 running — count form. Use total session count.
        count = self._data.flat_count
        noun = "session" if count == 1 else "sessions"
        return f"{count} {noun}{cost_suffix}"

    def _on_rotate_tick(self) -> None:
        """Carousel timer hook: advance to the next running name and
        repaint the label. No-op when the rotation list isn't worth
        rotating (length ≤ 1)."""
        if len(self._rotation_names) <= 1:
            return
        self._rotation_index = (self._rotation_index + 1) % len(self._rotation_names)
        if not self._is_dot:
            self._label.setText(self._compose_label_text())

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
        no-op when the target state matches."""
        has_active = bool(self._data.running_names)
        if has_active:
            self._dot_label.set_state(
                _RowStatusGlyph.STATE_RUNNING,
                bar_color=_DOT_RUNNING_COLOR,
            )
            self._is_breathing = True
        else:
            self._dot_label.set_state(
                _RowStatusGlyph.STATE_IDLE,
                dot_color=_DOT_IDLE_COLOR,
            )
            self._is_breathing = False

    # ------------------------------------------------------------------
    # Paint + events
    # ------------------------------------------------------------------

    def paintEvent(self, event: object) -> None:  # type: ignore[override]
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        path = QPainterPath()
        r = self.height() / 2

        # Background colour reflects quota severity. Critical (≥ 90 %)
        # gets a deep red wash; warning (≥ 70 %) gets amber. Below the
        # warn threshold (and in dot mode) we use the standard dark
        # grey so normal operation looks unobtrusive.
        pct = self._data.quota_pct
        if self._is_dot:
            color = _DOT_COLOR
        elif pct is not None and pct >= _QUOTA_CRITICAL_THRESHOLD:
            color = _BG_COLOR_CRITICAL
        elif pct is not None and pct >= _QUOTA_WARN_THRESHOLD:
            color = _BG_COLOR_WARN
        else:
            color = _BG_COLOR

        path.addRoundedRect(0, 0, self.width(), self.height(), r, r)
        painter.fillPath(path, color)

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

        # Fill — width = pct * bar_w / 100
        fill_w = max(1, int(_QUOTA_BAR_W * pct / 100))
        fill_path = QPainterPath()
        fill_path.addRoundedRect(
            bar_x, bar_y, fill_w, _QUOTA_BAR_H,
            _QUOTA_BAR_H / 2, _QUOTA_BAR_H / 2,
        )
        painter.fillPath(
            fill_path,
            _QUOTA_BAR_FILL_CRITICAL if critical else _QUOTA_BAR_FILL_WARN,
        )

        # "78%" caption to the right of the bar. Native QPainter draw
        # so we don't have to manage another QLabel + opacity effect.
        from PySide6.QtGui import QFont
        text = f"{int(pct)}%"
        font = QFont("Segoe UI", 9, QFont.Weight.DemiBold)
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
            # Horizontal drag (PR1 path): Y tracks each screen's top.
            # Crossing into a different monitor whose geom.top() isn't
            # equal to the drag-origin's screen recomputes Y so the
            # capsule lands on THAT monitor's top edge — without this,
            # the capsule floats mid-air on a taller / vertically-
            # offset secondary display.
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

    # ── Edge idle (PR3) ─────────────────────────────────────────────

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
        self.setGeometry(new_x, self.y(), _IDLE_W, _CAPSULE_H)
        self.setWindowOpacity(_IDLE_OPACITY)
        self._label.hide()

    def _exit_idle(self) -> None:
        """Restore the capsule from idle to its full-width form on
        hover. _apply_capsule decides the actual width based on
        snapshot state (with/without quota mini-bar)."""
        if not self._is_idle or self._is_dot:
            return
        self._is_idle = False
        self.setWindowOpacity(1.0)
        self._label.show()
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
        """Mouse entered the capsule's hit-area. If we're docked at a
        non-top edge in idle form, expand back to full size so the
        user can read the label. Top-edge docks have no idle state
        to exit."""
        if self._is_idle:
            self._exit_idle()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:  # type: ignore[override]
        """Mouse left the capsule. If we're docked at a non-top edge
        and not currently being dragged, collapse back to idle —
        completes the AssistiveTouch-style "fade away when you're
        not touching me" loop."""
        if (
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
