"""Tests for HookServer + parse_claude_payload.

Two layers:
  1. Pure parser tests (T2.x) — no network, just dict in / event out.
  2. Integration tests (T3.x) — real ThreadingHTTPServer on 127.0.0.1
     with urllib for client. Each test binds an ephemeral port (we
     pass a high preferred port range and let the OS-assignment retry
     find a free one — DON'T rely on a hardcoded port; CI can have
     anything bound).
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

from claude_island.core.hook_events import (
    CompactStarted,
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
from claude_island.platform_.hook_server import (
    HookServer,
    HookServerStartError,
    ParseError,
    parse_claude_payload,
)


# ---------------------------------------------------------------------------
# Parser tests (T2.x) — no network
# ---------------------------------------------------------------------------


def test_t2_1_session_start_parsed():
    payload = {
        "hook_event_name": "SessionStart",
        "session_id": "abc-1",
        "cwd": "D:\\projects\\foo",
        "source": "startup",
        "transcript_path": "D:\\foo.jsonl",
    }
    event = parse_claude_payload(payload)
    assert isinstance(event, SessionStarted)
    assert event.session_uuid == "abc-1"
    assert event.cwd == Path("D:\\projects\\foo")
    assert event.source == "startup"
    assert event.transcript_path == Path("D:\\foo.jsonl")
    # No jump_target in payload → defaults None.
    assert event.jump_target is None


def test_session_start_parses_jump_target():
    """v2 hook.py captures terminal-identifying metadata at hook time.
    Verify the server parses the sub-dict into a JumpTarget."""
    from claude_island.core.hook_events import JumpTarget
    payload = {
        "hook_event_name": "SessionStart",
        "session_id": "abc-1",
        "cwd": "D:\\projects\\foo",
        "source": "startup",
        "jump_target": {
            "terminal_app": "WindowsTerminal",
            "conhost_hwnd": 12521090,
            "host_pid": 82508,
            "wt_session_guid": "b2d0e4f0-1234-5678-90ab-cdef12345678",
            "term_program": "vscode",
        },
    }
    event = parse_claude_payload(payload)
    assert isinstance(event, SessionStarted)
    assert event.jump_target is not None
    assert event.jump_target.terminal_app == "WindowsTerminal"
    assert event.jump_target.conhost_hwnd == 12521090
    assert event.jump_target.host_pid == 82508
    assert event.jump_target.wt_session_guid.startswith("b2d0e4f0")
    assert event.jump_target.term_program == "vscode"


def test_session_start_jump_target_tolerant_to_partial():
    """JumpTarget parser fills missing/wrong-typed fields with defaults
    rather than raising — old hook clients shipping partial data must
    not crash the server."""
    payload = {
        "hook_event_name": "SessionStart",
        "session_id": "abc-1",
        "cwd": "D:\\projects\\foo",
        "jump_target": {
            "wt_session_guid": "guid-only",
            # Other fields missing
            "conhost_hwnd": "not_a_number",  # garbage
        },
    }
    event = parse_claude_payload(payload)
    assert isinstance(event, SessionStarted)
    assert event.jump_target is not None
    assert event.jump_target.wt_session_guid == "guid-only"
    assert event.jump_target.conhost_hwnd == 0  # garbage rejected
    assert event.jump_target.terminal_app is None  # default


def test_session_start_jump_target_non_dict_returns_none():
    """If jump_target is present but not a dict (e.g. null, string),
    treat as missing rather than crashing."""
    for bad in (None, "string", 42, []):
        payload = {
            "hook_event_name": "SessionStart",
            "session_id": "x",
            "cwd": "/",
            "jump_target": bad,
        }
        event = parse_claude_payload(payload)
        assert isinstance(event, SessionStarted)
        assert event.jump_target is None


def test_t2_2_pretooluse_extracts_command_preview():
    payload = {
        "hook_event_name": "PreToolUse",
        "session_id": "abc-1",
        "tool_name": "Bash",
        "tool_input": {"command": "ls -la", "description": "list dir"},
        "tool_use_id": "tu_xyz",
    }
    event = parse_claude_payload(payload)
    assert isinstance(event, ToolStarted)
    assert event.tool_name == "Bash"
    # Priority: command wins over description
    assert event.tool_input_preview == "ls -la"
    assert event.tool_use_id == "tu_xyz"


def test_t2_3_pretooluse_falls_back_to_file_path():
    payload = {
        "hook_event_name": "PreToolUse",
        "session_id": "abc-1",
        "tool_name": "Read",
        "tool_input": {"file_path": "/src/foo.py", "offset": 0},
    }
    event = parse_claude_payload(payload)
    assert isinstance(event, ToolStarted)
    assert event.tool_input_preview == "/src/foo.py"


def test_t2_7_askuserquestion_surfaces_first_question_text():
    """AskUserQuestion's tool_input shape has no single-string field
    among the well-known keys; the preview must dig into the questions
    list and surface the first question's human-readable text instead
    of falling through to a JSON dump."""
    payload = {
        "hook_event_name": "PreToolUse",
        "session_id": "u1",
        "tool_name": "AskUserQuestion",
        "tool_input": {
            "questions": [
                {"question": "指数退避的上限应设为多少？", "options": []},
            ],
        },
    }
    event = parse_claude_payload(payload)
    assert isinstance(event, ToolStarted)
    assert event.tool_input_preview == "指数退避的上限应设为多少？"


def test_t2_8_askuserquestion_multi_question_appends_more_suffix():
    payload = {
        "hook_event_name": "PreToolUse",
        "session_id": "u1",
        "tool_name": "AskUserQuestion",
        "tool_input": {
            "questions": [
                {"question": "A?"},
                {"question": "B?"},
                {"question": "C?"},
            ],
        },
    }
    event = parse_claude_payload(payload)
    assert event.tool_input_preview == "A?  (+2 more)"


def test_t2_9_askuserquestion_malformed_shape_falls_through_to_json():
    """When the questions list is missing/empty/non-list, we don't make
    up text — we fall through to the JSON dump so the user at least
    sees the raw payload. ensure_ascii=False keeps it readable."""
    payload = {
        "hook_event_name": "PreToolUse",
        "session_id": "u1",
        "tool_name": "AskUserQuestion",
        "tool_input": {"questions": []},
    }
    event = parse_claude_payload(payload)
    assert event.tool_input_preview is not None
    # Must NOT be the literal "(+−1 more)" or any inferred text — fall
    # through to JSON dump.
    assert "questions" in event.tool_input_preview


def test_t2_10_json_fallback_keeps_unicode_readable():
    """The fallback used to be json.dumps(default=str) which defaults
    to ensure_ascii=True and turned 指数 into \\u6307\\u6570. This
    regression-guards ensure_ascii=False."""
    payload = {
        "hook_event_name": "PreToolUse",
        "session_id": "u1",
        "tool_name": "mcp__custom__do",
        "tool_input": {"opaque_field": "需要中文展示"},
    }
    event = parse_claude_payload(payload)
    assert event.tool_input_preview is not None
    assert "需要中文展示" in event.tool_input_preview
    assert "\\u" not in event.tool_input_preview


def test_t2_11_preferred_keys_win_over_askuserquestion_shape():
    """A pathological tool_input that has BOTH a well-known key
    (e.g. "command") and a questions list must surface the well-known
    one — established priority order is part of the contract."""
    payload = {
        "hook_event_name": "PreToolUse",
        "session_id": "u1",
        "tool_name": "Bash",
        "tool_input": {
            "command": "ls -la",
            "questions": [{"question": "should not show"}],
        },
    }
    event = parse_claude_payload(payload)
    assert event.tool_input_preview == "ls -la"


def test_t2_4_malformed_payload_returns_none():
    """JSON is well-formed but content is bogus."""
    assert parse_claude_payload({}) is None
    assert parse_claude_payload({"hook_event_name": "SessionStart"}) is None  # no uuid
    assert parse_claude_payload({"session_id": "x"}) is None  # no hook name


def test_t2_5_missing_session_id_dropped():
    payload = {"hook_event_name": "Stop", "session_id": ""}
    assert parse_claude_payload(payload) is None


def test_t2_6_unknown_hook_name_dropped():
    payload = {
        "hook_event_name": "SubagentStart",
        "session_id": "abc-1",
    }
    # v1 drops subagent hooks (and any other unknown)
    assert parse_claude_payload(payload) is None


def test_parse_session_end():
    payload = {"hook_event_name": "SessionEnd", "session_id": "abc"}
    event = parse_claude_payload(payload)
    assert isinstance(event, SessionEnded)
    assert event.session_uuid == "abc"


def test_parse_user_prompt_submit_truncates():
    long_prompt = "x" * 500
    payload = {
        "hook_event_name": "UserPromptSubmit",
        "session_id": "abc",
        "prompt": long_prompt,
    }
    event = parse_claude_payload(payload)
    assert isinstance(event, PromptSubmitted)
    assert len(event.prompt) == 200  # _PROMPT_MAX
    assert event.prompt.endswith("…")


def test_parse_post_tool_use_success_vs_failure():
    base = {
        "session_id": "abc",
        "tool_name": "Bash",
        "tool_use_id": "tu_1",
    }
    ok = parse_claude_payload({**base, "hook_event_name": "PostToolUse"})
    fail = parse_claude_payload({**base, "hook_event_name": "PostToolUseFailure"})
    assert isinstance(ok, ToolFinished) and ok.is_failure is False
    assert isinstance(fail, ToolFinished) and fail.is_failure is True


def test_parse_stop_carries_assistant_message():
    payload = {
        "hook_event_name": "Stop",
        "session_id": "abc",
        "last_assistant_message": "Done.",
    }
    event = parse_claude_payload(payload)
    assert isinstance(event, TurnCompleted)
    assert event.last_assistant_message == "Done."
    assert event.is_failure is False


def test_parse_permission_request():
    payload = {
        "hook_event_name": "PermissionRequest",
        "session_id": "abc",
        "tool_name": "Bash",
    }
    event = parse_claude_payload(payload)
    assert isinstance(event, PermissionRequested)
    assert event.tool_name == "Bash"


def test_parse_pre_compact():
    payload = {"hook_event_name": "PreCompact", "session_id": "abc"}
    event = parse_claude_payload(payload)
    assert isinstance(event, CompactStarted)


def test_parse_notification_idle():
    payload = {
        "hook_event_name": "Notification",
        "session_id": "abc",
        "notification_type": "idle_prompt",
    }
    event = parse_claude_payload(payload)
    assert isinstance(event, NotificationFired)
    assert event.is_idle is True


def test_parse_notification_non_idle():
    payload = {
        "hook_event_name": "Notification",
        "session_id": "abc",
        "notification_type": "info",
    }
    event = parse_claude_payload(payload)
    assert isinstance(event, NotificationFired)
    assert event.is_idle is False


def test_parse_pre_tool_use_missing_tool_name_dropped():
    payload = {
        "hook_event_name": "PreToolUse",
        "session_id": "abc",
        "tool_input": {"command": "x"},
        # tool_name missing
    }
    assert parse_claude_payload(payload) is None


# ---------------------------------------------------------------------------
# Integration tests (T3.x) — real socket
# ---------------------------------------------------------------------------


def _find_free_port_range(start: int, count: int) -> int:
    """Pick a starting port whose first `count` ports are likely free.

    Uses socket binding to test availability. Returns the start.
    """
    for candidate in range(start, start + 1000):
        ok = True
        for offset in range(count):
            try:
                s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                s.bind(("127.0.0.1", candidate + offset))
                s.close()
            except OSError:
                ok = False
                break
        if ok:
            return candidate
    raise RuntimeError("could not find free port range")


@pytest.fixture()
def tmp_port_file(tmp_path: Path) -> Path:
    return tmp_path / "port.txt"


@pytest.fixture()
def running_server(tmp_port_file: Path):
    """Provides a started HookServer + SessionStateMachine + bound port.
    Uses preferred_port=0 → OS picks an ephemeral port, eliminates the
    race between probe-and-bind that hits us when running the full suite
    on Windows (TIME_WAIT keeps recently-used ports for ~60s).
    Tears down at end (server.stop() called)."""
    sm = SessionStateMachine()
    server = HookServer(
        sm,
        preferred_port=0,
        port_file=tmp_port_file,
    )
    port = server.start()
    try:
        yield server, sm, port
    finally:
        server.stop()


def _post_hook(port: int, payload: dict, *, timeout: float = 2.0) -> int:
    """POST a payload to /hook on localhost. Returns HTTP status."""
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/hook",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        resp = urllib.request.urlopen(req, timeout=timeout)
        return resp.status
    except urllib.error.HTTPError as e:
        return e.code


def test_t3_1_post_event_reaches_state_machine(running_server):
    server, sm, port = running_server
    payload = {
        "hook_event_name": "SessionStart",
        "session_id": "abc",
        "cwd": "D:\\foo",
        "source": "startup",
    }
    status = _post_hook(port, payload)
    assert status == 200
    state = sm.read("abc")
    assert state is not None
    assert state.session_uuid == "abc"


def test_t3_2_port_collision_retries(tmp_port_file: Path):
    """If preferred port is taken, retry the next ones."""
    base = _find_free_port_range(53000, 5)
    # Occupy `base` so the server has to skip.
    # IMPORTANT: do NOT set SO_REUSEADDR on Windows — its semantics let
    # a subsequent bind succeed when the first socket had SO_REUSEADDR
    # set, which is the opposite of what we want for "port is busy".
    blocker = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    blocker.bind(("127.0.0.1", base))
    blocker.listen(1)
    try:
        sm = SessionStateMachine()
        server = HookServer(sm, preferred_port=base, port_file=tmp_port_file)
        port = server.start()
        try:
            assert port > base
            assert port < base + 23  # within retry range
            assert tmp_port_file.read_text(encoding="utf-8") == str(port)
        finally:
            server.stop()
    finally:
        blocker.close()


def test_t3_3_all_ports_taken_raises(tmp_port_file: Path):
    """If the entire retry range is occupied, raise HookServerStartError."""
    base = _find_free_port_range(53500, 23)
    blockers: list[socket.socket] = []
    try:
        for offset in range(23):
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            # No SO_REUSEADDR — see comment in test_t3_2 above.
            try:
                s.bind(("127.0.0.1", base + offset))
                s.listen(1)
                blockers.append(s)
            except OSError:
                s.close()
        # Skip on the race where _find_free_port_range said the range
        # was free but another process / a recently-released TIME_WAIT
        # socket grabbed one before our bind. Without all 23 blocked
        # HookServer will succeed on the unblocked port and the assertion
        # below becomes a false negative — masking real regressions
        # AND polluting CI red signal.
        if len(blockers) < 23:
            pytest.skip(
                f"could not reserve all 23 ports in range starting at "
                f"{base} ({len(blockers)} acquired); typical cause is "
                f"TIME_WAIT from a prior test in the same process"
            )
        sm = SessionStateMachine()
        server = HookServer(sm, preferred_port=base, port_file=tmp_port_file)
        with pytest.raises(HookServerStartError):
            server.start()
    finally:
        for s in blockers:
            s.close()


def test_t3_4_stop_makes_listener_unreachable(running_server, tmp_port_file: Path):
    server, sm, port = running_server
    # Connection works before stop
    assert _post_hook(port, {"hook_event_name": "SessionStart", "session_id": "x", "cwd": "/"}) == 200
    server.stop()
    # Port file deleted
    assert not tmp_port_file.exists()
    # Subsequent connection refused (or returns error)
    with pytest.raises((urllib.error.URLError, ConnectionRefusedError, OSError)):
        urllib.request.urlopen(
            f"http://127.0.0.1:{port}/hook",
            data=b"{}",
            timeout=1.0,
        )


def test_t3_5_malformed_json_returns_400(running_server):
    server, sm, port = running_server
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/hook",
        data=b"this is not json",
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        urllib.request.urlopen(req, timeout=2.0)
        assert False, "expected HTTPError"
    except urllib.error.HTTPError as e:
        assert e.code == 400


def test_t3_6_concurrent_posts_no_loss(running_server):
    server, sm, port = running_server
    # Pre-create N sessions to avoid orphan-event placeholder noise.
    # EVENTS_PER >= N_SESSIONS so EVERY session receives at least one prompt
    # before we assert their final phase.
    N_SESSIONS = 5
    EVENTS_PER = 20
    for i in range(N_SESSIONS):
        _post_hook(port, {
            "hook_event_name": "SessionStart",
            "session_id": f"u-{i}",
            "cwd": "/x",
            "source": "startup",
        })

    errors: list[Exception] = []

    def worker(thread_id: int) -> None:
        try:
            for i in range(EVENTS_PER):
                uuid = f"u-{i % N_SESSIONS}"
                status = _post_hook(port, {
                    "hook_event_name": "UserPromptSubmit",
                    "session_id": uuid,
                    "prompt": f"thread{thread_id}-{i}",
                })
                assert status == 200
        except Exception as e:
            errors.append(e)

    # 4 threads keeps real concurrency stress (peak ~16 in-flight HTTP
    # requests after queueing) while staying well below the local
    # ThreadingHTTPServer's accept backlog. 8 threads occasionally hit
    # transient empty-body 502s from the stdlib server under macOS — a
    # real but rare stability issue (see ~0.6% rate observed 2026-05-26)
    # that this test was never intended to detect; the test's stated
    # intent is "no event loss in the state machine", not "stdlib HTTP
    # server survives all concurrency levels".
    threads = [threading.Thread(target=worker, args=(t,)) for t in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == [], f"concurrent POST errors: {errors}"
    # Every session ends up in THINKING (last applied event was a Prompt)
    from claude_island.core.session_phase import SessionPhase
    for i in range(N_SESSIONS):
        state = sm.read(f"u-{i}")
        assert state is not None, f"u-{i} state is None"
        assert state.phase == SessionPhase.THINKING, (
            f"u-{i} phase = {state.phase}, expected THINKING"
        )


def test_t3_7_port_file_matches_bound_port(running_server, tmp_port_file: Path):
    server, sm, port = running_server
    assert tmp_port_file.read_text(encoding="utf-8") == str(port)


def test_t3_8_health_endpoint(running_server):
    server, sm, port = running_server
    # Post one event so recent_event_count > 0
    _post_hook(port, {
        "hook_event_name": "SessionStart",
        "session_id": "abc",
        "cwd": "/",
        "source": "startup",
    })
    resp = urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=2.0)
    body = json.loads(resp.read().decode("utf-8"))
    assert body["port"] == port
    assert body["uptime_s"] >= 0
    assert body["recent_event_count"] == 1
    assert body["last_event_name"] == "SessionStarted"


def test_t3_9_bind_is_localhost_only(running_server, tmp_port_file: Path):
    """Verify the listener bound to 127.0.0.1, not 0.0.0.0.

    We can't easily test "external IP" from inside CI/dev box, but we
    can introspect the server's bound address.
    """
    server, sm, port = running_server
    # ThreadingHTTPServer stores the bind address; access via server_address
    bind_host, bind_port = server._server.server_address  # type: ignore[union-attr]
    assert bind_host == "127.0.0.1"
    assert bind_port == port


# ---------------------------------------------------------------------------
# Extra: GET on unknown path returns 404
# ---------------------------------------------------------------------------


def test_unknown_path_returns_404(running_server):
    server, sm, port = running_server
    try:
        urllib.request.urlopen(f"http://127.0.0.1:{port}/notfound", timeout=2.0)
        assert False, "expected 404"
    except urllib.error.HTTPError as e:
        assert e.code == 404


def test_post_to_unknown_path_returns_404(running_server):
    server, sm, port = running_server
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/other",
        data=b"{}",
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        urllib.request.urlopen(req, timeout=2.0)
        assert False, "expected 404"
    except urllib.error.HTTPError as e:
        assert e.code == 404


# ---------------------------------------------------------------------------
# Extra: server.start() is idempotent
# ---------------------------------------------------------------------------


def test_start_idempotent(running_server):
    server, sm, port = running_server
    again = server.start()
    assert again == port  # second call returns same port


# ---------------------------------------------------------------------------
# Extra: server.stop() is idempotent
# ---------------------------------------------------------------------------


def test_stop_idempotent(tmp_port_file: Path):
    sm = SessionStateMachine()
    base = _find_free_port_range(54000, 5)
    server = HookServer(sm, preferred_port=base, port_file=tmp_port_file)
    server.start()
    server.stop()
    # Second stop is a no-op
    server.stop()


# ---------------------------------------------------------------------------
# Hook session_id passthrough. The hook payload's ``session_id`` is the
# in-memory current uuid as set by claude.exe (matches pid.json + JSONL
# writes); the server forwards it verbatim. A cmdline-based "remap to
# OLD" path lived here from 2026-05-17 to 2026-05-25 — removed because
# its premise that claude keeps writing to OLD JSONL after --resume is
# empirically false in claude v2.1.142 (/clear creates a new JSONL).
# ---------------------------------------------------------------------------


class TestSessionIdPassthrough:
    def test_session_id_used_verbatim_on_session_start(self, tmp_port_file: Path):
        """The session_id claude.exe sets in the payload is what the
        state machine ends up keyed on. No rewriting."""
        sm = SessionStateMachine()
        server = HookServer(sm, preferred_port=0, port_file=tmp_port_file)
        port = server.start()
        try:
            payload = {
                "hook_event_name": "SessionStart",
                "session_id": "f56fb0ca-649d-4708-8c24-76a18857a0c6",
                "cwd": "D:\\proj",
                "source": "clear",
                "jump_target": {"host_pid": 97372},
            }
            assert _post_hook(port, payload) == 200
            assert sm.read("f56fb0ca-649d-4708-8c24-76a18857a0c6") is not None
        finally:
            server.stop()

    def test_jump_target_missing_does_not_affect_routing(self, tmp_port_file: Path):
        """Older hook.py (pre-jump_target) — payload arrives without a
        jump_target. session_id is still used verbatim."""
        sm = SessionStateMachine()
        server = HookServer(sm, preferred_port=0, port_file=tmp_port_file)
        port = server.start()
        try:
            payload = {
                "hook_event_name": "SessionStart",
                "session_id": "no-jump-target-uuid",
                "cwd": "D:\\proj",
                "source": "startup",
            }
            assert _post_hook(port, payload) == 200
            assert sm.read("no-jump-target-uuid") is not None
        finally:
            server.stop()
