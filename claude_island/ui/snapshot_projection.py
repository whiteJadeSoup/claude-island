"""WorldSnapshot → QML 可消费的纯 dict 投影。QML 唯一数据契约。

枚举一律取 .value 字符串(QML 侧好判断 phase / kind / risk)。
只投影 walking skeleton 需要的字段;后续 Plan 增量加。
"""
from __future__ import annotations

import re
from typing import Any

from claude_island.core.snapshot import WorldSnapshot, SessionView
from claude_island.core.pending_decisions import PendingDecisionView


def _epoch_ms(dt: Any) -> int:
    """Epoch milliseconds for a tz-aware datetime, or 0 when unavailable.

    QML uses this to compute a live relative countdown to a quota reset
    (``epoch_ms - Date.now()``). 0 signals "unknown" so QML shows "—".
    """
    if dt is None:
        return 0
    try:
        return int(dt.timestamp() * 1000)
    except Exception:
        return 0


def _fmt_model(raw: str | None) -> str | None:
    """Convert a raw Anthropic model id to a short friendly label.

    Examples:
        "claude-opus-4-7"    → "opus-4.7"
        "claude-sonnet-4-6"  → "sonnet-4.6"
        "claude-haiku-3-5"   → "haiku-3.5"
        "claude-haiku-3"     → "haiku-3"
        "opus-4.7"           → "opus-4.7"   (already formatted, pass through)
        None / ""            → None

    The label intentionally stays short so it fits in the model chip without
    truncation, avoiding the previous .substring(0,14) workaround.
    """
    if not raw:
        return None
    lower = raw.lower()
    # Match family + major + minor separated by dashes, e.g. "opus-4-7" → "opus-4.7".
    # The dash separator distinguishes raw ids from already-formatted labels.
    m = re.search(r"(opus|sonnet|haiku)-(\d+)-(\d+)", lower)
    if m:
        return f"{m.group(1)}-{m.group(2)}.{m.group(3)}"
    # Match family + major only (dash separator + digit at end or before non-digit),
    # but only when NOT followed by a dot (which would mean the label is already
    # formatted as "family-major.minor" and should pass through untouched).
    m2 = re.search(r"(opus|sonnet|haiku)-(\d+)(?!\.\d)", lower)
    if m2:
        return f"{m2.group(1)}-{m2.group(2)}"
    # Fallback: first 14 chars (better than nothing for unknown shapes)
    return raw[:14]


def _session(v: SessionView) -> dict[str, Any]:
    return {
        "id": v.session_uuid or f"{v.project_path}:{v.pid}",
        "name": v.name,
        "phase": v.phase.value,
        "cwd": str(v.project_path),
        "cost_usd": float(v.cost_usd),
        "is_high_cost": bool(v.is_high_cost),
        # Bug 4 fix: convert raw model id ("claude-opus-4-7") to a friendly
        # label ("opus-4.7") so the model chip doesn't need .substring(0,14).
        "model": _fmt_model(v.latest_model),
        "tokens_per_min": v.tokens_per_min,
        "current_tool_input": v.current_tool_input,
        "turn_count": int(v.turn_count or 0),
    }


def _decision(p: PendingDecisionView) -> dict[str, Any]:
    return {
        "id": p.id,
        "kind": p.kind.value,
        "session_name": p.session_name,
        # session_uuid is included so DecisionCard can call vm.focusSession() to
        # jump to the terminal that owns this decision (the "Jump to terminal" row).
        "session_uuid": p.session_uuid,
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

    # Derive transcript path: ~/.claude/projects/<hash>/<uuid>.jsonl
    # Same formula as _transcript_path_for_display in expanded_window.py.
    # Computed here (projection layer) so RecentsPage.qml can pass the path
    # directly to vm.openTranscript without any JS path arithmetic.
    uuid = str(getattr(d, "session_uuid", ""))
    cwd = getattr(d, "cwd", None)
    transcript_path = ""
    if uuid and cwd:
        try:
            from pathlib import Path as _Path
            from claude_island.core.models import project_hash as _ph
            transcript_path = str(
                _Path.home() / ".claude" / "projects" / _ph(cwd) / f"{uuid}.jsonl"
            )
        except Exception:
            transcript_path = ""

    result: dict[str, Any] = {
        "name": getattr(d, "name", None),
        "cwd": str(cwd) if cwd is not None else "",
        "last_seen": str(last_activity) if last_activity is not None else "",
        "last_activity_ts": last_activity_ts,
        "cost_usd": float(getattr(d, "cost_usd", 0.0)),
        "session_uuid": uuid,
        "transcript_path": transcript_path,
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
            # Reset timestamps — keep the raw string (legacy) AND an epoch-ms
            # number so QML can render a live relative countdown ("resets in
            # 1h 38m") instead of an unfriendly absolute timestamp. QML
            # computes remaining = epoch_ms - Date.now() and reformats on a
            # timer; 0 means "unknown" (QML then shows "—").
            **({
                "five_hour_reset": str(getattr(q, "five_hour_resets_at", "")),
                "five_hour_reset_epoch": _epoch_ms(getattr(q, "five_hour_resets_at", None)),
            } if hasattr(q, "five_hour_resets_at") else {}),
            **({
                "weekly_reset": str(getattr(q, "seven_day_resets_at", "")),
                "weekly_reset_epoch": _epoch_ms(getattr(q, "seven_day_resets_at", None)),
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
