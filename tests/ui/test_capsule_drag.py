"""Tests for CapsuleWindow PR1: horizontal drag along the top edge
plus X-coordinate persistence.

Strategy: drive Qt mouse events directly via QTest so we bypass the
real OS pointer (works headless under offscreen platform). Patch
WINDOW_POSITION_PATH at the module attribute so saves go to a tmp
dir per test.

These tests pin down the click-vs-drag discrimination — a small
mouse twitch must NOT reposition the window AND must still toggle
the panel; a real drag must reposition AND must NOT toggle.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

# Force offscreen for headless CI / local runs (mirrors test_expanded_window).
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPoint, Qt
from PySide6.QtGui import QMouseEvent
from PySide6.QtWidgets import QApplication

from claude_island.ui import window_position
from claude_island.ui.capsule_window import CapsuleWindow
from claude_island.ui.controller import IslandController


# ── Fixtures ──────────────────────────────────────────────────────────

@pytest.fixture
def tmp_position_file(tmp_path, monkeypatch):
    """Redirect window.json saves to a per-test tmp file. Yields the
    Path so the test can read what was written."""
    target = tmp_path / "window.json"
    monkeypatch.setattr(window_position, "WINDOW_POSITION_PATH", target)
    yield target


@pytest.fixture
def capsule(qtbot, tmp_position_file):
    """Construct a CapsuleWindow with no real wiring — controller only.
    Persisted position is loaded from the patched window.json (which
    starts absent, so every fresh capsule begins centred)."""
    controller = IslandController()
    cap = CapsuleWindow(controller)
    qtbot.addWidget(cap)
    yield cap


# ── Drag-vs-click discrimination ──────────────────────────────────────

def _press(widget, *, global_pos: QPoint) -> None:
    """Synthesise a left-button press at ``global_pos``. Both local
    and global positions need to be supplied to QMouseEvent — local
    is computed from globalPosition by mapping back."""
    local = widget.mapFromGlobal(global_pos)
    ev = QMouseEvent(
        QMouseEvent.Type.MouseButtonPress,
        local.toPointF() if hasattr(local, "toPointF") else local,
        global_pos.toPointF() if hasattr(global_pos, "toPointF") else global_pos,
        Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
    )
    QApplication.sendEvent(widget, ev)


def _move(widget, *, global_pos: QPoint) -> None:
    local = widget.mapFromGlobal(global_pos)
    ev = QMouseEvent(
        QMouseEvent.Type.MouseMove,
        local.toPointF() if hasattr(local, "toPointF") else local,
        global_pos.toPointF() if hasattr(global_pos, "toPointF") else global_pos,
        Qt.MouseButton.NoButton, Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
    )
    QApplication.sendEvent(widget, ev)


def _release(widget, *, global_pos: QPoint) -> None:
    local = widget.mapFromGlobal(global_pos)
    ev = QMouseEvent(
        QMouseEvent.Type.MouseButtonRelease,
        local.toPointF() if hasattr(local, "toPointF") else local,
        global_pos.toPointF() if hasattr(global_pos, "toPointF") else global_pos,
        Qt.MouseButton.LeftButton, Qt.MouseButton.NoButton,
        Qt.KeyboardModifier.NoModifier,
    )
    QApplication.sendEvent(widget, ev)


def test_small_motion_treated_as_click_toggles_panel(capsule, monkeypatch):
    """Press → move 2 px → release → controller.toggle_expanded must
    have been invoked (small motion is below
    QApplication.startDragDistance and does not promote to a drag).

    Spies on toggle_expanded directly because the controller's state
    machine no-ops a toggle from 'dot' state (no active sessions),
    which would mask the click signal in this test."""
    calls: list[None] = []
    monkeypatch.setattr(
        capsule._controller, "toggle_expanded",
        lambda: calls.append(None),
    )
    initial_pos = capsule.pos()

    origin = QPoint(initial_pos.x() + 50, initial_pos.y() + 10)
    _press(capsule, global_pos=origin)
    _move(capsule, global_pos=origin + QPoint(2, 0))
    _release(capsule, global_pos=origin + QPoint(2, 0))

    # Window must NOT have moved.
    assert capsule.pos() == initial_pos
    # And the click reached the controller.
    assert len(calls) == 1


def test_large_horizontal_motion_repositions_capsule(capsule, monkeypatch):
    """Press → move past the drag-distance threshold → release. The
    window's X must change; Y must stay locked (PR1 horizontal-only).
    toggle_expanded must NOT be invoked (this is a drag, not a click)."""
    calls: list[None] = []
    monkeypatch.setattr(
        capsule._controller, "toggle_expanded",
        lambda: calls.append(None),
    )
    initial_pos = capsule.pos()

    # Pick a delta well above any reasonable startDragDistance.
    drag_dx = QApplication.startDragDistance() + 50

    origin = QPoint(initial_pos.x() + 50, initial_pos.y() + 10)
    _press(capsule, global_pos=origin)
    _move(capsule, global_pos=origin + QPoint(drag_dx, 0))
    _release(capsule, global_pos=origin + QPoint(drag_dx, 0))

    new_pos = capsule.pos()
    # X moved by drag_dx (modulo clamp), Y unchanged.
    assert new_pos.x() != initial_pos.x()
    assert new_pos.y() == initial_pos.y()
    # No toggle on a real drag — the press/release pair was for moving.
    assert calls == []


def test_drag_persists_position_to_disk(capsule, tmp_position_file):
    """After a drag, window.json must contain the new (x, y)."""
    initial_pos = capsule.pos()
    drag_dx = QApplication.startDragDistance() + 50

    origin = QPoint(initial_pos.x() + 50, initial_pos.y() + 10)
    _press(capsule, global_pos=origin)
    _move(capsule, global_pos=origin + QPoint(drag_dx, 0))
    _release(capsule, global_pos=origin + QPoint(drag_dx, 0))

    assert tmp_position_file.exists(), "window.json should be created on drag"
    data = json.loads(tmp_position_file.read_text(encoding="utf-8"))
    assert data["x"] == capsule.pos().x()
    assert data["y"] == capsule.pos().y()


def test_click_does_not_persist_position(capsule, tmp_position_file):
    """A pure click (no move past threshold) must NOT touch window.json."""
    initial_pos = capsule.pos()
    origin = QPoint(initial_pos.x() + 50, initial_pos.y() + 10)
    _press(capsule, global_pos=origin)
    _release(capsule, global_pos=origin)

    assert not tmp_position_file.exists(), (
        "window.json must not be written for a pure click"
    )


# ── Persistence load path ─────────────────────────────────────────────

def test_saved_position_restored_on_construction(qtbot, tmp_position_file):
    """A previously-saved (x, y) must be applied to the new capsule
    instance instead of the default centred position."""
    # Pre-seed the position file with a value that's deliberately not
    # the default centre.
    saved_x, saved_y = 100, 8
    tmp_position_file.parent.mkdir(parents=True, exist_ok=True)
    tmp_position_file.write_text(
        json.dumps({"x": saved_x, "y": saved_y}), encoding="utf-8",
    )

    controller = IslandController()
    cap = CapsuleWindow(controller)
    qtbot.addWidget(cap)

    # The capsule starts in dot mode, so it took the dot-sized branch
    # of _center_top — but the persisted x must have been honoured
    # (clamped to the dot width).
    pos = cap.pos()
    assert pos.x() == saved_x
    assert pos.y() == saved_y


def test_corrupted_position_file_falls_back_to_default(
    qtbot, tmp_position_file,
):
    """A malformed window.json must NOT crash construction — capsule
    silently ignores it and uses the default centred position."""
    tmp_position_file.parent.mkdir(parents=True, exist_ok=True)
    tmp_position_file.write_text("{not valid json", encoding="utf-8")

    controller = IslandController()
    cap = CapsuleWindow(controller)
    qtbot.addWidget(cap)

    # Did not crash. Position took the default branch.
    assert cap._persisted_pos is None


def test_off_screen_persisted_position_falls_back_to_centre(
    qtbot, tmp_position_file,
):
    """If the persisted (x, y) lands entirely off-screen (e.g. saved
    on a now-disconnected monitor), construction falls back to the
    primary-screen-top-centre default."""
    # Pick coordinates guaranteed not to overlap any real screen by
    # an order of magnitude.
    tmp_position_file.parent.mkdir(parents=True, exist_ok=True)
    tmp_position_file.write_text(
        json.dumps({"x": -99999, "y": -99999}), encoding="utf-8",
    )

    controller = IslandController()
    cap = CapsuleWindow(controller)
    qtbot.addWidget(cap)

    # _persisted_pos was loaded but rejected by visibility check, so
    # the geometry must reflect the centred default.
    primary = QApplication.primaryScreen().geometry()
    assert primary.contains(cap.pos())


# ── Multi-screen clamp ────────────────────────────────────────────────

# ── PR2: long-press → free drag → edge snap ───────────────────────────

class TestFreeDrag:
    """Long-press unlocks 2D drag + 4-edge snap on release.

    Bypasses the long-press timer wait by directly invoking
    ``_on_long_press`` after a press — this is what the timer
    callback would do, and avoids 500 ms of real time per test."""

    def _press_and_promote(self, capsule, *, origin: QPoint) -> None:
        """Helper: synthesise a press, then immediately fire the long-
        press callback to enter free-drag mode."""
        _press(capsule, global_pos=origin)
        capsule._on_long_press()

    def test_long_press_enters_free_drag_with_visual_cue(self, capsule):
        origin = QPoint(capsule.x() + 50, capsule.y() + 10)
        self._press_and_promote(capsule, origin=origin)

        assert capsule._is_free_drag is True
        # Qt quantises windowOpacity to 8-bit (1/256 ≈ 0.004 step).
        assert capsule.windowOpacity() == pytest.approx(0.7, abs=0.01)

    def test_free_drag_unlocks_y_axis(self, capsule):
        """In free-drag mode mouseMove updates Y too — proves the
        PR1 horizontal lock is bypassed."""
        initial = capsule.pos()
        origin = QPoint(initial.x() + 50, initial.y() + 10)
        self._press_and_promote(capsule, origin=origin)

        # Drag down by 100 px (and right by 50). Both axes should move.
        target = origin + QPoint(50, 100)
        _move(capsule, global_pos=target)

        new = capsule.pos()
        assert new.x() != initial.x()
        assert new.y() != initial.y()
        assert new.y() == initial.y() + 100

    def test_movement_before_long_press_stays_horizontal(self, capsule):
        """If the user moves the cursor BEFORE the long-press fires
        (i.e. they wanted a quick horizontal nudge), the timer is
        cancelled and Y stays locked. Confirms the race resolves to
        horizontal-drag, not free-drag."""
        initial = capsule.pos()
        origin = QPoint(initial.x() + 50, initial.y() + 10)
        _press(capsule, global_pos=origin)
        # Move past startDragDistance immediately — promotes to
        # horizontal drag, cancels the long-press timer.
        far = origin + QPoint(QApplication.startDragDistance() + 30, 50)
        _move(capsule, global_pos=far)

        # Y should NOT have changed despite the +50 cursor delta.
        assert capsule.pos().y() == initial.y()
        # And the long-press timer was cancelled.
        assert not capsule._long_press_timer.isActive()
        # Free-drag flag stayed off.
        assert capsule._is_free_drag is False

    def test_release_after_free_drag_snaps_to_nearest_edge(
        self, capsule, monkeypatch,
    ):
        """Release after free-drag must call _snap_to_nearest_edge
        (which animates) instead of saving the raw release position."""
        snap_calls: list[None] = []
        monkeypatch.setattr(
            capsule, "_snap_to_nearest_edge",
            lambda: snap_calls.append(None),
        )
        origin = QPoint(capsule.x() + 50, capsule.y() + 10)
        self._press_and_promote(capsule, origin=origin)
        _move(capsule, global_pos=origin + QPoint(80, 80))
        _release(capsule, global_pos=origin + QPoint(80, 80))

        assert len(snap_calls) == 1
        # Opacity must be restored — the visual cue was a transient
        # signal, not a persistent state.
        assert capsule.windowOpacity() == pytest.approx(1.0)

    def test_snap_picks_nearest_edge(self, qtbot, tmp_position_file):
        """Place capsule near the bottom edge; snap should land it on
        the bottom edge (not top/left/right)."""
        controller = IslandController()
        cap = CapsuleWindow(controller)
        qtbot.addWidget(cap)
        screen = QApplication.primaryScreen().geometry()

        # Position near the bottom edge so dist_bottom is the min.
        target_y = screen.bottom() - cap.height() - 40
        target_x = screen.left() + screen.width() // 2  # mid-x
        cap.move(target_x, target_y)

        cap._snap_to_nearest_edge()
        # Animation runs over ~200 ms; force-finish synchronously.
        if cap._snap_anim is not None:
            cap._snap_anim.setCurrentTime(cap._snap_anim.duration())

        # Should have landed near the bottom edge (within a margin
        # for the spring overshoot rounding).
        landed_y = cap.y()
        expected_y = screen.bottom() - cap.height() - 8  # _TOP_MARGIN
        assert abs(landed_y - expected_y) <= 2

    def test_snap_persists_after_animation_finishes(
        self, qtbot, tmp_position_file,
    ):
        """The snapped position must be written to window.json once
        the animation completes (not the mid-air release point)."""
        controller = IslandController()
        cap = CapsuleWindow(controller)
        qtbot.addWidget(cap)
        screen = QApplication.primaryScreen().geometry()
        cap.move(
            screen.left() + screen.width() // 2,
            screen.top() + screen.height() - 40 - cap.height(),
        )

        cap._snap_to_nearest_edge()
        if cap._snap_anim is not None:
            cap._snap_anim.setCurrentTime(cap._snap_anim.duration())
        cap._on_snap_finished()  # belt-and-braces — emit may race

        assert tmp_position_file.exists()
        data = json.loads(tmp_position_file.read_text(encoding="utf-8"))
        assert data["x"] == cap.pos().x()
        assert data["y"] == cap.pos().y()


# ── PR3: edge-idle half-hide + hover restore + reset to home ──────────

class TestEdgeIdle:
    """Idle half-hide kicks in when the capsule snaps to a non-top
    edge. enterEvent restores; leaveEvent collapses; right-click
    "Reset position" returns to home."""

    def _force_apply_capsule(self, cap):
        """Move the capsule out of dot mode so PR3 idle paths apply
        (idle is a no-op while the capsule is in dot form)."""
        cap._is_dot = False

    def test_snap_to_top_edge_does_not_enter_idle(self, qtbot, tmp_position_file):
        """Top edge is the home position — must NOT collapse to idle.
        Verifies the _IDLE_EDGES filter excludes top."""
        controller = IslandController()
        cap = CapsuleWindow(controller)
        qtbot.addWidget(cap)
        self._force_apply_capsule(cap)
        screen = QApplication.primaryScreen().geometry()

        # Place near the top so snap picks "top".
        cap.move(screen.left() + screen.width() // 2, screen.top() + 30)
        cap._snap_to_nearest_edge()
        if cap._snap_anim is not None:
            cap._snap_anim.setCurrentTime(cap._snap_anim.duration())
        cap._on_snap_finished()

        assert cap._docked_edge == "top"
        assert cap._is_idle is False

    def test_snap_to_bottom_edge_enters_idle(self, qtbot, tmp_position_file):
        controller = IslandController()
        cap = CapsuleWindow(controller)
        qtbot.addWidget(cap)
        self._force_apply_capsule(cap)
        screen = QApplication.primaryScreen().geometry()

        # Place near bottom so snap picks "bottom".
        cap.move(
            screen.left() + screen.width() // 2,
            screen.bottom() - cap.height() - 30,
        )
        cap._snap_to_nearest_edge()
        if cap._snap_anim is not None:
            cap._snap_anim.setCurrentTime(cap._snap_anim.duration())
        cap._on_snap_finished()

        assert cap._docked_edge == "bottom"
        assert cap._is_idle is True
        # Width collapsed to the idle strip.
        from claude_island.ui.capsule_window import _IDLE_W, _IDLE_OPACITY
        assert cap.width() == _IDLE_W
        assert cap.windowOpacity() == pytest.approx(_IDLE_OPACITY, abs=0.01)

    def test_enter_event_exits_idle(self, qtbot, tmp_position_file):
        """Hovering over an idle capsule restores its full size and
        opacity, mirroring AssistiveTouch hover-out."""
        controller = IslandController()
        cap = CapsuleWindow(controller)
        qtbot.addWidget(cap)
        self._force_apply_capsule(cap)
        cap._docked_edge = "bottom"
        cap._enter_idle()
        assert cap._is_idle is True

        # Synthesise an enterEvent.
        from PySide6.QtGui import QEnterEvent
        from PySide6.QtCore import QPointF
        enter = QEnterEvent(QPointF(10, 10), QPointF(10, 10), QPointF(10, 10))
        cap.enterEvent(enter)

        assert cap._is_idle is False
        assert cap.windowOpacity() == pytest.approx(1.0, abs=0.01)

    def test_leave_event_re_enters_idle(self, qtbot, tmp_position_file):
        """After hover-out the capsule fades back to idle on
        leaveEvent — completes the AssistiveTouch hide loop."""
        controller = IslandController()
        cap = CapsuleWindow(controller)
        qtbot.addWidget(cap)
        self._force_apply_capsule(cap)
        cap._docked_edge = "bottom"
        cap._enter_idle()
        cap._exit_idle()
        assert cap._is_idle is False

        from PySide6.QtCore import QEvent
        leave = QEvent(QEvent.Type.Leave)
        cap.leaveEvent(leave)

        assert cap._is_idle is True

    def test_leave_during_drag_does_not_enter_idle(self, qtbot, tmp_position_file):
        """If the user is mid-drag, leaveEvent must NOT collapse to
        idle (would visually conflict with the drag-tracking pill).
        Drag origin not None ⇒ press is still active."""
        controller = IslandController()
        cap = CapsuleWindow(controller)
        qtbot.addWidget(cap)
        self._force_apply_capsule(cap)
        cap._docked_edge = "bottom"
        # Simulate active drag: drag_origin populated.
        cap._drag_origin_global = QPoint(0, 0)
        cap._drag_origin_window = QPoint(0, 0)

        from PySide6.QtCore import QEvent
        cap.leaveEvent(QEvent(QEvent.Type.Leave))

        assert cap._is_idle is False

    def test_reset_position_returns_to_home(self, qtbot, tmp_position_file):
        """_go_home must clear docked_edge, persisted_pos, idle, and
        re-centre. Used by the right-click "Reset position" menu."""
        controller = IslandController()
        cap = CapsuleWindow(controller)
        qtbot.addWidget(cap)
        self._force_apply_capsule(cap)
        cap._docked_edge = "right"
        cap._is_idle = True
        cap._persisted_pos = (1234, 567)

        cap._go_home()

        assert cap._docked_edge is None
        assert cap._is_idle is False
        assert cap._persisted_pos is None
        # Position is now centred on primary screen.
        primary = QApplication.primaryScreen().geometry()
        assert primary.contains(cap.pos())

    def test_apply_capsule_in_idle_does_not_resize(self, qtbot, tmp_position_file):
        """A render(snap) tick during idle must NOT bounce the capsule
        back to full _CAPSULE_W. Catches the bug where _apply_capsule
        unconditionally calls _center_top."""
        from claude_island.ui.capsule_window import _IDLE_W
        controller = IslandController()
        cap = CapsuleWindow(controller)
        qtbot.addWidget(cap)
        self._force_apply_capsule(cap)
        cap._docked_edge = "bottom"
        cap._enter_idle()

        # Trigger _apply_capsule (what render(snap) does).
        cap._apply_capsule()

        # Width must still be the idle strip size.
        assert cap.width() == _IDLE_W
        assert cap._is_idle is True


# ── Multi-screen Y tracking (heterogeneous-height monitors) ───────────

class _FakeScreen:
    """Stand-in for QScreen that returns a fixed geometry. Used to
    simulate a multi-monitor layout in unit tests without needing
    real hardware. Only ``geometry()`` is exercised by the helpers
    we test — we keep the surface intentionally tiny."""
    def __init__(self, x: int, y: int, w: int, h: int):
        from PySide6.QtCore import QRect
        self._geom = QRect(x, y, w, h)

    def geometry(self):
        return self._geom


@pytest.fixture
def two_screens(monkeypatch):
    """Patch QApplication.screens / primaryScreen to return two
    monitors of different heights and different ``top()`` values:

      A: 1920×1080 with top=0  (typical laptop)
      B: 3840×2160 with top=-540, immediately to the right of A
         (taller external monitor, vertically aligned to A's centre).

    Yields the (A, B) pair."""
    a = _FakeScreen(0, 0, 1920, 1080)
    b = _FakeScreen(1920, -540, 3840, 2160)
    monkeypatch.setattr(QApplication, "screens", lambda: [a, b])
    monkeypatch.setattr(QApplication, "primaryScreen", lambda: a)
    yield a, b


def test_top_y_for_x_picks_each_screens_top(capsule, two_screens):
    """The helper must return each screen's actual top + _TOP_MARGIN
    based on which screen the centre_x lands on. This is the core
    of the multi-monitor drag fix."""
    from claude_island.ui.capsule_window import _TOP_MARGIN
    a, b = two_screens
    # X inside screen A → A's top + margin.
    assert capsule._top_y_for_x(500) == 0 + _TOP_MARGIN
    # X inside screen B → B's top + margin (NOT A's).
    assert capsule._top_y_for_x(2500) == -540 + _TOP_MARGIN


def test_top_y_for_x_falls_back_to_primary_in_desktop_gap(
    capsule, monkeypatch,
):
    """If centre_x lands in a desktop gap (mismatched-height monitors
    arranged so part of the X union is one-screen-only), the helper
    falls back to primary's top — keeps the capsule visible."""
    from claude_island.ui.capsule_window import _TOP_MARGIN
    primary = _FakeScreen(0, 0, 1920, 1080)
    monkeypatch.setattr(QApplication, "screens", lambda: [primary])
    monkeypatch.setattr(QApplication, "primaryScreen", lambda: primary)

    # X way outside the only screen's range.
    assert capsule._top_y_for_x(99999) == 0 + _TOP_MARGIN


def test_horizontal_drag_to_taller_screen_lands_on_its_top(
    capsule, two_screens, tmp_position_file,
):
    """Bug repro: previously horizontal drag locked Y at the origin
    screen's top (Y=8). Dragging from screen A (top=0) to screen B
    (top=-540) left the capsule at Y=8 — visible mid-air on B,
    540 px below B's actual top edge.

    Fix: each mouseMoveEvent recomputes Y for whichever screen the
    centre lands on. Drag from A to B must end with Y = -540 + 8.
    """
    from claude_island.ui.capsule_window import _TOP_MARGIN

    # Place the capsule on screen A first so the press / origin
    # come from there.
    capsule.move(500, _TOP_MARGIN)  # X=500, well inside A
    initial_pos = capsule.pos()

    # Drag the capsule far enough right that its centre lands inside
    # screen B (B starts at x=1920). Origin x=500, capsule width
    # ~200, centre starts at ~600. Push centre to 2500 (well into B)
    # by moving cursor by +1900.
    drag_dx = 1900
    origin = QPoint(initial_pos.x() + 50, initial_pos.y() + 10)
    _press(capsule, global_pos=origin)
    _move(capsule, global_pos=origin + QPoint(drag_dx, 0))
    _release(capsule, global_pos=origin + QPoint(drag_dx, 0))

    # Capsule landed on screen B (centre x in B's range) and Y is
    # now B's top + margin, NOT A's top + margin.
    centre_x = capsule.x() + capsule.width() // 2
    assert centre_x >= 1920, f"capsule didn't reach screen B (centre_x={centre_x})"
    assert capsule.y() == -540 + _TOP_MARGIN, (
        f"capsule should track screen B's top edge, got y={capsule.y()}"
    )


def test_clamp_keeps_capsule_within_screen_union(capsule):
    """_clamp_x must never let the capsule's left edge slide past
    the leftmost screen edge or its right edge past the rightmost."""
    screens = QApplication.screens()
    leftmost = min(s.geometry().left() for s in screens)
    rightmost = max(s.geometry().right() for s in screens)
    w = capsule.width()

    # Way too far left
    assert capsule._clamp_x(leftmost - 9999, w) == leftmost
    # Way too far right
    assert capsule._clamp_x(rightmost + 9999, w) == rightmost - w + 1
    # Inside — pass-through
    inside = leftmost + 50
    assert capsule._clamp_x(inside, w) == inside
