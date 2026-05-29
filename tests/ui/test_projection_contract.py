from datetime import datetime, timezone
from pathlib import Path
from claude_island.core.snapshot import WorldSnapshot, SessionGroup, SessionView
from claude_island.core.session_phase import SessionPhase
from claude_island.core.models import Session
from claude_island.ui.snapshot_projection import project_snapshot

TOP = {"today_cost_usd", "quota", "sessions", "decisions", "recents"}
SESSION_BASE = {"id", "name", "phase", "cwd", "cost_usd", "is_high_cost", "model",
                "tokens_per_min", "current_tool_input", "command", "turn_count", "elapsed_s"}


def _snap():
    sess = Session(pid=1, project_path=Path("D:/x"),
                   last_activity=datetime.now(timezone.utc), session_uuid="u1")
    v = SessionView(pid=1, name="x", project_path=Path("D:/x"), project_basename="x",
        last_activity=datetime.now(timezone.utc), cost_usd=12.0, is_high_cost=False,
        latest_model="opus-4.7", status_word=None, session=sess, session_uuid="u1",
        phase=SessionPhase.TOOL_USE, turn_count=7,
        current_tool_input="npm run build", tokens_per_min=4585, tool_elapsed_s=134.0)
    return WorldSnapshot(today_cost_usd=1.0, quota=None, available_providers=("anthropic",),
        selected_provider="anthropic", fetched_at=datetime.now(timezone.utc),
        session_groups=(SessionGroup(group_id="g", title_hint=None, adapter_id="", views=(v,)),))


def test_top_level_contract():
    out = project_snapshot(_snap())
    assert TOP.issubset(set(out)), f"missing top keys: {TOP - set(out)}"
    assert isinstance(out["sessions"], list) and isinstance(out["decisions"], list)
    assert isinstance(out["recents"], list)


def test_session_contract():
    s = project_snapshot(_snap())["sessions"][0]
    assert SESSION_BASE.issubset(set(s)), f"missing session keys: {SESSION_BASE - set(s)}"
    assert s["command"] == "npm run build"
    assert s["tokens_per_min"] == 4585
    assert s["elapsed_s"] == 134
    assert isinstance(s["cost_usd"], float) and isinstance(s["turn_count"], int)
