"""Tests for PendingDecisionRegistry — the core of the bidirectional
hook protocol.

Test plan mirrors Detail Design §7 (G1, G7 cells):
  T1.1 happy   — register + resolve from another thread → wait returns
  T1.2 edge    — 17th register raises RegistryFull
  T1.3 edge    — resolve unknown id returns False
  T1.4 error   — wait timeout returns None and entry dropped from snapshot
  on_change    — fired on register / resolve / wait timeout / evict_expired
"""
from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

import pytest

from claude_island.core.pending_decisions import (
    Decision,
    DecisionKind,
    DecisionRequest,
    DecisionResult,
    MAX_PENDING_DECISIONS,
    PendingDecisionRegistry,
    RegistryFull,
    RiskLevel,
    build_request,
    classify_risk,
    new_decision_id,
)


# ── Fixtures ──────────────────────────────────────────────────────────


def _req(
    *,
    kind: DecisionKind = DecisionKind.PRE_TOOL_USE,
    timeout_s: float = 5.0,
    tool_name: str = "Bash",
    prompt_preview: str | None = None,
    session_uuid: str = "u1",
) -> DecisionRequest:
    return build_request(
        kind=kind,
        session_uuid=session_uuid,
        session_name="some session",
        cwd=Path("/tmp/proj"),
        hook_event="PreToolUse" if kind is DecisionKind.PRE_TOOL_USE else "UserPromptSubmit",
        timeout_s=timeout_s,
        tool_name=tool_name if kind is DecisionKind.PRE_TOOL_USE else None,
        tool_input_preview="npm test" if kind is DecisionKind.PRE_TOOL_USE else None,
        prompt_preview=prompt_preview if kind is DecisionKind.USER_PROMPT_SUBMIT else None,
    )


def _prompt_req(prompt: str = "what is 2+2?") -> DecisionRequest:
    return _req(
        kind=DecisionKind.USER_PROMPT_SUBMIT,
        tool_name="",
        prompt_preview=prompt,
    )


@pytest.fixture
def changes() -> list[int]:
    """Counter of on_change callback invocations."""
    return []


@pytest.fixture
def registry(changes: list[int]) -> PendingDecisionRegistry:
    return PendingDecisionRegistry(on_change=lambda: changes.append(1))


# ── Decision validation ──────────────────────────────────────────────


class TestDecisionValidation:
    def test_deny_requires_reason(self):
        with pytest.raises(ValueError, match="reason"):
            Decision(result=DecisionResult.DENY)

    def test_block_requires_reason(self):
        with pytest.raises(ValueError, match="reason"):
            Decision(result=DecisionResult.BLOCK, reason="")

    def test_inject_requires_context(self):
        with pytest.raises(ValueError, match="additional_context"):
            Decision(result=DecisionResult.INJECT)

    def test_remember_requires_allow(self):
        with pytest.raises(ValueError, match="remember"):
            Decision(result=DecisionResult.DENY, reason="x", remember=True)

    def test_allow_with_remember_ok(self):
        d = Decision(result=DecisionResult.ALLOW, remember=True)
        assert d.remember is True


class TestRequestValidation:
    def test_pretooluse_requires_tool_name(self):
        with pytest.raises(ValueError, match="tool_name"):
            DecisionRequest(
                id="x", kind=DecisionKind.PRE_TOOL_USE,
                session_uuid="u", session_name="s",
                cwd=Path("/"), cwd_basename="/", hook_event="PreToolUse",
                created_at=datetime.now(timezone.utc),
                expires_at=datetime.now(timezone.utc) + timedelta(seconds=1),
                tool_name=None,
            )

    def test_userpromptsubmit_requires_prompt(self):
        with pytest.raises(ValueError, match="prompt_preview"):
            DecisionRequest(
                id="x", kind=DecisionKind.USER_PROMPT_SUBMIT,
                session_uuid="u", session_name="s",
                cwd=Path("/"), cwd_basename="/", hook_event="UserPromptSubmit",
                created_at=datetime.now(timezone.utc),
                expires_at=datetime.now(timezone.utc) + timedelta(seconds=1),
                prompt_preview=None,
            )

    def test_expires_must_be_after_created(self):
        now = datetime.now(timezone.utc)
        with pytest.raises(ValueError, match="expires_at"):
            DecisionRequest(
                id="x", kind=DecisionKind.PRE_TOOL_USE,
                session_uuid="u", session_name="s",
                cwd=Path("/"), cwd_basename="/", hook_event="PreToolUse",
                created_at=now,
                expires_at=now,
                tool_name="Bash",
            )


# ── classify_risk ────────────────────────────────────────────────────


class TestClassifyRisk:
    def test_bash_is_high(self):
        assert classify_risk("Bash") is RiskLevel.HIGH

    def test_write_is_high(self):
        assert classify_risk("Write") is RiskLevel.HIGH

    def test_read_is_low(self):
        assert classify_risk("Read") is RiskLevel.LOW

    def test_unknown_defaults_to_medium(self):
        # Conservative default — promotes safety over silent low badge.
        assert classify_risk("mcp__some__newToolNobodyKnowsAbout") is RiskLevel.MEDIUM


# ── register ─────────────────────────────────────────────────────────


class TestRegister:
    def test_returns_request_id(self, registry: PendingDecisionRegistry):
        req = _req()
        rid = registry.register(req)
        assert rid == req.id

    def test_fires_on_change(
        self,
        registry: PendingDecisionRegistry,
        changes: list[int],
    ):
        registry.register(_req())
        assert len(changes) == 1

    def test_appears_in_snapshot(self, registry: PendingDecisionRegistry):
        req = _req()
        registry.register(req)
        snap = registry.snapshot()
        assert len(snap) == 1
        assert snap[0].id == req.id
        assert snap[0].tool_name == "Bash"
        # PRE_TOOL_USE projection should populate tool fields, leave prompt None.
        assert snap[0].prompt_preview is None

    def test_snapshot_sorted_by_created_at(self, registry: PendingDecisionRegistry):
        a = _req()
        b = _req()  # built second → later created_at (or equal — tolerate)
        registry.register(b)
        registry.register(a)
        snap = registry.snapshot()
        assert [v.id for v in snap] == sorted(
            [a.id, b.id],
            key=lambda i: a.created_at if i == a.id else b.created_at,
        )

    def test_full_raises_RegistryFull(self, registry: PendingDecisionRegistry):
        for _ in range(MAX_PENDING_DECISIONS):
            registry.register(_req())
        with pytest.raises(RegistryFull):
            registry.register(_req())

    def test_duplicate_id_raises(self, registry: PendingDecisionRegistry):
        req = _req()
        registry.register(req)
        with pytest.raises(ValueError, match="duplicate"):
            registry.register(req)


# ── resolve / wait ───────────────────────────────────────────────────


class TestResolveWait:
    def test_resolve_unknown_id_returns_False(
        self,
        registry: PendingDecisionRegistry,
    ):
        assert registry.resolve("nonexistent", Decision(DecisionResult.ALLOW)) is False

    def test_wait_returns_resolved_decision_from_other_thread(
        self,
        registry: PendingDecisionRegistry,
    ):
        req = _req(timeout_s=2.0)
        registry.register(req)

        # Spawn a "UI thread" that resolves after 50ms
        decision = Decision(result=DecisionResult.ALLOW, remember=True)
        def _resolver():
            time.sleep(0.05)
            assert registry.resolve(req.id, decision) is True

        t = threading.Thread(target=_resolver)
        t.start()

        # Server thread waits (blocks)
        result = registry.wait(req.id, timeout_s=2.0)
        t.join(timeout=1.0)

        assert result == decision

    def test_wait_timeout_returns_None_and_drops_entry(
        self,
        registry: PendingDecisionRegistry,
    ):
        req = _req(timeout_s=1.0)
        registry.register(req)
        # Fast timeout — wait will hit it.
        result = registry.wait(req.id, timeout_s=0.05)
        assert result is None
        # Entry should be dropped after timeout.
        assert len(registry) == 0
        assert registry.snapshot() == ()

    def test_wait_drops_entry_after_resolve(
        self,
        registry: PendingDecisionRegistry,
    ):
        req = _req(timeout_s=2.0)
        registry.register(req)
        registry.resolve(req.id, Decision(DecisionResult.ALLOW))
        # wait should immediately see the set Event and return.
        result = registry.wait(req.id, timeout_s=2.0)
        assert result is not None
        assert result.result is DecisionResult.ALLOW
        # And the entry should be gone now.
        assert len(registry) == 0

    def test_resolve_after_timeout_returns_False(
        self,
        registry: PendingDecisionRegistry,
    ):
        req = _req(timeout_s=1.0)
        registry.register(req)
        # Hit the wait timeout to drop entry.
        registry.wait(req.id, timeout_s=0.02)
        # Now a late resolve should be a no-op.
        ok = registry.resolve(req.id, Decision(DecisionResult.ALLOW))
        assert ok is False

    def test_resolve_already_resolved_returns_False(
        self,
        registry: PendingDecisionRegistry,
    ):
        req = _req(timeout_s=2.0)
        registry.register(req)
        assert registry.resolve(req.id, Decision(DecisionResult.ALLOW)) is True
        # Second resolve should be False — event already set.
        assert registry.resolve(req.id, Decision(DecisionResult.DENY, reason="x")) is False

    def test_resolved_entries_excluded_from_snapshot(
        self,
        registry: PendingDecisionRegistry,
    ):
        req = _req(timeout_s=2.0)
        registry.register(req)
        registry.resolve(req.id, Decision(DecisionResult.ALLOW))
        # Don't render still-pending after resolve — UI shouldn't see a
        # phantom card during the brief window between resolve and wait.
        snap = registry.snapshot()
        assert snap == ()


# ── evict_expired ────────────────────────────────────────────────────


class TestEvictExpired:
    def test_evicts_only_past_expires_at(
        self,
        registry: PendingDecisionRegistry,
    ):
        # one short, one long
        short = _req(timeout_s=0.1)
        long = _req(timeout_s=10.0)
        registry.register(short)
        registry.register(long)
        time.sleep(0.15)
        n = registry.evict_expired()
        assert n == 1
        ids = {v.id for v in registry.snapshot()}
        assert long.id in ids
        assert short.id not in ids

    def test_idempotent_when_nothing_to_evict(
        self,
        registry: PendingDecisionRegistry,
    ):
        registry.register(_req(timeout_s=10.0))
        assert registry.evict_expired() == 0

    def test_does_not_evict_resolved_entries(
        self,
        registry: PendingDecisionRegistry,
    ):
        req = _req(timeout_s=0.1)
        registry.register(req)
        registry.resolve(req.id, Decision(DecisionResult.ALLOW))
        # Even though expires_at has passed, resolved entries are owned
        # by the wait() path and shouldn't be ripped out from underneath.
        time.sleep(0.15)
        n = registry.evict_expired()
        assert n == 0

    def test_eviction_fires_on_change(
        self,
        registry: PendingDecisionRegistry,
        changes: list[int],
    ):
        registry.register(_req(timeout_s=0.05))
        baseline = len(changes)
        time.sleep(0.1)
        registry.evict_expired()
        # one eviction should fire one on_change
        assert len(changes) == baseline + 1


# ── PROMPT-flavoured projection ──────────────────────────────────────


class TestPromptProjection:
    def test_prompt_view_has_prompt_no_tool(
        self,
        registry: PendingDecisionRegistry,
    ):
        req = _prompt_req("hello world")
        registry.register(req)
        snap = registry.snapshot()
        assert snap[0].prompt_preview == "hello world"
        assert snap[0].tool_name is None
        assert snap[0].kind is DecisionKind.USER_PROMPT_SUBMIT


# ── new_decision_id ──────────────────────────────────────────────────


class TestNewDecisionId:
    def test_unique_ids(self):
        ids = {new_decision_id() for _ in range(1000)}
        assert len(ids) == 1000

    def test_ids_are_hex_strings(self):
        i = new_decision_id()
        assert isinstance(i, str)
        # uuid4 hex is 32 chars
        assert len(i) == 32
        int(i, 16)  # would raise if not hex
