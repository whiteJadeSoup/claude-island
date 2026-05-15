# iTerm Focus Performance — Async Dispatch + PyObjC Direct APIs

> **Status**: Overview Design — pending review.
> Once confirmed, Detail Design follows below the line.

---

## Part 1: Overview Design

### 1. Problem & Goals

#### Problem

Clicking a session row in the expanded panel feels visibly laggy — the panel hangs for a beat before disappearing and the terminal appears. Measured on a quiet machine:

| Component | Wall time |
|---|---|
| `subprocess.run(["/usr/bin/osascript", "-e", script])` cold | 255 ms |
| `subprocess.run(...)` warm-average | **223 ms** |
| `psutil.Process(pid).terminal()` | 0.1 ms |
| `_iterm_host_pid` parent walk | 0.3 ms |
| osascript binary spawn cost alone (`-e 'return ""'`) | ~80 ms |

The full focus path is invoked synchronously from `_on_row_clicked` on Qt's **main thread**, so the UI freezes for ~250 ms per click. Two iTerm2 instances side-by-side make it worse (the recently-shipped multi-instance fix doesn't speed up dispatch; it only fixes correctness).

#### Goals

| ID | Goal | How verified |
|----|------|--------------|
| **G1** | Click → panel begins to hide within **≤ 5 ms** of mouse-up (perceived latency = effectively zero). | Microbenchmark on Qt main thread: `_on_row_clicked` returns < 5 ms on a quiet machine; manual cross-check with a 4-session live panel. |
| **G2** | Click → target terminal frontmost within **≤ 50 ms** wall time (p95) on a warm path. | New e2e test: stub the iTerm app, time `iterm2.focus()` invocation to terminal-front signal. |
| **G3** | Pane-precision (right window / tab / session selected inside iTerm) is preserved — current correctness is **not** regressed for the sake of speed. | Existing iterm2 adapter tests + new test exercising id-match success / tty-match fallback. |
| **G4** | Graceful degradation when PyObjC is unavailable (extreme edge: pyenv built `--without-pymalloc`, or PyObjC removed from `pip` env). | Fallback to current subprocess osascript path; existing tests still green; unit test simulates `ImportError`. |
| **G5** | No change to non-FOCUS dispatches (RENAME, RESET_THINKING, REVEAL_CWD, etc.). Those are popup-internal saves that the user expects to be synchronous. | Existing tests on those paths unchanged and passing. |

#### Non-Goals

- **Native focus for Terminal.app / Ghostty / Warp.** They have their own adapters and their own perf issues; v1 of this work targets iTerm2 only (the bottleneck the user actually hits — 4 sessions all in iTerm2 in our test population).
- **Replacing AppleScript entirely.** iTerm's pane addressing has no documented Apple Events / Accessibility-API equivalent; the AppleScript pane-select round-trip is intrinsic to "select a specific session". We optimise around it, not through it.
- **Persistent iTerm Python-API connection** (`iterm2.Connection` websocket). Adds a runtime dependency, requires user to enable "Python API" in iTerm prefs, and only helps iTerm. Disproportionate to the gain.
- **Eliminating osascript altogether.** The fallback path remains — useful when PyObjC is missing or NSAppleScript hits an unexpected error mid-execute.

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
              ITerm2Adapter.focus(view)                           │
                       │                                          │
              ┌────────┴────────┐                                 │
              │ ① main-thread   │  S5: NSRunningApplication       │
              │   app raise     │      .activate(terminal_pid)    │
              │   (0.3 ms warm) │  → iTerm front, panel auto-hides│
              └────────┬────────┘                                 │
                       │                                          │
              ┌────────▼────────┐  QThreadPool submit             │
              │ ② return True   │  (fire-and-forget)              │
              └─────────────────┘                                 │
                       │                                          │
                       ▼                                          │
        ╔═══════════════════════════════════════════════════════════════════╗
        ║                 worker thread (QThreadPool)                       ║
        ║                                                                   ║
        ║    cached NSAppleScript (compiled once, instance reused)          ║
        ║    .executeAppleEvent_( focusByID(session_id) )                   ║
        ║       │ (40-200 ms; variable, depends on iTerm load)              ║
        ║       ▼                                                           ║
        ║    pane / tab / window select complete                            ║
        ║                                                                   ║
        ║    on miss → fall back to focusByTTY(tty)                         ║
        ║    on failure → no UI feedback (focus_host_app already succeeded) ║
        ╚═══════════════════════════════════════════════════════════════════╝
```

**Key design decisions on the diagram**:

- **App raise on main thread (`①`)** — bypasses AppleScript entirely; uses `NSRunningApplication.activate` (AppKit, in-process, ~0.3 ms warm). The user-perceived effect is "panel hides instantly + iTerm appears" because Qt's `WindowDeactivate` fires the moment iTerm is foreground.
- **Pane select on worker thread (`②`)** — the slow part (40–200 ms of AppleScript engine round-trip) is decoupled from the main thread. The user is already looking at iTerm by the time this finishes; the pane switch lands "in the background" and feels fluent.
- **Caching the `NSAppleScript` instance** — Apple's docs guarantee the compiled bytecode is retained for the instance's lifetime ([NSAppleScript.executeAndReturnError](https://developer.apple.com/documentation/foundation/nsapplescript/1410034-executeandreturnerror)). A single static instance per template (id-match + tty-match) amortises compile cost across the app lifetime.
- **Subroutine-style dispatch via `NSAppleEventDescriptor`** — the cached script defines `on focusByID(sessionID)` / `on focusByTTY(targetTTY)` handlers. Each click builds an `aevt/psbr` AppleEvent and `executeAppleEvent_error_` invokes the handler with arguments. Source compilation never happens twice; only the in-memory dispatch runs per call.

#### Core flow (happy path)

```
1. mouse-up on session row
2. main thread: NSRunningApplication.activate(host_pid)   [~0.3 ms]
3. main thread: schedule pane select onto QThreadPool, return True
4. Qt's WindowDeactivate fires → panel.hide()              [next tick]
5. user sees iTerm in front
6. worker thread: NSAppleScript.executeAppleEvent(focusByID(...))  [~40-100 ms]
7. user sees the correct pane highlighted
```

#### Edge flows

**a) PyObjC unavailable (ImportError on AppKit / Foundation):**
```
1. mouse-up
2. main thread: detect ImportError → fall back to current path
3. main thread: subprocess osascript (~250 ms, UI freezes — degraded)
4. panel hides via WindowDeactivate after osascript returns
```

**b) NSAppleScript execute returns error on worker thread:**
```
1. main thread: NSRunningApplication.activate succeeds → user already sees iTerm
2. worker thread: pane select raises / returns miss
3. log.warning("iterm2.focus pane select failed: %s", err)
4. no user-visible regression — iTerm is foreground; the wrong pane is on top
   (same outcome as today's tty-miss fallback)
```

---

### 3. Research & Comparison

#### Industry survey

| Tool | Mechanism | Caches compiled script? |
|---|---|---|
| **Hammerspoon** | OSAKit in-process (Obj-C) | No — re-creates `OSAScript` per call ([libosascript.m](https://github.com/Hammerspoon/hammerspoon/blob/master/extensions/osascript/libosascript.m)) |
| **Alfred** | Two modes: subprocess osascript OR NSAppleScript Action with explicit **"Cache compiled AppleScript"** toggle ([docs](https://www.alfredapp.com/help/workflows/actions/run-nsapplescript/)) | Optional — toggle exposes exactly the pattern we propose |
| **Raycast** | Subprocess osascript via Node bridge | No — issue [#163](https://github.com/raycast/script-commands/issues/163) reports 5s spikes |
| **open-vibe-island** | Subprocess osascript everywhere; two NSAppleScript sites (`TerminalTextSender.swift:162-174`, `KeystrokeInjector.swift:60-78`) — but **no caching** in either | No — same perf bug we have, hidden behind Swift |

Apple's `NSAppleScript.executeAndReturnError:` is the canonical way to amortise compile cost: first execute compiles, subsequent executes against the same instance reuse the compiled form.

#### Local microbenchmark

Same script body (System Events frontmost + iTerm pane select), 10 trials, target = a real running session.

| # | Implementation | First call | Warm avg |
|---|---|---|---|
| **S1** | `subprocess.run(osascript)` *(current)* | 255 ms | **223 ms** |
| **S3** | PyObjC `NSAppleScript.executeAndReturnError`, new instance per call | 289 ms | **89 ms** |
| **S4** | PyObjC NSAppleScript, explicit `compileAndReturnError` + execute, new instance | 51 ms | 36 ms ~ 267 ms (high variance) |
| **S6** | PyObjC NSAppleScript, **one cached instance** + `executeAppleEvent_error_` with subroutine handler | 137 ms | 70-180 ms (variable; floor ~40 ms) |
| **S5** | `NSRunningApplication.activate(host_pid)` (no pane precision) | 34 ms | **0.3 ms** |

The variance on S4/S6 is iTerm's AppleScript runtime itself — when iTerm is doing other work the request queues. **S5 is a flat 0.3 ms** because it's an in-process AppKit call to the dock / window-server, not a round-trip through iTerm.

#### Decision matrix

| | Current (S1 sync) | S1 async only | S3 async | S6 async | **S5 main + S6 worker** | iTerm Python API |
|---|---|---|---|---|---|---|
| Main-thread cost | 223 ms | 0 ms | 0 ms | 0 ms | **0.3 ms** | 0 ms |
| Pane select wall-time | 223 ms | 223 ms | 89 ms | 70-180 ms | 70-180 ms | 10-30 ms |
| User-perceived latency | bad | OK | OK | OK | **excellent** | excellent |
| New runtime dep | none | none | PyObjC* | PyObjC* | PyObjC* | iTerm Python API |
| Implementation cost | — | trivial | low | medium | medium | high |
| Multi-instance correct | yes (post-fix) | yes | yes | yes | yes | yes |

\* `pyobjc-framework-Cocoa` is a `pip` install marked `sys_platform == "darwin"`; bundled with macOS's system Python and present in 100% of macOS Homebrew Python builds we tested.

**Chosen: S5 + S6 combo.** Main-thread fast-path is the user-perceived win; cached NSAppleScript on a worker thread gives best-feasible pane select speed without leaving Python.

#### Risks of the chosen solution

**Type A — costs of choosing this over alternatives:**
- **PyObjC introduction.** `pip install pyobjc-framework-Cocoa` adds ~30 MB and a C-extension build. Marked `sys_platform == "darwin"` so Linux/Windows installs are untouched.
- **Lose error visibility for pane-select failures.** Today's sync path returns a bool the caller could in principle log on. Worker-thread fire-and-forget logs at WARNING but doesn't surface to UI. Acceptable given S5 already succeeded.

**Type B — intrinsic fragility:**
- **NSAppleScript thread affinity.** Apple's docs say "NSAppleScript is not thread-safe" but the same docs say "you may call executeAndReturnError on any thread *if* you serialise calls to that instance." We do serialise (single QThreadPool worker for FOCUS). Validated by `py-applescript`'s production usage pattern.
- **iTerm AppleScript engine variance.** Floor 40 ms, ceiling 1+ second seen under load. Outside our control; mitigated by the fact that the main thread already responded — the worst case is "iTerm front shows the wrong pane for a second, then snaps to the right one".
- **AppleScript permission denial.** First run prompts for accessibility; user can revoke at any time. Existing fallback to `focus_host_app` already handles this; new path adds nothing new on this axis.

---

### 4. Open Questions

None — all decisions grounded in measured numbers and Apple's documented contract.

---

## Part 2: Detail Design

> Confirmed Overview is the gate. Implementer can fill this in following
> `instructions/design-plan-guide.md` once we move from "research" to "ship".
