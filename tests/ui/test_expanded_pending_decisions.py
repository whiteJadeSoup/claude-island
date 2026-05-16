"""Integration tests for ExpandedWindow's pending-decisions wiring (v2).

v2 design (2026-05): all pending decisions render in the
:class:`StackedDecisionsPanel` (pile-of-cards) at the top of the
panel. The legacy v1 inline-under-row + cap-at-5 + per-id widget cache
were deleted; tests in this file now exercise:

  - panel hides when ``snap.pending_decisions`` is empty
  - panel becomes visible with the right card kind when non-empty
  - resolve callback wiring still routes card click → registry
  - dedup key still includes pending decisions so an idle world tick
    bringing a new approval card doesn't get swallowed by
    ``distinct_until_changed``

Tests that asserted on v1 internals (``_pending_layout``,
``_decision_cards``, ``_PENDING_VISIBLE_CAP``, inline wrappers, matched/
orphan buckets) were removed: those concepts are gone in v2.
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from claude_island.core.models import QuotaSnapshot, UsageTotals
from claude_island.core.pending_decisions import (
    Decision,
    DecisionKind,
    DecisionResult,
    PendingDecisionView,
    RiskLevel,
)
from claude_island.core.snapshot import WorldSnapshot
from claude_island.ui.controller import IslandController


def _view(
    *,
    id_: str = "d1",
    kind: DecisionKind = DecisionKind.PRE_TOOL_USE,
    risk: RiskLevel = RiskLevel.MEDIUM,
    tool: str = "Bash",
    session_uuid: str = "u1",
) -> PendingDecisionView:
    return PendingDecisionView(
        id=id_,
        kind=kind,
        session_uuid=session_uuid,
        session_name="my-session",
        cwd_basename="myproj",
        expires_at=datetime.now(timezone.utc) + timedelta(seconds=600),
        risk_level=risk,
        tool_name=tool if kind is DecisionKind.PRE_TOOL_USE else None,
        tool_input_preview="ls" if kind is DecisionKind.PRE_TOOL_USE else None,
        prompt_preview="hi" if kind is DecisionKind.USER_PROMPT_SUBMIT else None,
    )


def _snap(*, pending: tuple = (), groups: tuple = ()) -> WorldSnapshot:
    return WorldSnapshot.empty().__class__(
        today_cost_usd=0.0,
        quota=None,
        available_providers=(),
        selected_provider=None,
        fetched_at=datetime.now(timezone.utc),
        session_groups=groups,
        dormant_sessions=(),
        launching_sessions=(),
        pending_decisions=pending,
        notify_events=(),
    )


def _empty_totals(period: str) -> UsageTotals:
    return UsageTotals(period=period)


@pytest.fixture
def panel(qtbot):
    from claude_island.ui.capsule_window import CapsuleWindow
    from claude_island.ui.expanded_window import ExpandedWindow
    controller = IslandController()
    capsule = CapsuleWindow(controller)
    qtbot.addWidget(capsule)
    pw = ExpandedWindow(
        capsule=capsule,
        controller=controller,
        get_usage_totals=_empty_totals,
    )
    qtbot.addWidget(pw)
    return pw


@pytest.fixture
def panel_with_resolver(qtbot):
    from claude_island.ui.capsule_window import CapsuleWindow
    from claude_island.ui.expanded_window import ExpandedWindow
    controller = IslandController()
    capsule = CapsuleWindow(controller)
    qtbot.addWidget(capsule)
    captured: list[tuple[str, object]] = []
    pw = ExpandedWindow(
        capsule=capsule,
        controller=controller,
        get_usage_totals=_empty_totals,
        resolve_decision=lambda i, d: captured.append((i, d)) or True,
    )
    qtbot.addWidget(pw)
    return pw, captured


# ── Empty state ──────────────────────────────────────────────────────


class TestEmpty:
    def test_no_pending_hides_stacked_panel(self, panel):
        panel.render(_snap(pending=()))
        assert panel._pending_panel.isVisible() is False
        assert panel._pending_panel.active_card is None


# ── Render kind dispatch ─────────────────────────────────────────────


class TestRenderKind:
    def test_pre_tool_use_renders_approval_card(self, panel):
        from claude_island.ui.approval_card import ApprovalCard

        panel.show()
        panel.render(_snap(pending=(
            _view(kind=DecisionKind.PRE_TOOL_USE),
        )))
        assert panel._pending_panel.isVisible() is True
        assert isinstance(panel._pending_panel.active_card, ApprovalCard)

    def test_ask_question_renders_question_card(self, panel):
        from claude_island.ui.question_card import QuestionCard

        question_view = PendingDecisionView(
            id="q1",
            kind=DecisionKind.ASK_QUESTION,
            session_uuid="u1",
            session_name="my-session",
            cwd_basename="proj",
            expires_at=datetime.now(timezone.utc) + timedelta(seconds=600),
            risk_level=RiskLevel.MEDIUM,
            tool_name="AskUserQuestion",
            question_text="Pick one?",
            question_header="Choice",
            question_options=("A", "B"),
        )
        panel.show()
        panel.render(_snap(pending=(question_view,)))
        assert isinstance(panel._pending_panel.active_card, QuestionCard)


# ── Resolve plumbing ─────────────────────────────────────────────────


class TestResolve:
    def test_card_click_invokes_resolver(self, panel_with_resolver):
        from PySide6.QtWidgets import QPushButton

        panel, captured = panel_with_resolver
        panel.show()
        panel.render(_snap(pending=(_view(id_="d-x"),)))
        allow_btn = next(
            b for b in panel.findChildren(QPushButton)
            if b.objectName() == "approvalAllow"
        )
        allow_btn.click()
        assert len(captured) == 1
        did, decision = captured[0]
        assert did == "d-x"
        assert decision.result is DecisionResult.ALLOW

    def test_resolver_exception_does_not_propagate(self, qtbot):
        # An exception inside the registry-resolve callback must be
        # absorbed in ExpandedWindow — the user's click should never
        # surface a stack trace.
        from PySide6.QtWidgets import QPushButton
        from claude_island.ui.capsule_window import CapsuleWindow
        from claude_island.ui.expanded_window import ExpandedWindow

        controller = IslandController()
        capsule = CapsuleWindow(controller)
        qtbot.addWidget(capsule)

        def _raise(_did, _dec):
            raise RuntimeError("boom")

        pw = ExpandedWindow(
            capsule=capsule, controller=controller,
            get_usage_totals=_empty_totals,
            resolve_decision=_raise,
        )
        qtbot.addWidget(pw)
        pw.show()
        pw.render(_snap(pending=(_view(),)))
        allow_btn = next(
            b for b in pw.findChildren(QPushButton)
            if b.objectName() == "approvalAllow"
        )
        # No exception should escape this call.
        allow_btn.click()


class TestNoResolver:
    def test_card_renders_even_without_resolver(self, panel):
        from claude_island.ui.approval_card import ApprovalCard

        panel.show()
        panel.render(_snap(pending=(_view(),)))
        # Without a resolver the card still renders; clicking it is a
        # no-op (handled by the ExpandedWindow guard) and that's the
        # intended behaviour in tests / dev scaffolding.
        assert isinstance(panel._pending_panel.active_card, ApprovalCard)


# ── Snapshot dedup key includes pending decisions ────────────────────


class TestComputeReachesRender:
    """The distinct_until_changed key used by the UI subscription
    (``ExpandedWindow.compute``) must include pending decisions, so a
    new decision arriving on an otherwise-quiet world snapshot still
    wakes the UI."""

    def test_pending_decision_changes_compute_key(self, panel):
        empty_snap = _snap(pending=())
        snap_a = _snap(pending=(_view(id_="d-a"),))
        snap_b = _snap(pending=(_view(id_="d-b"),))
        assert panel.compute(empty_snap) != panel.compute(snap_a)
        assert panel.compute(snap_a) != panel.compute(snap_b)


# ── G8: review-mode toggle wiring (preserved across v1 → v2) ─────────


class TestG8ReviewToggleWiring:
    def test_expanded_window_accepts_get_review_mode(self, qtbot):
        from claude_island.ui.capsule_window import CapsuleWindow
        from claude_island.ui.expanded_window import ExpandedWindow

        controller = IslandController()
        capsule = CapsuleWindow(controller)
        qtbot.addWidget(capsule)

        gets: list[str] = []
        sets: list[tuple[str, bool]] = []
        pw = ExpandedWindow(
            capsule=capsule,
            controller=controller,
            get_usage_totals=_empty_totals,
            get_review_mode=lambda uuid: gets.append(uuid) or False,
            set_review_mode=lambda uuid, v: sets.append((uuid, v)),
        )
        qtbot.addWidget(pw)
        assert pw._get_review_mode is not None
        assert pw._set_review_mode is not None
