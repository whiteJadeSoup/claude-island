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


def test_title_shows_tool_name(app):
    from PySide6.QtWidgets import QLabel
    from claude_island.ui.approval_card import ApprovalCard
    card = ApprovalCard(_view(tool="Edit"))
    app.addWidget(card)
    title = card.findChild(QLabel, "approvalCardTitle")
    assert title.text() == "Edit"


def test_session_name_shown_in_badge(app):
    """v2: session lives in its own badge widget (with the accent
    dot), no longer concatenated into the title."""
    from PySide6.QtWidgets import QLabel
    from claude_island.ui.approval_card import ApprovalCard
    card = ApprovalCard(_view(tool="Edit"))
    app.addWidget(card)
    badge = card.findChild(QLabel, "approvalCardSessionBadge")
    assert badge is not None
    assert "my-session" in badge.text()


def test_preview_starts_folded_and_toggles_on_request(app):
    from claude_island.ui.approval_card import ApprovalCard
    card = ApprovalCard(_view(preview="a" * 500))
    app.addWidget(card)
    assert card.is_expanded is False
    folded_h = card.findChild(type(card.findChild(type, "approvalCardPreview")) or object, "approvalCardPreview")
    # Easier: just call toggle and inspect.
    card.toggle_expanded()
    assert card.is_expanded is True
    card.toggle_expanded()
    assert card.is_expanded is False


def test_min_height_floor_holds(app):
    """v2 invariant: the card declares min-height so the footer
    buttons stay visible even when the parent layout tries to
    compress it. Guard against accidental regression to the v1
    QSizePolicy.Maximum which let the buttons get clipped."""
    from claude_island.ui.approval_card import ApprovalCard
    card = ApprovalCard(_view())
    app.addWidget(card)
    assert card.minimumHeight() >= 100, (
        "ApprovalCard must declare a minimum height so the footer "
        "buttons can never be compressed out of view."
    )


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


# ── Hint + on_focus_terminal (mirror of QuestionCard) ────────────────


def test_hint_label_rendered_and_explains_terminal_fallback(app):
    """Regression: users reported clicking Allow in Island but the
    terminal prompt still showed — Claude renders its own prompt
    concurrently and the hook channel can race with it. The card now
    carries a hint so the user knows the terminal is the source of
    truth when our Allow/Deny doesn't dismiss the prompt in time."""
    from PySide6.QtWidgets import QLabel
    from claude_island.ui.approval_card import ApprovalCard

    card = ApprovalCard(_view())
    app.addWidget(card)
    hint = card.findChild(QLabel, "approvalCardHint")
    assert hint is not None, "ApprovalCard must surface the terminal-fallback hint"
    assert "terminal" in hint.text().lower()


def test_allow_click_focuses_terminal_then_emits_resolve(app):
    """on_focus_terminal must fire BEFORE on_resolve so the terminal is
    already on screen by the time the panel auto-hides on
    WindowDeactivate. Same ordering as QuestionCard._emit_decision."""
    from PySide6.QtWidgets import QPushButton
    from claude_island.ui.approval_card import ApprovalCard

    events: list[str] = []
    card = ApprovalCard(
        _view(),
        on_resolve=lambda _i, _d: events.append("resolve"),
        on_focus_terminal=lambda uuid: events.append(f"focus:{uuid}"),
    )
    app.addWidget(card)
    {b.objectName(): b for b in card.findChildren(QPushButton)}["approvalAllow"].click()
    assert events == ["focus:u1", "resolve"]


def test_deny_click_also_focuses_terminal(app):
    """Symmetry: Deny should focus the terminal too — if the user
    denies in Island and Claude already had its prompt up, the user
    will land on the terminal and can type 2 (Deny) to confirm."""
    from PySide6.QtWidgets import QPushButton
    from claude_island.ui.approval_card import ApprovalCard

    focuses: list[str] = []
    card = ApprovalCard(
        _view(),
        on_focus_terminal=lambda uuid: focuses.append(uuid),
    )
    app.addWidget(card)
    {b.objectName(): b for b in card.findChildren(QPushButton)}["approvalDeny"].click()
    assert focuses == ["u1"]


def test_focus_terminal_optional_card_works_without_callback(app):
    """Backwards compatibility: callers that don't pass
    on_focus_terminal (e.g. existing tests) must still get a working
    Allow/Deny — no AttributeError, no swallowed exception."""
    from PySide6.QtWidgets import QPushButton
    from claude_island.ui.approval_card import ApprovalCard

    resolves: list = []
    card = ApprovalCard(_view(), on_resolve=lambda i, d: resolves.append((i, d)))
    app.addWidget(card)
    {b.objectName(): b for b in card.findChildren(QPushButton)}["approvalAllow"].click()
    assert len(resolves) == 1


def test_allow_invokes_terminal_answer_default(app):
    """Regression: Claude's terminal prompt for sensitive operations
    (out-of-cwd Read, multi-option Bash) persists even after the hook
    ``allow`` because Claude requires explicit terminal confirmation
    for that class of operation. ApprovalCard.Allow now invokes the
    ``on_terminal_answer_default`` callback so the wiring layer can
    inject ``1\\n`` into the session's iTerm pane and dismiss the
    prompt without the user switching apps."""
    from PySide6.QtWidgets import QPushButton
    from claude_island.ui.approval_card import ApprovalCard

    typed: list[str] = []
    card = ApprovalCard(
        _view(),
        on_terminal_answer_default=lambda uuid: typed.append(uuid),
    )
    app.addWidget(card)
    {b.objectName(): b for b in card.findChildren(QPushButton)}["approvalAllow"].click()
    assert typed == ["u1"], (
        "Allow must invoke on_terminal_answer_default with the session uuid"
    )


def test_deny_does_not_inject_terminal_answer(app):
    """Symmetry: Deny intentionally skips injection. We don't know
    which digit means "deny" for an arbitrary prompt shape, and the
    hook ``deny`` already aborts the tool call so the terminal prompt
    becomes obsolete anyway. Mis-typed digit on a stale prompt would
    be worse than leaving the prompt for the user to dismiss."""
    from PySide6.QtWidgets import QPushButton
    from claude_island.ui.approval_card import ApprovalCard

    typed: list[str] = []
    card = ApprovalCard(
        _view(),
        on_terminal_answer_default=lambda uuid: typed.append(uuid),
    )
    app.addWidget(card)
    {b.objectName(): b for b in card.findChildren(QPushButton)}["approvalDeny"].click()
    assert typed == []


def test_terminal_answer_default_optional_card_works_without_callback(app):
    """Backwards compatibility: callers that don't pass the new
    callback must still get a working Allow path (no AttributeError,
    no swallowed exception). Mirrors the existing on_focus_terminal
    backcompat test."""
    from PySide6.QtWidgets import QPushButton
    from claude_island.ui.approval_card import ApprovalCard

    resolves: list = []
    card = ApprovalCard(_view(), on_resolve=lambda i, d: resolves.append((i, d)))
    app.addWidget(card)
    {b.objectName(): b for b in card.findChildren(QPushButton)}["approvalAllow"].click()
    assert len(resolves) == 1


def test_terminal_answer_default_callback_exception_does_not_block_resolve(app):
    """If the inject callback raises (e.g. iTerm AppleScript timeout),
    the user's Allow MUST still resolve the hook — losing the inject
    is acceptable degradation but losing the allow would be far worse
    (Claude would wait out the full 598s hook timeout). The exception
    is logged but swallowed."""
    from PySide6.QtWidgets import QPushButton
    from claude_island.ui.approval_card import ApprovalCard

    def boom(_uuid):
        raise RuntimeError("simulated AppleScript failure")
    resolves: list = []
    card = ApprovalCard(
        _view(),
        on_resolve=lambda i, d: resolves.append(d),
        on_terminal_answer_default=boom,
    )
    app.addWidget(card)
    {b.objectName(): b for b in card.findChildren(QPushButton)}["approvalAllow"].click()
    assert len(resolves) == 1, "resolve must still fire when inject raises"


def test_decisions_stack_wires_focus_terminal_to_approval_card(app):
    """The stack panel must pass on_focus_terminal through to BOTH
    QuestionCard and ApprovalCard — previously only QuestionCard got
    it, so Bash approvals never focused the terminal and the user
    couldn't recover when Claude's prompt was still waiting."""
    from PySide6.QtWidgets import QPushButton
    from claude_island.ui.decisions_stack import _build_card

    focuses: list[str] = []
    card = _build_card(
        _view(),
        on_resolve=lambda i, d: None,
        on_focus_terminal=lambda uuid: focuses.append(uuid),
    )
    app.addWidget(card)
    {b.objectName(): b for b in card.findChildren(QPushButton)}["approvalAllow"].click()
    assert focuses == ["u1"]


def test_decisions_stack_wires_terminal_answer_default_to_approval_card(app):
    """The stack panel must pass on_terminal_answer_default through
    to ApprovalCard (QuestionCard doesn't need it — its options are
    relayed via updatedInput in the hook response, not by typing
    digits into the terminal). Without this wiring the inject feature
    is dead in production even though ApprovalCard supports it."""
    from PySide6.QtWidgets import QPushButton
    from claude_island.ui.decisions_stack import _build_card

    typed: list[str] = []
    card = _build_card(
        _view(),
        on_resolve=lambda i, d: None,
        on_focus_terminal=None,
        on_terminal_answer_default=lambda uuid: typed.append(uuid),
    )
    app.addWidget(card)
    {b.objectName(): b for b in card.findChildren(QPushButton)}["approvalAllow"].click()
    assert typed == ["u1"]
