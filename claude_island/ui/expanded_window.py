from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
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
    QPushButton:hover {{ background: {_BG_HOVER_SINGLE}; }}
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
    QPushButton:hover {{ background: {_BG_HOVER_IN_GROUP}; }}
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
    ) -> None:
        super().__init__()
        self._capsule = capsule
        self._controller = controller
        self._get_usage_totals = get_usage_totals
        self._period = "daily"
        # Diff-based row update: keep widget references keyed by pid so that
        # session ticks (every ~10s) don't tear down rows the user might be
        # hovering. The placeholder widget (no sessions) is tracked separately
        # — its presence is mutually exclusive with any row.
        self._rows: dict[int, QPushButton] = {}
        self._placeholder: QLabel | None = None

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
        root = QVBoxLayout(self)
        root.setContentsMargins(14, 14, 14, 14)
        root.setSpacing(8)

        # Title
        title = QLabel("CLAUDE SESSIONS")
        title.setStyleSheet(_STYLE_TITLE)
        root.addWidget(title)

        # Session list container
        self._session_box = QVBoxLayout()
        self._session_box.setSpacing(_GROUP_GAP)
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
        self._clear_session_layout()

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
        for group in groups:
            self._session_box.addWidget(self._make_group_widget(group))
            for s in group:
                needed_pids.add(s.pid)

        self._gc_rows(needed_pids)

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
    # Session row factory
    # ------------------------------------------------------------------

    def _make_row(self, session: Session) -> QPushButton:
        """Build a click-target row with a 3-element horizontal layout:
        ``● name ............... age``.

        The QPushButton supplies the click target, hover/pressed
        backgrounds, and rounded background. A QHBoxLayout inside the
        button positions three QLabels (dot / name / age). Each label
        has WA_TransparentForMouseEvents so clicks anywhere on the row
        — dot, name, age, or the empty space between — fall through to
        the button.
        """
        btn = QPushButton()
        btn.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        btn.setFixedHeight(_ROW_HEIGHT)
        btn.setProperty("_session", session)
        btn.setProperty("_siblings", [])

        layout = QHBoxLayout(btn)
        layout.setContentsMargins(_ROW_PAD_H, 0, _ROW_PAD_H, 0)
        layout.setSpacing(10)

        dot = QLabel("●")
        dot.setObjectName("activity_dot")
        dot.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        layout.addWidget(dot)

        name_label = QLabel()
        name_label.setObjectName("name_label")
        name_label.setStyleSheet(_STYLE_NAME)
        name_label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        # Elide long project names from the right so the age stays visible.
        name_label.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        layout.addWidget(name_label, 1)

        age_label = QLabel()
        age_label.setObjectName("age_label")
        age_label.setStyleSheet(_STYLE_AGE)
        age_label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        age_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        layout.addWidget(age_label)

        self._update_row(btn, session)
        btn.clicked.connect(lambda: self._on_row_clicked(
            btn.property("_session"),
            btn.property("_siblings") or [],
        ))
        return btn

    def _update_row(self, btn: QPushButton, session: Session) -> None:
        """Refresh dot color, name, and age on every refresh tick so the
        traffic-light and "Xh" stay current without rebuilding the row."""
        name = session.project_path.name or str(session.project_path)
        age = _fmt_ago(session.last_activity)

        dot = btn.findChild(QLabel, "activity_dot")
        if dot is not None:
            dot.setStyleSheet(_STYLE_DOT.format(color=_activity_color(session.last_activity)))

        name_label = btn.findChild(QLabel, "name_label")
        if name_label is not None and name_label.text() != name:
            name_label.setText(name)

        age_label = btn.findChild(QLabel, "age_label")
        if age_label is not None and age_label.text() != age:
            age_label.setText(age)

        btn.setProperty("_session", session)

    # ------------------------------------------------------------------
    # Card composition (PR2: same-tab grouping)
    # ------------------------------------------------------------------

    def _make_group_widget(self, group: list[Session]) -> QWidget:
        """One group → one widget. Single-session groups render as a
        standalone rounded button; multi-session groups render as a
        rounded card with flat internal rows + thin separators."""
        if len(group) == 1:
            row = self._get_or_create_row(group[0], group, in_card=False)
            row.setParent(None)  # detach from any prior parent
            return row
        return self._make_multi_card(group)

    def _make_multi_card(self, sessions: list[Session]) -> QFrame:
        card = QFrame()
        card.setObjectName("group_card")
        card.setStyleSheet(_STYLE_GROUP_CARD)
        layout = QVBoxLayout(card)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        for i, session in enumerate(sessions):
            if i > 0:
                sep = QFrame()
                sep.setFixedHeight(1)
                sep.setStyleSheet(_STYLE_GROUP_ROW_SEP)
                layout.addWidget(sep)
            row = self._get_or_create_row(session, sessions, in_card=True)
            row.setParent(None)
            layout.addWidget(row)
        return card

    def _get_or_create_row(
        self, session: Session, group: list[Session], *, in_card: bool
    ) -> QPushButton:
        """Cached factory: same pid keeps the same QPushButton across
        refreshes (preserves hover/pressed state). Style is reapplied
        each call because a row can move between standalone (rounded)
        and in-card (flat) layouts as group membership changes.

        ``group`` is the full list of sessions in this row's group
        (including the row itself). Stored on the button so the click
        handler can pass siblings to the activator — needed for the
        inactive-pane case where the row's own console title doesn't
        appear in any TabItem.Name and we have to fall back to one of
        the siblings' titles to actually switch the WT tab.
        """
        btn = self._rows.get(session.pid)
        if btn is None:
            btn = self._make_row(session)
            self._rows[session.pid] = btn
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

    def _on_row_clicked(self, session: Session, siblings: list[Session]) -> None:
        # Activate first, then collapse — order matters: while our panel is
        # still on top (StaysOnTopHint) we are the foreground process, which
        # is the only state in which SetForegroundWindow is allowed to
        # surface another process's window.
        self.session_activated.emit(session, siblings)
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
