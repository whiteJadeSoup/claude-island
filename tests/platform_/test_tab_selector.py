"""Unit tests for tab_selector.select_tab_by_title (T1–T7)."""
from __future__ import annotations

import sys
from unittest.mock import MagicMock, patch

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform != "win32", reason="Windows-only UIA path"
)


def _make_auto_mock(
    *,
    root=None,
    tab_control_exists: bool = True,
    tab_item_exists: bool = True,
    is_selected: bool = False,
    select_raises: BaseException | None = None,
) -> MagicMock:
    """Build a minimal uiautomation module mock."""
    auto = MagicMock()

    if root is None:
        root_obj = MagicMock()
        auto.ControlFromHandle.return_value = root_obj
    else:
        auto.ControlFromHandle.return_value = root
        root_obj = root

    tab_control = MagicMock()
    tab_control.Exists.return_value = tab_control_exists
    root_obj.TabControl.return_value = tab_control

    tab_item = MagicMock()
    tab_item.Exists.return_value = tab_item_exists
    tab_control.TabItemControl.return_value = tab_item

    pattern = MagicMock()
    pattern.IsSelected = is_selected
    if select_raises is not None:
        pattern.Select.side_effect = select_raises
    tab_item.GetSelectionItemPattern.return_value = pattern

    return auto


# ---------------------------------------------------------------------------
# T1: empty title → return False, uiautomation never touched
# ---------------------------------------------------------------------------

def test_empty_title_returns_false_without_calling_uia():
    from claude_island.platform_.tab_selector import select_tab_by_title

    fake = MagicMock()
    with patch.dict("sys.modules", {"uiautomation": fake}):
        result = select_tab_by_title(hwnd=1234, title="")

    assert result is False
    fake.ControlFromHandle.assert_not_called()


# ---------------------------------------------------------------------------
# T2: ControlFromHandle returns None
# ---------------------------------------------------------------------------

def test_control_from_handle_none_returns_false():
    from claude_island.platform_.tab_selector import select_tab_by_title

    fake = _make_auto_mock(root=None)
    fake.ControlFromHandle.return_value = None
    with patch.dict("sys.modules", {"uiautomation": fake}):
        result = select_tab_by_title(hwnd=1234, title="some-title")

    assert result is False


# ---------------------------------------------------------------------------
# T3: TabControl not found (non-tabbed terminal)
# ---------------------------------------------------------------------------

def test_no_tab_control_returns_false():
    from claude_island.platform_.tab_selector import select_tab_by_title

    fake = _make_auto_mock(tab_control_exists=False)
    with patch.dict("sys.modules", {"uiautomation": fake}):
        result = select_tab_by_title(hwnd=1234, title="some-title")

    assert result is False


# ---------------------------------------------------------------------------
# T4: TabItemControl not found (title doesn't match any tab)
# ---------------------------------------------------------------------------

def test_no_matching_tab_item_returns_false():
    from claude_island.platform_.tab_selector import select_tab_by_title

    fake = _make_auto_mock(tab_item_exists=False)
    with patch.dict("sys.modules", {"uiautomation": fake}):
        result = select_tab_by_title(hwnd=1234, title="missing-title")

    assert result is False


# ---------------------------------------------------------------------------
# T5: happy path — tab exists and is not yet selected → Select called once
# ---------------------------------------------------------------------------

def test_selects_tab_when_not_already_selected():
    from claude_island.platform_.tab_selector import select_tab_by_title

    fake = _make_auto_mock(is_selected=False)
    with patch.dict("sys.modules", {"uiautomation": fake}):
        result = select_tab_by_title(hwnd=1234, title="my-tab")

    assert result is True
    root = fake.ControlFromHandle.return_value
    pattern = root.TabControl.return_value.TabItemControl.return_value.GetSelectionItemPattern.return_value
    pattern.Select.assert_called_once()


# ---------------------------------------------------------------------------
# T6: tab already selected → Select NOT called, return True
# ---------------------------------------------------------------------------

def test_already_selected_returns_true_without_select():
    from claude_island.platform_.tab_selector import select_tab_by_title

    fake = _make_auto_mock(is_selected=True)
    with patch.dict("sys.modules", {"uiautomation": fake}):
        result = select_tab_by_title(hwnd=1234, title="my-tab")

    assert result is True
    root = fake.ControlFromHandle.return_value
    pattern = root.TabControl.return_value.TabItemControl.return_value.GetSelectionItemPattern.return_value
    pattern.Select.assert_not_called()


# ---------------------------------------------------------------------------
# T7: Select raises (COMError / UIA RPC failure) → False, no exception leak
# ---------------------------------------------------------------------------

def test_select_exception_returns_false_without_raising():
    from claude_island.platform_.tab_selector import select_tab_by_title

    class COMError(Exception):
        pass

    fake = _make_auto_mock(select_raises=COMError("rpc failed"))
    with patch.dict("sys.modules", {"uiautomation": fake}):
        result = select_tab_by_title(hwnd=1234, title="my-tab")

    assert result is False
