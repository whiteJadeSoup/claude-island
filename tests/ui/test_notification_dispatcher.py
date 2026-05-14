"""Tests for NotificationDispatcher (G2).

Pure-Python — no Qt needed because the dispatcher's logic is OS- and
widget-free; the only Qt thing is "must run on GUI thread", which the
production wiring layer ensures and tests don't need to verify.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from claude_island.core.notify import (
    FrontmostInfo,
    NotifyEvent,
    NotifyKind,
    new_notify_id,
)
from claude_island.core.snapshot import WorldSnapshot
from claude_island.platform_.notify import NoopNotifyBackend, NotifyKindHint
from claude_island.ui.notification_dispatcher import NotificationDispatcher


_NOW = datetime(2026, 5, 14, 12, 0, 0, tzinfo=timezone.utc)


def _evt(
    *,
    uuid: str = "u1",
    name: str = "my-session",
    when: datetime | None = None,
    kind: NotifyKind = NotifyKind.TURN_COMPLETE,
) -> NotifyEvent:
    return NotifyEvent(
        id=new_notify_id(),
        kind=kind,
        session_uuid=uuid,
        session_name=name,
        cwd_basename="proj",
        occurred_at=when or datetime.now(timezone.utc),
    )


def _snap_with(*events: NotifyEvent) -> WorldSnapshot:
    return WorldSnapshot.empty().__class__(
        today_cost_usd=0.0,
        quota=None,
        available_providers=(),
        selected_provider=None,
        fetched_at=datetime.now(timezone.utc),
        session_groups=(),
        dormant_sessions=(),
        launching_sessions=(),
        pending_decisions=(),
        notify_events=tuple(events),
    )


# ── Single event posts ───────────────────────────────────────────────


def test_single_event_calls_backend_post():
    backend = NoopNotifyBackend()
    disp = NotificationDispatcher(backend=backend)
    e = _evt()
    disp.on_snapshot(_snap_with(e))
    assert len(backend.posted_calls) == 1
    title, body, kind = backend.posted_calls[0]
    assert title == "claude-island"
    assert "my-session" in body
    assert kind == NotifyKindHint.INFO


def test_failed_event_uses_warn_kind():
    backend = NoopNotifyBackend()
    disp = NotificationDispatcher(backend=backend)
    e = _evt(kind=NotifyKind.TURN_FAILED)
    disp.on_snapshot(_snap_with(e))
    _, _, kind = backend.posted_calls[0]
    assert kind == NotifyKindHint.WARN


# ── Idempotent: re-posting same snapshot doesn't duplicate ───────────


def test_same_event_in_two_snapshots_posts_once():
    backend = NoopNotifyBackend()
    disp = NotificationDispatcher(backend=backend)
    e = _evt()
    snap = _snap_with(e)
    disp.on_snapshot(snap)
    disp.on_snapshot(snap)  # rolling window — same event still present
    assert len(backend.posted_calls) == 1


# ── Frontmost suppression ────────────────────────────────────────────


def test_island_frontmost_suppresses():
    backend = NoopNotifyBackend()
    disp = NotificationDispatcher(
        backend=backend,
        frontmost_resolver=lambda: FrontmostInfo(island_is_frontmost=True),
    )
    disp.on_snapshot(_snap_with(_evt()))
    assert backend.posted_calls == []


def test_session_terminal_frontmost_suppresses():
    backend = NoopNotifyBackend()
    disp = NotificationDispatcher(
        backend=backend,
        frontmost_resolver=lambda: FrontmostInfo(
            frontmost_terminal_pids=frozenset({1234}),
        ),
        session_terminal_pids=lambda uuid: frozenset({1234}) if uuid == "u1" else frozenset(),
    )
    disp.on_snapshot(_snap_with(_evt(uuid="u1")))
    assert backend.posted_calls == []


def test_other_session_frontmost_does_not_suppress():
    backend = NoopNotifyBackend()
    disp = NotificationDispatcher(
        backend=backend,
        frontmost_resolver=lambda: FrontmostInfo(
            frontmost_terminal_pids=frozenset({9999}),
        ),
        session_terminal_pids=lambda uuid: frozenset({1234}),
    )
    disp.on_snapshot(_snap_with(_evt(uuid="u1")))
    assert len(backend.posted_calls) == 1


# ── Coalesce ─────────────────────────────────────────────────────────


def test_three_simultaneous_events_coalesced_to_one_post():
    backend = NoopNotifyBackend()
    disp = NotificationDispatcher(backend=backend)
    e1 = _evt(uuid="u1", when=_NOW)
    e2 = _evt(uuid="u2", when=_NOW + timedelta(seconds=1))
    e3 = _evt(uuid="u3", when=_NOW + timedelta(seconds=2))
    snap = _snap_with(e1, e2, e3)
    disp.on_snapshot(snap)
    # Should have ONE post (coalesced).
    assert len(backend.posted_calls) == 1
    _, body, _ = backend.posted_calls[0]
    assert "3 turns" in body


# ── Backend exception doesn't crash dispatcher ───────────────────────


class _RaisingBackend:
    def post(self, *, title, body, kind):
        raise RuntimeError("simulated failure")


def test_backend_exception_logged_not_raised():
    disp = NotificationDispatcher(backend=_RaisingBackend())
    # Should not raise.
    disp.on_snapshot(_snap_with(_evt()))


# ── Frontmost resolver exception falls back to no-suppress ───────────


def test_frontmost_resolver_exception_does_not_suppress():
    backend = NoopNotifyBackend()
    def _bad_resolver():
        raise RuntimeError("bad")
    disp = NotificationDispatcher(
        backend=backend, frontmost_resolver=_bad_resolver,
    )
    disp.on_snapshot(_snap_with(_evt()))
    assert len(backend.posted_calls) == 1


# ── Empty notify_events is no-op ─────────────────────────────────────


def test_empty_snapshot_no_op():
    backend = NoopNotifyBackend()
    disp = NotificationDispatcher(backend=backend)
    disp.on_snapshot(WorldSnapshot.empty())
    assert backend.posted_calls == []
