"""Tests for QuestionCard widget (Step 6 of decisions v2 rework)."""
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


def _view(
    *,
    multi_select: bool = False,
    options: tuple[str, ...] = ("Yes", "No"),
    descs: tuple[str, ...] = (),
    question: str = "Proceed?",
    header: str | None = "Confirmation",
) -> PendingDecisionView:
    return PendingDecisionView(
        id="q1",
        kind=DecisionKind.ASK_QUESTION,
        session_uuid="u1",
        session_name="my-session",
        cwd_basename="myproj",
        expires_at=datetime.now(timezone.utc) + timedelta(seconds=600),
        risk_level=RiskLevel.MEDIUM,
        tool_name="AskUserQuestion",
        question_text=question,
        question_header=header,
        question_options=options,
        question_option_descriptions=descs,
        multi_select=multi_select,
    )


@pytest.fixture
def app(qtbot):
    return qtbot


def _captured_resolve():
    calls: list[tuple[str, Decision]] = []
    return calls, lambda did, dec: calls.append((did, dec))


# ── construction ────────────────────────────────────────────────────────


def test_renders_question_header_question_text_and_options(app):
    from PySide6.QtWidgets import QLabel, QPushButton
    from claude_island.ui.question_card import QuestionCard

    card = QuestionCard(_view(
        question="Which size?", header="Sizing",
        options=("S", "M", "L"),
    ))
    app.addWidget(card)
    header = card.findChild(QLabel, "questionCardHeaderTitle")
    assert header.text() == "Sizing"
    text = card.findChild(QLabel, "questionCardText")
    assert text.text() == "Which size?"

    option_btns = [
        b for b in card.findChildren(QPushButton)
        if b.objectName() == "questionOption"
    ]
    assert len(option_btns) == 3
    # Keycaps are visible decoration ([1] / [2] / [3])
    assert "[1]" in option_btns[0].text()
    assert "S" in option_btns[0].text()


def test_session_badge_shows_session_name(app):
    from PySide6.QtWidgets import QLabel
    from claude_island.ui.question_card import QuestionCard

    card = QuestionCard(_view())
    app.addWidget(card)
    badge = card.findChild(QLabel, "questionCardSessionBadge")
    assert "my-session" in badge.text()


def test_min_height_floor_holds(app):
    """As with ApprovalCard, the footer must stay visible — guard the
    minimum height so future layout edits don't accidentally relax it."""
    from claude_island.ui.question_card import QuestionCard

    card = QuestionCard(_view())
    app.addWidget(card)
    assert card.minimumHeight() >= 150


# ── single-select interactions ─────────────────────────────────────────


def test_single_select_pick_resolves_immediately_with_reason(app):
    from PySide6.QtWidgets import QPushButton
    from claude_island.ui.question_card import QuestionCard

    calls, cb = _captured_resolve()
    card = QuestionCard(_view(options=("Yes", "No")), on_resolve=cb)
    app.addWidget(card)
    option_btns = [
        b for b in card.findChildren(QPushButton)
        if b.objectName() == "questionOption"
    ]
    option_btns[0].click()  # Yes
    assert len(calls) == 1
    did, dec = calls[0]
    assert did == "q1"
    assert dec.result is DecisionResult.ALLOW
    # Reason carries which option was picked so a future audit / replay
    # can reconstruct what the user did even though the hook itself
    # only sees a generic ALLOW.
    assert "Yes" in (dec.reason or "")
    assert "picked" in (dec.reason or "")


def test_single_select_focus_terminal_invoked_with_session_uuid(app):
    from PySide6.QtWidgets import QPushButton
    from claude_island.ui.question_card import QuestionCard

    focuses: list[str] = []
    card = QuestionCard(
        _view(),
        on_focus_terminal=lambda uuid: focuses.append(uuid),
    )
    app.addWidget(card)
    btn = next(
        b for b in card.findChildren(QPushButton)
        if b.objectName() == "questionOption"
    )
    btn.click()
    assert focuses == ["u1"]


# ── multi-select interactions ──────────────────────────────────────────


def test_multi_select_submit_disabled_until_pick(app):
    from PySide6.QtWidgets import QPushButton
    from claude_island.ui.question_card import QuestionCard

    card = QuestionCard(_view(
        multi_select=True, options=("A", "B", "C"),
    ))
    app.addWidget(card)
    submit = next(
        b for b in card.findChildren(QPushButton)
        if b.objectName() == "questionSubmit"
    )
    assert submit.isEnabled() is False
    # Pick one — submit enables
    options = [
        b for b in card.findChildren(QPushButton)
        if b.objectName() == "questionOption"
    ]
    options[1].click()
    assert submit.isEnabled() is True
    # Unpick — submit goes back to disabled
    options[1].click()
    assert submit.isEnabled() is False


def test_multi_select_submit_emits_all_picked_labels(app):
    from PySide6.QtWidgets import QPushButton
    from claude_island.ui.question_card import QuestionCard

    calls, cb = _captured_resolve()
    card = QuestionCard(
        _view(multi_select=True, options=("A", "B", "C")),
        on_resolve=cb,
    )
    app.addWidget(card)
    options = [
        b for b in card.findChildren(QPushButton)
        if b.objectName() == "questionOption"
    ]
    options[0].click()
    options[2].click()
    submit = next(
        b for b in card.findChildren(QPushButton)
        if b.objectName() == "questionSubmit"
    )
    submit.click()
    assert len(calls) == 1
    _, dec = calls[0]
    assert dec.result is DecisionResult.ALLOW
    # Picked indices sorted ⇒ "A | C"
    assert "A" in (dec.reason or "")
    assert "C" in (dec.reason or "")
    assert "B" not in (dec.reason or "")


def test_multi_select_card_tracks_picked_indices(app):
    from PySide6.QtWidgets import QPushButton
    from claude_island.ui.question_card import QuestionCard

    card = QuestionCard(_view(multi_select=True, options=("A", "B", "C")))
    app.addWidget(card)
    options = [
        b for b in card.findChildren(QPushButton)
        if b.objectName() == "questionOption"
    ]
    options[0].click()
    options[2].click()
    assert card.picked_indices == (0, 2)


# ── skip ───────────────────────────────────────────────────────────────


def test_skip_resolves_with_skip_reason(app):
    from PySide6.QtWidgets import QPushButton
    from claude_island.ui.question_card import QuestionCard

    calls, cb = _captured_resolve()
    card = QuestionCard(_view(), on_resolve=cb)
    app.addWidget(card)
    skip = next(
        b for b in card.findChildren(QPushButton)
        if b.objectName() == "questionSkip"
    )
    skip.click()
    assert len(calls) == 1
    _, dec = calls[0]
    assert dec.result is DecisionResult.ALLOW
    assert "skipped" in (dec.reason or "").lower()


def test_skip_also_focuses_terminal(app):
    from PySide6.QtWidgets import QPushButton
    from claude_island.ui.question_card import QuestionCard

    focuses: list[str] = []
    card = QuestionCard(
        _view(),
        on_focus_terminal=lambda uuid: focuses.append(uuid),
    )
    app.addWidget(card)
    skip = next(
        b for b in card.findChildren(QPushButton)
        if b.objectName() == "questionSkip"
    )
    skip.click()
    assert focuses == ["u1"]
