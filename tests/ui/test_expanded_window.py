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
