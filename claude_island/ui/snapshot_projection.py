"""WorldSnapshot → QML 可消费的纯 dict 投影。QML 唯一数据契约。

枚举一律取 .value 字符串(QML 侧好判断 phase / kind / risk)。
只投影 walking skeleton 需要的字段;后续 Plan 增量加。
"""
from __future__ import annotations

from typing import Any

from claude_island.core.snapshot import WorldSnapshot, SessionView
from claude_island.core.pending_decisions import PendingDecisionView


def _session(v: SessionView) -> dict[str, Any]:
    return {
        "id": v.session_uuid or f"{v.project_path}:{v.pid}",
        "name": v.name,
        "phase": v.phase.value,
        "cwd": str(v.project_path),
        "cost_usd": float(v.cost_usd),
        "is_high_cost": bool(v.is_high_cost),
        "model": v.latest_model,
        "tokens_per_min": v.tokens_per_min,
        "current_tool_input": v.current_tool_input,
        "turn_count": int(v.turn_count or 0),
    }


def _decision(p: PendingDecisionView) -> dict[str, Any]:
    return {
        "id": p.id,
        "kind": p.kind.value,
        "session_name": p.session_name,
        "risk": p.risk_level.value,
        "tool_name": p.tool_name,
        "tool_input_preview": p.tool_input_preview,
        "question_text": p.question_text,
        "options": list(p.question_options),
        "option_descriptions": list(p.question_option_descriptions),
        "multi_select": bool(p.multi_select),
    }


def project_snapshot(snap: WorldSnapshot) -> dict[str, Any]:
    sessions = [_session(v) for g in snap.session_groups for v in g.views]
    decisions = [_decision(p) for p in (snap.pending_decisions or ())]
    quota = None
    if snap.quota is not None:
        quota = {"five_hour_pct": int(getattr(snap.quota, "five_hour_pct", 0))}
    return {
        "today_cost_usd": float(snap.today_cost_usd),
        "quota": quota,
        "sessions": sessions,
        "decisions": decisions,
    }
