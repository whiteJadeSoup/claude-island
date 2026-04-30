"""ClaudeIsland entry point — `python -m claude_island`.

Three-section structure (from design §3.3):

  Section 1 — Core layer: pure Python objects, no Qt, no OS APIs
  Section 2 — Platform layer: OS-specific implementations
  Section 3 — UI layer: Qt widgets
  Section 4 — Bridge wiring: declarative table connecting core events to UI slots

Background threads are started here and stopped on QApplication exit.
"""

from __future__ import annotations

import sys
import threading
from pathlib import Path

from platformdirs import user_data_dir
from PySide6.QtWidgets import QApplication

# ---------------------------------------------------------------------------
# Section 1: Core layer (no Qt, no OS APIs)
# ---------------------------------------------------------------------------
from claude_island.core.jsonl_parser import JsonlParser
from claude_island.core.session_registry import SessionRegistry
from claude_island.core.usage_registry import UsageRegistry

# Resolve paths via platformdirs so they follow XDG / AppData conventions.
_APP_NAME = "ClaudeIsland"
_CLAUDE_PROJECTS = Path.home() / ".claude" / "projects"
_DB_PATH = Path(user_data_dir(_APP_NAME, appauthor=False)) / "usage.db"

session_registry = SessionRegistry()
usage_registry = UsageRegistry(db_path=_DB_PATH)
jsonl_parser = JsonlParser(
    usage_registry=usage_registry,
    claude_projects_dir=_CLAUDE_PROJECTS,
)

# ---------------------------------------------------------------------------
# Section 2: Platform layer (psutil, watchdog, pywin32/pyobjc)
# ---------------------------------------------------------------------------
from claude_island.platform_.file_watcher import FileWatcher
from claude_island.platform_.process_scanner import ProcessScanner
from claude_island.platform_.session_discovery import SessionDiscovery
from claude_island.platform_.window_activator import WindowActivator

process_scanner = ProcessScanner()
file_watcher = FileWatcher()
window_activator = WindowActivator()
session_discovery = SessionDiscovery(
    scanner=process_scanner,
    registry=session_registry,
)

# ---------------------------------------------------------------------------
# Section 3: UI layer (Qt)
# ---------------------------------------------------------------------------
app = QApplication(sys.argv)
app.setQuitOnLastWindowClosed(False)

from claude_island.ui.capsule_window import CapsuleWindow
from claude_island.ui.controller import IslandController
from claude_island.ui.expanded_window import ExpandedWindow
from claude_island.ui.qt_bridge import QtBridge

controller = IslandController()
capsule = CapsuleWindow(controller)
expanded = ExpandedWindow(
    capsule=capsule,
    controller=controller,
    get_usage_totals=usage_registry.get_totals,
)

# ---------------------------------------------------------------------------
# Section 4: Bridge wiring (declarative table)
#
# Each entry: (core Event[T], UI slot)
# QtBridge marshals the emit from background thread → Qt main thread.
# ---------------------------------------------------------------------------
_wiring = [
    (session_registry.sessions_changed,    controller.on_sessions_updated),
    (session_registry.sessions_changed,    capsule.refresh_sessions),
    (session_registry.sessions_changed,    expanded.refresh_sessions),
    (session_registry.permission_required, controller.on_permission_required),
    (usage_registry.totals_changed,        expanded.refresh_usage_bar),
]
_bridges = [QtBridge(event) for event, _ in _wiring]
for bridge, (_, slot) in zip(_bridges, _wiring):
    bridge.connect_to(slot)

# Platform → UI direct connection (session activation: UI emits, platform handles).
# No bridge needed — session_activated fires on the Qt main thread already.
expanded.session_activated.connect(window_activator.activate)

# ---------------------------------------------------------------------------
# Start background services
# ---------------------------------------------------------------------------
if _CLAUDE_PROJECTS.exists():
    file_watcher.watch(_CLAUDE_PROJECTS, jsonl_parser.parse_file)
    file_watcher.start()

    # Backfill existing JSONL history in a daemon thread so startup is instant.
    _backfill = threading.Thread(target=jsonl_parser.backfill_all, daemon=True)
    _backfill.start()

session_discovery.start()
capsule.show()

# ---------------------------------------------------------------------------
# Event loop + cleanup
# ---------------------------------------------------------------------------
exit_code = app.exec()

session_discovery.stop()
if _CLAUDE_PROJECTS.exists():
    file_watcher.stop()
usage_registry.close()

sys.exit(exit_code)
