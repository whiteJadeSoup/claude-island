"""Tests for SessionRegistry — emit semantics + project_hash format.

Per-session JSONL activity is no longer merged here (it lives on
``JsonlParser._session_meta`` keyed by uuid and is folded in by
``compose_session_view``). The registry is now a thin store of the
scanner's output with a "skip emit on no-op update" optimisation.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

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
# Emit semantics: skip no-op updates, fire on real changes.
# --------------------------------------------------------------------------

def test_update_emits_every_tick_even_when_unchanged():
    """update() emits sessions_changed on every call so HookSessionBridge
    can advance its scanner-miss counter when the session list isn't
    shape-changing. The Snapshotter pipeline downstream dedupes via
    distinct_until_changed on WorldSnapshot."""
    reg = SessionRegistry()
    s = _session(pid=1, cwd="/home/x",
                 started=datetime(2025, 1, 1, 9, 0, tzinfo=timezone.utc))

    received: list[list[Session]] = []
    reg.sessions_changed.subscribe(received.append)

    reg.update([s])
    reg.update([s])
    reg.update([s])

    # Three calls → three emits
    assert len(received) == 3


def test_update_emits_when_session_added():
    reg = SessionRegistry()
    s1 = _session(pid=1, cwd="/a", started=datetime(2025, 1, 1, tzinfo=timezone.utc))
    s2 = _session(pid=2, cwd="/b", started=datetime(2025, 1, 1, tzinfo=timezone.utc))

    received: list[list[Session]] = []
    reg.sessions_changed.subscribe(received.append)

    reg.update([s1])
    reg.update([s1, s2])  # added one

    assert len(received) == 2


def test_update_emits_when_session_removed():
    reg = SessionRegistry()
    s1 = _session(pid=1, cwd="/a", started=datetime(2025, 1, 1, tzinfo=timezone.utc))
    s2 = _session(pid=2, cwd="/b", started=datetime(2025, 1, 1, tzinfo=timezone.utc))

    received: list[list[Session]] = []
    reg.sessions_changed.subscribe(received.append)

    reg.update([s1, s2])
    reg.update([s1])

    assert len(received) == 2


def test_sessions_property_returns_current_list():
    reg = SessionRegistry()
    s = _session(pid=1, cwd="/home/x",
                 started=datetime(2025, 1, 1, tzinfo=timezone.utc))
    reg.update([s])
    assert [x.pid for x in reg.sessions] == [1]
