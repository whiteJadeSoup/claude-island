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
    # Option content lives in child QLabels (keycap + label + optional
    # description) so the description can word-wrap — see
    # _OptionButton docstring. The keycap text is the visible
    # 1-based index; the label text is the option name.
    first_keycap = option_btns[0].findChild(QLabel, "questionOptionKeycap")
    first_label = option_btns[0].findChild(QLabel, "questionOptionLabel")
    assert first_keycap.text() == "1"
    assert first_label.text() == "S"


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


def test_single_select_decision_carries_answers(app):
    """The hook layer needs decision.answers populated to build
    updatedInput; the reason field alone isn't enough."""
    from PySide6.QtWidgets import QPushButton
    from claude_island.ui.question_card import QuestionCard

    calls, cb = _captured_resolve()
    card = QuestionCard(
        _view(question="Pick size?", options=("S", "M", "L")),
        on_resolve=cb,
    )
    app.addWidget(card)
    options = [
        b for b in card.findChildren(QPushButton)
        if b.objectName() == "questionOption"
    ]
    options[1].click()   # M
    _, dec = calls[0]
    assert dec.answers == (("Pick size?", "M"),)


def test_multi_select_decision_carries_comma_list_answer(app):
    from PySide6.QtWidgets import QPushButton
    from claude_island.ui.question_card import QuestionCard

    calls, cb = _captured_resolve()
    card = QuestionCard(
        _view(multi_select=True, question="Features?",
              options=("A", "B", "C")),
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
    _, dec = calls[0]
    assert dec.answers == (("Features?", "A, C"),)


def test_skip_does_not_carry_answers(app):
    """Skip path is "user will answer in terminal" — the decision
    must NOT pretend an answer was given, or the hook layer would
    relay an empty/wrong answer back to Claude."""
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
    _, dec = calls[0]
    assert dec.answers == ()


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


def test_option_description_wraps_when_too_long_for_one_line(app):
    """Regression: option descriptions used to be packed into the
    QPushButton's ``text`` field, which doesn't word-wrap — long
    Chinese / multi-clause descriptions clipped on the right edge
    of the panel. Now the description lives in its own QLabel with
    ``setWordWrap(True)``; verify it actually wraps and that the
    button grows vertically rather than clipping the text."""
    from PySide6.QtWidgets import QLabel, QPushButton
    from claude_island.ui.question_card import QuestionCard

    long_desc = (
        "/origin/v3 基路径下，与 /busiline/session/list 等同包同风格；"
        "无包前缀冲突；可以与现有 controller 共用同一组拦截器与日志切面。"
    )
    card = QuestionCard(_view(
        options=("加进现有 BusinessLineController（推荐）", "新建文件"),
        descs=(long_desc, "按字面新建文件"),
    ))
    card.setFixedWidth(360)  # roughly the rendered panel width
    app.addWidget(card)
    card.adjustSize()

    option_btns = [
        b for b in card.findChildren(QPushButton)
        if b.objectName() == "questionOption"
    ]
    long_desc_label = option_btns[0].findChild(QLabel, "questionOptionDesc")
    assert long_desc_label is not None, (
        "option with a description must render a description QLabel"
    )
    # Word-wrap must be enabled; without it the QLabel clips horizontally.
    assert long_desc_label.wordWrap() is True
    # Description text is preserved verbatim (no truncation).
    assert long_desc_label.text() == long_desc
    # And the button must be tall enough to fit the wrapped lines plus
    # the label row — strictly more than the single-line floor.
    btn = option_btns[0]
    btn_w = btn.width() or 360
    assert btn.heightForWidth(btn_w) > 36, (
        "button height must grow with wrapped description; got "
        f"{btn.heightForWidth(btn_w)} px at width {btn_w}"
    )


def test_option_without_description_omits_desc_label(app):
    """Description QLabel should only exist when there's text to show
    — keeps the empty-state button visually compact."""
    from PySide6.QtWidgets import QLabel, QPushButton
    from claude_island.ui.question_card import QuestionCard

    card = QuestionCard(_view(
        options=("Yes", "No"),
        descs=(),  # no descriptions
    ))
    app.addWidget(card)
    btn = next(
        b for b in card.findChildren(QPushButton)
        if b.objectName() == "questionOption"
    )
    assert btn.findChild(QLabel, "questionOptionDesc") is None


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
