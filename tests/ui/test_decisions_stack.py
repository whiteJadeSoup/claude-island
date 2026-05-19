"""Tests for StackedDecisionsPanel — the pile-of-cards renderer."""
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


def _approval_view(idx: int) -> PendingDecisionView:
    return PendingDecisionView(
        id=f"d{idx}",
        kind=DecisionKind.PRE_TOOL_USE,
        session_uuid=f"u{idx}",
        session_name=f"session-{idx}",
        cwd_basename="proj",
        expires_at=datetime.now(timezone.utc) + timedelta(seconds=600),
        risk_level=RiskLevel.HIGH,
        tool_name="Bash",
        tool_input_preview=f"echo {idx}",
    )


def _question_view(idx: int) -> PendingDecisionView:
    return PendingDecisionView(
        id=f"q{idx}",
        kind=DecisionKind.ASK_QUESTION,
        session_uuid=f"u{idx}",
        session_name=f"session-{idx}",
        cwd_basename="proj",
        expires_at=datetime.now(timezone.utc) + timedelta(seconds=600),
        risk_level=RiskLevel.MEDIUM,
        tool_name="AskUserQuestion",
        question_text="Pick one?",
        question_header="Topic",
        question_options=("A", "B"),
    )


@pytest.fixture
def app(qtbot):
    return qtbot


# ── empty / single ─────────────────────────────────────────────────────


def test_empty_render_hides_panel(app):
    from claude_island.ui.decisions_stack import StackedDecisionsPanel

    panel = StackedDecisionsPanel(on_resolve=lambda *a: None)
    app.addWidget(panel)
    panel.render(())
    assert panel.isVisible() is False
    assert panel.active_card is None
    assert panel.peek_count == 0


def test_single_decision_renders_active_no_peeks(app):
    from claude_island.ui.decisions_stack import StackedDecisionsPanel
    from claude_island.ui.approval_card import ApprovalCard

    panel = StackedDecisionsPanel(on_resolve=lambda *a: None)
    app.addWidget(panel)
    panel.render((_approval_view(1),))
    panel.show()
    assert isinstance(panel.active_card, ApprovalCard)
    assert panel.peek_count == 0


# ── peek count + cap ───────────────────────────────────────────────────


def test_two_decisions_one_peek(app):
    from claude_island.ui.decisions_stack import StackedDecisionsPanel

    panel = StackedDecisionsPanel(on_resolve=lambda *a: None)
    app.addWidget(panel)
    panel.render((
        _approval_view(1),
        _approval_view(2),
    ))
    assert panel.peek_count == 1


def test_four_decisions_three_peeks_no_overflow(app):
    from claude_island.ui.decisions_stack import StackedDecisionsPanel

    panel = StackedDecisionsPanel(on_resolve=lambda *a: None)
    app.addWidget(panel)
    panel.render(tuple(_approval_view(i) for i in range(1, 5)))
    assert panel.peek_count == 3


def test_six_decisions_three_peeks_plus_overflow_label(app):
    from PySide6.QtWidgets import QLabel
    from claude_island.ui.decisions_stack import StackedDecisionsPanel

    panel = StackedDecisionsPanel(on_resolve=lambda *a: None)
    app.addWidget(panel)
    panel.render(tuple(_approval_view(i) for i in range(1, 7)))
    assert panel.peek_count == 3
    overflow = panel.findChild(QLabel, "stackOverflowLabel")
    assert overflow is not None
    # 6 total - 1 active - 3 peeks = 2 overflow
    assert "2" in overflow.text()


# ── kind dispatch ──────────────────────────────────────────────────────


def test_ask_question_view_renders_question_card_as_active(app):
    from claude_island.ui.decisions_stack import StackedDecisionsPanel
    from claude_island.ui.question_card import QuestionCard

    panel = StackedDecisionsPanel(on_resolve=lambda *a: None)
    app.addWidget(panel)
    panel.render((_question_view(1),))
    assert isinstance(panel.active_card, QuestionCard)


def test_mixed_kinds_active_is_first_kind(app):
    from claude_island.ui.decisions_stack import StackedDecisionsPanel
    from claude_island.ui.question_card import QuestionCard

    panel = StackedDecisionsPanel(on_resolve=lambda *a: None)
    app.addWidget(panel)
    panel.render((
        _question_view(1),
        _approval_view(2),
        _approval_view(3),
    ))
    assert isinstance(panel.active_card, QuestionCard)
    assert panel.peek_count == 2


# ── header ─────────────────────────────────────────────────────────────


def test_header_collapsed_in_v4c(app):
    """v4c removed the all-caps "PENDING DECISIONS [N] · M queued"
    header that v3 rendered above the active card.  The widget is
    kept around (as a 0-height invisible placeholder) so callers /
    tests that find _header_widget keep compiling, but it no longer
    paints anything."""
    from claude_island.ui.decisions_stack import StackedDecisionsPanel

    panel = StackedDecisionsPanel(on_resolve=lambda *a: None)
    app.addWidget(panel)
    panel.render(tuple(_approval_view(i) for i in range(1, 4)))
    header = panel._header_widget
    assert header is not None
    assert header.height() == 0
    # The old stackedDecisionsBadge / stackedDecisionsCounter children
    # are gone in v4c — they were the badge text/counter inside the
    # collapsed header.
    from PySide6.QtWidgets import QLabel
    assert panel.findChild(QLabel, "stackedDecisionsBadge") is None
    assert panel.findChild(QLabel, "stackedDecisionsCounter") is None


# ── resolve plumbing ───────────────────────────────────────────────────


def test_active_card_resolve_callback_routes_back(app):
    from PySide6.QtWidgets import QPushButton
    from claude_island.ui.decisions_stack import StackedDecisionsPanel

    seen: list[tuple[str, Decision]] = []
    panel = StackedDecisionsPanel(
        on_resolve=lambda did, dec: seen.append((did, dec)),
    )
    app.addWidget(panel)
    panel.render((_approval_view(7),))
    allow_btn = next(
        b for b in panel.findChildren(QPushButton)
        if b.objectName() == "approvalAllow"
    )
    allow_btn.click()
    assert len(seen) == 1
    assert seen[0][0] == "d7"
    assert seen[0][1].result is DecisionResult.ALLOW


# ── re-render shifts state correctly ───────────────────────────────────


def test_re_render_replaces_active_when_head_changes(app):
    from claude_island.ui.decisions_stack import StackedDecisionsPanel

    panel = StackedDecisionsPanel(on_resolve=lambda *a: None)
    app.addWidget(panel)
    panel.render((_approval_view(1), _approval_view(2)))
    first_active = panel.active_card
    # Simulate the head being resolved & dropped: new render starts at view 2.
    panel.render((_approval_view(2),))
    second_active = panel.active_card
    assert first_active is not second_active
    assert panel.peek_count == 0


def test_re_render_empty_hides_panel(app):
    from claude_island.ui.decisions_stack import StackedDecisionsPanel

    panel = StackedDecisionsPanel(on_resolve=lambda *a: None)
    app.addWidget(panel)
    panel.render((_approval_view(1),))
    assert panel.isVisible() is True
    panel.render(())
    assert panel.isVisible() is False
