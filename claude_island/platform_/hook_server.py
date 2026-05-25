"""HookServer — HTTP listener that translates Claude Code hook POSTs
into HookEvents and feeds them to the SessionStateMachine.

Lives in platform_/ because it does IPC / binds sockets / writes
~/.claude-island/port.txt. The core state machine doesn't know HookServer
exists; it just receives events.

Trust model (v1): bind 127.0.0.1 only. Any local process can POST a
forged event. Acceptable v1: if a malicious process is already running
as the user, the user is already compromised. v2 (when island starts
actually authorizing PermissionRequest) must upgrade to peer credential
check or short-lived token.

Cold-path failure handling: every internal exception is caught and a
500 returned to the hook.py client. hook.py treats any non-2xx as
"listener broken, fail open, exit 0" — Claude is never blocked by us.

Port retry: tries preferred_port, preferred_port+1, ..., up to 23 ports
(50777..50799). Writes the bound port to port_file. If all ports taken,
raises HookServerStartError — the wiring layer catches and degrades to
scanner-only.
"""
from __future__ import annotations

import json
import logging
import os
import socket
import threading
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from claude_island.core.hook_events import (
    CompactStarted,
    HookEvent,
    JumpTarget,
    NotificationFired,
    PermissionRequested,
    PromptSubmitted,
    SessionEnded,
    SessionStarted,
    ToolFinished,
    ToolStarted,
    TurnCompleted,
)
from claude_island.core.notify import (
    NotifyEventQueue,
    make_turn_complete,
)
from claude_island.core.pending_decisions import (
    DecisionKind,
    DecisionResult,
    PendingDecisionRegistry,
    RegistryFull,
    WAIT_TIMEOUT_SAFETY_S,
    build_request,
)
from claude_island.core.session_permissions import SessionPermissionCache
from claude_island.core.session_state_machine import SessionStateMachine

log = logging.getLogger(__name__)

_DEFAULT_PORT_FILE = Path.home() / ".claude-island" / "port.txt"
_PORT_RETRY_RANGE = 23     # 50777..50799 inclusive
_RECENT_EVENTS_RING_SIZE = 100
_DEFAULT_HOST = "127.0.0.1"

# Length cap for noisy free-text fields stored on SessionLiveState.
# Bounds memory + cuts PII flowing through the in-app state. Sized so
# the UI row preview can render a useful slice without truncating
# mid-word for most common cases.
_PROMPT_MAX = 200
_ASSISTANT_MAX = 300
_TOOL_INPUT_MAX = 200

# Keys to probe in a tool_input dict, in priority order, to extract a
# single human-readable preview string. Mirrors open-vibe-island's
# ``ClaudeHooks.swift`` toolInputPreview probe order — every well-known
# tool (Bash, Edit, Glob, Grep, Read, WebFetch, …) exposes one of
# these. Tools that don't fall through to the AskUserQuestion shape
# check and finally to a JSON fallback (with ensure_ascii=False so CJK
# / non-ASCII stays human-readable).
_TOOL_INPUT_PREVIEW_KEYS: tuple[str, ...] = (
    "command", "file_path", "pattern", "query",
    "prompt", "description", "skill", "url",
)

# AskUserQuestion uses an MCP-style tool_input shape that has no
# single-string field. The questions list carries the human-readable
# text; we surface the first question here so the UI preview is
# meaningful instead of raw JSON.
_ASK_USER_QUESTION_KEY = "questions"
_ASK_USER_QUESTION_TEXT_FIELD = "question"
_ASK_USER_QUESTION_HEADER_FIELD = "header"
_ASK_USER_QUESTION_OPTIONS_FIELD = "options"
_ASK_USER_QUESTION_OPTION_LABEL_FIELD = "label"
_ASK_USER_QUESTION_OPTION_DESC_FIELD = "description"
_ASK_USER_QUESTION_MULTISELECT_FIELD = "multiSelect"

# Tool names whose PermissionRequest should be routed to the question
# (option-picker) UI instead of the standard Allow/Deny approval card.
# Frozen for thread-safety and to signal "extend by editing here".
_QUESTION_TOOL_NAMES: frozenset[str] = frozenset({
    "AskUserQuestion",
})

# Bidirectional-hook timeouts (Bidirectional Hooks v1, 2026-05-14).
#
# Claude Code's command-hook timeout defaults to 600 s. We wait that
# minus a 2 s safety buffer so an expiring entry can write a defer
# directive before the hook process gives up. See
# core/pending_decisions.WAIT_TIMEOUT_SAFETY_S.
_BLOCKING_HOOK_TIMEOUT_S = 600.0 - WAIT_TIMEOUT_SAFETY_S
# Preview length for prompt shown on PromptReviewCard. ≤ 500 per Detail
# Design §2. (No separate cap for PreToolUse tool_input — the upstream
# extractor already caps at _TOOL_INPUT_MAX = 200, so a second truncate
# would be a no-op. Removed dead _PRETOOLUSE_INPUT_PREVIEW_MAX in code
# review A-005.)
_USERPROMPTSUBMIT_PROMPT_PREVIEW_MAX = 500

class HookServerStartError(RuntimeError):
    """Raised when start() cannot bind any port in the retry range."""


class ParseError(ValueError):
    """Raised internally when a hook POST body cannot be turned into a
    HookEvent. The handler catches this and returns 400."""


@dataclass(frozen=True, slots=True)
class RecentEventRecord:
    """One entry in the ring buffer surfaced by /health and --doctor.
    Kept minimal because the ring stays in memory."""
    received_at: datetime
    event_name: str
    session_uuid: str


class HookServer:
    """The HTTP listener. Single public entry: ``start()`` returns the
    bound port. ``stop()`` is idempotent. ``recent_events_log`` and
    ``bound_port`` are read by the /health handler and --doctor."""

    def __init__(
        self,
        state_machine: SessionStateMachine,
        *,
        preferred_port: int = 50777,
        port_file: Path = _DEFAULT_PORT_FILE,
        host: str = _DEFAULT_HOST,
        # Bidirectional-hook deps. All three are optional kwargs so that
        # tests + boot paths that pre-date this feature still construct
        # a working HookServer (it just falls back to today's behaviour:
        # always reply "{}", never block, no notifications).
        pending_registry: PendingDecisionRegistry | None = None,
        permission_cache: SessionPermissionCache | None = None,
        notify_queue: NotifyEventQueue | None = None,
    ) -> None:
        self._sm = state_machine
        self._preferred_port = preferred_port
        self._port_file = port_file
        self._host = host
        self._started_at: datetime | None = None
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._bound_port: int | None = None
        # Ring buffer for /health + --doctor. Bounded so a long-running
        # listener doesn't grow without limit.
        self._recent: deque[RecentEventRecord] = deque(maxlen=_RECENT_EVENTS_RING_SIZE)
        self._recent_lock = threading.Lock()
        # Bidirectional dependencies. None ⇒ feature disabled; the
        # corresponding hook event paths fall through to the legacy
        # "respond {}" behaviour.
        self._pending = pending_registry
        self._perm = permission_cache
        self._notify = notify_queue

    # ── public API ───────────────────────────────────────────────────────

    def start(self) -> int:
        """Bind on the preferred port (or fall back through the retry
        range), write the chosen port to port_file, and serve in a
        daemon thread. Returns the actually-bound port.

        Raises HookServerStartError if every candidate port in the retry
        range is occupied (collision with another claude-island or some
        unrelated local service)."""
        if self._server is not None:
            return self._bound_port  # type: ignore[return-value]

        last_err: OSError | None = None
        for offset in range(_PORT_RETRY_RANGE):
            candidate = self._preferred_port + offset
            try:
                server = ThreadingHTTPServer(
                    (self._host, candidate),
                    _make_handler_class(self),
                )
            except OSError as e:
                last_err = e
                continue
            self._server = server
            # ThreadingMixIn defaults: daemon_threads=False + block_on_close=True.
            # That makes server_close() join every in-flight handler thread —
            # but our bidirectional handlers can be parked in
            # pending.wait(timeout=598s), so a normal stop() would block
            # for up to ~10 minutes. Hook contract is fail-open already
            # (hook.py treats any non-2xx as listener-broken and exits 0),
            # so abandoning a long-poll handler at shutdown is benign.
            # Fixed in code review B-001.
            server.daemon_threads = True
            server.block_on_close = False
            # Read the ACTUALLY-bound port from the socket — when
            # preferred_port=0 (ephemeral, tests) the OS picks the port
            # and `candidate` (0) is the wrong value to expose.
            self._bound_port = server.server_address[1]
            break
        if self._server is None:
            raise HookServerStartError(
                f"could not bind any port in "
                f"{self._preferred_port}..{self._preferred_port + _PORT_RETRY_RANGE - 1}: "
                f"last error: {last_err}"
            )

        self._started_at = datetime.now(timezone.utc)
        # Write port file atomically so partial reads never see a half-written
        # number. mkdir parents so users don't need to pre-create the dir.
        self._port_file.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._port_file.with_suffix(".tmp")
        tmp.write_text(str(self._bound_port), encoding="utf-8")
        os.replace(tmp, self._port_file)

        self._thread = threading.Thread(
            target=self._server.serve_forever,
            name="claude-island-hook-server",
            daemon=True,
        )
        self._thread.start()
        log.info("hook listener bound on %s:%d", self._host, self._bound_port)
        return self._bound_port

    def stop(self) -> None:
        """Idempotent. Shuts down the HTTP server, joins the thread,
        deletes the port file so stale readers don't connect."""
        if self._server is None:
            return
        srv = self._server
        thr = self._thread
        self._server = None
        self._thread = None
        srv.shutdown()
        srv.server_close()
        if thr is not None:
            thr.join(timeout=2.0)
        # Delete port file last so hook.py never sees a port pointing at
        # a dead listener.
        try:
            self._port_file.unlink()
        except OSError:
            pass
        self._bound_port = None
        self._started_at = None

    @property
    def bound_port(self) -> int | None:
        return self._bound_port

    @property
    def recent_events_log(self) -> tuple[RecentEventRecord, ...]:
        """Snapshot of the ring buffer for --doctor."""
        with self._recent_lock:
            return tuple(self._recent)

    # ── internal: called by the handler ──────────────────────────────────

    def _handle_post(self, raw_body: bytes) -> bytes:
        """Parse + apply + decide. Returns the JSON body to write to
        the hook response (which Claude Code reads from stdout).

        Raises ParseError on malformed input. Other exceptions
        propagate (handler converts to 500).

        Bidirectional flow (Bidirectional Hooks v1):
          - PreToolUse: cache pre-check; on miss, register + wait for
            UI decision; encode permissionDecision JSON
          - UserPromptSubmit: review-mode pre-check; on True, register
            + wait for UI decision; encode block / inject directive
          - Stop / StopFailure: push NotifyEvent to queue, reply {}
          - SessionEnd: evict permission grants for this uuid, reply {}
          - everything else: state-machine apply, reply {}
        """
        try:
            payload = json.loads(raw_body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as e:
            raise ParseError(f"invalid JSON body: {e}") from e
        if not isinstance(payload, dict):
            raise ParseError("hook payload must be a JSON object")

        # Hook payload's ``session_id`` is the in-memory current uuid as
        # set by claude.exe — matches pid.json and the JSONL file claude
        # is appending to. Forwarded verbatim: cmdline ``--resume`` goes
        # stale on ``/clear`` and cannot be used as a remap source.
        event = parse_claude_payload(payload)
        if event is not None:
            with self._recent_lock:
                self._recent.append(RecentEventRecord(
                    received_at=event.at,
                    event_name=type(event).__name__,
                    session_uuid=event.session_uuid,
                ))
            # Apply to state machine BEFORE bidirectional routing so the
            # UI sees "phase=TOOL_USE" + "current_tool=Bash" alongside
            # any approval card we render.
            self._sm.apply(event)
        else:
            log.debug(
                "dropping unknown hook event: %r",
                payload.get("hook_event_name"),
            )

        hook_name = payload.get("hook_event_name")
        # Bidirectional event routing. Each branch returns its own body;
        # the catch-all default falls through to "{}" for legacy clients.
        #
        # PreToolUse is intentionally state-machine-only: we used to register
        # an approval card on every tool call, but Claude Code fires
        # PreToolUse unconditionally — including in bypassPermissions /
        # ``skipAutoPermissionPrompt: true`` setups where Claude itself
        # would never have prompted. That over-intercepted, so the
        # approval flow now lives under PermissionRequest, which Claude
        # fires only when it actually intends to ask the user.
        if hook_name == "PreToolUse":
            # Any older pending PRE_TOOL_USE for this session whose
            # tool_use_id differs from the current event's is stale.
            # The classic case: AskUserQuestion declined in the terminal
            # — Claude emits no PostToolUse (no tool result), so the
            # _maybe_mark_resolved_by_post path never fires and the card
            # sits stuck for 598 s. When Claude moves on to the next
            # tool, this sweeps the orphan. PermissionRequest will fire
            # right after PreToolUse and register the fresh entry for
            # the current tool_use_id — the matching except_tool_use_id
            # here guards against evicting that one before it lands.
            self._evict_stale_for_session(payload)
            return b"{}"
        if hook_name == "PermissionRequest":
            # Same orphan-sweep as PreToolUse — defensive in case a
            # permission check fires without a preceding PreToolUse
            # (e.g. an MCP / dynamic-tool path).
            self._evict_stale_for_session(payload)
            return self._handle_permission_request(payload)
        if hook_name == "UserPromptSubmit":
            # New prompt → previous turn's decisions are all moot,
            # even those that share a tool_use_id with anything (which
            # never happens across turns anyway). Full session sweep.
            self._evict_full_session(payload)
            return self._handle_user_prompt_submit(payload)
        if hook_name in ("Stop", "StopFailure"):
            self._handle_stop(payload)
            return b"{}"
        if hook_name == "SessionEnd":
            self._handle_session_end(payload)
            return b"{}"
        if hook_name in ("PostToolUse", "PostToolUseFailure", "PermissionDenied"):
            # An external resolution happened — Claude finished the
            # tool (PostToolUse / PostToolUseFailure) or the user
            # denied in the terminal (PermissionDenied). Either way,
            # the matching island card is stale and must disappear
            # so the user isn't asked to decide what's already
            # decided. See open-vibe-island/BridgeServer.swift:774.
            self._maybe_mark_resolved_by_post(payload, hook_name)
            return b"{}"
        return b"{}"

    def _evict_stale_for_session(self, payload: dict) -> None:
        """Sweep pending PRE_TOOL_USE entries for this payload's session
        whose tool_use_id differs from the payload's own. Best-effort —
        any failure stays silent so the dispatch keeps going."""
        if self._pending is None:
            return
        try:
            uuid = _safe_str(payload.get("session_id"))
            if not uuid:
                return
            tool_use_id = _str_or_none(payload.get("tool_use_id"))
            self._pending.evict_stale_pending(
                uuid, except_tool_use_id=tool_use_id,
            )
        except Exception:
            log.exception("evict_stale_pending raised; ignored")

    def _evict_full_session(self, payload: dict) -> None:
        """Sweep ALL pending entries for this payload's session.
        Used on UserPromptSubmit (a new turn invalidates everything
        from the previous turn)."""
        if self._pending is None:
            return
        try:
            uuid = _safe_str(payload.get("session_id"))
            if not uuid:
                return
            self._pending.evict_session_pending(uuid)
        except Exception:
            log.exception("evict_session_pending raised; ignored")

    # ── bidirectional handlers ──────────────────────────────────────────

    def _maybe_mark_resolved_by_post(
        self, payload: dict, hook_name: str,
    ) -> None:
        """Bridge PostToolUse / PostToolUseFailure into the pending
        registry so the UI card disappears as soon as Claude reports
        the tool finished.

        Errors are swallowed: this is best-effort UI cleanup. The
        state-machine apply already ran upstream in ``_handle_post``;
        a registry failure here must not break that path."""
        if self._pending is None:
            return
        try:
            session_uuid = _safe_str(payload.get("session_id"))
            if not session_uuid:
                return
            tool_use_id = _str_or_none(payload.get("tool_use_id"))
            tool_name = _str_or_none(payload.get("tool_name"))
            if not tool_use_id and not tool_name:
                return
            self._pending.mark_externally_resolved_by_tool(
                session_uuid,
                tool_use_id=tool_use_id,
                tool_name=tool_name,
                observed=hook_name.lower(),
            )
        except Exception:
            log.exception("mark_externally_resolved_by_tool raised; ignored")

    def _handle_permission_request(self, payload: dict) -> bytes:
        """Decide allow / deny for a PermissionRequest event.

        Claude Code fires PermissionRequest only when it intends to prompt
        the user for permission. ``bypassPermissions`` / ``dontAsk`` /
        ``auto`` modes and the ``skipAutoPermissionPrompt`` setting all
        suppress this event on Claude's side, so by the time we get here
        we know Claude wants the user to decide.

        Order:
          1. session_perm_cache hit ⇒ encode allow immediately (fast path)
          2. cache miss + bidirectional disabled ⇒ "{}" (legacy path)
          3. cache miss + bidirectional enabled ⇒ register pending +
             block until UI resolves or wait timeout fires
          4. RegistryFull ⇒ encode defer (Claude falls back to its own
             permission rules)
        """
        if self._pending is None or self._perm is None:
            return b"{}"
        uuid = _safe_str(payload.get("session_id"))
        tool_name = _safe_str(payload.get("tool_name"))
        if not uuid or not tool_name:
            return b"{}"

        # Fast path — granted earlier this session.
        if self._perm.check(uuid, tool_name):
            return _encode_permission_request(
                "allow", reason="auto-allowed (this session)",
            )

        cwd = _safe_path(payload.get("cwd"))
        session_name = self._resolve_session_name(uuid, cwd)
        tool_input = payload.get("tool_input")
        preview = _extract_tool_input_preview(tool_input)
        tool_use_id = _str_or_none(payload.get("tool_use_id"))

        # Tools shaped like AskUserQuestion get routed to the question
        # (option-picker) UI variant; everything else goes through the
        # standard PRE_TOOL_USE (Allow/Deny) approval card. Malformed
        # question payloads fall back to the PRE_TOOL_USE path so the
        # server never hard-fails on a partial input.
        parsed_question = (
            _parse_question_input(tool_input)
            if tool_name in _QUESTION_TOOL_NAMES
            else None
        )

        try:
            if parsed_question is not None:
                req = build_request(
                    kind=DecisionKind.ASK_QUESTION,
                    session_uuid=uuid,
                    session_name=session_name,
                    cwd=cwd,
                    hook_event="PermissionRequest",
                    timeout_s=_BLOCKING_HOOK_TIMEOUT_S,
                    tool_name=tool_name,
                    tool_input_preview=preview or None,
                    tool_use_id=tool_use_id,
                    question_text=parsed_question.text,
                    question_header=parsed_question.header,
                    question_options=parsed_question.options,
                    question_option_descriptions=parsed_question.option_descriptions,
                    multi_select=parsed_question.multi_select,
                )
            else:
                req = build_request(
                    kind=DecisionKind.PRE_TOOL_USE,
                    session_uuid=uuid,
                    session_name=session_name,
                    cwd=cwd,
                    hook_event="PermissionRequest",
                    timeout_s=_BLOCKING_HOOK_TIMEOUT_S,
                    tool_name=tool_name,
                    # _extract_tool_input_preview already truncates to
                    # _TOOL_INPUT_MAX upstream; no second cap needed.
                    tool_input_preview=preview or None,
                    # Carried so a later PostToolUse / PostToolUseFailure
                    # can be correlated back to this entry and clear the
                    # UI card without waiting out the 598 s timeout.
                    tool_use_id=tool_use_id,
                )
            decision_id = self._pending.register(req)
        except RegistryFull:
            log.warning(
                "pending registry full; deferring PermissionRequest(%s)",
                tool_name,
            )
            return _encode_permission_request(
                "defer", reason="claude-island queue full",
            )

        decision = self._pending.wait(
            decision_id, timeout_s=_BLOCKING_HOOK_TIMEOUT_S,
        )
        if decision is None:
            log.info(
                "PermissionRequest(%s) decision wait timed out — defer",
                tool_name,
            )
            return _encode_permission_request(
                "defer", reason="user did not respond in time",
            )

        # Side effect: if user ticked "remember", grant the cache so
        # future PermissionRequest hits the fast path.
        if decision.remember and decision.result is DecisionResult.ALLOW:
            self._perm.grant(uuid, tool_name)

        if decision.result is DecisionResult.ALLOW:
            if decision.answers:
                # Answer relay for AskUserQuestion: merge the user's
                # picks into the original tool_input under "answers"
                # and return them via updatedInput so Claude's tool
                # sees the choice on its first read and skips the
                # stdin prompt. Mirrors open-vibe-island
                # mergedClaudeQuestionInput (BridgeServer.swift:2434).
                merged = _merge_answers_into_tool_input(
                    tool_input, decision.answers,
                )
                return _encode_permission_request(
                    "allow", updated_input=merged,
                )
            return _encode_permission_request("allow")
        if decision.result is DecisionResult.DENY:
            return _encode_permission_request(
                "deny", reason=decision.reason or "denied by user",
            )
        # Defensive: BLOCK / INJECT shouldn't appear for PermissionRequest;
        # treat as defer rather than crashing.
        log.warning(
            "unexpected decision result for PermissionRequest: %s",
            decision.result,
        )
        return _encode_permission_request(
            "defer", reason=f"unexpected decision: {decision.result.value}",
        )

    def _handle_user_prompt_submit(self, payload: dict) -> bytes:
        """Decide allow / block / inject for a UserPromptSubmit event.

        v1 default: review mode is OFF per session ⇒ fast-path "{}"
        and let the prompt through. ON ⇒ register pending + wait.
        """
        if self._pending is None or self._perm is None:
            return b"{}"
        uuid = _safe_str(payload.get("session_id"))
        if not uuid:
            return b"{}"
        if not self._perm.is_review(uuid):
            # Default OFF — the user hasn't opted in to reviewing prompts.
            return b"{}"

        cwd = _safe_path(payload.get("cwd"))
        session_name = self._resolve_session_name(uuid, cwd)
        prompt = _safe_str(payload.get("prompt"))
        try:
            req = build_request(
                kind=DecisionKind.USER_PROMPT_SUBMIT,
                session_uuid=uuid,
                session_name=session_name,
                cwd=cwd,
                hook_event="UserPromptSubmit",
                timeout_s=_BLOCKING_HOOK_TIMEOUT_S,
                prompt_preview=_truncate(
                    prompt, _USERPROMPTSUBMIT_PROMPT_PREVIEW_MAX,
                ) or "",
            )
            decision_id = self._pending.register(req)
        except RegistryFull:
            log.warning(
                "pending registry full; passing through UserPromptSubmit",
            )
            return b"{}"

        decision = self._pending.wait(
            decision_id, timeout_s=_BLOCKING_HOOK_TIMEOUT_S,
        )
        if decision is None:
            log.info("UserPromptSubmit wait timed out — passing prompt through")
            return b"{}"

        if decision.result is DecisionResult.ALLOW:
            return b"{}"
        if decision.result is DecisionResult.BLOCK:
            return _encode_userpromptsubmit_block(
                decision.reason or "blocked by user",
            )
        if decision.result is DecisionResult.INJECT:
            return _encode_userpromptsubmit_inject(
                decision.additional_context or "",
            )
        log.warning(
            "unexpected decision result for UserPromptSubmit: %s",
            decision.result,
        )
        return b"{}"

    def _handle_stop(self, payload: dict) -> None:
        """Push a NotifyEvent for Stop / StopFailure and evict any
        orphan pending decisions for this session.

        Eviction reason: when the user Esc-interrupts a tool whose
        PermissionRequest is still blocked in ``_handle_permission_request``
        (waiting on the UI), Claude Code kills the hook.py subprocess
        but does NOT send a follow-up PostToolUseFailure /
        PermissionDenied that ``_maybe_mark_resolved_by_post`` could
        match on. The only signal we still get is this Stop / StopFailure
        marking the turn end — at which point any remaining pending
        entry for this session is an orphan we must clear so the UI
        card doesn't sit stale for the full 598 s wait timeout.

        Both writes are best-effort: the registry is the second concern,
        so a notify-queue None must not block eviction and vice versa.
        Non-blocking either way; caller writes "{}" immediately so
        Claude Code never waits on this path.
        """
        uuid = _safe_str(payload.get("session_id"))
        if not uuid:
            return
        cwd = _safe_path(payload.get("cwd"))
        is_failure = payload.get("hook_event_name") == "StopFailure"
        if self._notify is not None:
            self._notify.push(make_turn_complete(
                session_uuid=uuid,
                session_name=self._resolve_session_name(uuid, cwd),
                cwd_basename=cwd.name or "session",
                is_failure=is_failure,
            ))
        if self._pending is not None:
            try:
                self._pending.evict_session_pending(uuid)
            except Exception:
                log.exception("evict_session_pending raised; ignored")

    def _handle_session_end(self, payload: dict) -> None:
        """Evict the SessionPermissionCache entries for the ending session.
        Cap is 4 h TTL otherwise; this is the precise eviction signal."""
        if self._perm is None:
            return
        uuid = _safe_str(payload.get("session_id"))
        if not uuid:
            return
        self._perm.evict_session(uuid)

    # ── helpers ─────────────────────────────────────────────────────────

    def _resolve_session_name(self, uuid: str, cwd: Path) -> str:
        """Best-effort name for use in PendingDecisionView / NotifyEvent.

        Future iteration can plumb in SessionRegistry / NamesStore for
        the user-facing name; v1 falls back to cwd.name or uuid prefix
        which is good enough for the approval-card heading.
        """
        if cwd and cwd.name:
            return cwd.name
        if uuid:
            return uuid[:8]
        return "session"

    def _handle_health(self) -> dict[str, Any]:
        """Returns a dict serialized as the /health response."""
        now = datetime.now(timezone.utc)
        uptime = (now - self._started_at).total_seconds() if self._started_at else 0.0
        with self._recent_lock:
            count = len(self._recent)
            last = self._recent[-1] if self._recent else None
        return {
            "port": self._bound_port,
            "uptime_s": int(uptime),
            "recent_event_count": count,
            "last_event_name": last.event_name if last else None,
            "last_event_at": last.received_at.isoformat() if last else None,
        }


# ---------------------------------------------------------------------------
# Payload parser — Claude hook JSON → HookEvent union.
# ---------------------------------------------------------------------------


def parse_claude_payload(payload: dict) -> HookEvent | None:
    """Convert a Claude Code hook POST body into a HookEvent.

    Returns None when:
      * hook_event_name is missing or unknown
      * session_id is missing or empty

    Never raises on malformed values — best-effort extraction with
    sane fallbacks. The parser is liberal in what it accepts so a
    minor Claude schema bump doesn't break us; missing optional fields
    become None.
    """
    hook_name = payload.get("hook_event_name")
    uuid = payload.get("session_id") or ""
    if not isinstance(uuid, str) or not uuid:
        return None
    if not isinstance(hook_name, str):
        return None

    at = datetime.now(timezone.utc)
    cwd_str = payload.get("cwd") or ""
    cwd = Path(cwd_str) if cwd_str else Path(".")

    if hook_name == "SessionStart":
        source = payload.get("source") if isinstance(payload.get("source"), str) else None
        transcript = payload.get("transcript_path")
        return SessionStarted(
            session_uuid=uuid,
            cwd=cwd,
            started_at=at,
            source=source,
            transcript_path=Path(transcript) if isinstance(transcript, str) else None,
            at=at,
            jump_target=_parse_jump_target(payload.get("jump_target")),
        )

    if hook_name == "SessionEnd":
        return SessionEnded(session_uuid=uuid, at=at)

    if hook_name == "UserPromptSubmit":
        prompt = payload.get("prompt") or ""
        if not isinstance(prompt, str):
            prompt = ""
        return PromptSubmitted(
            session_uuid=uuid,
            prompt=_truncate(prompt, _PROMPT_MAX),
            at=at,
        )

    if hook_name == "PreToolUse":
        tool_name = payload.get("tool_name") or ""
        if not isinstance(tool_name, str) or not tool_name:
            return None    # malformed PreToolUse, drop
        tool_input = payload.get("tool_input")
        return ToolStarted(
            session_uuid=uuid,
            tool_name=tool_name,
            tool_input_preview=_extract_tool_input_preview(tool_input),
            tool_use_id=_str_or_none(payload.get("tool_use_id")),
            at=at,
        )

    if hook_name in ("PostToolUse", "PostToolUseFailure"):
        tool_name = payload.get("tool_name") or ""
        if not isinstance(tool_name, str) or not tool_name:
            return None
        return ToolFinished(
            session_uuid=uuid,
            tool_name=tool_name,
            tool_use_id=_str_or_none(payload.get("tool_use_id")),
            is_failure=(hook_name == "PostToolUseFailure"),
            at=at,
        )

    if hook_name in ("Stop", "StopFailure"):
        msg = payload.get("last_assistant_message") or payload.get("message") or ""
        if not isinstance(msg, str):
            msg = ""
        return TurnCompleted(
            session_uuid=uuid,
            last_assistant_message=_truncate(msg, _ASSISTANT_MAX) if msg else None,
            is_failure=(hook_name == "StopFailure"),
            at=at,
        )

    if hook_name == "PermissionRequest":
        return PermissionRequested(
            session_uuid=uuid,
            tool_name=_str_or_none(payload.get("tool_name")),
            at=at,
        )

    if hook_name == "PreCompact":
        return CompactStarted(session_uuid=uuid, at=at)

    if hook_name == "Notification":
        # is_idle: Claude uses notification_type / subtype to indicate
        # idle prompts vs other notifications. Mirror open-vibe-island's
        # heuristic (ClaudeHooks.swift `isIdleNotification`).
        nt = payload.get("notification_type")
        st = payload.get("subtype")
        values = [
            v.lower().strip() for v in (nt, st)
            if isinstance(v, str)
        ]
        is_idle = "idle_prompt" in values or "away_summary" in values
        return NotificationFired(session_uuid=uuid, is_idle=is_idle, at=at)

    # Subagent hooks and unknown variants are dropped (v1 doesn't track
    # subagents separately; their phase mirrors the parent).
    return None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse_jump_target(raw: Any) -> JumpTarget | None:
    """Build a JumpTarget from the hook-injected ``jump_target`` sub-dict.

    Tolerant: missing/malformed fields default to empty/zero rather than
    raising. Returns None when the input isn't a dict at all (old hook.py
    that pre-dates jump_target capture) so the caller can pass None along.
    """
    if not isinstance(raw, dict):
        return None
    def _s(key: str) -> str:
        v = raw.get(key)
        return v if isinstance(v, str) else ""
    def _i(key: str) -> int:
        v = raw.get(key)
        if isinstance(v, bool):  # bool is int subclass — exclude
            return 0
        if isinstance(v, int):
            return v
        if isinstance(v, str):
            try: return int(v)
            except ValueError: return 0
        return 0
    return JumpTarget(
        terminal_app=_s("terminal_app") or None,
        conhost_hwnd=_i("conhost_hwnd"),
        host_pid=_i("host_pid"),
        wt_session_guid=_s("wt_session_guid"),
        term_program=_s("term_program"),
        iterm_session_id=_s("iterm_session_id"),
        terminal_pid=_i("terminal_pid"),
    )


def _str_or_none(v: Any) -> str | None:
    return v if isinstance(v, str) and v else None


def _safe_str(v: Any) -> str:
    """Return v if it's a non-empty string, else empty string. Defensive
    helper for hook payload extraction (Claude may send null fields)."""
    return v if isinstance(v, str) else ""


def _safe_path(v: Any) -> Path:
    """Return v as Path if possible, else Path('.') as a benign default."""
    if isinstance(v, str) and v:
        return Path(v)
    return Path(".")


def _truncate(s: str, max_len: int) -> str:
    """Truncate with an ellipsis when needed. Preserves the first
    max_len-1 characters so the result is exactly max_len chars."""
    if len(s) <= max_len:
        return s
    return s[: max_len - 1] + "…"


# ---------------------------------------------------------------------------
# Hook directive encoders
#
# Mirror Claude Code's hook output schema (see
# https://code.claude.com/docs/en/hooks). We dump these as compact JSON
# (no indentation) — every byte over the wire is a byte the hook script
# has to forward to claude.exe.
# ---------------------------------------------------------------------------


def _encode_permission_request(
    permission: str,
    *,
    reason: str | None = None,
    updated_input: dict | None = None,
) -> bytes:
    """PermissionRequest directive.

    Two output shapes — both accepted by Claude Code:

    * **Legacy (default)** — ``hookSpecificOutput.permissionDecision``
      ∈ {"allow", "deny", "ask", "defer"}. Same shape as PreToolUse.

    * **Nested (when ``updated_input`` is given)** —
      ``hookSpecificOutput.decision = {behavior, reason?, updatedInput}``.
      Required to carry ``updatedInput``, which Claude merges into
      ``tool_input`` before executing the tool. Used by the
      AskUserQuestion answer-relay so the user's island-side pick
      reaches Claude without re-prompting on stdin. See
      https://code.claude.com/docs/en/hooks for the schema.

    Kept as one helper because every caller already routes its
    ``allow``/``deny``/``defer`` through here — splitting would
    duplicate the JSON envelope.
    """
    if updated_input is None:
        inner: dict = {
            "hookEventName": "PermissionRequest",
            "permissionDecision": permission,
        }
        if reason:
            inner["permissionDecisionReason"] = reason
        return json.dumps({"hookSpecificOutput": inner}).encode("utf-8")
    decision: dict = {"behavior": permission}
    if reason:
        decision["reason"] = reason
    decision["updatedInput"] = updated_input
    inner = {
        "hookEventName": "PermissionRequest",
        "decision": decision,
    }
    return json.dumps({"hookSpecificOutput": inner}).encode("utf-8")


def _encode_userpromptsubmit_block(reason: str) -> bytes:
    """UserPromptSubmit block directive: top-level ``decision: "block"``
    with required reason. Claude shows the reason to the user and erases
    the prompt from the transcript."""
    return json.dumps({"decision": "block", "reason": reason}).encode("utf-8")


def _encode_userpromptsubmit_inject(context: str) -> bytes:
    """UserPromptSubmit inject directive: ``additionalContext`` inside
    ``hookSpecificOutput``. Claude appends this to the model's context
    alongside the user's prompt."""
    return json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": context,
        },
    }).encode("utf-8")


@dataclass(frozen=True, slots=True)
class _ParsedQuestion:
    """Structured projection of AskUserQuestion's first question.
    Returned by ``_parse_question_input`` so the caller doesn't have to
    re-walk the JSON shape."""
    text: str
    header: str | None
    options: tuple[str, ...]
    option_descriptions: tuple[str, ...]
    multi_select: bool


def _parse_question_input(tool_input: Any) -> _ParsedQuestion | None:
    """Pull the structured question out of an AskUserQuestion payload.

    Returns None if the shape doesn't match — caller falls back to
    regular PRE_TOOL_USE handling so a malformed payload never blocks
    the hook server entirely.
    """
    if not isinstance(tool_input, dict):
        return None
    questions = tool_input.get(_ASK_USER_QUESTION_KEY)
    if not isinstance(questions, list) or not questions:
        return None
    first = questions[0]
    if not isinstance(first, dict):
        return None
    text = first.get(_ASK_USER_QUESTION_TEXT_FIELD)
    if not isinstance(text, str) or not text:
        return None

    raw_options = first.get(_ASK_USER_QUESTION_OPTIONS_FIELD)
    if not isinstance(raw_options, list) or not raw_options:
        return None
    labels: list[str] = []
    descs: list[str] = []
    for opt in raw_options:
        if not isinstance(opt, dict):
            continue
        label = opt.get(_ASK_USER_QUESTION_OPTION_LABEL_FIELD)
        if not isinstance(label, str) or not label:
            continue
        labels.append(label)
        desc = opt.get(_ASK_USER_QUESTION_OPTION_DESC_FIELD)
        descs.append(desc if isinstance(desc, str) else "")
    if not labels:
        return None
    # All descs empty → drop the tuple entirely so the view doesn't
    # carry a parallel array of "" that the validator would still let
    # through but the UI would render as empty subtitle slots.
    descriptions_tuple: tuple[str, ...] = (
        tuple(descs) if any(d for d in descs) else ()
    )
    header = first.get(_ASK_USER_QUESTION_HEADER_FIELD)
    return _ParsedQuestion(
        text=text,
        header=header if isinstance(header, str) and header else None,
        options=tuple(labels),
        option_descriptions=descriptions_tuple,
        multi_select=bool(first.get(_ASK_USER_QUESTION_MULTISELECT_FIELD, False)),
    )


def _merge_answers_into_tool_input(
    tool_input: Any,
    answers: tuple[tuple[str, str], ...],
) -> dict:
    """Build the ``updatedInput`` payload for an AskUserQuestion allow.

    Copies the original ``tool_input`` (defensively defaulting to ``{}``
    if it wasn't a dict) and adds an ``answers`` key mapping each
    question's text to the user's pick — keys mirror what Claude's
    AskUserQuestion tool expects so it skips the stdin prompt and
    emits the tool result directly.

    Shape matches open-vibe-island ``mergedClaudeQuestionInput``
    (BridgeServer.swift:2434-2481).
    """
    merged: dict = dict(tool_input) if isinstance(tool_input, dict) else {}
    merged["answers"] = {q: a for q, a in answers}
    return merged


def _extract_ask_user_question_preview(tool_input: dict) -> str | None:
    """Surface the first question text from an AskUserQuestion tool_input.

    Shape: ``{"questions": [{"question": str, ...}, ...]}``. Returns
    ``None`` when the shape doesn't match (caller falls through to the
    generic preview path). When multiple questions are present, the
    first one's text is suffixed with ``(+N more)``.
    """
    questions = tool_input.get(_ASK_USER_QUESTION_KEY)
    if not isinstance(questions, list) or not questions:
        return None
    first = questions[0]
    if not isinstance(first, dict):
        return None
    text = first.get(_ASK_USER_QUESTION_TEXT_FIELD)
    if not isinstance(text, str) or not text:
        return None
    extra = len(questions) - 1
    return f"{text}  (+{extra} more)" if extra > 0 else text


def _extract_tool_input_preview(tool_input: Any) -> str | None:
    """Pull the most renderable single string out of tool_input.

    Probe order:
      1. Well-known single-string keys (``_TOOL_INPUT_PREVIEW_KEYS``).
      2. AskUserQuestion shape (first question text).
      3. JSON fallback with ``ensure_ascii=False`` — keeps CJK / Unicode
         human-readable instead of ``\\u6307\\u6570`` escapes the
         default ``json.dumps`` emits.
    """
    if tool_input is None:
        return None
    if isinstance(tool_input, dict):
        for key in _TOOL_INPUT_PREVIEW_KEYS:
            v = tool_input.get(key)
            if isinstance(v, str) and v:
                return _truncate(v, _TOOL_INPUT_MAX)
        question_text = _extract_ask_user_question_preview(tool_input)
        if question_text is not None:
            return _truncate(question_text, _TOOL_INPUT_MAX)
        return _truncate(
            json.dumps(tool_input, default=str, ensure_ascii=False),
            _TOOL_INPUT_MAX,
        )
    if isinstance(tool_input, str):
        return _truncate(tool_input, _TOOL_INPUT_MAX) if tool_input else None
    return _truncate(repr(tool_input), _TOOL_INPUT_MAX)


# ---------------------------------------------------------------------------
# HTTP handler — built as a closure-bound class so each instance binds
# back to its parent HookServer for state access.
# ---------------------------------------------------------------------------


def _make_handler_class(server: HookServer) -> type[BaseHTTPRequestHandler]:
    class _Handler(BaseHTTPRequestHandler):
        # Suppress the default-to-stderr access log; we use our own log.
        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            log.debug("hook listener: " + format, *args)

        def do_POST(self) -> None:
            if self.path != "/hook":
                self.send_response(404)
                self.end_headers()
                return
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = 0
            if length <= 0 or length > 1_000_000:  # 1MB sanity cap
                self.send_response(400)
                self.end_headers()
                return
            raw = self.rfile.read(length)
            try:
                # Bidirectional Hooks v1: _handle_post returns the JSON
                # body to write back. May block for blocking events
                # (PreToolUse, UserPromptSubmit) up to ~600 s while
                # waiting for the UI to resolve the pending decision.
                body = server._handle_post(raw)
            except ParseError as e:
                log.debug("parse error: %s", e)
                self.send_response(400)
                self.end_headers()
                return
            except Exception:
                log.exception("hook handler raised; returning 500")
                self.send_response(500)
                self.end_headers()
                return
            if not body:
                body = b"{}"
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            if self.path != "/health":
                self.send_response(404)
                self.end_headers()
                return
            try:
                data = server._handle_health()
            except Exception:
                log.exception("health handler raised; returning 500")
                self.send_response(500)
                self.end_headers()
                return
            body = json.dumps(data).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    return _Handler
