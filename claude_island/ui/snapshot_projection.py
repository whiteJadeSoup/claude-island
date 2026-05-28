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


def _dormant(d: Any) -> dict[str, Any]:
    # Map DormantSession to a plain dict QML can consume.
    # last_activity is a datetime — stringify so QML gets a stable string.
    last_activity = getattr(d, "last_activity", None)
    # Provide an epoch-seconds sortable timestamp for grouping/sorting in JS.
    # Timezone-aware datetimes use .timestamp(); naive ones are treated as UTC.
    try:
        last_activity_ts = last_activity.timestamp() if last_activity is not None else 0.0
    except Exception:
        last_activity_ts = 0.0

    result: dict[str, Any] = {
        "name": getattr(d, "name", None),
        "cwd": str(getattr(d, "cwd", "")),
        "last_seen": str(last_activity) if last_activity is not None else "",
        "last_activity_ts": last_activity_ts,
        "cost_usd": float(getattr(d, "cost_usd", 0.0)),
        "session_uuid": str(getattr(d, "session_uuid", "")),
    }
    # turns — DormantSession.turn_count (int); omit key only if attr absent entirely.
    turns = getattr(d, "turn_count", None)
    if turns is not None:
        result["turns"] = int(turns)
    # model — DormantSession has no model field; omit gracefully if absent.
    model = getattr(d, "model", None)
    if model is not None:
        result["model"] = str(model)
    return result


def project_snapshot(snap: WorldSnapshot) -> dict[str, Any]:
    sessions = [_session(v) for g in snap.session_groups for v in g.views]
    decisions = [_decision(p) for p in (snap.pending_decisions or ())]
    quota = None
    if snap.quota is not None:
        q = snap.quota
        quota = {
            "five_hour_pct": int(getattr(q, "five_hour_pct", 0)),
            # seven-day window — may be missing on old QuotaSnapshot shapes
            **({
                "weekly_pct": int(getattr(q, "seven_day_pct", 0)),
            } if hasattr(q, "seven_day_pct") else {}),
            # Reset timestamps — keep as strings for QML compatibility
            **({
                "five_hour_reset": str(getattr(q, "five_hour_resets_at", "")),
            } if hasattr(q, "five_hour_resets_at") else {}),
            **({
                "weekly_reset": str(getattr(q, "seven_day_resets_at", "")),
            } if hasattr(q, "seven_day_resets_at") else {}),
        }
    # Dormant (offline) sessions — rendered by the Recents drawer.
    dormant = getattr(snap, "dormant_sessions", ()) or ()
    recents = [_dormant(d) for d in dormant]
    return {
        "today_cost_usd": float(snap.today_cost_usd),
        "quota": quota,
        "sessions": sessions,
        "decisions": decisions,
        "recents": recents,
    }
