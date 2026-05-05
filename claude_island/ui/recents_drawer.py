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
from claude_island.ui.fonts import UI_FONT_STACK
from claude_island.ui.tooltip_style import TOOLTIP_QSS
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
# Past this many chars the title gets tail-elided + a hover tooltip
# carries the full text. Picked at 200 because a real session title
# (user-given name OR first line of last_prompt) almost always fits in
# 2-3 wrapped lines below this; only pathological prompt-fallback
# titles hit the cap. See _row_title for what produces a "title".
_TITLE_HARD_CAP = 200
# LAST PROMPT collapsing now lives in LastPromptSection (shared with
# SessionDetailPopup) — no per-surface threshold here.

_ACCENT_COLOR = QColor("#9ca3af")  # selected row left-side accent

# Visual tokens specific to this surface (not reused from expanded_window
# because they only apply here — duplicating ~6 lines is cheaper than
# polluting the main panel's stylesheet vocabulary).
_STYLE_PREVIEW_BODY = "color: #c9c9c9; font-size: 12px;"
_STYLE_PREVIEW_TITLE = "color: #ffffff; font-size: 14px; font-weight: 500;"

_STYLE_MODE_CHIP = (
    "color: #f59e0b; font-size: 10px; padding: 1px 6px;"
    "border: 1px solid #f59e0b40; border-radius: 4px; "
    "background: #f59e0b14;"
)

_STYLE_PRIMARY_BTN = f"""
    QPushButton {{
        color: #e8efff;
        background: #1d4ed8;
        border: 1px solid #1e3a8a;
        border-radius: 14px;
        padding: 4px 12px;
        font-size: 11px;
        font-weight: 500;
    }}
    QPushButton:hover {{ background: #2563eb; border-color: #1d4ed8; }}
    QPushButton:disabled {{
        color: #6b7280; background: {_BG_SINGLE};
        border-color: {_GROUP_OUTLINE_COLOR};
    }}
"""

# Header close glyph — matches the panel's mute palette; only takes
# colour on hover so it stays unobtrusive at rest.
_STYLE_CLOSE_BTN = """
    QPushButton {
        color: #6b7280;
        background: transparent;
        border: none;
        font-size: 16px;
        padding: 0;
    }
    QPushButton:hover { color: #e8e8e8; }
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

    Resolution order:
      1. explicit name (ai_title or user-set)
      2. first line of last_prompt, truncated
      3. cwd basename — most sessions don't have an explicit name AND
         have no last_prompt yet (a launched-but-not-typed session).
         Without this fallback the list shows N rows of "Untitled
         session" with no way to tell them apart.
      4. literal "Untitled session" as last resort
    """
    if d.name and d.name.strip():
        return d.name.strip()
    if d.last_prompt and d.last_prompt.strip():
        line = d.last_prompt.strip().splitlines()[0]
        return (line[:40] + "…") if len(line) > 40 else line
    if d.cwd and d.cwd.name:
        return d.cwd.name
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
        # Per the Spotlight keyboard model the search box owns focus
        # always — arrow keys flow through ITS eventFilter to drive row
        # selection. If the row itself can take focus, clicking one
        # parks Qt focus on the row and Fusion's default focus
        # highlight (a bluish tint on QPushButton) bleeds through the
        # row's stylesheet, making the focused row look different from
        # the selected row. NoFocus pins both states under our own
        # ``_selected`` styling and preserves the eventFilter route.
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
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

        # Same flag combo as ExpandedWindow. Qt.Tool is dropped on
        # macOS — see CapsuleWindow._setup_window for the NSPanel
        # WA_TranslucentBackground rendering bug that motivates it.
        flags = (
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
        )
        if sys.platform != "darwin":
            flags |= Qt.WindowType.Tool
        self.setWindowFlags(flags)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setFixedWidth(_DRAWER_WIDTH_FULL)
        self.setMinimumHeight(300)

        # QToolTip QSS is appended below — Qt's stylesheet resolution
        # shadows the app-level rule for tooltips popping inside a
        # widget that has its own setStyleSheet (which we do here), so
        # the rule MUST also live in this widget's stylesheet to take
        # effect. Single source: claude_island.ui.tooltip_style.
        self.setStyleSheet(
            f"RecentsDrawer {{ color: white; font-family: {UI_FONT_STACK}; }}"
            + TOOLTIP_QSS
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
            # ``activateWindow()`` is required on Windows: ``Qt.Tool``
            # adds the ``WS_EX_TOOLWINDOW`` extended style, which by
            # design does NOT steal focus from the parent app on
            # show. Without this call ``_search.setFocus()`` only
            # sets the *logical* focus marker — the window stays
            # inactive, so the user's arrow-key presses route to
            # whichever window IS active (the panel behind, or the
            # underlying browser) and surface as page-scroll
            # instead of row navigation.
            self.activateWindow()
            self._search.setFocus()
            if self._selected_uuid is None and self._row_widgets:
                self._select_first_visible_row()

    # ── UI build ──────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        body = QVBoxLayout(self)
        body.setContentsMargins(14, 14, 14, 14)
        body.setSpacing(8)

        # header — RECENTS · count merged into one label so the count
        # sits adjacent to the title (the prior layout pushed it to the
        # far right via addStretch which made the gap confusing). The
        # decorative "Esc" label is replaced by an actual × close button
        # — same affordance, but actually clickable. Esc still works as
        # the tooltip + the existing QShortcut.
        header = QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        self._header_label = QLabel("RECENTS")
        self._header_label.setStyleSheet(_STYLE_TITLE)
        self._header_label.setToolTip(
            "Esc closes · Tab toggles preview · ↑↓ select · Enter resume"
        )
        header.addWidget(self._header_label)
        header.addStretch(1)
        close_btn = QPushButton("×")
        close_btn.setStyleSheet(_STYLE_CLOSE_BTN)
        close_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        close_btn.setToolTip("Close · Esc")
        close_btn.setFixedSize(20, 20)
        # All clickable widgets in the drawer set NoFocus so focus
        # stays on the search box (Spotlight pattern) — see _RecentRow
        # for the full rationale.
        close_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        close_btn.clicked.connect(self.hide)
        header.addWidget(close_btn)
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
        # QScrollArea defaults to Qt.WheelFocus — once the user wheel-
        # scrolls the list, focus migrates to the scroll area and ↑/↓
        # routes to its built-in keyPressEvent (= scroll one line),
        # bypassing the search box's eventFilter that drives row
        # selection. NoFocus pins the Spotlight contract: search box
        # owns focus, scroll area is mouse-only chrome.
        self._list_scroll.setFocusPolicy(Qt.FocusPolicy.NoFocus)
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
        # Same Spotlight focus contract as the list scroll above —
        # see comment there for why.
        self._preview_scroll.setFocusPolicy(Qt.FocusPolicy.NoFocus)
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

        # Resume button moved INTO the preview header (left of title)
        # so its visual binding to the selected session is unambiguous.
        # The previous full-width sticky footer button looked like a
        # banner action affecting the whole drawer rather than the
        # specific selection. ``_resume_target_uuid`` is the persistent
        # binding — the button widget itself is recreated on every
        # ``_render_preview`` pass alongside the rest of the header.
        self._resume_target_uuid: str | None = None

    def _on_resume_clicked(self) -> None:
        """Stable click handler that always reads the current selection
        target — avoids the per-render lambda-capture pattern that the
        previous in-content button needed."""
        if self._resume_target_uuid is not None:
            self._on_resume(self._resume_target_uuid)

    def _update_resume_target(self, uuid: str | None) -> None:
        """Rebind the persistent resume target the next click consults.

        After the v3.1 redesign the button widget itself is recreated
        every ``_render_preview`` (it lives inside the header row of
        the dynamically-rebuilt preview), so there's no widget to
        toggle here — only the bound UUID. The newly-created button
        reads ``self._resume_target_uuid`` via ``_on_resume_clicked``
        and sets its own enabled state at construction time."""
        self._resume_target_uuid = uuid

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

        count_text = f"RECENTS · {len(dormant_all)}"
        if launching:
            count_text += f"  ⏳ {len(launching)}"
        self._header_label.setText(count_text)

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
        # Reset prompt expansion — new selection is fresh content.
        # (Title no longer has an expansion state; long titles are
        # always tail-elided + tooltip, no per-selection toggle.)
        self._prompt_expanded = False
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
            # Sticky footer button stays visible for layout stability
            # but disables when there's nothing to resume into.
            self._update_resume_target(None)
            return

        # ── header row: [▶ Resume] <title> ──────────────────────────
        # The action button sits LEFT of the title so the visual group
        # reads "Resume → <this thing>". Compact pill (~96 px wide,
        # 28 px tall) keeps it from looking like a banner. Title wraps
        # freely to multiple lines if needed; the button stays anchored
        # top-left while the text flows beside / below it.
        #
        # Two-tier title strategy unchanged: short titles wrap; very
        # long titles (>200 chars, only happens when the title is a
        # fallback first-line of a giant prompt) get tail-elided with
        # the full text in tooltip.
        title_text = _row_title(d)
        if len(title_text) > _TITLE_HARD_CAP:
            display_title = title_text[:_TITLE_HARD_CAP] + "…"
        else:
            display_title = title_text

        header_row = QWidget()
        header_h = QHBoxLayout(header_row)
        header_h.setContentsMargins(0, 0, 0, 0)
        header_h.setSpacing(8)

        resume_btn = QPushButton("▶ Resume")
        resume_btn.setObjectName("preview_resume_btn")
        resume_btn.setStyleSheet(_STYLE_PRIMARY_BTN)
        resume_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        # Intentionally NO setToolTip — the "▶ Resume" label is its own
        # affordance; a tooltip repeating the same word adds noise and
        # overlaps the title text on hover (the bug the user reported).
        # Keyboard hint (Enter) is documented in the drawer header.
        resume_btn.setFixedHeight(28)
        resume_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        resume_btn.clicked.connect(self._on_resume_clicked)
        # AlignVCenter so a single-line title and the button share a
        # baseline (visually centred on each other). For multi-line
        # titles the button still vcenters on the whole title block,
        # which reads better than top-aligning the button while the
        # title stretches downward.
        header_h.addWidget(resume_btn, 0, Qt.AlignmentFlag.AlignVCenter)

        title_lbl = QLabel(display_title)
        title_lbl.setStyleSheet(_STYLE_PREVIEW_TITLE)
        title_lbl.setWordWrap(True)
        # Allow the label to shrink below its sizeHint so the parent
        # HBoxLayout's width constraint actually clips it. Without an
        # explicit minimum-zero, QLabel reserves sizeHint() width and
        # the title overflows past the panel edge instead of wrapping.
        title_lbl.setMinimumWidth(0)
        title_lbl.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred,
        )
        # Tooltip is set ONLY when the visible string was tail-elided
        # past the hard cap. For normal-length titles wordWrap shows
        # the full text across as many lines as needed and a hover-
        # overlay would be a pure repeat (NN/g: "tooltips with obvious
        # or redundant text are not beneficial"). For pathological
        # >200-char titles (typically a fallback first-line of a giant
        # prompt) we DO need the escape hatch — that's the only path
        # to recover the dropped tail.
        if display_title != title_text:
            title_lbl.setToolTip(title_text)
        header_h.addWidget(title_lbl, 1, Qt.AlignmentFlag.AlignVCenter)

        self._preview_box.addWidget(header_row)
        self._preview_box.addWidget(self._mk_divider())

        # ── meta block ───────────────────────────────────────────────
        # Order: UUID first (the canonical identifier — what `claude
        # --resume` actually consumes), then path / branch / time /
        # cost / permission. UUID + path rows use hover-reveal: a glyph
        # button appears on row hover and the text itself is also
        # clickable, so a near-cursor click works without aiming.
        # Same affordance shape as SessionDetailPopup — two surfaces,
        # one pattern.
        uuid_row = _HoverRevealRow()
        uuid_h = QHBoxLayout(uuid_row)
        uuid_h.setContentsMargins(0, 0, 0, 0)
        uuid_h.setSpacing(4)
        # UUID has hyphen-separated chunks (8-4-4-4-12). QLabel.wordWrap
        # only breaks at whitespace by default; a UUID has no spaces, so
        # without help it overflows or hard-clips. Inserting U+200B
        # (zero-width space) after each hyphen tells Qt those positions
        # are valid wrap points without changing the visible text. Falls
        # back to single-line if the label fits.
        uuid_text = d.session_uuid.replace("-", "-​")
        uuid_lbl = QLabel(f"🆔  {uuid_text}")
        uuid_lbl.setStyleSheet(_STYLE_PREVIEW_BODY)
        uuid_lbl.setWordWrap(True)
        uuid_lbl.setMinimumWidth(0)
        uuid_lbl.setToolTip("Click to copy session ID · Ctrl+C")
        uuid_lbl.setCursor(Qt.CursorShape.PointingHandCursor)
        uuid_lbl.mousePressEvent = lambda _: self._copy_uuid_current()
        uuid_h.addWidget(uuid_lbl, 1)
        uuid_copy = QPushButton("⧉")
        uuid_copy.setStyleSheet(_STYLE_TEXT_LINK)
        uuid_copy.setCursor(Qt.CursorShape.PointingHandCursor)
        uuid_copy.setToolTip("Copy session ID · Ctrl+C")
        uuid_copy.setFixedWidth(16)
        uuid_copy.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        uuid_copy.clicked.connect(self._copy_uuid_current)
        uuid_h.addWidget(uuid_copy)
        uuid_row.register_reveal(uuid_copy)
        self._preview_box.addWidget(uuid_row)

        cwd_row = _HoverRevealRow()
        cwd_h = QHBoxLayout(cwd_row)
        cwd_h.setContentsMargins(0, 0, 0, 0)
        cwd_h.setSpacing(4)
        # Path: insert U+200B after every "/" so wordWrap can break at
        # path-segment boundaries. Without the hint Qt has nowhere to
        # break (paths have no whitespace) and either overflows or
        # snaps mid-character. ``_shorten_cwd`` still applies for very
        # long paths so the visible string isn't insanely long even
        # before wrapping.
        cwd_visible = _shorten_cwd(str(d.cwd))
        cwd_with_breaks = cwd_visible.replace("/", "/​")
        cwd_lbl = QLabel(f"📁  {cwd_with_breaks}")
        cwd_lbl.setStyleSheet(_STYLE_PREVIEW_BODY)
        cwd_lbl.setToolTip(f"{d.cwd}\nClick to open · Ctrl+O")
        cwd_lbl.setWordWrap(True)
        cwd_lbl.setMinimumWidth(0)
        cwd_lbl.setCursor(Qt.CursorShape.PointingHandCursor)
        cwd_lbl.mousePressEvent = lambda _: self._open_folder_current()
        cwd_h.addWidget(cwd_lbl, 1)
        cwd_open = QPushButton("↗")
        cwd_open.setStyleSheet(_STYLE_TEXT_LINK)
        cwd_open.setCursor(Qt.CursorShape.PointingHandCursor)
        cwd_open.setToolTip("Open folder · Ctrl+O")
        cwd_open.setFixedWidth(16)
        cwd_open.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        cwd_open.clicked.connect(self._open_folder_current)
        cwd_h.addWidget(cwd_open)
        cwd_row.register_reveal(cwd_open)
        self._preview_box.addWidget(cwd_row)

        # Branch names like ``feat/capsule-three-region-layout`` carry
        # natural break points at ``/`` and ``-`` — insert U+200B at
        # each so wordWrap can split there if the row would otherwise
        # overflow. Time string after the dot has spaces, no help
        # needed.
        branch_raw = d.git_branch or "—"
        branch = branch_raw.replace("/", "/​").replace("-", "-​")
        bt_lbl = QLabel(f"🌿  {branch}  ·  {_relative_time(d.last_activity)}")
        bt_lbl.setStyleSheet(_STYLE_PREVIEW_BODY)
        bt_lbl.setWordWrap(True)
        bt_lbl.setMinimumWidth(0)
        # No setToolTip — wordWrap already shows the full branch name
        # across multiple lines if needed (the U+200B break hints above
        # let it split at "/" and "-" boundaries). A hover tooltip would
        # just repeat what's already visible.
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

        # Resume now lives in a sticky footer outside the preview
        # ScrollArea (see _build_ui) — it stays put when the prompt
        # expands and never moves with content. Just re-target it at
        # the current selection here.
        self._update_resume_target(d.session_uuid)

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
