"""Integration tests for ExpandedWindow's pending-decisions rendering.

Covers Phase 9 wiring:
  - Pending container hidden when snap.pending_decisions is empty
  - One ApprovalCard rendered per PRE_TOOL_USE entry
  - One PromptReviewCard rendered per USER_PROMPT_SUBMIT entry
  - Cap at 5 visible + "+N more" footer
  - Click resolves through injected callback
  - Cards survive across snapshot ticks (cache by id)
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
) -> PendingDecisionView:
    return PendingDecisionView(
        id=id_,
        kind=kind,
        session_uuid="u1",
        session_name="my-session",
        cwd_basename="myproj",
        expires_at=datetime.now(timezone.utc) + timedelta(seconds=600),
        risk_level=risk,
        tool_name=tool if kind is DecisionKind.PRE_TOOL_USE else None,
        tool_input_preview="ls" if kind is DecisionKind.PRE_TOOL_USE else None,
        prompt_preview="hi" if kind is DecisionKind.USER_PROMPT_SUBMIT else None,
    )


def _snap(*, pending: tuple = ()) -> WorldSnapshot:
    return WorldSnapshot.empty().__class__(
        today_cost_usd=0.0,
        quota=None,
        available_providers=(),
        selected_provider=None,
        fetched_at=datetime.now(timezone.utc),
        session_groups=(),
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
    def test_no_pending_hides_container(self, panel):
        panel.render(_snap(pending=()))
        assert panel._pending_container.isVisibleTo(panel) is False
        assert panel._decision_cards == {}


# ── Renders correct widget per kind ──────────────────────────────────


class TestRenderKind:
    def test_pre_tool_use_renders_approval_card(self, panel):
        from claude_island.ui.approval_card import ApprovalCard
        v = _view(id_="d1", kind=DecisionKind.PRE_TOOL_USE)
        panel.render(_snap(pending=(v,)))
        assert "d1" in panel._decision_cards
        assert isinstance(panel._decision_cards["d1"], ApprovalCard)
        assert panel._pending_container.isVisibleTo(panel)

    def test_user_prompt_submit_renders_review_card(self, panel):
        from claude_island.ui.prompt_review_card import PromptReviewCard
        v = _view(id_="d2", kind=DecisionKind.USER_PROMPT_SUBMIT)
        panel.render(_snap(pending=(v,)))
        assert isinstance(panel._decision_cards["d2"], PromptReviewCard)


# ── Diff stability: card cached across snapshots ─────────────────────


class TestDiffStability:
    def test_same_id_keeps_widget_instance(self, panel):
        v = _view(id_="d1")
        panel.render(_snap(pending=(v,)))
        first_card = panel._decision_cards["d1"]
        # Re-render with same view — should NOT create a new widget.
        panel.render(_snap(pending=(v,)))
        assert panel._decision_cards["d1"] is first_card

    def test_resolved_id_dropped(self, panel):
        v1 = _view(id_="d1")
        v2 = _view(id_="d2")
        panel.render(_snap(pending=(v1, v2)))
        assert set(panel._decision_cards.keys()) == {"d1", "d2"}
        # Snap drops d1 (resolved).
        panel.render(_snap(pending=(v2,)))
        assert set(panel._decision_cards.keys()) == {"d2"}


# ── Cap + overflow ──────────────────────────────────────────────────


class TestCap:
    def test_cap_at_5_visible_with_overflow_label(self, panel):
        decisions = tuple(_view(id_=f"d{i}") for i in range(8))
        panel.render(_snap(pending=decisions))
        # Only first 5 cached (visible).
        assert len(panel._decision_cards) == 5
        # Overflow label exists.
        assert hasattr(panel, "_pending_overflow_label")
        assert panel._pending_overflow_label is not None
        assert "+3" in panel._pending_overflow_label.text()

    def test_no_overflow_label_when_at_cap(self, panel):
        decisions = tuple(_view(id_=f"d{i}") for i in range(5))
        panel.render(_snap(pending=decisions))
        assert (
            getattr(panel, "_pending_overflow_label", None) is None
            or not panel._pending_overflow_label.parent()
        )

    def test_overflow_label_cleared_when_drops_below_cap(self, panel):
        # First render with overflow.
        panel.render(_snap(pending=tuple(_view(id_=f"d{i}") for i in range(8))))
        assert panel._pending_overflow_label is not None
        # Now drops to 3.
        panel.render(_snap(pending=tuple(_view(id_=f"d{i}") for i in range(3))))
        assert panel._pending_overflow_label is None


# ── Resolve callback ────────────────────────────────────────────────


class TestResolve:
    def test_card_click_invokes_resolver(self, panel_with_resolver):
        from PySide6.QtWidgets import QPushButton
        panel, captured = panel_with_resolver
        v = _view(id_="d1")
        panel.render(_snap(pending=(v,)))
        card = panel._decision_cards["d1"]
        # Click Allow.
        btns = {b.objectName(): b for b in card.findChildren(QPushButton)}
        btns["approvalAllow"].click()
        assert len(captured) == 1
        assert captured[0][0] == "d1"
        assert captured[0][1].result is DecisionResult.ALLOW

    def test_resolver_exception_does_not_propagate(self, qtbot):
        from claude_island.ui.capsule_window import CapsuleWindow
        from claude_island.ui.expanded_window import ExpandedWindow
        from PySide6.QtWidgets import QPushButton
        ctrl = IslandController()
        capsule = CapsuleWindow(ctrl)
        qtbot.addWidget(capsule)
        def _bad_resolver(i, d):
            raise RuntimeError("boom")
        pw = ExpandedWindow(
            capsule=capsule, controller=ctrl,
            get_usage_totals=_empty_totals,
            resolve_decision=_bad_resolver,
        )
        qtbot.addWidget(pw)
        v = _view(id_="d1")
        pw.render(_snap(pending=(v,)))
        card = pw._decision_cards["d1"]
        btns = {b.objectName(): b for b in card.findChildren(QPushButton)}
        # Should NOT raise.
        btns["approvalAllow"].click()


# ── No-op when resolver missing (ctor None default) ─────────────────


class TestNoResolver:
    def test_card_renders_even_without_resolver(self, panel):
        # Default panel fixture has resolve_decision=None — but cards
        # still render; click is just a silent no-op (testable: no
        # exception, decision_id stays in cards because nothing
        # resolved it).
        from PySide6.QtWidgets import QPushButton
        v = _view(id_="d1")
        panel.render(_snap(pending=(v,)))
        card = panel._decision_cards["d1"]
        btns = {b.objectName(): b for b in card.findChildren(QPushButton)}
        # Should not raise.
        btns["approvalAllow"].click()


class TestG8ReviewToggleWiring:
    """C-001 regression: ExpandedWindow must accept get_review_mode AND
    forward both getter+setter to SessionDetailPopup, otherwise the G8
    "Review prompts" checkbox is unreachable in production."""

    def test_expanded_window_accepts_get_review_mode(self, qtbot):
        """The kwarg must exist on the constructor signature."""
        from claude_island.ui.capsule_window import CapsuleWindow
        from claude_island.ui.expanded_window import ExpandedWindow
        ctrl = IslandController()
        capsule = CapsuleWindow(ctrl)
        qtbot.addWidget(capsule)
        # Should not raise TypeError("unexpected keyword argument").
        pw = ExpandedWindow(
            capsule=capsule,
            controller=ctrl,
            get_usage_totals=_empty_totals,
            get_review_mode=lambda uuid: False,
            set_review_mode=lambda uuid, on: None,
        )
        qtbot.addWidget(pw)
        # And the wiring should have stored both for the popup to read.
        assert pw._get_review_mode is not None
        assert pw._set_review_mode is not None

    def test_session_detail_popup_receives_both_callbacks(self, qtbot):
        """When ExpandedWindow opens a SessionDetailPopup, both review-
        mode callbacks must be passed through. Without this the popup's
        _build_review_section returns None and the toggle row never
        renders."""
        from datetime import datetime, timezone
        from pathlib import Path

        from claude_island.core.capabilities import (
            Capability,
            FocusGranularity,
        )
        from claude_island.core.models import Session
        from claude_island.core.snapshot import (
            SessionGroup,
            SessionView,
            _degraded_view,
        )
        from claude_island.ui.capsule_window import CapsuleWindow
        from claude_island.ui.expanded_window import ExpandedWindow

        ctrl = IslandController()
        capsule = CapsuleWindow(ctrl)
        qtbot.addWidget(capsule)
        captured_review: dict = {}
        pw = ExpandedWindow(
            capsule=capsule, controller=ctrl,
            get_usage_totals=_empty_totals,
            get_review_mode=lambda uuid: True,
            set_review_mode=lambda uuid, on: captured_review.setdefault(uuid, on),
        )
        qtbot.addWidget(pw)

        # Drive _show_detail_popup with a stub button carrying the view
        # as its '_session' property (mirrors how rows are constructed).
        from PySide6.QtCore import QPoint
        from PySide6.QtWidgets import QPushButton

        sess = Session(
            pid=1, project_path=Path("/tmp/proj"),
            session_uuid="u-popup-test",
            last_activity=datetime.now(timezone.utc),
        )
        view = _degraded_view(sess)
        anchor = QPushButton(parent=pw)
        anchor.setProperty("_session", view)
        pw._show_detail_popup(anchor, QPoint(0, 0))
        popup = pw._active_detail_popup
        assert popup is not None
        # Verify both callbacks landed on the popup.
        assert popup._get_review_mode is not None, (
            "C-001: SessionDetailPopup must receive get_review_mode "
            "from ExpandedWindow — without it the toggle row hides."
        )
        assert popup._set_review_mode is not None
