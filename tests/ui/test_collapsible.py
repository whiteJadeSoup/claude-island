"""Tests for the shared CollapsibleLinkButton helper."""
from __future__ import annotations

from claude_island.ui.collapsible import CollapsibleLinkButton


def test_initial_state_is_collapsed(qtbot):
    btn = CollapsibleLinkButton()
    qtbot.addWidget(btn)
    assert btn.is_expanded() is False
    assert btn.text() == "[展开]"


def test_click_toggles_state_and_label(qtbot):
    btn = CollapsibleLinkButton()
    qtbot.addWidget(btn)
    btn.click()
    assert btn.is_expanded() is True
    assert btn.text() == "[收起]"
    btn.click()
    assert btn.is_expanded() is False
    assert btn.text() == "[展开]"


def test_click_emits_state_changed_with_new_value(qtbot):
    btn = CollapsibleLinkButton()
    qtbot.addWidget(btn)
    received: list[bool] = []
    btn.state_changed.connect(received.append)
    btn.click()
    btn.click()
    btn.click()
    assert received == [True, False, True]


def test_set_expanded_does_not_emit(qtbot):
    btn = CollapsibleLinkButton()
    qtbot.addWidget(btn)
    received: list[bool] = []
    btn.state_changed.connect(received.append)
    btn.set_expanded(True)
    btn.set_expanded(False)
    assert received == []
    # But the visible state must reflect the call:
    btn.set_expanded(True)
    assert btn.text() == "[收起]"
    assert btn.is_expanded() is True


def test_custom_labels(qtbot):
    btn = CollapsibleLinkButton(labels=("[expand]", "[collapse]"))
    qtbot.addWidget(btn)
    assert btn.text() == "[expand]"
    btn.click()
    assert btn.text() == "[collapse]"
