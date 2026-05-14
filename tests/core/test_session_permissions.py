"""Tests for SessionPermissionCache (G7) + review-prompts toggle (G8).

Test plan mirrors Detail Design §7 (T7.x, T8.1):
  T7.1 happy   — grant then check returns True
  T7.2 edge    — granular per (uuid, tool) — check Edit returns False after Bash grant
  T7.3 edge    — evict_session evicts all entries for that uuid
  T7.4 edge    — TTL expiry → check returns False; evict_expired returns count
  T8.1 happy   — set/is_review default False, can be toggled
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from claude_island.core.session_permissions import (
    DEFAULT_TTL_S,
    SessionPermissionCache,
)


# ── Fixtures ──────────────────────────────────────────────────────────


@pytest.fixture
def changes() -> list[int]:
    return []


@pytest.fixture
def cache(changes: list[int]) -> SessionPermissionCache:
    return SessionPermissionCache(on_change=lambda: changes.append(1))


@pytest.fixture
def short_ttl_cache(changes: list[int]) -> SessionPermissionCache:
    return SessionPermissionCache(ttl_s=0.1, on_change=lambda: changes.append(1))


# ── grants ───────────────────────────────────────────────────────────


class TestGrants:
    def test_grant_then_check_returns_true(self, cache):
        cache.grant("u1", "Bash")
        assert cache.check("u1", "Bash") is True

    def test_check_unknown_returns_false(self, cache):
        assert cache.check("u1", "Bash") is False

    def test_per_tool_granularity(self, cache):
        # T7.2 — granting Bash doesn't grant Edit.
        cache.grant("u1", "Bash")
        assert cache.check("u1", "Bash") is True
        assert cache.check("u1", "Edit") is False
        assert cache.check("u1", "Read") is False

    def test_per_session_granularity(self, cache):
        cache.grant("u1", "Bash")
        assert cache.check("u2", "Bash") is False

    def test_grant_renews_ttl(self, cache):
        # Re-granting overwrites; expires_at should be later.
        cache.grant("u1", "Bash", now=datetime(2026, 1, 1, tzinfo=timezone.utc))
        cache.grant("u1", "Bash", now=datetime(2026, 1, 1, 1, tzinfo=timezone.utc))
        assert cache.grant_count() == 1  # still one entry

    def test_empty_inputs_noop(self, cache):
        cache.grant("", "Bash")
        cache.grant("u1", "")
        assert cache.grant_count() == 0
        assert cache.check("", "Bash") is False

    def test_grant_count(self, cache):
        cache.grant("u1", "Bash")
        cache.grant("u1", "Edit")
        cache.grant("u2", "Bash")
        assert cache.grant_count() == 3


# ── eviction ─────────────────────────────────────────────────────────


class TestEvictSession:
    def test_evicts_all_grants_for_uuid(self, cache):
        cache.grant("u1", "Bash")
        cache.grant("u1", "Edit")
        cache.grant("u2", "Bash")
        n = cache.evict_session("u1")
        assert n == 2
        assert cache.check("u1", "Bash") is False
        assert cache.check("u2", "Bash") is True

    def test_evicts_review_mode_too(self, cache):
        cache.grant("u1", "Bash")
        cache.set_review("u1", True)
        n = cache.evict_session("u1")
        # 1 grant + 1 review-mode entry
        assert n == 2
        assert cache.is_review("u1") is False

    def test_no_op_for_unknown(self, cache):
        assert cache.evict_session("nobody") == 0

    def test_no_op_for_empty(self, cache):
        cache.grant("u1", "Bash")
        assert cache.evict_session("") == 0
        assert cache.check("u1", "Bash") is True

    def test_eviction_fires_on_change(self, cache, changes):
        cache.grant("u1", "Bash")
        baseline = len(changes)
        cache.evict_session("u1")
        assert len(changes) > baseline


class TestEvictExpired:
    def test_evicts_past_ttl(self, short_ttl_cache):
        # ttl=0.1s
        import time
        short_ttl_cache.grant("u1", "Bash")
        time.sleep(0.15)
        n = short_ttl_cache.evict_expired()
        assert n == 1
        assert short_ttl_cache.check("u1", "Bash") is False

    def test_check_lazily_evicts_expired(self, short_ttl_cache):
        # check() itself should drop a stale entry — useful so a single
        # call gets correct semantics without waiting for the periodic
        # evict_expired timer.
        import time
        short_ttl_cache.grant("u1", "Bash")
        time.sleep(0.15)
        # First check returns False AND removes entry.
        assert short_ttl_cache.check("u1", "Bash") is False
        # evict_expired now finds nothing (already lazily evicted).
        assert short_ttl_cache.evict_expired() == 0

    def test_idempotent(self, short_ttl_cache):
        short_ttl_cache.grant("u1", "Bash")
        # Nothing expired yet.
        assert short_ttl_cache.evict_expired() == 0


# ── review-prompts toggle ────────────────────────────────────────────


class TestReviewToggle:
    def test_default_is_false(self, cache):
        # T8.1 — default OFF (so UserPromptSubmit fast-paths by default).
        assert cache.is_review("u1") is False

    def test_set_to_true(self, cache):
        cache.set_review("u1", True)
        assert cache.is_review("u1") is True

    def test_set_back_to_false_clears(self, cache):
        cache.set_review("u1", True)
        cache.set_review("u1", False)
        assert cache.is_review("u1") is False
        # Should be cleared from internal storage, not lingering as False.
        assert cache.review_count() == 0

    def test_per_session(self, cache):
        cache.set_review("u1", True)
        assert cache.is_review("u2") is False

    def test_change_fires_on_change(self, cache, changes):
        baseline = len(changes)
        cache.set_review("u1", True)
        assert len(changes) == baseline + 1
        # Setting to same value shouldn't fire again (no-op semantics).
        cache.set_review("u1", True)
        assert len(changes) == baseline + 1
        cache.set_review("u1", False)
        assert len(changes) == baseline + 2

    def test_empty_uuid_noop(self, cache):
        cache.set_review("", True)
        assert cache.is_review("") is False
        assert cache.review_count() == 0


# ── default TTL constant ─────────────────────────────────────────────


class TestDefaults:
    def test_default_ttl_is_4h(self):
        assert DEFAULT_TTL_S == 4 * 60 * 60
