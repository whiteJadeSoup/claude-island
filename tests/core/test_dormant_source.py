"""Tests for DormantSessionSource — the JSONL-backed view of offline sessions."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from claude_island.core.dormant_source import DormantSessionSource


class _FakeParser:
    def __init__(self, meta: dict[str, dict]) -> None:
        self._meta = meta

    def known_session_uuids(self) -> set[str]:
        return set(self._meta.keys())

    def get_session_metadata(self, uuid: str) -> dict:
        return dict(self._meta.get(uuid, {}))


class _FakeUsage:
    def __init__(self, summaries: dict[str, tuple[float, int, int]]) -> None:
        self._summaries = summaries

    def get_session_summary(self, uuid: str) -> tuple[float, int, int]:
        return self._summaries.get(uuid, (0.0, 0, 0))


def test_returns_dormant_for_each_complete_meta():
    parser = _FakeParser({
        "u1": {
            "cwd": "D:/projects/a",
            "last_activity": datetime(2026, 1, 1, 12, tzinfo=timezone.utc),
            "started_at": datetime(2026, 1, 1, 10, tzinfo=timezone.utc),
            "ai_title": "refactor",
            "permission_mode": "bypassPermissions",
            "git_branch": "main",
        },
        "u2": {
            "cwd": "D:/projects/b",
            "last_activity": datetime(2026, 1, 2, 12, tzinfo=timezone.utc),
        },
    })
    usage = _FakeUsage({"u1": (1.23, 5, 0), "u2": (0.0, 0, 0)})
    src = DormantSessionSource(jsonl_parser=parser, usage_registry=usage)

    sessions = {s.session_uuid: s for s in src.sessions}
    assert sessions.keys() == {"u1", "u2"}
    s1 = sessions["u1"]
    assert s1.cwd == Path("D:/projects/a")
    assert s1.name == "refactor"
    assert s1.permission_mode == "bypassPermissions"
    assert s1.git_branch == "main"
    assert s1.cost_usd == 1.23
    assert s1.turn_count == 5
    s2 = sessions["u2"]
    assert s2.name is None
    assert s2.permission_mode is None
    assert s2.cost_usd == 0.0


def test_filters_sessions_without_cwd():
    """A transcript not yet parsed past the first cwd-bearing row → drop.
    Resume needs cwd; UI shouldn't surface a row the user can't act on."""
    parser = _FakeParser({
        "incomplete": {
            "last_activity": datetime(2026, 1, 1, tzinfo=timezone.utc),
            # no cwd
        },
    })
    src = DormantSessionSource(jsonl_parser=parser, usage_registry=_FakeUsage({}))
    assert src.sessions == []


def test_filters_sessions_without_last_activity():
    """No timestamp → no sort key + likely empty transcript → drop."""
    parser = _FakeParser({
        "no-ts": {
            "cwd": "D:/projects/a",
            # no last_activity
        },
    })
    src = DormantSessionSource(jsonl_parser=parser, usage_registry=_FakeUsage({}))
    assert src.sessions == []


def test_usage_registry_failure_degrades_to_zero_cost():
    """If UsageRegistry raises (transient lock contention etc), the dormant
    card still renders with cost=0 — the user can still see + Resume it."""
    class _BoomUsage:
        def get_session_summary(self, uuid):
            raise RuntimeError("boom")
    parser = _FakeParser({
        "u": {
            "cwd": "D:/projects/a",
            "last_activity": datetime(2026, 1, 1, tzinfo=timezone.utc),
        },
    })
    src = DormantSessionSource(jsonl_parser=parser, usage_registry=_BoomUsage())
    sessions = src.sessions
    assert len(sessions) == 1
    assert sessions[0].cost_usd == 0.0
    assert sessions[0].turn_count == 0


def test_meta_provider_failure_skips_uuid():
    """Per-uuid metadata read failure shouldn't kill the whole listing."""
    class _PartialParser:
        def known_session_uuids(self):
            return {"good", "bad"}
        def get_session_metadata(self, uuid):
            if uuid == "bad":
                raise RuntimeError("disk error")
            return {
                "cwd": "D:/projects/a",
                "last_activity": datetime(2026, 1, 1, tzinfo=timezone.utc),
            }
    src = DormantSessionSource(jsonl_parser=_PartialParser(), usage_registry=_FakeUsage({}))
    sessions = src.sessions
    assert len(sessions) == 1
    assert sessions[0].session_uuid == "good"


def test_excludes_subagent_sessions():
    """Agent tool spawns child sessions whose UUIDs start with "agent-".
    These are not independently resumable and would duplicate the parent
    session's cwd in the History drawer — filter them out."""
    parser = _FakeParser({
        "normal-uuid-1234": {
            "cwd": "D:/projects/a",
            "last_activity": datetime(2026, 1, 1, tzinfo=timezone.utc),
        },
        "agent-a0036c4c57719dd43": {
            "cwd": "D:/projects/a",  # same cwd as parent
            "last_activity": datetime(2026, 1, 1, tzinfo=timezone.utc),
        },
        "agent-acompact-0340f62e4527adf0": {
            "cwd": "D:/projects/a",
            "last_activity": datetime(2026, 1, 1, tzinfo=timezone.utc),
        },
    })
    src = DormantSessionSource(jsonl_parser=parser, usage_registry=_FakeUsage({}))
    sessions = src.sessions
    assert [s.session_uuid for s in sessions] == ["normal-uuid-1234"]
