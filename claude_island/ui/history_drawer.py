"""HistoryDrawer — a sibling top-level window that lists offline sessions.

Lifecycle:
* Constructed once at app boot in __main__.py.
* Subscribes to ``world.observable()`` (via __main__) so it re-renders
  whenever a new ``WorldSnapshot`` is published.
* Initially hidden. Becomes visible when the user clicks the 🗂 N chip
  on ExpandedWindow's SESSIONS header, or hits Ctrl+H.
* Stays positioned snug to ExpandedWindow's right edge — re-positioned
  in :meth:`_position` whenever the parent moves or this window opens.

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
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Callable

from PySide6.QtCore import QPoint, Qt, QTimer
from PySide6.QtGui import QShortcut, QKeySequence
from PySide6.QtWidgets import (
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

log = logging.getLogger(__name__)


# Layout constants — kept here (not in a global config) because they only
# tune this window. Adjust if the user reports it being cramped or wide.
_DRAWER_WIDTH = 460
_DRAWER_GAP = 8           # px between expanded panel right edge and drawer
_ROW_HEIGHT_HINT = 64     # tall enough for 3 lines of body text
_ROW_GAP = 6


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
    """Compact ago-string for the row's L3 metadata. Same conventions as
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


class _DispatcherProto:
    """Duck-typed view of TerminalDispatcher used here. UI doesn't import
    the platform_ class directly (import-linter forbids it); __main__
    injects an instance."""
    def adapters_with(self, cap): ...  # noqa: D401
    def launch(self, adapter_name, *, cwd, command): ...  # noqa: D401


class _ResumeButton(QPushButton):
    """The ▶ Resume button — visual + hover tooltip carrying the full
    command preview so the user sees what's about to run."""

    def __init__(
        self,
        *,
        cwd: str,
        command_preview: str,
        launcher_name: str,
        on_click: Callable[[], None],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__("▶ Resume", parent)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setStyleSheet(
            "QPushButton {"
            "  background: #2563eb;"
            "  color: #ffffff;"
            "  border: none;"
            "  border-radius: 6px;"
            "  padding: 6px 12px;"
            "  font-size: 12px;"
            "}"
            "QPushButton:hover { background: #1d4ed8; }"
            "QPushButton:pressed { background: #1e40af; }"
            "QPushButton:disabled { background: #475569; color: #94a3b8; }"
        )
        # Tooltip is the trust handshake: shows the user exactly the
        # cwd we'll cd to, the cli we'll run, and the launcher we'll use.
        self.setToolTip(
            f"Resume in new terminal\n"
            f"──────────\n"
            f"cwd:\n  {cwd}\n\n"
            f"command:\n  {command_preview}\n\n"
            f"launcher: {launcher_name}"
        )
        self.clicked.connect(on_click)


class _DormantRow(QFrame):
    """One offline-session row in the drawer.

    Three-line layout:
      L1: name (custom > ai_title > 'Untitled' fallback) + 🛡 if bypass
      L2: cwd (greyed, ElidingLabel-like) — inline string elision
      L3: $cost · relative time · uuid prefix · [▶ Resume]
    """

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

        self.setStyleSheet(
            "QFrame {"
            "  background: rgba(255,255,255,0.04);"
            "  border: 1px solid rgba(255,255,255,0.08);"
            "  border-radius: 8px;"
            "}"
            "QFrame:hover { background: rgba(255,255,255,0.07); }"
        )
        self.setMinimumHeight(_ROW_HEIGHT_HINT)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 8, 10, 8)
        layout.setSpacing(2)

        # L1 — name + bypass shield
        name = self._row_name()
        l1 = QHBoxLayout()
        l1.setContentsMargins(0, 0, 0, 0)
        l1.setSpacing(4)
        name_lbl = QLabel(name)
        name_lbl.setStyleSheet("color: #e8e8e8; font-size: 13px; font-weight: 600;")
        l1.addWidget(name_lbl)
        l1.addStretch(1)
        if dormant.permission_mode == "bypassPermissions":
            shield = QLabel("\U0001f6e1")  # 🛡
            shield.setToolTip("This session ran with --dangerously-skip-permissions")
            shield.setStyleSheet("color: #f59e0b; font-size: 12px;")
            l1.addWidget(shield)
        layout.addLayout(l1)

        # L2 — cwd (greyed monospace)
        cwd_str = str(dormant.cwd)
        # Light client-side elision: truncate long paths in the middle so
        # both the parent dir and the leaf are visible.
        if len(cwd_str) > 56:
            cwd_str = cwd_str[:24] + " … " + cwd_str[-28:]
        cwd_lbl = QLabel(cwd_str)
        cwd_lbl.setStyleSheet(
            "color: #8a8a8a; font-size: 10px; font-family: Consolas, monospace;"
        )
        layout.addWidget(cwd_lbl)

        # L3 — $cost · time · uuid prefix · [Resume]
        l3 = QHBoxLayout()
        l3.setContentsMargins(0, 0, 0, 0)
        l3.setSpacing(8)

        meta_text = (
            f"${dormant.cost_usd:.2f} · "
            f"{_relative_time(dormant.last_activity)} · "
            f"{dormant.session_uuid[:8]}"
        )
        meta_lbl = QLabel(meta_text)
        meta_lbl.setStyleSheet("color: #9aa0a6; font-size: 11px;")
        meta_lbl.setToolTip(f"Full uuid: {dormant.session_uuid}")
        l3.addWidget(meta_lbl)
        l3.addStretch(1)

        flags = _flags_for_mode(dormant.permission_mode)
        command_preview = "claude --resume " + dormant.session_uuid
        if flags:
            command_preview += " " + " ".join(flags)
        self._resume_button = _ResumeButton(
            cwd=str(dormant.cwd),
            command_preview=command_preview,
            launcher_name=self._launcher_name_hint(),
            on_click=self._on_resume,
        )
        l3.addWidget(self._resume_button)
        layout.addLayout(l3)

    # ── internal ───────────────────────────────────────────────────────

    def _row_name(self) -> str:
        d = self._dormant
        if d.name and d.name.strip():
            return d.name.strip()
        if d.last_prompt and d.last_prompt.strip():
            preview = d.last_prompt.strip().splitlines()[0]
            return (preview[:30] + "…") if len(preview) > 30 else preview
        return "Untitled session"

    def _launcher_name_hint(self) -> str:
        """First available launcher name, or '(none)' if dispatcher
        has no LAUNCH-capable adapter. Cached per-row so the tooltip
        rendered at construction time matches what we'd actually use
        when the button is clicked. If the chain changes between
        construction and click (extremely rare in practice — adapters
        register at import time, never after) the click handler does a
        fresh lookup anyway, so the worst case is a stale tooltip."""
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
        # Optimistic UI: disable button immediately so a frantic
        # double-click doesn't spawn two terminals while we wait for
        # the next snapshot to render.
        self._resume_button.setEnabled(False)
        self._resume_button.setText("⏳ Launching…")
        self._on_wake()


class _LaunchingRow(QFrame):
    """A row that's mid-launch — shown while we wait for ProcessScanner
    to detect the new claude.exe (or for the LaunchIntent to time out)."""

    def __init__(
        self,
        *,
        intent: LaunchIntent,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setStyleSheet(
            "QFrame {"
            "  background: rgba(37,99,235,0.10);"
            "  border: 1px solid rgba(37,99,235,0.35);"
            "  border-radius: 8px;"
            "}"
        )
        self.setMinimumHeight(48)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 6, 10, 6)
        layout.setSpacing(2)

        title = QLabel(
            f"⏳ Launching {intent.session_uuid[:8]}…"
        )
        title.setStyleSheet("color: #e8e8e8; font-size: 12px; font-weight: 600;")
        layout.addWidget(title)

        sub = QLabel(
            f"{intent.terminal_name} pid {intent.terminal_pid} · {intent.cwd}"
        )
        sub.setStyleSheet("color: #9aa0a6; font-size: 10px;")
        layout.addWidget(sub)


class HistoryDrawer(QWidget):
    """Top-level frameless window snapped to ExpandedWindow's right edge."""

    def __init__(
        self,
        *,
        expanded: QWidget,
        dispatcher: _DispatcherProto,
        launch_intent: LaunchIntentRegistry,
        on_wake: Callable[[], None],
    ) -> None:
        # parent=None so this is a separate top-level window with its own
        # taskbar absence (Tool flag).
        super().__init__(None)
        self._expanded = expanded
        self._dispatcher = dispatcher
        self._launch_intent = launch_intent
        self._on_wake = on_wake
        # Track previously-seen launching uuids so we can detect transitions
        # back to dormant (i.e. timed-out launches) and surface a toast.
        self._prev_launching: set[str] = set()
        # Free-text filter; updated by the search box at top.
        self._search_query: str = ""

        # Same flag combo as ExpandedWindow (frameless, top-most, tool window
        # so it doesn't appear in the taskbar / Cmd+Tab).
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
        drawer cares about — avoids re-rendering on unrelated snapshot
        churn (e.g. quota tick)."""
        return (
            tuple((d.session_uuid, d.last_activity, d.cost_usd) for d in snap.dormant_sessions),
            tuple((i.session_uuid, i.terminal_pid) for i in snap.launching_sessions),
        )

    def render(self, snap: WorldSnapshot) -> None:
        """Rebuild the row list. Cheap at the user's scale (≤200 dormant
        sessions on a long-time-user machine; rebuild ≈ ms)."""
        self._render_rows(snap)
        # Detect launching → gone-without-becoming-live transitions and
        # toast them. Comparing prev vs current launching uuids is
        # enough — if the uuid disappeared from launching, reconcile
        # either upgraded it (ok, don't toast) or expired it (toast).
        # We can tell which by checking live_uuids in the snap.
        live_uuids = {
            v.session_uuid for g in snap.session_groups for v in g.views
            if v.session_uuid
        }
        cur = {i.session_uuid: i for i in snap.launching_sessions}
        for uuid in self._prev_launching - cur.keys():
            if uuid in live_uuids:
                continue  # upgraded — silent success
            # Find the intent we lost so we can include terminal info.
            # We don't have it any more (intent was discarded by reconcile),
            # so the toast is generic. Acceptable — the user knows which
            # one they just clicked.
            self._show_toast(
                f"Couldn't detect new claude session for {uuid[:8]}. "
                "Check the new terminal window."
            )
        self._prev_launching = set(cur.keys())

    def toggle(self) -> None:
        """Show if hidden, hide if visible. Wired to Ctrl+H + chip click."""
        if self.isVisible():
            self.hide()
        else:
            self._reposition()
            self.show()
            self.raise_()

    # ── UI build ───────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        # Single rounded card containing search + scroll.
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)

        card = QFrame(self)
        card.setStyleSheet(
            "QFrame {"
            "  background: #1e1f22;"
            "  border: 1px solid rgba(255,255,255,0.08);"
            "  border-radius: 12px;"
            "}"
        )
        outer.addWidget(card)

        body = QVBoxLayout(card)
        body.setContentsMargins(14, 14, 14, 14)
        body.setSpacing(8)

        # Header
        header = QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        title = QLabel("HISTORY")
        title.setStyleSheet("color: #cfcfcf; font-size: 11px; font-weight: 600; letter-spacing: 1.5px;")
        header.addWidget(title)
        header.addStretch(1)
        self._count_label = QLabel("")
        self._count_label.setStyleSheet("color: #9aa0a6; font-size: 11px;")
        header.addWidget(self._count_label)
        body.addLayout(header)

        # Search input
        self._search = QLineEdit()
        self._search.setPlaceholderText("Search title / cwd / uuid")
        self._search.setStyleSheet(
            "QLineEdit {"
            "  background: rgba(255,255,255,0.05);"
            "  color: #e8e8e8;"
            "  border: 1px solid rgba(255,255,255,0.08);"
            "  border-radius: 6px;"
            "  padding: 5px 8px;"
            "  font-size: 11px;"
            "}"
            "QLineEdit:focus { border: 1px solid #2563eb; }"
        )
        self._search.textChanged.connect(self._on_search_changed)
        body.addWidget(self._search)

        # Scroll area for rows
        self._scroll = QScrollArea(card)
        self._scroll.setWidgetResizable(True)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self._scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._scroll.setStyleSheet(
            "QScrollArea { background: transparent; border: none; }"
            "QScrollBar:vertical { background: transparent; width: 6px; margin: 0; }"
            "QScrollBar::handle:vertical { background: #3a3a3a; border-radius: 3px; min-height: 20px; }"
            "QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }"
        )
        self._rows_container = QWidget()
        self._rows_container.setStyleSheet("background: transparent;")
        self._rows_box = QVBoxLayout(self._rows_container)
        self._rows_box.setSpacing(_ROW_GAP)
        self._rows_box.setContentsMargins(0, 0, 0, 0)
        self._rows_box.addStretch(1)
        self._scroll.setWidget(self._rows_container)
        body.addWidget(self._scroll)

        # Toast bar (hidden by default; appears at the bottom when needed)
        self._toast = QLabel("")
        self._toast.setStyleSheet(
            "QLabel {"
            "  background: rgba(220, 38, 38, 0.18);"
            "  color: #fecaca;"
            "  border: 1px solid rgba(220, 38, 38, 0.4);"
            "  border-radius: 6px;"
            "  padding: 6px 10px;"
            "  font-size: 11px;"
            "}"
        )
        self._toast.setWordWrap(True)
        self._toast.setVisible(False)
        body.addWidget(self._toast)
        self._toast_timer = QTimer(self)
        self._toast_timer.setSingleShot(True)
        self._toast_timer.timeout.connect(lambda: self._toast.setVisible(False))

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
        # Newest first — matches "find what I was just working on" intent.
        dormant_sorted = sorted(
            dormant, key=lambda d: d.last_activity, reverse=True,
        )
        self._count_label.setText(
            f"{len(dormant_all)} sessions"
            + (f" · {len(launching)} launching" if launching else "")
        )

        # Launching rows first (they're the user's active intent).
        for intent in launching:
            self._rows_box.insertWidget(
                self._rows_box.count() - 1,
                _LaunchingRow(intent=intent, parent=self._rows_container),
            )

        if not dormant_sorted and not launching:
            empty = QLabel("No history yet.\nOffline sessions will appear here.")
            empty.setStyleSheet("color: #6b7280; font-size: 11px; padding: 20px;")
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
        # Re-render with the latest snapshot if we have one — search
        # filtering is purely client-side, no need to wake snapshotter.
        # We don't keep the snap around, so re-trigger a wake which will
        # publish + we'll re-render via the subscription. Cheap.
        self._on_wake()

    def _show_toast(self, msg: str) -> None:
        self._toast.setText(msg)
        self._toast.setVisible(True)
        self._toast_timer.start(6000)

    def _reposition(self) -> None:
        """Snap the drawer to expanded panel's right edge.

        Uses the panel's frameGeometry so the position math accounts for
        its window border (currently 0 — frameless — but resilient to
        future changes)."""
        try:
            geo = self._expanded.frameGeometry()
        except Exception:
            return
        x = geo.right() + _DRAWER_GAP
        y = geo.top()
        height = max(geo.height(), 300)
        self.setMinimumHeight(0)  # allow shrinking
        self.resize(_DRAWER_WIDTH, height)
        self.move(QPoint(x, y))
