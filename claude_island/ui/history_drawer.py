"""HistoryDrawer — a sibling top-level window that lists offline sessions.

Lifecycle:
* Constructed once at app boot in __main__.py.
* Subscribes to ``world.observable()`` (via __main__) so it re-renders
  whenever a new ``WorldSnapshot`` is published.
* Initially hidden. Becomes visible when the user clicks the 🗂 N chip
  on ExpandedWindow's SESSIONS header, or hits Ctrl+H.
* Position is anchored to ExpandedWindow's right edge by default. If
  the right anchor would land off-screen (multi-monitor edge / panel
  on rightmost screen), falls back to the left anchor; if still off-
  screen, centers on the screen containing the panel.

Render contract:
* ``compute(snap) → key tuple`` — what we care about; piped through
  ``ops.distinct_until_changed(key_mapper=)`` so we don't re-render
  on snapshots that change unrelated fields.
* ``render(snap)`` — rebuild the row list from
  ``snap.dormant_sessions`` and ``snap.launching_sessions``.

Resume click flow (the heart of the feature):
* Build the cli command from ``DormantSession.permission_mode`` →
  ``--dangerously-skip-permissions`` etc.
* Ask the dispatcher which adapters can LAUNCH; pick the
  highest-priority one (v1) — future v2 can let the user pick.
* Call ``dispatcher.launch(adapter_name, cwd, command)``; on success,
  ``LaunchIntentRegistry.add(LaunchIntent(...))`` and
  ``snapshotter.wake()`` so the next snapshot moves the row from
  *dormant* → *launching*.
* Spawn failure → toast, do NOT touch the registry.

Visual design:
* Imports the canonical style tokens from ``expanded_window`` so the
  drawer reads as the same product surface as the main panel — same
  panel BG (#121212 @ 94 % via paintEvent), same row BG/hover
  (#1e1e1e / #2a2a2a), same title typography, same row height (52
  px), same two-line row anatomy as the live-session rows.
* Resume affordance is hover-revealed: a compact ``▶`` icon button
  appears in the row's bottom-right corner only when the user is
  pointing at the row. Right-click and full-row click both also
  trigger Resume so the small button isn't load-bearing for
  discoverability — it's a visual confirmation of an intent the row
  already invites.
"""
from __future__ import annotations

import logging
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

# Import canonical visual tokens from expanded_window so this drawer
# never drifts visually from the main panel. Cross-file in same UI layer
# is fine; the alternative (duplicating values) is a guaranteed source
# of style drift over time.
from claude_island.ui.expanded_window import (
    _BG_HOVER_SINGLE,
    _BG_PRESSED,
    _BG_SINGLE,
    _GROUP_OUTLINE_COLOR,
    _ROW_HEIGHT,
    _ROW_PAD_H,
    _STYLE_AGE,
    _STYLE_COST_DEFAULT,
    _STYLE_COST_HIGH,
    _STYLE_NAME,
    _STYLE_STATUS,
    _STYLE_TITLE,
    _ElidingLabel,
    _fmt_money,
)

log = logging.getLogger(__name__)


# Layout constants specific to this surface — kept here (not in a
# global config) because they only tune this window. Adjust if user
# reports it being cramped or wide.
_DRAWER_WIDTH = 360         # close to _PANEL_W=320 but slightly wider for
                            # the 2nd line that carries cwd+uuid+time
_DRAWER_GAP = 6             # px gap between expanded right edge and drawer;
                            # matches _GAP from expanded_window
_ROW_GAP = 4                # px between consecutive rows; tighter than
                            # main panel's _GROUP_GAP=8 because there's no
                            # group concept here, just a flat list
_HIGH_COST_USD = 50.0       # mirrors HIGH_COST_USD_THRESHOLD from core


def _flags_for_mode(mode: str | None) -> tuple[str, ...]:
    """Translate the captured permissionMode string into the cli flags
    we should pass to ``claude --resume``. Unknown / None → no flags
    (the user gets default permission mode)."""
    return {
        "bypassPermissions": ("--dangerously-skip-permissions",),
        "acceptEdits":       ("--permission-mode", "acceptEdits"),
        "plan":              ("--permission-mode", "plan"),
    }.get(mode or "", ())


def _relative_time(then: datetime, *, now: datetime | None = None) -> str:
    """Compact ago-string for the row's L2 metadata. Same conventions as
    the rest of the UI — minute-grained for <1h, then hours, then days."""
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


def _shorten_cwd(cwd_str: str, max_len: int = 40) -> str:
    """Mid-path elision so both the parent dir and the leaf survive.
    Uses ``…`` (single char) rather than ``...`` to match the rest of
    the panel's elision style produced by ``_ElidingLabel``."""
    if len(cwd_str) <= max_len:
        return cwd_str
    keep_tail = max_len // 2 + 3
    keep_head = max_len - keep_tail - 1
    return cwd_str[:keep_head] + "…" + cwd_str[-keep_tail:]


class _DispatcherProto:
    """Duck-typed view of TerminalDispatcher used here. UI doesn't import
    the platform_ class directly (import-linter forbids it); __main__
    injects an instance."""
    def adapters_with(self, cap): ...  # noqa: D401
    def launch(self, adapter_name, *, cwd, command): ...  # noqa: D401


# Resume button stylesheet — uses the same muted grey-on-dark palette as
# the main panel's header icon buttons, NOT a primary-coloured CTA. The
# affordance is "subtle action available on hover", not "look at me".
_STYLE_RESUME_BTN = f"""
    QPushButton {{
        color: #c9c9c9;
        background: {_BG_HOVER_SINGLE};
        border: 1px solid {_GROUP_OUTLINE_COLOR};
        border-radius: 6px;
        padding: 2px 8px;
        font-size: 11px;
    }}
    QPushButton:hover {{
        color: #ffffff;
        background: {_BG_PRESSED};
        border-color: #6b7280;
    }}
    QPushButton:disabled {{
        color: #6b7280;
        background: {_BG_SINGLE};
        border-color: {_GROUP_OUTLINE_COLOR};
    }}
"""


class _DormantRow(QPushButton):
    """One offline-session row — a click-target QPushButton mirroring
    the main panel's HoverRow anatomy: 52 px tall, two-line layout,
    same colour palette.

      Line 1: name (left)                           $cost (right)
      Line 2: cwd · time · uuid8       [▶ Resume]  (button hover-only)

    Click anywhere → Resume. Right-click reserved for future actions
    (copy uuid / open transcript) — currently no-op."""

    def __init__(
        self,
        *,
        dormant: DormantSession,
        dispatcher: _DispatcherProto,
        launch_intent: LaunchIntentRegistry,
        on_wake: Callable[[], None],
        on_toast: Callable[[str], None],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._dormant = dormant
        self._dispatcher = dispatcher
        self._launch_intent = launch_intent
        self._on_wake = on_wake
        self._on_toast = on_toast

        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setFixedHeight(_ROW_HEIGHT)
        self.setStyleSheet(
            f"""
            QPushButton {{
                background: {_BG_SINGLE};
                border: none;
                border-radius: 8px;
                text-align: left;
                padding: 0;
            }}
            QPushButton:hover {{ background: {_BG_HOVER_SINGLE}; }}
            QPushButton:pressed {{ background: {_BG_PRESSED}; }}
            """
        )
        self.setAttribute(Qt.WidgetAttribute.WA_Hover, True)
        self.clicked.connect(self._on_resume)
        self.installEventFilter(self)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(_ROW_PAD_H, 6, _ROW_PAD_H, 6)
        outer.setSpacing(2)

        # ── L1: name + bypass shield + $cost ─────────────────────────
        l1 = QHBoxLayout()
        l1.setContentsMargins(0, 0, 0, 0)
        l1.setSpacing(6)

        name_lbl = _ElidingLabel(self._row_name())
        name_lbl.setStyleSheet(_STYLE_NAME)
        name_lbl.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        name_lbl.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred,
        )
        l1.addWidget(name_lbl, 1)

        if dormant.permission_mode == "bypassPermissions":
            shield = QLabel("\U0001f6e1")
            shield.setToolTip(
                "This session ran with --dangerously-skip-permissions"
            )
            shield.setStyleSheet("color: #f59e0b; font-size: 11px;")
            shield.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
            l1.addWidget(shield)

        cost_lbl = QLabel(_fmt_money(dormant.cost_usd))
        cost_lbl.setStyleSheet(
            _STYLE_COST_HIGH if dormant.cost_usd >= _HIGH_COST_USD
            else _STYLE_COST_DEFAULT
        )
        cost_lbl.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        cost_lbl.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )
        l1.addWidget(cost_lbl)
        outer.addLayout(l1)

        # ── L2: cwd · time · uuid8       [Resume] ────────────────────
        l2 = QHBoxLayout()
        l2.setContentsMargins(0, 0, 0, 0)
        l2.setSpacing(6)

        # cwd · time · uuid8 — stuffed into a single eliding label so
        # the line collapses gracefully under width pressure.
        cwd_short = _shorten_cwd(str(dormant.cwd))
        meta_text = (
            f"{cwd_short} · "
            f"{_relative_time(dormant.last_activity)} · "
            f"{dormant.session_uuid[:8]}"
        )
        meta_lbl = _ElidingLabel(meta_text)
        meta_lbl.setStyleSheet(_STYLE_AGE)
        meta_lbl.setToolTip(f"{dormant.cwd}\nFull uuid: {dormant.session_uuid}")
        meta_lbl.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        meta_lbl.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred,
        )
        l2.addWidget(meta_lbl, 1)

        # Resume affordance — hidden until the row is hovered; on
        # mouse leave it goes back into hiding so idle rows have a
        # clean two-line look. Click handler routes to _on_resume.
        flags = _flags_for_mode(dormant.permission_mode)
        command_preview = "claude --resume " + dormant.session_uuid
        if flags:
            command_preview += " " + " ".join(flags)
        self._resume_button = QPushButton("▶ Resume")  # ▶
        self._resume_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self._resume_button.setStyleSheet(_STYLE_RESUME_BTN)
        self._resume_button.setVisible(False)
        self._resume_button.setToolTip(
            f"Resume in new terminal\n"
            f"──────────\n"
            f"cwd:\n  {dormant.cwd}\n\n"
            f"command:\n  {command_preview}\n\n"
            f"launcher: {self._launcher_name_hint()}"
        )
        self._resume_button.clicked.connect(self._on_resume)
        l2.addWidget(self._resume_button)
        outer.addLayout(l2)

    # ── hover state — show/hide the resume button ─────────────────────

    def eventFilter(self, watched, event) -> bool:  # type: ignore[override]
        # We use eventFilter rather than enterEvent/leaveEvent so the
        # base QPushButton's own hover painting is not disturbed.
        if watched is self:
            if event.type() == QEvent.Type.HoverEnter:
                self._resume_button.setVisible(True)
            elif event.type() == QEvent.Type.HoverLeave:
                # Don't hide while the button itself is the hover target
                # — Qt toggles HoverEnter on child first, then HoverLeave
                # on parent, which would flicker the button. The button
                # remaining a child of self means hovering over the
                # button keeps the row's :hover state too, so this works
                # in practice.
                self._resume_button.setVisible(False)
        return super().eventFilter(watched, event)

    # ── internal ───────────────────────────────────────────────────────

    def _row_name(self) -> str:
        d = self._dormant
        if d.name and d.name.strip():
            return d.name.strip()
        if d.last_prompt and d.last_prompt.strip():
            preview = d.last_prompt.strip().splitlines()[0]
            return (preview[:40] + "…") if len(preview) > 40 else preview
        return "Untitled session"

    def _launcher_name_hint(self) -> str:
        try:
            cands = self._dispatcher.adapters_with(Capability.LAUNCH)
        except Exception:
            cands = ()
        return cands[0][0] if cands else "(none)"

    def _on_resume(self) -> None:
        d = self._dormant
        flags = _flags_for_mode(d.permission_mode)
        command = ("claude", "--resume", d.session_uuid, *flags)

        candidates = self._dispatcher.adapters_with(Capability.LAUNCH)
        if not candidates:
            self._on_toast(
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
            self._on_toast(f"Failed to launch: {e}")
            return
        except Exception:
            log.exception("dispatcher.launch raised unexpectedly")
            self._on_toast("Failed to launch (see logs)")
            return

        self._launch_intent.add(LaunchIntent(
            session_uuid=d.session_uuid,
            cwd=d.cwd,
            flags=flags,
            terminal_name=result.terminal_name,
            terminal_pid=result.terminal_pid,
            requested_at=result.started_at,
        ))
        # Optimistic UI: disable button so a frantic double-click can't
        # spawn two terminals while we wait for the next snapshot.
        self._resume_button.setEnabled(False)
        self._resume_button.setText("⏳ Launching…")
        self.setEnabled(False)
        self._on_wake()


class _LaunchingRow(QFrame):
    """A row that's mid-launch — waiting for ProcessScanner to detect
    the new claude.exe. Same 52 px row geometry as _DormantRow but with
    a subtle running-accent BG so it visually reads "in flight"."""

    def __init__(
        self,
        *,
        intent: LaunchIntent,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setFixedHeight(_ROW_HEIGHT)
        self.setStyleSheet(
            f"""
            QFrame {{
                background: {_BG_SINGLE};
                border: 1px solid {_GROUP_OUTLINE_COLOR};
                border-radius: 8px;
            }}
            """
        )

        outer = QVBoxLayout(self)
        outer.setContentsMargins(_ROW_PAD_H, 6, _ROW_PAD_H, 6)
        outer.setSpacing(2)

        title_row = QHBoxLayout()
        title_row.setContentsMargins(0, 0, 0, 0)
        title_row.setSpacing(6)
        title = QLabel(f"⏳ Launching {intent.session_uuid[:8]}…")
        title.setStyleSheet(_STYLE_NAME)
        title_row.addWidget(title, 1)
        outer.addLayout(title_row)

        sub = QLabel(
            f"{intent.terminal_name} pid {intent.terminal_pid} · "
            f"{_shorten_cwd(str(intent.cwd))}"
        )
        sub.setStyleSheet(_STYLE_STATUS)
        outer.addWidget(sub)


class HistoryDrawer(QWidget):
    """Top-level frameless window snapped to ExpandedWindow's edge.

    Visually a sibling of the main panel: same paint-event-drawn body,
    same row palette, same title typography. The user's mental model
    is "one product surface with two columns" — never "main app + a
    separate widget panel"."""

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
        self._prev_launching: set[str] = set()
        self._search_query: str = ""

        # Same flag combo as ExpandedWindow / SessionDetailPopup.
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setFixedWidth(_DRAWER_WIDTH)
        self.setMinimumHeight(300)

        self._build_ui()

        # Esc closes the drawer (does NOT propagate to parent panel).
        QShortcut(QKeySequence(Qt.Key.Key_Escape), self, self.hide)

    # ── public API ─────────────────────────────────────────────────────

    @staticmethod
    def compute(snap: WorldSnapshot):
        """``distinct_until_changed`` key projection. Just the bits the
        drawer cares about."""
        return (
            tuple((d.session_uuid, d.last_activity, d.cost_usd)
                  for d in snap.dormant_sessions),
            tuple((i.session_uuid, i.terminal_pid)
                  for i in snap.launching_sessions),
        )

    def render(self, snap: WorldSnapshot) -> None:
        self._render_rows(snap)
        # Detect launching → gone-without-becoming-live (timeout) and
        # toast the user — better than letting them wonder why the
        # row reappeared as dormant.
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

    # ── UI build ───────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        # No outer frame — the body is painted directly via paintEvent
        # to mirror ExpandedWindow exactly.
        body = QVBoxLayout(self)
        body.setContentsMargins(14, 14, 14, 14)
        body.setSpacing(8)

        # ── Header (matches main panel section title typography) ────
        header = QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        title = QLabel("HISTORY")
        title.setStyleSheet(_STYLE_TITLE)
        header.addWidget(title)
        header.addStretch(1)
        self._count_label = QLabel("")
        self._count_label.setStyleSheet(_STYLE_AGE)
        header.addWidget(self._count_label)
        body.addLayout(header)

        # ── Search ──────────────────────────────────────────────────
        self._search = QLineEdit()
        self._search.setPlaceholderText("Search title / cwd / uuid")
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
        body.addWidget(self._search)

        # ── Scroll area for rows ────────────────────────────────────
        self._scroll = QScrollArea(self)
        self._scroll.setWidgetResizable(True)
        self._scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self._scroll.setVerticalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAsNeeded
        )
        self._scroll.setFrameShape(QFrame.Shape.NoFrame)
        # Same scroll-bar styling as expanded panel's session scroll.
        self._scroll.setStyleSheet(
            "QScrollArea { background: transparent; border: none; }"
            "QScrollBar:vertical { background: transparent; width: 6px; "
            "  margin: 0; }"
            "QScrollBar::handle:vertical { background: #3a3a3a; "
            "  border-radius: 3px; min-height: 20px; }"
            "QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical "
            "  { height: 0; }"
        )
        self._rows_container = QWidget()
        self._rows_container.setStyleSheet("background: transparent;")
        self._rows_box = QVBoxLayout(self._rows_container)
        self._rows_box.setSpacing(_ROW_GAP)
        self._rows_box.setContentsMargins(0, 0, 0, 0)
        self._rows_box.addStretch(1)
        self._scroll.setWidget(self._rows_container)
        body.addWidget(self._scroll)

        # ── Toast (hidden at rest) ──────────────────────────────────
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

    # ── paintEvent — match ExpandedWindow body ─────────────────────

    def paintEvent(self, event: object) -> None:  # type: ignore[override]
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        path = QPainterPath()
        path.addRoundedRect(0, 0, self.width(), self.height(), 16, 16)
        # Same ink as ExpandedWindow.paintEvent — keeps the two
        # surfaces visually indistinguishable when sat side by side.
        painter.fillPath(path, QColor(18, 18, 18, 240))

    # ── render / reposition ────────────────────────────────────────────

    def _render_rows(self, snap: WorldSnapshot) -> None:
        # Clear all rows except the trailing stretch.
        while self._rows_box.count() > 1:
            item = self._rows_box.takeAt(0)
            w = item.widget()
            if w is not None:
                w.setParent(None)
                w.deleteLater()

        launching = snap.launching_sessions
        dormant_all = snap.dormant_sessions
        dormant = self._apply_filter(dormant_all)
        # Newest first — matches "find what I was just working on".
        dormant_sorted = sorted(
            dormant, key=lambda d: d.last_activity, reverse=True,
        )
        self._count_label.setText(
            f"{len(dormant_all)}"
            + (f" · {len(launching)} launching" if launching else "")
        )

        # Launching rows first (active intents the user just kicked off).
        for intent in launching:
            self._rows_box.insertWidget(
                self._rows_box.count() - 1,
                _LaunchingRow(intent=intent, parent=self._rows_container),
            )

        if not dormant_sorted and not launching:
            empty = QLabel("No history yet.\nOffline sessions will appear here.")
            empty.setStyleSheet(_STYLE_AGE + " padding: 20px;")
            empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self._rows_box.insertWidget(self._rows_box.count() - 1, empty)
            return

        for d in dormant_sorted:
            row = _DormantRow(
                dormant=d,
                dispatcher=self._dispatcher,
                launch_intent=self._launch_intent,
                on_wake=self._on_wake,
                on_toast=self._show_toast,
                parent=self._rows_container,
            )
            self._rows_box.insertWidget(self._rows_box.count() - 1, row)

    def _apply_filter(self, dormant: tuple[DormantSession, ...]) -> list[DormantSession]:
        q = self._search_query.strip().lower()
        if not q:
            return list(dormant)
        out: list[DormantSession] = []
        for d in dormant:
            haystack = " ".join([
                (d.name or "").lower(),
                str(d.cwd).lower(),
                d.session_uuid.lower(),
                (d.last_prompt or "").lower(),
            ])
            if q in haystack:
                out.append(d)
        return out

    def _on_search_changed(self, text: str) -> None:
        self._search_query = text
        self._on_wake()

    def _show_toast(self, msg: str) -> None:
        self._toast.setText(msg)
        self._toast.setVisible(True)
        self._toast_timer.start(6000)

    # ── positioning (multi-monitor aware) ─────────────────────────

    def _reposition(self) -> None:
        """Anchor the drawer next to the expanded panel.

        Strategy (in order):
          1. Right of expanded — preferred ("fly-out" feel).
          2. If the right anchor would push the drawer past the
             screen's right edge, dock to the LEFT instead.
          3. If neither side fits (panel taking the whole screen
             width somehow), center the drawer over the panel's
             screen — it'll overlap, but the user can still see it.
        """
        try:
            geo = self._expanded.frameGeometry()
        except Exception:
            return

        # The screen the EXPANDED panel is on (handles multi-monitor).
        screen = self._screen_for_geometry(geo)
        screen_geo = screen.availableGeometry() if screen else None

        height = max(geo.height(), 300)
        # Capped at the host screen's available height so the drawer
        # never grows below the taskbar.
        if screen_geo is not None:
            height = min(height, screen_geo.height())

        right_x = geo.right() + _DRAWER_GAP
        left_x = geo.left() - _DRAWER_GAP - _DRAWER_WIDTH
        y = geo.top()

        chosen_x = right_x
        if screen_geo is not None:
            fits_right = right_x + _DRAWER_WIDTH <= screen_geo.right() + 1
            fits_left = left_x >= screen_geo.left() - 1
            if not fits_right and fits_left:
                chosen_x = left_x
            elif not fits_right and not fits_left:
                # Centre over the expanded screen — last-resort.
                chosen_x = screen_geo.x() + (
                    (screen_geo.width() - _DRAWER_WIDTH) // 2
                )
            # Clamp y so the drawer doesn't run off the bottom edge.
            if y + height > screen_geo.bottom():
                y = max(screen_geo.top(), screen_geo.bottom() - height)
            if y < screen_geo.top():
                y = screen_geo.top()

        self.setMinimumHeight(0)
        self.resize(_DRAWER_WIDTH, height)
        self.move(QPoint(chosen_x, y))

    @staticmethod
    def _screen_for_geometry(geo) -> object | None:
        """Find the QScreen whose geometry contains the centre of
        ``geo``. Falls back to the primary screen if none matches —
        better an offset drawer than a crash."""
        try:
            centre = geo.center()
            for s in QGuiApplication.screens():
                if s.geometry().contains(centre):
                    return s
            return QGuiApplication.primaryScreen()
        except Exception:
            return None
