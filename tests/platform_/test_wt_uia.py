"""Unit tests for wt_uia: select_tab_by_title (T1-T7) + collect_wt_tab_titles (U1-U3)."""
from __future__ import annotations

import sys
from unittest.mock import MagicMock, patch

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform != "win32", reason="Windows-only UIA path"
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_auto_mock(
    *,
    root=None,
    tab_control_exists: bool = True,
    tab_item_exists: bool = True,
    is_selected: bool = False,
    select_raises: BaseException | None = None,
) -> MagicMock:
    """Build a minimal uiautomation module mock for select_tab_by_title."""
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


def _fake_tab_item(name: str) -> MagicMock:
    """A fake UIA child element that quacks like a TabItemControl."""
    item = MagicMock()
    item.ControlTypeName = "TabItemControl"
    item.Name = name
    return item


# ---------------------------------------------------------------------------
# T1: empty title → return False, uiautomation never touched
# ---------------------------------------------------------------------------

def test_empty_title_returns_false_without_calling_uia():
    from claude_island.platform_.wt_uia import select_tab_by_title

    fake = MagicMock()
    with patch.dict("sys.modules", {"uiautomation": fake}):
        result = select_tab_by_title(hwnd=1234, title="")

    assert result is False
    fake.ControlFromHandle.assert_not_called()


# ---------------------------------------------------------------------------
# T2: ControlFromHandle returns None
# ---------------------------------------------------------------------------

def test_control_from_handle_none_returns_false():
    from claude_island.platform_.wt_uia import select_tab_by_title

    fake = _make_auto_mock(root=None)
    fake.ControlFromHandle.return_value = None
    with patch.dict("sys.modules", {"uiautomation": fake}):
        result = select_tab_by_title(hwnd=1234, title="some-title")

    assert result is False


# ---------------------------------------------------------------------------
# T3: TabControl not found (non-tabbed terminal)
# ---------------------------------------------------------------------------

def test_no_tab_control_returns_false():
    from claude_island.platform_.wt_uia import select_tab_by_title

    fake = _make_auto_mock(tab_control_exists=False)
    with patch.dict("sys.modules", {"uiautomation": fake}):
        result = select_tab_by_title(hwnd=1234, title="some-title")

    assert result is False


# ---------------------------------------------------------------------------
# T4: TabItemControl not found (title doesn't match any tab)
# ---------------------------------------------------------------------------

def test_no_matching_tab_item_returns_false():
    from claude_island.platform_.wt_uia import select_tab_by_title

    fake = _make_auto_mock(tab_item_exists=False)
    with patch.dict("sys.modules", {"uiautomation": fake}):
        result = select_tab_by_title(hwnd=1234, title="missing-title")

    assert result is False


# ---------------------------------------------------------------------------
# T5: happy path — tab exists and is not yet selected → Select called once
# ---------------------------------------------------------------------------

def test_selects_tab_when_not_already_selected():
    from claude_island.platform_.wt_uia import select_tab_by_title

    fake = _make_auto_mock(is_selected=False)
    with patch.dict("sys.modules", {"uiautomation": fake}):
        result = select_tab_by_title(hwnd=1234, title="my-tab")

    assert result is True
    root = fake.ControlFromHandle.return_value
    pattern = (
        root.TabControl.return_value
        .TabItemControl.return_value
        .GetSelectionItemPattern.return_value
    )
    pattern.Select.assert_called_once()


# ---------------------------------------------------------------------------
# T6: tab already selected → Select NOT called, return True
# ---------------------------------------------------------------------------

def test_already_selected_returns_true_without_select():
    from claude_island.platform_.wt_uia import select_tab_by_title

    fake = _make_auto_mock(is_selected=True)
    with patch.dict("sys.modules", {"uiautomation": fake}):
        result = select_tab_by_title(hwnd=1234, title="my-tab")

    assert result is True
    root = fake.ControlFromHandle.return_value
    pattern = (
        root.TabControl.return_value
        .TabItemControl.return_value
        .GetSelectionItemPattern.return_value
    )
    pattern.Select.assert_not_called()


# ---------------------------------------------------------------------------
# T7: Select raises (COMError / UIA RPC failure) → False, no exception leak
# ---------------------------------------------------------------------------

def test_select_exception_returns_false_without_raising():
    from claude_island.platform_.wt_uia import select_tab_by_title

    class COMError(Exception):
        pass

    fake = _make_auto_mock(select_raises=COMError("rpc failed"))
    with patch.dict("sys.modules", {"uiautomation": fake}):
        result = select_tab_by_title(hwnd=1234, title="my-tab")

    assert result is False


# ===========================================================================
# collect_wt_tab_titles
# ===========================================================================

# ---------------------------------------------------------------------------
# U1: no WT windows found by EnumWindows → returns None (caller fail-open)
# ---------------------------------------------------------------------------

def test_collect_returns_none_when_no_wt_windows():
    """EnumWindows yields zero CASCADIA-class windows → None.

    Caller (ProcessScanner._filter_orphans) treats None as "skip orphan
    filter" so no claude.exe is mistakenly hidden when WT isn't running.
    """
    from claude_island.platform_ import wt_uia

    win32gui = MagicMock()
    win32gui.GetClassName.return_value = "Chrome_WidgetWin_1"  # not WT
    win32gui.IsWindowVisible.return_value = True

    def fake_enum(cb, _arg):
        cb(11111, None)
        cb(22222, None)
        return True

    win32gui.EnumWindows.side_effect = fake_enum

    auto = MagicMock()
    with patch.dict("sys.modules", {
        "uiautomation": auto,
        "win32gui": win32gui,
    }):
        result = wt_uia.collect_wt_tab_titles()

    assert result is None
    auto.ControlFromHandle.assert_not_called()


# ---------------------------------------------------------------------------
# U2: multiple WT windows + multiple tabs → union of all tab.Name values
# ---------------------------------------------------------------------------

def test_collect_returns_union_of_tab_names():
    """Two WT top-level windows, each with several tabs.
    Result is the union of every TabItem.Name (deduped via set)."""
    from claude_island.platform_ import wt_uia

    win32gui = MagicMock()

    def fake_class(hwnd):
        return "CASCADIA_HOSTING_WINDOW_CLASS" if hwnd in (10, 20) else "Other"

    win32gui.GetClassName.side_effect = fake_class
    win32gui.IsWindowVisible.return_value = True

    def fake_enum(cb, _arg):
        cb(10, None)
        cb(20, None)
        cb(30, None)  # not WT, ignored by class filter
        return True

    win32gui.EnumWindows.side_effect = fake_enum

    auto = MagicMock()

    # Configure the per-hwnd UIA tree.
    # Window 10: tabs ["proj-a", "proj-b"]
    # Window 20: tabs ["proj-b", "proj-c"]   (note duplicate proj-b)
    root_10 = MagicMock()
    tab_ctrl_10 = MagicMock()
    tab_ctrl_10.Exists.return_value = True
    tab_ctrl_10.GetChildren.return_value = [
        _fake_tab_item("proj-a"),
        _fake_tab_item("proj-b"),
    ]
    root_10.TabControl.return_value = tab_ctrl_10

    root_20 = MagicMock()
    tab_ctrl_20 = MagicMock()
    tab_ctrl_20.Exists.return_value = True
    tab_ctrl_20.GetChildren.return_value = [
        _fake_tab_item("proj-b"),
        _fake_tab_item("proj-c"),
    ]
    root_20.TabControl.return_value = tab_ctrl_20

    def from_handle(h):
        return {10: root_10, 20: root_20}.get(h)

    auto.ControlFromHandle.side_effect = from_handle

    with patch.dict("sys.modules", {
        "uiautomation": auto,
        "win32gui": win32gui,
    }):
        result = wt_uia.collect_wt_tab_titles()

    assert result == {"proj-a", "proj-b", "proj-c"}


# ---------------------------------------------------------------------------
# U3: any UIA exception → None (no leak), caller fail-open
# ---------------------------------------------------------------------------

def test_collect_swallows_uia_exceptions_returns_none():
    """If ControlFromHandle raises for the only WT window, the result
    should be None (no titles collected; caller treats as unknown)."""
    from claude_island.platform_ import wt_uia

    win32gui = MagicMock()
    win32gui.GetClassName.return_value = "CASCADIA_HOSTING_WINDOW_CLASS"
    win32gui.IsWindowVisible.return_value = True

    def fake_enum(cb, _arg):
        cb(10, None)
        return True

    win32gui.EnumWindows.side_effect = fake_enum

    auto = MagicMock()
    auto.ControlFromHandle.side_effect = RuntimeError("boom")

    with patch.dict("sys.modules", {
        "uiautomation": auto,
        "win32gui": win32gui,
    }):
        result = wt_uia.collect_wt_tab_titles()

    # Empty set is normalised to None by collect_wt_tab_titles.
    assert result is None


# ---------------------------------------------------------------------------
# U-bonus: EnumWindows itself raises → None
# ---------------------------------------------------------------------------

def test_collect_returns_none_when_enum_raises():
    from claude_island.platform_ import wt_uia

    win32gui = MagicMock()
    win32gui.EnumWindows.side_effect = RuntimeError("enum failed")

    auto = MagicMock()
    with patch.dict("sys.modules", {
        "uiautomation": auto,
        "win32gui": win32gui,
    }):
        result = wt_uia.collect_wt_tab_titles()

    assert result is None
