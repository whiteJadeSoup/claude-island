"""Python↔QML 桥:订阅 world,把 snapshot 投影成 QML 可绑定 Property。"""
from __future__ import annotations

from PySide6.QtCore import QObject, Property, Signal

from claude_island.core.snapshot import WorldSnapshot
from claude_island.ui.snapshot_projection import project_snapshot

_EMPTY = {"today_cost_usd": 0.0, "quota": None, "sessions": [], "decisions": []}


class WorldViewModel(QObject):
    changed = Signal()

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._d = dict(_EMPTY)

    def update(self, snap: WorldSnapshot) -> None:
        """在 Qt 主线程调用(world.push 已在主线程)。重投影 + 通知 QML。"""
        self._d = project_snapshot(snap)
        self.changed.emit()

    @Property("QVariantList", notify=changed)
    def sessions(self):
        return self._d["sessions"]

    @Property("QVariantList", notify=changed)
    def decisions(self):
        return self._d["decisions"]

    @Property(str, notify=changed)
    def todayCost(self) -> str:
        c = self._d["today_cost_usd"]
        return f"${c:.0f}" if c >= 100 else f"${c:.2f}"

    @Property(int, notify=changed)
    def quotaPct(self) -> int:
        q = self._d["quota"]
        return int(q["five_hour_pct"]) if q else 0
