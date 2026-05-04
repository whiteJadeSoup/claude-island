# ClaudeIsland

A Dynamic Island-style floating capsule for Windows and macOS that surfaces active Claude Code sessions and token usage at a glance.

## Features

- Collapses to a small capsule; expands to show all active Claude Code sessions
- Click a session to bring its terminal to focus
- Right-click for a per-session detail popup (cost breakdown, last prompt, transcript)
- Recents drawer for resuming offline sessions in a new terminal
- Daily / weekly / monthly token usage and USD cost

## Prerequisites

- **Python 3.11+**
- **Claude Code** installed and used at least once (the JSONL transcripts under `~/.claude/projects/` are the data source — claude-island only reads them)
- **Per-platform terminal**:
  - **Windows**: [Windows Terminal](https://aka.ms/terminal) for tab grouping + Resume. Standard `cmd.exe` / `powershell.exe` consoles still show as live sessions but Resume lands in a new WT window.
  - **macOS**: [iTerm2](https://iterm2.com) gives pane-level focus. Terminal.app works as a fallback (Resume opens via `osascript`).
- `claude` on PATH. The npm-installed wrapper (`claude.cmd` on Windows, `claude` on macOS/Linux) is fine — the launcher invokes it through `cmd.exe /k` (Windows) / `Terminal do script` (macOS) so PATHEXT and shell init both work.

## Quickstart

```bash
git clone <this-repo> claude-island
cd claude-island

python -m venv .venv
# Windows (PowerShell)
.venv\Scripts\Activate.ps1
# macOS / Linux
source .venv/bin/activate

pip install -e ".[dev]"
python -m claude_island
```

The capsule appears at the top-center of your primary monitor. Drag the capsule to reposition; the position persists across restarts.

## Using the UI

| Action | Where |
|--------|-------|
| Expand the capsule | Click the capsule |
| Bring a session's terminal to front | Click a session row |
| Open the detail popup | Right-click a session row |
| Open recents drawer (offline sessions) | Click the 📦 button beside the SESSIONS header |
| Resume a dormant session | Click `▶ Resume` in the recents drawer |
| Open the project folder | Detail popup → hover the path row → ↗, or `Ctrl+O` in recents |
| Copy session UUID | Detail popup → hover the ID row → ⧉, or `Ctrl+C` in recents |
| Open the session transcript (.jsonl) | Detail popup → hover the Transcript row → ↗ |
| Quit | Right-click the capsule → Quit |

## Architecture

Three independent layers enforced by `import-linter`:

```
ui  →  core  ←  platform
```

- **core** — pure Python, zero framework deps; reactivex `BehaviorSubject` for the `WorldSnapshot` stream, session/usage registries, JSONL parser
- **platform** — OS APIs (psutil, pywin32, pyobjc); implements core Protocols
- **ui** — PySide6 widgets; `WorldMarshaler` is the single bridge that crosses the worker → Qt main thread boundary

## Development

```bash
# Run the full test suite
pytest -q

# Validate architecture layering after any import change
lint-imports
```

Both must be green before merging.

## Design

See [docs/design/claude-island.md](docs/design/claude-island.md) for the full Overview + Detail Design.
