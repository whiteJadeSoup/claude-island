"""Pure-function tests for ui/recents_filter.

These tests have no Qt dependency — they could live anywhere. Kept under
tests/ui/ for source colocation with the module they test.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from claude_island.core.models import DormantSession
from claude_island.ui.recents_filter import (
    filter_by_query,
    search_haystack,
    sort_by_recency,
)


def _d(uuid: str = "u1", **kw) -> DormantSession:
    defaults = dict(
        session_uuid=uuid,
        cwd=Path("D:/projects/foo"),
        name=None,
        last_prompt=None,
        last_activity=datetime(2026, 5, 1, 12, tzinfo=timezone.utc),
        started_at=None,
        permission_mode=None,
        git_branch=None,
        cost_usd=0.0,
        turn_count=0,
    )
    defaults.update(kw)
    return DormantSession(**defaults)


# ── filter_by_query ─────────────────────────────────────────────────────

class TestFilterByQuery:
    def test_match_name(self):  # F1
        a = _d("u1", name="refactor auth")
        b = _d("u2", name="bug-fix")
        assert filter_by_query([a, b], "refactor") == [a]

    def test_match_cwd(self):  # F2
        a = _d("u1", cwd=Path("D:/projects/claude-island"))
        b = _d("u2", cwd=Path("D:/other"))
        assert filter_by_query([a, b], "claude-island") == [a]

    def test_match_git_branch(self):  # F3
        a = _d("u1", git_branch="feat-recents")
        b = _d("u2", git_branch="master")
        assert filter_by_query([a, b], "recents") == [a]

    def test_match_last_prompt(self):  # F4
        a = _d("u1", last_prompt="how do I add memory to claude")
        b = _d("u2", last_prompt="something else")
        assert filter_by_query([a, b], "memory") == [a]

    def test_match_full_uuid(self):  # F5
        a = _d("abc12345-aaaa-bbbb-cccc-ddddeeeeffff")
        b = _d("99999999-aaaa-bbbb-cccc-ddddeeeeffff")
        # power-user pasted the full uuid from a log
        assert filter_by_query([a, b], "abc12345-aaaa-bbbb-cccc-ddddeeeeffff") == [a]

    def test_empty_query_returns_input(self):  # F6
        a = _d("u1")
        b = _d("u2")
        assert filter_by_query([a, b], "") == [a, b]

    def test_whitespace_query_returns_input(self):  # F7
        a = _d("u1")
        assert filter_by_query([a], "   \t\n") == [a]

    def test_case_insensitive(self):  # F8
        a = _d("u1", name="Refactor Auth")
        assert filter_by_query([a], "REFACTOR") == [a]
        assert filter_by_query([a], "auth") == [a]

    def test_empty_input(self):  # F9
        assert filter_by_query([], "anything") == []
        assert filter_by_query((), "") == []

    def test_preserves_input_order(self):  # F10
        a = _d("u1", name="alpha refactor")
        b = _d("u2", name="bravo refactor")
        c = _d("u3", name="charlie refactor")
        # input order is c, a, b — output should be the same order
        assert filter_by_query([c, a, b], "refactor") == [c, a, b]


# ── sort_by_recency ─────────────────────────────────────────────────────

class TestSortByRecency:
    def test_newest_first(self):  # F11
        old = _d("u1", last_activity=datetime(2026, 1, 1, tzinfo=timezone.utc))
        mid = _d("u2", last_activity=datetime(2026, 3, 1, tzinfo=timezone.utc))
        new = _d("u3", last_activity=datetime(2026, 5, 1, tzinfo=timezone.utc))
        assert sort_by_recency([old, new, mid]) == [new, mid, old]

    def test_stable_on_ties(self):  # F12
        ts = datetime(2026, 5, 1, 12, tzinfo=timezone.utc)
        a = _d("u1", last_activity=ts)
        b = _d("u2", last_activity=ts)
        c = _d("u3", last_activity=ts)
        # all equal → preserves input order
        assert sort_by_recency([a, b, c]) == [a, b, c]
        assert sort_by_recency([c, a, b]) == [c, a, b]

    def test_empty_input(self):  # F13
        assert sort_by_recency([]) == []
        assert sort_by_recency(()) == []

    def test_does_not_mutate_input(self):
        old = _d("u1", last_activity=datetime(2026, 1, 1, tzinfo=timezone.utc))
        new = _d("u2", last_activity=datetime(2026, 5, 1, tzinfo=timezone.utc))
        original = [old, new]
        _ = sort_by_recency(original)
        assert original == [old, new]  # input list unchanged


# ── search_haystack ─────────────────────────────────────────────────────

class TestSearchHaystack:
    def test_includes_all_visible_fields(self):
        d = _d(
            "uuid-xyz",
            name="My Refactor",
            last_prompt="ASK ABOUT MEMORY",
            cwd=Path("D:/Projects/Foo"),
            git_branch="Feature-Bar",
        )
        h = search_haystack(d)
        assert "my refactor" in h
        assert "ask about memory" in h
        assert "d:/projects/foo" in h or "d:\\projects\\foo" in h
        assert "feature-bar" in h
        assert "uuid-xyz" in h

    def test_handles_none_fields(self):
        d = _d("u1", name=None, last_prompt=None, git_branch=None)
        h = search_haystack(d)
        # Just shouldn't raise; uuid still present
        assert "u1" in h
