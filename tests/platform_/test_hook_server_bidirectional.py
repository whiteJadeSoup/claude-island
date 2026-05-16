"""Integration tests for HookServer's bidirectional flow (Bidirectional
Hooks v1, 2026-05-14).

Real loopback HTTP — protocol contract is the test point. Threading
model exercised end-to-end. urllib for the client side because that's
what hook.py actually uses.

Test plan mirrors Detail Design §7 (T3.x cells):
  T3.1 happy   — PreToolUse cache hit ⇒ allow body, no UI blocked
  T3.2 happy   — PreToolUse cold ⇒ blocks, UI resolves, body matches decision
  T3.3 edge    — registry full ⇒ defer body
  T3.4 edge    — UserPromptSubmit toggle OFF ⇒ {} body (default fast-path)
  T3.5 happy   — UserPromptSubmit toggle ON + Inject ⇒ additionalContext body
  T3.6 happy   — Stop ⇒ {} body + NotifyEvent appears in queue
  T3.7 happy   — SessionEnd ⇒ {} body + cache evicted
"""
from __future__ import annotations

import json
import socket
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import pytest

from claude_island.core.notify import NotifyEventQueue
from claude_island.core.pending_decisions import (
    Decision,
    DecisionResult,
    PendingDecisionRegistry,
)
from claude_island.core.session_permissions import SessionPermissionCache
from claude_island.core.session_state_machine import SessionStateMachine
from claude_island.platform_.hook_server import HookServer


# ── Fixtures ─────────────────────────────────────────────────────────


@pytest.fixture
def tmp_port_file(tmp_path: Path) -> Path:
    return tmp_path / "port.txt"


@pytest.fixture
def state_machine() -> SessionStateMachine:
    return SessionStateMachine()


@pytest.fixture
def registry() -> PendingDecisionRegistry:
    return PendingDecisionRegistry()


@pytest.fixture
def perm_cache() -> SessionPermissionCache:
    return SessionPermissionCache()


@pytest.fixture
def notify_queue() -> NotifyEventQueue:
    return NotifyEventQueue()


@pytest.fixture
def server(
    state_machine: SessionStateMachine,
    registry: PendingDecisionRegistry,
    perm_cache: SessionPermissionCache,
    notify_queue: NotifyEventQueue,
    tmp_port_file: Path,
):
    """Start a real HookServer on an ephemeral port. Tear down on test exit."""
    srv = HookServer(
        state_machine,
        preferred_port=0,        # ephemeral; OS picks
        port_file=tmp_port_file,
        pending_registry=registry,
        permission_cache=perm_cache,
        notify_queue=notify_queue,
    )
    port = srv.start()
    yield srv, port
    srv.stop()


def _post(port: int, payload: dict, *, timeout: float = 5.0) -> tuple[int, dict]:
    """POST a hook payload, return (status, parsed_json_body)."""
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/hook",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
        status = resp.status
    return status, (json.loads(raw) if raw else {})


# ── Default behaviour preserved ──────────────────────────────────────


class TestLegacyPath:
    def test_unrelated_event_returns_empty_object(self, server):
        srv, port = server
        status, body = _post(port, {
            "hook_event_name": "PostToolUse",
            "session_id": "u1",
            "tool_name": "Read",
        })
        assert status == 200
        assert body == {}


# ── PreToolUse always non-blocking ───────────────────────────────────


class TestPreToolUseAlwaysEmpty:
    """The approval card flow moved off PreToolUse onto PermissionRequest
    (Claude fires PreToolUse for every tool call regardless of whether
    it would prompt; PermissionRequest fires only when it actually
    intends to ask the user). So PreToolUse now applies to the state
    machine and immediately returns ``{}`` — no pending decisions, no
    cache hits."""

    def test_pretooluse_returns_empty_regardless_of_mode(
        self, server, registry: PendingDecisionRegistry,
    ):
        srv, port = server
        for mode in ("bypassPermissions", "dontAsk", "auto",
                     "acceptEdits", "default", "plan", None):
            payload = {
                "hook_event_name": "PreToolUse",
                "session_id": f"u-{mode}",
                "tool_name": "Bash",
                "tool_input": {"command": "ls"},
                "cwd": "/tmp",
            }
            if mode is not None:
                payload["permission_mode"] = mode
            status, body = _post(port, payload)
            assert status == 200, f"mode={mode}"
            assert body == {}, f"mode={mode}"
        assert registry.snapshot() == ()

    def test_pretooluse_ignores_perm_cache(
        self, server, perm_cache: SessionPermissionCache,
    ):
        """Even if a grant exists in the cache, PreToolUse stays a
        passthrough — cache hits only matter on the PermissionRequest
        path now."""
        srv, port = server
        perm_cache.grant("u1", "Bash")
        status, body = _post(port, {
            "hook_event_name": "PreToolUse",
            "session_id": "u1",
            "tool_name": "Bash",
            "tool_input": {"command": "ls"},
            "cwd": "/tmp/proj",
        })
        assert status == 200
        assert body == {}


# ── PermissionRequest cache hit (T3.1, fast path) ────────────────────


class TestPermissionRequestCache:
    def test_cache_hit_returns_allow_immediately(
        self, server, perm_cache: SessionPermissionCache,
    ):
        srv, port = server
        perm_cache.grant("u1", "Bash")
        t0 = time.monotonic()
        status, body = _post(port, {
            "hook_event_name": "PermissionRequest",
            "session_id": "u1",
            "tool_name": "Bash",
            "tool_input": {"command": "ls"},
            "cwd": "/tmp/proj",
        })
        elapsed = time.monotonic() - t0
        assert status == 200
        out = body["hookSpecificOutput"]
        assert out["hookEventName"] == "PermissionRequest"
        assert out["permissionDecision"] == "allow"
        # Fast path should be << 200 ms (network round-trip on loopback).
        assert elapsed < 0.5

    def test_cache_miss_no_pending_disabled(
        self,
        state_machine,
        tmp_port_file: Path,
    ):
        # Construct a server with bidirectional disabled — should fall
        # through to "{}" for legacy compat.
        srv = HookServer(
            state_machine,
            preferred_port=0,
            port_file=tmp_port_file,
        )
        port = srv.start()
        try:
            status, body = _post(port, {
                "hook_event_name": "PermissionRequest",
                "session_id": "u1",
                "tool_name": "Bash",
                "tool_input": {"command": "ls"},
                "cwd": "/tmp",
            })
            assert status == 200
            assert body == {}
        finally:
            srv.stop()


# ── PermissionRequest blocking flow (T3.2) ───────────────────────────


class TestPermissionRequestBlocking:
    def test_blocks_until_resolve_then_returns_decision(
        self, server, registry: PendingDecisionRegistry,
    ):
        srv, port = server
        # We'll resolve from another thread after registering a decision.

        # Spawn a "UI resolver" that polls until a pending decision shows
        # up, then resolves it with allow + remember.
        result_holder: dict = {}
        def _resolver():
            for _ in range(50):  # up to ~5s
                snap = registry.snapshot()
                if snap:
                    decision = Decision(
                        result=DecisionResult.ALLOW,
                        remember=True,
                    )
                    ok = registry.resolve(snap[0].id, decision)
                    result_holder["resolved_id"] = snap[0].id
                    result_holder["resolve_ok"] = ok
                    return
                time.sleep(0.05)
            result_holder["never_saw_pending"] = True

        t = threading.Thread(target=_resolver, daemon=True)
        t.start()

        # POST will block until resolver acts.
        status, body = _post(port, {
            "hook_event_name": "PermissionRequest",
            "session_id": "u1",
            "tool_name": "Bash",
            "tool_input": {"command": "npm test"},
            "cwd": "/tmp/proj",
        }, timeout=10.0)
        t.join(timeout=2.0)

        assert status == 200
        out = body["hookSpecificOutput"]
        assert out["hookEventName"] == "PermissionRequest"
        assert out["permissionDecision"] == "allow"
        assert result_holder.get("resolve_ok") is True

    def test_deny_returns_deny_with_reason(
        self, server, registry: PendingDecisionRegistry,
    ):
        srv, port = server

        def _resolver():
            for _ in range(50):
                snap = registry.snapshot()
                if snap:
                    registry.resolve(snap[0].id, Decision(
                        result=DecisionResult.DENY,
                        reason="too risky",
                    ))
                    return
                time.sleep(0.05)

        t = threading.Thread(target=_resolver, daemon=True)
        t.start()
        status, body = _post(port, {
            "hook_event_name": "PermissionRequest",
            "session_id": "u1",
            "tool_name": "Bash",
            "tool_input": {"command": "rm -rf /"},
            "cwd": "/tmp/proj",
        }, timeout=10.0)
        t.join(timeout=2.0)
        out = body["hookSpecificOutput"]
        assert out["permissionDecision"] == "deny"
        assert out["permissionDecisionReason"] == "too risky"

    def test_remember_populates_cache(
        self, server, registry, perm_cache,
    ):
        srv, port = server

        def _resolver():
            for _ in range(50):
                snap = registry.snapshot()
                if snap:
                    registry.resolve(snap[0].id, Decision(
                        result=DecisionResult.ALLOW,
                        remember=True,
                    ))
                    return
                time.sleep(0.05)

        t = threading.Thread(target=_resolver, daemon=True)
        t.start()
        _post(port, {
            "hook_event_name": "PermissionRequest",
            "session_id": "u-remember",
            "tool_name": "Bash",
            "tool_input": {"command": "ls"},
            "cwd": "/tmp",
        }, timeout=10.0)
        t.join(timeout=2.0)
        # Cache should now have the grant.
        assert perm_cache.check("u-remember", "Bash") is True


# ── AskUserQuestion routes to ASK_QUESTION pending decision ──────────


class TestAskUserQuestionRouting:
    """When the PermissionRequest is for AskUserQuestion (or a similar
    question-shaped MCP tool), the pending decision must carry kind=
    ASK_QUESTION + a parsed question_text + options for the question
    card UI variant — not a plain PRE_TOOL_USE approval card."""

    def _post_ask_user_question(self, port, *, tool_input):
        # POST runs on a background thread because PermissionRequest
        # blocks until the UI resolves. Test then asserts on the
        # snapshot — body assertion not used here.
        def _send():
            try:
                _post(port, {
                    "hook_event_name": "PermissionRequest",
                    "session_id": "u-ask",
                    "tool_name": "AskUserQuestion",
                    "tool_input": tool_input,
                    "cwd": "/tmp/proj",
                }, timeout=10.0)
            except Exception:
                pass
        t = threading.Thread(target=_send, daemon=True)
        t.start()
        return t

    def _wait_for_pending(self, registry, *, attempts=60):
        for _ in range(attempts):
            snap = registry.snapshot()
            if snap:
                return snap
            time.sleep(0.05)
        raise AssertionError("no pending decision registered")

    def test_well_formed_question_registers_ask_question_view(
        self, server, registry: PendingDecisionRegistry,
    ):
        from claude_island.core.pending_decisions import DecisionKind, Decision, DecisionResult
        srv, port = server
        t = self._post_ask_user_question(port, tool_input={
            "questions": [{
                "question": "指数退避的上限应设为多少？",
                "header": "退避策略",
                "options": [
                    {"label": "5m → 10m → 20m → 40m → 80m", "description": "更保守"},
                    {"label": "固定 30m",                   "description": "可预测"},
                ],
                "multiSelect": False,
            }],
        })
        try:
            snap = self._wait_for_pending(registry)
            assert len(snap) == 1
            v = snap[0]
            assert v.kind is DecisionKind.ASK_QUESTION
            assert v.tool_name == "AskUserQuestion"
            assert v.question_text == "指数退避的上限应设为多少？"
            assert v.question_header == "退避策略"
            assert v.question_options == (
                "5m → 10m → 20m → 40m → 80m", "固定 30m",
            )
            assert v.question_option_descriptions == ("更保守", "可预测")
            assert v.multi_select is False
        finally:
            # Resolve so the blocking POST thread can exit cleanly.
            snap = registry.snapshot()
            if snap:
                registry.resolve(
                    snap[0].id, Decision(result=DecisionResult.ALLOW),
                )
            t.join(timeout=2.0)

    def test_answer_relay_emits_updated_input_for_askuserquestion(
        self, server, registry: PendingDecisionRegistry,
    ):
        """When the user picks an option in island, the hook response
        must carry the picked label back to Claude as updatedInput so
        AskUserQuestion's tool body skips the terminal stdin prompt.
        Mirrors open-vibe-island BridgeServer.swift:2434-2481."""
        from claude_island.core.pending_decisions import Decision, DecisionResult
        srv, port = server
        responses: dict = {}

        def _send():
            try:
                status, body = _post(port, {
                    "hook_event_name": "PermissionRequest",
                    "session_id": "u-relay",
                    "tool_name": "AskUserQuestion",
                    "tool_input": {
                        "questions": [{
                            "question": "Size?",
                            "options": [{"label": "S"}, {"label": "M"}],
                            "multiSelect": False,
                        }],
                    },
                    "cwd": "/tmp/proj",
                }, timeout=10.0)
                responses["status"] = status
                responses["body"] = body
            except Exception as e:
                responses["error"] = repr(e)

        t = threading.Thread(target=_send, daemon=True)
        t.start()
        try:
            self._wait_for_pending(registry)
            snap = registry.snapshot()
            registry.resolve(snap[0].id, Decision(
                result=DecisionResult.ALLOW,
                reason="picked: M",
                answers=(("Size?", "M"),),
            ))
            t.join(timeout=3.0)
            assert "status" in responses
            assert responses["status"] == 200
            inner = responses["body"]["hookSpecificOutput"]
            # Nested form: hookSpecificOutput.decision.{behavior, updatedInput}
            assert inner["hookEventName"] == "PermissionRequest"
            decision_obj = inner["decision"]
            assert decision_obj["behavior"] == "allow"
            updated = decision_obj["updatedInput"]
            # Original input preserved + answers added
            assert "questions" in updated
            assert updated["answers"] == {"Size?": "M"}
        finally:
            t.join(timeout=1.0)


    def test_malformed_question_falls_back_to_pre_tool_use(
        self, server, registry: PendingDecisionRegistry,
    ):
        from claude_island.core.pending_decisions import DecisionKind, Decision, DecisionResult
        srv, port = server
        # Missing options → can't render an option-picker card. Falls
        # back so the server still has a way to surface the decision.
        t = self._post_ask_user_question(port, tool_input={
            "questions": [{"question": "no options"}],
        })
        try:
            snap = self._wait_for_pending(registry)
            assert snap[0].kind is DecisionKind.PRE_TOOL_USE
            assert snap[0].tool_name == "AskUserQuestion"
        finally:
            snap = registry.snapshot()
            if snap:
                registry.resolve(
                    snap[0].id, Decision(result=DecisionResult.ALLOW),
                )
            t.join(timeout=2.0)


# ── PostToolUse evicts pending PermissionRequest (T10) ───────────────


class TestPostToolUseEvict:
    """Detail Design §7 T10 — PostToolUse / PostToolUseFailure arriving
    while a PermissionRequest is still blocked on the UI must clear the
    pending entry and unblock the waiting server thread."""

    def _spawn_permission_request(
        self, port: int, *, tool_use_id: str, session_uuid: str = "u-evict",
    ) -> tuple[threading.Thread, dict]:
        responses: dict = {}

        def _send():
            try:
                status, body = _post(port, {
                    "hook_event_name": "PermissionRequest",
                    "session_id": session_uuid,
                    "tool_name": "Bash",
                    "tool_input": {"command": "echo hi"},
                    "tool_use_id": tool_use_id,
                    "cwd": "/tmp/proj",
                }, timeout=15.0)
                responses["status"] = status
                responses["body"] = body
            except Exception as e:
                responses["error"] = repr(e)

        t = threading.Thread(target=_send, daemon=True)
        t.start()
        return t, responses

    def _wait_for_pending(
        self, registry: PendingDecisionRegistry, *, attempts: int = 60,
    ) -> None:
        for _ in range(attempts):
            if registry.snapshot():
                return
            time.sleep(0.05)
        raise AssertionError("PermissionRequest never registered pending")

    def test_post_tool_use_evicts_by_tool_use_id_and_unblocks(
        self, server, registry: PendingDecisionRegistry,
    ):
        srv, port = server
        perm_thread, responses = self._spawn_permission_request(
            port, tool_use_id="tu_evict_1",
        )
        self._wait_for_pending(registry)

        status, body = _post(port, {
            "hook_event_name": "PostToolUse",
            "session_id": "u-evict",
            "tool_name": "Bash",
            "tool_use_id": "tu_evict_1",
            "cwd": "/tmp/proj",
        })
        assert status == 200
        assert body == {}

        perm_thread.join(timeout=3.0)
        assert not perm_thread.is_alive(), "PermissionRequest still blocking"
        out = responses["body"]["hookSpecificOutput"]
        # decision is None inside the registry; HookServer encodes that
        # as "defer" — see _handle_permission_request wait-timeout branch
        # (mark_externally_resolved goes through the same return None
        # codepath in wait()).
        assert out["permissionDecision"] == "defer"
        assert registry.snapshot() == ()

    def test_post_tool_use_failure_also_evicts(
        self, server, registry: PendingDecisionRegistry,
    ):
        srv, port = server
        perm_thread, responses = self._spawn_permission_request(
            port, tool_use_id="tu_fail_1",
        )
        self._wait_for_pending(registry)

        _post(port, {
            "hook_event_name": "PostToolUseFailure",
            "session_id": "u-evict",
            "tool_name": "Bash",
            "tool_use_id": "tu_fail_1",
            "cwd": "/tmp/proj",
        })
        perm_thread.join(timeout=3.0)
        assert not perm_thread.is_alive()
        assert registry.snapshot() == ()

    def test_post_tool_use_for_different_session_is_noop(
        self, server, registry: PendingDecisionRegistry,
    ):
        srv, port = server
        perm_thread, responses = self._spawn_permission_request(
            port, tool_use_id="tu_keep", session_uuid="u-keep",
        )
        self._wait_for_pending(registry)

        # PostToolUse from a different session must not touch our entry.
        _post(port, {
            "hook_event_name": "PostToolUse",
            "session_id": "u-other",
            "tool_name": "Bash",
            "tool_use_id": "tu_keep",   # same id, different session
            "cwd": "/tmp/proj",
        })
        # Entry still pending — perm POST still blocked.
        time.sleep(0.1)
        assert perm_thread.is_alive()
        assert len(registry.snapshot()) == 1

        # Clean up: resolve so the blocking thread returns.
        snap = registry.snapshot()
        registry.resolve(
            snap[0].id, Decision(result=DecisionResult.ALLOW),
        )
        perm_thread.join(timeout=3.0)

    def test_permission_denied_evicts_pending_card(
        self, server, registry: PendingDecisionRegistry,
    ):
        """When the user denies in the terminal, Claude Code emits
        ``PermissionDenied`` instead of PostToolUse (the tool was
        never executed). Island must treat it the same way — clear
        the matching pending card — so the user doesn't see a stale
        card sitting around for 598 s."""
        srv, port = server
        perm_thread, responses = self._spawn_permission_request(
            port, tool_use_id="tu_denied_1",
        )
        self._wait_for_pending(registry)

        _post(port, {
            "hook_event_name": "PermissionDenied",
            "session_id": "u-evict",
            "tool_name": "Bash",
            "tool_use_id": "tu_denied_1",
            "cwd": "/tmp/proj",
        })
        perm_thread.join(timeout=3.0)
        assert not perm_thread.is_alive()
        assert registry.snapshot() == ()

    def test_post_tool_use_falls_back_to_tool_name_when_id_missing(
        self, server, registry: PendingDecisionRegistry,
    ):
        srv, port = server
        # Register a PermissionRequest WITHOUT tool_use_id in payload.
        responses: dict = {}

        def _send():
            try:
                status, body = _post(port, {
                    "hook_event_name": "PermissionRequest",
                    "session_id": "u-fallback",
                    "tool_name": "Edit",
                    "tool_input": {"file_path": "/tmp/x"},
                    "cwd": "/tmp/proj",
                }, timeout=15.0)
                responses["body"] = body
            except Exception as e:
                responses["error"] = repr(e)

        perm_thread = threading.Thread(target=_send, daemon=True)
        perm_thread.start()
        self._wait_for_pending(registry)

        # PostToolUse also without tool_use_id — must fall back to
        # (session_uuid, tool_name) FIFO match.
        _post(port, {
            "hook_event_name": "PostToolUse",
            "session_id": "u-fallback",
            "tool_name": "Edit",
            "cwd": "/tmp/proj",
        })
        perm_thread.join(timeout=3.0)
        assert not perm_thread.is_alive()
        assert registry.snapshot() == ()


# ── PermissionRequest: registry full → defer (T3.3) ──────────────────


class TestPermissionRequestRegistryFull:
    def test_full_registry_returns_defer(
        self, state_machine, perm_cache, notify_queue, tmp_port_file,
    ):
        # Custom registry with cap = 1 effectively (we manually fill it).
        from claude_island.core.pending_decisions import (
            MAX_PENDING_DECISIONS,
            build_request,
            DecisionKind,
        )
        registry = PendingDecisionRegistry()
        # Fill registry to MAX.
        for i in range(MAX_PENDING_DECISIONS):
            req = build_request(
                kind=DecisionKind.PRE_TOOL_USE,
                session_uuid=f"u{i}", session_name=f"s{i}",
                cwd=Path("/tmp"),
                hook_event="PermissionRequest",
                timeout_s=60.0,
                tool_name="Bash",
                tool_input_preview="x",
            )
            registry.register(req)

        srv = HookServer(
            state_machine,
            preferred_port=0,
            port_file=tmp_port_file,
            pending_registry=registry,
            permission_cache=perm_cache,
            notify_queue=notify_queue,
        )
        port = srv.start()
        try:
            status, body = _post(port, {
                "hook_event_name": "PermissionRequest",
                "session_id": "u-overflow",
                "tool_name": "Bash",
                "tool_input": {"command": "ls"},
                "cwd": "/tmp",
            })
            assert status == 200
            assert body["hookSpecificOutput"]["permissionDecision"] == "defer"
        finally:
            srv.stop()


# ── UserPromptSubmit (T3.4 + T3.5) ───────────────────────────────────


class TestUserPromptSubmit:
    def test_review_off_returns_empty(self, server):
        srv, port = server
        # Default: is_review = False.
        status, body = _post(port, {
            "hook_event_name": "UserPromptSubmit",
            "session_id": "u1",
            "prompt": "hello",
            "cwd": "/tmp",
        })
        assert status == 200
        assert body == {}

    def test_review_on_block_returns_block_directive(
        self, server, registry, perm_cache,
    ):
        srv, port = server
        perm_cache.set_review("u1", True)

        def _resolver():
            for _ in range(50):
                snap = registry.snapshot()
                if snap:
                    registry.resolve(snap[0].id, Decision(
                        result=DecisionResult.BLOCK,
                        reason="needs git status",
                    ))
                    return
                time.sleep(0.05)

        t = threading.Thread(target=_resolver, daemon=True)
        t.start()
        status, body = _post(port, {
            "hook_event_name": "UserPromptSubmit",
            "session_id": "u1",
            "prompt": "do the thing",
            "cwd": "/tmp",
        }, timeout=10.0)
        t.join(timeout=2.0)
        assert body == {"decision": "block", "reason": "needs git status"}

    def test_review_on_inject_returns_additional_context(
        self, server, registry, perm_cache,
    ):
        srv, port = server
        perm_cache.set_review("u1", True)

        def _resolver():
            for _ in range(50):
                snap = registry.snapshot()
                if snap:
                    registry.resolve(snap[0].id, Decision(
                        result=DecisionResult.INJECT,
                        additional_context="git status: clean",
                    ))
                    return
                time.sleep(0.05)

        t = threading.Thread(target=_resolver, daemon=True)
        t.start()
        status, body = _post(port, {
            "hook_event_name": "UserPromptSubmit",
            "session_id": "u1",
            "prompt": "hello",
            "cwd": "/tmp",
        }, timeout=10.0)
        t.join(timeout=2.0)
        out = body["hookSpecificOutput"]
        assert out["hookEventName"] == "UserPromptSubmit"
        assert out["additionalContext"] == "git status: clean"


# ── Stop → notify queue (T3.6) ───────────────────────────────────────


class TestStop:
    def test_stop_pushes_notify_event(self, server, notify_queue):
        srv, port = server
        status, body = _post(port, {
            "hook_event_name": "Stop",
            "session_id": "u-stop",
            "cwd": "/tmp/myproj",
        })
        assert status == 200
        assert body == {}
        events = notify_queue.snapshot()
        assert len(events) == 1
        assert events[0].session_uuid == "u-stop"
        assert events[0].cwd_basename == "myproj"

    def test_stopfailure_marks_failure(self, server, notify_queue):
        srv, port = server
        from claude_island.core.notify import NotifyKind
        _post(port, {
            "hook_event_name": "StopFailure",
            "session_id": "u1",
            "cwd": "/tmp",
        })
        events = notify_queue.snapshot()
        assert events[0].kind is NotifyKind.TURN_FAILED


# ── SessionEnd → cache eviction (T3.7) ───────────────────────────────


class TestSessionEnd:
    def test_session_end_evicts_cache(self, server, perm_cache):
        srv, port = server
        perm_cache.grant("u-end", "Bash")
        perm_cache.set_review("u-end", True)
        status, body = _post(port, {
            "hook_event_name": "SessionEnd",
            "session_id": "u-end",
        })
        assert status == 200
        assert body == {}
        assert perm_cache.check("u-end", "Bash") is False
        assert perm_cache.is_review("u-end") is False
