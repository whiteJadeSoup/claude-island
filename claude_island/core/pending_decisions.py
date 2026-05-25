"""Pending-decision registry — bridges hook server (blocking on response)
with the UI (which resolves decisions out-of-band).

Lifecycle
---------
A blocking hook event (PreToolUse, UserPromptSubmit when review-mode is on)
arrives at HookServer. The server registers a ``DecisionRequest`` and waits
on a ``threading.Event``. The UI later renders an approval card from the
public ``snapshot()`` view; user click resolves the decision via
``resolve(id, decision)`` which sets the Event. The server thread wakes,
reads the Decision, encodes it as Claude Code's hook-output JSON, and
writes it to the HTTP response body.

Why threading.Event (not asyncio): HookServer is built on stdlib
``ThreadingHTTPServer`` — every request is already on its own OS thread.
Adding asyncio would require either bridging the two worlds or rewriting
the server. Event.wait() with a timeout is exactly the primitive needed.

Why a hard cap (MAX_PENDING_DECISIONS): each pending decision holds an
HTTP server thread. Without a cap a misbehaving Claude session could
exhaust the OS thread limit. At cap, ``register`` raises ``RegistryFull``
and the server replies ``"defer"`` immediately — Claude falls back to its
own permission rules and the user manages backlog through the UI.
"""
from __future__ import annotations

import logging
import threading
import uuid as _uuid
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Callable

log = logging.getLogger(__name__)


# Cap chosen so a runaway session can't exhaust OS threads while still
# leaving plenty of headroom for legitimate "five sessions all paused
# for approval" scenarios. Server replies "defer" beyond this.
MAX_PENDING_DECISIONS = 16

# Safety buffer: HookServer's wait timeout is shorter than the hook-side
# POST timeout by this margin so an expiring entry can still write a
# defer directive before the hook process gives up. Mirrors open-vibe-
# island's 2 s buffer between BridgeCommandClient and BridgeServer.
# Applied by HookServer when computing wait timeout; NOT enforced inside
# the registry (which trusts the caller-provided timeout).
WAIT_TIMEOUT_SAFETY_S = 2.0


# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------


class DecisionKind(Enum):
    """Which hook event the user is being asked to decide on."""
    PRE_TOOL_USE = "pre_tool_use"
    USER_PROMPT_SUBMIT = "user_prompt_submit"
    # Claude wants to ASK the user a question (AskUserQuestion or a
    # similarly-shaped MCP tool). UI shows option buttons instead of
    # Allow/Deny — see ui/question_card.QuestionCard.
    ASK_QUESTION = "ask_question"


class DecisionResult(Enum):
    """What the user decided. Mapping to Claude Code hook directives:

    - ``ALLOW`` (PRE_TOOL_USE)   → ``permissionDecision: "allow"``
    - ``DENY``  (PRE_TOOL_USE)   → ``permissionDecision: "deny"``
    - ``ALLOW`` (USER_PROMPT_SUBMIT) → empty directive (prompt forwarded)
    - ``BLOCK`` (USER_PROMPT_SUBMIT) → ``decision: "block", reason: …``
    - ``INJECT`` (USER_PROMPT_SUBMIT) → ``hookSpecificOutput.additionalContext``
    """
    ALLOW = "allow"
    DENY = "deny"
    BLOCK = "block"
    INJECT = "inject"


class RiskLevel(Enum):
    """Drives ApprovalCard color + warning emphasis. Pre-resolved on the
    DecisionRequest so the UI doesn't reapply policy."""
    LOW = "low"        # Read, Glob, LS — safe to remember silently
    MEDIUM = "medium"  # Grep, search-style ops
    HIGH = "high"      # Bash, Write, Edit, MultiEdit — show red warning


@dataclass(frozen=True, slots=True)
class DecisionRequest:
    """Server-side request entering the registry."""
    id: str                          # uuid4 hex
    kind: DecisionKind
    session_uuid: str
    session_name: str                # cached for UI
    cwd: Path
    cwd_basename: str
    hook_event: str                  # raw hook_event_name
    created_at: datetime
    expires_at: datetime
    # PRE_TOOL_USE / ASK_QUESTION
    tool_name: str | None = None
    tool_input_preview: str | None = None
    # Tool invocation correlation id from Claude Code's hook payload. Used
    # to match a later PostToolUse / PostToolUseFailure / PermissionDenied
    # event back to this entry so the registry can mark it externally
    # resolved (Claude Code decided in some other path — CLI prompt,
    # fail-open, etc.). Optional because Claude's payload occasionally
    # omits it; falls back to (session_uuid, tool_name) FIFO matching.
    tool_use_id: str | None = None
    risk_level: RiskLevel = RiskLevel.MEDIUM
    # USER_PROMPT_SUBMIT-only
    prompt_preview: str | None = None
    # ASK_QUESTION-only — the human-readable question + options Claude
    # wants the user to answer (AskUserQuestion tool). Options carry
    # label + optional description in parallel tuples (no nested
    # dataclass — keeps the value type cheap to hash for
    # distinct_until_changed).
    question_text: str | None = None
    question_header: str | None = None
    question_options: tuple[str, ...] = ()
    question_option_descriptions: tuple[str, ...] = ()
    multi_select: bool = False

    def __post_init__(self) -> None:
        if self.expires_at <= self.created_at:
            raise ValueError("expires_at must be > created_at")
        if self.kind is DecisionKind.PRE_TOOL_USE and not self.tool_name:
            raise ValueError("PRE_TOOL_USE requires tool_name")
        if self.kind is DecisionKind.USER_PROMPT_SUBMIT and self.prompt_preview is None:
            raise ValueError("USER_PROMPT_SUBMIT requires prompt_preview")
        if self.kind is DecisionKind.ASK_QUESTION:
            if not self.tool_name:
                raise ValueError("ASK_QUESTION requires tool_name")
            if not self.question_text:
                raise ValueError("ASK_QUESTION requires question_text")
            if not self.question_options:
                raise ValueError("ASK_QUESTION requires non-empty question_options")
            if (
                self.question_option_descriptions
                and len(self.question_option_descriptions) != len(self.question_options)
            ):
                raise ValueError(
                    "question_option_descriptions must be empty or "
                    "the same length as question_options"
                )


@dataclass(frozen=True, slots=True)
class Decision:
    """The resolved decision the UI hands back to the registry."""
    result: DecisionResult
    reason: str | None = None              # required when DENY/BLOCK
    additional_context: str | None = None  # required when INJECT
    remember: bool = False                 # only meaningful for ALLOW + PRE_TOOL_USE
    # ASK_QUESTION answer relay: maps each question's text → the picked
    # option(s). HookServer merges this into the original ``tool_input``
    # as ``answers`` and returns it via ``hookSpecificOutput.decision
    # .updatedInput`` so Claude's AskUserQuestion tool sees the user's
    # choice on its first read and skips the terminal stdin prompt.
    # Mirrors open-vibe-island ``BridgeServer.swift:2434-2481``.
    # Modelled as a tuple of (question_text, answer) pairs instead of a
    # dict so the dataclass stays frozen + hashable; HookServer rebuilds
    # the dict at merge time.
    answers: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        if self.result in (DecisionResult.DENY, DecisionResult.BLOCK) and not self.reason:
            raise ValueError(f"{self.result.value} requires a non-empty reason")
        if self.result is DecisionResult.INJECT and not self.additional_context:
            raise ValueError("INJECT requires non-empty additional_context")
        if self.remember and self.result is not DecisionResult.ALLOW:
            raise ValueError("remember=True only valid with ALLOW")
        if self.answers and self.result is not DecisionResult.ALLOW:
            raise ValueError("answers only valid with ALLOW")


@dataclass(frozen=True, slots=True)
class PendingDecisionView:
    """Render-side projection — what UI sees in WorldSnapshot.

    Deliberately separate from DecisionRequest because:
      (a) tool_input may be very large; UI never needs it raw
      (b) we never want secrets (env vars in Bash) leaking into snapshot
      (c) reduces the surface of fields UI reads from the registry
    """
    id: str
    kind: DecisionKind
    session_uuid: str
    session_name: str
    cwd_basename: str
    expires_at: datetime
    risk_level: RiskLevel
    # PRE_TOOL_USE / ASK_QUESTION
    tool_name: str | None = None
    tool_input_preview: str | None = None
    # USER_PROMPT_SUBMIT-only
    prompt_preview: str | None = None
    # ASK_QUESTION-only
    question_text: str | None = None
    question_header: str | None = None
    question_options: tuple[str, ...] = ()
    question_option_descriptions: tuple[str, ...] = ()
    multi_select: bool = False


class RegistryFull(Exception):
    """Raised by ``register`` when at MAX_PENDING_DECISIONS cap.
    Caller (HookServer) should reply ``defer`` immediately."""


# ---------------------------------------------------------------------------
# Internal state
# ---------------------------------------------------------------------------


@dataclass
class _Entry:
    """Mutable per-decision server-side state. Never exposed to UI."""
    request: DecisionRequest
    event: threading.Event = field(default_factory=threading.Event)
    decision: Decision | None = None     # set on resolve(), or remains None on expiry


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


OnChangeCallback = Callable[[], None]


class PendingDecisionRegistry:
    """Thread-safe map of decision-id → pending state.

    Invariants
    ----------
    * Every entry is in exactly one of: PENDING (event not set), RESOLVED
      (event set + decision attached), or DROPPED (removed from _entries).
    * Removal happens on resolve(), wait() timeout, or evict_expired().
    * The on_change callback fires whenever the registry's snapshot()
      output would change — so the snapshotter can wake and rebuild.

    Lock discipline
    ---------------
    * Single threading.Lock guards _entries dict mutations only.
    * threading.Event.wait/set is its own synchronization — never called
      while holding the dict lock (would risk deadlock if a callback
      tried to register).
    """

    def __init__(self, *, on_change: OnChangeCallback | None = None) -> None:
        self._entries: dict[str, _Entry] = {}
        self._lock = threading.Lock()
        self._on_change = on_change or (lambda: None)

    # ── server-thread API ───────────────────────────────────────────────

    def register(self, request: DecisionRequest) -> str:
        """Register a new pending decision. Returns the request id.

        Raises RegistryFull if at MAX_PENDING_DECISIONS cap.
        """
        with self._lock:
            if len(self._entries) >= MAX_PENDING_DECISIONS:
                raise RegistryFull(
                    f"at cap of {MAX_PENDING_DECISIONS} pending decisions"
                )
            if request.id in self._entries:
                # Defensive — caller shouldn't reuse ids; uuid4 makes
                # this practically impossible. Treat as bug.
                raise ValueError(f"duplicate decision id: {request.id!r}")
            self._entries[request.id] = _Entry(request=request)
        self._on_change()
        return request.id

    def wait(self, decision_id: str, timeout_s: float) -> Decision | None:
        """Block calling thread until the decision resolves or timeout.

        Returns the resolved Decision, or None on timeout.

        Side effect on timeout: the entry is dropped from the registry
        (so a late resolve() returns False rather than landing in a
        zombie entry). on_change fires for the snapshot to refresh.

        It's safe to call wait() exactly once per decision_id from one
        thread. Concurrent wait() on the same id is undefined.
        """
        with self._lock:
            entry = self._entries.get(decision_id)
        if entry is None:
            log.warning("wait() called on unknown decision id: %s", decision_id)
            return None

        # Cap caller-provided timeout at the entry's own remaining lifetime.
        # If caller passes a too-large timeout, we still respect expires_at
        # so a stale entry doesn't get blocked-on past its intended window.
        # The 2s safety buffer (vs hook process's POST timeout) is the
        # caller's (HookServer's) responsibility, not the registry's.
        now = datetime.now(timezone.utc)
        remaining = (entry.request.expires_at - now).total_seconds()
        effective_timeout = max(0.0, min(timeout_s, remaining))

        if effective_timeout <= 0.0 and not entry.event.is_set():
            # Already expired before we even got here.
            self._drop(decision_id, reason="expired before wait")
            return None

        signaled = entry.event.wait(timeout=effective_timeout)

        if not signaled:
            # Timed out → drop and return None. Fire on_change so the
            # snapshot drops the (now-orphaned) card from the UI.
            self._drop(decision_id, reason="wait timeout")
            return None

        # event.set() was called by resolve(); decision is attached.
        # Drop the entry (already resolved; UI doesn't need to see it
        # anymore). on_change so snapshot reflects the removal.
        with self._lock:
            current = self._entries.pop(decision_id, None)
        decision = current.decision if current else entry.decision
        self._on_change()
        return decision

    # ── hook-server API for events that observe the outside world ──────

    def mark_externally_resolved_by_tool(
        self,
        session_uuid: str,
        *,
        tool_use_id: str | None,
        tool_name: str | None,
        observed: str,
    ) -> bool:
        """Drop a pending PRE_TOOL_USE entry because Claude Code already
        finished executing the tool through some other path (CLI prompt,
        fail-open default rules, hook timeout fallback, etc.).

        Unlike ``resolve``, this attaches no Decision — the registry
        truthfully does not know what the outside path decided. The
        waiting server thread will see ``entry.decision is None`` and
        return None, which the HookServer encodes as ``"defer"``. By
        that time Claude Code has already moved past this PreToolUse,
        so the directive is effectively a no-op on the wire; the value
        of this call is letting the UI card disappear immediately
        instead of waiting out the full 598 s timeout.

        Match strategy:
          1. tool_use_id exact match scoped to session_uuid (precise)
          2. fallback: (session_uuid, tool_name) FIFO — earliest
             created_at among pending entries for that pair

        Returns True iff an entry was matched and dropped. False means
        no pending entry matched (already resolved by UI, never
        registered, or different session) — caller treats as no-op.
        """
        if not session_uuid:
            return False
        if not tool_use_id and not tool_name:
            return False
        with self._lock:
            target_did: str | None = None
            match_kind = ""
            if tool_use_id is not None:
                for did, e in self._entries.items():
                    if (
                        not e.event.is_set()
                        and e.request.session_uuid == session_uuid
                        and e.request.tool_use_id == tool_use_id
                    ):
                        target_did = did
                        match_kind = "tool_use_id"
                        break
            if target_did is None and tool_name:
                # FIFO over (session_uuid, tool_name) — pick earliest
                # created_at. Tools run serially per session so duplicates
                # are rare; FIFO ensures we don't clear the wrong card
                # when they do appear.
                candidates = [
                    (e.request.created_at, did)
                    for did, e in self._entries.items()
                    if (
                        not e.event.is_set()
                        and e.request.session_uuid == session_uuid
                        and e.request.tool_name == tool_name
                    )
                ]
                if candidates:
                    candidates.sort()
                    target_did = candidates[0][1]
                    match_kind = "tool_name_fifo"
            if target_did is None:
                return False
            entry = self._entries.pop(target_did)
            entry.event.set()
        # decision stays None — see docstring
        self._on_change()
        log.info(
            "decision %s externally resolved (observed=%s, match=%s)",
            target_did, observed, match_kind,
        )
        return True

    def evict_stale_pending(
        self,
        session_uuid: str,
        *,
        except_tool_use_id: str | None = None,
    ) -> int:
        """Drop pending PRE_TOOL_USE entries for ``session_uuid`` whose
        ``tool_use_id`` differs from ``except_tool_use_id``.

        Used when a NEW PreToolUse / PermissionRequest arrives for the
        session: any older entries with a different tool_use_id are
        stale orphans. The classic trigger is AskUserQuestion declined
        in the terminal — Claude Code emits no PostToolUse for a
        declined question (no tool result to report), so
        ``_maybe_mark_resolved_by_post`` never matches, and the UI card
        sits stuck for the full 598 s wait timeout. When Claude
        proceeds to the next tool, this sweeps the orphan.

        Entries with the SAME tool_use_id as ``except_tool_use_id`` (or
        with no tool_use_id when except is None) are preserved — they
        represent the active permission request currently being routed.

        Like :meth:`evict_session_pending`, attaches no Decision; the
        waiting server thread sees ``entry.decision is None`` and
        HookServer encodes ``"defer"``.

        Returns count dropped. No-op on empty ``session_uuid``.
        """
        if not session_uuid:
            return 0
        with self._lock:
            to_drop = [
                did for did, e in self._entries.items()
                if (
                    not e.event.is_set()
                    and e.request.session_uuid == session_uuid
                    and e.request.tool_use_id != except_tool_use_id
                )
            ]
            for did in to_drop:
                entry = self._entries.pop(did)
                entry.event.set()
        if to_drop:
            self._on_change()
            log.info(
                "evicted %d stale pending decision(s) for session %s "
                "(except tool_use_id=%r)",
                len(to_drop), session_uuid, except_tool_use_id,
            )
        return len(to_drop)

    def evict_session_pending(self, session_uuid: str) -> int:
        """Drop every pending entry belonging to ``session_uuid``.

        Used by HookServer when Claude Code reports the turn ended
        (Stop / StopFailure) — at that point any still-pending
        PermissionRequest is an orphan (the user Esc-interrupted the
        tool, so Claude killed the blocking hook.py subprocess but never
        sent a PostToolUseFailure or PermissionDenied that
        ``mark_externally_resolved_by_tool`` could match on).

        Like ``mark_externally_resolved_by_tool``, this attaches no
        Decision — the waiting server thread sees ``entry.decision is
        None`` and HookServer encodes that as ``"defer"`` (a no-op on
        the wire by the time it gets back to Claude, since the tool was
        already cancelled). The value here is letting the UI card
        disappear immediately instead of waiting out the 598 s timeout.

        Returns count dropped. No-op when ``session_uuid`` is empty so
        a malformed Stop payload can't accidentally evict everything.
        """
        if not session_uuid:
            return 0
        with self._lock:
            to_drop = [
                did for did, e in self._entries.items()
                if not e.event.is_set() and e.request.session_uuid == session_uuid
            ]
            for did in to_drop:
                entry = self._entries.pop(did)
                entry.event.set()
        # decision stays None on each — wait() returns None → defer
        if to_drop:
            self._on_change()
            log.info(
                "evicted %d pending decision(s) for session %s on turn end",
                len(to_drop), session_uuid,
            )
        return len(to_drop)

    # ── UI-thread API (called from AppBackend.resolve_decision) ─────────

    def resolve(self, decision_id: str, decision: Decision) -> bool:
        """Mark a pending decision as resolved.

        Returns True iff the id existed and we set the decision (i.e.
        a server thread is/was waiting). Returns False if the id is
        unknown, already resolved, or had timed out.

        Doesn't drop the entry — wait() drops on its way out so the
        snapshot doesn't briefly flicker the card.
        """
        with self._lock:
            entry = self._entries.get(decision_id)
            if entry is None or entry.event.is_set():
                return False
            entry.decision = decision
            entry.event.set()
        # fire outside the lock; callback may itself acquire other locks
        self._on_change()
        return True

    # ── snapshot / introspection (any thread) ───────────────────────────

    def snapshot(self) -> tuple[PendingDecisionView, ...]:
        """Immutable, time-ordered view for inclusion in WorldSnapshot.

        Sorted by created_at asc so the UI list is stable regardless of
        dict iteration order. Excludes already-resolved entries (event
        set) — they're about to be wait()'d out anyway and rendering
        them as "still pending" would be a lie.
        """
        with self._lock:
            entries = [
                e for e in self._entries.values()
                if not e.event.is_set()
            ]
        entries.sort(key=lambda e: e.request.created_at)
        return tuple(_to_view(e.request) for e in entries)

    def evict_expired(self, *, now: datetime | None = None) -> int:
        """Drop entries whose expires_at has passed and never resolved.

        Called periodically by a 60 s timer in __main__. Idempotent:
        already-resolved entries (event set) are ignored — they'll be
        wait()'d out by the server thread shortly.

        Returns count dropped.
        """
        ts = now or datetime.now(timezone.utc)
        with self._lock:
            to_drop = [
                did for did, e in self._entries.items()
                if not e.event.is_set() and e.request.expires_at <= ts
            ]
            for did in to_drop:
                self._entries.pop(did, None)
        if to_drop:
            log.info("evict_expired dropped %d pending decisions", len(to_drop))
            self._on_change()
        return len(to_drop)

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)

    # ── internal ────────────────────────────────────────────────────────

    def _drop(self, decision_id: str, *, reason: str) -> None:
        with self._lock:
            popped = self._entries.pop(decision_id, None)
        if popped is not None:
            log.debug("dropped decision %s: %s", decision_id, reason)
            self._on_change()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def new_decision_id() -> str:
    """Public so callers can build DecisionRequest with a stable id
    (handy for tests; production HookServer just calls this once)."""
    return _uuid.uuid4().hex


def _to_view(req: DecisionRequest) -> PendingDecisionView:
    return PendingDecisionView(
        id=req.id,
        kind=req.kind,
        session_uuid=req.session_uuid,
        session_name=req.session_name,
        cwd_basename=req.cwd_basename,
        expires_at=req.expires_at,
        risk_level=req.risk_level,
        tool_name=req.tool_name,
        tool_input_preview=req.tool_input_preview,
        prompt_preview=req.prompt_preview,
        question_text=req.question_text,
        question_header=req.question_header,
        question_options=req.question_options,
        question_option_descriptions=req.question_option_descriptions,
        multi_select=req.multi_select,
    )


# Tools whose damage radius warrants a HIGH risk badge on ApprovalCard
# (and the prominent "this remembers ALL Bash calls" warning when the
# user ticks the remember checkbox).
_HIGH_RISK_TOOLS = frozenset({
    "Bash", "Write", "Edit", "MultiEdit", "NotebookEdit",
    # MCP-prefixed tools that invoke shell-like behavior follow same convention
})

_LOW_RISK_TOOLS = frozenset({
    "Read", "Glob", "LS", "TodoWrite", "WebFetch",
})


def classify_risk(tool_name: str) -> RiskLevel:
    """Pure helper for HookServer to set RiskLevel on the request.

    Conservative default: anything not explicitly low → MEDIUM. Means
    new MCP tools start MEDIUM (yellow card) and we promote/demote as
    we see real usage. Better than silently treating an unknown shell-
    spawning MCP tool as LOW.
    """
    if tool_name in _HIGH_RISK_TOOLS:
        return RiskLevel.HIGH
    if tool_name in _LOW_RISK_TOOLS:
        return RiskLevel.LOW
    return RiskLevel.MEDIUM


def build_request(
    *,
    kind: DecisionKind,
    session_uuid: str,
    session_name: str,
    cwd: Path,
    hook_event: str,
    timeout_s: float,
    now: datetime | None = None,
    tool_name: str | None = None,
    tool_input_preview: str | None = None,
    tool_use_id: str | None = None,
    prompt_preview: str | None = None,
    question_text: str | None = None,
    question_header: str | None = None,
    question_options: tuple[str, ...] = (),
    question_option_descriptions: tuple[str, ...] = (),
    multi_select: bool = False,
) -> DecisionRequest:
    """Convenience constructor that fills in id, timestamps, risk_level,
    and cwd_basename consistently. Use from HookServer; keeps callers
    out of the timezone / uuid weeds."""
    created = now or datetime.now(timezone.utc)
    expires = created + timedelta(seconds=timeout_s)
    risk = classify_risk(tool_name) if tool_name else RiskLevel.MEDIUM
    return DecisionRequest(
        id=new_decision_id(),
        kind=kind,
        session_uuid=session_uuid,
        session_name=session_name,
        cwd=cwd,
        cwd_basename=cwd.name or str(cwd),
        hook_event=hook_event,
        created_at=created,
        expires_at=expires,
        tool_name=tool_name,
        tool_input_preview=tool_input_preview,
        tool_use_id=tool_use_id,
        prompt_preview=prompt_preview,
        question_text=question_text,
        question_header=question_header,
        question_options=question_options,
        question_option_descriptions=question_option_descriptions,
        multi_select=multi_select,
        risk_level=risk,
    )
