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

**`claude_island/core/`** — Pure Python, no OS/framework deps.
- `events.py` — Thread-safe `Event[T]` generic observer (subscribe/emit pattern)
- `models.py` — Frozen dataclasses: `Session`, `UsageTotals`, `PricingTable`
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
- `qt_bridge.py` — **The only file allowed to import both core and PySide6.** Marshals core `Event[T]` → Qt `Signal` via `Qt.QueuedConnection`, ensuring UI updates always run on the Qt main thread.
- `controller.py` — `IslandController` state machine (`transitions` lib): dot ↔ collapsed ↔ expanded
- `capsule_window.py` — Frameless always-on-top floating capsule (collapsed state)
- `expanded_window.py` — Session list + usage breakdown panel (expanded state)

**`claude_island/__main__.py`** — Entry point. Instantiates all layers, then wires core Events to UI slots via a declarative table in `QtBridge`. Start here to understand the full dependency graph.

### Thread Model

Core emits `Event[T]` synchronously from whichever thread calls `emit()`. The watchdog file watcher runs on a worker thread. `QtBridge` is the single thread boundary — it re-emits on the Qt main thread via `QueuedConnection`. Core owns zero threads.

### Key Patterns to Follow

- **Adding a new session data field**: update `models.py` → `session_registry.py` → `jsonl_parser.py` (core only), then expose via existing `sessions_changed` Event.
- **Adding a new platform capability**: define a Protocol in `platform_/protocols.py`, implement in `platform_/`, inject via `__main__.py`. Never import platform code from core.
- **Adding a new UI reaction to a core event**: wire it in `qt_bridge.py`'s declarative table. Don't subscribe to core Events from anywhere else in the UI.
- **Session ID** is `f"{cwd}:{pid}"` — composite key deduplicating process scanner + file watcher sources.
