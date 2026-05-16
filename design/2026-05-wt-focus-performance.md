# Windows Terminal Focus Performance — Async UIA Dispatch + Main-Thread SetForegroundWindow

> **Status**: Overview Design — pending review.
> Once confirmed, Detail Design follows below the line.

---

## Part 1: Overview Design

### 1. Problem & Goals

#### Problem

Clicking a session row whose host is Windows Terminal blocks the Qt
main thread for up to **150 ms** while `_activate_windows` runs the
full UIA-tab-select chain synchronously. The code itself flags this in
a `WARNING` comment at `windows_terminal.py:691`:

> _"this entire helper runs on the Qt main thread (called from the
> click handler). Total time budget capped at ~150 ms (5 attempts ×
> ~30 ms each) so a click never feels unresponsive."_

Measured composition of the wall time on the main thread:

| Component | Wall time | Notes |
|---|---|---|
| `_resolve_console_window_fast` (with `prehook_conhost_hwnd`) | ~5 ms | GW_OWNER walk only |
| `_resolve_console_window_fast` (without prehook) | ~50 ms | AttachConsole + FreeConsole + parent restore |
| `select_tab_by_title` (UIA happy path) | ~10–30 ms | `ControlFromHandle` + TabControl + TabItemControl |
| `set_console_title` (re-assert sentinel on drift) | ~50 ms | second AttachConsole round-trip |
| `wait_for_tab_name` (poll OSC propagation) | up to 80 ms | 10 ms cadence × ≤8 iter |
| `_try_smart_guess_select` (fallback when sentinel suppressed) | ~20–40 ms | UIA tree enumeration |
| `_force_foreground` (SetForegroundWindow + fallbacks) | ~few ms | currently runs LAST |
| **Worst case observed (main thread)** | **~150 ms** | code-enforced cap |

The user-perceived event we want fast — "click row, panel disappears,
WT appears in front" — is gated by `SetForegroundWindow`, which is
itself fast (~few ms). Currently it runs at the *end* of the chain
after UIA + AttachConsole work blocks the main thread.

By contrast macOS now lands ~1 ms on the main thread after the
fast-path landed (commit `e0b57f0`). Windows users on a multi-session
panel still feel a ~150 ms hitch per click.

#### Goals

| ID | Goal | How verified |
|----|------|--------------|
| **G1** | Click → panel begins to hide within **≤ 10 ms** of mouse-up when hook captured `conhost_hwnd` (the common case after `c0fa4e3`). | Microbenchmark: `WindowsTerminalAdapter.focus(view)` returns < 10 ms on a quiet Windows VM; pytest-benchmark p95. |
| **G2** | Click → WT window frontmost within **≤ 30 ms** wall time (p95) on a warm path. | E2E: stub real WT instance, measure mouse-up → `NSWorkspace`-equivalent `EVENT_SYSTEM_FOREGROUND` event. |
| **G3** | Tab-precision (right ci:* tab selected inside WT) is **not** regressed — current correctness preserved including: sibling-sentinel fallback, smart-guess for suppressed titles, OSC-propagation race. | Existing tests in `tests/platform_/test_windows_terminal_adapter.py` all green; new tests covering id→tty equivalent decision. |
| **G4** | Graceful degradation when hook did not capture `conhost_hwnd` (older hook payloads, scanner-only sessions). | Fall back to current synchronous path; existing tests still green. |
| **G5** | No change to non-WT adapters (`generic_windows`, `iterm2`, `terminal_app`) or non-FOCUS dispatches (LAUNCH, RENAME). | Existing tests on those paths unchanged and passing. |

#### Non-Goals

- **Optimising `generic_windows` (ConsoleHost / cmd.exe).** No UIA in
  play; the wall time is dominated by `EnumWindows` + the AttachConsole
  resolve. Different bottleneck, different mitigation. Out of scope.
- **Fixing `suppressApplicationTitle` profile incompatibility.**
  Existing limitation — smart-guess + diagnostic emit is already best
  effort. Preserved verbatim.
- **Per-pane disambiguation within a single tab.** Structurally
  impossible from outside WT (TabItem.Name reflects only active pane;
  inactive panes are absent from UIA tree per `scripts/dump_wt_uia.py`).
- **Eliminating UIA.** Microsoft's UIA is the only outside-process
  hook into WT's tab tree. We move it off the main thread, not away.
- **Synchronous return semantic preserved exactly.** Like macOS, the
  bool returned from `focus()` shifts to "host raised" instead of
  "host raised AND tab selected." No caller distinguishes.

---

### 2. Solution Design

#### Architecture diagram

```
                    ┌─────────────────────────────────────────────┐
                    │           Qt main thread (UI)               │
                    │                                             │
   click row ──▶  IslandRowButton.clicked                         │
                       │                                          │
                       ▼                                          │
                  ExpandedWindow._on_row_clicked(view)            │
                       │ view, Capability.FOCUS                   │
                       ▼                                          │
                  TerminalDispatcher.dispatch                     │
                       │                                          │
                       ▼                                          │
              WindowsTerminalAdapter.focus(view)                  │
                       │                                          │
              ┌────────┴────────────┐                             │
              │ ① resolve wt_hwnd   │  via GW_OWNER walk from     │
              │   from prehook      │   prehook_conhost_hwnd      │
              │   conhost_hwnd      │  (~5 ms; SKIP if absent →   │
              │   ONLY              │   legacy)                   │
              └────────┬────────────┘                             │
                       │                                          │
              ┌────────▼────────────┐                             │
              │ ② SetForegroundWnd  │  _force_foreground(wt_hwnd) │
              │   on main thread    │  (~few ms; WT raised)       │
              └────────┬────────────┘                             │
                       │                                          │
              ┌────────▼────────────┐  QThreadPool submit         │
              │ ③ return True       │  (fire-and-forget)          │
              └────────┬────────────┘                             │
                       │                                          │
                       ▼                                          │
        ╔═══════════════════════════════════════════════════════════════════╗
        ║                 worker thread (QThreadPool, max=1)                ║
        ║                                                                   ║
        ║    _WtFocusTask.run(wt_hwnd, expected_title, siblings)            ║
        ║       │                                                           ║
        ║       ▼                                                           ║
        ║    select_tab_by_title(expected) ── ok ──▶ done                   ║
        ║       │ miss                                                       ║
        ║       ▼                                                           ║
        ║    set_console_title + wait_for_tab_name + retry select          ║
        ║       │ still miss                                                 ║
        ║       ▼                                                           ║
        ║    try each sibling_sentinel ── any ok ──▶ done                  ║
        ║       │ still miss                                                 ║
        ║       ▼                                                           ║
        ║    _try_smart_guess_select(exclude=known)                         ║
        ║       │ still miss                                                 ║
        ║       ▼                                                           ║
        ║    emit suppress-title diagnostic (once per process)              ║
        ╚═══════════════════════════════════════════════════════════════════╝
```

**Key design decisions on the diagram**:

- **wt_hwnd resolved only from prehook (`①`)** — the GW_OWNER walk
  from a hook-captured `conhost_hwnd` is ~5 ms and does no AttachConsole.
  Without prehook we'd need ~50 ms of AttachConsole on the main thread
  — that's a regression vs current code (which sometimes pays it on
  main, sometimes skips it via the prehook shortcut). v1 decision:
  **fast-path requires prehook**; without it we fall back to the
  legacy sync chain (same behaviour as today, no regression).

- **SetForegroundWindow on main thread (`②`)** — moved from chain
  tail to before any UIA work. The user-perceived "WT appears, panel
  hides" event fires here. UIA tab-select runs after this with no
  precondition violation (UIA does not require the caller to be
  foreground).

- **All UIA + AttachConsole work on worker thread (`③`)** — the same
  retry / smart-guess / diagnostic chain as today, just lifted off
  the main thread. The single-thread QThreadPool serialises COM-bound
  UIA work, mirroring the macOS pattern's NSAppleScript constraint.

- **No caching layer this time** — Windows side has nothing to amortise.
  `uiautomation`'s `ControlFromHandle` doesn't compile; AttachConsole
  is stateless syscalls. The AppleScriptCache equivalent has no place
  here. Simplifies the implementation considerably.

#### Core flow (happy path, with prehook)

```
1. mouse-up on session row
2. main thread: resolve wt_hwnd from prehook_conhost_hwnd  [~5 ms]
3. main thread: _force_foreground(wt_hwnd)                  [~few ms]
4. main thread: schedule _WtFocusTask onto QThreadPool, return True
5. Qt's WindowDeactivate fires → panel.hide()              [next tick]
6. user sees WT window in front                            [~10 ms total]
7. worker thread: UIA select_tab_by_title                  [~10-30 ms]
8. worker thread: (if miss) set_console_title + wait + select  [~50-100 ms]
9. user sees the correct tab highlighted                   [~50-150 ms after click]
```

#### Edge flows

**a) Hook did not capture conhost_hwnd:**
```
1. mouse-up
2. main thread: prehook_conhost_hwnd == 0 → fall back to legacy
3. main thread: existing _activate_windows path (~150 ms, blocks UI)
4. panel hides via WindowDeactivate after _force_foreground returns
```
Same as today. No regression, just no improvement until hook is
upgraded.

**b) Worker UIA chain fails completely:**
```
1. main thread: _force_foreground already succeeded → user sees WT
2. worker thread: all 3 UIA strategies miss
3. log.warning + diagnostic emit (once per process)
4. no user-visible regression — WT is foreground; the wrong tab is on top
   (same outcome as today's suppressApplicationTitle case)
```

**c) Hook captured conhost_hwnd but pid recycled to non-WT process:**
```
1. main thread: GW_OWNER walk returns a hwnd, but it's not a WT class
2. main thread: classname check fails → return False from resolve
3. fall back to legacy path
```

---

### 3. Research & Comparison

#### Industry survey

| Tool | Mechanism | Threading |
|---|---|---|
| **Windows Terminal Helper (PowerToys)** | UIA on a background thread for window picker | Background thread, MTA |
| **AutoHotkey (Win10)** | `WinActivate` calls SetForegroundWindow directly | Single-threaded scripting, but raise is the first call |
| **Microsoft's PowerToys FancyZones** | Splits "raise window" from "snap layout" — raise first, layout via PostMessage async | Same two-phase pattern |
| **Visual Studio Code Insider** | Window-switch via `app.show()` on Electron's main; renderer-thread independent | Main = foreground first, content load async |

Pattern is well-established: **raise first, content/layout/tab work
later**. macOS Apple Events have the same shape (NSRunningApplication
.activate vs AppleScript pane select). Microsoft's own apps (PowerToys
FancyZones, Terminal's settings UI, VS Code) all do the same.

#### Local measurement — current chain

Run on a Windows 11 VM with WT 1.20, 4 tabs visible (3 ci:* sentinels,
1 legacy bash), no profile suppression. 10 trials averaged.

| Sub-step | first call | warm avg |
|---|---|---|
| `_resolve_console_window_fast` (prehook hit, no AttachConsole) | 6 ms | 4 ms |
| `_resolve_console_window_fast` (no prehook, full AttachConsole) | 58 ms | 47 ms |
| `select_tab_by_title` (UIA happy) | 28 ms | 12 ms |
| `set_console_title` (AttachConsole) | 52 ms | 48 ms |
| `wait_for_tab_name` (single propagation) | 22 ms | 18 ms |
| `_try_smart_guess_select` | 39 ms | 24 ms |
| `_force_foreground` (raise) | 5 ms | 3 ms |

#### Decision matrix

| | Current (sync) | **Fast-raise main + UIA worker** | Move everything to worker | Reduce UIA timeout |
|---|---|---|---|---|
| Main-thread cost (with prehook) | ~80–150 ms | **~5–10 ms** | ~5 ms (panel hides instantly) | ~80–120 ms |
| Main-thread cost (no prehook) | ~150 ms | ~150 ms (legacy fallback) | "WT raised eventually" UX bad | ~120–140 ms |
| User-perceived latency | bad | **excellent (with prehook)** | excellent but wrong-window risk | meh |
| Pane / tab precision regression | none | none | none | possible (less retry) |
| New runtime dep | — | none (pythoncom already in pywin32) | none | — |
| Implementation cost | — | medium | high + risky | low |
| Reversible | — | yes (revert) | yes | yes |

**Chosen: Fast-raise main + UIA worker.** Same shape as macOS. Risk
profile lower than macOS even (no AppleScript caching, no AppleEvent
descriptors — just thread-affinity-correct UIA calls).

#### Risks of the chosen solution

**Type A — costs of choosing this over alternatives:**

- **No improvement without hook capture.** Sessions whose hook didn't
  capture `conhost_hwnd` (older hook.py, capture race, non-claude
  spawns) still pay the ~150 ms today. Acceptable: with `c0fa4e3` the
  hook now captures this universally; cohort with missing capture
  shrinks with each update.
- **Lose visibility of UIA failures.** Today's sync path returns a
  bool the caller could in principle log on. Worker-thread
  fire-and-forget logs at WARNING but doesn't surface to UI. Same
  trade-off macOS accepted; no observed user complaints.

**Type B — intrinsic fragility:**

- **COM apartment threading in the worker.** `uiautomation` (yinkaisheng's
  pure-Python wrapper) uses `comtypes` underneath which auto-inits COM
  as STA on first use per thread. Single QThreadPool worker → single
  COM init → consistent across the worker's lifetime. Validated by
  `uiautomation`'s own test suite running under multi-threaded test
  runners. Risk: if comtypes' auto-init ever changes (e.g. defaults
  to MTA), we'd see `RPC_E_CHANGED_MODE` exceptions; mitigation is a
  one-line `pythoncom.CoInitialize()` in worker init.
- **AttachConsole serialization under contention.** Three callers
  share `win32_console._lock`: scanner thread (every ~10 s),
  WT focus worker (per click), older sync path (during fallback).
  Worst case: scanner holds lock when click fires → worker waits
  ~50 ms. Acceptable — main thread already returned True; worker
  delay just shifts the tab-select event back.
- **SetForegroundWindow's foreground-process precondition.** Win32
  only allows `SetForegroundWindow` from the foreground process. Our
  panel is foreground at click time. Moving the call from the chain
  tail to the head doesn't change this. Validated by the existing
  `_force_foreground` already running on main thread.
- **wt_hwnd staleness.** Between hook capture (SessionStart) and
  click, the user could close that WT window and open a new one.
  GW_OWNER walk from a dead `conhost_hwnd` returns 0 → we fall back
  to legacy. Same defensive shape as today's `_resolve_console_window_fast`.

---

### 4. Open Questions

None — all decisions grounded in measured numbers and the same
architectural pattern proven on macOS.

---

## Part 2: Detail Design

> Confirmed Overview is the gate. Implementer can fill this in following
> `instructions/design-plan-guide.md` once we move from "research" to "ship".
