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

# JSONL transcripts are the single source of truth — UsageRegistry is
# in-memory and rebuilt from JSONL on every start, so there's no DB
# file to resolve here. We still use platformdirs for the QuotaProvider
# cache below.
_APP_NAME = "ClaudeIsland"
_CLAUDE_PROJECTS = Path.home() / ".claude" / "projects"

session_registry = SessionRegistry()
usage_registry = UsageRegistry()
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
from claude_island.platform_ import session_state as session_state_reader
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
from datetime import timedelta

from claude_island.ui.capsule_window import CapsuleWindow
from claude_island.ui.controller import IslandController
from claude_island.ui.expanded_window import ExpandedWindow
from claude_island.ui.qt_bridge import QtBridge


def _build_session_usage():
    """Combine the local 5h block with the (optional) remote quota
    snapshot. Lives here because it's cross-layer wiring: core gives
    the local block, platform gives the remote quota, UI consumes
    the combined record. Neither layer should know about the other.

    When a quota snapshot is available, we anchor the session window
    to Anthropic's authoritative reset time: window = [resets_at - 5h,
    resets_at]. Without it, the registry uses its local approximation
    (earliest record in the last 5h, plus 5h).
    """
    snap = quota_provider.get()
    if snap is not None:
        end_time = snap.five_hour_resets_at
        since = end_time - timedelta(hours=5)
        base = usage_registry.get_session_window(since=since, end_time=end_time)
    else:
        base = usage_registry.get_session_window()
    return replace(base, quota=snap)


def _build_session_details(session):
    """Compose the per-row hover-tooltip details from three sources:
    the JSONL parser's session metadata cache, ``~/.claude/sessions/
    <pid>.json``, and the UsageRegistry's per-session aggregate.

    The ProcessScanner can't easily fill in ``session.session_uuid``
    (it'd need to read the transcript, which isn't its job), so we
    look up the real uuid here from sessions/<pid>.json's ``sessionId``
    field. Without this, the per-session $ aggregate was always $0
    because the empty uuid never matched any UsageRecord.

    Each source is read independently and treated as best-effort; a
    miss in one leaves that field None and the tooltip degrades.
    """
    from claude_island.core.models import SessionDetails
    state = session_state_reader.read_session_state(session.pid) or {}
    # Prefer sessionId from the per-pid state file (always present for
    # a live Claude Code process) over Session.session_uuid (which
    # ProcessScanner leaves empty).
    sess_uuid = state.get("sessionId") if isinstance(state.get("sessionId"), str) else session.session_uuid
    meta = jsonl_parser.get_session_metadata(sess_uuid) or {}
    cost, turns, sides = usage_registry.get_session_summary(sess_uuid)
    return SessionDetails(
        session=session,
        name=state.get("name") if isinstance(state.get("name"), str) else None,
        ai_title=meta.get("ai_title"),
        git_branch=meta.get("git_branch"),
        last_prompt=meta.get("last_prompt"),
        started_at=session_state_reader.parse_started_at(state.get("startedAt")),
        status=state.get("status") if isinstance(state.get("status"), str) else None,
        cc_version=state.get("version") or meta.get("version"),
        cost_usd=cost,
        turn_count=turns,
        sidechain_count=sides,
    )


controller = IslandController()
capsule = CapsuleWindow(controller)
expanded = ExpandedWindow(
    capsule=capsule,
    controller=controller,
    get_usage_totals=usage_registry.get_totals,
    get_session_usage=_build_session_usage,
    on_refresh_clicked=quota_provider.force_refresh,
    get_session_details=_build_session_details,
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
    # Bounded join: backfill checks the stop flag at each file boundary,
    # so in the worst case we wait for the current file's parse to
    # finish. UsageRegistry is in-memory now, so there's nothing to
    # close — the GC reclaims the records list when the process exits.
    _backfill_thread.join(timeout=5.0)

sys.exit(exit_code)
