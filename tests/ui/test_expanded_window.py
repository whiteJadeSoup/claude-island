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


def _session(pid: int, cwd: str, ago_minutes: int = 0) -> Session:
    return Session(
        pid=pid,
        project_path=Path(cwd),
        session_uuid="",
        window_handle=None,
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
    panel.refresh_sessions([_session(1, "/a", ago_minutes=0)])
    btn = panel._rows[1]
    text_before = btn.text()

    # Same pid, same cwd, but newer activity timestamp would shift the "ago" label.
    panel.refresh_sessions([_session(1, "/a", ago_minutes=5)])
    assert panel._rows[1] is btn  # not recreated
    assert btn.text() != text_before  # but text changed in place


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

    received = []
    panel.session_activated.connect(received.append)
    btn.click()

    assert len(received) == 1
    assert received[0].last_activity == fresh.last_activity
