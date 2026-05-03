"""Tests for SessionRegistry — particularly the activity-override path
that joins JSONL parser updates to scanned sessions (B5)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from claude_island.core.models import Session, project_hash
from claude_island.core.session_registry import SessionRegistry


def _session(pid: int, cwd: str, started: datetime) -> Session:
    return Session(
        pid=pid,
        project_path=Path(cwd),
        session_uuid="",
        last_activity=started,
    )


# --------------------------------------------------------------------------
# project_hash matches Claude Code's actual encoding
# --------------------------------------------------------------------------

def test_project_hash_matches_claude_code_format():
    # The Claude Code project dir for "D:\coding projects\common-learn" on
    # the maintainer's box is "D--coding-projects-common-learn".
    assert project_hash("D:\\coding projects\\common-learn") == "D--coding-projects-common-learn"


def test_project_hash_preserves_dots_underscores_alphanumeric():
    assert project_hash("/home/user/my.project_v2") == "-home-user-my.project_v2"


# --------------------------------------------------------------------------
# B5: activity overrides
# --------------------------------------------------------------------------

def test_update_activity_does_not_emit():
    """update_activity must store the override silently — it should not fire
    sessions_changed every time a JSONL line arrives. Otherwise an active
    session producing 5 turns/sec floods the UI."""
    reg = SessionRegistry()
    received = []
    reg.sessions_changed.subscribe(received.append)

    reg.update_activity(("D--foo", datetime(2025, 1, 1, tzinfo=timezone.utc)))
    reg.update_activity(("D--bar", datetime(2025, 1, 2, tzinfo=timezone.utc)))

    assert received == []  # no emits


def test_update_emits_with_overrides_applied():
    reg = SessionRegistry()
    started = datetime(2025, 1, 1, 9, 0, tzinfo=timezone.utc)
    activity = datetime(2025, 1, 1, 14, 30, tzinfo=timezone.utc)

    s = _session(pid=123, cwd="D:\\foo bar", started=started)

    received: list[list[Session]] = []
    reg.sessions_changed.subscribe(received.append)

    reg.update_activity((project_hash("D:\\foo bar"), activity))
    reg.update([s])

    assert len(received) == 1
    enriched = received[0]
    assert len(enriched) == 1
    assert enriched[0].pid == 123
    assert enriched[0].last_activity == activity  # overridden


def test_override_only_applied_when_newer():
    reg = SessionRegistry()
    started = datetime(2025, 1, 1, 14, 0, tzinfo=timezone.utc)
    earlier_activity = datetime(2025, 1, 1, 9, 0, tzinfo=timezone.utc)

    s = _session(pid=123, cwd="/home/x", started=started)
    reg.update_activity((project_hash("/home/x"), earlier_activity))

    received: list[list[Session]] = []
    reg.sessions_changed.subscribe(received.append)
    reg.update([s])

    # Process started at 14:00, activity recorded at 09:00 — activity is older,
    # so the session keeps its start time.
    assert received[0][0].last_activity == started


def test_update_activity_keeps_latest_per_project():
    reg = SessionRegistry()
    proj = project_hash("/home/x")

    older = datetime(2025, 1, 1, 9, 0, tzinfo=timezone.utc)
    newer = datetime(2025, 1, 1, 14, 0, tzinfo=timezone.utc)

    # Simulate out-of-order arrival
    reg.update_activity((proj, newer))
    reg.update_activity((proj, older))  # older shouldn't overwrite

    s = _session(pid=1, cwd="/home/x", started=datetime(2025, 1, 1, 8, 0, tzinfo=timezone.utc))
    received: list[list[Session]] = []
    reg.sessions_changed.subscribe(received.append)
    reg.update([s])

    assert received[0][0].last_activity == newer


def test_multiple_sessions_same_project_all_get_override():
    """Two parallel claude sessions in the same cwd: both should reflect
    the project's most recent activity."""
    reg = SessionRegistry()
    proj = project_hash("/home/x")
    activity = datetime(2025, 1, 1, 12, 0, tzinfo=timezone.utc)

    s1 = _session(pid=1, cwd="/home/x", started=datetime(2025, 1, 1, 9, 0, tzinfo=timezone.utc))
    s2 = _session(pid=2, cwd="/home/x", started=datetime(2025, 1, 1, 10, 0, tzinfo=timezone.utc))

    reg.update_activity((proj, activity))

    received: list[list[Session]] = []
    reg.sessions_changed.subscribe(received.append)
    reg.update([s1, s2])

    assert all(s.last_activity == activity for s in received[0])


def test_session_for_unrelated_project_not_overridden():
    reg = SessionRegistry()
    started = datetime(2025, 1, 1, 9, 0, tzinfo=timezone.utc)

    reg.update_activity((project_hash("/other"), datetime(2025, 1, 2, tzinfo=timezone.utc)))

    s = _session(pid=1, cwd="/home/x", started=started)
    received: list[list[Session]] = []
    reg.sessions_changed.subscribe(received.append)
    reg.update([s])

    assert received[0][0].last_activity == started  # untouched


# --------------------------------------------------------------------------
# B6: skip emit when content unchanged (companion fix to ExpandedWindow diff)
# --------------------------------------------------------------------------

def test_update_with_identical_sessions_emits_only_once():
    """Scanner ticks every ~10s with usually identical content. The redundant
    emits force every UI subscriber to re-diff; skipping them at the source
    is a free win."""
    reg = SessionRegistry()
    s = _session(pid=1, cwd="/home/x",
                 started=datetime(2025, 1, 1, 9, 0, tzinfo=timezone.utc))

    received: list[list[Session]] = []
    reg.sessions_changed.subscribe(received.append)

    reg.update([s])
    reg.update([s])
    reg.update([s])

    assert len(received) == 1


def test_update_emits_when_session_added():
    reg = SessionRegistry()
    s1 = _session(pid=1, cwd="/a", started=datetime(2025, 1, 1, tzinfo=timezone.utc))
    s2 = _session(pid=2, cwd="/b", started=datetime(2025, 1, 1, tzinfo=timezone.utc))

    received: list[list[Session]] = []
    reg.sessions_changed.subscribe(received.append)

    reg.update([s1])
    reg.update([s1, s2])  # added one

    assert len(received) == 2


def test_update_emits_when_activity_override_changes_enriched_content():
    reg = SessionRegistry()
    s = _session(pid=1, cwd="/home/x",
                 started=datetime(2025, 1, 1, 9, 0, tzinfo=timezone.utc))

    received: list[list[Session]] = []
    reg.sessions_changed.subscribe(received.append)

    reg.update([s])  # emit 1
    reg.update_activity((project_hash("/home/x"),
                         datetime(2025, 1, 1, 14, 0, tzinfo=timezone.utc)))
    reg.update([s])  # enriched changed (last_activity now 14:00) → emit 2

    assert len(received) == 2
    assert received[0][0].last_activity.hour == 9
    assert received[1][0].last_activity.hour == 14
