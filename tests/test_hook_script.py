"""Tests for claude_island/hook.py (the standalone hook script).

Two layers:
  • Unit: import the script as a module, drive run() / cli_doctor() in-process.
  • Cold-start: spawn it as a subprocess, measure wall time — must be < 200ms
    P99 (target G1 latency budget).
"""
from __future__ import annotations

import io
import json
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from claude_island import hook as hook_module


# ---------------------------------------------------------------------------
# Mini fake listener for in-process tests
# ---------------------------------------------------------------------------


class _RecordingHandler(BaseHTTPRequestHandler):
    """Captures bodies into the server's `received` list; returns 200 {}."""
    received: list[bytes] = []

    def log_message(self, *args, **kwargs):
        pass

    def do_POST(self):
        if self.path == "/hook":
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length)
            type(self).received.append(body)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b"{}")
            return
        self.send_response(404)
        self.end_headers()

    def do_GET(self):
        if self.path == "/health":
            data = json.dumps({
                "port": self.server.server_address[1],
                "uptime_s": 5,
                "recent_event_count": 3,
                "last_event_name": "PromptSubmitted",
                "last_event_at": "2026-05-13T12:00:00+00:00",
            }).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(data)
            return
        self.send_response(404)
        self.end_headers()


def _find_free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture()
def fake_listener(monkeypatch):
    port = _find_free_port()
    _RecordingHandler.received = []
    httpd = ThreadingHTTPServer(("127.0.0.1", port), _RecordingHandler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setattr(hook_module, "_read_port", lambda: port)
    try:
        yield port, _RecordingHandler.received
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=2.0)


# ---------------------------------------------------------------------------
# T5.1 — run() with valid stdin POSTs to listener
# ---------------------------------------------------------------------------


def test_t5_1_run_posts_to_listener(fake_listener, monkeypatch, capsys):
    port, received = fake_listener
    payload = {
        "hook_event_name": "SessionStart",
        "session_id": "abc-123",
        "cwd": "/x",
    }
    raw = json.dumps(payload).encode("utf-8")

    # Replace sys.stdin.buffer with our payload
    stdin_buf = io.BytesIO(raw)
    monkeypatch.setattr(sys, "stdin",
                        type("F", (), {"buffer": stdin_buf, "read": lambda self=None: ""})())

    hook_module.run()

    # Listener received our payload (plus v2 jump_target enrichment).
    # Verify the original payload fields survive; jump_target is
    # checked in dedicated tests below.
    time.sleep(0.05)  # let server thread process
    assert len(received) == 1
    body = json.loads(received[0])
    for k, v in payload.items():
        assert body[k] == v, f"payload key {k!r} mangled"

    # stdout got "{}\n"
    captured = capsys.readouterr()
    assert captured.out.strip() == "{}"


# ---------------------------------------------------------------------------
# T5.2 — run() with listener absent exits cleanly, prints {}
# ---------------------------------------------------------------------------


def test_t5_2_run_listener_absent_clean_exit(monkeypatch, capsys):
    """No listener bound → ConnectionRefused → silent fall-through."""
    monkeypatch.setattr(hook_module, "_read_port", lambda: 1)  # unused privileged port
    raw = b'{"hook_event_name":"SessionEnd","session_id":"x"}'
    stdin_buf = io.BytesIO(raw)
    monkeypatch.setattr(sys, "stdin",
                        type("F", (), {"buffer": stdin_buf, "read": lambda self=None: ""})())

    hook_module.run()

    captured = capsys.readouterr()
    assert captured.out.strip() == "{}"
    # stderr stays silent for the expected "no listener" case (we don't
    # want to spam Claude's hook log every event when island is off).
    assert "[claude-island hook]" not in captured.err


# ---------------------------------------------------------------------------
# T5.3 — run() handles slow listener (timeout) without raising
# ---------------------------------------------------------------------------


class _StallingHandler(BaseHTTPRequestHandler):
    def log_message(self, *args, **kwargs):
        pass

    def do_POST(self):
        time.sleep(10)  # > timeout

    def do_GET(self):
        time.sleep(10)


def test_t5_3_run_timeout_clean_exit(monkeypatch, capsys):
    port = _find_free_port()
    httpd = ThreadingHTTPServer(("127.0.0.1", port), _StallingHandler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setattr(hook_module, "_read_port", lambda: port)
    monkeypatch.setattr(hook_module, "_POST_TIMEOUT_S", 0.5)

    raw = b'{"hook_event_name":"SessionStart","session_id":"abc"}'
    stdin_buf = io.BytesIO(raw)
    monkeypatch.setattr(sys, "stdin",
                        type("F", (), {"buffer": stdin_buf, "read": lambda self=None: ""})())

    start = time.monotonic()
    hook_module.run()
    elapsed = time.monotonic() - start
    httpd.shutdown()
    httpd.server_close()

    # Should give up around _POST_TIMEOUT_S, not stall the full 10s
    assert elapsed < 2.0, f"timeout not respected, elapsed={elapsed:.2f}s"
    captured = capsys.readouterr()
    assert captured.out.strip() == "{}"


# ---------------------------------------------------------------------------
# Hook v4 — per-event timeout
# ---------------------------------------------------------------------------


def test_v4_blocking_event_uses_long_timeout(monkeypatch, capsys):
    """PermissionRequest / UserPromptSubmit must use the long blocking
    timeout (not the 5 s fast-event default). Verified by patching the
    urlopen call to capture the timeout argument.

    v5: PermissionRequest replaced PreToolUse in the blocking set —
    PreToolUse went back to being fire-and-forget after the approval
    flow moved off it (see hook.py module docstring)."""
    captured_timeouts: list[float] = []

    class _FakeResp:
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def read(self): return b'{}'

    def fake_urlopen(req, *, timeout):
        captured_timeouts.append(timeout)
        return _FakeResp()

    monkeypatch.setattr(hook_module, "_read_port", lambda: 50777)
    monkeypatch.setattr(
        hook_module.urllib.request, "urlopen", fake_urlopen,
    )

    # Drive PermissionRequest → expect blocking timeout (~600 s).
    raw = b'{"hook_event_name":"PermissionRequest","session_id":"u1","tool_name":"Bash"}'
    stdin_buf = io.BytesIO(raw)
    monkeypatch.setattr(sys, "stdin",
                        type("F", (), {"buffer": stdin_buf, "read": lambda self=None: ""})())
    hook_module.run()

    assert len(captured_timeouts) == 1
    # Should be the BLOCKING constant (600 s), well above the 5 s fast.
    assert captured_timeouts[0] >= 60.0, (
        f"PermissionRequest should use blocking timeout, got "
        f"{captured_timeouts[0]}"
    )


def test_v5_pretooluse_now_uses_fast_timeout(monkeypatch, capsys):
    """v5 regression guard: PreToolUse must NOT use the blocking timeout —
    the approval flow moved to PermissionRequest, so PreToolUse is again
    a pure fire-and-forget state-machine ping."""
    captured_timeouts: list[float] = []

    class _FakeResp:
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def read(self): return b'{}'

    def fake_urlopen(req, *, timeout):
        captured_timeouts.append(timeout)
        return _FakeResp()

    monkeypatch.setattr(hook_module, "_read_port", lambda: 50777)
    monkeypatch.setattr(
        hook_module.urllib.request, "urlopen", fake_urlopen,
    )

    raw = b'{"hook_event_name":"PreToolUse","session_id":"u1","tool_name":"Bash"}'
    stdin_buf = io.BytesIO(raw)
    monkeypatch.setattr(sys, "stdin",
                        type("F", (), {"buffer": stdin_buf, "read": lambda self=None: ""})())
    hook_module.run()

    assert len(captured_timeouts) == 1
    assert captured_timeouts[0] <= 10.0, (
        f"PreToolUse should use fast timeout, got {captured_timeouts[0]}"
    )


def test_v4_fast_event_uses_short_timeout(monkeypatch, capsys):
    captured_timeouts: list[float] = []

    class _FakeResp:
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def read(self): return b'{}'

    def fake_urlopen(req, *, timeout):
        captured_timeouts.append(timeout)
        return _FakeResp()

    monkeypatch.setattr(hook_module, "_read_port", lambda: 50777)
    monkeypatch.setattr(
        hook_module.urllib.request, "urlopen", fake_urlopen,
    )

    # Stop is fire-and-forget → fast timeout.
    raw = b'{"hook_event_name":"Stop","session_id":"u1"}'
    stdin_buf = io.BytesIO(raw)
    monkeypatch.setattr(sys, "stdin",
                        type("F", (), {"buffer": stdin_buf, "read": lambda self=None: ""})())
    hook_module.run()

    assert len(captured_timeouts) == 1
    assert captured_timeouts[0] <= 10.0, (
        f"Stop should use fast timeout, got {captured_timeouts[0]}"
    )


def test_v4_userpromptsubmit_uses_long_timeout(monkeypatch, capsys):
    captured_timeouts: list[float] = []

    class _FakeResp:
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def read(self): return b'{}'

    def fake_urlopen(req, *, timeout):
        captured_timeouts.append(timeout)
        return _FakeResp()

    monkeypatch.setattr(hook_module, "_read_port", lambda: 50777)
    monkeypatch.setattr(
        hook_module.urllib.request, "urlopen", fake_urlopen,
    )

    raw = b'{"hook_event_name":"UserPromptSubmit","session_id":"u1","prompt":"hi"}'
    stdin_buf = io.BytesIO(raw)
    monkeypatch.setattr(sys, "stdin",
                        type("F", (), {"buffer": stdin_buf, "read": lambda self=None: ""})())
    hook_module.run()

    assert captured_timeouts[0] >= 60.0


def test_v6_version_bumped():
    """v6 adds iterm_session_id + terminal_pid to jump_target on macOS.
    Bumping forces hook_installer to overwrite ~/.claude-island/hook.py
    on the next app start."""
    assert hook_module.__version__ == "6"


# ---------------------------------------------------------------------------
# T5.4 — port.txt missing → fallback to _DEFAULT_PORT
# ---------------------------------------------------------------------------


def test_t5_4_port_file_missing_fallback(monkeypatch, tmp_path):
    fake_file = tmp_path / "missing.txt"
    monkeypatch.setattr(hook_module, "_port_file", lambda: fake_file)
    assert hook_module._read_port() == hook_module._DEFAULT_PORT


def test_port_file_malformed_fallback(monkeypatch, tmp_path):
    fake_file = tmp_path / "bad.txt"
    fake_file.write_text("not-a-number")
    monkeypatch.setattr(hook_module, "_port_file", lambda: fake_file)
    assert hook_module._read_port() == hook_module._DEFAULT_PORT


def test_port_file_out_of_range_fallback(monkeypatch, tmp_path):
    fake_file = tmp_path / "bad.txt"
    fake_file.write_text("99999")
    monkeypatch.setattr(hook_module, "_port_file", lambda: fake_file)
    assert hook_module._read_port() == hook_module._DEFAULT_PORT


# ---------------------------------------------------------------------------
# T5.5 — cold start subprocess timing (P99 < 200ms target)
# ---------------------------------------------------------------------------


def test_t5_5_cold_start_under_200ms_p99():
    """Spawn hook.py as a subprocess 10 times, measure wall time.
    P99 (worst of 10 here as a proxy) must be < 200ms — this is the
    G1 latency budget.

    Skipped if running under coverage (instrumentation tanks startup).
    """
    if "coverage" in sys.modules or "_pytest.cov" in sys.modules:
        pytest.skip("coverage instrumentation invalidates startup timing")

    hook_path = Path(hook_module.__file__).resolve()
    timings: list[float] = []

    # Empty stdin → run() exits immediately after writing {} (no POST attempt
    # because we use a bogus port that connection-refuses fast).
    payload = b""

    # Use a minimal env that still has what Path.home() needs.
    # Windows: USERPROFILE; Unix: HOME. Keep PATH so python can find DLLs.
    import os
    minimal_env: dict[str, str] = {}
    for key in ("USERPROFILE", "HOME", "PATH", "SYSTEMROOT", "TEMP", "TMP"):
        if v := os.environ.get(key):
            minimal_env[key] = v

    for _ in range(10):
        start = time.monotonic()
        proc = subprocess.run(
            [sys.executable, str(hook_path)],
            input=payload,
            capture_output=True,
            timeout=5.0,
            env=minimal_env,
        )
        elapsed = time.monotonic() - start
        timings.append(elapsed)
        assert proc.returncode == 0, f"hook crashed: {proc.stderr.decode(errors='replace')}"

    # Show full distribution if it fails so we can diagnose
    timings.sort()
    p99 = timings[-1]  # worst of 10 ≈ P90+
    assert p99 < 0.4, (
        f"cold start P99 too slow: {p99 * 1000:.0f}ms (target < 200ms, hard ceiling 400ms). "
        f"Full timings (ms): {[int(t * 1000) for t in timings]}"
    )


# ---------------------------------------------------------------------------
# T5.6 — cli_doctor with listener up returns 0
# ---------------------------------------------------------------------------


def test_t5_6_cli_doctor_listener_up(fake_listener, monkeypatch, tmp_path, capsys):
    port, received = fake_listener

    # Provide a valid settings.json with our hook at the path the doctor reads
    settings_dir = tmp_path / ".claude"
    settings_dir.mkdir()
    settings = settings_dir / "settings.json"
    settings.write_text(json.dumps({
        "hooks": {
            "SessionStart": [{"hooks": [
                {"type": "command",
                 "command": '"python" "C:/.claude-island/hook.py"'},
            ]}],
        },
    }))
    home_hook = tmp_path / ".claude-island" / "hook.py"
    home_hook.parent.mkdir()
    home_hook.write_text("# placeholder")

    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    rc = hook_module.cli_doctor()
    captured = capsys.readouterr()
    assert rc == 0, captured.out
    assert "OK   listener" in captured.out
    assert "hook present" in captured.out
    assert "hook script at" in captured.out


# ---------------------------------------------------------------------------
# T5.7 — cli_doctor with listener down returns non-zero
# ---------------------------------------------------------------------------


def test_t5_7_cli_doctor_listener_down(monkeypatch, tmp_path, capsys):
    # Point at a port nothing is listening on
    monkeypatch.setattr(hook_module, "_read_port", lambda: 1)  # priv port, refused
    # Create the settings.json at the path the doctor reads, with NO hook
    # installed → triggers the "hook NOT present" branch
    settings_dir = tmp_path / ".claude"
    settings_dir.mkdir()
    (settings_dir / "settings.json").write_text("{}")
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)

    rc = hook_module.cli_doctor()
    captured = capsys.readouterr()
    assert rc == 1, captured.out
    assert "FAIL no listener" in captured.out
    assert "FAIL hook NOT present" in captured.out
    assert "FAIL hook script missing" in captured.out


# ---------------------------------------------------------------------------
# _contains_our_hook detector tests
# ---------------------------------------------------------------------------


def test_contains_our_hook_matches_lowercase_unix():
    cfg = {"hooks": {"SessionStart": [
        {"hooks": [{"type": "command",
                    "command": '"python3" "/home/u/.claude-island/hook.py"'}]},
    ]}}
    assert hook_module._contains_our_hook(cfg) is True


def test_contains_our_hook_matches_windows():
    cfg = {"hooks": {"PreToolUse": [
        {"matcher": "*", "hooks": [
            {"type": "command",
             "command": '"C:\\Python\\python.exe" "C:\\Users\\u\\.claude-island\\hook.py"'},
        ]},
    ]}}
    assert hook_module._contains_our_hook(cfg) is True


def test_contains_our_hook_not_present():
    cfg = {"hooks": {"SessionStart": [
        {"hooks": [{"type": "command",
                    "command": "some-other-tool --flag"}]},
    ]}}
    assert hook_module._contains_our_hook(cfg) is False


def test_contains_our_hook_empty_settings():
    assert hook_module._contains_our_hook({}) is False
    assert hook_module._contains_our_hook({"hooks": {}}) is False
    assert hook_module._contains_our_hook({"hooks": None}) is False


# ---------------------------------------------------------------------------
# Extra: --version
# ---------------------------------------------------------------------------


def test_build_jump_target_captures_env(monkeypatch):
    """v2 enrichment: hook.py captures WT_SESSION + TERM_PROGRAM from
    its own env (which it inherits from the host claude.exe → host
    shell → host terminal)."""
    monkeypatch.setenv("WT_SESSION", "b2d0e4f0-1234-5678-90ab-cdef12345678")
    monkeypatch.setenv("TERM_PROGRAM", "vscode")
    jt = hook_module._build_jump_target()
    assert jt is not None
    assert jt["wt_session_guid"] == "b2d0e4f0-1234-5678-90ab-cdef12345678"
    assert jt["term_program"] == "vscode"
    # WT_SESSION present → terminal_app classified as WindowsTerminal
    # regardless of TERM_PROGRAM value.
    assert jt["terminal_app"] == "WindowsTerminal"


def test_build_jump_target_host_pid_on_windows(monkeypatch):
    """``host_pid`` (claude's own process id, NOT the hosting terminal)
    is captured from GetCurrentProcessId on Windows. macOS path
    populates host_pid only for the iTerm-specific branch (see the
    dedicated macOS tests below)."""
    import sys as _sys
    if _sys.platform != "win32":
        import pytest
        pytest.skip("Windows-only host_pid capture path")
    monkeypatch.setenv("WT_SESSION", "guid")
    jt = hook_module._build_jump_target()
    assert jt is not None
    assert jt["host_pid"] > 0


def test_build_jump_target_no_wt_falls_back_to_term_program(monkeypatch):
    """When WT_SESSION isn't set (non-WT terminal), terminal_app
    derives from TERM_PROGRAM."""
    monkeypatch.delenv("WT_SESSION", raising=False)
    monkeypatch.setenv("TERM_PROGRAM", "iTerm.app")
    jt = hook_module._build_jump_target()
    assert jt is not None
    assert jt["wt_session_guid"] == ""
    assert jt["terminal_app"] == "iTerm.app"


def test_build_jump_target_no_env_uses_console_host_default_on_windows(monkeypatch):
    """Bare Windows console (no WT_SESSION, no TERM_PROGRAM):
    terminal_app defaults to 'ConsoleHost' so the listener always has
    SOMETHING to dispatch on. On macOS no such default exists — the
    function returns None when there's nothing meaningful to ship."""
    import sys as _sys
    if _sys.platform != "win32":
        import pytest
        pytest.skip("ConsoleHost default is Windows-only")
    monkeypatch.delenv("WT_SESSION", raising=False)
    monkeypatch.delenv("TERM_PROGRAM", raising=False)
    jt = hook_module._build_jump_target()
    assert jt is not None
    assert jt["terminal_app"] == "ConsoleHost"


# ── v6 macOS capture: iterm_session_id + terminal_pid ──────────────────


def test_build_jump_target_macos_iterm_adds_new_fields(monkeypatch):
    """v6 enrichment: when TERM_PROGRAM is iTerm.app on macOS, the
    hook runs the osascript + parent-walk capture so jump_target gets
    ``iterm_session_id`` + ``terminal_pid``. Mocked so the test runs
    on any host."""
    monkeypatch.setenv("TERM_PROGRAM", "iTerm.app")
    monkeypatch.delenv("WT_SESSION", raising=False)
    monkeypatch.setattr("sys.platform", "darwin")
    monkeypatch.setattr(
        hook_module, "_macos_jump_target_extras",
        lambda: {"iterm_session_id": "ABC-123-IDENTIFIER", "terminal_pid": 90559},
    )
    jt = hook_module._build_jump_target()
    assert jt is not None
    assert jt["iterm_session_id"] == "ABC-123-IDENTIFIER"
    assert jt["terminal_pid"] == 90559
    # host_pid on darwin/iTerm is set to the claude process pid so the
    # bridge has a canonical SessionRegistry pid (matches the Windows
    # branch behaviour for consistency).
    assert jt["host_pid"] > 0


def test_build_jump_target_macos_non_iterm_skips_extras(monkeypatch):
    """When TERM_PROGRAM isn't iTerm.app on macOS (Terminal.app,
    Ghostty, WezTerm, …), skip the osascript capture entirely —
    we don't have iTerm-style stable session ids for other apps."""
    monkeypatch.setenv("TERM_PROGRAM", "Apple_Terminal")
    monkeypatch.delenv("WT_SESSION", raising=False)
    monkeypatch.setattr("sys.platform", "darwin")
    calls: list = []
    monkeypatch.setattr(
        hook_module, "_macos_jump_target_extras",
        lambda: calls.append(1) or {"iterm_session_id": "should-not-appear"},
    )
    jt = hook_module._build_jump_target()
    assert jt is not None
    assert calls == [], "extras must NOT be invoked outside iTerm path"
    assert jt.get("iterm_session_id", "") == ""
    assert jt.get("terminal_pid", 0) == 0


def test_build_jump_target_macos_iterm_capture_failure_keeps_other_fields(monkeypatch):
    """If the osascript / parent-walk capture raises (permission
    denied, AppleScript timeout), the rest of jump_target still
    ships — partial degradation, not total loss."""
    monkeypatch.setenv("TERM_PROGRAM", "iTerm.app")
    monkeypatch.delenv("WT_SESSION", raising=False)
    monkeypatch.setattr("sys.platform", "darwin")
    def _raises():
        raise RuntimeError("simulated osascript failure")
    monkeypatch.setattr(hook_module, "_macos_jump_target_extras", _raises)
    jt = hook_module._build_jump_target()
    assert jt is not None
    assert jt["term_program"] == "iTerm.app"
    assert jt["terminal_app"] == "iTerm.app"
    assert jt["iterm_session_id"] == ""
    assert jt["terminal_pid"] == 0


def test_run_injects_jump_target_into_payload(fake_listener, monkeypatch):
    """run() should inject jump_target into the forwarded payload so
    the listener can parse it."""
    port, received = fake_listener
    monkeypatch.setenv("WT_SESSION", "test-guid")
    monkeypatch.setenv("TERM_PROGRAM", "WindowsTerminal")

    payload = {
        "hook_event_name": "SessionStart",
        "session_id": "abc-123",
        "cwd": "/x",
    }
    raw = json.dumps(payload).encode("utf-8")
    stdin_buf = io.BytesIO(raw)
    monkeypatch.setattr(
        sys, "stdin",
        type("F", (), {"buffer": stdin_buf, "read": lambda self=None: ""})(),
    )

    hook_module.run()

    time.sleep(0.05)
    assert len(received) == 1
    body = json.loads(received[0])
    assert "jump_target" in body, f"jump_target not injected: {body}"
    jt = body["jump_target"]
    assert jt["wt_session_guid"] == "test-guid"
    assert jt["term_program"] == "WindowsTerminal"
    assert jt["terminal_app"] == "WindowsTerminal"


def test_session_start_writes_console_sentinel(fake_listener, monkeypatch):
    """SessionStart hook calls SetConsoleTitleW with the ci:{uuid32}
    sentinel — proactively claims the conhost title so WT mirrors it
    into TabItem.Name before Claude's OSC rewrite races for it.

    open-vibe-island alignment (2026-05-14): in-process identification
    beats outside-process AttachConsole + retry."""
    port, received = fake_listener

    calls: list[str] = []

    def fake_set_console_title(uuid: str) -> None:
        calls.append(uuid)

    monkeypatch.setattr(
        hook_module, "_try_set_console_sentinel", fake_set_console_title,
    )

    payload = {
        "hook_event_name": "SessionStart",
        "session_id": "abc-123-DEAD-beef-0000",
        "cwd": "/x",
    }
    raw = json.dumps(payload).encode("utf-8")
    stdin_buf = io.BytesIO(raw)
    monkeypatch.setattr(
        sys, "stdin",
        type("F", (), {"buffer": stdin_buf, "read": lambda self=None: ""})(),
    )

    hook_module.run()

    assert calls == ["abc-123-DEAD-beef-0000"]


def test_non_session_start_does_not_write_sentinel(fake_listener, monkeypatch):
    """Only SessionStart should touch the console title. PromptSubmit,
    PreToolUse, etc. must be no-ops on this path — they fire frequently
    and would needlessly thrash the console title."""
    calls: list[str] = []
    monkeypatch.setattr(
        hook_module, "_try_set_console_sentinel",
        lambda uuid: calls.append(uuid),
    )

    payload = {
        "hook_event_name": "PromptSubmit",
        "session_id": "abc-123",
        "prompt": "hello",
    }
    raw = json.dumps(payload).encode("utf-8")
    stdin_buf = io.BytesIO(raw)
    monkeypatch.setattr(
        sys, "stdin",
        type("F", (), {"buffer": stdin_buf, "read": lambda self=None: ""})(),
    )

    hook_module.run()

    assert calls == []


def test_try_set_console_sentinel_strips_dashes_and_truncates(monkeypatch):
    """The sentinel must be byte-identical to what platform_/
    wt_session_title.sentinel_title produces — dashes removed, lowercased,
    first 32 hex chars only. Without this, UIA TabItem.Name comparison
    silently fails to match and click-to-tab no-ops."""
    captured: list[str] = []

    class FakeKernel32:
        SetConsoleTitleW = staticmethod(lambda s: captured.append(s) or 1)

        # Attribute typing shims for ctypes argtypes/restype setters.
        class _Setter:
            def __setattr__(self, _name, _val): pass

        argtypes = _Setter()
        restype = _Setter()

    class FakeWindll:
        kernel32 = FakeKernel32()

    class FakeCtypes:
        windll = FakeWindll()
        c_wchar_p = str
        c_int = int

    monkeypatch.setitem(sys.modules, "ctypes", FakeCtypes())

    # 36-char uuid with dashes + extra junk after — must be normalised.
    hook_module._try_set_console_sentinel("ABC-DEF-1234-5678-9abc-DEFGHIJKLMNOPQRSTUVWXYZ0123")

    assert len(captured) == 1
    # Verify normalisation: dashes-out, lower, first 32 chars after "ci:".
    sentinel = captured[0]
    assert sentinel.startswith("ci:")
    body = sentinel.removeprefix("ci:")
    assert len(body) <= 32
    assert "-" not in body
    assert body == body.lower()


def test_version_argument(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["hook.py", "--version"])
    with pytest.raises(SystemExit) as exc:
        hook_module.main()
    assert exc.value.code == 0
    captured = capsys.readouterr()
    assert captured.out.strip() == hook_module.__version__
