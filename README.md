# ClaudeIsland

A Dynamic Island-style floating capsule for Windows and macOS that surfaces active Claude Code sessions and token usage at a glance.

## Features

- Collapses to a small capsule; expands to show all active Claude Code sessions
- Click a session to bring its terminal to focus
- Daily / weekly / monthly token usage and USD cost

## Architecture

Three independent layers enforced by import-linter:

```
ui  →  core  ←  platform
```

- **core** — pure Python, zero framework deps; `Event[T]` observer, session/usage registries
- **platform** — OS APIs (psutil, pywin32, pyobjc); implements core Protocols
- **ui** — PySide6 widgets; `QtBridge` is the sole file that imports both core and Qt

## Development

```bash
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
python -m claude_island
```

## Design

See [docs/design/claude-island.md](docs/design/claude-island.md) for the full Overview + Detail Design.
