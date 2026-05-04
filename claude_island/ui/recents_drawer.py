"""RecentsDrawer — Spotlight-style two-column selector for offline sessions.

Layout:
    420 px wide, two columns separated by a 1 px divider:
      * Left list (160 px) — compact 32 px rows, single-line elided title.
        Selected row gets a 2 px left-side accent bar drawn in paintEvent.
      * Right preview (rest) — full title (collapsible), cwd, branch+time,
        cost+turns, permission-mode chip, last_prompt (collapsible), uuid
        with copy button, and a primary [▶ Resume] + secondary [📂 Open]
        action bar.

Tab toggles the preview column off/on; with preview hidden the drawer
collapses to 220 px so the user can give the main panel more breathing
room (mirrors Mail.app / Raycast). Reposition runs after every width
change so the drawer stays anchored sensibly.

Keyboard model — Spotlight: focus stays on the search box always.
    ↑/↓     - move selection (overrides QLineEdit cursor moves)
    Enter   - resume selected
    Esc     - close drawer
    Tab     - toggle preview column
    Ctrl+O  - open folder of selected
    Ctrl+C  - copy uuid of selected
    printable chars - normal QLineEdit input (search)

Mouse model:
    Single click on row - select (updates preview)
    Double click on row - resume
    Click [▶ Resume]    - resume
    Click [📂 Open]     - open folder
    Click [📋]          - copy uuid

Why no virtual focus state machine: we considered ↑/↓ moving a
"keyboard target" between search and list, but that complicates focus
indication (search border highlight) and brings no real benefit when
↑/↓ are already overridden in eventFilter — the search box never needs
the arrow keys for cursor moves in this UX.
"""
from __future__ import annotations

import logging
import os
import subprocess
import sys
from datetime import datetime, timezone
from typing import Callable

from PySide6.QtCore import QEvent, QPoint, Qt, QTimer
from PySide6.QtGui import (
    QColor,
    QGuiApplication,
    QKeySequence,
    QPainter,
    QPainterPath,
    QShortcut,
)
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from claude_island.core.capabilities import Capability, LauncherSpawnError
from claude_island.core.launch_intent import LaunchIntent, LaunchIntentRegistry
from claude_island.core.models import DormantSession
from claude_island.core.snapshot import WorldSnapshot
from claude_island.ui.collapsible import CollapsibleLinkButton
from claude_island.ui.expanded_window import (
    _BG_HOVER_SINGLE,
    _BG_PRESSED,
    _BG_SINGLE,
    _GROUP_OUTLINE_COLOR,
    _ROW_PAD_H,
    _STYLE_AGE,
    _STYLE_COST_DEFAULT,
    _STYLE_COST_HIGH,
    _STYLE_NAME,
    _STYLE_TEXT_LINK,
    _STYLE_TITLE,
    _ElidingLabel,
    _fmt_money,
    _HoverRevealRow,
)
from claude_island.ui.last_prompt_section import LastPromptSection
from claude_island.ui.recents_filter import filter_by_query, sort_by_recency

log = logging.getLogger(__name__)


# ── Layout constants ───────────────────────────────────────────────────
_DRAWER_WIDTH_FULL = 420
_DRAWER_WIDTH_LIST_ONLY = 220
_LIST_COL_WIDTH = 160
_DRAWER_GAP = 6
_RECENT_ROW_HEIGHT = 32       # compact — main panel uses 52 for two-line rows
_ROW_GAP = 2
_HIGH_COST_USD = 50.0
_TITLE_COLLAPSE_AT = 60
# LAST PROMPT collapsing now lives in LastPromptSection (shared with
# SessionDetailPopup) — no per-surface threshold here.

_ACCENT_COLOR = QColor("#9ca3af")  # selected row left-side accent

# Visual tokens specific to this surface (not reused from expanded_window
# because they only apply here — duplicating ~6 lines is cheaper than
# polluting the main panel's stylesheet vocabulary).
_STYLE_PREVIEW_BODY = "color: #c9c9c9; font-size: 12px;"
_STYLE_PREVIEW_TITLE = "color: #ffffff; font-size: 14px; font-weight: 500;"
_STYLE_UUID = "color: #6b7280; font-size: 10px;"

_STYLE_MODE_CHIP = (
    "color: #f59e0b; font-size: 10px; padding: 1px 6px;"
    "border: 1px solid #f59e0b40; border-radius: 4px; "
    "background: #f59e0b14;"
)

_STYLE_PRIMARY_BTN = f"""
    QPushButton {{
        color: #ffffff;
        background: #2563eb;
        border: 1px solid #1d4ed8;
        border-radius: 6px;
        padding: 6px 0;
        font-size: 11px;
        font-weight: 500;
    }}
    QPushButton:hover {{ background: #1d4ed8; }}
    QPushButton:disabled {{
        color: #6b7280; background: {_BG_SINGLE};
        border-color: {_GROUP_OUTLINE_COLOR};
    }}
"""


def _flags_for_mode(mode: str | None) -> tuple[str, ...]:
    """Translate captured permissionMode → cli flags. Unknown / None → ()."""
    return {
        "bypassPermissions": ("--dangerously-skip-permissions",),
        "acceptEdits":       ("--permission-mode", "acceptEdits"),
        "plan":              ("--permission-mode", "plan"),
    }.get(mode or "", ())


def _relative_time(then: datetime, *, now: datetime | None = None) -> str:
    """Compact ago-string. Same conventions as the rest of the UI."""
    now = now or datetime.now(timezone.utc)
    delta = now - then
    secs = int(delta.total_seconds())
    if secs < 60:
        return "just now"
    mins = secs // 60
    if mins < 60:
        return f"{mins}m ago"
    hours = mins // 60
    if hours < 24:
        return f"{hours}h ago"
    days = hours // 24
    if days < 30:
        return f"{days}d ago"
    return then.strftime("%Y-%m-%d")


def _shorten_cwd(cwd_str: str, max_len: int = 60) -> str:
    """Mid-path elision so both parent dir and leaf survive."""
    if len(cwd_str) <= max_len:
        return cwd_str
    keep_tail = max_len // 2 + 3
    keep_head = max_len - keep_tail - 1
    return cwd_str[:keep_head] + "…" + cwd_str[-keep_tail:]


def _row_title(d: DormantSession) -> str:
    """Human-friendly title for a dormant session row.

    Resolution order: explicit name → first line of last_prompt
    (truncated) → fallback "Untitled session". The title ends up in
    both the left list (elided to row width) and the preview header
    (full text with optional [展开] for very long ones)."""
    if d.name and d.name.strip():
        return d.name.strip()
    if d.last_prompt and d.last_prompt.strip():
        line = d.last_prompt.strip().splitlines()[0]
        return (line[:40] + "…") if len(line) > 40 else line
    return "Untitled session"


class _DispatcherProto:
    """Duck-typed view of TerminalDispatcher used here. UI doesn't import
    the platform_ class directly (import-linter forbids it); __main__
    injects an instance."""
    def adapters_with(self, cap): ...  # noqa: D401
    def launch(self, adapter_name, *, cwd, command): ...  # noqa: D401


# ── _RecentRow ─────────────────────────────────────────────────────────


class _RecentRow(QPushButton):
    """One offline-session row in the left list — 32 px tall, single-line
    elided title with cost on the right.

    Single click → on_select(uuid) (drawer updates preview)
    Double click → on_resume(uuid) (drawer launches terminal)
    """

    def __init__(
        self,
        *,
        dormant: DormantSession,
        on_select: Callable[[str], None],
        on_resume: Callable[[str], None],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._dormant = dormant
        self._on_select = on_select
        self._on_resume_cb = on_resume
        self._selected = False

        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedHeight(_RECENT_ROW_HEIGHT)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self._apply_style()
        self.clicked.connect(self._on_clicked)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(_ROW_PAD_H, 0, _ROW_PAD_H, 0)
        layout.setSpacing(6)

        title_text = _row_title(dormant)
        self._title_lbl = _ElidingLabel(title_text)
        self._title_lbl.setStyleSheet(_STYLE_NAME)
        self._title_lbl.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self._title_lbl.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred,
        )
        # Tooltip carries the full title — even if the right preview is
        # currently showing a different selection, the user can mouse-
        # hover any row and see what it is at a glance.
        self._title_lbl.setToolTip(title_text)
        layout.addWidget(self._title_lbl, 1)

        cost = QLabel(_fmt_money(dormant.cost_usd))
        cost.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        cost.setStyleSheet(
            _STYLE_COST_HIGH if dormant.cost_usd >= _HIGH_COST_USD
            else _STYLE_COST_DEFAULT
        )
        layout.addWidget(cost)

    # ── public API ────────────────────────────────────────────────────

    def session_uuid(self) -> str:
        return self._dormant.session_uuid

    def set_selected(self, selected: bool) -> None:
        if selected == self._selected:
            return
        self._selected = selected
        self._apply_style()
        self.update()  # repaint accent

    # Compatibility shim for legacy tests that drove resume directly via
    # _on_resume() on the row. The redesigned flow routes through the
    # drawer, but keeping this method makes the migration painless.
    def _on_resume(self) -> None:
        self._on_resume_cb(self._dormant.session_uuid)

    # ── events ────────────────────────────────────────────────────────

    def mouseDoubleClickEvent(self, event):  # type: ignore[override]
        if event.button() == Qt.MouseButton.LeftButton:
            self._on_resume_cb(self._dormant.session_uuid)
        super().mouseDoubleClickEvent(event)

    def _on_clicked(self) -> None:
        self._on_select(self._dormant.session_uuid)

    # ── painting ──────────────────────────────────────────────────────

    def paintEvent(self, event):  # type: ignore[override]
        super().paintEvent(event)
        if self._selected:
            painter = QPainter(self)
            # Left-side 2 px accent bar with 4 px top/bottom inset so
            # neighbouring selected rows don't visually merge.
            painter.fillRect(0, 4, 2, self.height() - 8, _ACCENT_COLOR)
            painter.end()

    def _apply_style(self) -> None:
        bg = _BG_HOVER_SINGLE if self._selected else _BG_SINGLE
        self.setStyleSheet(
            f"""
            QPushButton {{
                background: {bg};
                border: none;
                border-radius: 6px;
                text-align: left;
                padding: 0;
            }}
            QPushButton:hover {{ background: {_BG_HOVER_SINGLE}; }}
            QPushButton:pressed {{ background: {_BG_PRESSED}; }}
            """
        )


class _LaunchingRow(QFrame):
    """Row for an in-flight Resume — no click handlers, just a status
    chip. Same 32 px height as _RecentRow so list rhythm stays even."""

    def __init__(
        self,
        *,
        intent: LaunchIntent,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setFixedHeight(_RECENT_ROW_HEIGHT)
        self.setStyleSheet(
            f"""
            QFrame {{
                background: {_BG_SINGLE};
                border: 1px solid {_GROUP_OUTLINE_COLOR};
                border-radius: 6px;
            }}
            """
        )
        layout = QHBoxLayout(self)
        layout.setContentsMargins(_ROW_PAD_H, 0, _ROW_PAD_H, 0)
        layout.setSpacing(6)
        title = QLabel(f"⏳ {intent.session_uuid[:8]}…")
        title.setStyleSheet(_STYLE_NAME)
        layout.addWidget(title, 1)
        sub = QLabel(intent.terminal_name)
        sub.setStyleSheet(_STYLE_AGE)
        layout.addWidget(sub)


# ── RecentsDrawer ──────────────────────────────────────────────────────


class RecentsDrawer(QWidget):
    """Top-level frameless drawer hosting the two-column selector.

    Visually a sibling of the main panel: same paint-event-drawn body,
    same row palette, same title typography. Goal: user feels this is
    "one product surface with two columns" — not a separate widget.
    """

    def __init__(
        self,
        *,
        expanded: QWidget,
        dispatcher: _DispatcherProto,
        launch_intent: LaunchIntentRegistry,
        on_wake: Callable[[], None],
    ) -> None:
        super().__init__(None)
        self._expanded = expanded
        self._dispatcher = dispatcher
        self._launch_intent = launch_intent
        self._on_wake = on_wake

        # ── state ─────────────────────────────────────────────────────
        self._search_query: str = ""
        self._last_snap: WorldSnapshot | None = None
        self._prev_launching: set[str] = set()
        self._selected_uuid: str | None = None
        self._row_widgets: dict[str, _RecentRow] = {}
        self._preview_visible: bool = True
        self._prompt_expanded: bool = False
        self._title_expanded: bool = False

        # Same flag combo as ExpandedWindow / SessionDetailPopup.
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setFixedWidth(_DRAWER_WIDTH_FULL)
        self.setMinimumHeight(300)

        # QToolTip styling — without this, tooltips inherit the parent's
        # translucent BG and render as an unreadable black box.
        self.setStyleSheet(
            "RecentsDrawer { color: white; font-family: 'Segoe UI', sans-serif; }"
            "QToolTip {"
            "  color: #e8e8e8;"
            "  background-color: #1e1e1e;"
            "  border: 1px solid #3a3a3a;"
            "  padding: 6px 8px;"
            "  border-radius: 4px;"
            "  font-size: 12px;"
            "}"
        )

        self._build_ui()

        # Esc closes; Ctrl+O / Ctrl+C act on selection. Parent on self
        # so the shortcut only fires while drawer has focus tree.
        QShortcut(QKeySequence(Qt.Key.Key_Escape), self, self.hide)
        QShortcut(QKeySequence("Ctrl+O"), self, self._open_folder_current)
        QShortcut(QKeySequence("Ctrl+C"), self, self._copy_uuid_current)

    # ── public API ────────────────────────────────────────────────────

    @staticmethod
    def compute(snap: WorldSnapshot):
        """``distinct_until_changed`` key projection. Includes name /
        last_prompt / permission_mode so an ai-title or mode change
        reaches the UI even without a row count change."""
        return (
            tuple(
                (d.session_uuid, d.last_activity, d.cost_usd,
                 d.name, d.last_prompt, d.permission_mode)
                for d in snap.dormant_sessions
            ),
            tuple(
                (i.session_uuid, i.terminal_pid)
                for i in snap.launching_sessions
            ),
        )

    def render(self, snap: WorldSnapshot) -> None:
        self._last_snap = snap
        self._render_rows(snap)
        self._reconcile_selection()
        self._render_preview(snap)
        # Detect launching → gone-without-becoming-live (timeout).
        live_uuids = {
            v.session_uuid for g in snap.session_groups for v in g.views
            if v.session_uuid
        }
        cur = {i.session_uuid: i for i in snap.launching_sessions}
        for uuid in self._prev_launching - cur.keys():
            if uuid in live_uuids:
                continue
            self._show_toast(
                f"Couldn't detect new claude session for {uuid[:8]}. "
                "Check the new terminal window."
            )
        self._prev_launching = set(cur.keys())

    def toggle(self) -> None:
        if self.isVisible():
            self.hide()
        else:
            self._reposition()
            self.show()
            self.raise_()
            self._search.setFocus()
            if self._selected_uuid is None and self._row_widgets:
                self._select_first_visible_row()

    # ── UI build ──────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        body = QVBoxLayout(self)
        body.setContentsMargins(14, 14, 14, 14)
        body.setSpacing(8)

        # header
        header = QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        title = QLabel("RECENTS")
        title.setStyleSheet(_STYLE_TITLE)
        header.addWidget(title)
        header.addStretch(1)
        self._count_label = QLabel("")
        self._count_label.setStyleSheet(_STYLE_AGE)
        header.addWidget(self._count_label)
        hint = QLabel("Esc")
        hint.setStyleSheet("color: #6b7280; font-size: 10px;")
        hint.setToolTip("Esc closes · Tab toggles preview · ↑↓ select · Enter resume")
        header.addWidget(hint)
        body.addLayout(header)

        # search
        self._search = QLineEdit()
        self._search.setPlaceholderText("Search title, path, branch")
        self._search.setStyleSheet(
            f"""
            QLineEdit {{
                background: {_BG_SINGLE};
                color: #e8e8e8;
                border: 1px solid {_GROUP_OUTLINE_COLOR};
                border-radius: 6px;
                padding: 5px 8px;
                font-size: 11px;
            }}
            QLineEdit:focus {{ border-color: #6b7280; }}
            """
        )
        self._search.textChanged.connect(self._on_search_changed)
        self._search.installEventFilter(self)
        body.addWidget(self._search)

        # body — two columns
        body_h = QHBoxLayout()
        body_h.setContentsMargins(0, 0, 0, 0)
        body_h.setSpacing(8)

        # ── left list column ─────────────────────────────────────────
        self._list_scroll = QScrollArea(self)
        self._list_scroll.setWidgetResizable(True)
        self._list_scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self._list_scroll.setVerticalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAsNeeded
        )
        self._list_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._list_scroll.setStyleSheet(_SCROLLAREA_STYLE)
        self._list_scroll.setFixedWidth(_LIST_COL_WIDTH)
        self._list_container = QWidget()
        self._list_container.setStyleSheet("background: transparent;")
        self._list_box = QVBoxLayout(self._list_container)
        self._list_box.setSpacing(_ROW_GAP)
        self._list_box.setContentsMargins(0, 0, 0, 0)
        self._list_box.addStretch(1)
        self._list_scroll.setWidget(self._list_container)
        body_h.addWidget(self._list_scroll)

        # vertical 1 px divider
        self._divider = QFrame()
        self._divider.setFrameShape(QFrame.Shape.VLine)
        self._divider.setStyleSheet("background: #2a2a2a;")
        self._divider.setFixedWidth(1)
        body_h.addWidget(self._divider)

        # ── right preview column ─────────────────────────────────────
        self._preview_scroll = QScrollArea(self)
        self._preview_scroll.setWidgetResizable(True)
        self._preview_scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self._preview_scroll.setVerticalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAsNeeded
        )
        self._preview_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._preview_scroll.setStyleSheet(_SCROLLAREA_STYLE)
        self._preview_container = QWidget()
        self._preview_container.setStyleSheet("background: transparent;")
        self._preview_box = QVBoxLayout(self._preview_container)
        self._preview_box.setSpacing(8)
        self._preview_box.setContentsMargins(0, 0, 0, 0)
        self._preview_scroll.setWidget(self._preview_container)
        body_h.addWidget(self._preview_scroll, 1)

        body.addLayout(body_h, 1)

        # toast
        self._toast = QLabel("")
        self._toast.setStyleSheet(
            f"""
            QLabel {{
                background: {_BG_SINGLE};
                color: #fecaca;
                border: 1px solid #b91c1c;
                border-radius: 6px;
                padding: 6px 10px;
                font-size: 11px;
            }}
            """
        )
        self._toast.setWordWrap(True)
        self._toast.setVisible(False)
        body.addWidget(self._toast)
        self._toast_timer = QTimer(self)
        self._toast_timer.setSingleShot(True)
        self._toast_timer.timeout.connect(lambda: self._toast.setVisible(False))

    # ── paintEvent — match ExpandedWindow body ────────────────────────

    def paintEvent(self, event):  # type: ignore[override]
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        path = QPainterPath()
        path.addRoundedRect(0, 0, self.width(), self.height(), 16, 16)
        painter.fillPath(path, QColor(18, 18, 18, 240))

    # ── render: rows ──────────────────────────────────────────────────

    def _render_rows(self, snap: WorldSnapshot) -> None:
        # Clear all but the trailing stretch.
        while self._list_box.count() > 1:
            item = self._list_box.takeAt(0)
            w = item.widget()
            if w is not None:
                w.setParent(None)
                w.deleteLater()
        self._row_widgets.clear()

        launching = snap.launching_sessions
        dormant_all = snap.dormant_sessions
        dormant_sorted = sort_by_recency(
            filter_by_query(dormant_all, self._search_query),
        )

        count_text = f"· {len(dormant_all)}"
        if launching:
            count_text += f"  ⏳ {len(launching)}"
        self._count_label.setText(count_text)

        # Launching first.
        for intent in launching:
            self._list_box.insertWidget(
                self._list_box.count() - 1,
                _LaunchingRow(intent=intent, parent=self._list_container),
            )

        if not dormant_sorted and not launching:
            empty = QLabel(
                "No matches." if self._search_query.strip()
                else "No recent sessions yet."
            )
            empty.setStyleSheet(_STYLE_AGE + " padding: 20px;")
            empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
            empty.setWordWrap(True)
            self._list_box.insertWidget(self._list_box.count() - 1, empty)
            return

        for d in dormant_sorted:
            row = _RecentRow(
                dormant=d,
                on_select=self._select_uuid,
                on_resume=self._on_resume,
                parent=self._list_container,
            )
            self._row_widgets[d.session_uuid] = row
            self._list_box.insertWidget(self._list_box.count() - 1, row)

    def _reconcile_selection(self) -> None:
        """Selection might no longer match a visible row (e.g. user
        searched and current selection got filtered out). Fall back to
        the first visible row, or None if list is empty."""
        if self._selected_uuid in self._row_widgets:
            self._row_widgets[self._selected_uuid].set_selected(True)
            return
        if self._row_widgets:
            self._selected_uuid = next(iter(self._row_widgets))
            self._row_widgets[self._selected_uuid].set_selected(True)
        else:
            self._selected_uuid = None

    def _select_first_visible_row(self) -> None:
        if self._row_widgets:
            self._select_uuid(next(iter(self._row_widgets)))

    def _select_uuid(self, uuid: str) -> None:
        if uuid == self._selected_uuid:
            return
        if (
            self._selected_uuid is not None
            and self._selected_uuid in self._row_widgets
        ):
            self._row_widgets[self._selected_uuid].set_selected(False)
        self._selected_uuid = uuid
        if uuid in self._row_widgets:
            self._row_widgets[uuid].set_selected(True)
            self._list_scroll.ensureWidgetVisible(self._row_widgets[uuid])
        # Reset both expansion states — new selection is fresh content.
        self._prompt_expanded = False
        self._title_expanded = False
        if self._last_snap is not None:
            self._render_preview(self._last_snap)

    # ── render: preview ───────────────────────────────────────────────

    def _selected_dormant(self, snap: WorldSnapshot) -> DormantSession | None:
        if self._selected_uuid is None:
            return None
        for d in snap.dormant_sessions:
            if d.session_uuid == self._selected_uuid:
                return d
        return None

    def _render_preview(self, snap: WorldSnapshot) -> None:
        # Clear preview_box entirely.
        while self._preview_box.count():
            item = self._preview_box.takeAt(0)
            w = item.widget()
            if w is not None:
                w.setParent(None)
                w.deleteLater()

        d = self._selected_dormant(snap)
        if d is None:
            placeholder = QLabel(
                "No matches." if self._search_query.strip()
                else "Select a session to preview"
            )
            placeholder.setStyleSheet(_STYLE_AGE + " padding: 40px 0;")
            placeholder.setAlignment(Qt.AlignmentFlag.AlignCenter)
            placeholder.setWordWrap(True)
            self._preview_box.addWidget(placeholder)
            self._preview_box.addStretch(1)
            return

        # ── title ────────────────────────────────────────────────────
        title_text = _row_title(d)
        if self._title_expanded or len(title_text) <= _TITLE_COLLAPSE_AT:
            display_title = title_text
        else:
            display_title = title_text[:_TITLE_COLLAPSE_AT] + "…"
        title_lbl = QLabel(display_title)
        title_lbl.setStyleSheet(_STYLE_PREVIEW_TITLE)
        title_lbl.setWordWrap(True)
        self._preview_box.addWidget(title_lbl)
        if len(title_text) > _TITLE_COLLAPSE_AT:
            t = CollapsibleLinkButton()
            t.set_expanded(self._title_expanded)
            t.state_changed.connect(self._on_title_toggle)
            self._preview_box.addWidget(t)

        self._preview_box.addWidget(self._mk_divider())

        # ── meta block ───────────────────────────────────────────────
        # The cwd row is hover-reveal: ↗ glyph appears on hover, the
        # path text itself is also clickable so a near-cursor click
        # works without aiming. Same affordance shape as the
        # SessionDetailPopup Path row — two surfaces, one pattern.
        cwd_row = _HoverRevealRow()
        cwd_h = QHBoxLayout(cwd_row)
        cwd_h.setContentsMargins(0, 0, 0, 0)
        cwd_h.setSpacing(4)
        cwd_lbl = QLabel(f"📁  {_shorten_cwd(str(d.cwd))}")
        cwd_lbl.setStyleSheet(_STYLE_PREVIEW_BODY)
        cwd_lbl.setToolTip(f"{d.cwd}\nClick to open · Ctrl+O")
        cwd_lbl.setWordWrap(True)
        cwd_lbl.setCursor(Qt.CursorShape.PointingHandCursor)
        cwd_lbl.mousePressEvent = lambda _: self._open_folder_current()
        cwd_h.addWidget(cwd_lbl, 1)
        cwd_open = QPushButton("↗")
        cwd_open.setStyleSheet(_STYLE_TEXT_LINK)
        cwd_open.setCursor(Qt.CursorShape.PointingHandCursor)
        cwd_open.setToolTip("Open folder · Ctrl+O")
        cwd_open.setFixedWidth(16)
        cwd_open.clicked.connect(self._open_folder_current)
        cwd_h.addWidget(cwd_open)
        cwd_row.register_reveal(cwd_open)
        self._preview_box.addWidget(cwd_row)

        branch = d.git_branch or "—"
        bt_lbl = QLabel(f"🌿  {branch}  ·  {_relative_time(d.last_activity)}")
        bt_lbl.setStyleSheet(_STYLE_PREVIEW_BODY)
        self._preview_box.addWidget(bt_lbl)

        ct_lbl = QLabel(f"💰  {_fmt_money(d.cost_usd)}  ·  {d.turn_count} turns")
        ct_lbl.setStyleSheet(_STYLE_PREVIEW_BODY)
        self._preview_box.addWidget(ct_lbl)

        if d.permission_mode and d.permission_mode != "default":
            chip = QLabel(d.permission_mode)
            chip.setStyleSheet(_STYLE_MODE_CHIP)
            chip.setMaximumHeight(20)
            chip_row = QHBoxLayout()
            chip_row.setContentsMargins(0, 0, 0, 0)
            chip_row.addWidget(chip)
            chip_row.addStretch(1)
            chip_holder = QWidget()
            chip_holder.setLayout(chip_row)
            self._preview_box.addWidget(chip_holder)

        # ── last_prompt — shared widget with SessionDetailPopup ─────
        if d.last_prompt:
            self._preview_box.addWidget(self._mk_divider())
            # Preview column inner width = drawer full width minus list
            # column minus a safety pad for the scrollbar + margins.
            inner_w = max(40, _DRAWER_WIDTH_FULL - _LIST_COL_WIDTH - 30)
            section = LastPromptSection(
                d.last_prompt, available_width=inner_w,
            )
            # Restore expansion across re-renders (selection unchanged
            # but a snapshot tick fired): the drawer-level
            # _prompt_expanded flag captures the user's last choice;
            # _select_uuid resets it on selection change.
            if self._prompt_expanded:
                section._on_toggle()
            section.expansion_changed.connect(self._on_prompt_section_toggled)
            self._preview_box.addWidget(section)

        # ── uuid (click to copy) ────────────────────────────────────
        # Same hover-reveal pattern as the cwd row above.
        self._preview_box.addWidget(self._mk_divider())
        uuid_row = _HoverRevealRow()
        uuid_h = QHBoxLayout(uuid_row)
        uuid_h.setContentsMargins(0, 0, 0, 0)
        uuid_h.setSpacing(4)
        uuid_lbl = QLabel(d.session_uuid)
        uuid_lbl.setStyleSheet(_STYLE_UUID)
        uuid_lbl.setToolTip("Click to copy · Ctrl+C")
        uuid_lbl.setCursor(Qt.CursorShape.PointingHandCursor)
        uuid_lbl.mousePressEvent = lambda _: self._copy_uuid_current()
        uuid_h.addWidget(uuid_lbl, 1)
        uuid_copy = QPushButton("⧉")
        uuid_copy.setStyleSheet(_STYLE_TEXT_LINK)
        uuid_copy.setCursor(Qt.CursorShape.PointingHandCursor)
        uuid_copy.setToolTip("Copy session ID · Ctrl+C")
        uuid_copy.setFixedWidth(16)
        uuid_copy.clicked.connect(self._copy_uuid_current)
        uuid_h.addWidget(uuid_copy)
        uuid_row.register_reveal(uuid_copy)
        self._preview_box.addWidget(uuid_row)

        # ── action: Resume only ─────────────────────────────────────
        # Open / Copy buttons removed — those affordances now live on
        # the rows above (hover ↗ / ⧉). Resume gets full width.
        resume_btn = QPushButton("▶ Resume")
        resume_btn.setObjectName("preview_resume_btn")
        resume_btn.setStyleSheet(_STYLE_PRIMARY_BTN)
        resume_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        resume_btn.setToolTip("Enter")
        # Capture uuid in default-arg closure so a later selection change
        # doesn't repoint this button at the wrong session.
        resume_btn.clicked.connect(
            lambda _checked=False, uuid=d.session_uuid: self._on_resume(uuid)
        )
        self._preview_box.addWidget(resume_btn)
        self._preview_box.addStretch(1)

    def _mk_divider(self) -> QFrame:
        f = QFrame()
        f.setFrameShape(QFrame.Shape.HLine)
        f.setStyleSheet("background: #2a2a2a; max-height: 1px;")
        return f

    def _on_prompt_section_toggled(self, expanded: bool) -> None:
        """LastPromptSection emits this when the user toggles. We just
        record the new state so the next ``_render_preview`` (e.g.
        triggered by a snapshot tick) restores the user's choice
        instead of collapsing back to default. No re-render here —
        the section already updated its own content in-place."""
        self._prompt_expanded = expanded

    def _on_title_toggle(self, expanded: bool) -> None:
        self._title_expanded = expanded
        if self._last_snap is not None:
            self._render_preview(self._last_snap)

    # ── search / keyboard ─────────────────────────────────────────────

    def _on_search_changed(self, text: str) -> None:
        self._search_query = text
        if self._last_snap is None:
            return
        self._render_rows(self._last_snap)
        self._reconcile_selection()
        self._render_preview(self._last_snap)

    def eventFilter(self, obj, event):  # type: ignore[override]
        if obj is self._search and event.type() == QEvent.Type.KeyPress:
            key = event.key()
            if key == Qt.Key.Key_Down:
                self._move_selection(+1)
                return True
            if key == Qt.Key.Key_Up:
                self._move_selection(-1)
                return True
            if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
                self._resume_current()
                return True
            if key == Qt.Key.Key_Escape:
                self.hide()
                return True
            if key == Qt.Key.Key_Tab:
                self._toggle_preview()
                return True
        return super().eventFilter(obj, event)

    def _move_selection(self, delta: int) -> None:
        uuids = list(self._row_widgets.keys())
        if not uuids:
            return
        if self._selected_uuid not in uuids:
            self._select_uuid(uuids[0])
            return
        idx = uuids.index(self._selected_uuid)
        new_idx = max(0, min(len(uuids) - 1, idx + delta))
        if new_idx != idx:
            self._select_uuid(uuids[new_idx])

    def _toggle_preview(self) -> None:
        self._preview_visible = not self._preview_visible
        new_w = (
            _DRAWER_WIDTH_FULL if self._preview_visible
            else _DRAWER_WIDTH_LIST_ONLY
        )
        self.setFixedWidth(new_w)
        self._divider.setVisible(self._preview_visible)
        self._preview_scroll.setVisible(self._preview_visible)
        self._reposition()

    # ── actions ───────────────────────────────────────────────────────

    def _current_dormant(self) -> DormantSession | None:
        if self._last_snap is None:
            return None
        return self._selected_dormant(self._last_snap)

    def _resume_current(self) -> None:
        if self._selected_uuid is None:
            return
        self._on_resume(self._selected_uuid)

    def _on_resume(self, uuid: str) -> None:
        d = None
        if self._last_snap is not None:
            for x in self._last_snap.dormant_sessions:
                if x.session_uuid == uuid:
                    d = x
                    break
        if d is None:
            return

        flags = _flags_for_mode(d.permission_mode)
        command = ("claude", "--resume", d.session_uuid, *flags)

        candidates = self._dispatcher.adapters_with(Capability.LAUNCH)
        if not candidates:
            self._show_toast(
                "No terminal launcher available — "
                "install Windows Terminal or iTerm2"
            )
            return
        adapter_name, _ = candidates[0]

        try:
            result = self._dispatcher.launch(
                adapter_name, cwd=d.cwd, command=command,
            )
        except LauncherSpawnError as e:
            self._show_toast(f"Failed to launch: {e}")
            return
        except Exception:
            log.exception("dispatcher.launch raised unexpectedly")
            self._show_toast("Failed to launch (see logs)")
            return

        self._launch_intent.add(LaunchIntent(
            session_uuid=d.session_uuid,
            cwd=d.cwd,
            flags=flags,
            terminal_name=result.terminal_name,
            terminal_pid=result.terminal_pid,
            requested_at=result.started_at,
        ))
        self._on_wake()

    def _copy_uuid_current(self) -> None:
        if self._selected_uuid is None:
            return
        try:
            QApplication.clipboard().setText(self._selected_uuid)
            self._show_toast(f"Copied: {self._selected_uuid[:8]}…")
        except Exception:
            pass

    def _open_folder_current(self) -> None:
        d = self._current_dormant()
        if d is None:
            return
        try:
            cwd = str(d.cwd)
            if sys.platform == "win32":
                os.startfile(cwd)  # type: ignore[attr-defined]
            elif sys.platform == "darwin":
                subprocess.Popen(["open", cwd])
            else:
                subprocess.Popen(["xdg-open", cwd])
        except Exception as e:
            self._show_toast(f"Could not open folder: {e}")

    def _show_toast(self, msg: str) -> None:
        self._toast.setText(msg)
        self._toast.setVisible(True)
        self._toast_timer.start(6000)

    # ── reposition (multi-monitor aware) ──────────────────────────────

    def _reposition(self) -> None:
        """Anchor next to the expanded panel.

        Strategy (in order):
          1. Right of expanded — preferred.
          2. If right anchor would overflow, dock to the LEFT.
          3. Else center on the panel's screen.
        """
        try:
            geo = self._expanded.frameGeometry()
        except Exception:
            return

        screen = self._screen_for_geometry(geo)
        screen_geo = screen.availableGeometry() if screen else None

        height = max(geo.height(), 300)
        if screen_geo is not None:
            height = min(height, screen_geo.height())

        cur_w = self.width()
        right_x = geo.right() + _DRAWER_GAP
        left_x = geo.left() - _DRAWER_GAP - cur_w
        y = geo.top()

        chosen_x = right_x
        if screen_geo is not None:
            fits_right = right_x + cur_w <= screen_geo.right() + 1
            fits_left = left_x >= screen_geo.left() - 1
            if not fits_right and fits_left:
                chosen_x = left_x
            elif not fits_right and not fits_left:
                chosen_x = screen_geo.x() + (
                    (screen_geo.width() - cur_w) // 2
                )
            if y + height > screen_geo.bottom():
                y = max(screen_geo.top(), screen_geo.bottom() - height)
            if y < screen_geo.top():
                y = screen_geo.top()

        self.setMinimumHeight(0)
        self.resize(cur_w, height)
        self.move(QPoint(chosen_x, y))

    @staticmethod
    def _screen_for_geometry(geo) -> object | None:
        try:
            centre = geo.center()
            for s in QGuiApplication.screens():
                if s.geometry().contains(centre):
                    return s
            return QGuiApplication.primaryScreen()
        except Exception:
            return None


# Reused scroll-area visual style — same as ExpandedWindow's session
# scroll. Defined at module scope so both list + preview scroll areas
# stay in lockstep.
_SCROLLAREA_STYLE = (
    "QScrollArea { background: transparent; border: none; }"
    "QScrollBar:vertical { background: transparent; width: 6px; margin: 0; }"
    "QScrollBar::handle:vertical { background: #3a3a3a; "
    "  border-radius: 3px; min-height: 20px; }"
    "QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }"
)
