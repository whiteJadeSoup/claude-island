"""Tests for ApprovalCard widget (G1).

Tests behaviour, not pixels — invariants the renderer must hold.
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from claude_island.core.pending_decisions import (
    Decision,
    DecisionKind,
    DecisionResult,
    PendingDecisionView,
    RiskLevel,
)


def _view(
    *,
    risk: RiskLevel = RiskLevel.MEDIUM,
    tool: str = "Bash",
    preview: str = "npm test",
) -> PendingDecisionView:
    return PendingDecisionView(
        id="d1",
        kind=DecisionKind.PRE_TOOL_USE,
        session_uuid="u1",
        session_name="my-session",
        cwd_basename="myproj",
        expires_at=datetime.now(timezone.utc) + timedelta(seconds=600),
        risk_level=risk,
        tool_name=tool,
        tool_input_preview=preview,
    )


@pytest.fixture
def app(qtbot):
    # qtbot fixture from pytest-qt creates QApplication
    return qtbot


# ── Allow / Deny click emits expected Decision ────────────────────────


def test_allow_click_emits_allow_decision(app):
    from claude_island.ui.approval_card import ApprovalCard
    captured: list[tuple[str, Decision]] = []
    card = ApprovalCard(_view(), on_resolve=lambda i, d: captured.append((i, d)))
    app.addWidget(card)
    # Click Allow button
    allow_btn = card.findChild(type(card).__mro__[0], "approvalAllow")  # noqa: SLF001
    # Easier: scan children by objectName
    from PySide6.QtWidgets import QPushButton
    buttons = {b.objectName(): b for b in card.findChildren(QPushButton)}
    buttons["approvalAllow"].click()
    assert len(captured) == 1
    decision_id, decision = captured[0]
    assert decision_id == "d1"
    assert decision.result is DecisionResult.ALLOW
    assert decision.remember is False  # checkbox unticked


def test_deny_click_emits_deny_decision_with_reason(app):
    from claude_island.ui.approval_card import ApprovalCard
    from PySide6.QtWidgets import QPushButton
    captured: list = []
    card = ApprovalCard(_view(), on_resolve=lambda i, d: captured.append((i, d)))
    app.addWidget(card)
    buttons = {b.objectName(): b for b in card.findChildren(QPushButton)}
    buttons["approvalDeny"].click()
    assert len(captured) == 1
    _, decision = captured[0]
    assert decision.result is DecisionResult.DENY
    assert decision.reason  # non-empty (Decision invariant)


def test_remember_checkbox_propagates(app):
    from claude_island.ui.approval_card import ApprovalCard
    from PySide6.QtWidgets import QCheckBox, QPushButton
    captured: list = []
    card = ApprovalCard(_view(), on_resolve=lambda i, d: captured.append((i, d)))
    app.addWidget(card)
    cb = card.findChild(QCheckBox, "approvalRemember")
    assert cb is not None
    cb.setChecked(True)
    buttons = {b.objectName(): b for b in card.findChildren(QPushButton)}
    buttons["approvalAllow"].click()
    _, decision = captured[0]
    assert decision.remember is True


def test_signal_also_emits(app, qtbot):
    """Both the on_resolve callback AND the Qt signal should fire — for
    consumers that prefer one wiring style or the other."""
    from claude_island.ui.approval_card import ApprovalCard
    from PySide6.QtWidgets import QPushButton
    card = ApprovalCard(_view())
    app.addWidget(card)
    with qtbot.waitSignal(card.resolved, timeout=1000) as blocker:
        buttons = {b.objectName(): b for b in card.findChildren(QPushButton)}
        buttons["approvalAllow"].click()
    decision_id, decision = blocker.args
    assert decision_id == "d1"
    assert decision.result is DecisionResult.ALLOW


# ── Risk-level visual cues ────────────────────────────────────────────


def test_high_risk_shows_warning_label(app):
    from PySide6.QtWidgets import QLabel
    from claude_island.ui.approval_card import ApprovalCard
    card = ApprovalCard(_view(risk=RiskLevel.HIGH))
    app.addWidget(card)
    warning = card.findChild(QLabel, "approvalCardWarning")
    # On offscreen QPA + no show(), isVisible() always returns False.
    # What we care about: the widget EXISTS in the tree for HIGH risk
    # (vs LOW where it should be absent — see test_low_risk_hides_…).
    assert warning is not None
    assert warning.text().strip() != ""


def test_low_risk_hides_warning_label(app):
    from PySide6.QtWidgets import QLabel
    from claude_island.ui.approval_card import ApprovalCard
    card = ApprovalCard(_view(risk=RiskLevel.LOW))
    app.addWidget(card)
    warning = card.findChild(QLabel, "approvalCardWarning")
    assert warning is None


# ── Title / preview surfaces correct fields ───────────────────────────


def test_title_includes_tool_and_session(app):
    from PySide6.QtWidgets import QLabel
    from claude_island.ui.approval_card import ApprovalCard
    card = ApprovalCard(_view(tool="Edit"))
    app.addWidget(card)
    title = card.findChild(QLabel, "approvalCardTitle")
    assert "Edit" in title.text()
    assert "my-session" in title.text()


def test_preview_label_shows_tool_input(app):
    from PySide6.QtWidgets import QLabel
    from claude_island.ui.approval_card import ApprovalCard
    card = ApprovalCard(_view(preview="npm test --watch"))
    app.addWidget(card)
    preview = card.findChild(QLabel, "approvalCardPreview")
    assert "npm test --watch" in preview.text()


def test_no_preview_falls_back_gracefully(app):
    from PySide6.QtWidgets import QLabel
    from claude_island.ui.approval_card import ApprovalCard
    v = PendingDecisionView(
        id="d1", kind=DecisionKind.PRE_TOOL_USE, session_uuid="u",
        session_name="s", cwd_basename="p",
        expires_at=datetime.now(timezone.utc) + timedelta(seconds=10),
        risk_level=RiskLevel.MEDIUM,
        tool_name="Bash", tool_input_preview=None,
    )
    card = ApprovalCard(v)
    app.addWidget(card)
    preview = card.findChild(QLabel, "approvalCardPreview")
    assert preview.text().strip() != ""  # something fallback-text shown
