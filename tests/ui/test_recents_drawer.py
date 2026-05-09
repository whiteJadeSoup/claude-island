"""Tests for RecentsDrawer — render + Resume click flow.

Strategy:
* Use pytest-qt's qtbot fixture for QApplication / signal handling.
* Stub the dispatcher so we can assert the LAUNCH dispatch contract
  without touching subprocess.Popen or any real adapter.
* Mock the launch_intent registry to verify add() is called with
  the exact LaunchIntent shape we expect.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

import pytest

from claude_island.core.capabilities import Capability, LauncherSpawnError, SpawnResult
from claude_island.core.launch_intent import LaunchIntent, LaunchIntentRegistry
from claude_island.core.models import DormantSession
from claude_island.core.snapshot import WorldSnapshot
from claude_island.ui.recents_drawer import (
    RecentsDrawer,
    _RecentRow,
    _flags_for_mode,
    _relative_time,
)


# ── Helpers ─────────────────────────────────────────────────────────────

def _dormant(uuid: str = "u1", **kw) -> DormantSession:
    defaults = dict(
        session_uuid=uuid,
        cwd=Path("D:/projects/foo"),
        name=None,
        last_prompt=None,
        last_activity=datetime(2026, 5, 1, 12, tzinfo=timezone.utc),
        started_at=None,
        permission_mode=None,
        git_branch=None,
        cost_usd=0.0,
        turn_count=0,
    )
    defaults.update(kw)
    return DormantSession(**defaults)


def _empty_snap(dormant=(), launching=()) -> WorldSnapshot:
    return WorldSnapshot(
        today_cost_usd=0.0,
        quota=None,
        available_providers=(),
        selected_provider=None,
        fetched_at=datetime(2026, 5, 1, tzinfo=timezone.utc),
        session_groups=(),
        dormant_sessions=tuple(dormant),
        launching_sessions=tuple(launching),
    )


class _FakeDispatcher:
    """Captures launch() calls + lets tests choose what to return."""

    def __init__(
        self,
        *,
        adapters: tuple[str, ...] = ("windows-terminal",),
        spawn_result: SpawnResult | None = None,
        spawn_error: Exception | None = None,
    ):
        self.adapter_names = adapters
        self._spawn_result = spawn_result or SpawnResult(
            terminal_name="windows-terminal",
            terminal_pid=4242,
            started_at=datetime(2026, 5, 1, 12, tzinfo=timezone.utc),
        )
        self._spawn_error = spawn_error
        self.launch_calls: list[dict] = []

    def adapters_with(self, cap):
        if cap is not Capability.LAUNCH:
            return ()
        return tuple((n, mock.Mock()) for n in self.adapter_names)

    def launch(self, adapter_name, *, cwd, command, session_uuid=None):
        self.launch_calls.append({
            "adapter_name": adapter_name,
            "cwd": cwd,
            "command": command,
            "session_uuid": session_uuid,
        })
        if self._spawn_error is not None:
            raise self._spawn_error
        return self._spawn_result


# ── Pure functions ──────────────────────────────────────────────────────

class TestFlagsForMode:
    def test_bypass_translates_to_dangerous_flag(self):
        assert _flags_for_mode("bypassPermissions") == ("--dangerously-skip-permissions",)

    def test_accept_edits_translates(self):
        assert _flags_for_mode("acceptEdits") == ("--permission-mode", "acceptEdits")

    def test_plan_mode_translates(self):
        assert _flags_for_mode("plan") == ("--permission-mode", "plan")

    def test_default_or_none_yields_no_flags(self):
        assert _flags_for_mode("default") == ()
        assert _flags_for_mode(None) == ()
        assert _flags_for_mode("") == ()

    def test_unknown_mode_yields_no_flags(self):
        assert _flags_for_mode("FUTURE_MODE_X") == ()


class TestRelativeTime:
    def test_minutes(self):
        now = datetime(2026, 5, 1, 12, 30, tzinfo=timezone.utc)
        then = datetime(2026, 5, 1, 12, 25, tzinfo=timezone.utc)
        assert _relative_time(then, now=now) == "5m ago"

    def test_hours(self):
        now = datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc)
        then = datetime(2026, 5, 1, 9, 0, tzinfo=timezone.utc)
        assert _relative_time(then, now=now) == "3h ago"

    def test_days(self):
        now = datetime(2026, 5, 5, 12, 0, tzinfo=timezone.utc)
        then = datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc)
        assert _relative_time(then, now=now) == "4d ago"

    def test_falls_back_to_iso_for_old_dates(self):
        now = datetime(2026, 6, 15, tzinfo=timezone.utc)
        then = datetime(2026, 5, 1, tzinfo=timezone.utc)
        assert _relative_time(then, now=now) == "2026-05-01"


# ── RecentsDrawer.compute (dedup key projection) ────────────────────────

class TestCompute:
    def test_compute_changes_when_dormant_changes(self):
        d_a = _dormant("u1", cost_usd=1.0)
        d_b = _dormant("u1", cost_usd=2.0)  # different cost
        snap_a = _empty_snap(dormant=[d_a])
        snap_b = _empty_snap(dormant=[d_b])
        assert RecentsDrawer.compute(snap_a) != RecentsDrawer.compute(snap_b)

    def test_compute_stable_when_unrelated_fields_change(self):
        """Distinct should NOT re-render on unrelated snapshot churn."""
        d = _dormant("u1")
        snap_a = _empty_snap(dormant=[d])
        snap_b = WorldSnapshot(
            today_cost_usd=99.0,  # different
            quota=None,
            available_providers=(),
            selected_provider=None,
            fetched_at=datetime(2026, 5, 1, tzinfo=timezone.utc),
            session_groups=(),
            dormant_sessions=(d,),
            launching_sessions=(),
        )
        assert RecentsDrawer.compute(snap_a) == RecentsDrawer.compute(snap_b)


# ── Drawer rendering ────────────────────────────────────────────────────

@pytest.fixture
def drawer(qtbot):
    """A drawer with a stubbed dispatcher + real LaunchIntentRegistry +
    a recording on_wake. Parent (expanded) is just a bare QWidget so
    _reposition() doesn't blow up."""
    from PySide6.QtWidgets import QWidget
    parent = QWidget()
    qtbot.addWidget(parent)
    dispatcher = _FakeDispatcher()
    registry = LaunchIntentRegistry()
    wakes: list[None] = []
    d = RecentsDrawer(
        expanded=parent,
        dispatcher=dispatcher,
        launch_intent=registry,
        on_wake=lambda: wakes.append(None),
    )
    qtbot.addWidget(d)
    return d, dispatcher, registry, wakes


class TestDrawerRender:
    def test_empty_snap_shows_placeholder(self, drawer):
        d, *_ = drawer
        d.render(_empty_snap())
        # The count is now merged into the header label as
        # "RECENTS · N" (count adjacent to title — no addStretch gap).
        assert "0" in d._header_label.text()

    def test_dormant_rows_appear(self, drawer):
        d, *_ = drawer
        snap = _empty_snap(dormant=[
            _dormant("u1", name="refactor"),
            _dormant("u2", name="bug-fix"),
        ])
        d.render(snap)
        # Two _RecentRow children inside the rows container
        rows = [w for w in d._list_container.children()
                if isinstance(w, _RecentRow)]
        assert len(rows) == 2

    def test_launching_rows_appear(self, drawer):
        d, *_ = drawer
        intent = LaunchIntent(
            session_uuid="u1", cwd=Path("D:/x"), flags=(),
            terminal_name="windows-terminal", terminal_pid=1234,
            requested_at=datetime(2026, 5, 1, tzinfo=timezone.utc),
        )
        d.render(_empty_snap(launching=[intent]))
        # Header label format: "RECENTS · N  ⏳ M" (N dormant, M launching).
        assert "⏳ 1" in d._header_label.text()


class TestResumeClick:
    def test_successful_resume_records_intent_and_wakes(self, drawer):
        d, dispatcher, registry, wakes = drawer
        dormant = _dormant("u1", permission_mode="bypassPermissions")
        d.render(_empty_snap(dormant=[dormant]))
        rows = [w for w in d._list_container.children()
                if isinstance(w, _RecentRow)]
        assert len(rows) == 1
        rows[0]._on_resume()
        # Dispatcher was asked about LAUNCH first, then launch() called
        assert dispatcher.launch_calls
        call = dispatcher.launch_calls[-1]
        assert call["adapter_name"] == "windows-terminal"
        assert call["cwd"] == Path("D:/projects/foo")
        # bypassPermissions → flag carried into command
        assert call["command"] == (
            "claude", "--resume", "u1", "--dangerously-skip-permissions",
        )
        # Plan-L: session_uuid forwarded so WT adapter can stamp the
        # tab title at spawn time. Guards against accidental removal of
        # the `session_uuid=d.session_uuid` kwarg in RecentsDrawer.
        assert call["session_uuid"] == "u1"
        # Intent registered with terminal pid from spawn result
        snap = registry.snapshot()
        assert len(snap) == 1
        assert snap[0].session_uuid == "u1"
        assert snap[0].terminal_pid == 4242
        # snapshotter.wake was triggered for immediate ⏳ render
        assert wakes

    def test_no_launcher_available_yields_toast_no_intent(self, qtbot):
        from PySide6.QtWidgets import QWidget
        parent = QWidget()
        qtbot.addWidget(parent)
        dispatcher = _FakeDispatcher(adapters=())  # no LAUNCH-capable adapter
        registry = LaunchIntentRegistry()
        d = RecentsDrawer(
            expanded=parent, dispatcher=dispatcher,
            launch_intent=registry, on_wake=lambda: None,
        )
        qtbot.addWidget(d)
        d.render(_empty_snap(dormant=[_dormant("u1")]))
        rows = [w for w in d._list_container.children()
                if isinstance(w, _RecentRow)]
        rows[0]._on_resume()
        assert dispatcher.launch_calls == []
        assert registry.snapshot() == ()
        # Toast text is set + the setVisible(True) was called.
        # isVisible() depends on parent being shown, which we don't do
        # in tests — isHidden() reflects the explicit hide() state alone.
        assert not d._toast.isHidden()
        assert "Windows Terminal" in d._toast.text()

    def test_spawn_error_yields_toast_no_intent(self, qtbot):
        from PySide6.QtWidgets import QWidget
        parent = QWidget()
        qtbot.addWidget(parent)
        dispatcher = _FakeDispatcher(
            spawn_error=LauncherSpawnError("wt.exe not found"),
        )
        registry = LaunchIntentRegistry()
        d = RecentsDrawer(
            expanded=parent, dispatcher=dispatcher,
            launch_intent=registry, on_wake=lambda: None,
        )
        qtbot.addWidget(d)
        d.render(_empty_snap(dormant=[_dormant("u1")]))
        rows = [w for w in d._list_container.children()
                if isinstance(w, _RecentRow)]
        rows[0]._on_resume()
        # launch was attempted but raised — registry stays empty
        assert dispatcher.launch_calls
        assert registry.snapshot() == ()
        assert not d._toast.isHidden()
        assert "wt.exe not found" in d._toast.text()


class TestToggle:
    def test_toggle_flips_visibility(self, drawer):
        d, *_ = drawer
        # isHidden() reflects the explicit hide() state; the bare drawer
        # starts hidden because the constructor doesn't call show().
        assert d.isHidden()
        d.toggle()
        assert not d.isHidden()
        d.toggle()
        assert d.isHidden()


# ── Search filtering ────────────────────────────────────────────────────

class TestSearchFiltering:
    """Verify search re-renders from cached snap without snapshotter.wake()."""

    def test_search_filters_dormant_rows(self, drawer):
        """T1: typing a search term filters the list immediately."""
        d, *_ = drawer
        d.render(_empty_snap(dormant=[
            _dormant("u1", name="refactor auth"),
            _dormant("u2", name="fix bug"),
        ]))
        # Both rows visible before search
        rows = [w for w in d._list_container.children()
                if isinstance(w, _RecentRow)]
        assert len(rows) == 2

        # Simulate search input
        d._on_search_changed("refactor")
        rows = [w for w in d._list_container.children()
                if isinstance(w, _RecentRow)]
        assert len(rows) == 1

    def test_search_no_match_shows_empty(self, drawer):
        """T2: search with no matches shows empty list."""
        d, *_ = drawer
        d.render(_empty_snap(dormant=[_dormant("u1", name="refactor")]))
        d._on_search_changed("zzz-nothing")
        rows = [w for w in d._list_container.children()
                if isinstance(w, _RecentRow)]
        assert len(rows) == 0

    def test_search_before_first_render_no_crash(self, drawer):
        """T3: _last_snap is None (render never called); search is no-op."""
        d, *_ = drawer
        assert d._last_snap is None
        d._on_search_changed("foo")  # must not crash

    def test_search_cleared_restores_all(self, drawer):
        """T4: clearing search restores full list."""
        d, *_ = drawer
        d.render(_empty_snap(dormant=[
            _dormant("u1", name="refactor"),
            _dormant("u2", name="bug-fix"),
        ]))
        d._on_search_changed("refactor")
        rows = [w for w in d._list_container.children()
                if isinstance(w, _RecentRow)]
        assert len(rows) == 1

        d._on_search_changed("")
        rows = [w for w in d._list_container.children()
                if isinstance(w, _RecentRow)]
        assert len(rows) == 2

    def test_search_by_uuid_prefix(self, drawer):
        """T5: search matching uuid prefix finds the session."""
        d, *_ = drawer
        d.render(_empty_snap(dormant=[_dormant("abc12345-uuid")]))
        d._on_search_changed("abc12345")
        rows = [w for w in d._list_container.children()
                if isinstance(w, _RecentRow)]
        assert len(rows) == 1

    def test_search_does_not_call_wake(self, drawer):
        """T6: search does not trigger snapshotter.wake()."""
        d, *_, wakes = drawer
        d.render(_empty_snap(dormant=[_dormant("u1")]))
        wakes.clear()

        d._on_search_changed("u1")
        assert len(wakes) == 0

    def test_dedup_still_works_for_unchanged_snap(self, drawer):
        """T7: distinct_until_changed still skips render on duplicate data."""
        from claude_island.ui.recents_drawer import RecentsDrawer as HD
        snap = _empty_snap(dormant=[_dormant("u1")])
        key1 = HD.compute(snap)
        key2 = HD.compute(snap)
        assert key1 == key2  # same data → same key → dedup skips

    def test_render_updates_cache_for_next_search(self, drawer):
        """T8: after a new render, search uses the latest snap data."""
        d, *_ = drawer
        # First render: 1 session
        d.render(_empty_snap(dormant=[_dormant("u1", name="alpha")]))
        d._on_search_changed("alpha")
        rows = [w for w in d._list_container.children()
                if isinstance(w, _RecentRow)]
        assert len(rows) == 1

        # Second render: 2 sessions — cache should update
        d.render(_empty_snap(dormant=[
            _dormant("u1", name="alpha"),
            _dormant("u2", name="beta"),
        ]))
        d._on_search_changed("beta")
        rows = [w for w in d._list_container.children()
                if isinstance(w, _RecentRow)]
        assert len(rows) == 1


# ── Selection state machine (Detail Design D3.1) ────────────────────────

from PySide6.QtCore import QEvent, Qt
from PySide6.QtGui import QKeyEvent


class TestSelectionState:
    """The selection state machine in the redesigned drawer:
    - On render with non-empty list, first row auto-selected.
    - Search that filters out current selection → fall back to first.
    - Empty filter result → _selected_uuid = None.
    """

    def test_S1_first_row_selected_after_render(self, drawer):
        d, *_ = drawer
        d.render(_empty_snap(dormant=[
            _dormant("u1", name="alpha"),
            _dormant("u2", name="bravo"),
        ]))
        d._reconcile_selection()
        assert d._selected_uuid == "u1"

    def test_S2_search_keeps_selection_when_still_visible(self, drawer):
        d, *_ = drawer
        d.render(_empty_snap(dormant=[
            _dormant("u1", name="refactor auth"),
            _dormant("u2", name="bug fix"),
        ]))
        d._reconcile_selection()
        d._select_uuid("u2")
        assert d._selected_uuid == "u2"
        # Search "bug" — u2 still matches → selection preserved
        d._on_search_changed("bug")
        assert d._selected_uuid == "u2"

    def test_S3_search_filters_out_selection_falls_back_to_first(self, drawer):
        d, *_ = drawer
        d.render(_empty_snap(dormant=[
            _dormant("u1", name="alpha"),
            _dormant("u2", name="beta"),
        ]))
        d._select_uuid("u2")
        # Search excludes u2 → reconcile picks first remaining row
        d._on_search_changed("alpha")
        assert d._selected_uuid == "u1"

    def test_S4_search_empty_clears_selection(self, drawer):
        d, *_ = drawer
        d.render(_empty_snap(dormant=[_dormant("u1", name="alpha")]))
        d._select_uuid("u1")
        d._on_search_changed("zzz-nomatch")
        assert d._selected_uuid is None

    def test_S5_close_reopen_resets_to_first(self, drawer):
        d, *_ = drawer
        d.render(_empty_snap(dormant=[
            _dormant("u1", name="alpha"),
            _dormant("u2", name="beta"),
        ]))
        d._select_uuid("u2")
        # Note: redesigned behaviour does NOT clear _selected_uuid on
        # close/reopen — once filtered the same uuid stays unless data
        # changes. Verify the documented behaviour matches.
        d.toggle()  # show
        d.toggle()  # hide
        # _selected_uuid stays — this matches D3.1 ("HasSelection" stays
        # until reconcile decides otherwise).
        assert d._selected_uuid == "u2"


# ── Keyboard / focus (D3.2) ─────────────────────────────────────────────


class TestKeyboardNavigation:
    """↑/↓ Enter Esc Tab handled in the search line edit's eventFilter.
    Focus stays on search; arrow keys override QLineEdit cursor moves
    to drive selection (Spotlight pattern)."""

    def _key(self, drawer, key, modifiers=Qt.KeyboardModifier.NoModifier):
        ev = QKeyEvent(QEvent.Type.KeyPress, key, modifiers)
        return drawer.eventFilter(drawer._search, ev)

    def test_K1_down_in_search_advances_selection(self, drawer):
        d, *_ = drawer
        d.render(_empty_snap(dormant=[
            _dormant("u1", name="alpha"),
            _dormant("u2", name="beta"),
        ]))
        d._select_uuid("u1")
        consumed = self._key(d, Qt.Key.Key_Down)
        assert consumed is True
        assert d._selected_uuid == "u2"

    def test_K2_up_at_top_clamps(self, drawer):
        d, *_ = drawer
        d.render(_empty_snap(dormant=[
            _dormant("u1"), _dormant("u2"),
        ]))
        d._select_uuid("u1")
        self._key(d, Qt.Key.Key_Up)
        assert d._selected_uuid == "u1"

    def test_K3_enter_triggers_resume_on_selected(self, drawer):
        d, dispatcher, *_ = drawer
        d.render(_empty_snap(dormant=[_dormant("u1")]))
        d._select_uuid("u1")
        self._key(d, Qt.Key.Key_Return)
        assert dispatcher.launch_calls
        assert dispatcher.launch_calls[-1]["command"][2] == "u1"

    def test_K4_esc_hides_drawer(self, drawer):
        d, *_ = drawer
        d.render(_empty_snap(dormant=[_dormant("u1")]))
        d.toggle()
        assert not d.isHidden()
        self._key(d, Qt.Key.Key_Escape)
        assert d.isHidden()

    def test_K5_tab_toggles_preview(self, drawer):
        d, *_ = drawer
        assert d._preview_visible is True
        self._key(d, Qt.Key.Key_Tab)
        assert d._preview_visible is False
        self._key(d, Qt.Key.Key_Tab)
        assert d._preview_visible is True

    def test_K6_down_with_no_selection_picks_first(self, drawer):
        d, *_ = drawer
        d.render(_empty_snap(dormant=[
            _dormant("u1"), _dormant("u2"),
        ]))
        d._selected_uuid = None  # force "no selection"
        for u in list(d._row_widgets):
            d._row_widgets[u].set_selected(False)
        self._key(d, Qt.Key.Key_Down)
        assert d._selected_uuid == "u1"

    def test_K7_toggle_activates_window_then_focuses_search(self, drawer):
        """Spotlight contract: search box owns focus the moment the
        drawer becomes visible — the only way the eventFilter
        intercepts ↑/↓ and turns them into row navigation.

        On Windows ``Qt.Tool`` adds ``WS_EX_TOOLWINDOW`` which by
        design does NOT activate on show — without an explicit
        ``activateWindow()`` the ``setFocus()`` call only sets a
        logical marker; arrow keys leak to whichever window IS
        active (panel behind, or the underlying browser → page
        scroll). offscreen Qt's ``hasFocus()`` is unreliable
        (windows never truly activate), so we assert the *call
        order*: ``activateWindow`` runs BEFORE ``setFocus`` on
        every show path."""
        d, *_ = drawer
        d.render(_empty_snap(dormant=[_dormant("u1")]))
        calls: list[str] = []
        # Wrap the methods to record the call ordering on toggle().
        # Using lambdas (not mock.patch) keeps the original behaviour
        # intact so the rest of toggle() still executes correctly.
        orig_activate = d.activateWindow
        orig_focus = d._search.setFocus
        d.activateWindow = lambda: (calls.append("activate"), orig_activate())[1]  # type: ignore[method-assign]
        d._search.setFocus = lambda: (calls.append("focus"), orig_focus())[1]      # type: ignore[method-assign]
        try:
            d.toggle()  # show
            assert calls == ["activate", "focus"], (
                f"expected activateWindow → setFocus, got {calls}"
            )
        finally:
            d.activateWindow = orig_activate         # type: ignore[method-assign]
            d._search.setFocus = orig_focus          # type: ignore[method-assign]
            if not d.isHidden():
                d.toggle()

    def test_K8_scrollareas_cannot_steal_focus(self, drawer):
        """QScrollArea defaults to ``Qt.WheelFocus``: a wheel-scroll
        moves focus into the scroll area, which then handles ↑/↓ as
        a built-in line-scroll instead of letting the search box's
        eventFilter turn them into row navigation. Both scroll areas
        must be ``NoFocus`` so the Spotlight focus contract holds
        even after the user mouse-wheels the list or preview."""
        d, *_ = drawer
        from PySide6.QtCore import Qt as _Qt
        assert d._list_scroll.focusPolicy() == _Qt.FocusPolicy.NoFocus
        assert d._preview_scroll.focusPolicy() == _Qt.FocusPolicy.NoFocus


# ── Preview toggle (D3.3) ───────────────────────────────────────────────


class TestPreviewToggle:
    def test_P1_tab_hides_preview_and_shrinks_width(self, drawer):
        d, *_ = drawer
        assert d.width() == 420
        d._toggle_preview()
        assert d.width() == 220
        # isHidden() reflects explicit setVisible(False) — independent
        # of whether the parent drawer is shown on screen.
        assert d._preview_scroll.isHidden() is True
        assert d._divider.isHidden() is True

    def test_P2_tab_again_restores_preview(self, drawer):
        d, *_ = drawer
        d._toggle_preview()  # hide
        d._toggle_preview()  # show
        assert d.width() == 420
        assert d._preview_scroll.isHidden() is False
        assert d._divider.isHidden() is False

    def test_P3_state_flag_flips(self, drawer):
        d, *_ = drawer
        assert d._preview_visible is True
        d._toggle_preview()
        assert d._preview_visible is False


# ── Prompt collapsible (D3.4) ───────────────────────────────────────────


class TestPromptCollapse:
    def _long_prompt(self, n: int = 250) -> str:
        # Simple synthetic prompt > 200 chars so the [展开] toggle appears.
        return "lorem ipsum " * 30  # 360 chars

    def test_C1_long_prompt_collapses_by_default(self, drawer):
        d, *_ = drawer
        long_p = self._long_prompt()
        d.render(_empty_snap(dormant=[_dormant("u1", last_prompt=long_p)]))
        d._select_uuid("u1")
        assert d._prompt_expanded is False
        # Find the collapsible link button in preview.
        from claude_island.ui.collapsible import CollapsibleLinkButton
        toggles = d._preview_container.findChildren(CollapsibleLinkButton)
        assert any(t.text() == "[展开]" for t in toggles)

    def test_C2_clicking_expand_shows_full_text(self, drawer):
        d, *_ = drawer
        long_p = self._long_prompt()
        d.render(_empty_snap(dormant=[_dormant("u1", last_prompt=long_p)]))
        d._select_uuid("u1")
        # The LAST PROMPT section is now a shared LastPromptSection
        # widget. Toggle via its own _on_toggle (which emits the
        # signal the drawer handles). Drawer-level _prompt_expanded
        # is updated via the signal so re-renders preserve state.
        from claude_island.ui.last_prompt_section import LastPromptSection
        sections = d._preview_container.findChildren(LastPromptSection)
        assert len(sections) == 1
        sections[0]._on_toggle()
        assert sections[0].is_expanded() is True
        assert d._prompt_expanded is True

    def test_C3_changing_selection_resets_expanded(self, drawer):
        d, *_ = drawer
        long_p = self._long_prompt()
        d.render(_empty_snap(dormant=[
            _dormant("u1", last_prompt=long_p),
            _dormant("u2", last_prompt=long_p),
        ]))
        d._select_uuid("u1")
        from claude_island.ui.last_prompt_section import LastPromptSection
        d._preview_container.findChildren(LastPromptSection)[0]._on_toggle()
        assert d._prompt_expanded is True
        d._select_uuid("u2")
        assert d._prompt_expanded is False  # reset on selection change

    def test_C4_short_prompt_no_toggle(self, drawer):
        d, *_ = drawer
        d.render(_empty_snap(dormant=[
            _dormant("u1", last_prompt="short"),
        ]))
        d._select_uuid("u1")
        # LastPromptSection's toggle is hidden when the content fits.
        from claude_island.ui.last_prompt_section import LastPromptSection
        sections = d._preview_container.findChildren(LastPromptSection)
        assert len(sections) == 1
        assert not sections[0]._toggle.isVisible()


# ── Slimmed preview action row (Open + Copy buttons removed) ───────────

class TestPreviewActionRow:
    """The Open / Copy buttons used to live next to Resume in the
    action row. They've been replaced by hover-reveal affordances on
    the cwd row (↗) and the uuid row (⧉) — same pattern as
    SessionDetailPopup. Resume is now the only button in the action
    row, full width."""

    def _drawer(self, qtbot):
        # Reuse the same dispatcher fake the rest of this file uses;
        # TerminalDispatcher needs real backend capability sets which
        # plain mock.Mock() doesn't supply.
        from PySide6.QtWidgets import QWidget
        parent = QWidget()
        qtbot.addWidget(parent)
        d = RecentsDrawer(
            expanded=parent,
            dispatcher=_FakeDispatcher(),
            launch_intent=LaunchIntentRegistry(),
            on_wake=lambda: None,
        )
        qtbot.addWidget(d)
        return d

    def test_no_open_button_in_preview(self, qtbot):
        d = self._drawer(qtbot)
        d.render(_empty_snap(dormant=[_dormant("u1")]))
        d._select_uuid("u1")
        from PySide6.QtWidgets import QPushButton
        # Old code put "📂 Open" in the action row. New code uses the
        # ↗ glyph on the cwd row; tooltip text is the affordance.
        btns_with_open_text = [
            b for b in d._preview_container.findChildren(QPushButton)
            if "📂 Open" in b.text()
        ]
        assert btns_with_open_text == []

    def test_no_clipboard_emoji_button_in_preview(self, qtbot):
        d = self._drawer(qtbot)
        d.render(_empty_snap(dormant=[_dormant("u1")]))
        d._select_uuid("u1")
        from PySide6.QtWidgets import QPushButton
        # Old code had a 📋 button next to the uuid; new code uses ⧉
        # via the hover-reveal pattern instead. Pin the removal so a
        # future revert doesn't sneak the truncated 📋 affordance back.
        btns_with_clipboard = [
            b for b in d._preview_container.findChildren(QPushButton)
            if b.text() == "📋"
        ]
        assert btns_with_clipboard == []

    def test_resume_button_in_preview_header(self, qtbot):
        """v3.1 redesign: Resume button moved INTO the preview header
        (left of title) so its visual binding to the selected session
        is unambiguous. The pre-v3.1 sticky-footer button sat in the
        drawer body and looked like a banner action affecting the
        whole drawer."""
        from PySide6.QtWidgets import QPushButton
        d = self._drawer(qtbot)
        d.render(_empty_snap(dormant=[_dormant("u1")]))
        d._select_uuid("u1")
        in_preview = d._preview_container.findChild(
            QPushButton, "preview_resume_btn",
        )
        assert in_preview is not None
        assert "Resume" in in_preview.text()

    def test_cwd_row_uses_hover_reveal(self, qtbot):
        """The cwd row should be a _HoverRevealRow so the ↗ open glyph
        only shows on hover — matches SessionDetailPopup's Path row."""
        d = self._drawer(qtbot)
        d.render(_empty_snap(dormant=[_dormant("u1")]))
        d._select_uuid("u1")
        from claude_island.ui.expanded_window import _HoverRevealRow
        rows = d._preview_container.findChildren(_HoverRevealRow)
        # Two hover rows expected: uuid row + cwd row.
        assert len(rows) == 2


# ── Redesign R1-R5: header / meta order / sticky footer / title ────────


class TestPreviewRedesign:
    """Pin the post-redesign UI contract:

    - **Header** combines title + count into one label; standalone
      "Esc" hint label is gone (× close button replaces it).
    - **Meta block order**: UUID is the FIRST row (primary identifier
      for ``claude --resume``), followed by path / branch / cost.
    - **Sticky Resume**: button lives outside the preview ScrollArea
      so expanding LAST PROMPT never pushes it down. Disabled state
      reserved for "no selection" so the layout is stable.
    - **Title without [展开]**: the title-level CollapsibleLinkButton
      was removed; long titles wrap freely up to a 200-char hard cap,
      then tail-elide with full text in tooltip.

    Each test pins one of these so a future refactor can't quietly
    revert the visual cleanup the user signed off on.
    """

    def _drawer(self, qtbot):
        from PySide6.QtWidgets import QWidget
        parent = QWidget()
        qtbot.addWidget(parent)
        d = RecentsDrawer(
            expanded=parent,
            dispatcher=_FakeDispatcher(),
            launch_intent=LaunchIntentRegistry(),
            on_wake=lambda: None,
        )
        qtbot.addWidget(d)
        return d

    def test_R1_header_label_carries_count_inline(self, qtbot):
        """Header text starts with 'RECENTS' AND ends with the count
        — no separate _count_label, no addStretch gap between them."""
        d = self._drawer(qtbot)
        d.render(_empty_snap(dormant=[_dormant("u1"), _dormant("u2")]))
        text = d._header_label.text()
        assert text.startswith("RECENTS")
        assert "2" in text
        # No bare 'Esc' text label anywhere in the header (replaced
        # by an actual × close button, see R2).
        from PySide6.QtWidgets import QLabel
        labels = [
            lbl.text() for lbl in d.findChildren(QLabel)
            if lbl.text() == "Esc"
        ]
        assert labels == []

    def test_R2_close_button_hides_drawer(self, qtbot):
        """The × close button replaces the decorative 'Esc' label —
        clicking it must actually close the drawer (Esc still works
        too via QShortcut, but the affordance is now real)."""
        from PySide6.QtWidgets import QPushButton
        d = self._drawer(qtbot)
        d.render(_empty_snap())
        d.toggle()  # show
        assert not d.isHidden()
        # Find the close button by its glyph text.
        close_btns = [
            b for b in d.findChildren(QPushButton) if b.text() == "×"
        ]
        assert len(close_btns) == 1
        close_btns[0].click()
        assert d.isHidden()

    def test_R3_uuid_is_first_meta_row(self, qtbot):
        """UUID must precede the cwd row in the meta block — it's the
        canonical identifier for ``claude --resume`` and the user's
        primary recognition cue when scrolling the recents list."""
        from claude_island.ui.expanded_window import _HoverRevealRow
        d = self._drawer(qtbot)
        d.render(_empty_snap(dormant=[
            _dormant("aaaa-bbbb-cccc-uuid", cwd=Path("D:/projects/foo")),
        ]))
        d._select_uuid("aaaa-bbbb-cccc-uuid")
        # _HoverRevealRow children appear in layout order (Qt's
        # findChildren traverses depth-first in insertion order).
        hover_rows = d._preview_container.findChildren(_HoverRevealRow)
        assert len(hover_rows) == 2
        # First hover row should contain the UUID; second should
        # contain the cwd path. Identify by inspecting child labels.
        from PySide6.QtWidgets import QLabel
        first_text = " ".join(
            lbl.text() for lbl in hover_rows[0].findChildren(QLabel)
        )
        second_text = " ".join(
            lbl.text() for lbl in hover_rows[1].findChildren(QLabel)
        )
        # v3.1 inserts U+200B after "-" in the UUID and after "/" in
        # the cwd so wordWrap has break points. Strip them before
        # substring matching so the test stays focused on row order
        # rather than rendering details.
        zwsp = "​"
        assert "aaaa-bbbb-cccc-uuid" in first_text.replace(zwsp, ""), (
            f"UUID row must be first — got first={first_text!r}, "
            f"second={second_text!r}"
        )
        cleaned_second = second_text.replace(zwsp, "")
        assert "projects" in cleaned_second and "foo" in cleaned_second

    def test_R4_resume_absent_when_no_selection(self, qtbot):
        """v3.1: with no selection the preview shows a placeholder
        ("Select a session...") and no Resume button is rendered at
        all — the button is part of the dynamic preview header which
        only exists when a session is selected. After selecting a
        session the button appears in the header."""
        from PySide6.QtWidgets import QPushButton
        d = self._drawer(qtbot)
        d.render(_empty_snap())
        # Empty selection → placeholder, no resume button.
        assert d._preview_container.findChild(
            QPushButton, "preview_resume_btn",
        ) is None
        d.render(_empty_snap(dormant=[_dormant("u1")]))
        d._select_uuid("u1")
        # After selection the button materialises in the header.
        btn = d._preview_container.findChild(
            QPushButton, "preview_resume_btn",
        )
        assert btn is not None
        assert btn.isEnabled() is True

    def test_R5_resume_stays_at_top_when_prompt_expands(self, qtbot):
        """v3.1 promise: Resume sits in the preview HEADER (top of the
        column), so expanding LAST PROMPT (which lives below the meta
        block) cannot push it down. Expansion grows the bottom of the
        preview, not the top."""
        from PySide6.QtWidgets import QPushButton
        d = self._drawer(qtbot)
        d.show()
        long_p = "lorem ipsum " * 30
        d.render(_empty_snap(dormant=[_dormant("u1", last_prompt=long_p)]))
        d._select_uuid("u1")
        d.adjustSize()
        btn_before = d._preview_container.findChild(
            QPushButton, "preview_resume_btn",
        )
        assert btn_before is not None
        before = btn_before.mapTo(d, btn_before.rect().topLeft())
        from claude_island.ui.last_prompt_section import LastPromptSection
        sections = d._preview_container.findChildren(LastPromptSection)
        assert sections
        sections[0]._on_toggle()
        d.adjustSize()
        btn_after = d._preview_container.findChild(
            QPushButton, "preview_resume_btn",
        )
        assert btn_after is not None
        after = btn_after.mapTo(d, btn_after.rect().topLeft())
        assert before == after, (
            f"Resume button moved on prompt expand: {before} -> {after}"
        )

    def test_R6_title_has_no_expand_toggle(self, qtbot):
        """Title-level [展开] button was removed — long titles use
        wordwrap + tooltip instead. Pin so a future revert doesn't
        bring back the noisy in-title toggle."""
        d = self._drawer(qtbot)
        long_title = "x" * 250  # past the hard cap
        d.render(_empty_snap(dormant=[_dormant("u1", name=long_title)]))
        d._select_uuid("u1")
        # The title-level CollapsibleLinkButton should NOT exist.
        # The LAST PROMPT section's toggle is fine — but here we
        # didn't pass a prompt, so any CollapsibleLinkButton found
        # would be the title's (which we just deleted).
        from claude_island.ui.collapsible import CollapsibleLinkButton
        toggles = d._preview_container.findChildren(CollapsibleLinkButton)
        assert toggles == [], (
            "Title-level [展开] button must not exist — title now "
            "uses wordwrap + tooltip for long text."
        )

    def test_R8_last_prompt_toggle_has_min_width(self, qtbot):
        """LAST PROMPT [展开] / [收起] toggle was getting clipped to
        '[展' in narrow preview columns because CJK QFontMetrics
        under-sized the QPushButton — fix is a hard min-width
        reservation. Pin the floor so a refactor doesn't drop it."""
        from claude_island.ui.collapsible import CollapsibleLinkButton
        d = self._drawer(qtbot)
        long_p = "lorem ipsum " * 30
        d.render(_empty_snap(dormant=[_dormant("u1", last_prompt=long_p)]))
        d._select_uuid("u1")
        toggles = d._preview_container.findChildren(CollapsibleLinkButton)
        assert len(toggles) == 1
        # 56 px is the floor — covers both [展开] and [收起] at 11 px
        # font without overflow.
        assert toggles[0].minimumWidth() >= 56

    def test_v3_row_title_falls_back_to_cwd_basename(self, qtbot):
        """When name and last_prompt are both absent (typical for a
        launched-but-not-yet-typed-into session) the row title should
        fall back to the cwd basename instead of the literal
        "Untitled session". Without this the recents list shows N rows
        of "Untitled session" with no way to tell them apart, which is
        exactly the bug the user reported in screenshot #7."""
        from claude_island.ui.recents_drawer import _row_title
        from dataclasses import replace
        d = _dormant("u1", name=None, cwd=Path("/Users/me/myproj"))
        # _dormant may default last_prompt to something non-empty;
        # force the worst-case "no name AND no prompt" combo.
        d = replace(d, name=None, last_prompt=None)
        assert _row_title(d) == "myproj"

    def test_v3_uuid_label_has_zero_width_space_break_hints(self, qtbot):
        """v3.1 fix for "UUID overflows / hard-clips in narrow preview":
        QLabel.wordWrap only breaks at whitespace, but a UUID has none.
        Insert U+200B after each "-" so wordWrap has legal break points
        and the visible string is unchanged. Pin so a refactor that
        strips the breaks doesn't silently regress wrapping."""
        from PySide6.QtWidgets import QLabel
        d = self._drawer(qtbot)
        d.render(_empty_snap(dormant=[
            _dormant("aaaa-bbbb-cccc-dddd-eeeeeeee"),
        ]))
        d._select_uuid("aaaa-bbbb-cccc-dddd-eeeeeeee")
        labels = [
            lbl for lbl in d._preview_container.findChildren(QLabel)
            if "aaaa" in lbl.text()
        ]
        assert labels
        # Visible text contains U+200B after every "-"
        text = labels[0].text()
        assert "​" in text
        # And word wrap is on so Qt can act on those break points.
        assert labels[0].wordWrap() is True

    def test_v3_branch_label_has_break_hints(self, qtbot):
        """Branch names like ``feat/capsule-three-region-layout`` need
        break hints at "/" and "-" so they wrap rather than overflow.
        Same U+200B mechanism as UUID."""
        from PySide6.QtWidgets import QLabel
        d = self._drawer(qtbot)
        d.render(_empty_snap(dormant=[
            _dormant("u1", git_branch="feat/capsule-three-region-layout"),
        ]))
        d._select_uuid("u1")
        labels = [
            lbl for lbl in d._preview_container.findChildren(QLabel)
            if "feat" in lbl.text() and "layout" in lbl.text()
        ]
        assert labels
        text = labels[0].text()
        # Break hints inserted at both "/" and "-" boundaries.
        assert "/​" in text
        assert "-​" in text
        assert labels[0].wordWrap() is True

    def test_R7_long_title_full_text_in_tooltip(self, qtbot):
        """When a title is tail-elided past the hard cap, the full
        text must remain accessible via tooltip — this is the user's
        only way to see the elided tail."""
        from PySide6.QtWidgets import QLabel
        d = self._drawer(qtbot)
        long_title = "abc" * 100  # 300 chars > _TITLE_HARD_CAP
        d.render(_empty_snap(dormant=[_dormant("u1", name=long_title)]))
        d._select_uuid("u1")
        # The title label is the first QLabel with PREVIEW_TITLE
        # styling — find it by its long-text fingerprint.
        title_labels = [
            lbl for lbl in d._preview_container.findChildren(QLabel)
            if lbl.text().startswith("abc") and len(lbl.text()) > 50
        ]
        assert len(title_labels) == 1
        # Display text is elided…
        assert title_labels[0].text().endswith("…")
        # …but tooltip carries the full string verbatim.
        assert title_labels[0].toolTip() == long_title


# ── Rename compatibility (R1-R3) ────────────────────────────────────────


class TestRenameImports:
    def test_R1_recents_drawer_importable(self):
        from claude_island.ui.recents_drawer import RecentsDrawer  # noqa
        assert RecentsDrawer is not None

    def test_R2_old_history_drawer_path_gone(self):
        import importlib
        try:
            importlib.import_module("claude_island.ui.history_drawer")
        except ModuleNotFoundError:
            return
        raise AssertionError(
            "old history_drawer module path still exists — should be removed"
        )

    def test_R3_recents_filter_importable(self):
        from claude_island.ui.recents_filter import (
            sort_by_recency, filter_by_query, search_haystack,
        )
        assert sort_by_recency is not None
        assert filter_by_query is not None
        assert search_haystack is not None
