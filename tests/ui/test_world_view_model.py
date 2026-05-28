from datetime import datetime, timezone
from pathlib import Path
from PySide6.QtCore import QCoreApplication
from claude_island.core.snapshot import WorldSnapshot, SessionGroup, SessionView
from claude_island.core.session_phase import SessionPhase
from claude_island.core.models import Session
from claude_island.ui.world_view_model import WorldViewModel

_app = QCoreApplication.instance() or QCoreApplication([])


def _snap(cost):
    sess = Session(pid=1, project_path=Path("D:/x"),
                   last_activity=datetime.now(timezone.utc), session_uuid="u1")
    v = SessionView(pid=1, name="cc", project_path=Path("D:/x"), project_basename="x",
                    last_activity=datetime.now(timezone.utc), cost_usd=cost,
                    is_high_cost=cost >= 50.0, latest_model="opus-4.7", status_word=None,
                    session=sess, session_uuid="u1", phase=SessionPhase.THINKING)
    return WorldSnapshot(today_cost_usd=cost, quota=None, available_providers=(),
                         selected_provider=None, fetched_at=datetime.now(timezone.utc),
                         session_groups=(SessionGroup("g", None, "", (v,)),))


def test_update_populates_properties_and_emits():
    vm = WorldViewModel()
    fired = []
    vm.changed.connect(lambda: fired.append(True))
    vm.update(_snap(227.0))
    assert fired == [True]
    assert vm.todayCost == "$227"
    assert len(vm.sessions) == 1
    assert vm.sessions[0]["name"] == "cc"
    assert vm.quotaPct == 0
