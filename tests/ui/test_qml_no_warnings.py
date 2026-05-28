"""Spec-grade verification: the QML scene must load and bind with ZERO runtime
warnings across every view (home / spend / recents / session) and every
islandState (collapsed / decision / expanded), driven by a REALISTIC snapshot
that includes an ACTIVE session (the live console card), a pending decision,
quota, idle sessions and a dormant session.

This is the regression guard that the earlier offscreen render-checks lacked:
it installs a Qt message handler and FAILS on any binding-level QML error
(ReferenceError / TypeError / "Unable to assign" / "Cannot read" / ...), which
is exactly the class of bug that only surfaced on a real display before.
"""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from PySide6.QtCore import QTimer, qInstallMessageHandler, QtMsgType
from PySide6.QtGui import QGuiApplication
from PySide6.QtQml import QQmlApplicationEngine

from claude_island.core.models import DormantSession, QuotaSnapshot, Session
from claude_island.core.pending_decisions import (
    DecisionKind,
    PendingDecisionView,
    RiskLevel,
)
from claude_island.core.session_phase import SessionPhase
from claude_island.core.snapshot import SessionGroup, SessionView, WorldSnapshot
from claude_island.ui.world_view_model import WorldViewModel

_QML = Path(__file__).resolve().parents[2] / "claude_island" / "ui" / "qml" / "Main.qml"

_NOW = datetime(2026, 5, 28, 10, 0, 0, tzinfo=timezone.utc)

_ERROR_MARKERS = (
    "ReferenceError",
    "TypeError",
    "Unable to assign",
    "Cannot read",
    "is not defined",
    "non-existent",
    "is not a function",
    "Unable to assign [undefined]",
)


class _FakeTotals:
    cost_usd = 9.28
    request_count = 400
    input_tokens = 58000
    output_tokens = 188000
    cache_read_tokens = 87_200_000


class _FakeModelTotals:
    def __init__(self, model, cost):
        self.model = model
        self.cost_usd = cost


def _active_view(name, phase, cost, model="claude-opus-4-7", tpm=2600):
    sess = Session(
        pid=4242,
        project_path=Path("D:/Learning/cc"),
        last_activity=_NOW,
        session_uuid="uuid-" + name,
    )
    return SessionView(
        pid=4242,
        name=name,
        project_path=Path("D:/Learning/cc"),
        project_basename="cc",
        last_activity=_NOW,
        cost_usd=cost,
        is_high_cost=cost >= 50.0,
        latest_model=model,
        status_word=None,
        session=sess,
        session_uuid="uuid-" + name,
        phase=phase,
        tokens_per_min=tpm,
    )


def _decision():
    return PendingDecisionView(
        id="dec-1",
        kind=DecisionKind.ASK_QUESTION,
        session_uuid="uuid-cc-learning",
        session_name="cc-learning",
        cwd_basename="cc",
        expires_at=_NOW + timedelta(seconds=600),
        risk_level=RiskLevel.MEDIUM,
        tool_name="AskUserQuestion",
        question_text="Which date library should we use for the new module?",
        question_header="date lib",
        question_options=("date-fns", "Day.js", "Luxon"),
        question_option_descriptions=("lightweight", "2KB", "timezones"),
        multi_select=False,
    )


def _dormant():
    return DormantSession(
        session_uuid="d-1",
        cwd=Path("D:/proj/api"),
        name="api-refactor",
        last_prompt="refactor",
        last_activity=_NOW,
        started_at=_NOW,
        permission_mode="default",
        git_branch="main",
        cost_usd=44.0,
        turn_count=142,
    )


def _full_snap():
    return WorldSnapshot(
        today_cost_usd=63.0,
        quota=QuotaSnapshot(
            five_hour_pct=41.0,
            five_hour_resets_at=_NOW + timedelta(minutes=42),
            seven_day_pct=67.0,
            seven_day_resets_at=_NOW + timedelta(days=2),
            fetched_at=_NOW,
            is_stale=False,
        ),
        available_providers=("anthropic",),
        selected_provider="anthropic",
        fetched_at=_NOW,
        session_groups=(
            SessionGroup(
                group_id="g1",
                title_hint=None,
                adapter_id="",
                views=(
                    _active_view("cc-learning", SessionPhase.THINKING, 264.0),
                    _active_view("agent-prompt", SessionPhase.TOOL_USE, 12.0,
                                 model="claude-sonnet-4-6", tpm=1400),
                    _active_view("build-mini", SessionPhase.IDLE, 0.0),
                ),
            ),
        ),
        dormant_sessions=(_dormant(),),
        pending_decisions=(_decision(),),
    )


def test_qml_loads_with_zero_runtime_warnings():
    app = QGuiApplication.instance() or QGuiApplication([])

    captured: list[str] = []

    def handler(mode, ctx, msg):
        if mode in (QtMsgType.QtWarningMsg, QtMsgType.QtCriticalMsg, QtMsgType.QtFatalMsg):
            captured.append(str(msg))

    qInstallMessageHandler(handler)
    try:
        vm = WorldViewModel(
            get_totals=lambda period: _FakeTotals(),
            get_totals_by_model=lambda period: (
                _FakeModelTotals("claude-opus-4-7", 55.0),
                _FakeModelTotals("claude-sonnet-4-6", 8.0),
            ),
            get_review=lambda uuid: False,
        )
        # Two updates so rate_series accumulates (the waveform reads it).
        vm.update(_full_snap())
        vm.update(_full_snap())

        engine = QQmlApplicationEngine()
        engine.rootContext().setContextProperty("worldVm", vm)
        engine.rootContext().setContextProperty("isMac", False)
        engine.load(str(_QML))
        roots = engine.rootObjects()
        assert roots, "Main.qml failed to load (no root objects)"
        root = roots[0]

        def spin(ms=120):
            loop_end = QTimer()
            loop_end.setSingleShot(True)
            loop_end.start(ms)
            while loop_end.isActive():
                app.processEvents()

        spin()
        # Drill-down views
        root.setProperty("detailData", vm.sessionDetail("uuid-cc-learning"))
        for page in ("spend", "recents", "session", "home"):
            root.setProperty("page", page)
            spin()
        # Pill morph states
        for state in ("collapsed", "decision", "expanded"):
            root.setProperty("islandState", state)
            spin()
        # Re-feed a snapshot to re-evaluate bindings under each state
        vm.update(_full_snap())
        spin()

        bad = [m for m in captured if any(k in m for k in _ERROR_MARKERS)]
        assert not bad, "QML runtime binding errors:\n" + "\n".join(bad)
    finally:
        qInstallMessageHandler(None)
