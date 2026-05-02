# AGENTS.md

## Key Commands

```bash
# Setup
python -m venv .venv && .venv\Scripts\activate && pip install -e ".[dev]"

# Run
python -m claude_island

# Test
pytest tests/

# Architecture check (run after any import changes)
python -m import_linter
```

`import_linter` is **not** part of `pytest` — run it manually after changing imports.

## Three-Layer Architecture

```
ui (PySide6)  →  core (pure Python)  ←  platform (OS APIs: psutil, pywin32)
```

**Critical constraint**: `claude_island/ui/` and `claude_island/platform_/` must **never** import each other. `claude_island/ui/qt_bridge.py` is the **only** file that imports both core and PySide6. Violations are caught by `import_linter`.

## Thread Model

Core emits `Event[T]` synchronously from whichever thread calls `emit()`. `QtBridge` is the **single thread boundary** — it re-emits on the Qt main thread via `Qt.QueuedConnection`. Core owns zero threads.

## UI-Win32 Gotcha

`WA_ShowWithoutActivating` is intentionally **not** set on the expanded panel. That flag sets `WS_EX_NOACTIVATE`, which causes `WM_MOUSEACTIVATE` to return `MA_NOACTIVATE` — clicks deliver but never make the app foreground, so `SetForegroundWindow` on the target terminal fails with "calling process must be foreground". The panel calls `activateWindow()` explicitly after show instead.

## Usage Periods

- `today` = current calendar day (midnight UTC → now)
- `daily` = trailing 24h
- `weekly` = trailing 7 days
- `monthly` = trailing 30 days

`cost_usd` is **not stored** in SQLite — `get_totals()` recomputes from tokens + live `PRICING` on every read.

## Session Activity Updates

`jsonl_parser.activity_updated` does **not** immediately emit `sessions_changed`. It stores an override in `SessionRegistry._activity_overrides`. The next `update()` call (triggered by the 10s process scan) applies overrides and emits if changed.

## Adding New Capabilities

- **New session field**: `models.py` → `session_registry.py` → `jsonl_parser.py` (core only) → expose via existing `sessions_changed`
- **New platform capability**: define `Protocol` in `platform_/protocols.py`, implement in `platform_/`, inject via `__main__.py` — never import platform from core
- **New UI reaction to core event**: wire in `qt_bridge.py`'s declarative table — no other UI file subscribes to core Events directly
- **New usage period**: add to `_PERIOD_DELTA` in `usage_registry.py` and add button in `expanded_window.py`

## Design Docs

See `docs/design/claude-island.md` for full Overview + Detail Design.
