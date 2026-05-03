"""Tests for ExpandedWindow's diff-based row update (B6).

Uses pytest-qt's qtbot to create a real QApplication. Verifies that:
- repeated refresh with the same pids reuses widgets (no flicker)
- removed pids destroy their widget; new pids insert a fresh one
- text updates happen in-place without recreating the widget
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

# Force offscreen for headless CI / local runs.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QWidget

from claude_island.core.models import Session, UsageTotals
from claude_island.ui.controller import IslandController
from claude_island.ui.expanded_window import ExpandedWindow


def _session(
    pid: int, cwd: str, ago_minutes: int = 0,
    window_handle: int | None = None,
) -> Session:
    return Session(
        pid=pid,
        project_path=Path(cwd),
        session_uuid="",
        window_handle=window_handle,
        last_activity=datetime.now(timezone.utc) - timedelta(minutes=ago_minutes),
    )


@pytest.fixture
def panel(qtbot):
    capsule = QWidget()  # stand-in capsule for positioning
    capsule.show()
    controller = IslandController()
    panel = ExpandedWindow(
        capsule=capsule,
        controller=controller,
        get_usage_totals=lambda period: UsageTotals(period=period),
    )
    qtbot.addWidget(panel)
    qtbot.addWidget(capsule)
    yield panel


def _panel_with_session(qtbot, get_session_usage):
    """Build a panel wired with a stub get_session_usage."""
    from claude_island.ui.expanded_window import ExpandedWindow
    capsule = QWidget()
    capsule.show()
    controller = IslandController()
    p = ExpandedWindow(
        capsule=capsule,
        controller=controller,
        get_usage_totals=lambda period: UsageTotals(period=period),
        get_session_usage=get_session_usage,
    )
    qtbot.addWidget(p)
    qtbot.addWidget(capsule)
    return p


def test_first_refresh_creates_one_row_per_session(panel):
    panel.refresh_sessions([_session(1, "/a"), _session(2, "/b")])
    assert set(panel._rows.keys()) == {1, 2}
    assert panel._placeholder is None


def test_repeated_refresh_with_same_pids_reuses_widgets(panel):
    """The core B6 invariant: same pid set must NOT recreate widgets,
    otherwise hover state and any user interaction is lost on every tick."""
    panel.refresh_sessions([_session(1, "/a"), _session(2, "/b")])
    btn1_before = panel._rows[1]
    btn2_before = panel._rows[2]

    panel.refresh_sessions([_session(1, "/a"), _session(2, "/b")])
    assert panel._rows[1] is btn1_before  # same widget instance
    assert panel._rows[2] is btn2_before


def test_removed_pid_drops_its_widget(panel):
    panel.refresh_sessions([_session(1, "/a"), _session(2, "/b")])
    panel.refresh_sessions([_session(1, "/a")])  # 2 gone

    assert set(panel._rows.keys()) == {1}


def test_added_pid_inserts_new_widget(panel):
    panel.refresh_sessions([_session(1, "/a")])
    btn1 = panel._rows[1]

    panel.refresh_sessions([_session(1, "/a"), _session(2, "/b")])

    assert set(panel._rows.keys()) == {1, 2}
    assert panel._rows[1] is btn1  # 1's widget preserved


def test_existing_row_meta_updates_in_place(panel):
    """The right-side meta slot used to show age; it now shows cost.
    The label is still updated in-place across refreshes (no widget
    rebuild)."""
    from PySide6.QtWidgets import QLabel
    panel.refresh_sessions([_session(1, "/a")])
    btn = panel._rows[1]
    label = btn.findChild(QLabel, "meta_label")
    assert label is not None
    # Without a get_session_details composer the meta reads "—".
    assert label.text() == "—"
    # The widget itself isn't recreated on the next refresh.
    panel.refresh_sessions([_session(1, "/a")])
    assert btn.findChild(QLabel, "meta_label") is label


def test_empty_sessions_shows_placeholder(panel):
    panel.refresh_sessions([])
    assert panel._placeholder is not None
    assert panel._rows == {}


def test_placeholder_disappears_when_sessions_arrive(panel):
    panel.refresh_sessions([])
    assert panel._placeholder is not None

    panel.refresh_sessions([_session(1, "/a")])
    assert panel._placeholder is None
    assert set(panel._rows.keys()) == {1}


def test_session_click_emits_latest_session_snapshot(panel, qtbot):
    """Property carrier (_session) on the button must be refreshed on each
    update so a click after activity changed emits the new last_activity."""
    panel.refresh_sessions([_session(1, "/a", ago_minutes=10)])
    btn = panel._rows[1]
    fresh = _session(1, "/a", ago_minutes=0)
    panel.refresh_sessions([fresh])

    received: list = []
    panel.session_activated.connect(lambda s, sibs: received.append((s, sibs)))
    btn.click()

    assert len(received) == 1
    session, siblings = received[0]
    assert session.last_activity == fresh.last_activity
    assert siblings == []  # singleton group → no siblings


# --------------------------------------------------------------------------
# Same-tab grouping (PR2)
# --------------------------------------------------------------------------

def _top_level_widgets(panel) -> list:
    """Return the widgets actually placed at the session_box top level
    (cards or standalone buttons), in layout order."""
    box = panel._session_box
    return [box.itemAt(i).widget() for i in range(box.count())]


def test_two_sessions_same_window_handle_and_path_share_one_card(panel):
    """G-UI-1: split-pane proxy. Two sessions with the same wt_hwnd and
    cwd are merged into a single rounded card containing both rows."""
    from PySide6.QtWidgets import QFrame, QPushButton

    panel.refresh_sessions([
        _session(1, "/proj", window_handle=0xAAAA),
        _session(2, "/proj", window_handle=0xAAAA),
    ])

    top = _top_level_widgets(panel)
    assert len(top) == 1, f"expected one merged card, got {len(top)} widgets"

    card = top[0]
    assert isinstance(card, QFrame)
    assert card.objectName() == "group_card"

    rows_in_card = card.findChildren(QPushButton)
    assert {r.property("_session").pid for r in rows_in_card} == {1, 2}


def test_different_window_handles_render_as_separate_widgets(panel):
    """G-UI-2: sessions in different WT windows must NOT be grouped,
    even if their cwd matches."""
    panel.refresh_sessions([
        _session(1, "/proj", window_handle=0xAAAA),
        _session(2, "/proj", window_handle=0xBBBB),
    ])

    top = _top_level_widgets(panel)
    assert len(top) == 2  # two standalone widgets, not one card


def test_same_window_handle_different_paths_render_as_separate_widgets(panel):
    """G-UI-3: same WT but different cwds → different (proxy-)tabs →
    two separate cards. The user's "agent-prompt" + "claude-island"
    in one WT shouldn't collapse into one card."""
    panel.refresh_sessions([
        _session(1, "/a", window_handle=0xAAAA),
        _session(2, "/b", window_handle=0xAAAA),
    ])

    top = _top_level_widgets(panel)
    assert len(top) == 2


def test_window_handle_none_renders_standalone(panel):
    """G-UI-4: window_handle=None means we couldn't resolve a host
    (pythonw, sandboxed shell, etc.). Such sessions are always
    standalone — never merged with any other session, even if cwd
    matches a grouped pair."""
    panel.refresh_sessions([
        _session(1, "/proj", window_handle=0xAAAA),
        _session(2, "/proj", window_handle=0xAAAA),
        _session(3, "/proj", window_handle=None),  # ungroupable
    ])

    top = _top_level_widgets(panel)
    # 1 card (pids 1+2) + 1 standalone (pid 3) = 2 top-level widgets
    assert len(top) == 2


def test_grouped_row_click_still_emits_session(panel, qtbot):
    """G-UI-5: clicking a row that lives inside a multi-session card
    must still emit session_activated with the right Session, and
    must include the OTHER group members as siblings (so the activator
    can fall back to their console titles for inactive-pane fix)."""
    panel.refresh_sessions([
        _session(1, "/proj", window_handle=0xAAAA),
        _session(2, "/proj", window_handle=0xAAAA),
    ])

    received: list = []
    panel.session_activated.connect(lambda s, sibs: received.append((s, sibs)))

    panel._rows[2].click()

    assert len(received) == 1
    session, siblings = received[0]
    assert session.pid == 2
    sibling_pids = {s.pid for s in siblings}
    assert sibling_pids == {1}  # the other pane in the same group


def test_worktree_groups_with_parent_repo(panel):
    """G-UI-7: a session whose cwd is a Claude Code worktree
    (``<repo>/.claude/worktrees/<branch>``) groups with another
    session whose cwd is the parent repo, when both share the same
    WT window. Common pattern: main repo in pane A, worktree in pane B,
    side-by-side in one tab."""
    panel.refresh_sessions([
        _session(1, "/repo", window_handle=0xAAAA),
        _session(2, "/repo/.claude/worktrees/feature-x", window_handle=0xAAAA),
    ])

    top = _top_level_widgets(panel)
    assert len(top) == 1, "worktree should merge into the parent's card"

    from PySide6.QtWidgets import QFrame, QPushButton
    card = top[0]
    assert isinstance(card, QFrame)
    rows_in_card = card.findChildren(QPushButton)
    assert {r.property("_session").pid for r in rows_in_card} == {1, 2}


def test_worktree_normalizer_does_not_overreach():
    """The normaliser must collapse ``.claude/worktrees/...`` only.
    A path that mentions ``.claude`` for a different reason (e.g. a
    file inside the user's home claude config) must pass through."""
    from claude_island.ui.expanded_window import _normalize_project_path

    # The actual case we want to collapse:
    assert _normalize_project_path(
        Path("D:/repo/.claude/worktrees/feat-x")
    ) == "D:\\repo" or _normalize_project_path(
        Path("D:/repo/.claude/worktrees/feat-x")
    ) == "D:/repo"

    # Regular project path: untouched.
    raw = "D:/coding/project-a"
    assert _normalize_project_path(Path(raw)) in (raw, raw.replace("/", "\\"))

    # ``.claude`` without ``worktrees`` next to it: untouched.
    p = Path("C:/Users/me/.claude/projects/some-file")
    out = _normalize_project_path(p)
    assert "projects" in out  # the .claude dir was kept


def test_two_worktrees_of_same_repo_group_together(panel):
    """G-UI-8: two different worktrees of the same repo should still
    merge — both normalise to the same parent repo path."""
    panel.refresh_sessions([
        _session(1, "/repo/.claude/worktrees/feat-a", window_handle=0xAAAA),
        _session(2, "/repo/.claude/worktrees/feat-b", window_handle=0xAAAA),
    ])

    top = _top_level_widgets(panel)
    assert len(top) == 1


def test_row_widget_preserved_when_moving_between_card_and_standalone(panel):
    """G-UI-6: pid 1 starts grouped (with pid 2), then pid 2 disappears.
    The pid-1 row widget should be the same instance (cached by pid)
    in both refreshes — only its parent / style changes."""
    panel.refresh_sessions([
        _session(1, "/proj", window_handle=0xAAAA),
        _session(2, "/proj", window_handle=0xAAAA),
    ])
    btn1 = panel._rows[1]

    panel.refresh_sessions([_session(1, "/proj", window_handle=0xAAAA)])

    assert panel._rows[1] is btn1  # same widget instance preserved


# --------------------------------------------------------------------------
# USAGE region: session card + period card (U1-U7)
# --------------------------------------------------------------------------

from claude_island.core.models import (
    ModelTotals as _ModelTotals,
    QuotaSnapshot as _QuotaSnapshot,
    SessionUsage as _SessionUsage,
)


def _make_session_usage(
    *,
    start_offset_h: float | None = 1.0,   # hours ago
    end_offset_h: float | None = 4.0,     # hours from now (positive=future)
    quota: _QuotaSnapshot | None = None,
    by_model: tuple[_ModelTotals, ...] = (),
    total_cost: float = 2.67,
):
    now = datetime.now(timezone.utc)
    start = (now - timedelta(hours=start_offset_h)) if start_offset_h is not None else None
    end = (now + timedelta(hours=end_offset_h)) if end_offset_h is not None else None
    return _SessionUsage(
        start_time=start,
        end_time=end,
        by_model=by_model,
        total_cost_usd=total_cost,
        quota=quota,
    )


def _panel_with_quota(qtbot, *, quota=None, totals=None, by_model=None):
    """Build a panel wired with the new SPEND + QUOTA APIs.

    ``quota``    — drives the QUOTA card (bars / pct / reset).
    ``totals``   — drives the SPEND card amount + I/O lines.
    ``by_model`` — drives the SPEND card per-model breakdown.

    Use this helper instead of ``_panel_with_session`` for any test
    that exercises the post-A2 layout. The legacy helper passes
    ``get_session_usage`` which the new cards no longer read."""
    capsule = QWidget()
    capsule.show()
    p = ExpandedWindow(
        capsule=capsule,
        controller=IslandController(),
        get_usage_totals=lambda period: (totals or UsageTotals(period=period)),
        get_totals_by_model=(lambda _period: by_model) if by_model is not None else None,
        get_quota_snapshot=(lambda: quota) if quota is not None else None,
    )
    qtbot.addWidget(p)
    qtbot.addWidget(capsule)
    return p


def _make_quota(*, five_pct: float = 53.0, is_stale: bool = False):
    now = datetime.now(timezone.utc)
    return _QuotaSnapshot(
        five_hour_pct=five_pct,
        five_hour_resets_at=now + timedelta(hours=3, minutes=25),
        seven_day_pct=17.0,
        seven_day_resets_at=now + timedelta(days=4),
        fetched_at=now if not is_stale else now - timedelta(hours=1),
        is_stale=is_stale,
    )


def test_quota_card_with_quota_shows_bar_and_pct(qtbot):
    """U1: quota snapshot present → 5h bar visible, value matches pct,
    % text rendered next to the bar."""
    p = _panel_with_quota(qtbot, quota=_make_quota(five_pct=53.0))
    p.refresh_usage_bar()

    assert p._quota_bar_5h.isVisibleTo(p._quota_card)
    assert p._quota_bar_5h.value() == 53
    assert "53%" in p._quota_pct_5h.text()


def test_quota_card_without_quota_hides_bars(qtbot):
    """U2: no quota snapshot → both bars hidden, pct empty."""
    p = _panel_with_quota(qtbot, quota=None,
                          totals=UsageTotals(period="today", input_tokens=10))
    p.refresh_usage_bar()

    assert not p._quota_bar_5h.isVisibleTo(p._quota_card)
    assert p._quota_pct_5h.text() == ""
    assert not p._quota_bar_week.isVisibleTo(p._quota_card)


def test_quota_card_stale_marks_warning(qtbot):
    """U3: quota.is_stale=True → bar still shown, ⚠ marker on pct text."""
    p = _panel_with_quota(qtbot, quota=_make_quota(five_pct=20.0, is_stale=True))
    p.refresh_usage_bar()
    assert "⚠" in p._quota_pct_5h.text()


def test_quota_card_no_quota_dot_gray(qtbot):
    """U4: no remote quota → live-dot greys out (no signal we can
    derive freshness from). Replaces the old "expired session" test
    that relied on SessionUsage.end_time."""
    p = _panel_with_quota(qtbot, quota=None,
                          totals=UsageTotals(period="today"))
    p.refresh_usage_bar()
    assert "#52525b" in p._quota_dot.styleSheet()


def test_spend_card_empty_totals_shows_zero(qtbot):
    """U5: empty totals → spend amount shows $0 (or fallback rendering),
    bars stay hidden because quota is None."""
    p = _panel_with_quota(qtbot, quota=None,
                          totals=UsageTotals(period="today"))
    p.refresh_usage_bar()
    # _fmt_money(0) returns "$0.001" or "$0.00" (sub-cent path); either way "$" is present
    assert "$" in p._spend_amount.text()
    assert not p._quota_bar_5h.isVisibleTo(p._quota_card)


def test_period_toggle_updates_spend_card(qtbot):
    """U6: switching period (Today → Weekly) calls get_usage_totals with
    the new key and the spend amount updates."""
    capsule = QWidget()
    capsule.show()

    calls: list[str] = []

    def fake_totals(period):
        calls.append(period)
        # Different cost per period so the amount label changes.
        return UsageTotals(
            period=period,
            input_tokens=1000 if period == "5h" else 9999,
            output_tokens=2000,
            input_cost=1.0 if period == "5h" else 9.99,
        )

    p = ExpandedWindow(
        capsule=capsule,
        controller=IslandController(),
        get_usage_totals=fake_totals,
    )
    qtbot.addWidget(p)
    qtbot.addWidget(capsule)
    p.refresh_usage_bar()

    # Default period is "5h" (most actionable window); first refresh
    # populates that. Switching to weekly should re-fetch + re-render.
    initial_amount = p._spend_amount.text()
    p._on_period("weekly")
    weekly_amount = p._spend_amount.text()

    assert initial_amount != weekly_amount
    assert "5h" in calls
    assert "weekly" in calls


def test_period_selector_includes_5h(qtbot):
    """A2 regression: the unified SPEND selector exposes "5H" alongside
    Today/Daily/Weekly/Monthly so 5h-spend isn't hidden in a separate
    card. Apple HIG max of 5 segments — exactly what we have."""
    p = _panel_with_quota(qtbot, totals=UsageTotals(period="today"))
    assert set(p._period_btns.keys()) == {"5h", "today", "daily", "weekly", "monthly"}


@pytest.mark.parametrize("pct,expected_color", [
    (0.0,   "#4ade80"),   # green tier — bottom of range
    (10.0,  "#4ade80"),
    (59.0,  "#4ade80"),   # still green just below threshold
    (60.0,  "#facc15"),   # yellow tier — at threshold
    (75.0,  "#facc15"),
    (84.0,  "#facc15"),   # still yellow just below threshold
    (85.0,  "#ef4444"),   # red tier — at threshold
    (99.0,  "#ef4444"),
    (100.0, "#ef4444"),
])
def test_quota_bar_color_thresholds(qtbot, pct, expected_color):
    """U8: bar chunk colour escalates green → yellow → red at 60% / 85%.
    The pct text is coloured to match so the signal reads either way."""
    p = _panel_with_quota(qtbot, quota=_make_quota(five_pct=pct))
    p.refresh_usage_bar()
    assert expected_color in p._quota_bar_5h.styleSheet()
    assert expected_color in p._quota_pct_5h.styleSheet()


def test_quota_bar_stale_overrides_red(qtbot):
    """U9: stale data wins over the percent-based colour — we want
    "I don't trust this" to surface before "you're at the limit",
    so a stale 95% reads gray, not red."""
    p = _panel_with_quota(qtbot, quota=_make_quota(five_pct=95.0, is_stale=True))
    p.refresh_usage_bar()
    assert "#6b7280" in p._quota_bar_5h.styleSheet()    # _BAR_STALE
    assert "#ef4444" not in p._quota_bar_5h.styleSheet()


def _make_full_details(s, **overrides):
    """Helper: SessionDetails with sensible defaults; tests override
    only what they care about."""
    from claude_island.core.models import (
        ModelTotals as _MT,
        SessionDetails as _SD,
    )
    base = dict(
        session=s,
        name="cc-learning",
        ai_title="Refactor scanner to async iter",
        git_branch="feat-async",
        last_prompt="please refactor this scanner",
        started_at=datetime.now(timezone.utc) - timedelta(hours=2),
        status="busy",
        cc_version="2.1.123",
        cost_usd=2.67,
        turn_count=42,
        sidechain_count=3,
        per_model=(
            _MT(model="claude-sonnet-4-6", input_tokens=1000,
                output_tokens=20000, cache_creation_tokens=300000,
                cache_read_tokens=5000000, cost_usd=2.40),
            _MT(model="claude-haiku-4-5", input_tokens=100,
                output_tokens=200, cache_creation_tokens=1000,
                cache_read_tokens=8000, cost_usd=0.27),
        ),
        effective_uuid="abc12345-6789-0000-0000-000000000000",
    )
    base.update(overrides)
    return _SD(**base)


def test_row_meta_shows_cost_when_details_available(qtbot):
    """The user wanted the right side of the row to show *cumulative
    session cost* instead of an age string. Title should also pick up
    the human ``name`` from the composer."""
    from claude_island.ui.expanded_window import ExpandedWindow
    from PySide6.QtWidgets import QLabel as _QL

    s = _session(1, "/some/path/foo")
    details = _make_full_details(s)

    capsule = QWidget()
    capsule.show()
    panel = ExpandedWindow(
        capsule=capsule,
        controller=IslandController(),
        get_usage_totals=lambda p: UsageTotals(period=p),
        get_session_details=lambda _s: details,
    )
    qtbot.addWidget(panel)
    qtbot.addWidget(capsule)
    panel.refresh_sessions([s])

    btn = panel._rows[1]
    assert btn.findChild(_QL, "name_label").text() == "cc-learning"
    assert btn.findChild(_QL, "meta_label").text() == "$2.67"
    # The hover tooltip is now intentionally empty — replaced by
    # right-click popup. Avoids two competing surfaces for the same info.
    assert btn.toolTip() == ""


def test_row_meta_renders_dash_when_no_details(qtbot):
    """Composer unwired → meta shows ``—`` (not an age fallback)."""
    from PySide6.QtWidgets import QLabel as _QL
    panel = _panel_with_session(qtbot, lambda: None)
    panel.refresh_sessions([_session(7, "/proj/foo")])
    btn = panel._rows[7]
    assert btn.findChild(_QL, "meta_label").text() == "—"


def test_row_has_custom_context_menu_policy(qtbot):
    """Right-click is wired via Qt.CustomContextMenu so we own the
    event. Without this, Qt would either show its built-in
    text-context-menu or do nothing."""
    panel = _panel_with_session(qtbot, lambda: None)
    panel.refresh_sessions([_session(1, "/a")])
    btn = panel._rows[1]
    from PySide6.QtCore import Qt as _Qt
    assert btn.contextMenuPolicy() == _Qt.ContextMenuPolicy.CustomContextMenu


def test_detail_popup_renders_all_sections(qtbot):
    """Build a popup directly with full details and verify every
    section's text is present. Doesn't simulate the right-click event;
    that path is exercised by _show_detail_popup which we test
    separately."""
    from claude_island.ui.expanded_window import SessionDetailPopup
    s = _session(1, "/some/path/foo")
    details = _make_full_details(s)

    popup = SessionDetailPopup(details, s)
    qtbot.addWidget(popup)

    # Walk all child labels and concatenate their text — easiest way
    # to assert "this string appears somewhere in the popup".
    from PySide6.QtWidgets import QLabel as _QL
    text = " | ".join(
        lbl.text() for lbl in popup.findChildren(_QL) if lbl.text()
    )
    # Header card
    assert "cc-learning" in text
    assert "Refactor scanner to async iter" in text
    # Status pill removed by design — idle/waiting/busy carry low signal
    # for popup users; no longer rendered.
    # Meta card
    assert "abc12345" in text                    # short uuid
    assert "feat-async" in text
    assert "2.1.123" in text                     # cc version (header subtitle)
    assert "foo" in text                         # cwd basename
    # Tokens card
    assert "TOKENS" in text
    assert "Sonnet" in text and "Haiku" in text
    assert "$2.40" in text                       # per-model cost
    assert "42 turns" in text
    assert "3 subagent" in text
    # Prompt card — collapsed view elides to popup-inner-width to
    # keep popup at _PANEL_W (was: full string visible until elide
    # was added in the dense-inspector pass).
    assert "LAST PROMPT" in text
    assert "please refactor" in text


def test_detail_popup_skips_prompt_card_when_empty(qtbot):
    """No last_prompt → don't render an empty 'LAST PROMPT' card."""
    from claude_island.ui.expanded_window import SessionDetailPopup
    s = _session(1, "/a")
    details = _make_full_details(s, last_prompt=None)
    popup = SessionDetailPopup(details, s)
    qtbot.addWidget(popup)
    from PySide6.QtWidgets import QLabel as _QL
    text = " | ".join(l.text() for l in popup.findChildren(_QL) if l.text())
    assert "LAST PROMPT" not in text


def test_detail_popup_uses_main_panel_style_tokens(qtbot):
    """Visual-consistency safety net. The dense-inspector design uses
    flat sections separated by `_STYLE_SEP` dividers (no sub-cards),
    so check that the standard separator colour is in use."""
    from claude_island.ui.expanded_window import SessionDetailPopup, _STYLE_SEP
    s = _session(1, "/a")
    popup = SessionDetailPopup(_make_full_details(s), s)
    qtbot.addWidget(popup)
    from PySide6.QtWidgets import QFrame as _QF
    frames = popup.findChildren(_QF)
    # At least one section divider should carry the shared sep style.
    assert any(_STYLE_SEP in (f.styleSheet() or "") for f in frames)


def test_show_detail_popup_constructs_and_holds_reference(qtbot):
    """End-to-end-ish: ExpandedWindow._show_detail_popup constructs a
    popup from the composer's details and keeps a reference so Qt
    doesn't immediately GC it."""
    from claude_island.ui.expanded_window import (
        ExpandedWindow, SessionDetailPopup,
    )
    s = _session(1, "/a")
    details = _make_full_details(s)

    capsule = QWidget()
    capsule.show()
    panel = ExpandedWindow(
        capsule=capsule, controller=IslandController(),
        get_usage_totals=lambda p: UsageTotals(period=p),
        get_session_details=lambda _s: details,
    )
    qtbot.addWidget(panel)
    qtbot.addWidget(capsule)
    panel.refresh_sessions([s])

    btn = panel._rows[1]
    # Drive the slot directly (bypassing Qt's mouse-event plumbing).
    from PySide6.QtCore import QPoint
    panel._show_detail_popup(btn, QPoint(5, 5))

    assert isinstance(panel._active_detail_popup, SessionDetailPopup)


def test_detail_popup_uuid_short_display_full_copy(qtbot):
    """ID row shows the first-8-char prefix for scanability; clicking it
    copies the *full* 36-char UUID to the clipboard so ``claude --resume``
    paste-flow still works. Click also shows 'Copied' feedback."""
    from claude_island.ui.expanded_window import SessionDetailPopup, _CopyableIdLabel
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QLabel, QApplication
    s = _session(1, "/a")
    full_uuid = "abc12345-6789-0000-0000-000000000000"
    details = _make_full_details(s, effective_uuid=full_uuid)

    popup = SessionDetailPopup(details, s)
    qtbot.addWidget(popup)
    popup.show()   # required before event delivery

    copyable = popup.findChild(_CopyableIdLabel)
    assert copyable is not None, "ID row should contain a _CopyableIdLabel"
    # The visible label is the 8-char prefix, NOT the full UUID.
    uuid_label = copyable.findChild(QLabel)
    assert uuid_label is not None
    assert uuid_label.text() == "abc12345"
    assert full_uuid not in uuid_label.text()

    # Click → clipboard contains the FULL uuid, "Copied" feedback shows.
    from PySide6.QtGui import QMouseEvent
    from PySide6.QtCore import QPointF
    copyable.mousePressEvent(QMouseEvent(
        QMouseEvent.Type.MouseButtonPress,
        QPointF(), QPointF(), QPointF(),
        Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier,
    ))
    qtbot.wait(50)
    assert QApplication.clipboard().text() == full_uuid
    copied_label = next(
        (lbl for lbl in copyable.findChildren(QLabel) if lbl.text() == "Copied"), None
    )
    assert copied_label is not None
    assert copied_label.isVisible()


def test_spend_card_model_breakdown_shows_top_models(qtbot):
    """U7: get_totals_by_model populated → proportional bar rows shown for top models."""
    by_model = (
        _ModelTotals(model="claude-sonnet-4-5", input_tokens=0, output_tokens=0,
                     cache_creation_tokens=0, cache_read_tokens=0, cost_usd=2.54),
        _ModelTotals(model="claude-haiku-4-5", input_tokens=0, output_tokens=0,
                     cache_creation_tokens=0, cache_read_tokens=0, cost_usd=0.13),
    )
    # Provide non-zero totals so the bar container is shown (cost_usd > 0 gate)
    # cost_usd is derived from input_cost + output_cost + cache_creation_cost + cache_read_cost
    totals = UsageTotals(
        period="5h",
        input_tokens=0, output_tokens=0,
        cache_creation_tokens=0, cache_read_tokens=0,
        input_cost=1.0, output_cost=1.0,
        cache_creation_cost=0.4, cache_read_cost=0.27,
    )
    p = _panel_with_quota(qtbot, totals=totals, by_model=by_model)
    p.refresh_usage_bar()

    # Spend bar container should be shown (cost > 0 and by_model wired).
    # Use isHidden() not isVisible() — isVisible() returns False when the
    # top-level window is hidden, but isHidden() correctly reflects whether
    # show() was called on this widget regardless of parent visibility.
    assert not p._spend_bar_container.isHidden()
    # First row should show Sonnet with its cost
    first_row = p._spend_bar_rows[0]
    assert not first_row.isHidden()
    assert first_row._spend_name.text() == "Sonnet"
    assert "$" in first_row._spend_cost.text()


# ============================================================================
# Multi-provider tabs (5h card pill switcher)
# ============================================================================

def _build_panel_with_tabs(
    qtbot,
    *,
    available,
    selected="anthropic",
    on_provider_selected=None,
    get_session_usage=None,
):
    capsule = QWidget()
    capsule.show()
    controller = IslandController()
    p = ExpandedWindow(
        capsule=capsule,
        controller=controller,
        get_usage_totals=lambda period: UsageTotals(period=period),
        get_session_usage=get_session_usage,
        available_providers=available,
        selected_provider=selected,
        on_provider_selected=on_provider_selected,
    )
    qtbot.addWidget(p)
    qtbot.addWidget(capsule)
    return p


def test_single_provider_renders_one_pill(qtbot):
    """Single-provider state still renders the one pill (e.g. just
    [Anthropic]) — earlier the single-provider branch dropped to a
    static "ANTHROPIC QUOTA" text label, which read as a section
    header rather than a current-selection indicator and confused
    users. With the always-pill design the user sees a uniform
    selected-tab affordance regardless of provider count."""
    p = _build_panel_with_tabs(qtbot, available=["anthropic"])
    assert set(p._provider_btns.keys()) == {"anthropic"}
    assert p._provider_btns["anthropic"].isChecked() is True


def test_no_tabs_rendered_when_zero_providers(qtbot):
    """Empty list / None → no tabs (legacy callers, tests)."""
    p = _build_panel_with_tabs(qtbot, available=[], selected=None)
    assert p._provider_btns == {}


def test_tabs_rendered_for_two_providers(qtbot):
    p = _build_panel_with_tabs(qtbot, available=["anthropic", "minimax"])
    assert set(p._provider_btns.keys()) == {"anthropic", "minimax"}
    assert p._provider_btns["anthropic"].isChecked() is True
    assert p._provider_btns["minimax"].isChecked() is False


def test_clicking_tab_updates_state_and_notifies(qtbot):
    """Click → selected_provider changes, callback fires once with the
    new name, refresh_usage_bar runs, and only the clicked pill is
    checked."""
    fired: list[str] = []
    p = _build_panel_with_tabs(
        qtbot,
        available=["anthropic", "minimax"],
        on_provider_selected=fired.append,
    )
    p._provider_btns["minimax"].click()

    assert fired == ["minimax"]
    assert p.selected_provider_name() == "minimax"
    assert p._provider_btns["anthropic"].isChecked() is False
    assert p._provider_btns["minimax"].isChecked() is True


def test_reclicking_active_tab_is_noop(qtbot):
    fired: list[str] = []
    p = _build_panel_with_tabs(
        qtbot,
        available=["anthropic", "minimax"],
        selected="anthropic",
        on_provider_selected=fired.append,
    )
    p._provider_btns["anthropic"].click()
    assert fired == []
    assert p.selected_provider_name() == "anthropic"


def test_tab_callback_failure_does_not_crash_ui(qtbot):
    """A persistence failure (disk full, permission error) must not
    take down the UI thread."""
    def boom(_: str) -> None:
        raise OSError("disk full")

    p = _build_panel_with_tabs(
        qtbot,
        available=["anthropic", "minimax"],
        on_provider_selected=boom,
    )
    p._provider_btns["minimax"].click()  # must not raise
    # Selection still flips in-process even though persistence failed.
    assert p.selected_provider_name() == "minimax"


# ============================================================================
# Detail popup: REPAIR card (strip thinking blocks for cross-provider rescue)
# ============================================================================

def test_repair_icon_renders_when_session_has_uuid(qtbot):
    """The repair ⟲ icon appears in the popup header whenever the
    session has an effective uuid (i.e., a transcript on disk to
    operate on). The tooltip carries the full explanation."""
    from claude_island.ui.expanded_window import SessionDetailPopup
    s = _session(1, "/some/path/foo")
    details = _make_full_details(s)
    popup = SessionDetailPopup(details, s)
    qtbot.addWidget(popup)

    assert popup._repair_icon is not None
    # Now an icon, not a text-link button.
    assert popup._repair_icon.text() == "⟲"
    assert "thinking" in popup._repair_icon.toolTip().lower()
    assert "backup" in popup._repair_icon.toolTip().lower()


def test_repair_icon_hidden_when_no_uuid(qtbot):
    """Synthetic / orphan sessions have no transcript file to repair —
    don't render the ⚙ icon at all (it would be useless)."""
    from claude_island.ui.expanded_window import SessionDetailPopup
    from PySide6.QtWidgets import QLabel as _QL
    s = _session(1, "/p")
    details = _make_full_details(s, effective_uuid="")
    popup = SessionDetailPopup(details, s)
    qtbot.addWidget(popup)

    # No repair icon when there's no UUID
    assert popup._repair_icon is None
    # Status label is hidden (created but kept for layout consistency)
    assert not popup._repair_status.isVisible()


def test_repair_button_strips_thinking_and_disables_self(qtbot, tmp_path):
    """Click → strip_thinking_blocks runs → status reads "Removed N
    blocks", button becomes disabled with text "Done"."""
    import json
    from unittest.mock import patch
    from claude_island.ui.expanded_window import SessionDetailPopup

    # Use a short project_path (NOT pytest's nested tmp_path) so the
    # final slug stays well under Windows' 260-char MAX_PATH limit —
    # the .bak.<unix-ts> suffix adds another 14 chars.
    s = _session(1, "C:/X")
    full_uuid = "abc12345-6789-0000-0000-000000000000"
    details = _make_full_details(s, effective_uuid=full_uuid)

    # Build a transcript on disk that the popup will try to repair.
    # Path follows Claude Code's convention: ~/.claude/projects/<slug>/<uuid>.jsonl
    # — we patch _claude_projects_root() to redirect at that root.
    from claude_island.core.models import project_hash
    fake_home = tmp_path / "fake_home"
    proj_dir = fake_home / ".claude" / "projects" / project_hash(s.project_path)
    proj_dir.mkdir(parents=True)
    jsonl_path = proj_dir / f"{full_uuid}.jsonl"
    jsonl_path.write_text(
        json.dumps({"message": {"content": [
            {"type": "thinking", "thinking": "x", "signature": "abc"},
            {"type": "text", "text": "hello"},
        ]}}) + "\n",
        encoding="utf-8",
    )

    popup = SessionDetailPopup(details, s)
    qtbot.addWidget(popup)

    with patch(
        "claude_island.ui.expanded_window._claude_projects_root",
        return_value=fake_home / ".claude" / "projects",
    ):
        popup._on_strip_thinking()

    # File should no longer contain the thinking block, and a backup
    # should have been written alongside.
    cleaned = json.loads(jsonl_path.read_text(encoding="utf-8").strip())
    types = [c.get("type") for c in cleaned["message"]["content"]]
    assert types == ["text"]
    backups = list(proj_dir.glob(f"{full_uuid}.jsonl.bak.*"))
    assert len(backups) == 1
    # Status reflects the result; icon becomes disabled and shows "Done".
    assert "Removed 1 thinking block" in popup._repair_status.text()
    assert popup._repair_icon.isEnabled() is False
    assert popup._repair_icon.text() == "Done"


def test_repair_button_handles_missing_transcript(qtbot, tmp_path):
    """If the JSONL doesn't exist (session moved / wrong cwd), surface
    a clear error in the status line — never crash the popup."""
    from unittest.mock import patch
    from claude_island.ui.expanded_window import SessionDetailPopup

    s = _session(1, str(tmp_path))
    details = _make_full_details(s, effective_uuid="abc12345-6789-0000-0000-000000000000")
    popup = SessionDetailPopup(details, s)
    qtbot.addWidget(popup)

    fake_home = tmp_path / "empty_home"   # nothing under here
    fake_home.mkdir()
    with patch(
        "claude_island.ui.expanded_window._claude_projects_root",
        return_value=fake_home / ".claude" / "projects",
    ):
        popup._on_strip_thinking()  # must not raise

    assert "Transcript not found" in popup._repair_status.text()
    # Icon still active so the user can retry after fixing the path.
    assert popup._repair_icon.isEnabled() is True


def test_repair_button_zero_blocks_message(qtbot, tmp_path):
    """A clean transcript should report 'No thinking blocks found' and
    still leave a backup (for symmetry / forensic clarity)."""
    import json
    from unittest.mock import patch
    from claude_island.ui.expanded_window import SessionDetailPopup
    from claude_island.core.models import project_hash

    s = _session(1, "C:/Y")  # short path → short slug → fits MAX_PATH
    full_uuid = "abc12345-6789-0000-0000-000000000000"
    details = _make_full_details(s, effective_uuid=full_uuid)

    fake_home = tmp_path / "fake_home"
    proj_dir = fake_home / ".claude" / "projects" / project_hash(s.project_path)
    proj_dir.mkdir(parents=True)
    jsonl_path = proj_dir / f"{full_uuid}.jsonl"
    jsonl_path.write_text(
        json.dumps({"message": {"content": [{"type": "text", "text": "ok"}]}}) + "\n",
        encoding="utf-8",
    )

    popup = SessionDetailPopup(details, s)
    qtbot.addWidget(popup)
    with patch(
        "claude_island.ui.expanded_window._claude_projects_root",
        return_value=fake_home / ".claude" / "projects",
    ):
        popup._on_strip_thinking()

    assert "No thinking blocks found" in popup._repair_status.text()
    assert len(list(proj_dir.glob(f"{full_uuid}.jsonl.bak.*"))) == 1


# ============================================================================
# Detail popup: Dense Inspector redesign — aggregation, hiding rules, footer
# ============================================================================

def test_aggregate_per_model_dedupes_by_display_label():
    """Two raw model ids that share a display label (both 'Opus') get
    merged into one row with summed cost/tokens, sorted by cost desc."""
    from claude_island.ui.expanded_window import _aggregate_per_model_for_display
    from claude_island.core.models import ModelTotals as _MT
    rows = _aggregate_per_model_for_display((
        _MT(model="claude-opus-4-5", input_tokens=100, output_tokens=200,
            cache_creation_tokens=10, cache_read_tokens=20, cost_usd=10.0),
        _MT(model="claude-opus-4-6", input_tokens=200, output_tokens=300,
            cache_creation_tokens=30, cache_read_tokens=40, cost_usd=15.0),
        _MT(model="claude-sonnet-4-6", input_tokens=50, output_tokens=60,
            cache_creation_tokens=5, cache_read_tokens=6, cost_usd=2.0),
    ))
    # Two Opus + one Sonnet input → one Opus + one Sonnet output.
    labels = [r.label for r in rows]
    assert labels == ["Opus", "Sonnet"]   # sorted by cost desc
    opus = rows[0]
    assert opus.cost_usd == 25.0
    assert opus.input_tokens == 300
    assert opus.output_tokens == 500
    assert opus.cache_creation_tokens == 40
    assert opus.cache_read_tokens == 60


def test_aggregate_per_model_drops_zero_cost_zero_token_rows():
    """``<synthetic>`` and similar all-zero placeholders disappear
    entirely — they're noise, not data."""
    from claude_island.ui.expanded_window import _aggregate_per_model_for_display
    from claude_island.core.models import ModelTotals as _MT
    rows = _aggregate_per_model_for_display((
        _MT(model="<synthetic>", input_tokens=0, output_tokens=0,
            cache_creation_tokens=0, cache_read_tokens=0, cost_usd=0.0),
        _MT(model="claude-haiku-4-5", input_tokens=10, output_tokens=20,
            cache_creation_tokens=0, cache_read_tokens=0, cost_usd=0.5),
    ))
    labels = [r.label for r in rows]
    assert "<synthetic>" not in labels
    assert labels == ["Haiku"]


def test_detail_popup_dedupes_models_in_render(qtbot):
    """End-to-end check: feeding two Opus rows produces ONE 'Opus'
    label in the rendered popup (visual de-dup, not just data-level)."""
    from claude_island.ui.expanded_window import SessionDetailPopup
    from claude_island.core.models import ModelTotals as _MT
    from PySide6.QtWidgets import QLabel as _QL
    s = _session(1, "/x")
    details = _make_full_details(s, per_model=(
        _MT(model="claude-opus-4-5", input_tokens=100, output_tokens=200,
            cache_creation_tokens=10, cache_read_tokens=20, cost_usd=65.0),
        _MT(model="claude-opus-4-6", input_tokens=200, output_tokens=300,
            cache_creation_tokens=30, cache_read_tokens=40, cost_usd=64.0),
    ))
    popup = SessionDetailPopup(details, s)
    qtbot.addWidget(popup)
    # Count "Opus" labels — must be exactly 1 (the merged row).
    opus_labels = [
        l for l in popup.findChildren(_QL) if l.text() == "Opus"
    ]
    assert len(opus_labels) == 1


def test_detail_popup_hides_synthetic_zero_cost_row(qtbot):
    """``<synthetic>`` placeholder gets dropped from the rendered popup
    so the user doesn't see meaningless ``<synthetic>  $0.000`` noise."""
    from claude_island.ui.expanded_window import SessionDetailPopup
    from claude_island.core.models import ModelTotals as _MT
    from PySide6.QtWidgets import QLabel as _QL
    s = _session(1, "/x")
    details = _make_full_details(s, per_model=(
        _MT(model="<synthetic>", input_tokens=0, output_tokens=0,
            cache_creation_tokens=0, cache_read_tokens=0, cost_usd=0.0),
        _MT(model="claude-sonnet-4-6", input_tokens=100, output_tokens=200,
            cache_creation_tokens=0, cache_read_tokens=0, cost_usd=1.0),
    ))
    popup = SessionDetailPopup(details, s)
    qtbot.addWidget(popup)
    text = " | ".join(l.text() for l in popup.findChildren(_QL) if l.text())
    assert "<synthetic>" not in text
    assert "Sonnet" in text  # the real row still rendered


def test_detail_popup_hides_branch_when_head(qtbot):
    """``HEAD`` is git's detached-state placeholder, not a useful
    branch name — suppress the row to avoid visual noise."""
    from claude_island.ui.expanded_window import SessionDetailPopup
    from PySide6.QtWidgets import QLabel as _QL
    s = _session(1, "/x")
    details = _make_full_details(s, git_branch="HEAD")
    popup = SessionDetailPopup(details, s)
    qtbot.addWidget(popup)
    keys = [l.text() for l in popup.findChildren(_QL) if l.text() == "Branch"]
    assert keys == []


def test_detail_popup_prompt_collapsed_by_default(qtbot):
    """A long multi-line prompt renders as a single truncated preview
    by default; the toggle expands it to the full body."""
    from claude_island.ui.expanded_window import SessionDetailPopup
    s = _session(1, "/x")
    long_prompt = "first line of prompt\n" + ("x" * 200)
    details = _make_full_details(s, last_prompt=long_prompt)
    popup = SessionDetailPopup(details, s)
    qtbot.addWidget(popup)

    # Default: collapsed → first line + "…" so the user knows more
    # content exists beneath.
    assert popup._prompt_expanded is False
    assert popup._prompt_body is not None
    assert popup._prompt_body.text() == "first line of prompt…"
    assert popup._prompt_toggle is not None
    assert popup._prompt_toggle.text() == "[展开]"

    # Toggle → expanded: collapsed QLabel hides, expanded QTextEdit
    # appears with the full body. Toggle text flips to "[收起]".
    popup._on_toggle_prompt()
    assert popup._prompt_expanded is True
    assert popup._prompt_full_view is not None
    assert popup._prompt_body.isHidden()
    assert not popup._prompt_full_view.isHidden()
    full_text = popup._prompt_full_view.toPlainText()
    assert "first line of prompt" in full_text
    assert "x" * 100 in full_text  # original tail present
    assert popup._prompt_toggle.text() == "[收起]"

    # Toggle back → collapsed: full view hidden, label visible again.
    popup._on_toggle_prompt()
    assert popup._prompt_expanded is False
    assert popup._prompt_full_view.isHidden()
    assert not popup._prompt_body.isHidden()
    assert popup._prompt_toggle.text() == "[展开]"


def test_detail_popup_short_prompt_no_toggle(qtbot):
    """Short single-line prompts that fit fully render without the
    expand toggle — collapsing them would be confusing.

    Toggle is always *created* (hidden by default) and shown only after
    the post-show font-metrics check confirms elision actually happens.
    For "quick question" no elision → toggle stays hidden."""
    from claude_island.ui.expanded_window import SessionDetailPopup
    s = _session(1, "/x")
    details = _make_full_details(s, last_prompt="quick question")
    popup = SessionDetailPopup(details, s)
    qtbot.addWidget(popup)
    popup.show()
    assert popup._prompt_toggle is not None
    assert not popup._prompt_toggle.isVisible()
    assert popup._prompt_body.text() == "quick question"


def test_detail_popup_cjk_long_prompt_shows_toggle(qtbot):
    """CJK glyphs render at ~14px each in 12px-stylesheet labels —
    30+ chars exceed the 288px collapsed budget. Earlier `len > 80`
    heuristic missed this; the post-show metrics check catches it."""
    from claude_island.ui.expanded_window import SessionDetailPopup
    s = _session(1, "/x")
    # User's real prompt from screenshot — 53 chars but ~439px wide.
    long_cjk = "我已经merge了 checkout到master pull最新代码，然后把改动同步到~/.claude下"
    details = _make_full_details(s, last_prompt=long_cjk)
    popup = SessionDetailPopup(details, s)
    qtbot.addWidget(popup)
    popup.show()
    assert popup._prompt_toggle is not None
    assert popup._prompt_toggle.isVisible(), (
        "toggle should be visible because the CJK prompt is wider "
        "than the popup-inner-width and was elided"
    )
    # The displayed text should include the elide marker.
    assert "…" in popup._prompt_body.text()


def test_detail_popup_header_action_icons(qtbot):
    """Header right side carries three icon buttons:
        ⧉ Copy ID  ↗ Open folder  ⟲ Reset thinking blocks
    The destructive reset uses a separate style (amber hover) but
    sits next to the safe actions for visual consistency."""
    from claude_island.ui.expanded_window import SessionDetailPopup
    s = _session(1, "/x")
    details = _make_full_details(s)
    popup = SessionDetailPopup(details, s)
    qtbot.addWidget(popup)
    assert popup._copy_id_btn is not None
    assert popup._copy_id_btn.text() == "⧉"
    assert "Copy" in popup._copy_id_btn.toolTip()
    assert popup._open_folder_btn is not None
    assert popup._open_folder_btn.text() == "↗"
    assert "Open" in popup._open_folder_btn.toolTip()
    assert popup._repair_btn is not None
    assert popup._repair_btn.text() == "⟲"
    assert "thinking" in popup._repair_btn.toolTip().lower()


def test_detail_popup_no_actions_when_no_uuid(qtbot):
    """No uuid → no transcript to repair AND no id to copy. Copy ID
    and Reset hide; Open folder (works without uuid) stays."""
    from claude_island.ui.expanded_window import SessionDetailPopup
    s = _session(1, "/x")
    details = _make_full_details(s, effective_uuid="")
    popup = SessionDetailPopup(details, s)
    qtbot.addWidget(popup)
    assert popup._copy_id_btn is None
    assert popup._repair_btn is None
    assert popup._open_folder_btn is not None  # always present


def test_detail_popup_status_pill_never_renders(qtbot):
    """Status pill (idle / waiting / busy) was removed by design — the
    information is low-value for popup users (who came here to inspect,
    not monitor) and the chip created visual noise next to the action
    icons. None of the three states should render a pill."""
    from claude_island.ui.expanded_window import SessionDetailPopup
    from PySide6.QtWidgets import QLabel as _QL
    s = _session(1, "/x")
    for state in ("idle", "waiting", "busy"):
        details = _make_full_details(s, status=state)
        popup = SessionDetailPopup(details, s)
        qtbot.addWidget(popup)
        text = " | ".join(l.text() for l in popup.findChildren(_QL) if l.text())
        assert state not in text, f"status '{state}' should not render"


def test_detail_popup_copy_id_action_writes_clipboard(qtbot):
    """Clicking the footer 'Copy ID' button puts the full UUID on the
    clipboard and surfaces a confirmation in the status line."""
    from claude_island.ui.expanded_window import SessionDetailPopup
    from PySide6.QtWidgets import QApplication
    s = _session(1, "/x")
    full_uuid = "deadbeef-0000-0000-0000-000000000000"
    details = _make_full_details(s, effective_uuid=full_uuid)
    popup = SessionDetailPopup(details, s)
    qtbot.addWidget(popup)
    popup._on_copy_id()
    assert QApplication.clipboard().text() == full_uuid
    assert "deadbeef" in popup._repair_status.text()


def test_detail_popup_path_click_opens_folder(qtbot, monkeypatch):
    """Path value is clickable and opens the folder in explorer (same as
    the ↗ button). Clicking the path label invokes _open_in_explorer."""
    from PySide6.QtCore import Qt, QPointF
    from PySide6.QtGui import QMouseEvent
    from claude_island.ui import expanded_window as ew
    from claude_island.ui.expanded_window import SessionDetailPopup
    s = _session(1, "/some/proj/path")
    details = _make_full_details(s)
    popup = SessionDetailPopup(details, s)
    qtbot.addWidget(popup)
    popup.show()

    # Path label is now a plain QLabel (not _ClickToCopyLabel)
    from PySide6.QtWidgets import QLabel
    path_label = None
    for lbl in popup.findChildren(QLabel):
        if lbl.text() == str(s.project_path):
            path_label = lbl
            break
    assert path_label is not None
    assert path_label.toolTip() == "Open project folder in file explorer"

    called: list[Path] = []
    monkeypatch.setattr(ew, "_open_in_explorer", lambda p: called.append(p))
    path_label.mousePressEvent(QMouseEvent(
        QMouseEvent.Type.MouseButtonPress,
        QPointF(), QPointF(), QPointF(),
        Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier,
    ))
    assert called == [s.project_path]


def test_detail_popup_header_layout_stable_when_prompt_toggles(qtbot):
    """Regression: when prompt is expanded, header section's geometry
    must NOT change. Earlier bugs let layout surplus stretch the
    header, leaving a visible gap between subtitle and ID row."""
    from claude_island.ui.expanded_window import SessionDetailPopup
    long_prompt = "first line\n" + ("x" * 500)
    s = _session(1, "/x")
    details = _make_full_details(s, last_prompt=long_prompt)
    popup = SessionDetailPopup(details, s)
    qtbot.addWidget(popup)
    popup.show()

    root = popup.layout()
    geom_before = root.itemAt(0).widget().geometry()
    popup._on_toggle_prompt()
    geom_after = root.itemAt(0).widget().geometry()
    # Allow 2px tolerance for font-baseline rounding; the original bug
    # stretched header by 30+ px so this still catches it definitively.
    height_diff = abs(geom_after.height() - geom_before.height())
    assert height_diff <= 2, (
        f"header section height changed by {height_diff}px on prompt "
        f"expand (was {geom_before.height()}, now {geom_after.height()})"
    )
    assert geom_before.topLeft() == geom_after.topLeft()


def test_detail_popup_open_folder_calls_helper(qtbot, monkeypatch):
    """Clicking 'Open folder' invokes _open_in_explorer with the
    project_path. Helper is patched so the test doesn't actually shell
    out."""
    from claude_island.ui import expanded_window as ew
    from claude_island.ui.expanded_window import SessionDetailPopup
    s = _session(1, "/test/proj")
    details = _make_full_details(s)
    popup = SessionDetailPopup(details, s)
    qtbot.addWidget(popup)

    called: list[Path] = []
    monkeypatch.setattr(ew, "_open_in_explorer", lambda p: called.append(p))
    popup._on_open_folder()
    assert called == [s.project_path]


# ============================================================================
# Inline session rename — ✎ button on the detail popup header swaps the
# title for a QLineEdit; Enter commits via on_rename callback, Esc cancels.
# ============================================================================

class TestDetailPopupRename:
    """Edit affordance on the detail popup header."""

    @staticmethod
    def _popup(qtbot, *, with_rename: bool):
        from claude_island.ui.expanded_window import SessionDetailPopup
        s = _session(1, "/proj")
        details = _make_full_details(s, effective_uuid="uuid-1")
        renames: list = []
        popup = SessionDetailPopup(
            details, s,
            on_rename=(lambda u, n: renames.append((u, n))) if with_rename else None,
        )
        qtbot.addWidget(popup)
        return popup, renames

    def test_edit_button_visible_when_callback_wired_and_uuid_present(self, qtbot):
        popup, _ = self._popup(qtbot, with_rename=True)
        assert popup._edit_btn is not None
        assert popup._edit_btn.text() == "✎"
        assert "Rename" in popup._edit_btn.toolTip()

    def test_edit_button_hidden_without_callback(self, qtbot):
        # Tests / detached use that don't supply on_rename → no edit
        # affordance, popup stays read-only.
        popup, _ = self._popup(qtbot, with_rename=False)
        assert popup._edit_btn is None

    def test_enter_rename_mode_swaps_label_for_lineedit(self, qtbot):
        popup, _ = self._popup(qtbot, with_rename=True)
        from PySide6.QtWidgets import QLineEdit
        assert popup._name_edit is None
        popup._enter_rename_mode()
        assert popup._name_edit is not None
        assert isinstance(popup._name_edit, QLineEdit)
        # Edit button disabled while editing so the user can't double-fire.
        assert popup._edit_btn is not None and not popup._edit_btn.isEnabled()
        # Original label preserved (hidden) so cancel can restore it.
        assert popup._name_label is not None and popup._name_label.isHidden()

    def test_enter_rename_mode_idempotent(self, qtbot):
        # Double-clicking ✎ shouldn't create a second QLineEdit.
        popup, _ = self._popup(qtbot, with_rename=True)
        popup._enter_rename_mode()
        first_edit = popup._name_edit
        popup._enter_rename_mode()
        assert popup._name_edit is first_edit

    def test_commit_rename_invokes_callback_and_exits_edit(self, qtbot):
        popup, renames = self._popup(qtbot, with_rename=True)
        popup._enter_rename_mode()
        popup._name_edit.setText("frontend refactor")
        popup._commit_rename()
        # Per-session callback: uuid + new name. Project_path is no
        # longer threaded through (the dual-key design bled renames
        # across siblings).
        assert renames == [("uuid-1", "frontend refactor")]
        # Edit mode torn down, label restored, edit button re-enabled.
        # Use isHidden() not isVisible() — the popup itself is never
        # shown in tests, so isVisible would always return False.
        # isHidden() checks the explicit hide() flag.
        assert popup._name_edit is None
        assert popup._edit_btn.isEnabled()
        assert not popup._name_label.isHidden()
        assert popup._name_label.text() == "frontend refactor"

    def test_commit_empty_clears_override(self, qtbot):
        # Saving an empty string is the "restore default" gesture —
        # the callback receives "" and the platform layer translates
        # it into a delete.
        popup, renames = self._popup(qtbot, with_rename=True)
        popup._enter_rename_mode()
        popup._name_edit.setText("   ")
        popup._commit_rename()
        assert renames == [("uuid-1", "")]

    def test_cancel_rename_does_not_invoke_callback(self, qtbot):
        popup, renames = self._popup(qtbot, with_rename=True)
        original = popup._name_label.text()
        popup._enter_rename_mode()
        popup._name_edit.setText("typed but cancelled")
        popup._cancel_rename()
        assert renames == []
        # Label text untouched.
        assert popup._name_label.text() == original
        assert popup._name_edit is None

    def test_callback_exception_keeps_popup_alive(self, qtbot):
        # If on_rename raises (e.g. disk full), surface a status line
        # and exit edit mode — but don't crash the popup.
        from claude_island.ui.expanded_window import SessionDetailPopup
        s = _session(1, "/proj")
        details = _make_full_details(s, effective_uuid="uuid-1")
        def boom(u, n):
            raise RuntimeError("disk full")
        popup = SessionDetailPopup(details, s, on_rename=boom)
        qtbot.addWidget(popup)
        popup._enter_rename_mode()
        popup._name_edit.setText("anything")
        popup._commit_rename()
        # Popup still alive, edit mode exited, error in status.
        assert popup._name_edit is None
        assert "Rename failed" in popup._repair_status.text()


class TestDetailPopupSubtitle:
    """Italic subtitle in the detail popup header. Two-tier resolution:
    prefer ai_title (AI-generated, descriptive), fall back to
    original_name (Claude Code's auto name) only when the user
    renamed but the AI never assigned a title."""

    @staticmethod
    def _label_with_text(popup, text: str):
        from PySide6.QtWidgets import QLabel
        for lbl in popup.findChildren(QLabel):
            if lbl.text() == text:
                return lbl
        return None

    @staticmethod
    def _count_labels(popup, text: str) -> int:
        from PySide6.QtWidgets import QLabel
        return sum(
            1 for lbl in popup.findChildren(QLabel) if lbl.text() == text
        )

    def test_ai_title_shown_when_renamed_and_differs(self, qtbot):
        # Classic case: AI generated a title, user renamed → italic
        # surfaces the AI title underneath.
        from claude_island.ui.expanded_window import SessionDetailPopup
        s = _session(1, "/proj")
        details = _make_full_details(
            s, name="learning python",
            ai_title="Learn Python and TypeScript basics",
            original_name="cc-learning",
        )
        popup = SessionDetailPopup(details, s)
        qtbot.addWidget(popup)
        assert self._label_with_text(
            popup, "Learn Python and TypeScript basics",
        ) is not None

    def test_original_name_shown_when_renamed_and_no_ai_title(self, qtbot):
        # Session has no ai_title (Claude Code never set one) but DOES
        # have a state.name that the user renamed away from. Italic
        # falls back to the state.name so the user can still see what
        # the session used to be called.
        from claude_island.ui.expanded_window import SessionDetailPopup
        s = _session(1, "/proj")
        details = _make_full_details(
            s, name="claude md prompt coding",
            ai_title=None,
            original_name="claude md prompt",
        )
        popup = SessionDetailPopup(details, s)
        qtbot.addWidget(popup)
        assert self._label_with_text(popup, "claude md prompt") is not None

    def test_original_name_NOT_shown_when_no_rename(self, qtbot):
        # Without a rename, original_name == name == title. Surfacing
        # original_name as subtitle would just echo the title — noise.
        # Verify by counting: only one label with that text (the
        # title), not two (title + would-be-subtitle).
        from claude_island.ui.expanded_window import SessionDetailPopup
        s = _session(1, "/proj")
        details = _make_full_details(
            s, name="cc-learning",
            ai_title=None,
            original_name="cc-learning",
        )
        popup = SessionDetailPopup(details, s)
        qtbot.addWidget(popup)
        assert self._count_labels(popup, "cc-learning") == 1

    def test_no_subtitle_when_no_ai_title_and_no_rename(self, qtbot):
        # Same as above but with original_name=None too — guards the
        # "MiniMax-style session with neither ai_title nor state.name"
        # path. Title degrades to project basename; no subtitle.
        from claude_island.ui.expanded_window import SessionDetailPopup
        s = _session(1, "/proj-name")
        details = _make_full_details(
            s, name=None,
            ai_title=None,
            original_name=None,
        )
        popup = SessionDetailPopup(details, s)
        qtbot.addWidget(popup)
        # No phantom italic line of any kind.
        assert self._count_labels(popup, "proj-name") == 1

    def test_ai_title_preferred_over_original_when_both_present(self, qtbot):
        # When both candidates differ from the renamed title, ai_title
        # wins — it's typically more descriptive than the auto name.
        from claude_island.ui.expanded_window import SessionDetailPopup
        s = _session(1, "/proj")
        details = _make_full_details(
            s, name="custom",
            ai_title="The AI Generated Title",
            original_name="The Original Name",
        )
        popup = SessionDetailPopup(details, s)
        qtbot.addWidget(popup)
        assert self._label_with_text(
            popup, "The AI Generated Title",
        ) is not None
        assert self._label_with_text(popup, "The Original Name") is None

    def test_falls_through_to_original_when_rename_matches_ai_title(self, qtbot):
        # User renamed to the same string ai_title would have produced
        # → ai_title would echo the title. Fall through to
        # original_name (which still differs) so the user can see what
        # the auto-detected name was.
        from claude_island.ui.expanded_window import SessionDetailPopup
        s = _session(1, "/proj")
        details = _make_full_details(
            s, name="Refactor",
            ai_title="Refactor",
            original_name="cc-learning",
        )
        popup = SessionDetailPopup(details, s)
        qtbot.addWidget(popup)
        # Title appears once; "cc-learning" appears as the italic
        # subtitle (fall-through after ai_title echoed).
        assert self._count_labels(popup, "Refactor") == 1
        assert self._label_with_text(popup, "cc-learning") is not None

    def test_no_subtitle_when_all_candidates_echo_title(self, qtbot):
        # ai_title and original_name both match the rename, AND the
        # project basename also matches → nothing to add. Subtitle
        # suppressed, only the title shown.
        from claude_island.ui.expanded_window import SessionDetailPopup
        s = _session(1, "/Same")  # path basename also "Same"
        details = _make_full_details(
            s, name="Same",
            ai_title="Same",
            original_name="Same",
        )
        popup = SessionDetailPopup(details, s)
        qtbot.addWidget(popup)
        assert self._count_labels(popup, "Same") == 1

    def test_basename_fallback_when_renamed_and_no_other_names(self, qtbot):
        # The bug fix: user renamed but state.name was never written
        # AND no ai_title exists. Without this fallback the popup
        # would show only the custom title and nothing underneath —
        # leaving the user wondering what Claude would have called it.
        # The project basename is the last-ditch "natural" name.
        from claude_island.ui.expanded_window import SessionDetailPopup
        s = _session(1, "/home/me/claude-island")
        details = _make_full_details(
            s, name="claude-island-dev",
            ai_title=None,
            original_name=None,
        )
        popup = SessionDetailPopup(details, s)
        qtbot.addWidget(popup)
        assert self._label_with_text(popup, "claude-island") is not None


# ============================================================================
# Add-provider dialog (in-app + button) — frameless popup that lets the
# user paste credentials and have a new tab appear without restart.
# ============================================================================

class TestAddProviderDialog:
    """Pure-UI tests: dialog renders fields from each provider's
    default_config(), Save validates + invokes the callback. The
    actual file write is tested separately at the platform layer."""

    @staticmethod
    def _zhipu_cfg():
        return {
            "_help": "Paste your Z.AI key here.",
            "auth_token": "",
            "base_url": "https://api.z.ai",
        }

    @staticmethod
    def _minimax_cfg():
        return {
            "_help": "Paste your MiniMax key.",
            "auth_token": "",
            "base_url": "https://api.minimaxi.com",
        }

    def test_renders_radio_for_each_configurable(self, qtbot):
        from claude_island.ui.expanded_window import _AddProviderDialog
        dlg = _AddProviderDialog(
            configurable=[("zhipu", self._zhipu_cfg()),
                          ("minimax", self._minimax_cfg())],
            on_save=lambda *a: None,
        )
        qtbot.addWidget(dlg)
        # Radio strip has one button per configurable provider.
        assert "zhipu" in dlg._radio_btns
        assert "minimax" in dlg._radio_btns
        assert len(dlg._radio_btns) == 2

    def test_renders_form_fields_from_default_config(self, qtbot):
        from claude_island.ui.expanded_window import _AddProviderDialog
        from PySide6.QtWidgets import QLineEdit
        dlg = _AddProviderDialog(
            configurable=[("zhipu", self._zhipu_cfg())],
            on_save=lambda *a: None,
        )
        qtbot.addWidget(dlg)
        # auth_token + base_url QLineEdits both registered for the active form.
        keys = [k for k, _ in dlg._inputs["zhipu"]]
        assert keys == ["auth_token", "base_url"]
        # auth_token is password-mode (echo hides input).
        token_edit = dict(dlg._inputs["zhipu"])["auth_token"]
        assert token_edit.echoMode() == QLineEdit.EchoMode.Password
        assert token_edit.text() == ""  # always empty initial regardless of seed
        # base_url pre-filled from default_config.
        url_edit = dict(dlg._inputs["zhipu"])["base_url"]
        assert url_edit.text() == "https://api.z.ai"

    def test_save_with_empty_token_shows_error_no_callback(self, qtbot):
        from claude_island.ui.expanded_window import _AddProviderDialog
        called: list = []
        dlg = _AddProviderDialog(
            configurable=[("zhipu", self._zhipu_cfg())],
            on_save=lambda *a: called.append(a),
        )
        qtbot.addWidget(dlg)
        # auth_token starts empty; click Save without typing.
        dlg._on_save_clicked()
        assert called == []                  # callback NOT invoked
        # isVisible() returns False when the widget isn't realised
        # (qtbot.addWidget doesn't show the parent). isHidden() reflects
        # only the explicit hide()/show() state regardless of ancestry.
        assert not dlg._status.isHidden()
        assert "auth_token" in dlg._status.text()

    def test_save_invokes_callback_with_field_values(self, qtbot):
        from claude_island.ui.expanded_window import _AddProviderDialog
        captured: list[tuple[str, dict]] = []
        dlg = _AddProviderDialog(
            configurable=[("zhipu", self._zhipu_cfg())],
            on_save=lambda n, f: captured.append((n, f)),
        )
        qtbot.addWidget(dlg)
        # Fill the form: paste a token, change base_url.
        inputs = dict(dlg._inputs["zhipu"])
        inputs["auth_token"].setText("z-test-key")
        inputs["base_url"].setText("https://api.z.ai")
        dlg._on_save_clicked()
        assert captured == [("zhipu", {"auth_token": "z-test-key",
                                       "base_url": "https://api.z.ai"})]

    def test_radio_switch_changes_active_form(self, qtbot):
        from claude_island.ui.expanded_window import _AddProviderDialog
        dlg = _AddProviderDialog(
            configurable=[("zhipu", self._zhipu_cfg()),
                          ("minimax", self._minimax_cfg())],
            on_save=lambda *a: None,
        )
        qtbot.addWidget(dlg)
        # Default-selected is the first provider in the list.
        assert dlg._active == "zhipu"
        assert not dlg._form_widgets["zhipu"].isHidden()
        assert dlg._form_widgets["minimax"].isHidden()
        # Click the minimax radio.
        dlg._select_provider("minimax")
        assert dlg._active == "minimax"
        assert dlg._form_widgets["zhipu"].isHidden()
        assert not dlg._form_widgets["minimax"].isHidden()

    def test_empty_state_when_nothing_configurable(self, qtbot):
        from claude_island.ui.expanded_window import _AddProviderDialog
        from PySide6.QtWidgets import QLabel
        dlg = _AddProviderDialog(
            configurable=[],
            on_save=lambda *a: None,
        )
        qtbot.addWidget(dlg)
        # No radio buttons, no inputs, no save button.
        assert not hasattr(dlg, "_radio_btns") or not dlg._radio_btns
        assert dlg._inputs == {}
        # Empty-state message is rendered as a QLabel containing the
        # word "configured" — check by text.
        labels = [l.text() for l in dlg.findChildren(QLabel)]
        assert any("configured" in t for t in labels)

    def test_callback_exception_surfaces_in_status(self, qtbot):
        # The dialog should not crash when on_save raises; instead it
        # shows the error in the status slot so the user can retry.
        from claude_island.ui.expanded_window import _AddProviderDialog

        def boom(_n, _f):
            raise RuntimeError("simulated write failure")

        dlg = _AddProviderDialog(
            configurable=[("zhipu", self._zhipu_cfg())],
            on_save=boom,
        )
        qtbot.addWidget(dlg)
        dict(dlg._inputs["zhipu"])["auth_token"].setText("k")
        dlg._on_save_clicked()
        assert not dlg._status.isHidden()
        assert "simulated write failure" in dlg._status.text()


# ============================================================================
# ExpandedWindow.set_available_providers — runtime tab strip rebuild
# ============================================================================

def _panel_with_providers(qtbot, available: list[str], selected: str | None = None,
                          on_provider_config_changed=None):
    capsule = QWidget()
    capsule.show()
    panel = ExpandedWindow(
        capsule=capsule,
        controller=IslandController(),
        get_usage_totals=lambda period: UsageTotals(period=period),
        available_providers=available,
        selected_provider=selected or (available[0] if available else None),
        on_provider_config_changed=on_provider_config_changed,
    )
    qtbot.addWidget(panel)
    qtbot.addWidget(capsule)
    return panel


class TestSetAvailableProviders:
    def test_adds_new_tab(self, qtbot):
        # Start with anthropic only — single-provider state still
        # renders one pill (always-pill design, see
        # test_single_provider_renders_one_pill).
        panel = _panel_with_providers(qtbot, ["anthropic"])
        assert set(panel._provider_btns.keys()) == {"anthropic"}
        # User adds zhipu via the + dialog → wiring layer pushes the
        # updated list back; tab strip rebuilds.
        panel.set_available_providers(["anthropic", "zhipu"], selected="anthropic")
        assert set(panel._provider_btns.keys()) == {"anthropic", "zhipu"}
        # Selected pill is checked.
        assert panel._provider_btns["anthropic"].isChecked()
        assert not panel._provider_btns["zhipu"].isChecked()

    def test_removes_obsolete_tab(self, qtbot):
        panel = _panel_with_providers(qtbot, ["anthropic", "zhipu"], "zhipu")
        assert set(panel._provider_btns.keys()) == {"anthropic", "zhipu"}
        # Token removed → wiring pushes a smaller list.
        panel.set_available_providers(["anthropic"])
        assert "zhipu" not in panel._provider_btns

    def test_falls_back_when_selected_removed(self, qtbot):
        # Selected provider is dropped; selection must reset to the
        # first remaining (since no explicit override is passed).
        panel = _panel_with_providers(qtbot, ["anthropic", "zhipu"], "zhipu")
        panel.set_available_providers(["anthropic"])
        assert panel._selected_provider == "anthropic"

    def test_explicit_selected_arg_wins(self, qtbot):
        panel = _panel_with_providers(qtbot, ["anthropic"])
        panel.set_available_providers(["anthropic", "zhipu", "minimax"], selected="minimax")
        assert panel._selected_provider == "minimax"


class TestPlusButtonVisibility:
    def test_plus_visible_when_provider_addable(self, qtbot):
        # anthropic is registered, others (minimax, zhipu) have
        # default_config() and are NOT in available → + should appear.
        called: list = []
        panel = _panel_with_providers(
            qtbot, ["anthropic"],
            on_provider_config_changed=lambda: called.append(1),
        )
        # The + button is held on the panel as _add_provider_btn.
        assert panel._add_provider_btn is not None
        assert panel._add_provider_btn.text() == "+"

    def test_plus_hidden_when_callback_not_wired(self, qtbot):
        # Without on_provider_config_changed (e.g. tests that don't
        # care about the dialog), the + button stays out of the strip.
        panel = _panel_with_providers(qtbot, ["anthropic"])
        assert getattr(panel, "_add_provider_btn", None) is None

    def test_plus_hidden_when_all_configured(self, qtbot):
        # All three providers in available → nothing left to add → no +.
        panel = _panel_with_providers(
            qtbot, ["anthropic", "minimax", "zhipu"], "anthropic",
            on_provider_config_changed=lambda: None,
        )
        assert getattr(panel, "_add_provider_btn", None) is None

    def test_dialog_save_persists_then_triggers_callback(self, qtbot, monkeypatch):
        # End-to-end through the panel: filling the form and clicking
        # Save calls set_provider_settings AND invokes the
        # on_provider_config_changed callback exactly once.
        #
        # Patch the SOURCE symbol (claude_island.platform_.providers
        # .set_provider_settings) rather than expanded_window's namespace
        # because _on_dialog_save uses a function-local lazy import.
        writes: list[tuple[str, dict]] = []
        refreshes: list = []
        monkeypatch.setattr(
            "claude_island.platform_.providers.set_provider_settings",
            lambda name, fields: writes.append((name, fields)),
        )

        panel = _panel_with_providers(
            qtbot, ["anthropic"],
            on_provider_config_changed=lambda: refreshes.append(1),
        )
        # Drive the save handler directly with realistic args.
        panel._on_dialog_save("zhipu", {"auth_token": "k", "base_url": "https://api.z.ai"})
        assert writes == [("zhipu", {"auth_token": "k", "base_url": "https://api.z.ai"})]
        assert refreshes == [1]


class TestProviderTabContextMenu:
    """Right-click on a non-anthropic quota tab → Delete option that
    removes the provider's block from providers.json and triggers a
    rebuild via the on_provider_config_changed callback."""

    def test_anthropic_tab_has_no_context_menu(self, qtbot):
        """Anthropic is the always-available baseline; the wiring layer
        must not install a delete affordance on its pill."""
        panel = _panel_with_providers(
            qtbot, ["anthropic", "minimax"], "anthropic",
            on_provider_config_changed=lambda: None,
        )
        anth_btn = panel._provider_btns["anthropic"]
        assert anth_btn.contextMenuPolicy() != Qt.ContextMenuPolicy.CustomContextMenu

    def test_non_anthropic_tab_has_custom_context_menu(self, qtbot):
        """MiniMax / Zhipu tabs opt into custom context menus so the
        Delete action can be wired."""
        panel = _panel_with_providers(
            qtbot, ["anthropic", "minimax"], "anthropic",
            on_provider_config_changed=lambda: None,
        )
        mm_btn = panel._provider_btns["minimax"]
        assert mm_btn.contextMenuPolicy() == Qt.ContextMenuPolicy.CustomContextMenu

    def test_context_menu_skipped_when_callback_not_wired(self, qtbot):
        """Without on_provider_config_changed the rebuild can't happen,
        so don't install the menu — would silently corrupt user state."""
        panel = _panel_with_providers(qtbot, ["anthropic", "minimax"], "anthropic")
        mm_btn = panel._provider_btns["minimax"]
        assert mm_btn.contextMenuPolicy() != Qt.ContextMenuPolicy.CustomContextMenu

    def test_delete_invokes_platform_helper_and_callback(self, qtbot, monkeypatch):
        """Driving _on_delete_provider_clicked directly: it must call
        delete_provider_settings(name) AND fire the rebuild callback
        exactly once. Patch the SOURCE symbol because the handler uses
        a function-local lazy import."""
        deletes: list[str] = []
        refreshes: list = []
        monkeypatch.setattr(
            "claude_island.platform_.providers.delete_provider_settings",
            lambda name: deletes.append(name),
        )
        panel = _panel_with_providers(
            qtbot, ["anthropic", "minimax"], "anthropic",
            on_provider_config_changed=lambda: refreshes.append(1),
        )
        panel._on_delete_provider_clicked("minimax")
        assert deletes == ["minimax"]
        assert refreshes == [1]
