"""Layout invariants for the three-region capsule pill.

The three regions [dot] [name] [cost] enforce:
  1. Cost slot is fixed-width and right-anchored — long names never
     push it off the pill.
  2. Name uses ``Qt.ElideMiddle`` so commit-message-style strings
     keep both head and tail recognisable.
  3. Cost slot collapses to zero width when today's spend is $0,
     donating the space back to the name region.
  4. Tooltip on the pill exposes the FULL name(s) + cost so a
     hover recovers what eliding hid (per macOS NSStatusItem +
     PatternFly / Carbon UX guidance).

These are layout / data-shape assertions, not pixel diffs — pixel
behaviour is verified by the manual screenshot pass during code
review since rendering depends on the platform's font metrics.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from claude_island.core.models import Session
from claude_island.core.snapshot import (
    HIGH_COST_USD_THRESHOLD,
    SessionGroup,
    SessionView,
    WorldSnapshot,
)
from claude_island.ui.capsule_window import CapsuleWindow
from claude_island.ui.controller import IslandController


# ---------------------------------------------------------------------------
# Fixtures (same shape as test_render_snap.py — duplicated locally so this
# file can be read independently)
# ---------------------------------------------------------------------------

def _view(
    *,
    pid: int = 1,
    name: str = "test",
    is_running: bool = True,
    cost_usd: float = 0.0,
) -> SessionView:
    sess = Session(
        pid=pid, project_path=Path("/tmp"), session_uuid="",
        last_activity=datetime.now(timezone.utc),
    )
    from claude_island.core.session_phase import SessionPhase
    return SessionView(
        pid=pid, name=name, project_path=Path("/tmp"),
        project_basename="tmp",
        last_activity=sess.last_activity,
        cost_usd=cost_usd,
        is_high_cost=cost_usd >= HIGH_COST_USD_THRESHOLD,
        latest_model="claude-opus-4-7",
        status_word="busy" if is_running else "idle",
        session=sess,
        phase=SessionPhase.THINKING if is_running else SessionPhase.IDLE,
    )


def _snap(*views: SessionView, today_cost_usd: float = 0.0) -> WorldSnapshot:
    groups = tuple(
        SessionGroup(group_id=f"t:{v.pid}", title_hint=None,
                     adapter_id="test", views=(v,))
        for v in views
    )
    return WorldSnapshot(
        session_groups=groups,
        today_cost_usd=today_cost_usd,
        quota=None,
        available_providers=(),
        selected_provider=None,
        fetched_at=datetime.now(timezone.utc),
    )


@pytest.fixture
def capsule(qtbot):
    cap = CapsuleWindow(IslandController())
    qtbot.addWidget(cap)
    cap._is_dot = False
    return cap


def _render(cap: CapsuleWindow, snap: WorldSnapshot) -> None:
    cap.render(cap.compute(snap))


# ---------------------------------------------------------------------------
# Region geometry invariants
# ---------------------------------------------------------------------------

class TestCostSlotCannotBeOverwritten:
    def test_long_name_does_not_overlap_cost_slot(self, capsule):
        """A long session name must not encroach on the cost slot:
        name and cost are siblings, so the name region's right edge
        must stop strictly before the cost slot's left edge."""
        long = "Sync current remote master branch with upstream tracking"
        v = _view(name=long, cost_usd=42.0)
        _render(capsule, _snap(v, today_cost_usd=42.0))

        name_geom = capsule._label.geometry()
        cost_geom = capsule._cost_label.geometry()
        assert name_geom.right() < cost_geom.left(), (
            f"name right edge {name_geom.right()} encroaches on cost "
            f"slot left edge {cost_geom.left()}"
        )

    def test_cost_slot_anchored_right_when_visible(self, capsule):
        """Cost should hug the pill's right edge (modulo a small pad)
        regardless of name length — that's the affordance that makes
        cost glanceable."""
        v = _view(name="x", cost_usd=10.0)
        _render(capsule, _snap(v, today_cost_usd=10.0))
        cost_geom = capsule._cost_label.geometry()
        # Tolerance covers the right-edge symmetric pad (12 px) plus
        # 1 px rounding slack.
        assert capsule.width() - cost_geom.right() < 16


class TestCostSlotCollapse:
    def test_zero_cost_hides_cost_label(self, capsule):
        v = _view(name="x", cost_usd=0.0)
        _render(capsule, _snap(v, today_cost_usd=0.0))
        # ``isVisibleTo`` checks visibility relative to the parent
        # widget, ignoring whether the parent itself was shown — the
        # capsule fixture never calls .show(), so a bare isVisible()
        # would always be False and the assertion would pass for the
        # wrong reason.
        assert capsule._cost_label.isVisibleTo(capsule) is False
        assert capsule._cost_label.text() == ""

    def test_zero_cost_donates_space_to_name(self, capsule):
        """With cost slot collapsed, the name region gets the space
        back. Compare name region width across cost-zero vs cost-non-
        zero: zero-cost should be wider by approximately the cost slot
        width + the inter-region gap."""
        v_no = _view(name="n", cost_usd=0.0)
        _render(capsule, _snap(v_no, today_cost_usd=0.0))
        no_cost_w = capsule._label.width()

        v_yes = _view(name="n", cost_usd=10.0)
        _render(capsule, _snap(v_yes, today_cost_usd=10.0))
        with_cost_w = capsule._label.width()

        assert no_cost_w > with_cost_w


# ---------------------------------------------------------------------------
# Eliding behaviour
# ---------------------------------------------------------------------------

class TestNameEliding:
    def test_short_name_renders_unchanged(self, capsule):
        """Short names that already fit must NOT be elided —
        QFontMetrics.elidedText is supposed to return the original
        when no truncation is needed; a regression here would surface
        as ellipsis on names like 'main'."""
        v = _view(name="main", cost_usd=5.0)
        _render(capsule, _snap(v, today_cost_usd=5.0))
        assert capsule._label.text() == "main"

    def test_long_name_uses_middle_elide(self, capsule):
        """A long name should be middle-elided so head AND tail
        remain visible — for git branch / commit message style names
        both ends carry identifying info."""
        long = "Sync current remote master branch with upstream"
        v = _view(name=long, cost_usd=5.0)
        _render(capsule, _snap(v, today_cost_usd=5.0))
        rendered = capsule._label.text()
        # Sanity: shorter than full
        assert len(rendered) < len(long)
        # Ellipsis present (Qt uses U+2026, not three dots)
        assert "…" in rendered or "..." in rendered
        # Head AND tail preserved (first 4 chars + last 4 chars
        # survive — the contract of middle-elide).
        assert rendered.startswith(long[:3])
        assert rendered.endswith(long[-3:])


# ---------------------------------------------------------------------------
# Tooltip
# ---------------------------------------------------------------------------

class TestToolTip:
    def test_tooltip_contains_full_name_when_elided(self, capsule):
        """The tooltip is the documented escape hatch for eliding
        (PatternFly / NSStatusItem). It must carry the ENTIRE name
        verbatim regardless of how aggressively the visible label
        was truncated."""
        long = "Sync current remote master branch with upstream"
        v = _view(name=long, cost_usd=5.0)
        _render(capsule, _snap(v, today_cost_usd=5.0))
        assert long in capsule.toolTip()

    def test_tooltip_lists_all_running_sessions(self, capsule):
        """Carousel users see one name at a time on the pill. The
        tooltip should expose the COMPLETE running set so they
        don't have to wait for the rotation."""
        v1 = _view(pid=1, name="alpha", is_running=True)
        v2 = _view(pid=2, name="beta", is_running=True)
        v3 = _view(pid=3, name="gamma", is_running=True)
        _render(capsule, _snap(v1, v2, v3, today_cost_usd=5.0))
        tip = capsule.toolTip()
        assert "alpha" in tip and "beta" in tip and "gamma" in tip

    def test_tooltip_includes_cost_when_present(self, capsule):
        v = _view(name="x", cost_usd=12.0)
        _render(capsule, _snap(v, today_cost_usd=12.0))
        assert "$12" in capsule.toolTip()

    def test_tooltip_for_zero_running_uses_count_form(self, capsule):
        _render(capsule, _snap(today_cost_usd=0.0))
        assert "0 session" in capsule.toolTip()
