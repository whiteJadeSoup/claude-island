"""Tests for Plan 3 Task 1: extended quota projection, recents, spendDetail slot.
Also covers R1 Deliverable 3: sessionDetail() slot.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from PySide6.QtCore import QCoreApplication

from claude_island.core.models import (
    DormantSession,
    ModelTotals,
    QuotaSnapshot,
    Session,
    SessionDetails,
    UsageTotals,
)
from claude_island.core.session_phase import SessionPhase
from claude_island.core.snapshot import (
    SessionGroup,
    SessionView,
    WorldSnapshot,
)
from claude_island.ui.snapshot_projection import project_snapshot
from claude_island.ui.world_view_model import WorldViewModel

_app = QCoreApplication.instance() or QCoreApplication([])

_NOW = datetime(2026, 5, 28, 10, 0, 0, tzinfo=timezone.utc)
_RESET_5H = datetime(2026, 5, 28, 12, 0, 0, tzinfo=timezone.utc)
_RESET_7D = datetime(2026, 6, 4, 10, 0, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _minimal_snap(*, quota=None, dormant=()):
    """A WorldSnapshot with no live sessions, used to test quota/recents."""
    return WorldSnapshot(
        today_cost_usd=0.0,
        quota=quota,
        available_providers=("anthropic",),
        selected_provider="anthropic",
        fetched_at=_NOW,
        session_groups=(),
        dormant_sessions=tuple(dormant),
    )


def _real_quota():
    return QuotaSnapshot(
        five_hour_pct=42.0,
        five_hour_resets_at=_RESET_5H,
        seven_day_pct=17.5,
        seven_day_resets_at=_RESET_7D,
        fetched_at=_NOW,
        is_stale=False,
    )


def _dormant_session(name, uuid="d-uuid-1", cost=3.14):
    return DormantSession(
        session_uuid=uuid,
        cwd=Path("D:/projects/myapp"),
        name=name,
        last_prompt="fix the bug",
        last_activity=_NOW,
        started_at=_NOW,
        permission_mode="default",
        git_branch="main",
        cost_usd=cost,
        turn_count=5,
    )


# ---------------------------------------------------------------------------
# quota projection tests
# ---------------------------------------------------------------------------

def test_quota_none_when_snap_quota_is_none():
    d = project_snapshot(_minimal_snap(quota=None))
    assert d["quota"] is None


def test_quota_has_five_hour_pct():
    d = project_snapshot(_minimal_snap(quota=_real_quota()))
    assert d["quota"]["five_hour_pct"] == 42


def test_quota_has_weekly_pct():
    d = project_snapshot(_minimal_snap(quota=_real_quota()))
    assert d["quota"]["weekly_pct"] == 17


def test_quota_has_five_hour_reset():
    d = project_snapshot(_minimal_snap(quota=_real_quota()))
    # five_hour_reset is a stringified datetime — just check it's a non-empty string
    assert isinstance(d["quota"]["five_hour_reset"], str)
    assert d["quota"]["five_hour_reset"] != ""


def test_quota_has_weekly_reset():
    d = project_snapshot(_minimal_snap(quota=_real_quota()))
    assert isinstance(d["quota"]["weekly_reset"], str)
    assert d["quota"]["weekly_reset"] != ""


# ---------------------------------------------------------------------------
# recents projection tests
# ---------------------------------------------------------------------------

def test_recents_empty_when_no_dormant():
    d = project_snapshot(_minimal_snap())
    assert d["recents"] == []


def test_recents_length():
    dormant = [_dormant_session("proj-a", "u1"), _dormant_session("proj-b", "u2")]
    d = project_snapshot(_minimal_snap(dormant=dormant))
    assert len(d["recents"]) == 2


def test_recents_has_name_and_uuid():
    dormant = [_dormant_session("proj-a", "uuid-42")]
    d = project_snapshot(_minimal_snap(dormant=dormant))
    rec = d["recents"][0]
    assert rec["name"] == "proj-a"
    assert rec["session_uuid"] == "uuid-42"


def test_recents_has_cwd_and_cost():
    dormant = [_dormant_session("proj-a", cost=7.77)]
    d = project_snapshot(_minimal_snap(dormant=dormant))
    rec = d["recents"][0]
    assert "myapp" in rec["cwd"]
    assert abs(rec["cost_usd"] - 7.77) < 1e-9


def test_recents_last_seen_is_string():
    dormant = [_dormant_session("proj-a")]
    d = project_snapshot(_minimal_snap(dormant=dormant))
    assert isinstance(d["recents"][0]["last_seen"], str)
    assert d["recents"][0]["last_seen"] != ""


# ---------------------------------------------------------------------------
# WorldViewModel.spendDetail() tests
# ---------------------------------------------------------------------------

def _fake_totals(cost=12.34, reqs=99, inp=1000, out=500, cr=200):
    """Return a real UsageTotals with deterministic values.

    UsageTotals.cost_usd is a @property = sum of the four *_cost fields,
    so we set them directly to produce the desired total.
    """
    t = UsageTotals(
        period="today",
        input_tokens=inp,
        output_tokens=out,
        cache_creation_tokens=50,
        cache_read_tokens=cr,
        # Set the sub-costs so cost_usd == cost exactly.
        input_cost=cost,
        output_cost=0.0,
        cache_creation_cost=0.0,
        cache_read_cost=0.0,
        request_count=reqs,
    )
    return t


def _fake_by_model():
    """Return a tuple[ModelTotals, ...] with two entries."""
    return (
        ModelTotals(
            model="claude-sonnet-4-6",
            input_tokens=800,
            output_tokens=400,
            cache_creation_tokens=30,
            cache_read_tokens=150,
            cost_usd=9.50,
        ),
        ModelTotals(
            model="claude-haiku-3",
            input_tokens=200,
            output_tokens=100,
            cache_creation_tokens=20,
            cache_read_tokens=50,
            cost_usd=2.84,
        ),
    )


def test_spend_detail_cost():
    vm = WorldViewModel(
        get_totals=lambda period: _fake_totals(cost=12.34),
        get_totals_by_model=lambda period: _fake_by_model(),
    )
    detail = vm.spendDetail()
    assert abs(detail["cost"] - 12.34) < 1e-9


def test_spend_detail_reqs():
    vm = WorldViewModel(
        get_totals=lambda period: _fake_totals(reqs=99),
        get_totals_by_model=lambda period: _fake_by_model(),
    )
    assert vm.spendDetail()["reqs"] == 99


def test_spend_detail_tokens():
    vm = WorldViewModel(
        get_totals=lambda period: _fake_totals(inp=1000, out=500, cr=200),
        get_totals_by_model=lambda period: _fake_by_model(),
    )
    d = vm.spendDetail()
    assert d["input_tokens"] == 1000
    assert d["output_tokens"] == 500
    assert d["cache_read"] == 200


def test_spend_detail_per_model_length():
    vm = WorldViewModel(
        get_totals=lambda period: _fake_totals(),
        get_totals_by_model=lambda period: _fake_by_model(),
    )
    assert len(vm.spendDetail()["per_model"]) == 2


def test_spend_detail_per_model_fields():
    vm = WorldViewModel(
        get_totals=lambda period: _fake_totals(),
        get_totals_by_model=lambda period: _fake_by_model(),
    )
    by_m = {m["model"]: m["cost"] for m in vm.spendDetail()["per_model"]}
    assert abs(by_m["claude-sonnet-4-6"] - 9.50) < 1e-9
    assert abs(by_m["claude-haiku-3"] - 2.84) < 1e-9


def test_spend_detail_no_callbacks_returns_zeros():
    vm = WorldViewModel()
    d = vm.spendDetail()
    assert d["cost"] == 0.0
    assert d["reqs"] == 0
    assert d["per_model"] == []


def test_spend_detail_hit_rate():
    """hit_rate = cache_read / (cache_read + input_tokens) = 84 / (84 + 16) = 0.84."""
    vm = WorldViewModel(
        get_totals=lambda period: _fake_totals(inp=16, cr=84),
        get_totals_by_model=lambda period: _fake_by_model(),
    )
    d = vm.spendDetail()
    assert abs(d["hit_rate"] - 0.84) < 1e-9


def test_spend_detail_hit_rate_zero_when_no_tokens():
    """Both buckets zero → hit_rate must be 0.0 (guard divide-by-zero)."""
    vm = WorldViewModel(
        get_totals=lambda period: _fake_totals(inp=0, cr=0),
        get_totals_by_model=lambda period: _fake_by_model(),
    )
    assert vm.spendDetail()["hit_rate"] == 0.0


# ---------------------------------------------------------------------------
# refreshQuota and resumeSession slot tests
# ---------------------------------------------------------------------------

def test_refresh_quota_calls_callback():
    calls = []
    vm = WorldViewModel(refresh_quota_fn=lambda: calls.append("refresh"))
    vm.refreshQuota()
    assert calls == ["refresh"]


def test_resume_session_calls_callback_with_uuid():
    calls = []
    vm = WorldViewModel(resume_fn=lambda uuid: calls.append(uuid))
    vm.resumeSession("uuid-99")
    assert calls == ["uuid-99"]


def test_refresh_quota_no_callback_is_noop():
    vm = WorldViewModel()
    vm.refreshQuota()  # must not raise


def test_resume_session_no_callback_is_noop():
    vm = WorldViewModel()
    vm.resumeSession("any-uuid")  # must not raise


# ---------------------------------------------------------------------------
# recents property on WorldViewModel
# ---------------------------------------------------------------------------

def test_vm_recents_property_reflects_snapshot():
    sess = Session(
        pid=1,
        project_path=Path("D:/x"),
        last_activity=_NOW,
        session_uuid="live-1",
    )
    view = SessionView(
        pid=1, name="live", project_path=Path("D:/x"), project_basename="x",
        last_activity=_NOW, cost_usd=0.0, is_high_cost=False,
        latest_model=None, status_word=None, session=sess,
        session_uuid="live-1", phase=SessionPhase.IDLE,
    )
    snap = WorldSnapshot(
        today_cost_usd=0.0,
        quota=None,
        available_providers=("anthropic",),
        selected_provider="anthropic",
        fetched_at=_NOW,
        session_groups=(SessionGroup("g1", None, "", (view,)),),
        dormant_sessions=(_dormant_session("past-proj", "d-42"),),
    )
    vm = WorldViewModel()
    vm.update(snap)
    recents = vm.recents
    assert len(recents) == 1
    assert recents[0]["name"] == "past-proj"
    assert recents[0]["session_uuid"] == "d-42"


# ---------------------------------------------------------------------------
# R1 Deliverable 3: sessionDetail() slot
# ---------------------------------------------------------------------------

def _make_session(uuid: str = "uuid-detail-1") -> Session:
    return Session(
        pid=42,
        project_path=Path("D:/myproject"),
        last_activity=_NOW,
        session_uuid=uuid,
    )


def _make_live_view(uuid: str = "uuid-detail-1") -> SessionView:
    sess = _make_session(uuid)
    return SessionView(
        pid=42,
        name="detail-test",
        project_path=Path("D:/myproject"),
        project_basename="myproject",
        last_activity=_NOW,
        cost_usd=3.50,
        is_high_cost=False,
        latest_model="claude-sonnet-4-6",
        status_word=None,
        session=sess,
        session_uuid=uuid,
        phase=SessionPhase.IDLE,
    )


def _snap_with_live_view(view: SessionView) -> WorldSnapshot:
    return WorldSnapshot(
        today_cost_usd=0.0,
        quota=None,
        available_providers=(),
        selected_provider=None,
        fetched_at=_NOW,
        session_groups=(SessionGroup("g", None, "", (view,)),),
    )


def _stub_details(session: Session) -> SessionDetails:
    """Fake get_session_details — returns a minimal SessionDetails with known values."""
    return SessionDetails(
        session=session,
        name="my-session",
        original_name="cc-learning",
        ai_title="Fixing the authentication bug",
        git_branch="feat/auth-fix",
        last_prompt="Can you fix the login flow?",
        started_at=_NOW,
        status="idle",
        cc_version="2.1.0",
        cost_usd=3.50,
        turn_count=7,
        sidechain_count=1,
        per_model=(
            ModelTotals(
                model="claude-sonnet-4-6",
                input_tokens=500,
                output_tokens=300,
                cache_creation_tokens=10,
                cache_read_tokens=50,
                cost_usd=3.50,
            ),
        ),
        latest_model="claude-sonnet-4-6",
        effective_uuid="uuid-detail-1",
    )


def test_session_detail_returns_mapped_dict():
    """sessionDetail() with a wired callback returns a dict with all expected keys."""
    uuid = "uuid-detail-1"
    view = _make_live_view(uuid)
    vm = WorldViewModel(get_session_details=_stub_details)
    vm.update(_snap_with_live_view(view))

    detail = vm.sessionDetail(uuid)

    assert detail["name"] == "my-session"
    assert detail["ai_title"] == "Fixing the authentication bug"
    assert detail["branch"] == "feat/auth-fix"
    assert detail["latest_prompt"] == "Can you fix the login flow?"
    assert abs(detail["cost"] - 3.50) < 1e-9
    assert detail["turns"] == 7
    assert detail["uuid"] == "uuid-detail-1"
    assert detail["model"] == "claude-sonnet-4-6"
    assert len(detail["per_model"]) == 1
    assert detail["per_model"][0]["model"] == "claude-sonnet-4-6"
    assert abs(detail["per_model"][0]["cost"] - 3.50) < 1e-9


def test_session_detail_unknown_id_returns_empty():
    """sessionDetail("nope") returns {} when no matching session in snapshot."""
    view = _make_live_view("uuid-detail-1")
    vm = WorldViewModel(get_session_details=_stub_details)
    vm.update(_snap_with_live_view(view))

    result = vm.sessionDetail("nope")
    assert result == {}


def test_session_detail_no_callback_returns_empty():
    """When no get_session_details is injected, sessionDetail returns {}."""
    uuid = "uuid-detail-1"
    view = _make_live_view(uuid)
    vm = WorldViewModel()  # no get_session_details
    vm.update(_snap_with_live_view(view))

    result = vm.sessionDetail(uuid)
    assert result == {}


def test_session_detail_contains_cwd():
    """sessionDetail() includes the cwd from the view's project_path."""
    uuid = "uuid-detail-1"
    view = _make_live_view(uuid)
    vm = WorldViewModel(get_session_details=_stub_details)
    vm.update(_snap_with_live_view(view))

    detail = vm.sessionDetail(uuid)
    # cwd should contain "myproject"
    assert "myproject" in detail["cwd"]
