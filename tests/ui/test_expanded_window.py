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


def test_existing_row_text_updates_in_place(panel):
    from PySide6.QtWidgets import QLabel
    panel.refresh_sessions([_session(1, "/a", ago_minutes=0)])
    btn = panel._rows[1]
    age_before = btn.findChild(QLabel, "age_label").text()

    # Same pid, same cwd, but newer activity timestamp shifts the age label.
    panel.refresh_sessions([_session(1, "/a", ago_minutes=5)])
    assert panel._rows[1] is btn  # not recreated
    age_after = btn.findChild(QLabel, "age_label").text()
    assert age_after != age_before  # age label updated in place


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


def test_session_card_active_with_quota_shows_amount_bar_and_pct(qtbot):
    """U1: active session + quota present → amount text rendered, progress
    bar visible with the right value, % text shown."""
    su = _make_session_usage(quota=_make_quota(five_pct=53.0))
    p = _panel_with_session(qtbot, lambda: su)
    p.refresh_usage_bar()

    assert "$" in p._session_amount.text()
    assert p._session_bar.isVisibleTo(p._session_card)
    assert p._session_bar.value() == 53
    assert "53%" in p._session_pct.text()


def test_session_card_active_without_quota_hides_bar(qtbot):
    """U2: active session but quota=None → main amount renders, but
    progress bar and pct text are hidden/empty."""
    su = _make_session_usage(quota=None, total_cost=1.50)
    p = _panel_with_session(qtbot, lambda: su)
    p.refresh_usage_bar()

    assert "$" in p._session_amount.text()
    assert not p._session_bar.isVisibleTo(p._session_card)
    assert p._session_pct.text() == ""


def test_session_card_quota_stale_marks_warning(qtbot):
    """U3: quota.is_stale=True → progress bar still shown but % text
    carries the ⚠ marker so the user knows the value is old."""
    su = _make_session_usage(quota=_make_quota(five_pct=20.0, is_stale=True))
    p = _panel_with_session(qtbot, lambda: su)
    p.refresh_usage_bar()

    assert "⚠" in p._session_pct.text()


def test_session_card_expired_session_dot_gray_reset_expired(qtbot):
    """U4: end_time in the past → dot uses gray colour, reset text
    reads 'expired' (when no quota overrides), amount still rendered."""
    su = _make_session_usage(
        start_offset_h=10.0,        # 10h ago
        end_offset_h=-5.0,          # 5h ago — expired
        quota=None,
    )
    p = _panel_with_session(qtbot, lambda: su)
    p.refresh_usage_bar()

    # Dot stylesheet should reference the gray colour token
    assert "#52525b" in p._session_dot.styleSheet()
    assert "expired" in p._session_reset.text().lower()


def test_session_card_empty_db_shows_no_active_session(qtbot):
    """U5: SessionUsage with start_time=None → 'No active session' text,
    no progress bar."""
    su = _make_session_usage(start_offset_h=None, end_offset_h=None,
                             quota=None, total_cost=0.0)
    p = _panel_with_session(qtbot, lambda: su)
    p.refresh_usage_bar()

    assert "no active" in p._session_amount.text().lower()
    assert not p._session_bar.isVisibleTo(p._session_card)


def test_period_card_toggle_updates_total_and_token_rows(qtbot):
    """U6: switching period (Today → Weekly) calls get_usage_totals with
    the new key and the period_total + token rows update."""
    capsule = QWidget()
    capsule.show()

    calls: list[str] = []

    def fake_totals(period):
        calls.append(period)
        # Different totals per period so the assertion can prove an update
        return UsageTotals(
            period=period,
            input_tokens=1000 if period == "today" else 9999,
            output_tokens=2000,
        )

    p = ExpandedWindow(
        capsule=capsule,
        controller=IslandController(),
        get_usage_totals=fake_totals,
    )
    qtbot.addWidget(p)
    qtbot.addWidget(capsule)
    p.refresh_usage_bar()

    today_text = p._period_tokens_io.text()
    p._on_period("weekly")
    weekly_text = p._period_tokens_io.text()

    assert today_text != weekly_text
    assert "today" in calls
    assert "weekly" in calls


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
def test_session_card_progress_bar_color_thresholds(qtbot, pct, expected_color):
    """U8: bar chunk colour escalates green → yellow → red at 60% / 85%.
    The pct text is coloured to match so the signal reads either way."""
    su = _make_session_usage(quota=_make_quota(five_pct=pct))
    p = _panel_with_session(qtbot, lambda: su)
    p.refresh_usage_bar()
    assert expected_color in p._session_bar.styleSheet()
    assert expected_color in p._session_pct.styleSheet()


def test_session_card_stale_overrides_red(qtbot):
    """U9: stale data wins over the percent-based colour — we want
    "I don't trust this" to surface before "you're at the limit",
    so a stale 95% reads gray, not red."""
    su = _make_session_usage(quota=_make_quota(five_pct=95.0, is_stale=True))
    p = _panel_with_session(qtbot, lambda: su)
    p.refresh_usage_bar()
    assert "#6b7280" in p._session_bar.styleSheet()    # _BAR_STALE
    assert "#ef4444" not in p._session_bar.styleSheet()


def test_session_card_model_breakdown_shows_top_models(qtbot):
    """U7: by_model populated → first 3 entries shown joined with ' · '
    using friendly labels (Sonnet/Haiku/Opus); unknown ids truncated."""
    by_model = (
        _ModelTotals(model="claude-sonnet-4-5", input_tokens=0, output_tokens=0,
                     cache_creation_tokens=0, cache_read_tokens=0, cost_usd=2.54),
        _ModelTotals(model="claude-haiku-4-5", input_tokens=0, output_tokens=0,
                     cache_creation_tokens=0, cache_read_tokens=0, cost_usd=0.13),
    )
    su = _make_session_usage(by_model=by_model, total_cost=2.67, quota=None)
    p = _panel_with_session(qtbot, lambda: su)
    p.refresh_usage_bar()

    text = p._session_models.text()
    assert "Sonnet" in text
    assert "Haiku" in text
    assert "·" in text
