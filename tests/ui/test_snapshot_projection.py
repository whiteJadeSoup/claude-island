from datetime import datetime, timezone
from pathlib import Path
from claude_island.core.snapshot import WorldSnapshot, SessionGroup, SessionView
from claude_island.core.session_phase import SessionPhase
from claude_island.core.models import Session
from claude_island.ui.snapshot_projection import project_snapshot


def _view(name, phase, cost):
    sess = Session(pid=123, project_path=Path("D:/x"),
                   last_activity=datetime.now(timezone.utc), session_uuid="u-" + name)
    return SessionView(
        pid=123, name=name, project_path=Path("D:/x"), project_basename="x",
        last_activity=datetime.now(timezone.utc), cost_usd=cost,
        is_high_cost=cost >= 50.0, latest_model="opus-4.7", status_word=None,
        session=sess, session_uuid="u-" + name, phase=phase, turn_count=7,
    )


def test_project_snapshot_shapes_sessions_and_decisions():
    snap = WorldSnapshot(
        today_cost_usd=9.28, quota=None, available_providers=("anthropic",),
        selected_provider="anthropic", fetched_at=datetime.now(timezone.utc),
        session_groups=(
            SessionGroup(group_id="g1", title_hint=None, adapter_id="",
                         views=(_view("cc-learning", SessionPhase.THINKING, 227.0),
                                _view("build-mini", SessionPhase.IDLE, 0.0))),
        ),
    )
    d = project_snapshot(snap)
    assert d["today_cost_usd"] == 9.28
    assert d["quota"] is None
    names = {s["name"]: s for s in d["sessions"]}
    assert names["cc-learning"]["phase"] == "thinking"
    assert names["cc-learning"]["cost_usd"] == 227.0
    assert names["cc-learning"]["model"] == "opus-4.7"
    assert names["build-mini"]["phase"] == "idle"
    assert d["decisions"] == []
