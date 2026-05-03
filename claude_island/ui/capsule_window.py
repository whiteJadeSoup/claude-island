from __future__ import annotations

from datetime import datetime, timezone
from typing import Callable

from PySide6.QtCore import (
    QEasingCurve,
    QPoint,
    QPropertyAnimation,
    Qt,
)
from PySide6.QtGui import QColor, QPainter, QPainterPath
from PySide6.QtWidgets import (
    QApplication,
    QGraphicsOpacityEffect,
    QLabel,
    QMenu,
    QWidget,
)

from claude_island.core.models import Session, SessionDetails
from .controller import IslandController

_CAPSULE_W = 200
_CAPSULE_H = 36
_DOT_W = 12
_DOT_H = 12
_TOP_MARGIN = 8

_DOT_LEFT_PAD = 12  # px from pill's left edge to dot's left edge
_DOT_LABEL_W = 14   # px reserved for the "●" glyph
_TEXT_LEFT = _DOT_LEFT_PAD + _DOT_LABEL_W + 4  # 4px gap between dot and text

# Heuristic for "this session is currently doing something". An active
# Claude Code session writes a JSONL row at least every few seconds
# while the model is streaming or the tool loop is iterating; once the
# user has stopped typing and the model is done, writes pause. 30 s
# gives a comfortable margin for one tool round-trip without flapping
# the breathing animation off mid-burst.
_ACTIVE_THRESHOLD_SECONDS = 30

# Breathing animation parameters. 2.0 s round-trip (1.0 s in, 1.0 s out)
# matches Apple's slow-pulse cadence — perceptible but not distracting.
# Floor 0.55 keeps the dot legible at the dimmest point so it never
# disappears entirely (which would read as "no session" not "live").
_BREATH_PERIOD_MS = 2000
_BREATH_OPACITY_FLOOR = 0.55
_BREATH_OPACITY_PEAK = 1.0

_STYLE_LABEL = "color: white; font-size: 12px; font-family: 'Segoe UI', sans-serif;"
# Dot colours: green when at least one session is active, neutral grey
# when all idle. Matches the in-card status-dot semantics so the user
# learns one mapping ("green = something is happening") globally.
_STYLE_DOT_ACTIVE = "color: #4ade80; font-size: 14px;"
_STYLE_DOT_IDLE = "color: #6b7280; font-size: 14px;"
_BG_COLOR = QColor(18, 18, 18, 230)
_DOT_COLOR = QColor(80, 80, 80, 200)


def _fmt_money(amount: float) -> str:
    """Compact $ formatter — duplicated from expanded_window._fmt_money.
    Keep in sync; extract to a shared util at the 3rd usage."""
    if amount < 0.01:
        return f"${amount:.3f}"
    if amount < 10:
        return f"${amount:.2f}"
    if amount < 1000:
        return f"${amount:.0f}"
    return f"${amount / 1000:.1f}K"

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
    ) -> None:
        super().__init__()
        self._controller = controller
        self._is_dot = True
        # Once the user picks "Hide" from the right-click menu the capsule
        # stays gone until the next process restart — there is no tray icon
        # to bring it back, so all auto-show paths must respect this flag.
        self._hidden_by_user = False
        # Pull today's spend on demand (closure → main wires it to
        # usage_registry.get_totals('today').cost_usd). None means the
        # capsule omits the $ field — keeps the constructor optional so
        # existing tests instantiating CapsuleWindow(controller) still work.
        self._get_today_cost = get_today_cost
        self._cost_cache: float = 0.0
        # Used to render the running-session's name (custom rename ↦
        # ai_title ↦ project basename) in place of "N sessions" when
        # exactly one session is active. None ⇒ capsule falls back to
        # the count-only label.
        self._get_session_details = get_session_details

        self._setup_window()

        # Two labels rather than one so the "●" can pulse independently
        # of the text. A QGraphicsOpacityEffect on the dot label is the
        # cleanest way to animate just one part of the pill — animating
        # the whole label would fade the text in/out, which is
        # distracting when the user is trying to read the cost.
        self._dot_label = QLabel("●", self)
        self._dot_label.setAlignment(
            Qt.AlignmentFlag.AlignCenter | Qt.AlignmentFlag.AlignVCenter
        )
        self._dot_label.setStyleSheet(_STYLE_DOT_IDLE)
        self._dot_opacity = QGraphicsOpacityEffect(self._dot_label)
        self._dot_opacity.setOpacity(1.0)
        self._dot_label.setGraphicsEffect(self._dot_opacity)

        self._label = QLabel("", self)
        self._label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._label.setStyleSheet(_STYLE_LABEL)

        # Animation is created lazily-armed (target set up front, but
        # the loop only starts when refresh_sessions detects an active
        # session). Keeping a single instance avoids a leaking
        # animation-per-tick pattern.
        self._breath_anim = QPropertyAnimation(self._dot_opacity, b"opacity", self)
        self._breath_anim.setDuration(_BREATH_PERIOD_MS)
        self._breath_anim.setStartValue(_BREATH_OPACITY_PEAK)
        self._breath_anim.setKeyValueAt(0.5, _BREATH_OPACITY_FLOOR)
        self._breath_anim.setEndValue(_BREATH_OPACITY_PEAK)
        self._breath_anim.setEasingCurve(QEasingCurve.Type.InOutSine)
        self._breath_anim.setLoopCount(-1)
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
        screen = QApplication.primaryScreen()
        geom = screen.geometry()
        x = geom.center().x() - w // 2
        self.setGeometry(x, geom.top() + _TOP_MARGIN, w, h)

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
        self._stop_breathing()
        self.update()
        self.show()

    def _apply_capsule(self) -> None:
        self._is_dot = False
        self._label.setText(self._compose_label_text())
        self._center_top(_CAPSULE_W, _CAPSULE_H)
        # Dot sits in a fixed slot on the left; text label takes the rest
        # of the width minus a symmetric right margin so centering looks
        # balanced even though the dot occupies space on one side only.
        self._dot_label.setGeometry(_DOT_LEFT_PAD, 0, _DOT_LABEL_W, _CAPSULE_H)
        right_pad = _DOT_LEFT_PAD + _DOT_LABEL_W  # mirror left side
        self._label.setGeometry(
            _TEXT_LEFT, 0, _CAPSULE_W - _TEXT_LEFT - right_pad, _CAPSULE_H,
        )
        self._dot_label.show()
        self._label.show()
        self._refresh_active_state()
        self.update()
        self.show()

    def _compose_label_text(self) -> str:
        """Render the pill text — combines session count or running
        session name with today's cumulative spend.

        Text logic:
          - Exactly one *active* session AND we can resolve its name
            ⇒ show that name (so the user can tell which session is
            burning their $ at a glance).
          - All other cases (zero / multiple active, or name unknown)
            ⇒ show "N sessions" so the digit is at least informative.

        Cost suffix is appended when the getter is wired and the
        cumulative is > 0. ``$0`` is suppressed (no records yet) so a
        fresh first launch reads cleanly as "1 session" instead of
        "1 session  $0".
        """
        cost_suffix = ""
        if self._get_today_cost is not None and self._cost_cache > 0:
            cost_suffix = f"  {_fmt_money(self._cost_cache)}"

        active = self._active_sessions()
        if len(active) == 1 and self._get_session_details is not None:
            name = self._resolve_session_name(active[0])
            if name:
                return f"{name}{cost_suffix}"

        count = len(self._controller.sessions)
        noun = "session" if count == 1 else "sessions"
        return f"{count} {noun}{cost_suffix}"

    def _resolve_session_name(self, session: Session) -> str | None:
        """Return the best human label for ``session``.

        Falls through user-rename → AI title → project directory name.
        Mirrors the resolution order used in ExpandedWindow's row
        rendering so "what the panel calls this session" matches "what
        the pill calls it" for the running-session case."""
        try:
            details = self._get_session_details(session)  # type: ignore[misc]
        except Exception:
            return None
        if details is not None:
            if details.name:
                return details.name
            if details.ai_title:
                return details.ai_title
        basename = session.project_path.name
        return basename or None

    def _active_sessions(self) -> list[Session]:
        """Sessions whose JSONL has been written within the active
        threshold. ``last_activity`` is updated by the JSONL parser
        through SessionRegistry.update_activity, so this read is
        Eventually-Consistent with the live transcript."""
        now = datetime.now(timezone.utc)
        result: list[Session] = []
        for s in self._controller.sessions:
            try:
                age = (now - s.last_activity).total_seconds()
            except (TypeError, ValueError):
                continue
            if age < _ACTIVE_THRESHOLD_SECONDS:
                result.append(s)
        return result

    def _refresh_active_state(self) -> None:
        """Synchronise dot colour + breathing animation with whether
        any session is currently active. Idempotent — safe to call from
        every refresh tick; only restarts the animation on transitions."""
        active = bool(self._active_sessions())
        self._dot_label.setStyleSheet(
            _STYLE_DOT_ACTIVE if active else _STYLE_DOT_IDLE
        )
        if active:
            self._start_breathing()
        else:
            self._stop_breathing()

    def _start_breathing(self) -> None:
        if self._is_breathing:
            return
        self._is_breathing = True
        self._breath_anim.start()

    def _stop_breathing(self) -> None:
        if not self._is_breathing:
            return
        self._is_breathing = False
        self._breath_anim.stop()
        # Snap back to fully visible so the dot doesn't get stranded at
        # 0.55 alpha after the animation cuts off mid-cycle.
        self._dot_opacity.setOpacity(1.0)

    def refresh_sessions(self, sessions: object) -> None:
        """Called by bridge when sessions list changes (updates count label)."""
        if self._hidden_by_user:
            return
        if not self._is_dot:
            self._apply_capsule()

    def refresh_cost(self, _: object = None) -> None:
        """Called by bridge when usage totals change. Pulls today's
        cost via the injected getter and refreshes the pill text + the
        breathing-dot state.

        Why refresh active state here too: ``totals_changed`` fires on
        every JSONL write, which is the most reliable signal that "a
        session is currently producing turns" — much more responsive
        than waiting for the 10-s process scan cycle to re-run
        ``refresh_sessions``. Without this hook, the breathing dot
        would lag activity by up to 10 s.
        """
        if self._hidden_by_user or self._get_today_cost is None:
            return
        try:
            self._cost_cache = float(self._get_today_cost())
        except Exception:
            # Cost is presentational — never let a registry hiccup crash
            # the UI. Keep the previous cached value rather than zeroing
            # (which would briefly flash "$0" between refreshes).
            return
        if not self._is_dot:
            self._label.setText(self._compose_label_text())
            self._refresh_active_state()

    # ------------------------------------------------------------------
    # Paint + events
    # ------------------------------------------------------------------

    def paintEvent(self, event: object) -> None:  # type: ignore[override]
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        path = QPainterPath()
        r = self.height() / 2
        color = _DOT_COLOR if self._is_dot else _BG_COLOR
        path.addRoundedRect(0, 0, self.width(), self.height(), r, r)
        painter.fillPath(path, color)

    def mousePressEvent(self, event) -> None:  # type: ignore[override]
        if event.button() == Qt.MouseButton.RightButton:
            self._show_context_menu(event.globalPosition().toPoint())
            return
        if event.button() == Qt.MouseButton.LeftButton:
            self._controller.toggle_expanded()

    def _show_context_menu(self, global_pos: QPoint) -> None:
        menu = QMenu(self)
        menu.setStyleSheet(_STYLE_MENU)
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
