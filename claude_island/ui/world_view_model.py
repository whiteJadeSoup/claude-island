"""Python↔QML 桥:订阅 world,把 snapshot 投影成 QML 可绑定 Property。"""
from __future__ import annotations

from typing import Callable

from PySide6.QtCore import QObject, Property, Signal, Slot

from claude_island.core.pending_decisions import Decision, DecisionResult
from claude_island.core.snapshot import WorldSnapshot
from claude_island.ui.snapshot_projection import project_snapshot

_EMPTY = {"today_cost_usd": 0.0, "quota": None, "sessions": [], "decisions": []}


class WorldViewModel(QObject):
    changed = Signal()

    def __init__(
        self,
        parent: QObject | None = None,
        *,
        # Injected callbacks so the VM stays in the UI layer (no direct
        # dependency on PendingDecisionRegistry or platform_). Defaults to
        # no-ops so existing code that constructs WorldViewModel without
        # callbacks continues to work.
        resolve_fn: Callable[[str, Decision], bool] | None = None,
        focus_fn: Callable[[str], None] | None = None,
    ) -> None:
        super().__init__(parent)
        self._d = dict(_EMPTY)
        # Default no-ops: resolve returns False (unknown id), focus does nothing.
        self._resolve_fn: Callable[[str, Decision], bool] = resolve_fn or (lambda did, dec: False)
        self._focus_fn: Callable[[str], None] = focus_fn or (lambda sid: None)

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

    # ── Decision / focus slots (called from QML or test code) ─────────────

    @Slot(str, bool)
    def approve(self, decision_id: str, remember: bool) -> None:
        """Resolve a pending decision as ALLOW. remember=True tells Claude
        Code to remember this permission for the tool permanently."""
        self._resolve_fn(decision_id, Decision(result=DecisionResult.ALLOW, remember=bool(remember)))

    @Slot(str)
    def deny(self, decision_id: str) -> None:
        """Resolve a pending decision as DENY."""
        self._resolve_fn(decision_id, Decision(result=DecisionResult.DENY, reason="declined from island"))

    @Slot(str, str, str)
    def answerQuestion(self, decision_id: str, question_text: str, answer: str) -> None:
        """Relay a single-question answer back to the hook server.
        Wraps the answer as a one-element answers tuple matching Decision's
        tuple[tuple[str, str], ...] contract."""
        self._resolve_fn(decision_id, Decision(result=DecisionResult.ALLOW, answers=((question_text, answer),)))

    @Slot(str)
    def focusSession(self, session_id: str) -> None:
        """Bring the terminal window for session_id to the foreground."""
        self._focus_fn(session_id)
