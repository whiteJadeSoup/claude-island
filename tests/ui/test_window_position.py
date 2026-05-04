"""Tests for window_position load/save helpers.

Pure file-IO module — no Qt or platform dependencies — but lives
under ui/ because it's only consumed by the capsule. Test exercises
the atomic write, the cross-platform path resolution, and the
defensive load (corrupt file → None, missing file → None).
"""
from __future__ import annotations

import json

import pytest

from claude_island.ui import window_position


@pytest.fixture
def tmp_path_file(tmp_path, monkeypatch):
    """Redirect WINDOW_POSITION_PATH at the module attribute so each
    test gets a fresh file. Mirrors the platformdirs / session_names
    test pattern in this codebase."""
    target = tmp_path / "window.json"
    monkeypatch.setattr(window_position, "WINDOW_POSITION_PATH", target)
    yield target


def test_load_returns_none_when_file_missing(tmp_path_file):
    assert window_position.load_position() is None


def test_save_creates_file_with_x_y(tmp_path_file):
    window_position.save_position(123, 45)
    assert tmp_path_file.exists()
    data = json.loads(tmp_path_file.read_text(encoding="utf-8"))
    assert data == {"x": 123, "y": 45}


def test_load_round_trips_saved_value(tmp_path_file):
    window_position.save_position(123, 45)
    assert window_position.load_position() == (123, 45)


def test_load_returns_none_on_corrupt_file(tmp_path_file):
    """Malformed JSON must not raise — corrupt file → None so the
    capsule falls back to default centre rather than crash on boot."""
    tmp_path_file.write_text("{not valid", encoding="utf-8")
    assert window_position.load_position() is None


def test_load_returns_none_when_keys_wrong_type(tmp_path_file):
    """Schema drift (e.g. user manually edited the file with strings)
    must also fall through cleanly to None — better default-centre
    than crash on type confusion."""
    tmp_path_file.write_text(
        json.dumps({"x": "lol", "y": "no"}), encoding="utf-8",
    )
    assert window_position.load_position() is None


def test_save_creates_parent_directory(tmp_path, monkeypatch):
    """First-run case: ~/.claude-island/ doesn't exist yet, save
    must mkdir it (parents=True) before writing."""
    target = tmp_path / "fresh-island-dir" / "window.json"
    monkeypatch.setattr(window_position, "WINDOW_POSITION_PATH", target)

    window_position.save_position(7, 8)
    assert target.exists()
    assert target.parent.is_dir()


def test_save_overwrites_previous_value_atomically(tmp_path_file):
    """Atomic write: a second save must fully replace the first
    file's contents (no JSON appending / partial write)."""
    window_position.save_position(1, 2)
    window_position.save_position(99, 100)
    data = json.loads(tmp_path_file.read_text(encoding="utf-8"))
    assert data == {"x": 99, "y": 100}
