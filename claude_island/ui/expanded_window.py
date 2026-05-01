from __future__ import annotations

from datetime import datetime, timezone
from typing import Callable

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPainterPath
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from claude_island.core.models import Session, UsageTotals
from .controller import IslandController

_PANEL_W = 320
_GAP = 6  # px gap between capsule bottom and panel top

_STYLE_PANEL = """
    color: white;
    font-family: 'Segoe UI', sans-serif;
"""
_STYLE_TITLE = "color: #888; font-size: 10px; letter-spacing: 1px;"
_STYLE_SEP = "background: #2a2a2a;"
_STYLE_SESSION_BTN = """
    QPushButton {
        color: #e0e0e0;
        background: #1e1e1e;
        border: none;
        border-radius: 8px;
        padding: 10px 12px;
        text-align: left;
        font-size: 12px;
    }
    QPushButton:hover { background: #2e2e2e; }
    QPushButton:pressed { background: #383838; }
"""
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


def _fmt_tokens(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.0f}K"
    return str(n)


def _fmt_ago(dt: datetime) -> str:
    delta = datetime.now(timezone.utc) - dt.astimezone(timezone.utc)
    s = int(delta.total_seconds())
    if s < 60:
        return f"{s}s ago"
    if s < 3600:
        return f"{s // 60}m ago"
    return f"{s // 3600}h ago"


class ExpandedWindow(QWidget):
    """Floating panel that appears below the capsule when expanded.

    Shows the session list (clicking activates the terminal) and a usage
    summary with a period selector (Daily / Weekly / Monthly).

    ``session_activated`` is connected in __main__.py to WindowActivator.activate
    so the UI layer never imports platform code directly.
    """

    session_activated: Signal = Signal(Session)

    def __init__(
        self,
        capsule: QWidget,
        controller: IslandController,
        get_usage_totals: Callable[[str], UsageTotals],
    ) -> None:
        super().__init__()
        self._capsule = capsule
        self._controller = controller
        self._get_usage_totals = get_usage_totals
        self._period = "daily"

        self._setup_window()
        self._build_ui()

        controller.state_changed.connect(self._on_state_changed)

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
        root = QVBoxLayout(self)
        root.setContentsMargins(14, 14, 14, 14)
        root.setSpacing(8)

        # Title
        title = QLabel("CLAUDE SESSIONS")
        title.setStyleSheet(_STYLE_TITLE)
        root.addWidget(title)

        # Session list container
        self._session_box = QVBoxLayout()
        self._session_box.setSpacing(4)
        root.addLayout(self._session_box)

        # Separator
        sep = QFrame()
        sep.setFixedHeight(1)
        sep.setStyleSheet(_STYLE_SEP)
        root.addWidget(sep)

        # Usage title
        usage_title = QLabel("USAGE")
        usage_title.setStyleSheet(_STYLE_TITLE)
        root.addWidget(usage_title)

        # Monospace so the token / cost columns line up.
        self._usage_label = QLabel("—")
        self._usage_label.setStyleSheet(
            "color: #ccc; font-size: 11px; font-family: 'Consolas', 'Menlo', monospace;"
        )
        self._usage_label.setTextFormat(Qt.TextFormat.PlainText)
        self._usage_label.setWordWrap(False)
        root.addWidget(self._usage_label)

        # Period selector
        period_row = QHBoxLayout()
        period_row.setSpacing(6)
        self._period_btns: dict[str, QPushButton] = {}
        for label, key in [("Daily", "daily"), ("Weekly", "weekly"), ("Monthly", "monthly")]:
            btn = QPushButton(label)
            btn.setCheckable(True)
            btn.setStyleSheet(_STYLE_PERIOD_BTN)
            btn.setChecked(key == self._period)
            btn.clicked.connect(lambda _, k=key: self._on_period(k))
            period_row.addWidget(btn)
            self._period_btns[key] = btn
        period_row.addStretch()
        root.addLayout(period_row)

        self.setStyleSheet(_STYLE_PANEL)

    # ------------------------------------------------------------------
    # Slots (called by QtBridge / signals)
    # ------------------------------------------------------------------

    def refresh_sessions(self, sessions: list[Session]) -> None:
        # Clear existing rows
        while self._session_box.count():
            item = self._session_box.takeAt(0)
            w = item.widget()
            if w:
                w.deleteLater()

        if not sessions:
            placeholder = QLabel("No active sessions")
            placeholder.setStyleSheet("color: #555; font-size: 12px;")
            self._session_box.addWidget(placeholder)
        else:
            for session in sessions:
                self._session_box.addWidget(self._make_row(session))

        self.adjustSize()
        self._position()

    def refresh_usage_bar(self, _: object = None) -> None:
        t = self._get_usage_totals(self._period)
        rows = [
            ("Input  ", t.input_tokens,          t.input_cost),
            ("Output ", t.output_tokens,         t.output_cost),
            ("Cache W", t.cache_creation_tokens, t.cache_creation_cost),
            ("Cache R", t.cache_read_tokens,     t.cache_read_cost),
        ]
        lines = [
            f"{label}  {_fmt_tokens(tok):>6}  ${cost:>9.4f}"
            for label, tok, cost in rows
        ]
        lines.append("─" * 27)
        lines.append(f"Total           ${t.cost_usd:>9.4f}")
        self._usage_label.setText("\n".join(lines))

    def _on_state_changed(self, state: str) -> None:
        if state == "expanded":
            self.refresh_sessions(self._controller.sessions)
            self.refresh_usage_bar()
            self._position()
            self.show()
            self.raise_()
        else:
            self.hide()

    def _on_period(self, period: str) -> None:
        self._period = period
        for key, btn in self._period_btns.items():
            btn.setChecked(key == period)
        self.refresh_usage_bar()

    # ------------------------------------------------------------------
    # Session row factory
    # ------------------------------------------------------------------

    def _make_row(self, session: Session) -> QPushButton:
        name = session.project_path.name or str(session.project_path)
        ago = _fmt_ago(session.last_activity)
        btn = QPushButton(f"  {name}\n  {ago}")
        btn.setStyleSheet(_STYLE_SESSION_BTN)
        btn.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        btn.setFixedHeight(52)
        btn.clicked.connect(lambda: self._on_row_clicked(session))
        return btn

    def _on_row_clicked(self, session: Session) -> None:
        # Activate first, then collapse — order matters: while our panel is
        # still on top (StaysOnTopHint) we are the foreground process, which
        # is the only state in which SetForegroundWindow is allowed to
        # surface another process's window.
        self.session_activated.emit(session)
        self._controller.toggle_expanded()

    # ------------------------------------------------------------------
    # Paint
    # ------------------------------------------------------------------

    def paintEvent(self, event: object) -> None:  # type: ignore[override]
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        path = QPainterPath()
        path.addRoundedRect(0, 0, self.width(), self.height(), 16, 16)
        painter.fillPath(path, QColor(18, 18, 18, 240))
