"""Standalone Claude Code hook entrypoint.

This file is invoked by Claude Code as a subprocess for every hook event.
Two invocation paths use the same code:

  • From settings.json (the actual hook):
        python ~/.claude-island/hook.py
    stdin = ClaudeHookPayload JSON, stdout = directive (v1: always "{}")

  • From the user (--doctor / CLI):
        python -m claude_island.hook --doctor
    prints listener health + settings.json install state

Constraints:
  • stdlib only — no PySide6, no reactivex, no other claude_island imports
  • cold start < 200ms (measured P99 on Windows + Python 3.12)
  • exit 0 on ANY failure (G6 fail-open) — Claude must never hang on us

The package-internal copy of this file is bundled with pip. At app boot
``hook_installer.sync_hook_script`` copies it to ~/.claude-island/hook.py
so settings.json's ``command`` can point at a stable path that survives
``pip install --upgrade``.

The ``__version__`` constant below is the sync key — version bumps
trigger ~/.claude-island/hook.py to be overwritten on next app boot.
Bump on any user-visible semantics change.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

__version__ = "4"
"""Stable schema version of this hook script.

When ``hook_installer.sync_hook_script`` runs at boot it compares this
constant against the version in ``~/.claude-island/hook.py``. A mismatch
overwrites the user's copy. Bump when:
  • the wire format changes
  • the bundled-vs-home script content changes meaningfully
Do NOT bump for whitespace / comment-only edits.

v2 (2026-05-14): inject ``jump_target`` sub-dict into the payload before
forwarding to listener. Captures WT_SESSION / TERM_PROGRAM env + conhost
hwnd at hook time so the click handler doesn't need to AttachConsole.

v3 (2026-05-14): on SessionStart, proactively SetConsoleTitleW the
``ci:{uuid}`` sentinel from inside the claude.exe console so WT
mirrors it into TabItem.Name before Claude's OSC overwrite has a
chance to race. Eliminates the click-time fallback diagnostic in the
common (non-suppressApplicationTitle) case.

v4 (2026-05-14): per-event POST timeout. ``PreToolUse`` and
``UserPromptSubmit`` may block on the listener for up to 600 s waiting
for the user to approve / inject context (Bidirectional Hooks v1
design). Other events keep the 5 s timeout — they're pure
fire-and-forget and the longer wait would just mask listener bugs.
Fail-open contract preserved: any timeout (5 s or 600 s) still results
in stdout="{}" and exit 0 so Claude never hangs on us.
"""

_DEFAULT_PORT = 50777

# Default timeout for fire-and-forget hook events (Stop, PostToolUse,
# SessionStart, …). Bounded so Claude doesn't hang if the listener
# stalls or has a bug — the listener would normally reply within ms.
_POST_TIMEOUT_FAST_S = 5.0

# Timeout for events that may legitimately block on a human decision
# (PreToolUse approval, UserPromptSubmit review). Matches Claude Code's
# default command-hook timeout so the hook process still exits 0
# within Claude's own deadline. The listener uses a slightly shorter
# wait (598 s — see core/pending_decisions.WAIT_TIMEOUT_SAFETY_S) so
# a defer directive still squeaks in before this hard cap.
_POST_TIMEOUT_BLOCKING_S = 600.0

# Hook events that may legitimately block on a human in the loop. All
# others use _POST_TIMEOUT_FAST_S.
_BLOCKING_HOOK_EVENTS = frozenset({
    "PreToolUse",
    "UserPromptSubmit",
})

# Back-compat alias — older tests + external scripts reference the
# constant. Resolve to the FAST timeout to preserve the previous
# semantics (no blocking events existed in v3).
_POST_TIMEOUT_S = _POST_TIMEOUT_FAST_S


def _port_file() -> Path:
    """Lazy resolver — Path.home() can raise on some Windows envs (no
    USERPROFILE), so we don't call it at module-import time. Callers
    that want a stable cached path should wrap this themselves; the
    per-call cost is one envvar read.
    """
    return Path.home() / ".claude-island" / "port.txt"


# Back-compat module-level alias so tests that monkeypatch `_PORT_FILE`
# still work. Resolved lazily via __getattr__.
def __getattr__(name: str):  # pragma: no cover — only used by tests
    if name == "_PORT_FILE":
        return _port_file()
    raise AttributeError(name)


# ---------------------------------------------------------------------------
# Main entry — dispatches by argv.
# ---------------------------------------------------------------------------


def main() -> None:
    """Default entrypoint. ``python hook.py`` runs ``run()``;
    ``python hook.py --doctor`` runs ``cli_doctor()`` instead.

    Wrapped in try/except so the process never exits non-zero — that
    would propagate to Claude Code as a hook failure (G6 fail-open)."""
    try:
        argv = sys.argv[1:]
        if "--doctor" in argv:
            sys.exit(cli_doctor())
        if "--version" in argv:
            print(__version__)
            sys.exit(0)
        run()
    except SystemExit:
        raise
    except Exception as e:
        # Last-resort safety net. Print to stderr (Claude shows hook
        # stderr in logs) but exit 0 so Claude continues.
        _stderr(f"hook crashed: {e!r}")
        sys.exit(0)


# ---------------------------------------------------------------------------
# run() — the actual hook entry. Read stdin, POST, write stdout.
# ---------------------------------------------------------------------------


def run() -> None:
    """Read Claude payload from stdin, forward to local listener,
    write listener response (or ``{}``) to stdout.

    Hook-side enrichment (v2, 2026-05-14): before forwarding, parse
    the payload and inject a ``jump_target`` sub-dict capturing the
    terminal that hosts this claude session. The listener uses this
    to skip syscalls at click time and to render terminal context
    in the UI. See ``_build_jump_target`` for the fields.

    EVERY failure path here exits 0 — Claude treats non-zero exit
    as hook failure which can block the CLI (see Claude Code hook docs).
    The contract is "the listener is best-effort; Claude proceeds
    regardless".
    """
    # stdin can be empty (some Claude hooks fire with no payload).
    try:
        raw = sys.stdin.buffer.read()
    except Exception as e:
        _stderr(f"stdin read failed: {e!r}")
        sys.stdout.write("{}\n")
        return
    if not raw:
        sys.stdout.write("{}\n")
        return

    # ── v2 enrichment: inject jump_target before forwarding ──────────
    enriched = raw
    payload: dict | None = None  # bind for v4 timeout dispatch below
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, dict):
            payload = parsed
            jt = _build_jump_target()
            if jt:
                payload["jump_target"] = jt
                enriched = json.dumps(payload).encode("utf-8")
            # Open-vibe-island alignment (2026-05-14): on SessionStart,
            # proactively SetConsoleTitleW to our ci:UUID sentinel from
            # INSIDE the claude.exe console. The hook subprocess
            # inherits its parent's console on Windows (subprocess.Popen
            # default — no CREATE_NEW_CONSOLE flag from Claude Code), so
            # this call updates the same conhost that WT mirrors into
            # TabItem.Name. Done here (not by listener via win32 outside
            # the process) because:
            #   1. No AttachConsole/FreeConsole race window.
            #   2. Wins against the first OSC rewrite — we run before
            #      Claude paints its prompt.
            #   3. Profiles with suppressApplicationTitle:true still
            #      block this, but at least one external blocker
            #      (Claude's OSC) is no longer in the race.
            hook_event = payload.get("hook_event_name")
            session_id = payload.get("session_id")
            if hook_event == "SessionStart" and isinstance(session_id, str) and session_id:
                _try_set_console_sentinel(session_id)
    except Exception as e:
        # Enrichment is opportunistic — never block the forward.
        _stderr(f"jump_target enrichment failed: {e!r}")

    port = _read_port()

    # v4: per-event timeout. PreToolUse / UserPromptSubmit may block on
    # the listener for up to 600 s while the user approves; everything
    # else is pure fire-and-forget at 5 s. Falling back to FAST when the
    # hook_event_name couldn't be parsed is safe — those events don't
    # legitimately block.
    hook_event = payload.get("hook_event_name") if payload else None
    # Use ``_POST_TIMEOUT_S`` for the fast path (not ``_POST_TIMEOUT_FAST_S``)
    # so existing tests that monkeypatch ``_POST_TIMEOUT_S`` still control
    # the effective timeout. The two constants point at the same value
    # by default; tests pin the patchable one.
    timeout = (
        _POST_TIMEOUT_BLOCKING_S
        if hook_event in _BLOCKING_HOOK_EVENTS
        else _POST_TIMEOUT_S
    )

    # Forward to listener. ANY error → fall back to silent {} stdout.
    body = b"{}"
    try:
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/hook",
            data=enriched,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read() or b"{}"
    except (urllib.error.URLError, ConnectionRefusedError, OSError, TimeoutError) as e:
        # Listener absent / unreachable / slow. Don't log — this is the
        # expected case when claude-island isn't running, and Claude
        # invokes hooks frequently enough that the noise would be a
        # nuisance. The user runs --doctor to diagnose.
        pass
    except Exception as e:
        _stderr(f"hook POST failed: {e!r}")

    # Always write stdout — Claude reads the directive even on no-op.
    try:
        sys.stdout.buffer.write(body)
        if not body.endswith(b"\n"):
            sys.stdout.buffer.write(b"\n")
    except Exception:
        pass    # absolute last resort — pipe closed?


# ---------------------------------------------------------------------------
# --doctor — user-facing diagnostic.
# ---------------------------------------------------------------------------


def cli_doctor() -> int:
    """Print listener health and settings.json install state.

    Returns a process exit code:
      0  everything OK (listener reachable + hook installed)
      1  any check failed (use stdout to tell the user what)
    """
    exit_code = 0
    port = _read_port()

    # ── Check 1: is the listener reachable? ──────────────────────────────
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/health",
            timeout=2.0,
        ) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        print(f"OK   listener on 127.0.0.1:{port}")
        print(f"     uptime: {data.get('uptime_s', '?')}s")
        print(f"     recent events: {data.get('recent_event_count', 0)}")
        if data.get("last_event_name"):
            print(f"     last event: {data['last_event_name']}")
            print(f"     last event at: {data.get('last_event_at', '?')}")
    except (urllib.error.URLError, ConnectionRefusedError, OSError, TimeoutError):
        print(f"FAIL no listener on 127.0.0.1:{port}")
        print(f"     → Is claude-island running?")
        exit_code = 1

    # ── Check 2: is the hook installed in ~/.claude/settings.json? ───────
    try:
        settings = Path.home() / ".claude" / "settings.json"
    except RuntimeError:
        print("FAIL cannot resolve home directory")
        return 1
    if not settings.exists():
        print(f"WARN {settings} does not exist")
        print(f"     → Run claude-island once to create + install hooks")
        exit_code = 1
    else:
        try:
            text = settings.read_text(encoding="utf-8")
            data = json.loads(text)
        except (OSError, json.JSONDecodeError) as e:
            print(f"FAIL could not read {settings}: {e!r}")
            exit_code = 1
        else:
            if _contains_our_hook(data):
                print(f"OK   hook present in {settings}")
            else:
                print(f"FAIL hook NOT present in {settings}")
                print(f"     → Run claude-island once to install")
                exit_code = 1

    # ── Check 3: does ~/.claude-island/hook.py exist? ────────────────────
    home_hook = Path.home() / ".claude-island" / "hook.py"
    if home_hook.exists():
        print(f"OK   hook script at {home_hook}")
    else:
        print(f"FAIL hook script missing at {home_hook}")
        print(f"     → Run claude-island once to copy the hook script")
        exit_code = 1

    return exit_code


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build_jump_target() -> dict | None:
    """Capture terminal-identifying metadata from the hook's environment.

    Runs INSIDE claude.exe's subprocess tree → sees env vars and console
    state that an external probe (claude-island main process) cannot
    read. Specifically valuable:

      * ``WT_SESSION`` — Windows Terminal's per-pane GUID (WT 1.18+).
        Stable across title drift; closest thing to a unique pane id.
      * ``TERM_PROGRAM`` — terminal app identifier ("WindowsTerminal",
        "vscode", "iTerm.app", etc.). Drives adapter dispatch.
      * ``GetConsoleWindow()`` — the conhost hwnd for our process.
        Same value claude-island would get via AttachConsole(pid) +
        GetConsoleWindow() later, but capturing here saves the
        50ms AttachConsole at click time.
      * ``GetCurrentProcessId()`` — sanity check value, matches the
        pid the listener already knows from the wire payload.

    Returns a dict suitable for the wire (str/int only), or None if
    capture failed entirely. Sub-fields default to empty/zero rather
    than missing so the listener's parse is simpler.

    Pure stdlib (ctypes + os). No psutil — keeps hook.py cold-start
    fast (target <100ms).
    """
    out: dict = {
        "term_program": "",
        "terminal_app": "",
        "wt_session_guid": "",
        "conhost_hwnd": 0,
        "host_pid": 0,
    }
    try:
        out["term_program"] = os.environ.get("TERM_PROGRAM", "") or ""
        out["wt_session_guid"] = os.environ.get("WT_SESSION", "") or ""
        # Derive a normalized terminal_app from the env signals. Order
        # mirrors open-vibe-island's inferTerminalApp priority: explicit
        # WT_SESSION wins, then TERM_PROGRAM, then catch-alls.
        if out["wt_session_guid"]:
            out["terminal_app"] = "WindowsTerminal"
        elif out["term_program"]:
            out["terminal_app"] = out["term_program"]
        else:
            out["terminal_app"] = "ConsoleHost"
    except Exception:
        pass

    # Win32 calls via ctypes — stay stdlib-only.
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        user32 = ctypes.windll.user32
        # GetConsoleWindow returns a HANDLE; coerce to int hwnd.
        conhost = int(kernel32.GetConsoleWindow() or 0)
        out["conhost_hwnd"] = conhost
        out["host_pid"] = int(kernel32.GetCurrentProcessId() or 0)
    except Exception:
        # ctypes not available (extremely rare on Windows) or call
        # failed — leave conhost_hwnd=0 / host_pid=0. The listener
        # interprets 0 as "not captured".
        pass

    # Don't emit a fully-empty record — it adds wire weight without info.
    if not any(out.values()):
        return None
    return out


def _try_set_console_sentinel(session_id: str) -> None:
    """Set our ``ci:{uuid32}`` sentinel as the console title via direct
    SetConsoleTitleW in the hook process.

    Why in-process: this Python subprocess shares its console with the
    parent claude.exe on Windows (Claude Code spawns hooks without
    CREATE_NEW_CONSOLE), so SetConsoleTitleW modifies the SAME conhost
    that hosts Claude. WT mirrors conhost title into TabItem.Name (for
    profiles without ``suppressApplicationTitle``), giving click-to-tab
    the unique identifier it needs.

    Sentinel format MUST match ``platform_/wt_session_title.py``:
    ``ci:{uuid_no_dashes_first_32}``. Tail-trim is identical to the
    listener-side reconcile logic — the two must produce byte-identical
    strings or UIA equality breaks.

    Best-effort: any failure (ctypes import, syscall error,
    non-Windows) silently swallowed. Hook must never exit non-zero.
    """
    try:
        # Normalize uuid: dashes-out, lower-case, truncate to 32 chars.
        # Mirror of platform_/wt_session_title.sentinel_title.
        clean = session_id.replace("-", "").lower()[:32]
        if not clean:
            return
        sentinel = f"ci:{clean}"
        import ctypes
        kernel32 = ctypes.windll.kernel32
        kernel32.SetConsoleTitleW.argtypes = [ctypes.c_wchar_p]
        kernel32.SetConsoleTitleW.restype = ctypes.c_int
        kernel32.SetConsoleTitleW(sentinel)
    except Exception:
        # No raise — hook contract is fail-open.
        pass


def _read_port() -> int:
    """Read the listener port from ~/.claude-island/port.txt.
    Falls back to ``_DEFAULT_PORT`` on any error (file missing, malformed,
    Path.home() raising, etc.)."""
    try:
        text = _port_file().read_text(encoding="utf-8").strip()
        port = int(text)
        if 1 <= port <= 65535:
            return port
    except (OSError, ValueError, RuntimeError):
        pass
    return _DEFAULT_PORT


def _contains_our_hook(settings_root: dict) -> bool:
    """Walk the hooks tree looking for any command string that points
    at our hook script. Match by the substring ``.claude-island`` +
    ``hook.py`` because the absolute path differs per OS / venv.
    """
    if not isinstance(settings_root, dict):
        return False
    hooks = settings_root.get("hooks")
    if not isinstance(hooks, dict):
        return False
    for groups in hooks.values():
        if not isinstance(groups, list):
            continue
        for group in groups:
            if not isinstance(group, dict):
                continue
            inner = group.get("hooks")
            if not isinstance(inner, list):
                continue
            for hook in inner:
                if not isinstance(hook, dict):
                    continue
                cmd = hook.get("command")
                if not isinstance(cmd, str):
                    continue
                lower = cmd.lower().replace("\\", "/")
                if ".claude-island/hook.py" in lower or "claude-island/hook.py" in lower:
                    return True
    return False


def _stderr(msg: str) -> None:
    """Best-effort stderr write. Never raises."""
    try:
        sys.stderr.write(f"[claude-island hook] {msg}\n")
        sys.stderr.flush()
    except Exception:
        pass


if __name__ == "__main__":
    main()
