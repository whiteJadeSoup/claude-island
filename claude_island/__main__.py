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
from pathlib import Path

from platformdirs import user_data_dir
from PySide6.QtCore import QtMsgType, qInstallMessageHandler
from PySide6.QtWidgets import QApplication


def _qt_message_filter(msg_type: QtMsgType, _ctx, message: str) -> None:
    """Filter Qt's stylesheet-noise warnings out of stderr.

    Qt prints "QFont::setPointSize: Point size <= 0 (-1)" any time a
    stylesheet sets ``font-size`` in pixels (which we do extensively
    for layout reasons — pt scaling at 1.5× DPI looks fuzzy). The
    warning is harmless — Qt clamps the value internally — but it
    spams stderr enough to drown out real diagnostics. Suppress it
    while passing every other Qt log line through unchanged so we
    don't accidentally mute something useful.
    """
    text = str(message) if message is not None else ""
    suppressed_substrings = (
        "QFont::setPointSize",
        "This plugin does not support raise()",  # WindowsWindow noise
    )
    if any(s in text for s in suppressed_substrings):
        return
    # Forward everything else to stderr so genuine warnings still surface.
    print(text, file=sys.stderr)


qInstallMessageHandler(_qt_message_filter)

# ---------------------------------------------------------------------------
# Section 1: Core layer (no Qt, no OS APIs)
# ---------------------------------------------------------------------------
from claude_island.core.jsonl_parser import JsonlParser
from claude_island.core.session_registry import SessionRegistry
from claude_island.core.snapshot import Snapshotter, world
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
# Kick off parallel backfill immediately — it runs during Qt construction
# (~300ms) and finishes well before the user notices the USAGE card.
# Workers parse different files concurrently via per-file locks; the
# pool is daemon-threaded so it never blocks shutdown.
jsonl_parser.start_backfill_pool()

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
from claude_island.platform_ import session_names as session_names_store
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
from claude_island.ui.world_marshaler import WorldMarshaler

import reactivex.operators as ops
from PySide6.QtCore import QObject, Qt, Signal


class _ControllerMarshaler(QObject):
    """Qt Signal bridge marshaling session-list updates from any thread
    onto the Qt main thread before they reach the IslandController.

    Why: ``session_registry.sessions_changed.on_next(...)`` fires
    synchronously on whichever thread called ``update()`` — typically
    the process scanner's worker thread. ``IslandController`` is a
    QObject with a transitions state machine; its mutator
    ``on_sessions_updated`` should run on the Qt main thread so the
    state-machine callbacks (which emit Qt Signals to the UI) end up
    on the right thread without surprise queueing.

    Constructed on the Qt main thread, so the QueuedConnection always
    crosses to the main thread when emit is on a worker thread.
    """

    sessions_ready: Signal = Signal(object)

    def __init__(self, controller: IslandController) -> None:
        super().__init__()
        self.sessions_ready.connect(
            controller.on_sessions_updated,
            Qt.ConnectionType.QueuedConnection,
        )


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
    """User clicked a provider tab. Persist the choice and poke the
    snapshotter so the new provider's quota lands on screen without
    waiting for the next 60 s heartbeat.

    Phase G1: was two refresh_xxx calls; now a single snapshotter.wake().
    The next snap will pick up the new selected_provider via the
    ``get_selected_provider`` closure injected at Snapshotter
    construction, fetch its cached quota, and push to render(snap).

    No ``force_refresh`` here: that would block the UI thread on a
    3 s HTTP timeout per click. The provider's disk cache (5 min TTL)
    and the engine's in-memory cache (90 s TTL) make tab switches
    essentially instant from the user's perspective. The manual ↻
    button still exists for cases where the user wants to force a
    network fetch."""
    set_selected_provider(name)
    if "snapshotter" in globals():
        snapshotter.wake()


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
    """Manual-refresh button hook. Force a network fetch (bypasses
    the QuotaProvider's TTL) and poke the snapshotter so the fresh
    snapshot reaches both UI surfaces in one go.

    Phase G1: was an explicit refresh_quota call on the capsule; now
    just wake the snapshotter — the next snap rebuild reads the
    freshly-fetched quota and pushes it through render(snap)."""
    selected = expanded.selected_provider_name() if "expanded" in globals() else _selected_provider
    quota_engine.force_refresh(provider_name=selected)
    if "snapshotter" in globals():
        snapshotter.wake()


def _on_provider_config_changed() -> None:
    """In-app + dialog persisted a new provider's credentials. Re-run
    detection (the new providers.json entry is on disk now, the next
    detect() call will see it), push the updated list to the panel so
    its tab strip rebuilds, and force a quota refresh so the user sees
    real numbers in the new tab within ~1 second instead of waiting
    for the next heartbeat tick."""
    if "expanded" not in globals():
        return
    new_available = _resolve_available_providers()
    # Honour the user's stored selection if still valid; otherwise
    # default to the explicit anthropic fallback (matches startup logic).
    stored = get_selected_provider()
    if stored in new_available:
        selected = stored
    elif _DEFAULT_FALLBACK_PROVIDER in new_available:
        selected = _DEFAULT_FALLBACK_PROVIDER
    elif new_available:
        selected = new_available[0]
    else:
        selected = None
    expanded.set_available_providers(new_available, selected=selected)
    if selected:
        quota_engine.force_refresh(provider_name=selected)
    # Phase G1: poke the snapshotter so the new provider list /
    # quota land in render(snap) on the next tick.
    if "snapshotter" in globals():
        snapshotter.wake()


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
    # User's custom name (set via the detail popup's edit affordance)
    # wins over Claude Code's auto-generated name. Falls through to the
    # state-file name when not overridden, then to ai_title / basename
    # downstream in the UI. Strict per-session lookup — the per-project
    # fallback was removed because it bled renames across siblings.
    custom_name = session_names_store.get_session_name(sess_uuid or "")
    state_name = state.get("name") if isinstance(state.get("name"), str) else None
    return SessionDetails(
        session=session,
        name=custom_name or state_name,
        original_name=state_name,
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
        latest_model=usage_registry.get_latest_model(sess_uuid) if sess_uuid else None,
        effective_uuid=sess_uuid or None,
    )


controller = IslandController()
capsule = CapsuleWindow(
    controller,
    # Pull today's cumulative spend lazily — usage_registry is rebuilt
    # from JSONL on every start, so the call is just a list comprehension
    # over in-memory records (cheap, sub-ms at typical user scale).
    get_today_cost=lambda: usage_registry.get_totals("today").cost_usd,
    # Capsule shows the running session's name when exactly one is
    # active. Reuse the same composer the panel rows use so the pill
    # picks up custom renames + ai-titles consistently.
    get_session_details=_build_session_details,
    # Quota snapshot for the mini progress bar. Same closure pattern
    # as expanded.refresh_usage_bar — reads the panel's selected
    # provider so a tab click in the panel propagates to the pill.
    get_quota_snapshot=_get_quota_snapshot,
)
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
    on_provider_config_changed=_on_provider_config_changed,
)

# ---------------------------------------------------------------------------
# Section 4: Source → controller wiring
#
# After Phase G2, UI rendering is driven SOLELY by the WorldSnapshot
# broadcast pipeline (Section 4b). The only thing wired here is the
# controller's session-list update — internal state used by the
# IslandController state machine (dot ↔ collapsed ↔ expanded
# transitions). The Snapshotter handles the rest: it wakes on the same
# source events (sessions_changed, totals_changed) and pushes a
# snapshot that drives render(snap) on every subscribed UI surface.
# ---------------------------------------------------------------------------

_controller_marshaler = _ControllerMarshaler(controller)  # pin reference
session_registry.sessions_changed.subscribe(_controller_marshaler.sessions_ready.emit)

# core → core direct subscription: JSONL activity feeds the session registry's
# override map. update_activity is thread-safe and does not emit, so there is
# no need to marshal through Qt — the parser thread can call it directly.
jsonl_parser.activity_updated.subscribe(session_registry.update_activity)

# ---------------------------------------------------------------------------
# Section 4b: WorldSnapshot broadcast (Phase E — runs IN PARALLEL with the
# legacy wiring above).
#
# Architecture:
#
#   sources ── wake() ──→ Snapshotter (worker thread)
#                              │
#                              │ build → publish=marshaler.snap_ready.emit
#                              ▼
#                       WorldMarshaler (Qt main thread, QueuedConnection)
#                              │
#                              ▼
#                       world.push(snap)        ← all on Qt main thread
#                              │
#                              │ pipe(distinct_until_changed(render_key))
#                              ▼
#               capsule.render(snap), expanded.render(snap)
#
# The legacy refresh_xxx slots above stay wired during Phase E/F so behaviour
# can be visually compared. Phase G deletes the legacy slots, the QtBridge,
# and Event[T] entirely.
# ---------------------------------------------------------------------------

_world_marshaler = WorldMarshaler()  # pin reference: QObject lifetime

snapshotter = Snapshotter(
    session_source=session_registry,
    state_reader=session_state_reader,
    metadata_provider=jsonl_parser,
    usage_registry=usage_registry,
    names_store=session_names_store,
    get_quota=_get_quota_snapshot,
    get_available_providers=_resolve_available_providers,
    get_selected_provider=lambda: (
        expanded.selected_provider_name() if "expanded" in globals() else _selected_provider
    ),
    publish=_world_marshaler.snap_ready.emit,
    debounce_window_s=0.1,
    throttle_first_window_s=0.2,
)

def _safe_render(target_name: str, render_fn):
    """Wrap a render callable so an exception inside render() is logged
    but never propagates upstream as ``on_error``.

    Why this matters: reactivex's contract is that once ``on_error``
    fires, the subscription terminates and no future ``on_next`` will
    reach the subscriber. After Phase G that subscription is the UI's
    sole rendering input — one render-time bug = capsule (or panel)
    permanently frozen until restart, while everything else still
    runs. Catching inside ``on_next`` keeps the stream alive: the
    next snap that comes through gets another chance to render.
    """
    def _safe(snap):
        try:
            render_fn(snap)
        except Exception as exc:
            print(
                f"[claude-island] {target_name}.render(snap) raised "
                f"(stream preserved): {exc}",
                file=sys.stderr,
            )
    return _safe


# UI subscription pipelines: distinct_until_changed against render_key
# (excludes fetched_at) so periodic ticks producing identical data don't
# trigger no-op re-renders. observe_on is NOT needed — world.push runs
# on the Qt main thread (because WorldMarshaler.QueuedConnection
# guarantees that), so subscribers fire on the main thread by default.
#
# render() is wrapped in _safe_render so a render-time exception is
# logged but the subscription stays alive — Rx's on_error would
# otherwise terminate the stream on the first failure and leave the
# UI permanently frozen. on_error is still wired as a backstop for
# upstream pipeline failures (which terminate regardless), but render
# bugs no longer reach it.
_capsule_subscription = (
    world.observable()
    .pipe(ops.distinct_until_changed(key_mapper=lambda s: s.render_key()))
    .subscribe(
        on_next=_safe_render("capsule", capsule.render),
        on_error=lambda e: print(
            f"[claude-island] capsule pipeline died (upstream error): {e}",
            file=sys.stderr,
        ),
    )
)
_expanded_subscription = (
    world.observable()
    .pipe(ops.distinct_until_changed(key_mapper=lambda s: s.render_key()))
    .subscribe(
        on_next=_safe_render("expanded", expanded.render),
        on_error=lambda e: print(
            f"[claude-island] expanded pipeline died (upstream error): {e}",
            file=sys.stderr,
        ),
    )
)

# Wake hooks: every legacy event source also pokes the snapshotter so a
# JSONL write / process scan triggers a snap rebuild within the debounce
# window. wake() is thread-safe — no QtBridge marshaling needed.
session_registry.sessions_changed.subscribe(lambda _: snapshotter.wake())
usage_registry.totals_changed.subscribe(lambda _: snapshotter.wake())
# jsonl_parser.activity_updated already feeds session_registry, which then
# emits sessions_changed → wakes via the line above. No extra hook needed.

snapshotter.start()
# Boot the UI with one snapshot synchronously so capsule + panel render
# real data on first paint instead of the empty default. publish is the
# marshaler emit, so this enqueues a push on the Qt main thread that
# fires once app.exec() begins spinning the event loop.
_world_marshaler.snap_ready.emit(snapshotter.build_now())

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

# Backfill runs in a thread pool started immediately after jsonl_parser
# construction (above Section 2). No daemon thread needed here.

# 60s heartbeat: tick the 5h reset countdown and pull a fresh quota
# snapshot. QuotaProvider gates HTTP internally on its 300s TTL, so
# this only issues a network call every 5 min. After Phase G1 the
# heartbeat just pokes the snapshotter — the rebuild flow handles
# the rest (fresh quota → new snapshot → distinct_until_changed →
# render(snap) on both surfaces).
_usage_heartbeat = QTimer()
_usage_heartbeat.timeout.connect(snapshotter.wake)
_usage_heartbeat.start(60_000)

# UI first — capsule shows immediately. All process scanning happens
# off the Qt main thread so neither psutil enumeration nor the slow
# Win32 AttachConsole probe inside _filter_orphans can stall the UI.
capsule.show()


def _bootstrap_session_discovery() -> None:
    """Background-thread bootstrap for the session pipeline.

    Two phases on the worker thread:
      1. scan_fast() — pure psutil, no Win32. Sessions appear in the
         UI within ~200ms because session_registry.update marshals
         sessions_changed back to the Qt main thread via QtBridge.
      2. session_discovery.start() — runs one full scan() (with the
         orphan filter that costs ~1-2s) and arms the periodic
         10-second timer. Doing this on the worker thread means the
         user sees the fast-scan result instantly and the full
         filtered list lands a second later, while the Qt main thread
         stays responsive throughout startup.
    """
    try:
        sessions = process_scanner.scan_fast()
        session_registry.update(sessions)
    except Exception as exc:
        import sys as _sys
        print(f"[claude-island] fast scan failed: {exc}", file=_sys.stderr)
    # session_discovery.start() runs the first scan() synchronously
    # then arms a Timer for periodic ticks; both stay on this worker.
    session_discovery.start()


import threading as _threading
_threading.Thread(target=_bootstrap_session_discovery, daemon=True).start()

# Periodic cleanup of session_names.json — drop overrides whose
# session_uuid no longer corresponds to any transcript on disk so
# the file doesn't accumulate dead entries from sessions the user
# renamed and then closed permanently. Cadence is generous (every
# 6 hours) because the work is cheap, the file is tiny, and stale
# entries are harmless until the next rename anyway. First fire is
# also delayed 6 hours, by which time backfill_all has finished
# and known_session_uuids() returns a complete picture.
#
# The actual gc runs on a daemon thread because both the rglob over
# ~/.claude/projects/ and the read-modify-write of the names file
# touch disk — a slow disk shouldn't be able to stutter the Qt main
# thread. The QTimer just dispatches; the worker does the work.
def _gc_session_names_tick() -> None:
    def _work() -> None:
        try:
            session_names_store.gc_session_names(jsonl_parser.known_session_uuids())
        except Exception as exc:
            import sys as _sys
            print(f"[claude-island] session_names gc failed: {exc}", file=_sys.stderr)
    _threading.Thread(target=_work, daemon=True).start()


_session_names_gc_timer = QTimer()
_session_names_gc_timer.timeout.connect(_gc_session_names_tick)
_session_names_gc_timer.start(6 * 60 * 60 * 1000)  # 6 hours

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

# Stop the snapshotter first so a tick fired during shutdown doesn't
# try to read from registries that are about to close. dispose() is
# idempotent on the underlying subscriptions.
snapshotter.stop()
_capsule_subscription.dispose()
_expanded_subscription.dispose()

session_discovery.stop()
file_watcher.stop()
jsonl_parser.request_stop()

sys.exit(exit_code)
