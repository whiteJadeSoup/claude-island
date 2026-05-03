"""Tests for session_names: persistent custom display names keyed by
session UUID. Mirrors the providers/__init__ test style — patch the
module-level path attribute, drive the public helpers, assert the
file shape on disk."""
from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from claude_island.platform_ import session_names


@pytest.fixture
def tmp_path_patched(tmp_path):
    """Redirect SESSION_NAMES_PATH to a tmp file for the test."""
    path = tmp_path / "session_names.json"
    with patch("claude_island.platform_.session_names.SESSION_NAMES_PATH", path):
        yield path


# --------------------------------------------------------------------------
# get_session_name — read path
# --------------------------------------------------------------------------

class TestGetSessionName:
    def test_returns_none_when_file_missing(self, tmp_path_patched):
        assert session_names.get_session_name("any") is None

    def test_returns_none_when_empty_uuid(self, tmp_path_patched):
        # Empty uuid is a real case — sessions whose transcript hasn't
        # been resolved yet pass "" through. Don't accidentally match
        # an empty key in a corrupted file.
        tmp_path_patched.write_text(json.dumps({"": "ghost"}), encoding="utf-8")
        assert session_names.get_session_name("") is None

    def test_returns_stored_name(self, tmp_path_patched):
        tmp_path_patched.write_text(
            json.dumps({"abc-123": "frontend refactor"}), encoding="utf-8",
        )
        assert session_names.get_session_name("abc-123") == "frontend refactor"

    def test_treats_empty_string_value_as_unset(self, tmp_path_patched):
        # An empty string in the file should not surface as the override
        # — set_session_name uses "" as the delete sentinel; if the file
        # somehow ends up with "" anyway, get() should hide it.
        tmp_path_patched.write_text(
            json.dumps({"abc": ""}), encoding="utf-8",
        )
        assert session_names.get_session_name("abc") is None

    def test_skips_non_string_values(self, tmp_path_patched):
        # Defence against a hand-edited / corrupted file: a numeric or
        # null value would break the QLabel.setText call downstream.
        tmp_path_patched.write_text(
            json.dumps({"a": "ok", "b": 42, "c": None}), encoding="utf-8",
        )
        assert session_names.get_session_name("a") == "ok"
        assert session_names.get_session_name("b") is None
        assert session_names.get_session_name("c") is None

    def test_returns_none_on_malformed_json(self, tmp_path_patched):
        tmp_path_patched.write_text("not json {[", encoding="utf-8")
        assert session_names.get_session_name("any") is None


# --------------------------------------------------------------------------
# set_session_name — write path
# --------------------------------------------------------------------------

class TestSetSessionName:
    def test_creates_file_with_first_entry(self, tmp_path_patched):
        session_names.set_session_name("uuid-1", "feature work")
        data = json.loads(tmp_path_patched.read_text(encoding="utf-8"))
        assert data == {"uuid-1": "feature work"}

    def test_merges_into_existing_file(self, tmp_path_patched):
        tmp_path_patched.write_text(
            json.dumps({"existing": "keep me"}), encoding="utf-8",
        )
        session_names.set_session_name("new", "added")
        data = json.loads(tmp_path_patched.read_text(encoding="utf-8"))
        assert data == {"existing": "keep me", "new": "added"}

    def test_overwrites_existing_uuid(self, tmp_path_patched):
        session_names.set_session_name("u", "first")
        session_names.set_session_name("u", "second")
        data = json.loads(tmp_path_patched.read_text(encoding="utf-8"))
        assert data == {"u": "second"}

    def test_strips_whitespace(self, tmp_path_patched):
        session_names.set_session_name("u", "   spaced   ")
        assert session_names.get_session_name("u") == "spaced"

    def test_empty_string_deletes_entry(self, tmp_path_patched):
        # The "restore default" gesture: clear the input, hit save.
        session_names.set_session_name("u", "named")
        session_names.set_session_name("u", "")
        assert session_names.get_session_name("u") is None
        # File should still exist (other entries may live there).
        data = json.loads(tmp_path_patched.read_text(encoding="utf-8"))
        assert data == {}

    def test_whitespace_only_deletes_entry(self, tmp_path_patched):
        # Same as empty — a user who types spaces and saves clearly
        # didn't intend to override with whitespace.
        session_names.set_session_name("u", "named")
        session_names.set_session_name("u", "   \t  ")
        assert session_names.get_session_name("u") is None

    def test_empty_uuid_is_noop(self, tmp_path_patched):
        # Sessions whose transcript hasn't resolved yet pass "" — must
        # not corrupt the file by writing an empty key.
        session_names.set_session_name("", "ghost")
        assert not tmp_path_patched.exists()

    def test_idempotent_save_skips_write(self, tmp_path_patched):
        # Same value twice → second call is a no-op. Verified by mtime
        # not changing across calls. Matters for the rename UI which
        # may fire on every keystroke if ever wired that way; today
        # it fires on Enter only but the guarantee is cheap to keep.
        session_names.set_session_name("u", "name")
        mtime_a = tmp_path_patched.stat().st_mtime_ns
        session_names.set_session_name("u", "name")
        mtime_b = tmp_path_patched.stat().st_mtime_ns
        assert mtime_a == mtime_b

    def test_unicode_round_trips(self, tmp_path_patched):
        # CJK / emoji etc. must survive; ensure_ascii=False is set in
        # the writer specifically so the file stays readable when
        # someone opens it manually.
        session_names.set_session_name("u", "前端重构 🚀")
        # On disk: actual UTF-8, not \uXXXX escapes.
        text = tmp_path_patched.read_text(encoding="utf-8")
        assert "前端重构" in text
        assert "🚀" in text
        assert session_names.get_session_name("u") == "前端重构 🚀"


# --------------------------------------------------------------------------
# delete_session_name — convenience wrapper
# --------------------------------------------------------------------------

class TestDeleteSessionName:
    def test_removes_entry(self, tmp_path_patched):
        session_names.set_session_name("u", "named")
        session_names.delete_session_name("u")
        assert session_names.get_session_name("u") is None

    def test_noop_when_uuid_unknown(self, tmp_path_patched):
        # Doesn't raise, doesn't corrupt the file.
        session_names.set_session_name("a", "keep")
        session_names.delete_session_name("never-seen")
        assert session_names.get_session_name("a") == "keep"


# --------------------------------------------------------------------------
# gc_session_names — startup hygiene
# --------------------------------------------------------------------------

class TestGcSessionNames:
    def test_drops_entries_for_unknown_uuids(self, tmp_path_patched):
        session_names.set_session_name("alive", "keep me")
        session_names.set_session_name("dead", "stale")
        session_names.gc_session_names({"alive"})
        assert session_names.get_session_name("alive") == "keep me"
        assert session_names.get_session_name("dead") is None

    def test_empty_known_set_is_noop(self, tmp_path_patched):
        # Safety guard: empty set means "I don't know any sessions yet"
        # (not "wipe everything"). Without this guard, calling gc before
        # the JSONL parser had indexed anything would nuke the file.
        session_names.set_session_name("u", "named")
        session_names.gc_session_names(set())
        assert session_names.get_session_name("u") == "named"

    def test_skips_write_when_nothing_to_prune(self, tmp_path_patched):
        # If every entry is in known_uuids, leave the file alone (no
        # pointless mtime bump).
        session_names.set_session_name("u", "named")
        mtime_a = tmp_path_patched.stat().st_mtime_ns
        session_names.gc_session_names({"u"})
        mtime_b = tmp_path_patched.stat().st_mtime_ns
        assert mtime_a == mtime_b
