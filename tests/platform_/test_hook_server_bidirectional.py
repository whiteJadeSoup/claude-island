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


# ── PreToolUse cache hit (T3.1, fast path) ───────────────────────────


class TestBypassPermissionMode:
    """When the session is in bypassPermissions / dontAsk mode the user
    has explicitly opted out of any prompts. HookServer must NOT register
    a pending decision — that would override the user's intent."""

    def test_bypass_mode_returns_empty_no_pending(
        self, server, registry: PendingDecisionRegistry,
    ):
        srv, port = server
        status, body = _post(port, {
            "hook_event_name": "PreToolUse",
            "session_id": "u-bypass",
            "tool_name": "Bash",
            "tool_input": {"command": "rm -rf node_modules"},
            "cwd": "/tmp",
            "permission_mode": "bypassPermissions",
        })
        assert status == 200
        assert body == {}
        # Nothing should have been registered.
        assert registry.snapshot() == ()

    def test_dontask_mode_alias_also_skips(
        self, server, registry: PendingDecisionRegistry,
    ):
        srv, port = server
        status, body = _post(port, {
            "hook_event_name": "PreToolUse",
            "session_id": "u-dontask",
            "tool_name": "Bash",
            "tool_input": {"command": "ls"},
            "cwd": "/tmp",
            "permission_mode": "dontAsk",
        })
        assert status == 200
        assert body == {}
        assert registry.snapshot() == ()

    def test_auto_mode_skips(
        self, server, registry: PendingDecisionRegistry,
    ):
        """Autonomous mode — Claude's classifier decides; intercepting
        with our card would override that explicit user intent."""
        srv, port = server
        status, body = _post(port, {
            "hook_event_name": "PreToolUse",
            "session_id": "u-auto",
            "tool_name": "Bash",
            "tool_input": {"command": "ls"},
            "cwd": "/tmp",
            "permission_mode": "auto",
        })
        assert status == 200
        assert body == {}
        assert registry.snapshot() == ()

    def test_acceptedits_skips_only_edit_tools(
        self, server, registry: PendingDecisionRegistry,
    ):
        """acceptEdits is partial bypass: Edit/Write/MultiEdit/Notebook
        Edit auto-skip; Bash + others still hit the approval flow."""
        srv, port = server
        # Edit tool in acceptEdits → skip.
        status, body = _post(port, {
            "hook_event_name": "PreToolUse",
            "session_id": "u-edits",
            "tool_name": "Edit",
            "tool_input": {"file_path": "/tmp/x"},
            "cwd": "/tmp",
            "permission_mode": "acceptEdits",
        })
        assert body == {}
        assert registry.snapshot() == ()

    def test_acceptedits_still_blocks_for_bash(
        self, server, registry: PendingDecisionRegistry,
    ):
        srv, port = server

        def _resolver():
            for _ in range(50):
                snap = registry.snapshot()
                if snap:
                    registry.resolve(
                        snap[0].id, Decision(result=DecisionResult.ALLOW),
                    )
                    return
                time.sleep(0.05)

        t = threading.Thread(target=_resolver, daemon=True)
        t.start()
        status, body = _post(port, {
            "hook_event_name": "PreToolUse",
            "session_id": "u-edits-bash",
            "tool_name": "Bash",
            "tool_input": {"command": "ls"},
            "cwd": "/tmp",
            "permission_mode": "acceptEdits",
        }, timeout=10.0)
        t.join(timeout=2.0)
        # Should have entered the approval flow, NOT the bypass fast-path.
        assert body["hookSpecificOutput"]["permissionDecision"] == "allow"

    def test_default_mode_still_blocks(
        self, server, registry: PendingDecisionRegistry,
    ):
        """Sanity: default mode (no permission_mode set, or "default")
        still triggers the pending-decision flow as before."""
        srv, port = server

        def _resolver():
            for _ in range(50):
                snap = registry.snapshot()
                if snap:
                    registry.resolve(snap[0].id, Decision(result=DecisionResult.ALLOW))
                    return
                time.sleep(0.05)

        t = threading.Thread(target=_resolver, daemon=True)
        t.start()
        status, body = _post(port, {
            "hook_event_name": "PreToolUse",
            "session_id": "u-default",
            "tool_name": "Bash",
            "tool_input": {"command": "ls"},
            "cwd": "/tmp",
            "permission_mode": "default",
        }, timeout=10.0)
        t.join(timeout=2.0)
        assert body["hookSpecificOutput"]["permissionDecision"] == "allow"


class TestPreToolUseCache:
    def test_cache_hit_returns_allow_immediately(
        self, server, perm_cache: SessionPermissionCache,
    ):
        srv, port = server
        perm_cache.grant("u1", "Bash")
        t0 = time.monotonic()
        status, body = _post(port, {
            "hook_event_name": "PreToolUse",
            "session_id": "u1",
            "tool_name": "Bash",
            "tool_input": {"command": "ls"},
            "cwd": "/tmp/proj",
        })
        elapsed = time.monotonic() - t0
        assert status == 200
        assert body["hookSpecificOutput"]["permissionDecision"] == "allow"
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
                "hook_event_name": "PreToolUse",
                "session_id": "u1",
                "tool_name": "Bash",
                "tool_input": {"command": "ls"},
                "cwd": "/tmp",
            })
            assert status == 200
            assert body == {}
        finally:
            srv.stop()


# ── PreToolUse blocking flow (T3.2) ──────────────────────────────────


class TestPreToolUseBlocking:
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
            "hook_event_name": "PreToolUse",
            "session_id": "u1",
            "tool_name": "Bash",
            "tool_input": {"command": "npm test"},
            "cwd": "/tmp/proj",
        }, timeout=10.0)
        t.join(timeout=2.0)

        assert status == 200
        assert body["hookSpecificOutput"]["permissionDecision"] == "allow"
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
            "hook_event_name": "PreToolUse",
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
            "hook_event_name": "PreToolUse",
            "session_id": "u-remember",
            "tool_name": "Bash",
            "tool_input": {"command": "ls"},
            "cwd": "/tmp",
        }, timeout=10.0)
        t.join(timeout=2.0)
        # Cache should now have the grant.
        assert perm_cache.check("u-remember", "Bash") is True


# ── PreToolUse: registry full → defer (T3.3) ──────────────────────────


class TestPreToolUseRegistryFull:
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
                hook_event="PreToolUse",
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
                "hook_event_name": "PreToolUse",
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
