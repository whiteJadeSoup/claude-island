"""G6 — approval click → Claude resume p95 < 200 ms.

Synthetic integration test: simulate the path
  hook POST → server registers + waits → UI resolves → server returns body
across 100 iterations and measure p95.
"""
from __future__ import annotations

import json
import threading
import time
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


@pytest.fixture
def server(tmp_path):
    sm = SessionStateMachine()
    pr = PendingDecisionRegistry()
    pc = SessionPermissionCache()
    nq = NotifyEventQueue()
    srv = HookServer(
        sm,
        preferred_port=0,
        port_file=tmp_path / "port.txt",
        pending_registry=pr,
        permission_cache=pc,
        notify_queue=nq,
    )
    port = srv.start()
    yield srv, port, pr
    srv.stop()


def _post_with_resolver(port: int, registry: PendingDecisionRegistry, *, resolve_after_s: float) -> float:
    """Run one PreToolUse blocking flow; return total elapsed time."""
    body = json.dumps({
        "hook_event_name": "PreToolUse",
        "session_id": "u-perf",
        "tool_name": "Bash",
        "tool_input": {"command": "ls"},
        "cwd": "/tmp",
    }).encode("utf-8")

    def _resolver():
        # Wait until the server registers the decision, then resolve.
        # Polling with tiny sleeps is fine — not in the measurement window.
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            snap = registry.snapshot()
            if snap:
                # Tiny sleep to simulate real "user thinks then clicks"
                # latency we want to NOT count toward G6 (G6 is the
                # click → resume budget, not the human think time).
                time.sleep(resolve_after_s)
                registry.resolve(snap[0].id, Decision(result=DecisionResult.ALLOW))
                return
            time.sleep(0.001)

    t = threading.Thread(target=_resolver, daemon=True)
    t.start()

    t0 = time.monotonic()
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/hook",
        data=body, headers={"Content-Type": "application/json"}, method="POST",
    )
    with urllib.request.urlopen(req, timeout=10.0) as resp:
        resp.read()
    t.join(timeout=2.0)
    return (time.monotonic() - t0) - resolve_after_s


# ── G6 perf gate ─────────────────────────────────────────────────────


@pytest.mark.perf
def test_g6_click_to_resume_p95_under_200ms(server):
    """Drive 100 iterations; assert p95 < 200 ms.

    The "click → resume" measurement subtracts the simulated user-think
    delay (50 ms) from the wall clock so what we measure is purely
    server-side (event.set, JSON encode, HTTP write) + hook-side
    (urllib read, stdout flush). Per Detail Design §6, this is ~13 ms.
    Budget 200 ms gives plenty of margin for OS scheduling jitter.
    """
    _, port, registry = server
    # Warm up — first POST tends to be slower (GC, JIT-ish caches).
    for _ in range(5):
        _post_with_resolver(port, registry, resolve_after_s=0.05)

    samples_ms: list[float] = []
    for _ in range(100):
        elapsed = _post_with_resolver(port, registry, resolve_after_s=0.05)
        samples_ms.append(elapsed * 1000.0)

    samples_ms.sort()
    p50 = samples_ms[50]
    p95 = samples_ms[95]
    p99 = samples_ms[99]
    print(f"\nG6 click-to-resume: p50={p50:.1f}ms p95={p95:.1f}ms p99={p99:.1f}ms")
    assert p95 < 200.0, (
        f"G6 violated: p95 = {p95:.1f}ms (budget 200ms); "
        f"p50={p50:.1f}ms p99={p99:.1f}ms"
    )
