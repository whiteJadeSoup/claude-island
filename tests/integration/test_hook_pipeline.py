"""End-to-end test of the hook pipeline.

T8.x family from Detail Design v2 §7.

Wires the same components production wires (real HookServer + real
SessionStateMachine + real HookSessionBridge + real SessionRegistry +
real Snapshotter), then exercises a Claude session lifecycle by POSTing
synthetic hook payloads to the listener. Verifies:

  * Phase sequence in the published snapshot follows the
    SessionStart → Prompt → Tool → Tool-finish → Stop chain
  * G1 latency: session appears in snapshot < 1s after SessionStart
    fires (the race fix from F-1)
  * Scanner-only sessions (no hooks) still work (G4 regression)

Uses real HTTP, real reactivex, real threads. The publish callback is
a plain list.append — no Qt, no marshaller — because we're testing the
core pipeline, not the Qt thread crossing.
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

from claude_island.core.models import Session
from claude_island.core.session_phase import SessionPhase
from claude_island.core.session_registry import SessionRegistry
from claude_island.core.session_state_machine import SessionStateMachine
from claude_island.core.snapshot import Snapshotter, WorldSnapshot
from claude_island.platform_.hook_server import HookServer
from claude_island.platform_.hook_session_bridge import HookSessionBridge


# ---------------------------------------------------------------------------
# Minimal fakes for the non-hook dependencies of compose_session_view
# (state_reader, metadata_provider, usage_registry, names_store).
# Each returns "nothing" so compose_session_view falls through to the
# hook-driven path for phase resolution.
# ---------------------------------------------------------------------------


class _FakeStateReader:
    def read_session_state(self, pid: int) -> dict | None:
        return None


class _FakeMetadataProvider:
    def get_session_metadata(self, uuid: str) -> dict | None:
        return None


class _FakeUsageRegistry:
    def get_session_summary(self, uuid: str) -> tuple[float, int, int]:
        return (0.0, 0, 0)

    def get_latest_model(self, uuid: str) -> str | None:
        return None

    def get_totals(self, period: str):
        # Snapshotter calls this with period="today". Stub returns an
        # object with cost_usd attribute (matches UsageTotals shape).
        class _T:
            cost_usd = 0.0
        return _T()


class _FakeNamesStore:
    def get_session_name(self, uuid: str) -> str | None:
        return None


# ---------------------------------------------------------------------------
# Fixture: full pipeline up and running
# ---------------------------------------------------------------------------


def _find_free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


@pytest.fixture()
def pipeline(tmp_path: Path):
    """Builds the full pipeline. Yields (port, sm, registry, snapshots).

    snapshots is a list — every WorldSnapshot published lands here, so
    tests can assert on the sequence.
    """
    registry = SessionRegistry()
    state_machine = SessionStateMachine()
    bridge = HookSessionBridge(registry=registry, state_machine=state_machine)

    server = HookServer(
        state_machine,
        preferred_port=_find_free_port(),
        port_file=tmp_path / "port.txt",
    )
    port = server.start()

    snapshots: list[WorldSnapshot] = []
    publish_lock = threading.Lock()

    def _publish(snap: WorldSnapshot) -> None:
        with publish_lock:
            snapshots.append(snap)

    snapshotter = Snapshotter(
        session_source=registry,
        state_reader=_FakeStateReader(),
        metadata_provider=_FakeMetadataProvider(),
        usage_registry=_FakeUsageRegistry(),
        names_store=_FakeNamesStore(),
        live_state_reader=state_machine.read,
        get_quota=lambda: None,
        get_available_providers=lambda: [],
        get_selected_provider=lambda: None,
        publish=_publish,
        # Tight debounce/throttle so tests don't have to wait
        debounce_window_s=0.02,
        throttle_first_window_s=0.0,
    )
    snapshotter.start()

    # Wire hook event flow → snapshotter wakeup
    state_machine.live_state_changed.subscribe(
        on_next=lambda _: snapshotter.wake(),
    )
    # Wire scanner flow → snapshotter wakeup (mirrors production)
    registry.sessions_changed.subscribe(
        on_next=lambda _: snapshotter.wake(),
    )

    try:
        yield port, state_machine, registry, snapshotter, snapshots
    finally:
        snapshotter.stop()
        bridge.stop()
        server.stop()


def _post(port: int, payload: dict) -> int:
    """POST a hook payload to /hook. Returns HTTP status."""
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/hook",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=2.0) as resp:
        return resp.status


def _wait_for(predicate, *, timeout: float = 2.0, interval: float = 0.05):
    """Spin until predicate() returns truthy or timeout. Returns the
    last predicate value."""
    deadline = time.monotonic() + timeout
    last = predicate()
    while not last and time.monotonic() < deadline:
        time.sleep(interval)
        last = predicate()
    return last


# ---------------------------------------------------------------------------
# T8.1 — full lifecycle sequence
# ---------------------------------------------------------------------------


def test_t8_1_full_lifecycle_phase_sequence(pipeline):
    port, sm, registry, snapshotter, snapshots = pipeline
    UUID = "abc"
    CWD = "D:/projects/foo"

    # SessionStart
    _post(port, {
        "hook_event_name": "SessionStart",
        "session_id": UUID,
        "cwd": CWD,
        "source": "startup",
    })

    # Expect a snapshot containing UUID at phase IDLE
    def _has_idle():
        if not snapshots:
            return False
        last = snapshots[-1]
        for g in last.session_groups:
            for v in g.views:
                if v.session_uuid == UUID and v.phase == SessionPhase.IDLE:
                    return True
        return False
    assert _wait_for(_has_idle), \
        f"Did not see session at IDLE; last snapshot: {snapshots[-1] if snapshots else None}"

    # UserPromptSubmit → THINKING
    _post(port, {
        "hook_event_name": "UserPromptSubmit",
        "session_id": UUID,
        "prompt": "fix the bug",
    })
    assert _wait_for(lambda: _phase_of(snapshots, UUID) == SessionPhase.THINKING)
    assert _last_prompt_of(snapshots, UUID) == "fix the bug"

    # PreToolUse → TOOL_USE
    _post(port, {
        "hook_event_name": "PreToolUse",
        "session_id": UUID,
        "tool_name": "Bash",
        "tool_input": {"command": "ls -la"},
    })
    assert _wait_for(lambda: _phase_of(snapshots, UUID) == SessionPhase.TOOL_USE)
    assert _current_tool_of(snapshots, UUID) == "Bash"

    # PostToolUse → THINKING
    _post(port, {
        "hook_event_name": "PostToolUse",
        "session_id": UUID,
        "tool_name": "Bash",
    })
    assert _wait_for(lambda: _phase_of(snapshots, UUID) == SessionPhase.THINKING)
    assert _current_tool_of(snapshots, UUID) is None

    # Stop → IDLE with last_assistant_message
    _post(port, {
        "hook_event_name": "Stop",
        "session_id": UUID,
        "last_assistant_message": "Done.",
    })
    assert _wait_for(lambda: _phase_of(snapshots, UUID) == SessionPhase.IDLE)


# ---------------------------------------------------------------------------
# T8.1b — G1 latency: session appears in WorldSnapshot < 1s of SessionStart
# ---------------------------------------------------------------------------


def test_g1_latency_session_visible_under_one_second(pipeline):
    """The hard latency target. From the moment we POST SessionStart to
    the moment a WorldSnapshot containing that uuid is published, the
    elapsed wall time must be < 1s. This is the whole reason
    HookSessionBridge exists (placeholder insertion races scanner)."""
    port, sm, registry, snapshotter, snapshots = pipeline
    UUID = "fast"

    start = time.monotonic()
    _post(port, {
        "hook_event_name": "SessionStart",
        "session_id": UUID,
        "cwd": "/fast",
        "source": "startup",
    })

    appeared = _wait_for(
        lambda: any(
            v.session_uuid == UUID
            for snap in snapshots
            for g in snap.session_groups
            for v in g.views
        ),
        timeout=1.0,
    )
    elapsed = time.monotonic() - start
    assert appeared, f"session never appeared in {elapsed:.2f}s of snapshots"
    assert elapsed < 1.0, f"session appeared after {elapsed:.2f}s (G1 target < 1s)"


# ---------------------------------------------------------------------------
# T8.2 — pure scanner path (no hooks) still works (G4 regression)
# ---------------------------------------------------------------------------


def test_t8_2_scanner_only_session_renders_via_pid_json_fallback(pipeline):
    """A session pushed via scanner with no hooks at all should still
    produce a SessionView in the snapshot. Phase resolution falls
    through hook → pid.json → activity heuristic. With our fake state
    reader returning None and recent activity, the fallback yields
    THINKING (via the activity heuristic)."""
    port, sm, registry, snapshotter, snapshots = pipeline

    registry.update([Session(
        pid=42,
        project_path=Path("/legacy"),
        session_uuid="",  # scanner doesn't know uuid
        last_activity=datetime.now(timezone.utc),
    )])

    appeared = _wait_for(
        lambda: any(
            v.pid == 42
            for snap in snapshots
            for g in snap.session_groups
            for v in g.views
        ),
        timeout=1.0,
    )
    assert appeared
    # state_machine is empty (no hook events) — phase from activity heuristic
    last = snapshots[-1]
    legacy = next(
        v
        for g in last.session_groups
        for v in g.views
        if v.pid == 42
    )
    # Activity is now → THINKING (within active_threshold_s window)
    assert legacy.phase == SessionPhase.THINKING


# ---------------------------------------------------------------------------
# Extra: malformed payload → 400 but pipeline still alive
# ---------------------------------------------------------------------------


def test_malformed_post_does_not_kill_pipeline(pipeline):
    port, sm, registry, snapshotter, snapshots = pipeline

    # Send garbage
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/hook",
        data=b"garbage",
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with pytest.raises(urllib.error.HTTPError) as exc:
        urllib.request.urlopen(req, timeout=2.0)
    assert exc.value.code == 400

    # Legitimate POST still works
    status = _post(port, {
        "hook_event_name": "SessionStart",
        "session_id": "after-garbage",
        "cwd": "/x",
    })
    assert status == 200
    assert _wait_for(lambda: sm.read("after-garbage") is not None)


# ---------------------------------------------------------------------------
# Extra: SessionEnd hook → phase ENDED + bridge tombstones
# ---------------------------------------------------------------------------


def test_session_end_hook_transitions_to_ended(pipeline):
    port, sm, registry, snapshotter, snapshots = pipeline
    UUID = "ending"

    _post(port, {
        "hook_event_name": "SessionStart",
        "session_id": UUID,
        "cwd": "/x",
        "source": "startup",
    })
    _wait_for(lambda: sm.read(UUID) is not None)

    _post(port, {"hook_event_name": "SessionEnd", "session_id": UUID})
    assert _wait_for(lambda: sm.read(UUID).phase == SessionPhase.ENDED)


# ---------------------------------------------------------------------------
# Open-vibe-island JumpTarget pipeline (2026-05-14)
# ---------------------------------------------------------------------------


def test_jump_target_roundtrips_hook_to_session_view(pipeline):
    """End-to-end: POST a SessionStart with jump_target → state machine
    persists it → compose_session_view copies to SessionView →
    snapshot publishes a view that includes the JumpTarget data.

    Mirrors open-vibe-island's pattern: hook captures terminal-identifying
    metadata at SessionStart, the click handler consumes it.
    """
    port, sm, registry, snapshotter, snapshots = pipeline

    UUID = "jt-roundtrip"
    CONHOST = 0xCAFEBABE
    HOST_PID = 12345
    WT_GUID = "guid-from-WT_SESSION-env"

    # Drive scanner to know about this pid (otherwise SessionView won't
    # be built — compose_session_view iterates registry.sessions).
    registry.update([Session(
        pid=HOST_PID,
        project_path=Path("D:/jt-test"),
        session_uuid="",  # scanner doesn't know uuid
        last_activity=datetime.now(timezone.utc),
    )])

    # Hook-side: post SessionStart with full jump_target sub-dict.
    _post(port, {
        "hook_event_name": "SessionStart",
        "session_id": UUID,
        "cwd": "D:/jt-test",
        "source": "startup",
        "jump_target": {
            "terminal_app": "WindowsTerminal",
            "conhost_hwnd": CONHOST,
            "host_pid": HOST_PID,
            "wt_session_guid": WT_GUID,
            "term_program": "WindowsTerminal",
        },
    })

    # State machine should now have the jump_target
    def _state_has_jt():
        live = sm.read(UUID)
        return (
            live is not None
            and live.jump_target is not None
            and live.jump_target.conhost_hwnd == CONHOST
        )
    assert _wait_for(_state_has_jt), \
        f"state machine missing jump_target. live={sm.read(UUID)}"

    # SessionView in published snapshot should also have it (composed
    # via compose_session_view → live_state_reader → SessionLiveState.jump_target)
    def _view_has_jt():
        if not snapshots:
            return False
        last = snapshots[-1]
        for g in last.session_groups:
            for v in g.views:
                if v.session_uuid == UUID and v.jump_target is not None:
                    return v.jump_target.conhost_hwnd == CONHOST
        return False
    assert _wait_for(_view_has_jt, timeout=2.0), \
        f"SessionView missing jump_target. Last snapshot views: " \
        f"{[(v.session_uuid, v.jump_target) for g in (snapshots[-1].session_groups if snapshots else []) for v in g.views]}"


def test_jump_target_survives_subsequent_hook_events(pipeline):
    """Once captured at SessionStart, JumpTarget should persist across
    other hook events (PromptSubmit, ToolUse, Stop) so the click
    handler always has fresh data."""
    port, sm, registry, snapshotter, snapshots = pipeline

    UUID = "jt-persist"
    _post(port, {
        "hook_event_name": "SessionStart",
        "session_id": UUID,
        "cwd": "/x",
        "source": "startup",
        "jump_target": {
            "terminal_app": "WindowsTerminal",
            "conhost_hwnd": 0xABCD,
            "host_pid": 999,
            "wt_session_guid": "g",
            "term_program": "WindowsTerminal",
        },
    })
    _wait_for(lambda: sm.read(UUID) is not None)

    # Subsequent events
    _post(port, {"hook_event_name": "UserPromptSubmit", "session_id": UUID, "prompt": "hi"})
    _post(port, {
        "hook_event_name": "PreToolUse", "session_id": UUID,
        "tool_name": "Bash", "tool_input": {"command": "x"},
    })
    _post(port, {"hook_event_name": "Stop", "session_id": UUID, "last_assistant_message": "done"})

    # jump_target must still be there.
    _wait_for(lambda: sm.read(UUID).phase == SessionPhase.IDLE)
    live = sm.read(UUID)
    assert live.jump_target is not None
    assert live.jump_target.conhost_hwnd == 0xABCD
    assert live.jump_target.terminal_app == "WindowsTerminal"


# ---------------------------------------------------------------------------
# Helpers for snapshot inspection
# ---------------------------------------------------------------------------


def _phase_of(snapshots: list[WorldSnapshot], uuid: str) -> SessionPhase | None:
    if not snapshots:
        return None
    last = snapshots[-1]
    for g in last.session_groups:
        for v in g.views:
            if v.session_uuid == uuid:
                return v.phase
    return None


def _current_tool_of(snapshots: list[WorldSnapshot], uuid: str) -> str | None:
    if not snapshots:
        return None
    last = snapshots[-1]
    for g in last.session_groups:
        for v in g.views:
            if v.session_uuid == uuid:
                return v.current_tool
    return None


def _last_prompt_of(snapshots: list[WorldSnapshot], uuid: str) -> str | None:
    if not snapshots:
        return None
    last = snapshots[-1]
    for g in last.session_groups:
        for v in g.views:
            if v.session_uuid == uuid:
                return v.last_prompt
    return None
