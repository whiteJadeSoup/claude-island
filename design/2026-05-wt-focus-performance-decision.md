# Windows Terminal Focus Performance — Decision

> Following the Overview Design review at
> `2026-05-wt-focus-performance-review.md`.

---

## TL;DR

**Decision: implement, with all blocking + high-value question findings folded into the design.**

Caveat: **the implementation cannot be live-validated from this development machine (macOS)**. Unit + integration tests will run with mocks; real COM apartment behavior, AttachConsole side effects, and end-to-end timing under a real Windows Terminal must be verified by a Windows tester before merge.

---

## Reasoning

### Why implement (not defer)

1. **Architecture parity matters.** Windows users feel the same ~150 ms hitch macOS users felt before commit `e0b57f0`. Shipping macOS-only widens the platform gap that the project's `Three-Layer Architecture` is meant to avoid.

2. **The new finding B-001 (cache shortcut) is itself worth the work.** Even without the async split, using `_wt_hwnd_cache` saves ~50 ms (AttachConsole) on the no-prehook cohort. That's a real win we can ship.

3. **The blocking finding C-001 has a known fix.** `pythoncom.CoInitializeEx(COINIT_APARTMENTTHREADED)` in worker init is one line. The risk it raises is "behavior unverified on Windows" rather than "we don't know what to do" — that's a verification problem, not a design problem.

4. **macOS implementation is the proof-of-concept.** The pattern works; the question is whether Windows ports cleanly. Worst case, the Windows port falls back to legacy on every click and we ship a no-op change.

### Why NOT defer

1. The cohort distribution (S-2 / A-001) won't get quantified without shipping something. We'd be deferring forever.
2. The review's other questions (D-1 through D-5) need a real Windows session to answer; documents alone can't resolve them.

### What I cannot do

1. **Verify COM apartment behavior live.** Mock tests will pass on macOS; the real `pythoncom.CoInitialize` call only matters at runtime on Windows.
2. **Measure actual G1 / G2 latency on real WT.** All my numbers are inherited from the design doc's measurements (presumably done elsewhere).
3. **Detect AttachConsole stderr contamination.** Same reason.

### What I will do

1. Implement the architecture exactly as designed (with revisions from review).
2. Write tests that exercise every branch with mocks, including the COM init path.
3. Document every assumption that needs Windows verification with `# TODO(windows-verify): ...` comments.
4. Tell the user explicitly which assertions are believed-true vs verified.

---

## Resolved findings from review (folded into implementation)

| ID | Severity | Resolution |
|---|---|---|
| C-001 | blocking → resolved | Worker init calls `pythoncom.CoInitializeEx(COINIT_APARTMENTTHREADED)`. `wt_uia.py` docstring updated to document the threading contract. Integration test mocks pythoncom to assert it's called. |
| B-001 | question → adopted | `focus()` checks `_wt_hwnd_cache.get(pid)` after prehook miss; fast-path covers both cohorts. |
| B-007 | question → adopted | Reuse exact backlog policy from `_iterm_fast_path.FocusWorker` (WARN=4, REJECT=10, 60s log throttle). |
| C-006 | question → adopted | `__main__.py` calls `wt_focus_worker.get_worker()` at app start to pre-warm pool + COM init. |
| C-004 | question → adopted | `walk_to_visible_host` result gets `GetClassName` validation before SetForegroundWindow. |
| A-002 | nit → adopted | G2 verification language fixed to use `SetWinEventHook(EVENT_SYSTEM_FOREGROUND, ...)`. |
| A-004 / A-005 | question → adopted | G1 split into G1a (warm ≤10 ms) + G1b (cold ≤25 ms). G2 cohort = prehook OR cache hit. |

## Deferred to operational follow-up

| ID | Action |
|---|---|
| A-001 (S-2) | Quantify cohort distribution after shipping. Add lightweight telemetry counter (`fast_path_taken`, `legacy_taken`). |
| D-1 (C-003) | Validate UIA Select() interaction with foreground lock on real WT. If problematic, revert to legacy chain order. |
| D-2 (C-005) | Per-call UIA deadline. Defer until real-world hang observed. |
| D-3 (C-002) | Audit stderr writes during AttachConsole window. Pre-existing risk; track separately. |
| D-4 (B-004) | UIA element caching measurement. Only if G2 fails empirically. |
| A-003 / N-1-3 | Documentation polish. Will fold into commit message but don't gate. |

## Scope

This change ships:
- `claude_island/platform_/terminals/_wt_fast_path.py` — new module
- `claude_island/platform_/terminals/windows_terminal.py` — focus() integration
- `claude_island/__main__.py` — pre-warm hook
- 3 new test files mirroring the macOS layout
- This decision doc + the design doc + the review doc

This change does NOT ship:
- Telemetry counters (A-001 follow-up)
- UIA element caching (B-004 follow-up)
- Live Windows validation (out of my reach)
