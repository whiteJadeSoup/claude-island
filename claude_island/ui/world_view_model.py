"""Python↔QML 桥:订阅 world,把 snapshot 投影成 QML 可绑定 Property。"""
from __future__ import annotations

from typing import Callable

from PySide6.QtCore import QObject, Property, Signal, Slot

from claude_island.core.pending_decisions import Decision, DecisionResult
from claude_island.core.snapshot import WorldSnapshot, SessionView
from claude_island.ui.snapshot_projection import project_snapshot, _fmt_model

_EMPTY = {"today_cost_usd": 0.0, "quota": None, "sessions": [], "decisions": [], "recents": []}

# Maximum samples kept in the rolling token-rate buffer per session.
# One sample is appended per update() call — i.e. per snapshot push, whose
# cadence is UNEVEN (multi-source: file/hook/totals changes coalesced by
# throttle_first ~0.2s when active, the 60s quota heartbeat when idle). So
# this is the last N samples, NOT a fixed time window — the waveform reads
# it for SHAPE only, never as a time axis.
_RATE_HISTORY_MAX = 60


def _compute_hit_rate(cache_read: int, cache_creation: int) -> float:
    """Cache hit rate as a 0..1 float.

    Formula: cache_read / (cache_read + cache_creation) — reads ÷
    (reads + creations). This is the fraction of *cache traffic* that
    was a hit (read) vs a miss (write/creation). The denominator must
    NOT include plain ``input_tokens``: those tokens never went through
    the cache at all, so folding them in is meaningless — and because
    cache_read dwarfs input_tokens (e.g. 70M vs 10K), it pinned the
    rate at ~100%. Mirrors expanded_window.py:4795 (the old app).
    Returns 0.0 when there is no cache traffic in either direction.
    """
    total = cache_read + cache_creation
    return cache_read / total if total > 0 else 0.0


def _clean_per_model(rows) -> list[dict]:
    """Filter Claude Code's ``<synthetic>`` placeholder + zero-usage rows,
    aggregate by friendly label, sort by cost descending.

    Mirrors expanded_window.py ``_aggregate_per_model_for_display``. The
    ``<synthetic>`` bucket is what Claude Code attributes ``/compact``
    summaries and other non-API messages to; it carries zero real usage,
    so surfacing it as a model row is pure noise (user saw it leak into
    the SessionDetail breakdown). Aggregating by friendly label also folds
    two ``opus`` rows into one when the raw model id changed mid-session.

    Each row is duck-typed (ModelTotals): ``.model``, ``.cost_usd`` and the
    four token buckets. Returns ``[{"model", "cost"}]`` for QML.
    """
    buckets: dict[str, float] = {}
    for mt in (rows or ()):
        raw = str(getattr(mt, "model", "") or "")
        cost = float(getattr(mt, "cost_usd", 0.0) or 0.0)
        inp = int(getattr(mt, "input_tokens", 0) or 0)
        out = int(getattr(mt, "output_tokens", 0) or 0)
        cw = int(getattr(mt, "cache_creation_tokens", 0) or 0)
        cr = int(getattr(mt, "cache_read_tokens", 0) or 0)
        if "synthetic" in raw.lower():
            continue
        if cost == 0 and inp == 0 and out == 0 and cw == 0 and cr == 0:
            continue
        label = _fmt_model(raw) or raw
        buckets[label] = buckets.get(label, 0.0) + cost
    return [
        {"model": label, "cost": cost}
        for label, cost in sorted(buckets.items(), key=lambda kv: kv[1], reverse=True)
    ]


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
        # Spend / quota callbacks — injected from __main__ so VM has no
        # direct dependency on UsageRegistry or the platform layer.
        get_totals: Callable | None = None,
        get_totals_by_model: Callable | None = None,
        get_sidechain_totals: Callable | None = None,
        refresh_quota_fn: Callable | None = None,
        resume_fn: Callable | None = None,
        # Session detail callback — injected from qml_app so VM never
        # imports platform_ code directly (import-linter contract).
        # Accepts a Session and returns a SessionDetails (or equivalent).
        get_session_details: Callable | None = None,
        # R7 action callbacks — safe no-op defaults so existing callers
        # that don't need these actions continue to construct without error.
        rename_fn: Callable | None = None,
        open_folder_fn: Callable | None = None,
        reset_thinking_fn: Callable | None = None,
    ) -> None:
        super().__init__(parent)
        self._d = dict(_EMPTY)
        # Default no-ops: resolve returns False (unknown id), focus does nothing.
        self._resolve_fn: Callable[[str, Decision], bool] = resolve_fn or (lambda did, dec: False)
        self._focus_fn: Callable[[str], None] = focus_fn or (lambda sid: None)
        # Spend / quota callbacks — no-op defaults so callers that don't
        # inject these still construct without error.
        self._get_totals: Callable | None = get_totals
        self._get_totals_by_model: Callable | None = get_totals_by_model
        self._get_sidechain_totals: Callable | None = get_sidechain_totals
        self._refresh_quota_fn: Callable = refresh_quota_fn or (lambda: None)
        self._resume_fn: Callable = resume_fn or (lambda uuid: None)
        self._get_session_details: Callable | None = get_session_details
        # R7 action callbacks — no-op lambdas keep the VM callable even
        # when callers haven't wired the full action set yet.
        self._rename_fn: Callable = rename_fn or (lambda uuid, name: None)
        self._open_folder_fn: Callable = open_folder_fn or (lambda sid: None)
        self._reset_thinking_fn: Callable = reset_thinking_fn or (lambda uuid: None)

        # Rolling token-rate history: session_id → list of up-to-60 int samples.
        # One sample (tokens_per_min or 0) appended per update() call.
        # Pruned when sessions leave the snapshot so memory doesn't grow
        # unboundedly across session churn.
        self._rate_history: dict[str, list[int]] = {}

        # Latest SessionView index for detail lookup.  Populated in update()
        # and read by sessionDetail().  Keyed by session_uuid.
        self._views_by_id: dict[str, SessionView] = {}

    def update(self, snap: WorldSnapshot) -> None:
        """在 Qt 主线程调用(world.push 已在主线程)。重投影 + 通知 QML。"""
        self._d = project_snapshot(snap)

        # ── R1 Deliverable 1: enrich sessions with rate_series ─────────────
        # Build the current set of session ids so we can prune stale history.
        current_ids: set[str] = set()
        for group in snap.session_groups:
            for view in group.views:
                sid = view.session_uuid or f"{view.project_path}:{view.pid}"
                current_ids.add(sid)
                # Append one rate sample (0 when None so the waveform stays
                # continuous even during idle phases).
                rate = view.tokens_per_min if view.tokens_per_min is not None else 0
                hist = self._rate_history.setdefault(sid, [])
                hist.append(rate)
                # Cap at _RATE_HISTORY_MAX by dropping the oldest sample.
                if len(hist) > _RATE_HISTORY_MAX:
                    del hist[0]

        # Prune history for sessions that are no longer in the snapshot so
        # the dict doesn't accumulate indefinitely across session churn.
        stale = [k for k in self._rate_history if k not in current_ids]
        for k in stale:
            del self._rate_history[k]

        # Rebuild views_by_id (keyed by session_uuid, used by sessionDetail).
        self._views_by_id = {}
        for group in snap.session_groups:
            for view in group.views:
                if view.session_uuid:
                    self._views_by_id[view.session_uuid] = view

        # Attach rate_series to each projected session dict in-place.
        # The projection produced plain dicts — we can mutate them freely
        # before handing them to QML.
        for s in self._d["sessions"]:
            sid = s["id"]
            s["rate_series"] = list(self._rate_history.get(sid, []))

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

    @Property("QVariant", notify=changed)
    def quota(self):
        """Full quota dict for SpendPage (five_hour_pct, weekly_pct, reset times).
        Returns None when no quota data is available."""
        return self._d.get("quota")

    @Property("QVariantList", notify=changed)
    def recents(self):
        return self._d.get("recents", [])

    # ── Spend / quota / resume slots ──────────────────────────────────────

    @Slot(result="QVariant")
    def spendDetail(self):
        """Return today's spend breakdown as a plain dict for QML.

        Calls the injected get_totals / get_totals_by_model callbacks so
        the VM never imports UsageRegistry directly (UI layer isolation).
        Returns zeros when no callbacks were injected (e.g. in legacy
        callers or tests that only care about other functionality).
        """
        totals = self._get_totals("today") if self._get_totals else None
        by_model = self._get_totals_by_model("today") if self._get_totals_by_model else None

        def g(o, *names, default=0):
            """Defensive multi-name getattr — tries names in order."""
            for n in names:
                if o is not None and hasattr(o, n):
                    return getattr(o, n)
            return default

        # get_totals_by_model returns tuple[ModelTotals, ...]. _clean_per_model
        # applies the friendly label, drops the <synthetic> placeholder and
        # zero-usage rows, and aggregates by label (see its docstring).
        try:
            per_model = _clean_per_model(by_model)
        except Exception:
            per_model = []

        # Subagent (sidechain) aggregation for the "↳ incl. N subagent reqs" line.
        # get_sidechain_totals returns (count: int, cost: float) for the period.
        subagent_reqs = 0
        subagent_cost = 0.0
        if self._get_sidechain_totals:
            try:
                _sc = self._get_sidechain_totals("today")
                subagent_reqs = int(_sc[0])
                subagent_cost = float(_sc[1])
            except Exception:
                pass

        cache_read = int(g(totals, "cache_read_tokens", "cache_read"))
        cache_creation = int(g(totals, "cache_creation_tokens", "cache_creation"))
        input_tok = int(g(totals, "input_tokens"))
        output_tok = int(g(totals, "output_tokens"))

        return {
            # cost_usd is a @property on UsageTotals computed from the
            # four sub-costs; request_count is an int field.
            "cost": float(g(totals, "cost_usd", "cost")),
            "reqs": int(g(totals, "request_count", "reqs")),
            "input_tokens": input_tok,
            "output_tokens": output_tok,
            # total_tokens = input + output (cache excluded — not "generated now").
            "total_tokens": input_tok + output_tok,
            # cache_creation_tokens is the "cache write" bucket;
            # cache_read_tokens is the "cache read / hit" bucket.
            "cache_read": cache_read,
            # hit_rate = cache_read / (cache_read + cache_creation).
            # UsageTotals has no hit_rate field; derive it here so QML
            # SpendPage can display it without recomputing in JS.
            # Guard divide-by-zero: return 0.0 when there is no cache traffic.
            "hit_rate": _compute_hit_rate(cache_read, cache_creation),
            # Subagent / sidechain breakdown for the TODAY card sub-line.
            "subagent_reqs": subagent_reqs,
            "subagent_cost": subagent_cost,
            "per_model": per_model,
        }

    @Slot()
    def refreshQuota(self):
        """Trigger an out-of-band quota refresh via the injected callback."""
        self._refresh_quota_fn()

    @Slot(str)
    def resumeSession(self, session_uuid: str):
        """Resume a dormant session by uuid via the injected callback."""
        self._resume_fn(session_uuid)

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

    @Slot(str, str, "QStringList")
    def answerQuestionMulti(self, decision_id: str, question_text: str, answers: list) -> None:
        """Relay a multi-select answer back to the hook server.

        Joins the selected option labels with ", " and wraps as a single
        answer pair, matching Decision's tuple[tuple[str, str], ...] contract.
        Called from DecisionCard.qml when multi_select===true and the user
        clicks the Submit button.
        """
        joined = ", ".join(str(a) for a in answers)
        self._resolve_fn(decision_id, Decision(result=DecisionResult.ALLOW, answers=((question_text, joined),)))

    @Slot(str)
    def focusSession(self, session_id: str) -> None:
        """Bring the terminal window for session_id to the foreground."""
        self._focus_fn(session_id)

    # ── Session detail slot (Deliverable 3) ──────────────────────────────

    @Slot(str, result="QVariant")
    def sessionDetail(self, session_id: str) -> dict:
        """Return a rich detail dict for the given session_uuid.

        Maps SessionDetails fields to a flat dict QML can bind to.
        Returns {} when no matching view exists (unknown id or not yet
        in the snapshot) so callers can guard with ``if (detail)`` in QML.

        The get_session_details callback is injected by qml_app so the
        VM never imports platform_ code directly (import-linter contract).
        When no callback is wired (tests that don't need detail), returns
        {} for any id.
        """
        view = self._views_by_id.get(session_id)
        if view is None or self._get_session_details is None:
            return {}
        try:
            details = self._get_session_details(view.session)
        except Exception:
            return {}

        def _g(obj, *names, default=None):
            """Defensive multi-name getattr — tries names in order."""
            for n in names:
                if obj is not None and hasattr(obj, n):
                    return getattr(obj, n)
            return default

        # Drop <synthetic> + zero rows and aggregate by friendly label so the
        # SessionDetail breakdown never shows the "synthetic" noise row.
        try:
            per_model = _clean_per_model(_g(details, "per_model"))
        except Exception:
            per_model = []

        # Derive transcript path: ~/.claude/projects/<hash>/<uuid>.jsonl
        # Same formula as _transcript_path_for_display in expanded_window.py.
        # Done here in the VM (UI layer) so it never touches platform_.
        eff_uuid = str(_g(details, "effective_uuid") or session_id)
        transcript_path = ""
        if eff_uuid:
            try:
                from pathlib import Path as _Path
                from claude_island.core.models import project_hash as _ph
                transcript_path = str(
                    _Path.home() / ".claude" / "projects"
                    / _ph(view.project_path) / f"{eff_uuid}.jsonl"
                )
            except Exception:
                transcript_path = ""

        # Apply friendly model label so the detail page shows "opus-4.7" not "claude-opus-4-7".
        raw_model = str(_g(details, "latest_model") or view.latest_model or "")
        friendly_model = _fmt_model(raw_model) or raw_model

        # Friendly "Created" — str(datetime) yields "2026-05-29 13:25:58.221000
        # +00:00" (raw microseconds + tz offset), which the detail page showed
        # verbatim and looked broken. Format to "YYYY-MM-DD HH:MM" — minute
        # precision is plenty for "when did this session start". Falls back to
        # the raw string for any non-datetime value.
        created_raw = _g(details, "started_at")
        if hasattr(created_raw, "strftime"):
            created_str = created_raw.strftime("%Y-%m-%d %H:%M")
        else:
            created_str = str(created_raw or "")

        return {
            "name":            str(_g(details, "name") or ""),
            "model":           friendly_model,
            "cost":            float(_g(details, "cost_usd", default=0.0)),
            "turns":           int(_g(details, "turn_count", default=0)),
            "input_tokens":    0,   # UsageRegistry per-session input not exposed by SessionDetails
            "output_tokens":   0,   # same — per_model breakdown covers token detail
            "cwd":             str(view.project_path),
            "branch":          str(_g(details, "git_branch") or ""),
            "created":         created_str,
            "ai_title":        str(_g(details, "ai_title") or ""),
            "transcript_path": transcript_path,
            "latest_prompt":   str(_g(details, "last_prompt") or ""),
            "uuid":            eff_uuid,
            "per_model":       per_model,
            # Real session phase so SessionDetailPage can show it accurately
            # instead of hardcoding "active". SessionView.phase is a SessionPhase
            # enum; .value gives the string used by QML (e.g. "thinking", "idle").
            "phase":           view.phase.value if view.phase else "",
        }

    # ── R7 action slots ───────────────────────────────────────────────────

    @Slot(str)
    def copyId(self, text: str) -> None:
        """Copy the given text to the system clipboard.

        Uses QGuiApplication.clipboard(). Only available when a
        QGuiApplication (not just QCoreApplication) is running —
        offscreen test environments use QCoreApplication which has no
        clipboard; calling clipboard() without a GUI app crashes on some
        platforms. We check that a QGuiApplication is present by testing
        for the 'clipboard' attribute on the app instance (QCoreApplication
        doesn't have it), which avoids the C-level isinstance issue in
        PySide6 mocks."""
        try:
            from PySide6.QtGui import QGuiApplication
            app = QGuiApplication.instance()
            # clipboard() exists only on QGuiApplication, not QCoreApplication.
            # Use hasattr rather than isinstance so test mocks that spec
            # QGuiApplication also pass this guard without triggering
            # PySide6's C-level type check.
            if app is None or not hasattr(app, "clipboard"):
                return
            clipboard = app.clipboard()
            if clipboard is not None:
                clipboard.setText(text)
        except Exception:
            pass

    @Slot(str, str)
    def renameSession(self, uuid: str, name: str) -> None:
        """Persist a custom name for the session via the injected rename callback."""
        try:
            self._rename_fn(uuid, name)
        except Exception as exc:
            import sys
            print(f"[island] renameSession error: {exc}", file=sys.stderr)

    @Slot(str)
    def openFolder(self, session_id: str) -> None:
        """Open the session's working directory in the OS file manager."""
        try:
            self._open_folder_fn(session_id)
        except Exception as exc:
            import sys
            print(f"[island] openFolder error: {exc}", file=sys.stderr)

    @Slot(str)
    def openTranscript(self, path: str) -> None:
        """Open the transcript file at `path` with the OS default application.

        Guards against empty path so callers can call unconditionally."""
        if not path:
            return
        try:
            from PySide6.QtGui import QDesktopServices
            from PySide6.QtCore import QUrl
            QDesktopServices.openUrl(QUrl.fromLocalFile(path))
        except Exception as exc:
            import sys
            print(f"[island] openTranscript error: {exc}", file=sys.stderr)

    @Slot(str)
    def resetThinking(self, uuid: str) -> None:
        """Strip thinking blocks from the session's JSONL transcript.

        Destructive: delegates to the injected reset_thinking_fn which is
        expected to create a .bak backup before modifying the file."""
        try:
            self._reset_thinking_fn(uuid)
        except Exception as exc:
            import sys
            print(f"[island] resetThinking error: {exc}", file=sys.stderr)
