"""Tests for LaunchIntentRegistry — pending Resume tracking + reconcile.

Ground rules verified:
* add → snapshot → reconcile (no upgrade, no timeout) keeps the intent
* uuid in live_uuids → reconcile drops it (upgrade)
* now - requested_at > ttl → reconcile drops it (timeout)
* duplicate add for same uuid overwrites the previous intent
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from claude_island.core.launch_intent import LaunchIntent, LaunchIntentRegistry


def _intent(uuid: str = "u1", ts: datetime | None = None) -> LaunchIntent:
    return LaunchIntent(
        session_uuid=uuid,
        cwd=Path("D:/projects/a"),
        flags=("--dangerously-skip-permissions",),
        terminal_name="windows-terminal",
        terminal_pid=1234,
        requested_at=ts or datetime(2026, 1, 1, 12, tzinfo=timezone.utc),
    )


def test_add_then_snapshot_returns_intent():
    reg = LaunchIntentRegistry()
    reg.add(_intent("u1"))
    snap = reg.snapshot()
    assert len(snap) == 1
    assert snap[0].session_uuid == "u1"


def test_reconcile_keeps_intent_when_neither_live_nor_expired():
    reg = LaunchIntentRegistry(ttl_seconds=30.0)
    ts = datetime(2026, 1, 1, 12, tzinfo=timezone.utc)
    reg.add(_intent("u1", ts=ts))
    # Same instant — not expired
    reg.reconcile(live_uuids=set(), now=ts)
    assert len(reg.snapshot()) == 1


def test_reconcile_discards_intent_when_uuid_appears_live():
    """The "upgrade" path: ProcessScanner saw the new claude.exe, so the
    intent has done its job and should disappear from launching state."""
    reg = LaunchIntentRegistry(ttl_seconds=30.0)
    reg.add(_intent("u1"))
    reg.reconcile(
        live_uuids={"u1"},
        now=datetime(2026, 1, 1, 12, tzinfo=timezone.utc),
    )
    assert reg.snapshot() == ()


def test_reconcile_discards_intent_after_ttl():
    """The "timeout" path: 30s passed and claude.exe never appeared —
    user will see the row come back to dormant + a toast."""
    reg = LaunchIntentRegistry(ttl_seconds=30.0)
    ts = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    reg.add(_intent("u1", ts=ts))
    # 31s later — past ttl
    reg.reconcile(live_uuids=set(), now=ts + timedelta(seconds=31))
    assert reg.snapshot() == ()


def test_reconcile_keeps_intent_inside_ttl():
    reg = LaunchIntentRegistry(ttl_seconds=30.0)
    ts = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    reg.add(_intent("u1", ts=ts))
    reg.reconcile(live_uuids=set(), now=ts + timedelta(seconds=29))
    assert len(reg.snapshot()) == 1


def test_duplicate_add_overwrites_previous():
    """User double-clicks Resume → the second click (with possibly
    different terminal_pid) replaces the first."""
    reg = LaunchIntentRegistry()
    reg.add(_intent("u1"))
    second = LaunchIntent(
        session_uuid="u1",
        cwd=Path("D:/projects/a"),
        flags=("--dangerously-skip-permissions",),
        terminal_name="windows-terminal",
        terminal_pid=9999,  # ← different
        requested_at=datetime(2026, 1, 1, 13, tzinfo=timezone.utc),
    )
    reg.add(second)
    snap = reg.snapshot()
    assert len(snap) == 1
    assert snap[0].terminal_pid == 9999


def test_discard_is_idempotent():
    reg = LaunchIntentRegistry()
    reg.add(_intent("u1"))
    reg.discard("u1")
    reg.discard("u1")  # second call must not raise
    reg.discard("nonexistent")
    assert reg.snapshot() == ()


def test_snapshot_orders_by_requested_at_desc():
    """Newest intents first so HistoryDrawer can render them with
    minimal layout reshuffling as new ones arrive."""
    reg = LaunchIntentRegistry()
    reg.add(_intent("old", ts=datetime(2026, 1, 1, 10, tzinfo=timezone.utc)))
    reg.add(_intent("new", ts=datetime(2026, 1, 1, 12, tzinfo=timezone.utc)))
    reg.add(_intent("mid", ts=datetime(2026, 1, 1, 11, tzinfo=timezone.utc)))
    uuids = [i.session_uuid for i in reg.snapshot()]
    assert uuids == ["new", "mid", "old"]
