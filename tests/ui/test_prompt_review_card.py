"""Tests for PromptReviewCard widget (G8)."""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from claude_island.core.pending_decisions import (
    Decision,
    DecisionKind,
    DecisionResult,
    PendingDecisionView,
    RiskLevel,
)


def _view(prompt: str = "what is 2+2?") -> PendingDecisionView:
    return PendingDecisionView(
        id="d2",
        kind=DecisionKind.USER_PROMPT_SUBMIT,
        session_uuid="u1",
        session_name="my-session",
        cwd_basename="myproj",
        expires_at=datetime.now(timezone.utc) + timedelta(seconds=600),
        risk_level=RiskLevel.MEDIUM,
        prompt_preview=prompt,
    )


@pytest.fixture
def app(qtbot):
    return qtbot


# ── Allow is one-click ────────────────────────────────────────────────


def test_allow_click_emits_allow(app):
    from PySide6.QtWidgets import QPushButton
    from claude_island.ui.prompt_review_card import PromptReviewCard
    captured: list = []
    card = PromptReviewCard(_view(), on_resolve=lambda i, d: captured.append((i, d)))
    app.addWidget(card)
    buttons = {b.objectName(): b for b in card.findChildren(QPushButton)}
    buttons["promptAllow"].click()
    assert len(captured) == 1
    _, decision = captured[0]
    assert decision.result is DecisionResult.ALLOW


# ── Block is two-click (arm + confirm) ────────────────────────────────


def test_block_first_click_arms_then_confirm_emits(app):
    from PySide6.QtWidgets import QPlainTextEdit, QPushButton
    from claude_island.ui.prompt_review_card import PromptReviewCard
    captured: list = []
    card = PromptReviewCard(_view(), on_resolve=lambda i, d: captured.append((i, d)))
    app.addWidget(card)
    buttons = {b.objectName(): b for b in card.findChildren(QPushButton)}
    block_btn = buttons["promptBlock"]

    # First click: arms but doesn't emit. Input box becomes visible.
    block_btn.click()
    assert captured == []
    input_box = card.findChild(QPlainTextEdit, "promptReviewInput")
    # offscreen QPA needs an ancestor shown for isVisible() — assert the
    # property we control (visibility flag) instead.
    assert input_box.isVisibleTo(card)
    assert "Block" in block_btn.text()  # text changed to "Confirm Block"

    # Type a reason and confirm.
    input_box.setPlainText("needs more context first")
    block_btn.click()
    assert len(captured) == 1
    _, decision = captured[0]
    assert decision.result is DecisionResult.BLOCK
    assert decision.reason == "needs more context first"


def test_block_with_empty_reason_does_not_emit(app):
    from PySide6.QtWidgets import QPushButton
    from claude_island.ui.prompt_review_card import PromptReviewCard
    captured: list = []
    card = PromptReviewCard(_view(), on_resolve=lambda i, d: captured.append((i, d)))
    app.addWidget(card)
    buttons = {b.objectName(): b for b in card.findChildren(QPushButton)}
    block_btn = buttons["promptBlock"]
    block_btn.click()  # arm
    block_btn.click()  # confirm with empty text
    # Should NOT have emitted (Decision invariant requires non-empty reason).
    assert captured == []


# ── Inject ────────────────────────────────────────────────────────────


def test_inject_two_click_emits_with_context(app):
    from PySide6.QtWidgets import QPlainTextEdit, QPushButton
    from claude_island.ui.prompt_review_card import PromptReviewCard
    captured: list = []
    card = PromptReviewCard(_view(), on_resolve=lambda i, d: captured.append((i, d)))
    app.addWidget(card)
    buttons = {b.objectName(): b for b in card.findChildren(QPushButton)}
    inject_btn = buttons["promptInject"]
    inject_btn.click()
    input_box = card.findChild(QPlainTextEdit, "promptReviewInput")
    assert input_box.isVisibleTo(card)
    input_box.setPlainText("git status: clean")
    inject_btn.click()
    assert len(captured) == 1
    _, decision = captured[0]
    assert decision.result is DecisionResult.INJECT
    assert decision.additional_context == "git status: clean"


# ── Allow short-circuits any armed mode ───────────────────────────────


def test_allow_after_arming_block_short_circuits(app):
    from PySide6.QtWidgets import QPushButton
    from claude_island.ui.prompt_review_card import PromptReviewCard
    captured: list = []
    card = PromptReviewCard(_view(), on_resolve=lambda i, d: captured.append((i, d)))
    app.addWidget(card)
    buttons = {b.objectName(): b for b in card.findChildren(QPushButton)}
    buttons["promptBlock"].click()  # arm BLOCK
    buttons["promptAllow"].click()  # but Allow short-circuits
    assert len(captured) == 1
    assert captured[0][1].result is DecisionResult.ALLOW
