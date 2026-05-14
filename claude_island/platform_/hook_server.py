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
from typing import Any, Callable

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

    def _handle_post(self, raw_body: bytes) -> None:
        """Parse + apply. Raises ParseError on malformed input.
        Other exceptions propagate (handler converts to 500)."""
        try:
            payload = json.loads(raw_body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as e:
            raise ParseError(f"invalid JSON body: {e}") from e
        if not isinstance(payload, dict):
            raise ParseError("hook payload must be a JSON object")

        event = parse_claude_payload(payload)
        if event is None:
            # Unknown hook_event_name — we don't crash, just ignore.
            log.debug("dropping unknown hook event: %r", payload.get("hook_event_name"))
            return

        with self._recent_lock:
            self._recent.append(RecentEventRecord(
                received_at=event.at,
                event_name=type(event).__name__,
                session_uuid=event.session_uuid,
            ))

        self._sm.apply(event)

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
    )


def _str_or_none(v: Any) -> str | None:
    return v if isinstance(v, str) and v else None


def _truncate(s: str, max_len: int) -> str:
    """Truncate with an ellipsis when needed. Preserves the first
    max_len-1 characters so the result is exactly max_len chars."""
    if len(s) <= max_len:
        return s
    return s[: max_len - 1] + "…"


def _extract_tool_input_preview(tool_input: Any) -> str | None:
    """Pull the most renderable single string out of tool_input.

    Mirrors open-vibe-island's logic (ClaudeHooks.swift toolInputPreview):
    for an object, prefer command / file_path / pattern / query / prompt /
    description / skill / url. For other types, render to string.
    """
    if tool_input is None:
        return None
    if isinstance(tool_input, dict):
        for key in (
            "command", "file_path", "pattern", "query",
            "prompt", "description", "skill", "url",
        ):
            v = tool_input.get(key)
            if isinstance(v, str) and v:
                return _truncate(v, _TOOL_INPUT_MAX)
        # Fallback: stringify
        return _truncate(json.dumps(tool_input, default=str), _TOOL_INPUT_MAX)
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
                server._handle_post(raw)
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
