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


# ---------------------------------------------------------------------------
# wait_for_tab_name — used after set_console_title to confirm WT
# propagated the OSC update into TabItem.Name before we issue
# select_tab_by_title.
# ---------------------------------------------------------------------------

class TestWaitForTabName:

    def test_returns_true_immediately_when_already_present(self):
        """Fast path: TabItem with the target Name already exists at
        the first poll — return True without sleeping."""
        from claude_island.platform_ import wt_uia

        auto = _make_auto_mock(tab_item_exists=True)
        with patch.dict("sys.modules", {"uiautomation": auto}):
            ok = wt_uia.wait_for_tab_name(
                hwnd=0xCAFE, name="ci:abc", timeout_ms=200,
            )

        assert ok is True

    def test_returns_false_on_timeout(self):
        """TabItem never appears within timeout → return False so the
        caller can fall back (typically to plain SetForegroundWindow)."""
        from claude_island.platform_ import wt_uia

        auto = _make_auto_mock(tab_item_exists=False)
        with patch.dict("sys.modules", {"uiautomation": auto}):
            ok = wt_uia.wait_for_tab_name(
                hwnd=0xCAFE, name="ci:abc",
                timeout_ms=30, poll_ms=10,
            )

        assert ok is False

    def test_empty_name_returns_false(self):
        """Defensive: empty name would match no TabItem and would also
        be a degraded sentinel path (uuid-less SessionView)."""
        from claude_island.platform_ import wt_uia

        ok = wt_uia.wait_for_tab_name(hwnd=0xCAFE, name="", timeout_ms=10)
        assert ok is False

    def test_uia_exception_does_not_bubble(self):
        """ControlFromHandle / TabControl / TabItemControl raising mid
        poll must not crash the caller — keep polling until deadline."""
        from claude_island.platform_ import wt_uia

        auto = MagicMock()
        auto.ControlFromHandle.side_effect = RuntimeError("UIA hiccup")

        with patch.dict("sys.modules", {"uiautomation": auto}):
            ok = wt_uia.wait_for_tab_name(
                hwnd=0xCAFE, name="ci:abc",
                timeout_ms=20, poll_ms=10,
            )

        assert ok is False  # never found, but didn't raise

    def test_eventual_appearance_returns_true(self):
        """TabItem.Exists returns False initially, then True on a later
        poll — should return True without waiting for the full timeout."""
        from claude_island.platform_ import wt_uia

        auto = MagicMock()
        root = MagicMock()
        auto.ControlFromHandle.return_value = root
        tab_control = MagicMock()
        tab_control.Exists.return_value = True
        root.TabControl.return_value = tab_control

        # First two calls → not found, third → found.
        tab_item = MagicMock()
        tab_item.Exists.side_effect = [False, False, True, True]
        tab_control.TabItemControl.return_value = tab_item

        with patch.dict("sys.modules", {"uiautomation": auto}):
            ok = wt_uia.wait_for_tab_name(
                hwnd=0xCAFE, name="ci:abc",
                timeout_ms=200, poll_ms=10,
            )

        assert ok is True
        assert tab_item.Exists.call_count >= 3


# ---------------------------------------------------------------------------
# select_any_ci_tab — split-pane click fallback. Picks the first
# TabItem under hwnd whose Name starts with "ci:" so WT lands on a
# tab containing one of our sessions, even if not the exact pane.
# ---------------------------------------------------------------------------

class TestSelectAnyCiTab:

    def _make_tabs_mock(self, *, tab_names: list[str], existing: bool = True):
        """Build an auto mock with a TabControl whose children are
        TabItemControls with the given Names."""
        auto = MagicMock()
        root = MagicMock()
        auto.ControlFromHandle.return_value = root
        tab_control = MagicMock()
        tab_control.Exists.return_value = True
        root.TabControl.return_value = tab_control

        # GetChildren returns TabItemControl-typed mocks. Each one has
        # a SelectionItemPattern we can inspect.
        children = []
        patterns = []
        for name in tab_names:
            child = MagicMock()
            child.ControlTypeName = "TabItemControl"
            child.Name = name
            pattern = MagicMock()
            pattern.IsSelected = False
            child.GetSelectionItemPattern.return_value = pattern
            children.append(child)
            patterns.append(pattern)
        tab_control.GetChildren.return_value = children

        return auto, patterns

    def test_selects_first_ci_prefix_tab(self):
        """Common case: WT window with a Claude Code default tab and
        a labeled ci:* tab. We pick the ci:* one."""
        from claude_island.platform_ import wt_uia

        auto, patterns = self._make_tabs_mock(
            tab_names=["Claude Code", "ci:abc", "ci:def"],
        )
        with patch.dict("sys.modules", {"uiautomation": auto}):
            ok = wt_uia.select_any_ci_tab(hwnd=0xCAFE)

        assert ok is True
        # Patterns list maps 1:1 to tab_names. The first ci:* is index 1.
        patterns[0].Select.assert_not_called()
        patterns[1].Select.assert_called_once()

    def test_returns_true_no_op_when_already_selected(self):
        """If the first ci:* tab is already the active one, no Select
        call is needed — still success."""
        from claude_island.platform_ import wt_uia

        auto, patterns = self._make_tabs_mock(tab_names=["ci:abc"])
        patterns[0].IsSelected = True

        with patch.dict("sys.modules", {"uiautomation": auto}):
            ok = wt_uia.select_any_ci_tab(hwnd=0xCAFE)

        assert ok is True
        patterns[0].Select.assert_not_called()

    def test_returns_false_when_no_ci_tab(self):
        """Window with no ci:* tabs (user closed all our sessions or
        targeted a window that has none) — fallback fails so caller
        can degrade to plain SetForegroundWindow."""
        from claude_island.platform_ import wt_uia

        auto, _ = self._make_tabs_mock(
            tab_names=["Claude Code", "PowerShell"],
        )
        with patch.dict("sys.modules", {"uiautomation": auto}):
            ok = wt_uia.select_any_ci_tab(hwnd=0xCAFE)

        assert ok is False

    def test_returns_false_when_no_tab_control(self):
        """Non-WT terminal (conhost / cmd.exe stand-alone) — no
        TabControl in the UIA tree."""
        from claude_island.platform_ import wt_uia

        auto = MagicMock()
        root = MagicMock()
        auto.ControlFromHandle.return_value = root
        tab_control = MagicMock()
        tab_control.Exists.return_value = False
        root.TabControl.return_value = tab_control

        with patch.dict("sys.modules", {"uiautomation": auto}):
            ok = wt_uia.select_any_ci_tab(hwnd=0xCAFE)

        assert ok is False

    def test_uia_exception_returns_false(self):
        """ControlFromHandle / GetChildren raising must not bubble."""
        from claude_island.platform_ import wt_uia

        auto = MagicMock()
        auto.ControlFromHandle.side_effect = RuntimeError("UIA disconnected")

        with patch.dict("sys.modules", {"uiautomation": auto}):
            ok = wt_uia.select_any_ci_tab(hwnd=0xCAFE)

        assert ok is False


# ---------------------------------------------------------------------------
# enumerate_active_tab_sentinels — feeds PaneSiblingTracker. Walks
# the active TabItem's subtree and returns ci:* TermControl Names.
# ---------------------------------------------------------------------------

class TestEnumerateActiveTabSentinels:
    """The new BFS algorithm: walk root's subtree, skip TabItem
    children (tab strip labels, not pane content), collect Name=ci:*.

    Realistic WT UIA tree shape we're emulating:
    ┌─ root (WT window)
    │  ├─ TabControl (the tab strip)
    │  │   └─ TabItem (label: Image + TextBlock + Button)  ← SKIPPED
    │  │   └─ TabItem ...                                  ← SKIPPED
    │  └─ ContentArea (content of active tab)
    │      └─ Pane (split layout)
    │          ├─ TermControl(Name="ci:build")
    │          └─ TermControl(Name="ci:mini")
    """

    def _term(self, name: str) -> MagicMock:
        tc = MagicMock()
        tc.ControlTypeName = "PaneControl"  # WT TermControl is Pane control type
        tc.ClassName = "TermControl"
        tc.Name = name
        tc.GetChildren.return_value = []  # we skip TermControl subtree
        return tc

    def _tab_strip_label(self, label_name: str) -> MagicMock:
        """A TabItemControl in the tab strip — has Name (the tab
        label) but its subtree is just label widgets, NOT panes."""
        tab = MagicMock()
        tab.ControlTypeName = "TabItemControl"
        tab.ClassName = "TabViewItem"
        tab.Name = label_name
        # Children are label widgets — Image, TextBlock, Button.
        # In real WT, TextBlock.Name == TabItem.Name (the same
        # sentinel string). If our BFS descends into TabItem, it
        # would collect this TextBlock as a false sentinel — which
        # is exactly the bug the new "skip TabItem subtree" guards
        # against.
        textblock = MagicMock()
        textblock.ControlTypeName = "TextControl"
        textblock.ClassName = "TextBlock"
        textblock.Name = label_name  # ← same string, would pollute
        textblock.GetChildren.return_value = []
        tab.GetChildren.return_value = [textblock]
        return tab

    def _wrapper(self, *children: object) -> MagicMock:
        """A no-Name layout wrapper (ContentPresenter / Border /
        Grid / Pane) — we descend through these."""
        w = MagicMock()
        w.ControlTypeName = "PaneControl"
        w.ClassName = "ContentPresenter"
        w.Name = ""
        w.GetChildren.return_value = list(children)
        return w

    def _root(self, *children: object) -> tuple[MagicMock, MagicMock]:
        auto = MagicMock()
        root = MagicMock()
        root.GetChildren.return_value = list(children)
        auto.ControlFromHandle.return_value = root
        return auto, root

    def test_collects_active_tab_panes(self):
        """Active tab content area contains 2 panes → both collected."""
        from claude_island.platform_ import wt_uia

        # Realistic shape: tab strip + content area as siblings.
        tab_strip = self._wrapper(
            self._tab_strip_label("ci:build"),
        )
        content = self._wrapper(
            self._wrapper(  # split-pane container
                self._term("ci:build"),
                self._term("ci:mini"),
            ),
        )
        auto, _ = self._root(tab_strip, content)

        with patch.dict("sys.modules", {"uiautomation": auto}):
            result = wt_uia.enumerate_active_tab_sentinels(hwnd=0xCAFE)

        assert result == {"ci:build", "ci:mini"}

    def test_skips_tab_strip_labels(self):
        """The TabItem in the tab strip has Name=ci:build (same as
        the active pane), but BFS must NOT descend into it. Otherwise
        the label's child TextBlock with the same Name would be
        collected as a false sentinel."""
        from claude_island.platform_ import wt_uia

        tab_strip = self._wrapper(
            self._tab_strip_label("ci:build"),
            self._tab_strip_label("ci:other_tab"),  # inactive tab label
        )
        # Content area: only build is here (other_tab's content is
        # virtualized away).
        content = self._wrapper(self._term("ci:build"))
        auto, _ = self._root(tab_strip, content)

        with patch.dict("sys.modules", {"uiautomation": auto}):
            result = wt_uia.enumerate_active_tab_sentinels(hwnd=0xCAFE)

        # Just build (from content area). other_tab's label name
        # is in the strip but subtree is skipped. build's TabItem
        # label is also skipped — only the TermControl in content
        # contributes.
        assert result == {"ci:build"}

    def test_filters_non_ci_names(self):
        """Pane Names that don't start with ci: are ignored."""
        from claude_island.platform_ import wt_uia

        content = self._wrapper(
            self._term("ci:claude_a"),
            self._term("PowerShell"),  # non-claude shell
            self._term("ci:claude_b"),
        )
        auto, _ = self._root(content)

        with patch.dict("sys.modules", {"uiautomation": auto}):
            result = wt_uia.enumerate_active_tab_sentinels(hwnd=0xCAFE)

        assert result == {"ci:claude_a", "ci:claude_b"}

    def test_root_none_returns_empty(self):
        from claude_island.platform_ import wt_uia

        auto = MagicMock()
        auto.ControlFromHandle.return_value = None

        with patch.dict("sys.modules", {"uiautomation": auto}):
            result = wt_uia.enumerate_active_tab_sentinels(hwnd=0xCAFE)

        assert result == set()

    def test_uia_exception_returns_empty(self):
        """Mid-walk UIA exceptions get swallowed; result empty."""
        from claude_island.platform_ import wt_uia

        auto = MagicMock()
        auto.ControlFromHandle.side_effect = RuntimeError("UIA gone")

        with patch.dict("sys.modules", {"uiautomation": auto}):
            result = wt_uia.enumerate_active_tab_sentinels(hwnd=0xCAFE)

        assert result == set()
