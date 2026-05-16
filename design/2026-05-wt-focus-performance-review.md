# Windows Terminal Focus Performance — Overview Design Review

> Multi-agent review of `2026-05-wt-focus-performance.md` per
> `instructions/design-review.md`. Three subagents in parallel:
> Agent A (Problem), Agent B (Direction), Agent C (Reality).

---

## Summary

| Severity | Count | Net (after dedup + conflict resolution) |
|---|---|---|
| `blocking` | 3 raw (A:1, C:2) | **1** (C-001 COM apartment) |
| `question` | 12 raw | 8 after dedup |
| `suggestion` | 4 raw | 4 |
| `nit` | 4 raw | 3 |

**Conclusion**: `Request Changes` — one true blocking issue (C-001) on COM threading
that must be addressed before implementation. Several high-value `question` findings
warrant scope expansion (notably B-001: leveraging existing `_wt_hwnd_cache`).
Suggestion-grade items are noted for follow-up.

---

## Blocking findings (consolidated)

### B-1. COM apartment cross-thread sharing (C-001, related to B-003)

**What**: Three threads in the new design will touch `uiautomation`:
(a) ProcessScanner via `collect_wt_tab_titles` (already exists today),
(b) Qt main thread via legacy fallback (`select_tab_by_title`),
(c) the new FocusWorker thread.

`uiautomation` caches `IUIAutomation` in a module-level singleton
(`auto._AutomationClient`). Whichever thread first imports/uses it
"wins" the apartment of that pointer. Subsequent calls from another
thread may see `RPC_E_WRONG_THREAD` / `RPC_E_CHANGED_MODE`.

**Why it's blocking**: The macOS sibling has a clean documented contract
(NSAppleScript is thread-safe if a single thread owns the instance).
UIA has no such contract; behaviour depends on `sys.coinit_flags` and
on which thread happens to import `uiautomation` first.

**Resolution required before implementation**:

1. Worker thread must explicitly call `pythoncom.CoInitializeEx(pythoncom.COINIT_APARTMENTTHREADED)` (i.e. STA) before first UIA call.
2. Add an integration test that imports `uiautomation` on thread A,
   then calls UIA from thread B (the worker), and asserts no exception.
3. Add a comment to `wt_uia.py` documenting the threading contract:
   "All UIA calls into this module must originate from a thread that
   has called `pythoncom.CoInitializeEx(COINIT_APARTMENTTHREADED)` and
   serialised by the caller. The QThreadPool with maxThreadCount=1
   provides that serialisation."

---

## High-value question findings (addressed in this re-design)

### Q-1. Extend fast-path to use `_wt_hwnd_cache` (B-001)

**Finding**: `WindowsTerminalAdapter._wt_hwnd_cache` already maps live
WT pids to wt_hwnd, populated by `group()` on each snapshotter tick.
The original design ignored it and required `prehook_conhost_hwnd`
exclusively. A session whose hook didn't capture conhost can still
have a cached wt_hwnd from a previous `group()` pass — looking it up
is ~1 µs.

**Resolution**: Update Solution Design step `①`:
> `resolve wt_hwnd: prehook_conhost_hwnd OR adapter._wt_hwnd_cache.get(pid) OR fall back to legacy.`

Impact: covers more clicks with the fast path. The cache may be stale
on rapid tab-drag, but `_force_foreground` already validates via
`IsWindow`, and a stale hwnd just falls through to legacy — same
shape as the prehook-staleness story already in Type B.

### Q-2. Goal G1: budget headroom + cohort cold/warm (A-004, C-006)

**Finding**: G1 ≤ 10 ms is tight against measured cold-first
costs (~11 ms cold). Pool spawn overhead (~5–15 ms on first click)
not budgeted. No headroom.

**Resolution**: Restate G1 with separate cold/warm targets:
- **G1a (warm)**: ≤ 10 ms p95, after first click
- **G1b (cold first click)**: ≤ 25 ms p99 (allows for pool spawn,
  first PyObjC-equivalent module load)

Also: **pre-warm the worker pool at app startup** by submitting a
no-op task during `__main__.py` initialisation. Removes first-click
penalty.

### Q-3. Goal G2: cohort scoping (A-005)

**Finding**: G2 says "p95 ≤ 30 ms" but doesn't define population.
If population is "all clicks" the legacy-fallback cohort dominates
the tail and G2 is unachievable. If population is "prehook-hit
cohort", it should be stated.

**Resolution**: G2 cohort = `(prehook_conhost_hwnd > 0) OR (_wt_hwnd_cache hit)`.
Legacy cohort is explicitly excluded; tracked separately as
"unchanged from current".

### Q-4. Backlog policy (B-007)

**Finding**: Worker queue management was omitted. macOS adopted
WARN=4 / REJECT=10 / 60s log throttle.

**Resolution**: Adopt the same policy verbatim. Code reuse from
`_iterm_fast_path.FocusWorker` where the structure is identical.

### Q-5. G2 verification method (A-002, C-007)

**Finding**: G2 verification used macOS terminology
("NSWorkspace-equivalent") and didn't address visual-paint vs
foreground-event divergence.

**Resolution**: Replace G2 verification with concrete Win32 recipe:
- Subscribe to `EVENT_SYSTEM_FOREGROUND` from sidecar via
  `SetWinEventHook(EVENT_SYSTEM_FOREGROUND, ..., WINEVENT_OUTOFCONTEXT)`
- Record timestamp at adapter.focus() entry; record timestamp at
  matching hook callback
- p95 over N=50 trials
- Document that visual paint may follow by 16-32 ms (compositor frame),
  not in G2 budget

---

## Question findings deferred to Detail Design

### D-1. UIA Select() foreground interaction (C-003)

Detail Design must specify: measure whether `SelectionItemPattern.Select()`
triggers a second SetForegroundWindow (Win32 foreground-lock interaction).
If it does, decide between (a) accepting the brief flash and
(b) reverting to legacy chain order (foreground LAST). Test plan
required.

### D-2. UIA call timeout (C-005)

Detail Design must specify behaviour when a UIA call stalls (e.g. WT
window closed mid-call). Default COM proxy timeout is ~60 s; that
would wedge the maxThreadCount=1 pool for a minute. Options:
- `pythoncom.PumpWaitingMessages()` with watchdog
- Per-call deadline via threading.Timer + interrupt flag
- Accept and document (rare case)

### D-3. AttachConsole stderr contamination (C-002)

Detail Design must document the audit + mitigation. The risk is
pre-existing (scanner thread does AttachConsole today), but the new
design widens the window. Mitigation options:
- Audit project for `print(..., file=sys.stderr)` and replace with `log.*` (logging library redirects to file/handler we control)
- Redirect stderr to a file at startup (loses console diagnostics for non-Windows debugging)
- Accept the rare contamination risk

### D-4. UIA element caching (B-004)

Detail Design must measure whether caching `TabControl` element
between worker invocations meaningfully helps G2 (target ≤ 30 ms).
If yes, add caching layer; if no, document why caching was dismissed
(with measurement).

### D-5. walk_to_visible_host classname check (C-004)

Detail Design must specify: add `GetClassName` check to verify the
resolved hwnd is actually a WT window (CASCADIA_HOSTING_WINDOW_CLASS).
~50 µs cost; prevents wrong-window raise on hwnd recycle.

---

## Suggestions (not blocking, follow-up)

- **S-1 (A-003)**: Reorganize Goals table — G1/G2 are pain-relieving goals; G3/G4/G5 are non-regression invariants. Restructure for clarity.
- **S-2 (A-001)**: Quantify pain distribution. Currently the doc cites "up to 150 ms" cap without saying what fraction of clicks pay this. Instrument 50–100 real clicks before final implementation; classify by cohort. (Not blocking; design proceeds either way, but a baseline measurement is good engineering hygiene.)
- **S-3 (B-006)**: Strengthen Research & Comparison with PowerToys Window Walker source citations.
- **S-4 (C-009)**: Document the failure mode shift (no-op vs wrong-tab).

## Nits

- **N-1 (A-006)**: "macOS now lands ~1 ms" — source from iTerm doc S5 row (0.3 ms warm, 34 ms cold).
- **N-2 (A-007)**: Reframe "Non-Goals" that are structurally-impossible as "Known Limitations Preserved".
- **N-3 (B-008)**: Add edge flow (d) for placeholder pid + stale prehook hwnd.

---

## Conclusion

**Request Changes**.

Before proceeding to Detail Design + implementation, the Overview must
be revised to:

1. Resolve C-001 (COM apartment): explicit `pythoncom.CoInitializeEx`
   contract; integration test.
2. Adopt B-001 (`_wt_hwnd_cache` shortcut) in step `①`.
3. Adopt B-007 (backlog policy) verbatim from macOS.
4. Split G1 into cold (G1b) and warm (G1a) targets; pre-warm pool.
5. Scope G2 cohort explicitly (prehook-hit OR cache-hit only).
6. Fix G2 verification: SetWinEventHook recipe.

After revision, Detail Design can begin with D-1 through D-5 as
explicit topics.
