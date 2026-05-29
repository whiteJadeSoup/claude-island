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
    """A 4-option question with descriptions must reserve real height for every
    row. Pre-fix the option rows used `height:` (ignored by the ColumnLayout) so
    they reserved 0 and overlapped — the card stayed short (~question-text only).
    Each option row is ~40px+; 4 options + toggle + jump must clear 150px.
    """
    question = dict(_APPROVAL)
    question.update(
        kind="ask_question",
        question_text="Which direction should the memory system take next?",
        options=["真机端到端验证", "P3 可写", "P4 可久", "巩固已交付"],
        option_descriptions=[
            "跑 uv run mini-cc, 让真实 LLM 看到注入的 MEMORY.md 索引并存一条 memory。",
            "写前去重 + 可选的子代理会话结束时把对话蒸馏成 memory。",
            "读取侧时效治理:给注入的 memory 加 age/新鲜度提示。",
            "不加新功能:code review 最近的 feat(memory) 提交并处理延后边界。",
        ],
    )
    engine, comp, card = _make_card(question)
    ih = card.property("implicitHeight")
    assert ih is not None and ih > 150, (
        f"DecisionCard(question).implicitHeight={ih} — option rows reserve no "
        f"height, so they overlap (Image #8). Expected > 150 for 4 options."
    )
