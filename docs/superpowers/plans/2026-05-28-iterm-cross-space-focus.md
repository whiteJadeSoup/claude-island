# iTerm Cross-Space Focus Fix Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make clicking a session whose iTerm2 window lives on a different macOS Space actually switch to that Space, instead of silently doing nothing.

**Architecture:** The iTerm2 focus AppleScript currently does `set frontmost of process` + `select w/t/s` + `set index of w to 1`. None of those switch macOS Spaces — `select w` only reorders iTerm's *internal* window list (see the stale `I-5 cross-Space hint` comment at `_iterm_fast_path.py:189`). The fix adds one additive step to all four focus-script bodies: after selecting the window, capture its title (`name of w`) and raise it through the Accessibility API (`System Events … perform action "AXRaise"`), which *does* pull the window's Space to the foreground. The step is `try`-guarded so any failure degrades to today's behaviour — the change can only help, never regress.

**Tech Stack:** Python 3.13, PySide6, AppleScript via `osascript` (subprocess) and `NSAppleScript` (PyObjC fast path), pytest.

---

## Background / Confirmed Root Cause

- The bug is cross-Space only: a session whose iTerm window is on a non-visible Space of a display → clicking the row does nothing visible.
- Proven by a natural experiment: when the window was on another Space → no response; after it moved back to the current Space → works.
- All four focus-script bodies share the same gap. Two live in `_iterm_fast_path.py` (compiled `NSAppleScript`, run on the worker thread); two live in `iterm2.py` (subprocess `osascript`, the legacy fallback). All four must be fixed or the bug persists on whichever path runs.
- Verified fact the fix depends on: iTerm's scriptable `name of w` matches System Events' window title **exactly**, including the leading status glyph (e.g. `✳ Discussion on business and making money`). So `first window whose name is winName` is a valid exact match. (Confirmed live 2026-05-28.)

## Scope

**In scope:** iTerm2 adapter focus (both fast path and subprocess fallback).

**Non-goals:**
- Terminal.app (`terminal_app.py`) — same class of bug may exist but the reporting user runs iTerm2; track separately if needed.
- Windows Terminal — Spaces are a macOS concept; not applicable.
- Automated CI verification of the actual Space switch — requires a real multi-Space display, which CI lacks. Space-switch correctness is verified by the **manual** procedure in Task 4. The automated tests pin the *script content* (that the AXRaise step is present, correctly targeted, and `try`-guarded).

## File Structure

| File | Responsibility | Change |
|------|----------------|--------|
| `claude_island/platform_/terminals/_iterm_fast_path.py` | Compiled-once `NSAppleScript` focus subroutines (`focusByID`, `focusByTTY`) run on the worker thread | Add AXRaise step to `_FOCUS_BY_ID_SOURCE` and `_FOCUS_BY_TTY_SOURCE` |
| `claude_island/platform_/terminals/iterm2.py` | Subprocess `osascript` focus templates (legacy fallback when PyObjC unavailable / fast-path returns False) | Add AXRaise step to `_FOCUS_SCRIPT_TEMPLATE` and `_FOCUS_SCRIPT_BY_ID_TEMPLATE` |
| `tests/platform_/test_iterm2_adapter.py` | Content tests for the subprocess templates | New `TestFocusScriptSwitchesSpace` class |
| `tests/platform_/test_iterm2_fast_path.py` | Content tests for the compiled fast-path sources | New `TestFocusSourceSwitchesSpace` class |
| `docs/superpowers/plans/2026-05-28-iterm-cross-space-focus.md` | This plan | — |

## The AXRaise step (the single conceptual change, applied 4×)

Inside each matched branch (`if <match> then … return "ok"`), two additions:

1. As the **first** line of the branch: `set winName to name of w` (capture the title while `w` is in scope).
2. **Immediately before** `return "ok"` (after the existing `select`/`index` block): a `System Events` block that AXRaises the window by that title, targeting the same iTerm host pid the script already resolved, wrapped in `try`.

For the **fast-path subroutines** (`_iterm_fast_path.py`), the host pid is the subroutine arg `hostPID`:

```applescript
                                    tell application "System Events"
                                        try
                                            tell (first process whose unix id is (hostPID as integer))
                                                perform action "AXRaise" of (first window whose name is winName)
                                            end tell
                                        end try
                                    end tell
```

For the **subprocess templates** (`iterm2.py`), the host pid is the Python format field `{host_pid}`:

```applescript
                            tell application "System Events"
                                try
                                    tell (first process whose unix id is {host_pid})
                                        perform action "AXRaise" of (first window whose name is winName)
                                    end tell
                                end try
                            end tell
```

(The added text contains no `{`/`}` other than `{host_pid}`, so `.format()` on the templates and `.format(timeout=…)` on the sources remain valid.)

---

### Task 1: AXRaise in the subprocess osascript templates (`iterm2.py`)

**Files:**
- Modify: `claude_island/platform_/terminals/iterm2.py` (`_FOCUS_SCRIPT_TEMPLATE` ~lines 159-208, `_FOCUS_SCRIPT_BY_ID_TEMPLATE` ~lines 223-266)
- Test: `tests/platform_/test_iterm2_adapter.py`

- [ ] **Step 1: Write the failing content tests**

Append this class to `tests/platform_/test_iterm2_adapter.py` (the file already imports `_FOCUS_SCRIPT_TEMPLATE` and `_FOCUS_SCRIPT_BY_ID_TEMPLATE` at lines 23-24):

```python
class TestFocusScriptSwitchesSpace:
    """Cross-Space regression: clicking a session whose iTerm window is
    on another macOS Space did nothing — ``select w`` only reorders
    iTerm's internal window list, it does NOT switch Spaces. The script
    must AXRaise the target window (matched by its title in System
    Events) to pull its Space to the foreground. ``try``-guarded so a
    title mismatch / AX error degrades to the prior behaviour."""

    def _assert_axraise(self, script: str, host_pid: int) -> None:
        # Title captured from the iTerm window before any mutation.
        assert "set winName to name of w" in script
        # AXRaise targets the resolved host pid by unix id (multi-iTerm
        # correctness) and matches the window by the captured title.
        assert "unix id is {}".format(host_pid) in script
        assert 'perform action "AXRaise" of (first window whose name is winName)' in script
        # Ordering: title captured, window selected, THEN raised.
        i_name = script.index("set winName to name of w")
        i_select_w = script.index("select w")
        i_raise = script.index('perform action "AXRaise"')
        assert i_name < i_select_w < i_raise, (
            "must capture title, select window, then AXRaise; "
            "got name={} select_w={} raise={}".format(i_name, i_select_w, i_raise)
        )
        # Graceful degradation: AXRaise is inside a try block that
        # closes before the success return.
        i_end_try = script.index("end try", i_raise)
        i_ok = script.index('return "ok"', i_raise)
        assert i_end_try < i_ok, "AXRaise must be try-guarded before return ok"

    def test_tty_template_axraises_after_select(self):
        script = _FOCUS_SCRIPT_TEMPLATE.format(host_pid=42, tty="/dev/ttys004")
        self._assert_axraise(script, 42)

    def test_id_template_axraises_after_select(self):
        script = _FOCUS_SCRIPT_BY_ID_TEMPLATE.format(host_pid=42, session_id="ABC-123")
        self._assert_axraise(script, 42)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/platform_/test_iterm2_adapter.py::TestFocusScriptSwitchesSpace -v`
Expected: FAIL — `AssertionError` on `"set winName to name of w" in script` (the step doesn't exist yet).

- [ ] **Step 3: Edit `_FOCUS_SCRIPT_TEMPLATE`**

In `claude_island/platform_/terminals/iterm2.py`, find the matched branch in `_FOCUS_SCRIPT_TEMPLATE`:

```applescript
                        if tty of s is "{tty}" then
                            -- Guarded mutators: skip the no-op
```

Change it to insert the title capture as the first line of the branch:

```applescript
                        if tty of s is "{tty}" then
                            set winName to name of w
                            -- Guarded mutators: skip the no-op
```

Then find the end of that branch:

```applescript
                            if index of w is not 1 then
                                set index of w to 1
                            end if
                            return "ok"
                        end if
```

Change it to insert the AXRaise block before `return "ok"`:

```applescript
                            if index of w is not 1 then
                                set index of w to 1
                            end if
                            -- Cross-Space: select w only reorders iTerm's
                            -- internal window list; it does NOT switch the
                            -- macOS Space. When the target window is on a
                            -- different Space, raise it via the Accessibility
                            -- API, which DOES pull its Space to the front.
                            -- try-guarded: a title mismatch / AX error
                            -- degrades to the prior no-switch behaviour.
                            tell application "System Events"
                                try
                                    tell (first process whose unix id is {host_pid})
                                        perform action "AXRaise" of (first window whose name is winName)
                                    end tell
                                end try
                            end tell
                            return "ok"
                        end if
```

- [ ] **Step 4: Edit `_FOCUS_SCRIPT_BY_ID_TEMPLATE`**

In the same file, find the matched branch in `_FOCUS_SCRIPT_BY_ID_TEMPLATE`:

```applescript
                        if (id of s as text) is "{session_id}" then
                            -- Guarded mutators: skip the no-op
```

Change it to:

```applescript
                        if (id of s as text) is "{session_id}" then
                            set winName to name of w
                            -- Guarded mutators: skip the no-op
```

Then find its branch end:

```applescript
                            if index of w is not 1 then
                                set index of w to 1
                            end if
                            return "ok"
                        end if
```

Change it to:

```applescript
                            if index of w is not 1 then
                                set index of w to 1
                            end if
                            -- Cross-Space: see _FOCUS_SCRIPT_TEMPLATE. AXRaise
                            -- the matched window so a session on another macOS
                            -- Space is actually surfaced. try-guarded so any
                            -- failure degrades to the prior no-switch path.
                            tell application "System Events"
                                try
                                    tell (first process whose unix id is {host_pid})
                                        perform action "AXRaise" of (first window whose name is winName)
                                    end tell
                                end try
                            end tell
                            return "ok"
                        end if
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/platform_/test_iterm2_adapter.py::TestFocusScriptSwitchesSpace -v`
Expected: PASS (2 passed).

- [ ] **Step 6: Run the full adapter suite to confirm no regression**

Run: `.venv/bin/python -m pytest tests/platform_/test_iterm2_adapter.py -q`
Expected: PASS (all existing tests still green — the existing `select w/t/s` ordering and deminiaturize tests are unaffected since the new lines are additive and placed after them).

- [ ] **Step 7: Commit**

```bash
git add claude_island/platform_/terminals/iterm2.py tests/platform_/test_iterm2_adapter.py
git commit -m "$(cat <<'EOF'
fix(iterm2): AXRaise target window in subprocess focus templates

select w only reorders iTerm's internal window list; it does not
switch macOS Spaces, so clicking a session whose window is on another
Space did nothing. AXRaise the matched window (by title, via System
Events) to pull its Space forward. try-guarded — degrades to prior
behaviour on any failure.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 2: AXRaise in the compiled fast-path sources (`_iterm_fast_path.py`)

**Files:**
- Modify: `claude_island/platform_/terminals/_iterm_fast_path.py` (`_FOCUS_BY_ID_SOURCE` ~lines 135-213, `_FOCUS_BY_TTY_SOURCE` ~lines 215-261)
- Test: `tests/platform_/test_iterm2_fast_path.py`

- [ ] **Step 1: Write the failing content tests**

Append this class to `tests/platform_/test_iterm2_fast_path.py`. Add the import of the two source constants at the top of the file if not already present (`from claude_island.platform_.terminals._iterm_fast_path import _FOCUS_BY_ID_SOURCE, _FOCUS_BY_TTY_SOURCE`):

```python
from claude_island.platform_.terminals._iterm_fast_path import (
    _FOCUS_BY_ID_SOURCE,
    _FOCUS_BY_TTY_SOURCE,
)


class TestFocusSourceSwitchesSpace:
    """The compiled fast-path subroutines must AXRaise the matched
    window so a session on another macOS Space is actually surfaced.
    select w alone only reorders iTerm's internal window list. The
    subroutines resolve the iTerm host via the ``hostPID`` argument, so
    AXRaise targets ``unix id is (hostPID as integer)``. try-guarded for
    graceful degradation (mirrors the subprocess templates in iterm2.py)."""

    def _assert_axraise(self, source: str) -> None:
        assert "set winName to name of w" in source
        assert "unix id is (hostPID as integer)" in source
        assert 'perform action "AXRaise" of (first window whose name is winName)' in source
        i_name = source.index("set winName to name of w")
        i_select_w = source.index("select w")
        i_raise = source.index('perform action "AXRaise"')
        assert i_name < i_select_w < i_raise
        i_end_try = source.index("end try", i_raise)
        i_ok = source.index('return "ok"', i_raise)
        assert i_end_try < i_ok, "AXRaise must be try-guarded before return ok"

    def test_tty_source_axraises_after_select(self):
        self._assert_axraise(_FOCUS_BY_TTY_SOURCE)

    def test_id_source_axraises_after_select(self):
        self._assert_axraise(_FOCUS_BY_ID_SOURCE)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/platform_/test_iterm2_fast_path.py::TestFocusSourceSwitchesSpace -v`
Expected: FAIL — `AssertionError` on `"set winName to name of w" in source`.

- [ ] **Step 3: Edit `_FOCUS_BY_TTY_SOURCE`**

In `claude_island/platform_/terminals/_iterm_fast_path.py`, find the matched branch in `_FOCUS_BY_TTY_SOURCE`:

```applescript
                                if (tty of s) is targetTTY then
                                    -- Guarded mutators (see focusByID)
```

Change it to:

```applescript
                                if (tty of s) is targetTTY then
                                    set winName to name of w
                                    -- Guarded mutators (see focusByID)
```

Then find that branch's end:

```applescript
                                    if index of w is not 1 then
                                        set index of w to 1
                                    end if
                                    return "ok"
                                end if
```

Change it to:

```applescript
                                    if index of w is not 1 then
                                        set index of w to 1
                                    end if
                                    -- Cross-Space: select w only reorders
                                    -- iTerm's internal window list; AXRaise
                                    -- pulls the target window's macOS Space
                                    -- to the front. try-guarded so a title
                                    -- mismatch / AX error degrades to the
                                    -- prior no-Space-switch behaviour.
                                    tell application "System Events"
                                        try
                                            tell (first process whose unix id is (hostPID as integer))
                                                perform action "AXRaise" of (first window whose name is winName)
                                            end tell
                                        end try
                                    end tell
                                    return "ok"
                                end if
```

- [ ] **Step 4: Edit `_FOCUS_BY_ID_SOURCE`**

In the same file, find the matched branch in `_FOCUS_BY_ID_SOURCE`:

```applescript
                                if (id of s as text) is sessionID then
                                    -- All three mutators are guarded
```

Change it to:

```applescript
                                if (id of s as text) is sessionID then
                                    set winName to name of w
                                    -- All three mutators are guarded
```

Then find that branch's end:

```applescript
                                    if index of w is not 1 then
                                        set index of w to 1
                                    end if
                                    return "ok"
                                end if
```

Change it to:

```applescript
                                    if index of w is not 1 then
                                        set index of w to 1
                                    end if
                                    -- Cross-Space: see focusByTTY. AXRaise the
                                    -- matched window so a session on another
                                    -- macOS Space is surfaced. try-guarded.
                                    tell application "System Events"
                                        try
                                            tell (first process whose unix id is (hostPID as integer))
                                                perform action "AXRaise" of (first window whose name is winName)
                                            end tell
                                        end try
                                    end tell
                                    return "ok"
                                end if
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/platform_/test_iterm2_fast_path.py::TestFocusSourceSwitchesSpace -v`
Expected: PASS (2 passed).

- [ ] **Step 6: Run the full fast-path + cache + worker suites to confirm no regression**

Run: `.venv/bin/python -m pytest tests/platform_/test_iterm2_fast_path.py tests/platform_/test_iterm2_apple_script_cache.py tests/platform_/test_iterm2_focus_worker.py -q`
Expected: PASS. (The cache compiles these sources via `NSAppleScript` only on macOS with PyObjC; on a dev machine without it the compile path is skipped/mocked. The added AppleScript is syntactically valid — nested `tell application "System Events"` inside `tell application "iTerm"` with a `try` is legal AppleScript.)

- [ ] **Step 7: Commit**

```bash
git add claude_island/platform_/terminals/_iterm_fast_path.py tests/platform_/test_iterm2_fast_path.py
git commit -m "$(cat <<'EOF'
fix(iterm2): AXRaise target window in fast-path focus subroutines

Mirror the subprocess-template fix in the compiled NSAppleScript
focusByTTY/focusByID subroutines so the cross-Space switch works on
the fast path too. Replaces the stale "set index of w to 1" cross-Space
hint, which only reordered iTerm's internal window list.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 3: Full regression + architecture lint

**Files:** none (verification only).

- [ ] **Step 1: Run the full platform terminal suite**

Run: `.venv/bin/python -m pytest tests/platform_/ -q`
Expected: PASS (all green).

- [ ] **Step 2: Run the architecture import-linter**

Run: `.venv/bin/python -m import_linter`
Expected: PASS — contracts kept. (No imports changed; this is a sanity gate per CLAUDE.md.)

- [ ] **Step 3: Confirm the stale comment is gone or accurate**

Verify `_iterm_fast_path.py` no longer claims `set index of w to 1` is the cross-Space mechanism. The `I-5 cross-Space hint` comment (~line 189) described `set index of w to 1` as the cross-Space handler — now superseded by AXRaise. Run:

Run: `grep -n "cross-Space" claude_island/platform_/terminals/_iterm_fast_path.py`
Expected: the only `cross-Space` references are the new AXRaise comments. If the old `I-5 cross-Space hint` text on the `set index` block still implies it switches Spaces, edit that comment to: `-- I-5: set index when it would change something; this orders the window inside iTerm but does NOT switch Spaces — see the AXRaise step below for the Space switch.` Then re-run Step 1, and commit:

```bash
git add claude_island/platform_/terminals/_iterm_fast_path.py
git commit -m "docs(iterm2): correct stale I-5 cross-Space comment (set index doesn't switch Spaces)"
```

(If the comment is already accurate after Task 2, skip the commit.)

---

### Task 4: Manual end-to-end Space-switch validation

**Files:**
- Create (temporary, not committed): `/tmp/space_probe.py`

This is the acceptance gate that automated tests cannot cover (CI has no multi-Space display). Run it once a session's iTerm window is on a different Space than the one you're viewing.

- [ ] **Step 1: Write the active-Space probe**

```python
# /tmp/space_probe.py — prints the current active macOS Space id
import ctypes
cg = ctypes.CDLL('/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics')
cg.CGSMainConnectionID.restype = ctypes.c_int
cg.CGSGetActiveSpace.restype = ctypes.c_uint64
cg.CGSGetActiveSpace.argtypes = [ctypes.c_int]
print(cg.CGSGetActiveSpace(cg.CGSMainConnectionID()))
```

- [ ] **Step 2: Stage a cross-Space scenario**

Move one Claude session's iTerm window to a different Space (drag it in Mission Control to another desktop, OR make that iTerm window fullscreen so it gets its own Space), then switch back to your main Space so that window is NOT visible.

- [ ] **Step 3: Restart Claude Island so it runs the patched scripts**

The running instance (pid from `ps aux | grep claude_island`) holds the pre-fix compiled `NSAppleScript`. Restart it however it was launched (e.g. `uv run claude_island`) so the fast-path cache recompiles the patched sources.

- [ ] **Step 4: Measure, click, measure**

```bash
echo "before: $(.venv/bin/python /tmp/space_probe.py)"
# now click the off-Space session's row in the Island panel
echo "after:  $(.venv/bin/python /tmp/space_probe.py)"
```

Expected: the active-Space id **changes** between before and after, and visually iTerm switches to the off-Space window's desktop with the correct pane focused.
Pass criteria: id changed AND the target session's terminal is now frontmost and visible. If the id did not change, AXRaise did not switch the Space on this macOS build — STOP and revisit the approach (do not ship); capture `Console.app` / stderr for `NSAppleScript error` or `AXRaise` permission (`errno -1743`).

- [ ] **Step 5: Regression — same-Space still works**

Click a session whose window is on your current Space. Expected: it focuses as before (no regression; AXRaise of an on-Space window is a no-op Space-wise and just raises it).

- [ ] **Step 6: Clean up**

```bash
rm -f /tmp/space_probe.py
```

---

## Self-Review Notes

- **Spec coverage:** all four focus-script bodies are patched (Tasks 1-2); regression + lint (Task 3); the un-automatable Space-switch is covered by manual validation (Task 4). Stale `I-5` comment addressed (Task 3 Step 3).
- **Type/identifier consistency:** the AppleScript variable is `winName` in all four bodies; the match line differs per body (`tty of s is …` / `(id of s as text) is …`); the host-pid reference is `(hostPID as integer)` in the fast-path subroutines and `{host_pid}` in the subprocess templates — matching each body's existing `set frontmost` line.
- **Graceful degradation:** every AXRaise is inside `try … end try`; on any failure the script still reaches `return "ok"`, so behaviour is never worse than today.
- **No placeholders:** every edit shows exact before/after AppleScript and every test step shows full code + the exact pytest command and expected result.
