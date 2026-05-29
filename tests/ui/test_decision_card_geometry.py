"""Regression guard for the decision-card overlap bug (Images #6/#7).

Root cause: DecisionCard is a Rectangle whose content ColumnLayout is anchored
top/left/right only, so it never drives the Rectangle's implicitHeight. Loaded
via the Loader in Main.qml's `bands` ColumnLayout, the Loader then reserves
ZERO vertical space (Loader.implicitHeight == item.implicitHeight == 0), and
every band below it (TODAY / Active / Idle) overlaps the card's real content.

This test instantiates DecisionCard.qml standalone with a realistic approval
decision and asserts it reports a non-trivial implicitHeight. Pre-fix: 0.
"""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from pathlib import Path

from PySide6.QtCore import QUrl
from PySide6.QtGui import QGuiApplication
from PySide6.QtQml import QQmlComponent, QQmlEngine
from PySide6.QtQuickControls2 import QQuickStyle

# The real app sets the Basic style globally (qml_app.main); mirror it here so
# Controls inside DecisionCard don't emit native-style customization warnings.
QQuickStyle.setStyle("Basic")

_DECISION_QML = (
    Path(__file__).resolve().parents[2]
    / "claude_island" / "ui" / "qml" / "DecisionCard.qml"
)

_APPROVAL = {
    "id": "d1",
    "kind": "pre_tool_use",            # not "ask_question" → approval body
    "session_name": "mini-cc-opus-dev",
    "session_uuid": "u-1",
    "risk": "high",
    "tool_name": "Bash",
    "tool_input_preview": "uv run pytest -q 2>&1 | tail -15",
    "question_text": "",
    "options": [],
    "option_descriptions": [],
    "multi_select": False,
}


def _make_card(decision: dict):
    engine = QQmlEngine()
    comp = QQmlComponent(engine, QUrl.fromLocalFile(str(_DECISION_QML)))
    assert comp.status() == QQmlComponent.Status.Ready, comp.errorString()
    card = comp.createWithInitialProperties({"decision": decision, "vm": None})
    assert card is not None, comp.errorString()
    # Give it the width it gets in the panel so the content column lays out,
    # then let the layout engine polish so implicitHeight is computed.
    card.setProperty("width", 440)
    app = QGuiApplication.instance()
    for _ in range(5):
        app.processEvents()
    return engine, comp, card


def test_decision_card_reserves_height_approval():
    engine, comp, card = _make_card(_APPROVAL)
    ih = card.property("implicitHeight")
    assert ih is not None and ih > 40, (
        f"DecisionCard.implicitHeight={ih} — the card reserves no height, so the "
        f"Loader in bands collapses to 0 and TODAY/Active overlap it (Image #6/#7)."
    )


def test_decision_card_reserves_height_question():
    question = dict(_APPROVAL)
    question.update(
        kind="ask_question",
        question_text="Which approach?",
        options=["A", "B", "C"],
        option_descriptions=["first", "second", "third"],
    )
    engine, comp, card = _make_card(question)
    ih = card.property("implicitHeight")
    assert ih is not None and ih > 40, (
        f"DecisionCard(question).implicitHeight={ih} — question card reserves no height."
    )
