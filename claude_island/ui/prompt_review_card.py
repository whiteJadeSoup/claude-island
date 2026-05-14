"""PromptReviewCard widget — renders a single PendingDecisionView for
UserPromptSubmit when per-session "Review prompts" toggle is ON.

Three primary actions:
  ▶ Allow            — pass the prompt through unchanged
  ✕ Block(reason)    — refuse the prompt, claude shows reason to user
  ⊕ Inject(context)  — let the prompt through + add additionalContext

Per Claude Code spec, prompt **rewriting** is NOT supported — directives
only allow block + additionalContext injection. The card surfaces only
what the spec allows.

Reason / Inject text input: a single QPlainTextEdit that toggles its
visibility based on which mode the user picked. Block requires non-empty
text; Inject ditto. Allow has no text needed.
"""
from __future__ import annotations

import logging
from typing import Callable

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from claude_island.core.pending_decisions import (
    Decision,
    DecisionResult,
    PendingDecisionView,
)
from claude_island.ui.fonts import MONO_FONT_STACK, UI_FONT_STACK
from claude_island.ui.tooltip_style import TOOLTIP_QSS

log = logging.getLogger(__name__)


ResolveCallback = Callable[[str, Decision], None]


_CARD_QSS = """
QFrame#promptReviewCard {
    background-color: #1f1f1f;
    border-radius: 8px;
    border: 1px solid #2a2a2a;
}
QFrame#promptReviewTopBar {
    background-color: #6366f1;  /* indigo-500 — distinct from approval cards */
    border-top-left-radius: 8px;
    border-top-right-radius: 8px;
}
QLabel { color: #e8e8e8; }
QLabel#promptReviewTitle {
    font-family: """ + UI_FONT_STACK + """;
    font-size: 13px;
    font-weight: 600;
}
QLabel#promptReviewMeta {
    font-family: """ + UI_FONT_STACK + """;
    font-size: 11px;
    color: #999;
}
QLabel#promptReviewBody {
    font-family: """ + MONO_FONT_STACK + """;
    font-size: 11px;
    color: #cdd2d8;
    background-color: #111;
    padding: 8px 10px;
    border-radius: 4px;
}
QPlainTextEdit#promptReviewInput {
    background-color: #111;
    color: #e8e8e8;
    border: 1px solid #404040;
    border-radius: 4px;
    padding: 4px 6px;
    font-family: """ + MONO_FONT_STACK + """;
    font-size: 11px;
}
QPushButton {
    border-radius: 6px;
    padding: 6px 12px;
    font-family: """ + UI_FONT_STACK + """;
    font-size: 12px;
    border: 1px solid transparent;
}
QPushButton#promptAllow {
    background-color: #1d4ed8;
    color: white;
    border: none;
    font-weight: 600;
}
QPushButton#promptAllow:hover { background-color: #2563eb; }
QPushButton#promptBlock {
    background-color: transparent;
    color: #f87171;
    border-color: #404040;
}
QPushButton#promptBlock:hover { background-color: #2a1010; }
QPushButton#promptInject {
    background-color: transparent;
    color: #c4b5fd;
    border-color: #404040;
}
QPushButton#promptInject:hover { background-color: #1a1530; }
""" + TOOLTIP_QSS


class PromptReviewCard(QFrame):
    """One UserPromptSubmit review card.

    Default mode shows three primary buttons. Click Block or Inject to
    expand the inline text input. Re-clicking the same button submits;
    Allow is one-click.
    """

    resolved = Signal(str, object)  # (decision_id, Decision)

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
        # "armed" = which mode the user picked but hasn't confirmed yet.
        # None ⇒ default state (no input visible). When armed=BLOCK or
        # INJECT, the corresponding input box is visible and the button
        # acts as Submit.
        self._armed: DecisionResult | None = None
        self._build_ui()

    @property
    def view(self) -> PendingDecisionView:
        return self._view

    # ── UI ──────────────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        self.setObjectName("promptReviewCard")
        self.setStyleSheet(_CARD_QSS)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Maximum)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        top_bar = QFrame()
        top_bar.setObjectName("promptReviewTopBar")
        top_bar.setFixedHeight(3)
        outer.addWidget(top_bar)

        body = QVBoxLayout()
        body.setContentsMargins(12, 10, 12, 10)
        body.setSpacing(8)

        title = QLabel(f"Review prompt — {self._view.session_name}")
        title.setObjectName("promptReviewTitle")
        body.addWidget(title)

        meta = QLabel(f"in {self._view.cwd_basename}")
        meta.setObjectName("promptReviewMeta")
        body.addWidget(meta)

        prompt_body = QLabel(self._view.prompt_preview or "(empty prompt)")
        prompt_body.setObjectName("promptReviewBody")
        prompt_body.setWordWrap(True)
        prompt_body.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        body.addWidget(prompt_body)

        # Hidden by default; shown when armed.
        self._input = QPlainTextEdit()
        self._input.setObjectName("promptReviewInput")
        self._input.setFixedHeight(60)
        self._input.setPlaceholderText("(reason or context)")
        self._input.setVisible(False)
        body.addWidget(self._input)

        # Buttons
        btn_row = QHBoxLayout()
        btn_row.setContentsMargins(0, 4, 0, 0)
        btn_row.setSpacing(8)
        btn_row.addStretch(1)

        self._inject_btn = QPushButton("Inject context")
        self._inject_btn.setObjectName("promptInject")
        self._inject_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._inject_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._inject_btn.clicked.connect(self._on_inject_clicked)
        btn_row.addWidget(self._inject_btn)

        self._block_btn = QPushButton("Block")
        self._block_btn.setObjectName("promptBlock")
        self._block_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._block_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._block_btn.clicked.connect(self._on_block_clicked)
        btn_row.addWidget(self._block_btn)

        allow_btn = QPushButton("Allow")
        allow_btn.setObjectName("promptAllow")
        allow_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        allow_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        allow_btn.clicked.connect(self._on_allow_clicked)
        btn_row.addWidget(allow_btn)

        body.addLayout(btn_row)
        outer.addLayout(body)

    # ── handlers ───────────────────────────────────────────────────────

    def _on_allow_clicked(self) -> None:
        # Allow is always one-click — even if user previously armed
        # Block/Inject, hitting Allow short-circuits.
        self._emit(Decision(result=DecisionResult.ALLOW))

    def _on_block_clicked(self) -> None:
        if self._armed is DecisionResult.BLOCK:
            text = self._input.toPlainText().strip()
            if not text:
                # Decision invariant requires non-empty reason; nudge.
                self._input.setPlaceholderText(
                    "(reason required to block — type something then click Block)"
                )
                return
            self._emit(Decision(result=DecisionResult.BLOCK, reason=text))
        else:
            self._arm(DecisionResult.BLOCK)
            self._block_btn.setText("Confirm Block")
            self._inject_btn.setText("Inject context")
            self._input.setPlaceholderText("Why? (shown to Claude / user)")

    def _on_inject_clicked(self) -> None:
        if self._armed is DecisionResult.INJECT:
            text = self._input.toPlainText().strip()
            if not text:
                self._input.setPlaceholderText(
                    "(context required to inject — type then click Inject)"
                )
                return
            self._emit(Decision(
                result=DecisionResult.INJECT,
                additional_context=text,
            ))
        else:
            self._arm(DecisionResult.INJECT)
            self._inject_btn.setText("Confirm Inject")
            self._block_btn.setText("Block")
            self._input.setPlaceholderText(
                "Extra context to give Claude alongside the prompt"
            )

    def _arm(self, mode: DecisionResult) -> None:
        self._armed = mode
        self._input.setVisible(True)
        self._input.setFocus()

    def _emit(self, decision: Decision) -> None:
        try:
            if self._on_resolve is not None:
                self._on_resolve(self._view.id, decision)
            self.resolved.emit(self._view.id, decision)
        except Exception:
            log.exception("PromptReviewCard.on_resolve raised")
