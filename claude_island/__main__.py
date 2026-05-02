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
from claude_island.platform_.providers import (
    ProviderEngine,
    all_providers,
    ensure_provider_config,
    get_selected_provider,
    set_selected_provider,
)

# First-time-user friendly: drop a self-documented providers.json at
# ~/.claude-island/providers.json so users discover where + how to
# configure additional providers without trawling the README. No-op
# if the file already exists.
ensure_provider_config()
# No per-provider class imports here — providers self-register via the
# @provider("name") decorator when the providers package is imported.
# Adding a new provider is pure extension: drop a file under providers/
# and append it to the package's bottom-of-module import list. NO change
# to __main__.py is needed.
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
# ProviderEngine auto-detects the active provider (Anthropic / MiniMax)
# and dispatches to the right quota API. Each provider manages its own
# cache; the engine just calls get() and force_refresh().
quota_engine = ProviderEngine(
    cache_dir=Path(user_data_dir(_APP_NAME, appauthor=False)),
)

# ---------------------------------------------------------------------------
# Section 3: UI layer (Qt)
# ---------------------------------------------------------------------------
from PySide6.QtCore import QTimer

app = QApplication(sys.argv)
app.setQuitOnLastWindowClosed(False)

from claude_island.ui.capsule_window import CapsuleWindow
from claude_island.ui.controller import IslandController
from claude_island.ui.expanded_window import ExpandedWindow
from claude_island.ui.qt_bridge import QtBridge


def _get_quota_snapshot():
    """Fetch the currently-selected provider's quota snapshot.

    Reads the panel's selection at call time (closure pattern) so a
    tab click immediately re-fetches the right provider. Returns None
    when no quota is available (provider unconfigured, network error,
    etc.) — the QUOTA card hides its bars.
    """
    selected = expanded.selected_provider_name() if "expanded" in globals() else None
    return quota_engine.get(provider_name=selected)


def _resolve_available_providers() -> list[str]:
    """Build the tab list shown in the 5h-session card by asking each
    registered provider whether it has been *signalled* for use.

    Declarative: iterates ``all_providers()`` (auto-populated by the
    ``@provider`` decorator at import time) and keeps the ones whose
    ``detect()`` returns truthy. Adding a 4th / 5th provider needs no
    change here — just drop a file under ``providers/`` and add it to
    the bottom-of-module import list in ``providers/__init__.py``.

    Anthropic always detects (every Claude Code user has the OAuth
    credential), so when no other provider is configured the tab strip
    contains just Anthropic, and ExpandedWindow renders no tabs at all
    (single-provider users see the pre-feature look)."""
    return [name for name, cls in all_providers().items() if cls().detect()]


def _on_provider_tab_clicked(name: str) -> None:
    """User clicked a provider tab. Persist the choice — the next
    refresh tick will pick up the new selection.

    No force_refresh here: that would block the UI thread on a 3 s
    HTTP timeout per click. The provider's disk cache (5 min TTL) and
    the engine's in-memory cache (90 s TTL) make tab switches
    essentially instant from the user's perspective. The manual ↻
    button still exists for cases where the user wants to force a
    network fetch."""
    set_selected_provider(name)


_available_providers = _resolve_available_providers()
# Honour the user's stored choice, but fall back to a sensible default
# if the stored name is no longer valid (e.g. user removed the MiniMax
# token but providers.json still says "selected": "minimax").
#
# Explicit prefer-Anthropic fallback (NOT _available_providers[0]).
# The positional approach worked only because the import order at
# providers/__init__.py:448 happens to put anthropic first; one
# careless re-ordering of that line would silently swap the default.
# Naming "anthropic" explicitly makes "the default tab is Anthropic"
# a contract, not an accident — pairs with the ``"selected":
# "anthropic"`` literal in providers/__init__.py::_build_default_config.
_DEFAULT_FALLBACK_PROVIDER = "anthropic"
_selected_provider = get_selected_provider()
if _selected_provider not in _available_providers:
    _selected_provider = (
        _DEFAULT_FALLBACK_PROVIDER
        if _DEFAULT_FALLBACK_PROVIDER in _available_providers
        else _available_providers[0]
    )


def _force_refresh_selected() -> None:
    """Manual-refresh button hook. Re-fetches whichever provider the
    user is currently looking at, not the auto-detected default."""
    selected = expanded.selected_provider_name() if "expanded" in globals() else _selected_provider
    quota_engine.force_refresh(provider_name=selected)


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
    per_model = usage_registry.get_session_per_model(sess_uuid)
    started_at = session_state_reader.parse_started_at(state.get("startedAt"))
    # Fallback to the earliest JSONL timestamp when sessions JSON is absent
    # (MiniMax sessions don't write ~/.claude/sessions/<pid>.json).
    if started_at is None:
        started_at = meta.get("started_at")
    return SessionDetails(
        session=session,
        name=state.get("name") if isinstance(state.get("name"), str) else None,
        ai_title=meta.get("ai_title"),
        git_branch=meta.get("git_branch"),
        last_prompt=meta.get("last_prompt"),
        started_at=started_at,
        status=state.get("status") if isinstance(state.get("status"), str) else None,
        cc_version=state.get("version") or meta.get("version"),
        cost_usd=cost,
        turn_count=turns,
        sidechain_count=sides,
        per_model=per_model,
        effective_uuid=sess_uuid or None,
    )


controller = IslandController()
capsule = CapsuleWindow(controller)
expanded = ExpandedWindow(
    capsule=capsule,
    controller=controller,
    get_usage_totals=usage_registry.get_totals,
    get_totals_by_model=usage_registry.get_totals_by_model,
    get_quota_snapshot=_get_quota_snapshot,
    on_refresh_clicked=_force_refresh_selected,
    get_session_details=_build_session_details,
    available_providers=_available_providers,
    selected_provider=_selected_provider,
    on_provider_selected=_on_provider_tab_clicked,
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
