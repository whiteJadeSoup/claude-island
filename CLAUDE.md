# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
# Setup
python -m venv .venv
.venv\Scripts\activate          # Windows
pip install -e ".[dev]"

# Run
python -m claude_island

# Test
pytest tests/

# Validate architecture layering (run after any import changes)
python -m import_linter
```

## Architecture

Claude Island is a floating capsule UI (Windows/macOS) that monitors running Claude Code sessions and token usage. It mimics the iPhone Dynamic Island — a minimal pill that expands to show sessions and costs.

### Three-Layer Architecture (strictly enforced by import-linter)

```
UI Layer (PySide6)  ──→  Core Layer (pure Python)  ←──  Platform Layer (OS APIs)
```

**Dependency rule**: UI depends on core; platform depends on core (protocols only). Core has **zero** UI framework or OS dependencies. Violations are caught by `python -m import_linter` — run it after any import changes. Contracts are defined in `pyproject.toml`.

### Layer Responsibilities

**`claude_island/core/`** — Pure Python, no OS/framework deps. (`reactivex` is permitted — pure Python, no UI / OS deps.)
- `events.py` — Thread-safe `Event[T]` generic observer (subscribe/emit pattern). **Used by source registries only**; UI never subscribes — UI listens to `world.observable()` instead.
- `models.py` — Frozen dataclasses: `Session`, `UsageTotals`, `PricingTable`, `SessionDetails`, `QuotaSnapshot`
- `snapshot.py` — **Single source of truth for UI state.** Defines `SessionView`, `WorldSnapshot`, the `world` singleton (a `BehaviorSubject[WorldSnapshot]` wrapper), and the `Snapshotter` worker. See "WorldSnapshot broadcast" section below.
- `session_registry.py` — Thread-locked in-memory store of live sessions; emits `sessions_changed`
- `usage_registry.py` — SQLite wrapper; aggregates token usage by time window; computes USD cost
- `jsonl_parser.py` — Incremental parser for `~/.claude/projects/**/*.jsonl`; tracks byte offset per file

**`claude_island/platform_/`** — OS-specific implementations.
- `protocols.py` — Defines `ProcessScannerProtocol`, `WindowActivatorProtocol`, `FileWatcherProtocol` (typing.Protocol)
- `process_scanner.py` — psutil-based process enumeration; finds Claude PIDs + parent terminal HWNDs
- `file_watcher.py` — watchdog wrapper monitoring `~/.claude/projects/`
- `window_activator.py` — Win32 `SetForegroundWindow` (Windows) / `NSRunningApplication.activate` (macOS)
- `session_discovery.py` — Timer-driven orchestrator merging process scan + file watch into `SessionRegistry`

**`claude_island/ui/`** — PySide6 Qt widgets.
- `qt_bridge.py` — Marshals core `Event[T]` → Qt `Signal` via `Qt.QueuedConnection`. **Legacy** — only used by the controller's `on_sessions_updated` wire after the broadcast refactor (Phase G1). New code should subscribe to `world.observable()` and let the marshaler handle threading.
- `world_marshaler.py` — Tiny `QObject` whose `snap_ready` Signal is connected to `world.push` via `Qt.QueuedConnection`. This is what crosses the worker → Qt main thread boundary for the WorldSnapshot pipeline (reactivex's `QtScheduler` doesn't, see comments in `world_marshaler.py`).
- `controller.py` — `IslandController` state machine (`transitions` lib): dot ↔ collapsed ↔ expanded
- `capsule_window.py` — Frameless always-on-top floating capsule (collapsed state). Renders via `render(snap)`.
- `expanded_window.py` — Session list + usage breakdown panel (expanded state). Renders via `render(snap)`.

**`claude_island/__main__.py`** — Entry point. Instantiates all layers, builds the `Snapshotter`, wires sources to `snapshotter.wake()`, and subscribes capsule + expanded `render` to `world.observable()`. Start here to understand the full dependency graph.

### Thread Model

Core's source registries emit `Event[T]` (synchronous, on the calling thread). These events drive `snapshotter.wake()` only — they do NOT directly drive UI.

`Snapshotter` runs builds on a single dedicated worker thread (reactivex's `EventLoopScheduler`). After each build, the snapshot is sent through `WorldMarshaler.snap_ready` (a Qt Signal connected via `QueuedConnection`), which marshals the call onto the Qt main thread. There, `world.push(snap)` synchronously notifies subscribers — so `capsule.render(snap)` and `expanded.render(snap)` always run on the Qt main thread, regardless of which thread the build ran on.

### WorldSnapshot broadcast (the new architecture, Phase B-G1)

```
sources ── wake() ──→ Snapshotter (worker thread)
                           │
                           │ build → publish=marshaler.snap_ready.emit
                           ▼
                    WorldMarshaler (Qt main thread, QueuedConnection)
                           │
                           ▼
                    world.push(snap)        ← all on Qt main thread
                           │
                           │ pipe(distinct_until_changed(render_key))
                           ▼
            capsule.render(snap), expanded.render(snap)
```

`WorldSnapshot` is the single source of truth. UI surfaces consume it; they never call back into registries. Adding a new UI surface = one `world.observable().pipe(...).subscribe(my_render)` call — no event-by-event wiring.

### reactivex usage (operator whitelist)

This project uses **only these reactivex primitives**. Don't reach for others without discussion — Rx has 200+ operators and we want a small, well-understood vocabulary:

- `BehaviorSubject[T]` — variable that broadcasts on write, replays current value on subscribe. Used for `world`.
- `Subject[T]` — fire-and-forget event stream. Used for `Snapshotter._wake_signal`.
- `pipe(...)` — chain operators on a stream.
- `subscribe(on_next=, on_error=)` — attach a callback. **Always pass `on_error`** so a downstream exception doesn't kill the stream.

Operators (whitelist):
- `ops.distinct_until_changed(key_mapper=...)` — drop consecutive equal values (use `WorldSnapshot.render_key`).
- `ops.observe_on(scheduler)` — switch downstream callback to a different thread. **Currently unused** — we use Qt Signal QueuedConnection for thread crossing instead. Don't introduce `observe_on(QtScheduler)` without re-reading `tests/integration/test_reactivex_qt_compat.py` (the smoke test pinning down why `QtScheduler` doesn't work cross-thread).
- `ops.debounce(window, scheduler=...)` — coalesce a burst of events into the last one after `window` of quiet.
- `ops.throttle_first(window, scheduler=...)` — emit the first event, then suppress until `window` elapses.

### Key Patterns to Follow

- **Adding a new session data field**: update `models.py` → `session_registry.py` → `jsonl_parser.py` → add to `SessionView` in `core/snapshot.py` → resolve in `compose_session_view` → consume in `render(snap)` on the relevant UI surface.
- **Adding a new platform capability**: define a Protocol in `platform_/protocols.py`, implement in `platform_/`, inject via `__main__.py`. Never import platform code from core.
- **Adding a new UI surface (e.g. tray icon)**: write `render(snap: WorldSnapshot)`. Subscribe in `__main__.py`: `world.observable().pipe(ops.distinct_until_changed(key_mapper=lambda s: s.render_key())).subscribe(on_next=tray.render, on_error=...)`. No event wiring needed.
- **Triggering an immediate refresh**: call `snapshotter.wake()`. Returns instantly; debounced internally.
- **Session ID** is `f"{cwd}:{pid}"` — composite key deduplicating process scanner + file watcher sources.
