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
from claude_island.platform_.quota_provider import QuotaProvider
from claude_island.platform_.session_discovery import SessionDiscovery
from claude_island.platform_.window_activator import WindowActivator

process_scanner = ProcessScanner()
file_watcher = FileWatcher()
window_activator = WindowActivator()
session_discovery = SessionDiscovery(
    scanner=process_scanner,
    registry=session_registry,
)
# QuotaProvider hits Anthropic's private /api/oauth/usage with the OAuth
# token Claude Code already maintains. Cache lives next to our usage DB
# so all per-user state stays in one platformdirs-resolved location.
quota_provider = QuotaProvider(
    credentials_path=Path.home() / ".claude" / ".credentials.json",
    cache_path=Path(user_data_dir(_APP_NAME, appauthor=False)) / "usage-cache.json",
    enabled=True,
)

# ---------------------------------------------------------------------------
# Section 3: UI layer (Qt)
# ---------------------------------------------------------------------------
from PySide6.QtCore import QTimer

app = QApplication(sys.argv)
app.setQuitOnLastWindowClosed(False)

from dataclasses import replace

from claude_island.ui.capsule_window import CapsuleWindow
from claude_island.ui.controller import IslandController
from claude_island.ui.expanded_window import ExpandedWindow
from claude_island.ui.qt_bridge import QtBridge


def _build_session_usage():
    """Combine the local 5h block with the (optional) remote quota
    snapshot. Lives here because it's cross-layer wiring: core gives
    the local block, platform gives the remote quota, UI consumes
    the combined record. Neither layer should know about the other."""
    base = usage_registry.get_session_window()
    snap = quota_provider.get()
    return replace(base, quota=snap)


controller = IslandController()
capsule = CapsuleWindow(controller)
expanded = ExpandedWindow(
    capsule=capsule,
    controller=controller,
    get_usage_totals=usage_registry.get_totals,
    get_session_usage=_build_session_usage,
)

# ---------------------------------------------------------------------------
# Section 4: Bridge wiring (declarative table)
#
# Each entry: (core Event[T], UI slot)
# QtBridge marshals the emit from background thread → Qt main thread.
# ---------------------------------------------------------------------------
_wiring = [
    (session_registry.sessions_changed, controller.on_sessions_updated),
    (session_registry.sessions_changed, capsule.refresh_sessions),
    (session_registry.sessions_changed, expanded.refresh_sessions),
    (usage_registry.totals_changed,     expanded.refresh_usage_bar),
]
# Group by source event so we don't create N redundant QtBridge instances
# subscribing to the same Event (sessions_changed has 3 slots — they should
# share one bridge, not three). Matches the QtBridge docstring's example.
_bridges_by_event: dict[object, QtBridge] = {}
for event, slot in _wiring:
    bridge = _bridges_by_event.get(id(event))
    if bridge is None:
        bridge = QtBridge(event)
        _bridges_by_event[id(event)] = bridge
    bridge.connect_to(slot)
_bridges = list(_bridges_by_event.values())  # keep references alive

# core → core direct subscription: JSONL activity feeds the session registry's
# override map. update_activity is thread-safe and does not emit, so there is
# no need to marshal through Qt — the parser thread can call it directly.
jsonl_parser.activity_updated.subscribe(session_registry.update_activity)

# Platform → UI direct connection (session activation: UI emits, platform handles).
# No bridge needed — session_activated fires on the Qt main thread already.
expanded.session_activated.connect(window_activator.activate)

# ---------------------------------------------------------------------------
# Start background services
# ---------------------------------------------------------------------------
# Ensure the projects dir exists so we can watch it unconditionally. First-
# time users (no Claude Code history yet) had a gap: file_watcher.start was
# skipped at boot, then if they ran claude mid-session and quit,
# file_watcher.stop would raise RuntimeError("cannot stop unscheduled
# observer"). mkdir + unconditional start/stop closes that gap.
_CLAUDE_PROJECTS.mkdir(parents=True, exist_ok=True)

file_watcher.watch(_CLAUDE_PROJECTS, jsonl_parser.parse_file)
file_watcher.start()

# Backfill existing JSONL history in a daemon thread so startup is instant.
_backfill_thread = threading.Thread(target=jsonl_parser.backfill_all, daemon=True)
_backfill_thread.start()

# 60s heartbeat: tick the 5h reset countdown and pull a fresh quota
# snapshot. QuotaProvider gates HTTP internally on its 300s TTL, so this
# only issues a network call every 5 min.
_usage_heartbeat = QTimer()
_usage_heartbeat.timeout.connect(expanded.refresh_usage_bar)
_usage_heartbeat.start(60_000)

session_discovery.start()
capsule.show()

# ---------------------------------------------------------------------------
# Event loop + cleanup
#
# Order matters at shutdown: we must stop every producer of writes to the
# SQLite connection BEFORE closing it, or background threads will raise
# sqlite3.ProgrammingError on a closed connection. Order:
#   1. session_discovery.stop()  — no more sessions_changed emits
#   2. file_watcher.stop()       — no more parse_file callbacks
#   3. jsonl_parser.request_stop() + join — backfill thread exits cleanly
#   4. usage_registry.close()    — DB closed; nothing left to write to it
# ---------------------------------------------------------------------------
exit_code = app.exec()

session_discovery.stop()
file_watcher.stop()
jsonl_parser.request_stop()
if _backfill_thread is not None:
    # Bounded join: backfill checks the stop flag at each file boundary, so
    # in the worst case we wait for the current file's parse to finish.
    # 5 seconds is generous; if it ever exceeds, the daemon thread is killed
    # at process exit anyway and SQLite close() will raise harmlessly.
    _backfill_thread.join(timeout=5.0)
usage_registry.close()

sys.exit(exit_code)
