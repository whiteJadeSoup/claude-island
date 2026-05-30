"""Regression: a failed force_refresh must NOT wipe a good cached quota.

Root cause of the "quota disappeared" bug: qml_app's 60s heartbeat called
quota_engine.force_refresh(), which on a 429/network failure evicts the
in-memory cache (force_refresh's documented "invalidate on failure"
behaviour). The next get() then re-fetches, hits the same 429, and returns
None — so the whole QUOTA card blanks out on every transient failure.

These tests pin the engine's contract so the fix (heartbeat → get(), not
force_refresh()) rests on verified behaviour rather than assumption.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from claude_island.core.models import QuotaSnapshot
from claude_island.platform_.providers import ProviderEngine, _PROVIDERS


_GOOD = QuotaSnapshot(
    five_hour_pct=5.0,
    five_hour_resets_at=datetime(2026, 5, 30, 19, 0, tzinfo=timezone.utc),
    seven_day_pct=45.0,
    seven_day_resets_at=datetime(2026, 5, 31, 8, 0, tzinfo=timezone.utc),
    fetched_at=datetime(2026, 5, 30, 14, 0, tzinfo=timezone.utc),
    is_stale=False,
    provider="fakeprov",
)


class _FlakyProvider:
    """A provider whose fetch() succeeds once then fails (simulates 429)."""
    name = "fakeprov"

    def __init__(self):
        self.calls = 0

    def detect(self):
        return True

    def fetch(self, *, cache_dir: Path, bypass_cache: bool = False):
        self.calls += 1
        # First successful fetch seeds the cache; every later fetch "429"s.
        return _GOOD if self.calls == 1 else None


@pytest.fixture
def engine(tmp_path, monkeypatch):
    inst = _FlakyProvider()
    # Register our fake under its name so _resolve("fakeprov") finds it.
    monkeypatch.setitem(_PROVIDERS, "fakeprov", lambda: inst)
    eng = ProviderEngine(cache_dir=tmp_path)
    return eng, inst


def test_get_serves_memory_cache_after_first_success(engine):
    """get() must serve the in-memory cache on the 2nd call instead of
    re-fetching — so a later 429 never even reaches the network."""
    eng, prov = engine
    first = eng.get(provider_name="fakeprov")
    assert first is _GOOD
    assert prov.calls == 1
    # Second get() within the memory TTL → cache hit, NO new fetch.
    second = eng.get(provider_name="fakeprov")
    assert second is _GOOD
    assert prov.calls == 1, "get() re-fetched instead of using the cache"


def test_force_refresh_failure_evicts_cache(engine):
    """force_refresh on failure evicts the cache (its documented contract).
    This is WHY the heartbeat must not call it: the next get() is forced to
    re-fetch and gets None."""
    eng, prov = engine
    assert eng.get(provider_name="fakeprov") is _GOOD   # seed cache (call 1)
    # force_refresh bypasses cache → fetch #2 → None → evicts memory cache.
    assert eng.force_refresh(provider_name="fakeprov") is None
    # Now get() has no cache → fetch #3 → None. Quota blanks out.
    assert eng.get(provider_name="fakeprov") is None
    assert prov.calls == 3


def test_get_only_heartbeat_keeps_quota_through_failures(engine):
    """The FIX's behaviour: if the heartbeat only ever calls get() (never
    force_refresh), a one-time success keeps serving from cache and quota
    never blanks, even though every subsequent fetch would 429."""
    eng, prov = engine
    assert eng.get(provider_name="fakeprov") is _GOOD   # call 1 seeds cache
    # Simulate many heartbeat ticks — all get(), all cache hits.
    for _ in range(5):
        assert eng.get(provider_name="fakeprov") is _GOOD
    assert prov.calls == 1, "heartbeat get() should never re-fetch within TTL"
