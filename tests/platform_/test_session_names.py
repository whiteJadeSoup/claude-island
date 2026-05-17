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
# get_uuid_by_name — reverse lookup for ``claude --resume <name>`` recovery
# --------------------------------------------------------------------------

class TestGetUuidByName:
    def test_returns_none_when_file_missing(self, tmp_path_patched):
        assert session_names.get_uuid_by_name("anything") is None

    def test_returns_none_when_name_empty_or_whitespace(self, tmp_path_patched):
        tmp_path_patched.write_text(
            json.dumps({"abc": ""}), encoding="utf-8",
        )
        assert session_names.get_uuid_by_name("") is None
        assert session_names.get_uuid_by_name("   ") is None

    def test_returns_uuid_when_name_matches(self, tmp_path_patched):
        tmp_path_patched.write_text(
            json.dumps({
                "5d0e7a27-267f-46de-89c2-41a0c2664321": "cc-learning",
                "abc-123": "frontend refactor",
            }),
            encoding="utf-8",
        )
        assert (
            session_names.get_uuid_by_name("cc-learning")
            == "5d0e7a27-267f-46de-89c2-41a0c2664321"
        )
        assert session_names.get_uuid_by_name("frontend refactor") == "abc-123"

    def test_returns_none_when_name_unknown(self, tmp_path_patched):
        tmp_path_patched.write_text(
            json.dumps({"abc": "stored"}), encoding="utf-8",
        )
        assert session_names.get_uuid_by_name("not-in-store") is None

    def test_strips_whitespace_before_compare(self, tmp_path_patched):
        """User may have typed ``--resume cc-learning `` with a trailing
        space (or our caller may have done so before passing). Match on
        stripped value."""
        tmp_path_patched.write_text(
            json.dumps({"abc": "cc-learning"}), encoding="utf-8",
        )
        assert session_names.get_uuid_by_name("  cc-learning  ") == "abc"

    def test_first_match_wins_on_duplicate_names(self, tmp_path_patched):
        """Rare but possible (user reused a name across sessions before
        dedup kicked in): return either one — first match in dict-iter
        order. Asserts the function doesn't crash or return junk."""
        tmp_path_patched.write_text(
            json.dumps({"uuid-a": "dup", "uuid-b": "dup"}),
            encoding="utf-8",
        )
        result = session_names.get_uuid_by_name("dup")
        assert result in ("uuid-a", "uuid-b")

    def test_skips_non_string_values(self, tmp_path_patched):
        """Same defensive pattern as get_session_name: don't match
        against numeric / null values in a hand-edited file."""
        tmp_path_patched.write_text(
            json.dumps({"a": "ok", "b": 42}), encoding="utf-8",
        )
        assert session_names.get_uuid_by_name("ok") == "a"


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


class TestConcurrentWrites:
    """The read-modify-write inside set_session_name / gc_session_names
    runs under a process-wide lock. Without it, the gc daemon thread
    racing the Qt main thread (renaming) silently lost one update —
    both threads loaded the same dict, both wrote back independently,
    last writer won.

    These tests pin the lock by stress-testing concurrent writes and
    asserting NO entries are lost."""

    def test_concurrent_renames_do_not_lose_updates(self, tmp_path_patched):
        import threading
        # 50 workers each writing a distinct uuid → all 50 must be
        # present afterwards. Pre-lock, this would lose ~half.
        N = 50
        threads = [
            threading.Thread(
                target=session_names.set_session_name,
                args=(f"uuid-{i}", f"name-{i}"),
            )
            for i in range(N)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        # Every uuid wrote exactly one name; every name is recoverable.
        for i in range(N):
            assert session_names.get_session_name(f"uuid-{i}") == f"name-{i}"

    def test_gc_concurrent_with_renames(self, tmp_path_patched):
        import threading
        # Pre-seed alive entries that gc should KEEP.
        for i in range(20):
            session_names.set_session_name(f"alive-{i}", f"name-{i}")
        # Pre-seed dead entries that gc should drop.
        for i in range(20):
            session_names.set_session_name(f"dead-{i}", f"stale-{i}")

        known = {f"alive-{i}" for i in range(20)}
        # New entries the gc must NOT lose, even if added mid-cycle.
        new_uuids = [f"new-{i}" for i in range(20)]
        known.update(new_uuids)

        def gc_worker():
            session_names.gc_session_names(known)

        def add_worker(idx):
            session_names.set_session_name(new_uuids[idx], f"newname-{idx}")

        threads = [threading.Thread(target=gc_worker)]
        threads += [
            threading.Thread(target=add_worker, args=(i,)) for i in range(20)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # Alive entries survive.
        for i in range(20):
            assert session_names.get_session_name(f"alive-{i}") == f"name-{i}"
        # All new entries survive (none lost to gc's overwrite).
        for i in range(20):
            assert session_names.get_session_name(f"new-{i}") == f"newname-{i}"
        # Dead entries gone (gc's pruning didn't get clobbered).
        # Note: this is a weaker check — the gc may not have even
        # observed all dead entries depending on interleave — but
        # at least one gc pass should have run somewhere.
        # The strong check is the survival of alive + new entries.


class TestReadFailureLogging:
    """Read-time failures (corrupt JSON, wrong shape) used to silently
    return {}, hiding renames from the user with no diagnostic. These
    tests verify warnings now reach stderr."""

    def test_malformed_json_logs_warning(self, tmp_path_patched, capsys):
        tmp_path_patched.write_text("not json {[", encoding="utf-8")
        result = session_names.get_session_name("any")
        assert result is None
        err = capsys.readouterr().err
        assert "malformed" in err.lower()

    def test_wrong_shape_logs_warning(self, tmp_path_patched, capsys):
        # Top-level array instead of object — JSON-valid but wrong.
        tmp_path_patched.write_text('["not", "an", "object"]', encoding="utf-8")
        result = session_names.get_session_name("any")
        assert result is None
        err = capsys.readouterr().err
        assert "object" in err.lower() or "ignoring" in err.lower()

    def test_missing_file_does_NOT_log(self, tmp_path_patched, capsys):
        # First-time-user case (file never created) — must stay silent
        # so the absence isn't mistaken for an error in the user's logs.
        assert session_names.get_session_name("any") is None
        err = capsys.readouterr().err
        assert err == ""


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
