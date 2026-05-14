"""ApprovalCard widget — renders a single PendingDecisionView for
PreToolUse + emits Allow/Deny + remember on click.

Lives at the top of ExpandedWindow when WorldSnapshot.pending_decisions
is non-empty. Capped at 5 visible (caller decides; see ApprovalCardList).

Visual scheme:
  HIGH-risk tools   → red top bar + bold red "remember" warning
  MEDIUM-risk tools → yellow top bar + plain remember checkbox
  LOW-risk tools    → green top bar + plain remember checkbox

Buttons:
  ▶ Allow   (primary, blue) — emits ALLOW + remember-checkbox state
  ✕ Deny    (secondary)     — emits DENY with empty reason (UI keeps
                              v1 simple; future v2 may add a reason
                              text box for power users)

Threading: Qt main thread only; constructor + signal emit all happen
on the UI thread. The on_resolve callback is invoked synchronously
when the user clicks; AppBackend then routes to PendingDecisionRegistry.
"""
from __future__ import annotations

import logging
from typing import Callable

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QCheckBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from claude_island.core.pending_decisions import (
    Decision,
    DecisionResult,
    PendingDecisionView,
    RiskLevel,
)
from claude_island.ui.fonts import MONO_FONT_STACK, UI_FONT_STACK
from claude_island.ui.tooltip_style import TOOLTIP_QSS

log = logging.getLogger(__name__)


# Risk-driven color scheme. Top bar tint + warning text emphasis. Mirrors
# the existing capsule's status palette (green/yellow/red) so the user
# already knows the colour vocabulary.
_RISK_TOP_BAR_COLOR: dict[RiskLevel, str] = {
    RiskLevel.HIGH:   "#ef4444",  # red-500
    RiskLevel.MEDIUM: "#f59e0b",  # amber-500
    RiskLevel.LOW:    "#22c55e",  # green-500
}

# Warning text shown next to the "remember" checkbox for HIGH-risk tools.
# Lower risks hide the warning entirely (checkbox label alone suffices).
_HIGH_RISK_WARNING = (
    "⚠ This will allow ALL future Bash/Edit/Write calls "
    "in this session without asking."
)


# Callback signature: (decision_id, decision)
# Decision encapsulates Allow/Deny + remember + reason.
ResolveCallback = Callable[[str, Decision], None]


# ---------------------------------------------------------------------------
# QSS style — pulled out so test asserts on widget structure not strings.
# ---------------------------------------------------------------------------

_CARD_QSS = """
QFrame#approvalCard {
    background-color: #1f1f1f;
    border-radius: 8px;
    border: 1px solid #2a2a2a;
}
QFrame#approvalCardTopBar {
    border-top-left-radius: 8px;
    border-top-right-radius: 8px;
}
QLabel { color: #e8e8e8; }
QLabel#approvalCardTitle {
    font-family: """ + UI_FONT_STACK + """;
    font-size: 13px;
    font-weight: 600;
}
QLabel#approvalCardMeta {
    font-family: """ + UI_FONT_STACK + """;
    font-size: 11px;
    color: #999;
}
QLabel#approvalCardPreview {
    font-family: """ + MONO_FONT_STACK + """;
    font-size: 11px;
    color: #cdd2d8;
    background-color: #111;
    padding: 6px 8px;
    border-radius: 4px;
}
QLabel#approvalCardWarning {
    font-family: """ + UI_FONT_STACK + """;
    font-size: 10px;
    color: #f59e0b;
    font-weight: 600;
}
QPushButton#approvalAllow {
    background-color: #1d4ed8;
    color: white;
    border-radius: 6px;
    padding: 6px 14px;
    font-family: """ + UI_FONT_STACK + """;
    font-size: 12px;
    font-weight: 600;
    border: none;
}
QPushButton#approvalAllow:hover { background-color: #2563eb; }
QPushButton#approvalDeny {
    background-color: transparent;
    color: #d4d4d4;
    border-radius: 6px;
    padding: 6px 12px;
    font-family: """ + UI_FONT_STACK + """;
    font-size: 12px;
    border: 1px solid #404040;
}
QPushButton#approvalDeny:hover { background-color: #2a2a2a; }
QCheckBox#approvalRemember {
    color: #cdd2d8;
    font-family: """ + UI_FONT_STACK + """;
    font-size: 11px;
}
""" + TOOLTIP_QSS


class ApprovalCard(QFrame):
    """Renders one PreToolUse approval prompt.

    Constructor takes an immutable ``view``; the widget never re-reads
    the registry. When the user clicks Allow/Deny, the card emits
    ``on_resolve(view.id, decision)`` synchronously.
    """

    # Signal kept for callers that prefer Qt-style wiring; the
    # ``on_resolve`` constructor callback is the recommended path.
    resolved = Signal(str, object)   # (decision_id, Decision)

    def __init__(
        self,
        view: PendingDecisionView,
        *,
        on_resolve: ResolveCallback | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._view = view
        self._on_resolve = on_resolve
        self._build_ui()

    # ── public ──────────────────────────────────────────────────────────

    @property
    def view(self) -> PendingDecisionView:
        return self._view

    # ── internal ───────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        self.setObjectName("approvalCard")
        self.setStyleSheet(_CARD_QSS)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Maximum)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        # Top bar (risk indicator)
        top_bar = QFrame()
        top_bar.setObjectName("approvalCardTopBar")
        top_bar.setFixedHeight(3)
        top_bar.setStyleSheet(
            f"#approvalCardTopBar {{ background-color: "
            f"{_RISK_TOP_BAR_COLOR[self._view.risk_level]}; }}"
        )
        outer.addWidget(top_bar)

        body = QVBoxLayout()
        body.setContentsMargins(12, 10, 12, 10)
        body.setSpacing(8)

        # Title row: tool icon + name + session
        title = QLabel(self._format_title())
        title.setObjectName("approvalCardTitle")
        title.setTextInteractionFlags(Qt.TextInteractionFlag.NoTextInteraction)
        body.addWidget(title)

        meta = QLabel(self._format_meta())
        meta.setObjectName("approvalCardMeta")
        body.addWidget(meta)

        # Preview (the actual command / file path / etc.)
        preview = QLabel(self._view.tool_input_preview or "(no preview)")
        preview.setObjectName("approvalCardPreview")
        preview.setWordWrap(True)
        preview.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        body.addWidget(preview)

        # Remember checkbox + warning row
        remember_row = QHBoxLayout()
        remember_row.setContentsMargins(0, 4, 0, 4)
        self._remember = QCheckBox("Remember for this session")
        self._remember.setObjectName("approvalRemember")
        self._remember.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        remember_row.addWidget(self._remember)
        remember_row.addStretch(1)
        body.addLayout(remember_row)

        if self._view.risk_level is RiskLevel.HIGH:
            warning = QLabel(_HIGH_RISK_WARNING)
            warning.setObjectName("approvalCardWarning")
            warning.setWordWrap(True)
            body.addWidget(warning)

        # Buttons
        btn_row = QHBoxLayout()
        btn_row.setContentsMargins(0, 4, 0, 0)
        btn_row.setSpacing(8)
        btn_row.addStretch(1)

        deny_btn = QPushButton("Deny")
        deny_btn.setObjectName("approvalDeny")
        deny_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        deny_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        deny_btn.clicked.connect(self._on_deny)
        btn_row.addWidget(deny_btn)

        allow_btn = QPushButton("Allow")
        allow_btn.setObjectName("approvalAllow")
        allow_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        allow_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        allow_btn.clicked.connect(self._on_allow)
        btn_row.addWidget(allow_btn)

        body.addLayout(btn_row)
        outer.addLayout(body)

    def _format_title(self) -> str:
        tool = self._view.tool_name or "(unknown tool)"
        return f"{tool} — {self._view.session_name}"

    def _format_meta(self) -> str:
        return f"in {self._view.cwd_basename}"

    # ── handlers ────────────────────────────────────────────────────────

    def _on_allow(self) -> None:
        decision = Decision(
            result=DecisionResult.ALLOW,
            remember=self._remember.isChecked(),
        )
        self._emit(decision)

    def _on_deny(self) -> None:
        # v1 keeps Deny one-click — no reason text box. Empty reason
        # would violate Decision invariant; supply a sensible default.
        decision = Decision(
            result=DecisionResult.DENY,
            reason="denied by user",
        )
        self._emit(decision)

    def _emit(self, decision: Decision) -> None:
        try:
            if self._on_resolve is not None:
                self._on_resolve(self._view.id, decision)
            self.resolved.emit(self._view.id, decision)
        except Exception:
            log.exception("ApprovalCard.on_resolve raised")
