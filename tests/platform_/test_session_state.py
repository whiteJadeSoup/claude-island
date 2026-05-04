"""Tests for the session-state reader (~/.claude/sessions/<pid>.json).

Verifies the on-disk reader, the 5 s in-memory cache, and the
``startedAt`` (epoch ms → datetime) parser. Filesystem is isolated
via tmp_path; the cache is reset between tests.
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone

import pytest

from claude_island.platform_ import session_state


@pytest.fixture(autouse=True)
def _isolated_cache():
    session_state.reset_cache_for_tests()
    yield
    session_state.reset_cache_for_tests()


def _write_state(tmp_path, pid, payload):
    p = tmp_path / f"{pid}.json"
    p.write_text(json.dumps(payload), encoding="utf-8")
    return p


def test_read_returns_parsed_dict(tmp_path):
    _write_state(tmp_path, 1234, {
        "pid": 1234,
        "name": "cc-learning",
        "status": "busy",
        "startedAt": 1777620018418,
    })
    out = session_state.read_session_state(1234, sessions_dir=tmp_path)
    assert out is not None
    assert out["name"] == "cc-learning"
    assert out["status"] == "busy"


def test_missing_file_returns_none(tmp_path):
    """The user's pid scanner finds processes that may not have a
    sessions/<pid>.json (e.g. a pid that just exited). Tooltip should
    degrade gracefully, not crash."""
    assert session_state.read_session_state(99999, sessions_dir=tmp_path) is None


def test_malformed_json_returns_none(tmp_path):
    p = tmp_path / "555.json"
    p.write_text("not json {", encoding="utf-8")
    assert session_state.read_session_state(555, sessions_dir=tmp_path) is None


class TestInvalidateCache:
    """Single-pid cache invalidation: drives the sessions/ watchdog
    fix for slow status-change propagation. Wiring layer
    (__main__.py) calls invalidate_cache(pid) when sessions/<pid>.json
    is modified so the next read bypasses TTL and picks up the
    fresh status word.

    The granularity invariant is critical — invalidation must NEVER
    leak into other pids' cache entries. Mass-clearing the cache on
    every status flip would force a redundant disk read for every
    other session on the next snapshot tick (10+ sessions during
    active development).
    """

    def test_invalidate_drops_only_target_pid(self, tmp_path):
        # Seed cache for two pids.
        _write_state(tmp_path, 1234, {"status": "busy"})
        _write_state(tmp_path, 5678, {"status": "idle"})
        session_state.read_session_state(1234, sessions_dir=tmp_path)
        session_state.read_session_state(5678, sessions_dir=tmp_path)
        # Both should be in cache now.
        assert 1234 in session_state._cache
        assert 5678 in session_state._cache

        session_state.invalidate_cache(1234)

        # 1234 evicted; 5678 untouched. Single-pid granularity.
        assert 1234 not in session_state._cache
        assert 5678 in session_state._cache

    def test_invalidate_unknown_pid_is_noop(self, tmp_path):
        """Invalidating a pid that was never cached must not raise —
        the watchdog can fire for a pid we haven't read yet (e.g.
        first-write of a brand-new sessions/<pid>.json)."""
        # Seed an unrelated pid first so we can confirm it survives.
        _write_state(tmp_path, 1234, {"status": "idle"})
        session_state.read_session_state(1234, sessions_dir=tmp_path)

        session_state.invalidate_cache(99999)  # never cached

        assert 1234 in session_state._cache  # untouched

    def test_invalidate_then_read_picks_up_fresh_value(self, tmp_path):
        """End-to-end of the bug fix: write idle → read → write busy →
        invalidate → read again must return busy (not the cached idle
        that the TTL would otherwise hold)."""
        _write_state(tmp_path, 1234, {"status": "idle"})
        first = session_state.read_session_state(1234, sessions_dir=tmp_path)
        assert first["status"] == "idle"

        # Disk update — without invalidate the next read returns the
        # cached "idle" until TTL expires.
        _write_state(tmp_path, 1234, {"status": "busy"})
        session_state.invalidate_cache(1234)

        second = session_state.read_session_state(1234, sessions_dir=tmp_path)
        assert second["status"] == "busy"


def test_within_ttl_returns_cached_value(tmp_path):
    """A second read for the same pid within the TTL must NOT touch
    the disk. We prove that by mutating the file after the first
    read — the second read should still return the OLD value."""
    _write_state(tmp_path, 7, {"name": "first"})
    first = session_state.read_session_state(7, sessions_dir=tmp_path)
    assert first["name"] == "first"

    # Overwrite on disk; read should still serve cached "first".
    _write_state(tmp_path, 7, {"name": "second"})
    second = session_state.read_session_state(7, sessions_dir=tmp_path)
    assert second["name"] == "first"


def test_started_at_parses_epoch_ms():
    dt = session_state.parse_started_at(1_700_000_000_000)
    assert dt is not None
    assert dt.tzinfo == timezone.utc
    # Sanity: 1700000000000 ms = 2023-11-14 22:13:20 UTC
    assert dt.year == 2023


def test_started_at_handles_string_input():
    dt = session_state.parse_started_at("1700000000000")
    assert dt is not None and dt.year == 2023


def test_started_at_returns_none_on_garbage():
    assert session_state.parse_started_at(None) is None
    assert session_state.parse_started_at("nope") is None
    assert session_state.parse_started_at({"a": 1}) is None
    assert session_state.parse_started_at(0) is None
    assert session_state.parse_started_at(-5) is None
