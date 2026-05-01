"""Tests for QuotaProvider — the Anthropic /api/oauth/usage client.

Network isolation: every test patches ``urllib.request.urlopen`` so we
never make real HTTP. Filesystem isolation: every test uses pytest's
``tmp_path`` for both the credentials file and the cache file, so the
real ~/.claude/.credentials.json is never read.

Q1-Q11 covers the failure-mode matrix from the plan: missing file,
each HTTP error class, TTL behaviour, stale-cache fallback, opt-out,
and concurrent invocation.
"""
from __future__ import annotations

import io
import json
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError, URLError

import pytest

from claude_island.core.models import QuotaSnapshot
from claude_island.platform_ import quota_provider as qp_mod
from claude_island.platform_.quota_provider import QuotaProvider, _CACHE_VERSION


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _good_creds(path: Path, token: str = "tok-abc-123") -> None:
    path.write_text(json.dumps({
        "claudeAiOauth": {"accessToken": token},
    }), encoding="utf-8")


def _good_response(five_pct: float = 53.0, seven_pct: float = 17.0) -> dict:
    """Anthropic-shape JSON, both windows present."""
    return {
        "five_hour": {
            "utilization": five_pct,
            "resets_at": "2026-05-01T20:00:00+00:00",
        },
        "seven_day": {
            "utilization": seven_pct,
            "resets_at": "2026-05-08T16:00:00+00:00",
        },
    }


def _mock_urlopen(payload: dict | None = None,
                  exc: BaseException | None = None,
                  status: int = 200):
    """Build a context manager that ``urlopen`` returns / raises."""
    if exc is not None:
        return MagicMock(side_effect=exc)
    fake = MagicMock()
    fake.status = status
    fake.read.return_value = json.dumps(payload or {}).encode("utf-8")
    cm = MagicMock()
    cm.__enter__.return_value = fake
    cm.__exit__.return_value = False
    return MagicMock(return_value=cm)


@pytest.fixture
def paths(tmp_path):
    return {
        "creds": tmp_path / "credentials.json",
        "cache": tmp_path / "usage-cache.json",
    }


# ---------------------------------------------------------------------------
# Q1: missing credentials → None, no crash
# ---------------------------------------------------------------------------

def test_missing_credentials_returns_none(paths):
    """Q1: credentials file doesn't exist → get() returns None silently."""
    p = QuotaProvider(credentials_path=paths["creds"], cache_path=paths["cache"])
    # urlopen patched to assert NOT called — without creds we shouldn't
    # even attempt the network call.
    with patch.object(qp_mod.urllib.request, "urlopen") as fake:
        result = p.get()
    assert result is None
    fake.assert_not_called()


# ---------------------------------------------------------------------------
# Q2: happy path
# ---------------------------------------------------------------------------

def test_happy_path_returns_snapshot_and_writes_cache(paths):
    """Q2: creds present + endpoint 200 → QuotaSnapshot with parsed values,
    cache file written."""
    _good_creds(paths["creds"])
    p = QuotaProvider(credentials_path=paths["creds"], cache_path=paths["cache"])
    with patch.object(qp_mod.urllib.request, "urlopen",
                      _mock_urlopen(_good_response(five_pct=53.0, seven_pct=17.0))):
        snap = p.get()
    assert isinstance(snap, QuotaSnapshot)
    assert snap.five_hour_pct == 53.0
    assert snap.seven_day_pct == 17.0
    assert not snap.is_stale
    assert paths["cache"].exists()


# ---------------------------------------------------------------------------
# Q3-Q5: HTTP error classes → None, no exception
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("exc", [
    HTTPError("u", 403, "forbidden", {}, None),
    HTTPError("u", 429, "rate limited", {}, None),
    HTTPError("u", 500, "server error", {}, None),
    URLError("network down"),
    TimeoutError("timed out"),
])
def test_http_errors_return_none_silently(paths, exc):
    """Q3/Q4/Q5/Q6: every error class swallowed → None, no raise."""
    _good_creds(paths["creds"])
    p = QuotaProvider(credentials_path=paths["creds"], cache_path=paths["cache"])
    with patch.object(qp_mod.urllib.request, "urlopen", _mock_urlopen(exc=exc)):
        snap = p.get()
    assert snap is None
    # No cache should have been written on failure
    assert not paths["cache"].exists()


# ---------------------------------------------------------------------------
# Q7: TTL — second call within window doesn't re-issue HTTP
# ---------------------------------------------------------------------------

def test_consecutive_calls_within_ttl_dont_refetch(paths):
    """Q7: get() inside POLL_TTL returns cached without an HTTP round trip."""
    _good_creds(paths["creds"])
    p = QuotaProvider(credentials_path=paths["creds"], cache_path=paths["cache"])

    fake = _mock_urlopen(_good_response(five_pct=42.0))
    with patch.object(qp_mod.urllib.request, "urlopen", fake):
        first = p.get()
        second = p.get()

    assert first.five_hour_pct == 42.0 == second.five_hour_pct
    # Exactly one network call across the two get()s
    assert fake.call_count == 1


# ---------------------------------------------------------------------------
# Q8: TTL expired + endpoint fails → serve stale
# ---------------------------------------------------------------------------

def test_expired_cache_served_stale_when_refresh_fails(paths):
    """Q8: cache exists but stale; endpoint errors → return cached
    snapshot with is_stale=True (don't lose the previous value)."""
    _good_creds(paths["creds"])
    # Plant an old cache directly so we don't have to time-travel.
    old = datetime.now(timezone.utc) - timedelta(seconds=qp_mod.POLL_TTL_SECONDS + 60)
    paths["cache"].write_text(json.dumps({
        "version": _CACHE_VERSION,
        "fetched_at": old.isoformat(),
        "five_hour": {"utilization": 88.0, "resets_at": "2026-05-01T20:00:00+00:00"},
        "seven_day": {"utilization": 22.0, "resets_at": "2026-05-08T16:00:00+00:00"},
    }), encoding="utf-8")

    p = QuotaProvider(credentials_path=paths["creds"], cache_path=paths["cache"])
    with patch.object(qp_mod.urllib.request, "urlopen",
                      _mock_urlopen(exc=URLError("offline"))):
        snap = p.get()

    assert snap is not None
    assert snap.five_hour_pct == 88.0   # served from old cache
    assert snap.is_stale is False        # only > 3*TTL flips this; this is just past TTL


# ---------------------------------------------------------------------------
# Q9: cache > 3×TTL → is_stale True
# ---------------------------------------------------------------------------

def test_very_old_cache_marked_is_stale(paths):
    """Q9: cache age > STALE_MULTIPLIER * TTL → is_stale=True."""
    _good_creds(paths["creds"])
    very_old = datetime.now(timezone.utc) - timedelta(
        seconds=qp_mod.POLL_TTL_SECONDS * (qp_mod.STALE_MULTIPLIER + 1)
    )
    paths["cache"].write_text(json.dumps({
        "version": _CACHE_VERSION,
        "fetched_at": very_old.isoformat(),
        "five_hour": {"utilization": 11.0, "resets_at": "2026-05-01T20:00:00+00:00"},
        "seven_day": {"utilization": 3.0,  "resets_at": "2026-05-08T16:00:00+00:00"},
    }), encoding="utf-8")

    p = QuotaProvider(credentials_path=paths["creds"], cache_path=paths["cache"])
    with patch.object(qp_mod.urllib.request, "urlopen",
                      _mock_urlopen(exc=URLError("offline"))):
        snap = p.get()
    assert snap is not None
    assert snap.is_stale is True


# ---------------------------------------------------------------------------
# Q10: enabled=False → never reads creds, never hits network
# ---------------------------------------------------------------------------

def test_disabled_provider_returns_none_without_io(paths):
    """Q10: enabled=False → no credential read, no network call, returns None."""
    _good_creds(paths["creds"])
    p = QuotaProvider(
        credentials_path=paths["creds"], cache_path=paths["cache"], enabled=False,
    )
    with patch.object(qp_mod.urllib.request, "urlopen") as fake:
        result = p.get()
    assert result is None
    fake.assert_not_called()


# ---------------------------------------------------------------------------
# Q11: concurrent calls share one fetch
# ---------------------------------------------------------------------------

def test_concurrent_calls_serialise_through_lock(paths):
    """Q11: two threads calling get() at once → only one HTTP round trip
    fires; the second waits on the lock and then sees the cache the
    first thread just wrote."""
    _good_creds(paths["creds"])
    p = QuotaProvider(credentials_path=paths["creds"], cache_path=paths["cache"])

    call_count = 0
    in_flight_max = 0
    in_flight = 0
    counter_lock = threading.Lock()

    def slow_open(*_a, **_kw):
        nonlocal call_count, in_flight, in_flight_max
        with counter_lock:
            call_count += 1
            in_flight += 1
            in_flight_max = max(in_flight_max, in_flight)
        try:
            time.sleep(0.05)   # hold the "fetch" long enough to overlap
            cm = MagicMock()
            fake = MagicMock()
            fake.status = 200
            fake.read.return_value = json.dumps(_good_response()).encode("utf-8")
            cm.__enter__.return_value = fake
            cm.__exit__.return_value = False
            return cm
        finally:
            with counter_lock:
                in_flight -= 1

    results: list[QuotaSnapshot | None] = [None, None]

    def worker(i):
        results[i] = p.get()

    # Patch urlopen ONCE, both threads see the same fake — that's how
    # we can observe in-flight overlap if the provider's lock failed.
    with patch.object(qp_mod.urllib.request, "urlopen", side_effect=slow_open):
        t1 = threading.Thread(target=worker, args=(0,))
        t2 = threading.Thread(target=worker, args=(1,))
        t1.start()
        t2.start()
        t1.join(timeout=3)
        t2.join(timeout=3)

    # Exactly one HTTP call: the second thread finds a fresh cache and skips.
    assert call_count == 1, f"expected 1 fetch, got {call_count}"
    # No two slow_open ran concurrently (proves the in-process lock works).
    assert in_flight_max == 1
    # Both threads got a snapshot back.
    assert all(r is not None for r in results)


# ---------------------------------------------------------------------------
# Bonus: malformed responses are rejected (don't poison the cache)
# ---------------------------------------------------------------------------

def test_malformed_response_does_not_overwrite_good_cache(paths):
    """Plant a good cache, then have the endpoint return junk on refresh
    after TTL — we should keep the good cache, not poison it."""
    _good_creds(paths["creds"])
    fresh = datetime.now(timezone.utc) - timedelta(seconds=qp_mod.POLL_TTL_SECONDS + 1)
    paths["cache"].write_text(json.dumps({
        "version": _CACHE_VERSION,
        "fetched_at": fresh.isoformat(),
        "five_hour": {"utilization": 77.0, "resets_at": "2026-05-01T20:00:00+00:00"},
        "seven_day": {"utilization": 5.0, "resets_at": "2026-05-08T16:00:00+00:00"},
    }), encoding="utf-8")

    p = QuotaProvider(credentials_path=paths["creds"], cache_path=paths["cache"])
    # Endpoint returns garbage shape (no 'five_hour' key)
    with patch.object(qp_mod.urllib.request, "urlopen",
                      _mock_urlopen({"oops": "wrong shape"})):
        snap = p.get()

    # Should still have the old utilization, NOT 0/missing
    assert snap is not None
    assert snap.five_hour_pct == 77.0
