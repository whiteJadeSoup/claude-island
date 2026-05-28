"""End-to-end: island user clicks 允许 → the blocked hook thread wakes with Decision.

This proves the decision pipeline's core promise (resolve in the island unblocks
the waiting hook) using the real PendingDecisionRegistry + the WorldViewModel slot.
No GUI involved.
"""
import threading
import time
from pathlib import Path

from PySide6.QtCore import QCoreApplication

from claude_island.core.pending_decisions import (
    PendingDecisionRegistry,
    build_request,
    DecisionKind,
    DecisionResult,
)
from claude_island.ui.world_view_model import WorldViewModel

_app = QCoreApplication.instance() or QCoreApplication([])


def test_island_approve_unblocks_waiting_hook():
    reg = PendingDecisionRegistry()
    req = build_request(
        kind=DecisionKind.PRE_TOOL_USE,
        session_uuid="u1",
        session_name="db-migrate",
        cwd=Path("D:/x"),
        hook_event="PreToolUse",
        timeout_s=30,
        tool_name="Bash",
        tool_input_preview="kubectl apply",
    )
    reg.register(req)

    result = {}

    def hook_thread():
        result["decision"] = reg.wait(req.id, timeout_s=10)

    t = threading.Thread(target=hook_thread)
    t.start()
    time.sleep(0.2)  # ensure the hook thread is blocked in wait()

    vm = WorldViewModel(resolve_fn=reg.resolve)
    vm.approve(req.id, False)  # island user clicks 允许这次

    t.join(timeout=5)
    assert result["decision"] is not None, "hook thread did not wake"
    assert result["decision"].result is DecisionResult.ALLOW


def test_island_deny_unblocks_with_deny():
    reg = PendingDecisionRegistry()
    req = build_request(
        kind=DecisionKind.PRE_TOOL_USE,
        session_uuid="u2",
        session_name="deploy",
        cwd=Path("D:/x"),
        hook_event="PreToolUse",
        timeout_s=30,
        tool_name="Bash",
        tool_input_preview="rm -rf /",
    )
    reg.register(req)

    result = {}

    def hook_thread():
        result["decision"] = reg.wait(req.id, timeout_s=10)

    t = threading.Thread(target=hook_thread)
    t.start()
    time.sleep(0.2)

    vm = WorldViewModel(resolve_fn=reg.resolve)
    vm.deny(req.id)

    t.join(timeout=5)
    assert result["decision"] is not None
    assert result["decision"].result is DecisionResult.DENY
    assert result["decision"].reason
