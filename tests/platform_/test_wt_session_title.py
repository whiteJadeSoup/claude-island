"""Tests for the sentinel tab-title format used to bind claude
sessions to specific Windows Terminal tabs."""
from __future__ import annotations

import pytest

from claude_island.platform_.wt_session_title import (
    is_sentinel,
    sentinel_title,
)


class TestSentinelTitle:

    def test_full_uuid_with_dashes_strips_to_32_hex(self):
        title = sentinel_title("a1b2c3d4-e5f6-7890-abcd-ef1234567890")
        assert title == "ci:a1b2c3d4e5f67890abcdef1234567890"

    def test_uuid_without_dashes_passes_through(self):
        title = sentinel_title("a1b2c3d4e5f67890abcdef1234567890")
        assert title == "ci:a1b2c3d4e5f67890abcdef1234567890"

    def test_empty_uuid_returns_none(self):
        """Degraded SessionView with no uuid (scanner caught process
        before its JSONL was parsed) → reconcile must skip."""
        assert sentinel_title("") is None

    def test_returns_str_for_short_uuid(self):
        """Don't validate uuid format here — trust upstream. We only
        guarantee the result is unique iff input is unique."""
        # Test isn't asserting valid uuids; just that the function
        # doesn't reject short strings (might be useful for tests).
        assert sentinel_title("abc") == "ci:abc"


class TestIsSentinel:

    @pytest.mark.parametrize("title", [
        "ci:a1b2c3d4e5f67890abcdef1234567890",
        "ci:abc",            # short but still our prefix
        "ci:",               # malformed but starts with prefix
    ])
    def test_starts_with_prefix(self, title):
        assert is_sentinel(title) is True

    @pytest.mark.parametrize("title", [
        "",
        "Claude Code",       # WT default profile name
        "✳ memory",          # claude topic-shift title
        "CI:abc",            # case-sensitive
        "  ci:abc",          # leading whitespace
        "Some ci: thing",    # prefix not at start
    ])
    def test_rejects_non_prefix(self, title):
        assert is_sentinel(title) is False

    def test_handles_none_safely(self):
        """is_sentinel never raises — even on None or odd input."""
        # Defensive: callers may pass values from os.environ / win32 APIs
        # that haven't been narrowed yet.
        assert is_sentinel(None) is False  # type: ignore[arg-type]
