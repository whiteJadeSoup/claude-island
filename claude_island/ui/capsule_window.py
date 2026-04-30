from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QPainter, QPainterPath
from PySide6.QtWidgets import QApplication, QLabel, QWidget

from .controller import IslandController

_CAPSULE_W = 160
_CAPSULE_H = 36
_DOT_W = 12
_DOT_H = 12
_TOP_MARGIN = 8

_STYLE_LABEL = "color: white; font-size: 12px; font-family: 'Segoe UI', sans-serif;"
_BG_COLOR = QColor(18, 18, 18, 230)
_DOT_COLOR = QColor(80, 80, 80, 200)


class CapsuleWindow(QWidget):
    """Frameless, always-on-top pill anchored to the top-centre of the screen.

    Clicking toggles the expanded panel via the controller.
    Resizes to a small dot when there are no active sessions.
    """

    def __init__(self, controller: IslandController) -> None:
        super().__init__()
        self._controller = controller
        self._is_dot = True

        self._setup_window()

        self._label = QLabel("", self)
        self._label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._label.setStyleSheet(_STYLE_LABEL)

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
        if state == "dot":
            self._apply_dot()
        else:
            self._apply_capsule()

    def _apply_dot(self) -> None:
        self._is_dot = True
        self._center_top(_DOT_W, _DOT_H)
        self._label.hide()
        self.update()
        self.show()

    def _apply_capsule(self) -> None:
        self._is_dot = False
        count = len(self._controller.sessions)
        noun = "session" if count == 1 else "sessions"
        self._label.setText(f"● {count} {noun}")
        self._center_top(_CAPSULE_W, _CAPSULE_H)
        self._label.setGeometry(0, 0, _CAPSULE_W, _CAPSULE_H)
        self._label.show()
        self.update()
        self.show()

    def refresh_sessions(self, sessions: object) -> None:
        """Called by bridge when sessions list changes (updates count label)."""
        if not self._is_dot:
            self._apply_capsule()

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

    def mousePressEvent(self, event: object) -> None:  # type: ignore[override]
        self._controller.toggle_expanded()
