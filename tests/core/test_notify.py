"""Tests for NotifyDispatchPolicy (G2) — pure decision rules.

Test plan mirrors Detail Design §7 (T2.x cells):
  T2.1 happy   — single event → action=post with formatted body
  T2.2 edge    — frontmost suppression (island OR target terminal)
  T2.3 edge    — coalesce N≥3 same-kind in 5s window
  T2.4 edge    — debounce 3s same session
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from claude_island.core.notify import (
    COALESCE_MIN_COUNT,
    COALESCE_WINDOW_S,
    DEBOUNCE_PER_SESSION_S,
    DispatchRecord,
    FrontmostInfo,
    NotifyDispatchPolicy,
    NotifyEvent,
    NotifyKind,
    make_turn_complete,
    new_notify_id,
)


# ── Fixtures / helpers ───────────────────────────────────────────────


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
        occurred_at=when or _NOW,
    )


@pytest.fixture
def policy() -> NotifyDispatchPolicy:
    return NotifyDispatchPolicy()


# ── Single post ──────────────────────────────────────────────────────


class TestSinglePost:
    def test_default_posts_with_formatted_body(self, policy):
        e = _evt(name="my-session")
        d = policy.evaluate(e, recent=[], frontmost=FrontmostInfo())
        assert d.action == "post"
        assert d.title == "claude-island"
        assert "my-session" in d.body
        assert "turn complete" in d.body
        assert d.coalesced_ids == (e.id,)

    def test_failure_kind_uses_failure_text(self, policy):
        e = _evt(name="bad-session", kind=NotifyKind.TURN_FAILED)
        d = policy.evaluate(e, recent=[], frontmost=FrontmostInfo())
        assert d.action == "post"
        assert "bad-session" in d.body
        assert "turn failed" in d.body

    def test_falls_back_to_cwd_basename_when_no_name(self, policy):
        e = NotifyEvent(
            id=new_notify_id(),
            kind=NotifyKind.TURN_COMPLETE,
            session_uuid="u",
            session_name="",
            cwd_basename="my-proj",
            occurred_at=_NOW,
        )
        d = policy.evaluate(e, recent=[], frontmost=FrontmostInfo())
        assert "my-proj" in d.body


# ── Frontmost suppression ────────────────────────────────────────────


class TestFrontmostSuppress:
    def test_island_frontmost_drops(self, policy):
        e = _evt()
        d = policy.evaluate(
            e,
            recent=[],
            frontmost=FrontmostInfo(island_is_frontmost=True),
        )
        assert d.action == "drop"
        assert "frontmost" in d.reason

    def test_session_terminal_frontmost_drops(self, policy):
        e = _evt(uuid="u1")
        d = policy.evaluate(
            e,
            recent=[],
            frontmost=FrontmostInfo(frontmost_terminal_pids=frozenset({1234})),
            session_terminal_pids=frozenset({1234}),
        )
        assert d.action == "drop"

    def test_other_session_terminal_frontmost_does_not_drop(self, policy):
        # User is in a DIFFERENT terminal; this session's notification
        # should still come through.
        e = _evt(uuid="u1")
        d = policy.evaluate(
            e,
            recent=[],
            frontmost=FrontmostInfo(frontmost_terminal_pids=frozenset({9999})),
            session_terminal_pids=frozenset({1234}),
        )
        assert d.action == "post"

    def test_no_terminal_pid_info_does_not_drop(self, policy):
        # When we can't detect (e.g. find_ui_app_ancestor returned None),
        # err on the side of notifying.
        e = _evt()
        d = policy.evaluate(
            e,
            recent=[],
            frontmost=FrontmostInfo(),
            session_terminal_pids=frozenset(),
        )
        assert d.action == "post"


# ── Debounce ─────────────────────────────────────────────────────────


class TestDebounce:
    def test_drop_within_debounce_window(self, policy):
        e = _evt(uuid="u1", when=_NOW + timedelta(seconds=1.0))
        recent = [DispatchRecord(
            session_uuid="u1",
            posted_at=_NOW,
            notify_id="prev",
        )]
        d = policy.evaluate(e, recent=recent, frontmost=FrontmostInfo())
        assert d.action == "drop"
        assert "debounced" in d.reason

    def test_post_after_debounce_window(self, policy):
        # > 3s window
        e = _evt(uuid="u1", when=_NOW + timedelta(seconds=DEBOUNCE_PER_SESSION_S + 0.5))
        recent = [DispatchRecord(
            session_uuid="u1",
            posted_at=_NOW,
            notify_id="prev",
        )]
        d = policy.evaluate(e, recent=recent, frontmost=FrontmostInfo())
        assert d.action == "post"

    def test_other_session_within_window_does_not_debounce(self, policy):
        # Debounce is per-session.
        e = _evt(uuid="u1", when=_NOW + timedelta(seconds=1.0))
        recent = [DispatchRecord(
            session_uuid="u2",
            posted_at=_NOW,
            notify_id="prev",
        )]
        d = policy.evaluate(e, recent=recent, frontmost=FrontmostInfo())
        assert d.action == "post"


# ── Coalesce ─────────────────────────────────────────────────────────


class TestCoalesce:
    def test_three_within_window_coalesce(self, policy):
        # COALESCE_MIN_COUNT=3, COALESCE_WINDOW=5s
        e1 = _evt(uuid="u1", when=_NOW)
        e2 = _evt(uuid="u2", when=_NOW + timedelta(seconds=1))
        e3 = _evt(uuid="u3", when=_NOW + timedelta(seconds=2))
        # Evaluate the latest (e3) with e1, e2 as siblings.
        d = policy.evaluate(
            e3,
            recent=[],
            frontmost=FrontmostInfo(),
            sibling_events=[e1, e2],
        )
        assert d.action == "post"
        assert "coalesced" in d.reason
        assert "3 turns" in d.body
        assert set(d.coalesced_ids) == {e1.id, e2.id, e3.id}

    def test_two_within_window_does_not_coalesce(self, policy):
        e1 = _evt(uuid="u1", when=_NOW)
        e2 = _evt(uuid="u2", when=_NOW + timedelta(seconds=1))
        d = policy.evaluate(
            e2,
            recent=[],
            frontmost=FrontmostInfo(),
            sibling_events=[e1],
        )
        assert d.action == "post"
        assert d.reason == "single"

    def test_old_siblings_outside_window_excluded(self, policy):
        # Old event > 5s before; coalesce treats only recent ones.
        e_old = _evt(uuid="u1", when=_NOW)
        e1 = _evt(uuid="u2", when=_NOW + timedelta(seconds=10))
        e2 = _evt(uuid="u3", when=_NOW + timedelta(seconds=11))
        e3 = _evt(uuid="u4", when=_NOW + timedelta(seconds=12))
        # Evaluate e3; window is [e3.when - 5s, e3.when]; e_old is way out.
        d = policy.evaluate(
            e3,
            recent=[],
            frontmost=FrontmostInfo(),
            sibling_events=[e_old, e1, e2],
        )
        # 3 in window (e1, e2, e3) → coalesce
        assert d.action == "post"
        assert "3 turns" in d.body
        assert e_old.id not in d.coalesced_ids

    def test_different_kinds_not_coalesced_together(self, policy):
        # TURN_COMPLETE doesn't coalesce with TURN_FAILED.
        e1 = _evt(uuid="u1", kind=NotifyKind.TURN_COMPLETE, when=_NOW)
        e2 = _evt(uuid="u2", kind=NotifyKind.TURN_COMPLETE, when=_NOW + timedelta(seconds=1))
        e3 = _evt(uuid="u3", kind=NotifyKind.TURN_FAILED, when=_NOW + timedelta(seconds=2))
        # Evaluate e3 (failed); siblings are completes — not same kind.
        d = policy.evaluate(
            e3,
            recent=[],
            frontmost=FrontmostInfo(),
            sibling_events=[e1, e2],
        )
        # Only 1 in cluster (e3 alone); not coalesced.
        assert d.reason == "single"


# ── Helpers ──────────────────────────────────────────────────────────


class TestHelpers:
    def test_make_turn_complete_defaults(self):
        e = make_turn_complete(
            session_uuid="u1", session_name="x", cwd_basename="proj",
        )
        assert e.kind is NotifyKind.TURN_COMPLETE
        assert e.session_uuid == "u1"

    def test_make_turn_complete_failure(self):
        e = make_turn_complete(
            session_uuid="u1", session_name="x", cwd_basename="proj",
            is_failure=True,
        )
        assert e.kind is NotifyKind.TURN_FAILED

    def test_new_notify_id_unique(self):
        ids = {new_notify_id() for _ in range(1000)}
        assert len(ids) == 1000


# ── Constants ────────────────────────────────────────────────────────


class TestConstants:
    def test_coalesce_window(self):
        assert COALESCE_WINDOW_S == 5.0
        assert COALESCE_MIN_COUNT == 3

    def test_debounce_window(self):
        assert DEBOUNCE_PER_SESSION_S == 3.0
