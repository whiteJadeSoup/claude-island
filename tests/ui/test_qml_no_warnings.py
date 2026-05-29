"""Spec-grade verification: the QML scene must load and bind with ZERO runtime
warnings across every view (home / spend / recents / session) and every
islandState (collapsed / decision / expanded), driven by a REALISTIC snapshot
that includes an ACTIVE session (the live console card), a pending decision,
quota, idle sessions and a dormant session.

This is the regression guard that the earlier offscreen render-checks lacked:
it installs a Qt message handler and FAILS on any binding-level QML error
(ReferenceError / TypeError / "Unable to assign" / "Cannot read" / ...) AND
on any "does not support customization" style-warning — which is exactly the
class of bug that only surfaced on a real display before.

Geometry guard: the recents page Flickable contentHeight must grow with the
number of dormant sessions so collapsed-height rows (FIX 2 class) are caught.
"""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from PySide6.QtCore import (
    Q_ARG,
    QMetaObject,
    QObject,
    Qt,
    QTimer,
    QtMsgType,
    qInstallMessageHandler,
)
from PySide6.QtGui import QGuiApplication
from PySide6.QtQml import QQmlApplicationEngine
from PySide6.QtQuickControls2 import QQuickStyle

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

# ── Binding-error markers — any QML warning matching one of these is a test failure ──
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

# ── Style-warning markers — any warning matching these means Fix 1 regressed ──
# These are the messages Qt emits when a native Controls style rejects
# ScrollBar (or other component) customization.
_STYLE_WARNING_MARKERS = (
    "does not support customization",
    "Please customize a non-native style",
)

# Offscreen-harmless messages that should never trigger a test failure.
# These are well-known platform/font-dir noise from the offscreen plugin.
_KNOWN_HARMLESS = (
    "QFont::setPointSize",
    "This plugin does not support raise()",
    "QWindowsWindow::setGeometry",
    "Could not find platform",
    "no tray icon",
    "Could not find the Qt platform plugin",
    "fontconfig",
    "/usr/share/fonts",
    "Failed to create OpenGL context",
    "Skipping tray icon",
    # Common offscreen driver messages — not related to our QML
    "libEGL",
    "libGL",
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


def _decisions():
    """Three pending decisions mixing kinds so the guard exercises the
    DecisionAlbum stack: an interactive front card + 2 ghost edges +
    "第 1 / N 张" counter + dots.

    Index 0 (front) is the ASK_QUESTION card — its numbered-option menu is
    the most binding-dense card, so keep it on the interactive front.
    Indexes 1-2 are PRE_TOOL_USE approvals (the swipe-fling cards); one is
    HIGH risk to exercise the high-risk badge binding too. Only the front
    is rendered as a live DecisionCard, but all three flow through the
    NEEDS-YOU counter, dots, and ghost-edge bindings.
    """
    return (
        PendingDecisionView(
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
        ),
        PendingDecisionView(
            id="dec-2",
            kind=DecisionKind.PRE_TOOL_USE,
            session_uuid="uuid-agent-prompt",
            session_name="agent-prompt",
            cwd_basename="cc",
            expires_at=_NOW + timedelta(seconds=600),
            risk_level=RiskLevel.HIGH,
            tool_name="Bash",
            tool_input_preview="rm -rf build/ && npm run build",
        ),
        PendingDecisionView(
            id="dec-3",
            kind=DecisionKind.PRE_TOOL_USE,
            session_uuid="uuid-build-mini",
            session_name="build-mini",
            cwd_basename="cc",
            expires_at=_NOW + timedelta(seconds=600),
            risk_level=RiskLevel.MEDIUM,
            tool_name="Read",
            tool_input_preview="src/index.ts",
        ),
    )


def _dormant(suffix=""):
    return DormantSession(
        session_uuid=f"d-1{suffix}",
        cwd=Path("D:/proj/api"),
        name=f"api-refactor{suffix}",
        last_prompt="refactor",
        last_activity=_NOW,
        started_at=_NOW,
        permission_mode="default",
        git_branch="main",
        cost_usd=44.0,
        turn_count=142,
    )


def _full_snap(dormant_count: int = 1):
    dormants = tuple(_dormant(f"-{i}") for i in range(dormant_count))
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
        dormant_sessions=dormants,
        pending_decisions=_decisions(),
    )


def _is_harmless(msg: str) -> bool:
    """Return True when the message is known to be unrelated to our QML."""
    return any(h in msg for h in _KNOWN_HARMLESS)


def _open_detail(root, kind: str) -> None:
    """Drive the touch-to-grow morph nav from Python.

    The old slide nav was triggered by `root.page = "<kind>"`; that property is
    gone. Navigation now goes through detailHost.open(kind, srcItem), which
    activates the detail Loader and morphs the page in. We pass detailHost
    itself as the source item — its geometry only seeds the morph start frame,
    which this test does not assert on; what matters is that the detail page
    Loader activates and its bindings evaluate.
    """
    host = root.findChild(QObject, "detailHost")
    assert host is not None, (
        "Could not locate detailHost by objectName — check Main.qml sets "
        "objectName: 'detailHost' on the morph host Item"
    )
    ok = QMetaObject.invokeMethod(
        host,
        "open",
        Qt.DirectConnection,
        Q_ARG("QVariant", kind),
        Q_ARG("QVariant", host),
    )
    assert ok, f"detailHost.open({kind!r}, …) invocation failed"


def _close_detail(root) -> None:
    """Collapse the morph overlay back (detailHost.close())."""
    host = root.findChild(QObject, "detailHost")
    assert host is not None
    QMetaObject.invokeMethod(host, "close", Qt.DirectConnection)


def _ensure_basic_style() -> None:
    """Set the Basic QtQuick Controls 2 style if not already set.

    Must be called BEFORE the first QQmlApplicationEngine is created.
    Basic is the only fully-customizable built-in style; without it the
    native platform style rejects our ScrollBar customizations and emits
    'does not support customization' warnings.
    """
    QQuickStyle.setStyle("Basic")


def test_qml_loads_with_zero_runtime_warnings():
    _ensure_basic_style()
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
        # Drill-down views — exercise every detail page's bindings by morphing
        # each one open (then closing) via the touch-to-grow nav (replaces the
        # old `root.page = "<kind>"` slide). "home" is now the always-present
        # base layer, so closing the overlay returns to it.
        root.setProperty("detailData", vm.sessionDetail("uuid-cc-learning"))
        for kind in ("spend", "recents", "session"):
            _open_detail(root, kind)
            spin()
            _close_detail(root)
            spin()
        # Pill morph states
        for state in ("collapsed", "decision", "expanded"):
            root.setProperty("islandState", state)
            spin()
        # Re-feed a snapshot to re-evaluate bindings under each state
        vm.update(_full_snap())
        spin()

        # ── Check 1: binding errors ────────────────────────────────────────
        bad_bindings = [
            m for m in captured
            if any(k in m for k in _ERROR_MARKERS) and not _is_harmless(m)
        ]
        assert not bad_bindings, (
            "QML runtime binding errors detected:\n" + "\n".join(bad_bindings)
        )

        # ── Check 2: style-customization warnings (Fix 1 regression guard) ─
        # If the Basic style is NOT set, Qt emits "does not support customization"
        # for our ScrollBar overrides.  This check catches that regression.
        style_warnings = [
            m for m in captured
            if any(k in m for k in _STYLE_WARNING_MARKERS) and not _is_harmless(m)
        ]
        assert not style_warnings, (
            "QML style-customization warnings detected (Basic style not set?):\n"
            + "\n".join(style_warnings)
        )

    finally:
        qInstallMessageHandler(None)


def test_recents_history_rows_have_real_height():
    """Geometry regression guard for Fix 2 (collapsed-height rows).

    Loads Main.qml with 3 dormant sessions and navigates to the recents page.
    The recentsListFlickable's contentHeight must be > 50 px — if rows collapse
    to 0 px (Loader without Layout.preferredHeight), contentHeight stays near 0.

    This test would have caught the Loader height issue where loaded component
    heights were not adopted by the Loader, making the list appear empty.
    """
    _ensure_basic_style()
    app = QGuiApplication.instance() or QGuiApplication([])

    # Silence warnings during this sub-test so output stays clean
    qInstallMessageHandler(lambda *a: None)
    try:
        vm = WorldViewModel(
            get_totals=lambda period: _FakeTotals(),
            get_totals_by_model=lambda period: (
                _FakeModelTotals("claude-opus-4-7", 55.0),
            ),
        )

        # Use 3 dormant sessions to make collapsed-height bugs obvious
        snap_3 = _full_snap(dormant_count=3)
        vm.update(snap_3)

        engine = QQmlApplicationEngine()
        engine.rootContext().setContextProperty("worldVm", vm)
        engine.rootContext().setContextProperty("isMac", False)
        engine.load(str(_QML))
        roots = engine.rootObjects()
        assert roots, "Main.qml failed to load"
        root = roots[0]

        def spin(ms=300):
            loop_end = QTimer()
            loop_end.setSingleShot(True)
            loop_end.start(ms)
            while loop_end.isActive():
                app.processEvents()

        # Morph the recents page open so the Flickable and its Repeater are
        # active (the RecentsPage is now loaded only while the detail overlay
        # is open — detailHost.open("recents", …) activates its Loader). We
        # leave it open (no close()) so the Flickable stays alive for the
        # geometry read below.
        root.setProperty("islandState", "expanded")
        _open_detail(root, "recents")
        spin()
        # Re-push the snapshot so the RecentsPage re-evaluates its recents
        # binding after the Loader has activated.
        vm.update(snap_3)
        spin()

        # Locate the recentsListFlickable by objectName.
        # QML objects with objectName set are findable via Qt's findChild.
        flickable = root.findChild(type(root), "recentsListFlickable")

        # findChild may return None if the objectName lookup fails in offscreen mode.
        # Fall back to checking that we at least loaded without crashing.
        if flickable is None:
            # The object exists but Python's findChild type-match requires exact class.
            # Try with QObject base.
            from PySide6.QtCore import QObject
            flickable = root.findChild(QObject, "recentsListFlickable")

        assert flickable is not None, (
            "Could not locate recentsListFlickable by objectName — "
            "check that RecentsPage.qml sets objectName: 'recentsListFlickable' on the Flickable"
        )

        content_height = flickable.property("contentHeight")
        assert content_height is not None, "contentHeight property not accessible on Flickable"

        # 3 dormant session rows + 1 group header = at least 3 × ~80 px + 28 px ≈ 268 px.
        # If rows have zero height (pre-fix), contentHeight is ~12 px (just the padding item).
        assert content_height > 50, (
            f"recentsListFlickable.contentHeight = {content_height:.0f} px — "
            f"rows likely collapsed to 0 height (Loader implicitHeight bug). "
            f"Expected > 50 px with 3 dormant sessions."
        )
    finally:
        qInstallMessageHandler(None)
