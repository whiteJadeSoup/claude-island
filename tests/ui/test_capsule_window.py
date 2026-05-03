"""Tests for CapsuleWindow's pill text composition + breathing dot.

Focus areas:
- Today-cost field appears next to the session count when wired and > 0
- Running-session-name display when exactly one session is active
- Breathing animation starts/stops in lockstep with active state
- The "●" lives in its own QLabel so it can pulse independently — text
  label intentionally NO LONGER carries the dot glyph
"""
from __future__ import annotations

import os
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from claude_island.core.models import Session, SessionDetails
from claude_island.ui.capsule_window import CapsuleWindow
from claude_island.ui.controller import IslandController


def _session(pid: int = 100, *, ago_seconds: int = 0) -> Session:
    """Session whose last_activity is ``ago_seconds`` in the past.

    Default ``ago_seconds=0`` means "active right now" — picks up the
    breathing animation by default. Pass a value ≥ 60 to mark idle."""
    return Session(
        pid=pid,
        project_path=Path("/tmp/proj"),
        session_uuid="",
        window_handle=None,
        last_activity=datetime.now(timezone.utc) - timedelta(seconds=ago_seconds),
    )


def _details(session: Session, *, name: str | None = None,
             ai_title: str | None = None) -> SessionDetails:
    return SessionDetails(
        session=session,
        name=name,
        ai_title=ai_title,
        git_branch=None,
        last_prompt=None,
        started_at=None,
        status=None,
        cc_version=None,
        cost_usd=0.0,
        turn_count=0,
        sidechain_count=0,
    )


@pytest.fixture
def controller_with_one_session():
    """Single session, idle (200 s old) — keeps breathing OFF by default
    so individual tests can flip activity on explicitly when they need
    to assert running-name behaviour."""
    controller = IslandController()
    controller.on_sessions_updated([_session(ago_seconds=200)])
    return controller


def test_text_omits_cost_when_no_getter(qtbot, controller_with_one_session):
    """Backwards-compat path — constructing without ``get_today_cost``
    must keep the bare count text. Dot is rendered in a separate label,
    so the text label itself does not carry the "●" glyph."""
    capsule = CapsuleWindow(controller_with_one_session)
    qtbot.addWidget(capsule)
    capsule._apply_capsule()  # force out of dot mode
    assert capsule._label.text() == "1 session"
    assert capsule._dot_label.text() == "●"


def test_text_omits_cost_when_zero(qtbot, controller_with_one_session):
    """Fresh first-launch case: cost getter exists but returns 0.0
    (no JSONL records yet). Suppressing $0 keeps the pill quiet rather
    than asserting "you've spent zero" which is technically true but
    visually noisy."""
    capsule = CapsuleWindow(
        controller_with_one_session,
        get_today_cost=lambda: 0.0,
    )
    qtbot.addWidget(capsule)
    capsule.refresh_cost()
    capsule._apply_capsule()
    assert capsule._label.text() == "1 session"


def test_text_includes_cost_when_positive(qtbot, controller_with_one_session):
    """Common case: today's spend > 0 → pill text becomes
    ``1 session  $86`` so the user can see the running total without
    expanding the panel."""
    capsule = CapsuleWindow(
        controller_with_one_session,
        get_today_cost=lambda: 86.42,
    )
    qtbot.addWidget(capsule)
    capsule.refresh_cost()
    capsule._apply_capsule()
    assert capsule._label.text() == "1 session  $86"


def test_refresh_cost_updates_text_in_place(qtbot, controller_with_one_session):
    """Backfill / live-write path: totals_changed fires, refresh_cost
    re-pulls the getter, label updates without going through
    _apply_capsule (which would recenter / reshow the window)."""
    cost_box = [9.99]  # < $10 keeps cents; _fmt_money switches at 10
    capsule = CapsuleWindow(
        controller_with_one_session,
        get_today_cost=lambda: cost_box[0],
    )
    qtbot.addWidget(capsule)
    capsule._apply_capsule()
    capsule.refresh_cost()
    assert capsule._label.text() == "1 session  $9.99"
    cost_box[0] = 250.0
    capsule.refresh_cost()
    assert capsule._label.text() == "1 session  $250"


def test_refresh_cost_swallows_getter_exception(
    qtbot, controller_with_one_session,
):
    """A getter failure (e.g. registry mid-rebuild) must not propagate
    — the cost is presentational; raising would crash the Qt event loop.
    Previous cached value should survive."""
    cost_box = [42.0]

    def flaky_getter() -> float:
        if cost_box[0] < 0:
            raise RuntimeError("simulated registry hiccup")
        return cost_box[0]

    capsule = CapsuleWindow(
        controller_with_one_session,
        get_today_cost=flaky_getter,
    )
    qtbot.addWidget(capsule)
    capsule._apply_capsule()
    capsule.refresh_cost()
    assert capsule._label.text() == "1 session  $42"
    cost_box[0] = -1  # next call will raise
    capsule.refresh_cost()  # must not raise
    # Cached value preserved → label unchanged.
    assert capsule._label.text() == "1 session  $42"


def test_refresh_cost_noop_when_hidden(qtbot, controller_with_one_session):
    """If user picked Hide-until-restart, we must not flip a hidden
    capsule's internal state — the next process restart is the sole
    way back, so refresh_cost should silently skip."""
    calls = [0]

    def counting_getter() -> float:
        calls[0] += 1
        return 5.0

    capsule = CapsuleWindow(
        controller_with_one_session,
        get_today_cost=counting_getter,
    )
    qtbot.addWidget(capsule)
    capsule._hidden_by_user = True
    capsule.refresh_cost()
    assert calls[0] == 0  # getter never invoked under hide


# --------------------------------------------------------------------------
# Single-running-session name display (P0.2)
# --------------------------------------------------------------------------


def test_text_uses_session_name_when_exactly_one_active(qtbot):
    """When exactly one session is active AND a details composer is
    wired, the pill should show that session's name in place of the
    impersonal "1 session" — same resolution order the panel uses
    (custom rename → ai_title → basename)."""
    sess = _session(ago_seconds=0)  # active now
    controller = IslandController()
    controller.on_sessions_updated([sess])
    capsule = CapsuleWindow(
        controller,
        get_today_cost=lambda: 12.0,
        get_session_details=lambda s: _details(s, name="frontend refactor"),
    )
    qtbot.addWidget(capsule)
    capsule._apply_capsule()
    capsule.refresh_cost()  # mirrors the bridge's totals_changed dispatch
    assert capsule._label.text() == "frontend refactor  $12"


def test_text_falls_back_to_ai_title_then_basename(qtbot):
    """Composer returns no custom name → fall through to ai_title.
    No ai_title either → fall through to project basename."""
    sess = _session(ago_seconds=0)
    controller = IslandController()
    controller.on_sessions_updated([sess])

    capsule = CapsuleWindow(
        controller,
        get_session_details=lambda s: _details(s, ai_title="Refactor auth flow"),
    )
    qtbot.addWidget(capsule)
    capsule._apply_capsule()
    assert capsule._label.text() == "Refactor auth flow"

    capsule_b = CapsuleWindow(
        controller,
        get_session_details=lambda s: _details(s),  # name + ai_title None
    )
    qtbot.addWidget(capsule_b)
    capsule_b._apply_capsule()
    # project_path is "/tmp/proj" → basename "proj"
    assert capsule_b._label.text() == "proj"


def test_text_falls_back_to_count_when_multiple_active(qtbot):
    """Two sessions active simultaneously → name display is ambiguous,
    so we fall back to the count format. Avoids cycling-name behaviour
    in P0; that's deferred to a later phase per the design plan."""
    s1 = _session(pid=1, ago_seconds=0)
    s2 = _session(pid=2, ago_seconds=0)
    controller = IslandController()
    controller.on_sessions_updated([s1, s2])
    capsule = CapsuleWindow(
        controller,
        get_session_details=lambda s: _details(s, name="ignored-because-multi"),
    )
    qtbot.addWidget(capsule)
    capsule._apply_capsule()
    assert capsule._label.text() == "2 sessions"


def test_text_falls_back_to_count_when_idle(qtbot):
    """Single session but it's gone idle (last_activity > 30 s ago).
    The pill should NOT show the name — name display is reserved for
    "actually doing something right now" so the user can spot bursts."""
    sess = _session(ago_seconds=120)  # idle
    controller = IslandController()
    controller.on_sessions_updated([sess])
    capsule = CapsuleWindow(
        controller,
        get_session_details=lambda s: _details(s, name="not-shown-when-idle"),
    )
    qtbot.addWidget(capsule)
    capsule._apply_capsule()
    assert capsule._label.text() == "1 session"


def test_session_name_resolution_swallows_composer_exception(qtbot):
    """A throwing details composer must degrade to the count format
    rather than break the pill — same rule as the cost getter."""
    sess = _session(ago_seconds=0)
    controller = IslandController()
    controller.on_sessions_updated([sess])

    def broken(_s):
        raise RuntimeError("composer is having a moment")

    capsule = CapsuleWindow(controller, get_session_details=broken)
    qtbot.addWidget(capsule)
    capsule._apply_capsule()  # must not raise
    assert capsule._label.text() == "1 session"


# --------------------------------------------------------------------------
# Breathing animation lifecycle (P0.2)
# --------------------------------------------------------------------------


def test_breathing_starts_when_any_session_active(qtbot):
    """Active session present → animation running, dot styled green.
    Idle baseline already covered indirectly elsewhere; this is the
    positive-side assertion."""
    sess = _session(ago_seconds=0)
    controller = IslandController()
    controller.on_sessions_updated([sess])
    capsule = CapsuleWindow(controller)
    qtbot.addWidget(capsule)
    capsule._apply_capsule()
    assert capsule._is_breathing is True
    # Stylesheet flipped to green — substring check rather than exact
    # equality so future colour tweaks don't break the test for the
    # wrong reason.
    assert "4ade80" in capsule._dot_label.styleSheet()


def test_breathing_stops_when_no_session_active(qtbot):
    """All sessions idle → animation stopped, dot styled neutral, and
    opacity snapped back to 1.0 so the dot doesn't get stranded mid-cycle."""
    sess = _session(ago_seconds=200)
    controller = IslandController()
    controller.on_sessions_updated([sess])
    capsule = CapsuleWindow(controller)
    qtbot.addWidget(capsule)
    capsule._apply_capsule()
    assert capsule._is_breathing is False
    assert capsule._dot_opacity.opacity() == pytest.approx(1.0)
    assert "6b7280" in capsule._dot_label.styleSheet()


def test_breathing_transitions_on_session_activity(qtbot):
    """Idle → active transition (a JSONL write lands) ⇒ breathing
    starts. The reverse (active → idle) ⇒ breathing stops. Simulated
    by mutating the controller's session list and re-driving the
    refresh path the bridge would normally trigger."""
    idle = _session(ago_seconds=200)
    controller = IslandController()
    controller.on_sessions_updated([idle])
    capsule = CapsuleWindow(controller)
    qtbot.addWidget(capsule)
    capsule._apply_capsule()
    assert capsule._is_breathing is False

    # Same pid, fresh activity timestamp — what session_registry would
    # publish after JSONL parser update_activity → next process scan.
    controller.on_sessions_updated([replace(idle, last_activity=datetime.now(timezone.utc))])
    capsule.refresh_sessions(None)
    assert capsule._is_breathing is True

    # Back to idle — timestamp older than threshold.
    controller.on_sessions_updated([replace(idle, last_activity=datetime.now(timezone.utc) - timedelta(seconds=200))])
    capsule.refresh_sessions(None)
    assert capsule._is_breathing is False
    assert capsule._dot_opacity.opacity() == pytest.approx(1.0)


def test_dot_label_hidden_in_dot_mode(qtbot, controller_with_one_session):
    """When the controller drops to the "no sessions" dot mode, the
    pill collapses to a 12 px round and BOTH labels must hide — leaving
    the "●" on screen would draw a glyph next to the painted dot."""
    capsule = CapsuleWindow(controller_with_one_session)
    qtbot.addWidget(capsule)
    capsule._apply_capsule()
    capsule._apply_dot()
    assert capsule._dot_label.isHidden()
    assert capsule._label.isHidden()
    assert capsule._is_breathing is False
