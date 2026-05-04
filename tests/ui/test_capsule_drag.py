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
