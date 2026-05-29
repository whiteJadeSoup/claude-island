from datetime import datetime, timezone
from pathlib import Path
from claude_island.core.snapshot import WorldSnapshot, SessionGroup, SessionView
from claude_island.core.session_phase import SessionPhase
from claude_island.core.models import Session
from claude_island.ui.snapshot_projection import project_snapshot, _fmt_model, _epoch_ms


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
    # _view() passes latest_model="opus-4.7" which has no "claude-" prefix;
    # _fmt_model falls through to the fallback (raw[:14]) — stays "opus-4.7".
    assert names["cc-learning"]["model"] == "opus-4.7"
    assert names["build-mini"]["phase"] == "idle"
    assert d["decisions"] == []


# ---------------------------------------------------------------------------
# _fmt_model unit tests (Bug 4)
# ---------------------------------------------------------------------------

def test_fmt_model_opus_4_7():
    assert _fmt_model("claude-opus-4-7") == "opus-4.7"


def test_fmt_model_sonnet_4_6():
    assert _fmt_model("claude-sonnet-4-6") == "sonnet-4.6"


def test_fmt_model_haiku_3_5():
    assert _fmt_model("claude-haiku-3-5") == "haiku-3.5"


def test_fmt_model_haiku_3_major_only():
    assert _fmt_model("claude-haiku-3") == "haiku-3"


def test_epoch_ms_from_datetime():
    dt = datetime(2026, 5, 29, 13, 30, 0, tzinfo=timezone.utc)
    assert _epoch_ms(dt) == int(dt.timestamp() * 1000)


def test_epoch_ms_none_returns_zero():
    # 0 is the "unknown" sentinel QML reads as "resets in —".
    assert _epoch_ms(None) == 0


def test_epoch_ms_bad_input_returns_zero():
    assert _epoch_ms("not-a-datetime") == 0


def test_quota_projection_emits_reset_epoch():
    # A real QuotaSnapshot's reset datetimes must surface as *_reset_epoch ms.
    from claude_island.core.models import QuotaSnapshot
    reset5h = datetime(2026, 5, 29, 13, 30, 0, tzinfo=timezone.utc)
    reset7d = datetime(2026, 6, 2, 13, 30, 0, tzinfo=timezone.utc)
    q = QuotaSnapshot(
        provider="anthropic", five_hour_pct=56, seven_day_pct=21,
        five_hour_resets_at=reset5h, seven_day_resets_at=reset7d,
        fetched_at=datetime.now(timezone.utc), is_stale=False,
    )
    snap = WorldSnapshot(
        today_cost_usd=1.0, quota=q, available_providers=("anthropic",),
        selected_provider="anthropic", fetched_at=datetime.now(timezone.utc),
        session_groups=(),
    )
    out = project_snapshot(snap)["quota"]
    assert out["five_hour_reset_epoch"] == int(reset5h.timestamp() * 1000)
    assert out["weekly_reset_epoch"] == int(reset7d.timestamp() * 1000)


def test_fmt_model_none_returns_none():
    assert _fmt_model(None) is None


def test_fmt_model_empty_returns_none():
    assert _fmt_model("") is None


def test_fmt_model_already_short_falls_through():
    # Already a short label without the "claude-" prefix — falls to raw[:14].
    assert _fmt_model("opus-4.7") == "opus-4.7"


def test_fmt_model_applied_in_projection():
    """project_snapshot must apply _fmt_model to the session's model field."""
    sess = Session(pid=1, project_path=Path("D:/x"),
                   last_activity=datetime.now(timezone.utc), session_uuid="u-raw")
    view = SessionView(
        pid=1, name="raw-model-test", project_path=Path("D:/x"), project_basename="x",
        last_activity=datetime.now(timezone.utc), cost_usd=0.0,
        is_high_cost=False, latest_model="claude-opus-4-7", status_word=None,
        session=sess, session_uuid="u-raw", phase=SessionPhase.IDLE, turn_count=0,
    )
    snap = WorldSnapshot(
        today_cost_usd=0.0, quota=None, available_providers=(),
        selected_provider=None, fetched_at=datetime.now(timezone.utc),
        session_groups=(SessionGroup("g", None, "", (view,)),),
    )
    d = project_snapshot(snap)
    assert d["sessions"][0]["model"] == "opus-4.7"
