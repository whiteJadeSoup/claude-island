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

# Install the stderr noise filter BEFORE importing Qt / pyobjc so the
# pipe redirect catches every C-level write to FD 2 — Qt's font
# subsystem and macOS Input Method Kit both emit harmless lines that
# we drop here. No-op on non-darwin platforms.
from claude_island.platform_.stderr_noise_filter import install as _install_stderr_filter
_install_stderr_filter()


def _hide_from_macos_dock() -> None:
    """Mutate the running app's NSBundle info dict so macOS treats us
    as a background-only LSUIElement — no dock icon, no Cmd-Tab entry,
    no menu-bar title.

    Why: launching via ``python -m claude_island`` (or ``uv run``) makes
    macOS show the generic Python file icon labelled "python3", which
    is both ugly and confusing for users who don't know they're running
    Python under the hood. The floating capsule is already the app's
    persistent affordance — a redundant dock entry adds clutter without
    adding capability. This matches the menu-bar / floating-utility
    convention used by Bartender, BetterTouchTool, Ice, Alfred's
    background mode, and the ActivityWatch tray app.

    Must run BEFORE QApplication() is constructed: Qt instantiates
    NSApplication during QApplication init, which freezes the activation
    policy. Mutating ``infoDictionary`` after that point has no effect.

    Graceful degrade: if pyobjc isn't installed (manual install or
    explicit opt-out) we silently skip — the user sees the original
    ugly Python icon but the app is otherwise unaffected.
    """
    if sys.platform != "darwin":
        return
    try:
        from AppKit import NSBundle  # type: ignore[import-not-found]
    except ImportError:
        return
    bundle = NSBundle.mainBundle()
    info = bundle.infoDictionary()
    if info is not None:
        # ``LSUIElement`` is the correct flag for an "accessory" GUI app:
        # hidden from dock + Cmd-Tab + menu-bar app title, but windows
        # can still take keyboard focus. ``LSBackgroundOnly`` looks
        # similar but tells macOS we are a daemon with no UI — under
        # that policy NSApplication refuses to make our windows key,
        # which silently breaks every keyboard-driven affordance (arrow
        # nav, Enter, search input). A previous revision set both as a
        # "belt and braces" measure; that's wrong — the two flags
        # express different intents and combining them inherits the
        # more restrictive one.
        info["LSUIElement"] = "1"


def _apply_macos_accessory_policy() -> None:
    """Call ``NSApp.setActivationPolicy_(Accessory)`` after QApplication
    has been created.

    The infoDictionary mutation in ``_hide_from_macos_dock`` runs BEFORE
    NSApplication is loaded so the early activation policy decision is
    correct, but on some macOS versions the launcher's cached state
    overrides our infoDict mutation and the app still ends up in the
    default Regular policy. The runtime ``setActivationPolicy_`` call
    is authoritative — once QApplication has constructed NSApplication,
    we tell it explicitly to switch to Accessory mode. Belt + braces:
    one of the two paths always wins.
    """
    if sys.platform != "darwin":
        return
    try:
        from AppKit import (  # type: ignore[import-not-found]
            NSApp,
            NSApplicationActivationPolicyAccessory,
        )
    except ImportError:
        return
    try:
        NSApp.setActivationPolicy_(NSApplicationActivationPolicyAccessory)
    except Exception:
        pass


_hide_from_macos_dock()

from platformdirs import user_data_dir
from PySide6.QtCore import QtMsgType, qInstallMessageHandler
from PySide6.QtWidgets import QApplication


from claude_island.core.safe_stderr import safe_stderr_write as _safe_stderr_write


def _qt_message_filter(msg_type: QtMsgType, _ctx, message: str) -> None:
    """Filter Qt's stylesheet-noise warnings out of stderr.

    Qt prints "QFont::setPointSize: Point size <= 0 (-1)" any time a
    stylesheet sets ``font-size`` in pixels (which we do extensively
    for layout reasons — pt scaling at 1.5× DPI looks fuzzy). The
    warning is harmless — Qt clamps the value internally — but it
    spams stderr enough to drown out real diagnostics. Suppress it
    while passing every other Qt log line through unchanged so we
    don't accidentally mute something useful.

    QWindowsWindow::setGeometry warnings fire on multi-monitor /
    high-DPI setups when a frameless popup's natural minimumSizeHint
    is recomputed after first paint and ends up a few pixels taller
    than what was originally requested. Cosmetic — the popup paints
    correctly — but extremely noisy. Suppressed.
    """
    text = str(message) if message is not None else ""
    suppressed_substrings = (
        "QFont::setPointSize",
        "This plugin does not support raise()",  # WindowsWindow noise
        # Frameless popups (SessionDetailPopup, RecentsDrawer) trigger
        # this when Qt's predicted sizeHint underestimates the post-
        # layout content height by a few pixels. The OS clamps up to
        # the actual minimum, which is the correct behaviour, but Qt
        # still logs it as an "unable to set geometry" warning. Visual
        # result is identical to a successful set; suppress to stop
        # the spam.
        "QWindowsWindow::setGeometry: Unable to set geometry",
    )
    if any(s in text for s in suppressed_substrings):
        return
    _safe_stderr_write(text)


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
from claude_island.platform_.process_scanner import ProcessScanner, resume_uuid_for_pid
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
from claude_island.platform_.app_backend import LocalAppBackend
from claude_island.platform_.dispatcher import TerminalDispatcher
from claude_island.platform_.terminals import build_registry
from claude_island.platform_.os import get_os_backend


def _resume_uuid_reader(pid: int) -> str | None:
    """Production helper threaded into every consumer of the OLD-uuid
    lookup (Snapshotter / compose_session_view, _build_session_details,
    HookServer). Wraps :func:`resume_uuid_for_pid` so the names-store
    reverse lookup is injected once here — the helper's two-step
    resolution (cmdline UUID, then ``--resume <name>`` → uuid via
    session_names.json) stays internal to platform_, and callers just
    pass this single callable around."""
    return resume_uuid_for_pid(
        pid, names_lookup=session_names_store.get_uuid_by_name,
    )


process_scanner = ProcessScanner()
file_watcher = FileWatcher()
session_discovery = SessionDiscovery(
    scanner=process_scanner,
    registry=session_registry,
)
# Three-port dispatcher (groups sessions via adapter chain; dispatches
# UI actions by scope). Two outbound wirings:
# - data path: dispatcher.group_sessions injected into Snapshotter
# - control path: dispatcher.dispatch injected into ExpandedWindow
_app_backend = LocalAppBackend(
    names_store=session_names_store,
    claude_projects_dir=_CLAUDE_PROJECTS,
    on_change=lambda: snapshotter.wake() if "snapshotter" in globals() else None,
)
_dispatcher = TerminalDispatcher(
    terminals=build_registry(),
    os_backend=get_os_backend(),
    app_backend=_app_backend,
)
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
# Authoritative dock-hide call — must come AFTER QApplication() because
# it operates on the live NSApplication instance Qt just constructed.
# Pairs with ``_hide_from_macos_dock`` above (which seeds the early
# launch decision via NSBundle.infoDictionary). No-op on non-darwin.
_apply_macos_accessory_policy()

# Force the Fusion style on macOS so QSS is honoured for QToolTip.
#
# Why this matters: macOS's native Qt style routes tooltip rendering
# through Cocoa's NSToolTip, which silently ignores ``QToolTip { ... }``
# QSS rules — and ALSO silently ignores ``QToolTip.setPalette`` for
# ToolTipBase / ToolTipText. A dark-themed QSS plus a setPalette pair
# both pass without error and look correct in code review, but the
# user sees the system light-gray tooltip on a dark drawer. The only
# reliable fix is to swap out the platform style for Fusion (Qt's
# cross-platform style) which honours QSS for every widget including
# tooltips. The visible blast radius is small here because every
# top-level surface in this app is frameless + heavily QSS-styled —
# Fusion replaces only the unstyled bits (default focus rings, scroll
# bar arrows, etc), and those changes are barely noticeable.
#
# On Windows / Linux the platform style already honours the QToolTip
# QSS rule, so swap is unnecessary and skipped — keeping native widget
# look-and-feel where it works.
if sys.platform == "darwin":
    app.setStyle("Fusion")

# Global tooltip styling — both paths together (see tooltip_style.py
# for the rationale; tl;dr Qt's QSS resolution for QToolTip on macOS
# Fusion is flaky depending on widget flags, so we belt-and-braces it
# with the palette path which works at a lower level).
from claude_island.ui.tooltip_style import TOOLTIP_QSS, apply_tooltip_palette
apply_tooltip_palette(app)
app.setStyleSheet(TOOLTIP_QSS)

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
    # uuid resolution mirrors ``core.snapshot.compose_session_view``:
    # ``claude --resume <OLD_UUID>`` makes claude.exe assign a NEW
    # in-memory uuid (visible in pid.json) but keep writing transcripts
    # to OLD's JSONL — so UsageRegistry's records live under OLD. We
    # must check the cmdline FIRST or the per-row $ aggregate +
    # latest_model lookup miss entirely.
    pid_json_uuid = (
        state.get("sessionId")
        if isinstance(state.get("sessionId"), str)
        else None
    )
    try:
        cmdline_resume_uuid = _resume_uuid_reader(session.pid)
    except Exception:
        cmdline_resume_uuid = None
    sess_uuid = cmdline_resume_uuid or pid_json_uuid or session.session_uuid
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
)
# Provider-settings hooks: wrap the platform_.providers module-level
# functions and inject them so the UI layer never imports platform code
# (import-linter contract). _list_configurable_providers normalises the
# (name, default_config) shape so the UI can stay free of `cls.default_config`
# discovery logic.
def _list_configurable_providers() -> list[tuple[str, dict]]:
    from claude_island.platform_.providers import all_providers
    out: list[tuple[str, dict]] = []
    for name, cls in all_providers().items():
        cfg_fn = getattr(cls, "default_config", None)
        if cfg_fn is None:
            continue
        try:
            cfg = cfg_fn()
        except Exception:
            cfg = None
        if isinstance(cfg, dict):
            out.append((name, cfg))
    return out


def _save_provider_settings(name: str, fields: dict) -> None:
    from claude_island.platform_.providers import set_provider_settings
    set_provider_settings(name, fields)


def _delete_provider_settings(name: str) -> None:
    from claude_island.platform_.providers import (
        delete_provider_settings as _del,
    )
    _del(name)


# ── Bidirectional Hooks v1 (2026-05-14) ──────────────────────────────
# Constructed BEFORE ExpandedWindow + HookServer so both can be injected
# with the same registry / cache / queue instances. Snapshotter is built
# AFTER ExpandedWindow (line ~700), so on_change uses lazy lookup so an
# early mutation (extremely unlikely at this point in boot) doesn't
# NameError on a not-yet-bound snapshotter.
from claude_island.core.notify import NotifyEventQueue
from claude_island.core.pending_decisions import PendingDecisionRegistry
from claude_island.core.session_permissions import SessionPermissionCache

def _wake_if_ready() -> None:
    snap = globals().get("snapshotter")
    if snap is not None:
        snap.wake()

pending_registry = PendingDecisionRegistry(on_change=_wake_if_ready)
permission_cache = SessionPermissionCache(on_change=_wake_if_ready)
notify_queue = NotifyEventQueue(on_change=_wake_if_ready)


def _resolve_pending_decision(decision_id: str, decision: object) -> bool:
    """Bridge from ApprovalCard / PromptReviewCard click → registry.

    The registry resolve sets a threading.Event the HookServer thread
    is blocked on (in PreToolUse / UserPromptSubmit handlers). The
    server then encodes the directive in the HTTP response body,
    Claude Code reads stdout, and the tool / prompt proceeds."""
    return pending_registry.resolve(decision_id, decision)  # type: ignore[arg-type]


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
    # Capability dispatch (FOCUS / REVEAL_CWD / RENAME / RESET_THINKING).
    # The dispatcher routes each by scope: TERMINAL → adapter chain,
    # OS → os_backend, APP → app_backend. UI calls this for every
    # user-triggered action and never imports platform code itself.
    dispatch=_dispatcher.dispatch,
    list_configurable_providers=_list_configurable_providers,
    save_provider_settings=_save_provider_settings,
    delete_provider_settings=_delete_provider_settings,
    # Bidirectional Hooks v1: ApprovalCard / PromptReviewCard clicks
    # route through the registry (sets the Event the HookServer thread
    # is blocked on); SessionDetailPopup's "Review prompts" toggle
    # writes to the permission cache.
    resolve_decision=_resolve_pending_decision,
    get_review_mode=permission_cache.is_review,
    set_review_mode=permission_cache.set_review,
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
#                              │ per-surface dedup (F4):
#                              │   capsule  → map(compute) | distinct | render(data)
#                              │   expanded → distinct(key_mapper=compute) | render(snap)
#                              ▼
#               capsule.render(data), expanded.render(snap)
#
# The legacy refresh_xxx slots above stay wired during Phase E/F so behaviour
# can be visually compared. Phase G deletes the legacy slots, the QtBridge,
# and Event[T] entirely.
# ---------------------------------------------------------------------------

_world_marshaler = WorldMarshaler()  # pin reference: QObject lifetime

# ── resume-offline (Phase 5) sources ─────────────────────────────────────
# DormantSessionSource is a thin view over JsonlParser._session_meta +
# UsageRegistry — it doesn't open files of its own, so construction is
# essentially free. LaunchIntentRegistry is similarly lightweight.
# Both are passed into Snapshotter so reconcile can run; they're kept
# in module scope so RecentsDrawer can also write to launch_intent.
from claude_island.core.dormant_source import DormantSessionSource
from claude_island.core.launch_intent import LaunchIntentRegistry

dormant_source = DormantSessionSource(
    jsonl_parser=jsonl_parser, usage_registry=usage_registry,
)
launch_intent = LaunchIntentRegistry()

# ── Hook pipeline (Step 8) ────────────────────────────────────────────────
# Hooks complement the scanner: scanner = discovery + liveness fallback;
# hooks = real-time phase / tool / prompt push. See Detail Design v2.
#
# Boot order:
#   1. sync_hook_script: copy bundled hook.py to ~/.claude-island/hook.py
#      so settings.json's command points at a stable absolute path.
#   2. install_if_needed: idempotently merge our hook entries into
#      ~/.claude/settings.json, preserving user's existing hooks.
#   3. Build SessionStateMachine (pure reducer).
#   4. Start HookServer on 127.0.0.1:<port>. If listener bind fails
#      across the retry range we degrade to scanner-only — Claude
#      hooks will fail-open and the UI continues working off pid.json.
#   5. Build HookSessionBridge to wire state_machine ↔ session_registry
#      (placeholder upsert for race + tombstone for crashed processes).
#   6. Wire state_machine.live_state_changed → snapshotter.wake (done
#      AFTER Snapshotter is constructed below).
from importlib import resources

from claude_island.core.session_state_machine import SessionStateMachine
from claude_island.platform_ import hook_installer
from claude_island.platform_.hook_server import HookServer, HookServerStartError
from claude_island.platform_.hook_session_bridge import HookSessionBridge

state_machine = SessionStateMachine()
hook_server: HookServer | None = None
hook_bridge: HookSessionBridge | None = None

try:
    # Step 1: sync the bundled hook script to ~/.claude-island/hook.py
    with resources.as_file(
        resources.files("claude_island") / "hook.py"
    ) as bundled_hook:
        dest_hook = Path.home() / ".claude-island" / "hook.py"
        try:
            hook_installer.sync_hook_script(
                bundled_script=Path(bundled_hook), dest=dest_hook,
            )
        except OSError as e:
            _safe_stderr_write(
                f"[claude-island] could not sync hook.py to {dest_hook}: {e}; "
                f"hooks disabled this session"
            )
            raise

    # Step 2: write our hook entries into ~/.claude/settings.json
    hook_command = hook_installer.build_hook_command(
        python_exe=sys.executable,
        hook_script=dest_hook,
    )
    try:
        result = hook_installer.install_if_needed(
            settings_path=Path.home() / ".claude" / "settings.json",
            hook_command=hook_command,
        )
        if result.changed:
            _safe_stderr_write(
                f"[claude-island] installed Claude Code hooks "
                f"({len(result.installed_events)} events); "
                f"preserved {result.user_hooks_preserved} user hook(s)"
            )
    except hook_installer.InstallError as e:
        _safe_stderr_write(
            f"[claude-island] could not install hooks in settings.json: {e}"
        )
        # Continue anyway — listener still works if user pre-installed hooks

    # Step 4: start the HTTP listener with bidirectional deps.
    # ``resume_uuid_reader`` makes the server rewrite incoming session_id
    # to the OLD uuid recovered from cmdline (``--resume <UUID>`` directly
    # or ``--resume <name>`` via session_names reverse lookup) so the
    # state machine ends up keyed on the same uuid UsageRegistry uses.
    # See ``platform_.process_scanner.resume_uuid_for_pid``.
    hook_server = HookServer(
        state_machine,
        pending_registry=pending_registry,
        permission_cache=permission_cache,
        notify_queue=notify_queue,
        resume_uuid_reader=_resume_uuid_reader,
    )
    try:
        bound_port = hook_server.start()
        _safe_stderr_write(
            f"[claude-island] hook listener bound on 127.0.0.1:{bound_port}"
        )
    except HookServerStartError as e:
        _safe_stderr_write(
            f"[claude-island] hook listener failed to start: {e}; "
            f"degrading to scanner-only (phase will come from pid.json)"
        )
        hook_server = None

    # Step 5: wire registry ↔ state_machine via the bridge.
    hook_bridge = HookSessionBridge(
        registry=session_registry, state_machine=state_machine,
    )
except Exception as e:
    # Catastrophic boot failure of the hook subsystem — degrade gracefully.
    _safe_stderr_write(
        f"[claude-island] hook subsystem failed to initialize ({e!r}); "
        f"running scanner-only"
    )

snapshotter = Snapshotter(
    session_source=session_registry,
    state_reader=session_state_reader,
    metadata_provider=jsonl_parser,
    usage_registry=usage_registry,
    names_store=session_names_store,
    live_state_reader=state_machine.read,
    # ``resume_uuid_reader`` lets compose_session_view surface the OLD
    # uuid (recovered from ``claude --resume <UUID>`` cmdline or from
    # ``--resume <name>`` via the session_names store) so the
    # UsageRegistry lookup hits the right key after a resume.
    resume_uuid_reader=_resume_uuid_reader,
    get_quota=_get_quota_snapshot,
    get_available_providers=_resolve_available_providers,
    get_selected_provider=lambda: (
        expanded.selected_provider_name() if "expanded" in globals() else _selected_provider
    ),
    publish=_world_marshaler.snap_ready.emit,
    group_sessions=_dispatcher.group_sessions,
    dormant_source=dormant_source,
    launch_intent=launch_intent,
    pending_decisions=pending_registry,
    notify_queue=notify_queue,
    debounce_window_s=0.1,
    throttle_first_window_s=0.2,
)

# Step 6: hook events drive snapshotter wakes. live_state_changed fires
# on the HookServer worker thread; snapshotter.wake() is thread-safe
# (BehaviorSubject mutex) and debounces internally.
state_machine.live_state_changed.subscribe(
    on_next=lambda _: snapshotter.wake(),
    on_error=lambda e: _safe_stderr_write(
        f"[claude-island] live_state_changed subscription died: {e!r}"
    ),
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
            _safe_stderr_write(
                f"[claude-island] {target_name}.render(snap) raised "
                f"(stream preserved): {exc}"
            )
    return _safe


# UI subscription pipelines: per-surface dedup (F4). Each surface has
# its own ``compute(snap)`` declaring what it cares about; distinct
# tracks exactly those reads. observe_on is NOT needed — world.push
# runs on the Qt main thread (WorldMarshaler.QueuedConnection
# guarantees that), so subscribers fire on the main thread by default.
#
# render() is wrapped in _safe_render so a render-time exception is
# logged but the subscription stays alive — Rx's on_error would
# otherwise terminate the stream on the first failure and leave the
# UI permanently frozen. on_error is still wired as a backstop for
# upstream pipeline failures (which terminate regardless), but render
# bugs no longer reach it.
# Per-surface dedup (F4):
#
# Each surface declares what it cares about via its own ``compute(snap)``
# function — the function body itself is the dedup contract. compute
# reads what it needs; dedup tracks exactly those reads; render is
# only invoked when the resulting projection actually changed.
#
# Two flow shapes coexist by design:
#
# * capsule — data-flow: ``map(compute) → distinct → render(data)``.
#   Capsule's data is small (4 scalars) and rendering is pure painting,
#   so feeding ``data`` directly through is ergonomic and forces
#   render to never read snap. Used by simple surfaces.
#
# * expanded — key-extractor: ``distinct_until_changed(key_mapper=compute)
#   → render(snap)``. Expanded's row construction needs live SessionView
#   instances (FOCUS dispatch, sibling lookups), so the snap must
#   stay reachable. compute is the dedup key extractor; render is
#   unchanged. Used by complex surfaces that need raw data.
#
# Both shapes give F4's core property: dedup precision == display
# precision, because compute reads what render renders.
_capsule_subscription = (
    world.observable()
    .pipe(
        ops.map(capsule.compute),
        ops.distinct_until_changed(),
    )
    .subscribe(
        on_next=_safe_render("capsule", capsule.render),
        on_error=lambda e: _safe_stderr_write(
            f"[claude-island] capsule pipeline died (upstream error): {e}"
        ),
    )
)
_expanded_subscription = (
    world.observable()
    .pipe(ops.distinct_until_changed(key_mapper=expanded.compute))
    .subscribe(
        on_next=_safe_render("expanded", expanded.render),
        on_error=lambda e: _safe_stderr_write(
            f"[claude-island] expanded pipeline died (upstream error): {e}"
        ),
    )
)

# ── RecentsDrawer (resume-offline) ─────────────────────────────────────
# Uses _dispatcher (already constructed at line 120 area) for the LAUNCH
# adapter lookup, launch_intent for the optimistic UI transition, and
# snapshotter.wake to trigger an immediate re-render after Resume click.
from claude_island.ui.recents_drawer import RecentsDrawer

recents_drawer = RecentsDrawer(
    expanded=expanded,
    dispatcher=_dispatcher,
    launch_intent=launch_intent,
    on_wake=snapshotter.wake,
)
expanded.set_recents_toggle(recents_drawer.toggle)
# Inject the drawer instance so the panel's auto-hide-on-focus-loss
# whitelist recognises clicks into the drawer as "still our app" and
# keeps the panel open.
expanded.set_recents_drawer(recents_drawer)

_recents_subscription = (
    world.observable()
    .pipe(ops.distinct_until_changed(key_mapper=RecentsDrawer.compute))
    .subscribe(
        on_next=_safe_render("recents", recents_drawer.render),
        on_error=lambda e: _safe_stderr_write(
            f"[claude-island] recents pipeline died (upstream error): {e}"
        ),
    )
)

# ── Bidirectional Hooks v1: NotificationDispatcher ─────────────────────
# Subscribed to world.observable like every other UI surface; the dedup
# key is the sequence of notify_event ids so a snap that doesn't add /
# remove an event doesn't re-deliver. Backend is chosen per-platform.
from claude_island.ui.notification_dispatcher import NotificationDispatcher
from claude_island.platform_.notify import (
    MacOsNotifyBackend,
    NoopNotifyBackend,
    WindowsNotifyBackend,
)

if sys.platform == "darwin":
    _notify_backend = MacOsNotifyBackend()
elif sys.platform == "win32":
    # tray_icon=None ⇒ no tray fallback (we don't have a tray yet).
    # When winrt isn't installed, the dispatcher silently drops with
    # one warn. The user adds winsdk to their pip install to enable.
    _notify_backend = WindowsNotifyBackend(tray_icon=None)
else:
    _notify_backend = NoopNotifyBackend()

_notification_dispatcher = NotificationDispatcher(backend=_notify_backend)

_notify_subscription = (
    world.observable()
    .pipe(
        ops.distinct_until_changed(
            key_mapper=lambda s: tuple(e.id for e in s.notify_events),
        ),
    )
    .subscribe(
        on_next=_safe_render("notify", _notification_dispatcher.on_snapshot),
        on_error=lambda e: _safe_stderr_write(
            f"[claude-island] notify pipeline died: {e}"
        ),
    )
)

# Periodic eviction (60 s) for stale grants + expired pending decisions.
# Runs on Qt main thread via QTimer; both registry methods are thread-safe.
from PySide6.QtCore import QTimer as _QTimer
_evict_timer = _QTimer()
_evict_timer.setInterval(60_000)
def _periodic_evict() -> None:
    try:
        n_perm = permission_cache.evict_expired()
        n_pending = pending_registry.evict_expired()
        if (n_perm + n_pending) > 0:
            _safe_stderr_write(
                f"[claude-island] evicted {n_perm} grants + "
                f"{n_pending} stale pending decisions"
            )
    except Exception as e:
        _safe_stderr_write(f"[claude-island] periodic evict raised: {e!r}")
_evict_timer.timeout.connect(_periodic_evict)
_evict_timer.start()

# Recents drawer shortcuts.  Two bindings sit alongside each other:
#   · Ctrl+H — kept for backwards compat with existing user muscle memory.
#   · Ctrl+J — added by the v3 redesign so the binding matches the
#     prototype's "open Recents" affordance ("⌘J" label on the capsule).
#     Qt's QKeySequence maps "Ctrl+..." to ⌘+... on macOS automatically;
#     same string works cross-platform.
# Both share the ApplicationShortcut context so they fire regardless of
# which capsule / panel widget currently has keyboard focus.  Parent is
# `expanded` so the shortcut's lifetime tracks the panel object.
from PySide6.QtGui import QKeySequence as _QKeySeq, QShortcut as _QShortcut
_recents_shortcut_h = _QShortcut(_QKeySeq("Ctrl+H"), expanded, recents_drawer.toggle)
_recents_shortcut_h.setContext(Qt.ShortcutContext.ApplicationShortcut)
_recents_shortcut_j = _QShortcut(_QKeySeq("Ctrl+J"), expanded, recents_drawer.toggle)
_recents_shortcut_j.setContext(Qt.ShortcutContext.ApplicationShortcut)

# Wake hooks: every legacy event source also pokes the snapshotter so a
# JSONL write / process scan triggers a snap rebuild within the debounce
# window. wake() is thread-safe — no QtBridge marshaling needed.
session_registry.sessions_changed.subscribe(lambda _: snapshotter.wake())
# usage_registry.totals_changed fires on every JSONL ingest — that's the
# wake driver for activity bumps, since per-session last_activity now
# lives on JsonlParser._session_meta and is read in compose_session_view.
usage_registry.totals_changed.subscribe(lambda _: snapshotter.wake())

snapshotter.start()
# Boot the UI with one snapshot synchronously so capsule + panel render
# real data on first paint instead of the empty default. publish is the
# marshaler emit, so this enqueues a push on the Qt main thread that
# fires once app.exec() begins spinning the event loop.
_world_marshaler.snap_ready.emit(snapshotter.build_now())

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

# Sessions/<pid>.json status-file watcher. Claude Code writes this
# file on every state flip (idle → busy → idle); without monitoring
# it we only learn about a flip when the next jsonl line is written
# — which can lag the flip by several seconds during the model's
# "thinking" window before the first token streams. By the time the
# jsonl event arrives the user has already seen the prompt sit
# unchanged for ~5 s in the island.
#
# Wire-up: same FileWatcher instance, second watch() call. The
# callback is intentionally tiny (parse pid from filename, drop
# that pid's session_state cache entry, kick the snapshotter); all
# heavier work happens on the snapshotter's worker thread, off the
# watchdog thread.
_CLAUDE_SESSIONS = Path.home() / ".claude" / "sessions"
_CLAUDE_SESSIONS.mkdir(parents=True, exist_ok=True)


def _on_session_state_file_changed(path: Path) -> None:
    """sessions/<pid>.json written/modified → invalidate just that
    pid's cache entry and wake the snapshotter so the new status
    shows up within the next debounce window (~150 ms total).

    Single-pid granularity: ``invalidate_cache(pid)`` only touches
    the one entry — we don't blow away the whole cache so other
    sessions' state stays warm and the next snapshot doesn't pay
    N disk reads it would have skipped."""
    try:
        pid = int(path.stem)
    except ValueError:
        # Filename wasn't <pid>.json (e.g. a temp / lock file). Ignore.
        return
    session_state_reader.invalidate_cache(pid)
    snapshotter.wake()


file_watcher.watch(
    _CLAUDE_SESSIONS, _on_session_state_file_changed, suffix=".json",
)
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


# Pre-warm the Windows Terminal focus worker pool. First-click cost
# would otherwise include ~5-15 ms of QThread spawn + ~1 ms of
# pythoncom.CoInitializeEx. No-op on non-Windows. See
# `design/2026-05-wt-focus-performance-decision.md` — Q-2 / C-006.
try:
    from claude_island.platform_.terminals import _wt_fast_path as _wt_fp
    _wt_fp.prewarm()
except Exception as _e:
    # Pool warmup is best-effort; first click pays the cost if this
    # fails. Don't gate app start on it.
    import logging as _logging
    _logging.getLogger(__name__).debug(
        "wt fast-path prewarm skipped: %s", _e,
    )


def _bootstrap_session_discovery() -> None:
    """Background-thread bootstrap for the session pipeline.

    Two phases on the worker thread:
      1. scan_fast() — pure psutil, lazy attr access. Sessions appear
         in the UI within tens of milliseconds. After registry.update
         we publish a snapshot synchronously via marshaler.snap_ready
         so the first frame doesn't pay the wake pipeline's
         debounce(0.1) — that 100 ms is ~free during steady-state
         coalescing but pure waste on a cold first-frame request.
      2. session_discovery.start() — runs one full scan() (with the
         orphan filter) and arms the periodic 10-second timer. Doing
         this on the worker thread means the user sees the fast-scan
         result instantly and the full filtered list lands a moment
         later, while the Qt main thread stays responsive throughout.
    """
    try:
        sessions = process_scanner.scan_fast()
        session_registry.update(sessions)
        # Bypass wake pipeline debounce for the first frame: build now,
        # publish via the same marshaler used by the wake-driven path.
        # snap_ready.emit is thread-safe (Qt Signal QueuedConnection
        # marshals onto the Qt main thread regardless of caller).
        snap = snapshotter.build_now()
        _world_marshaler.snap_ready.emit(snap)
    except Exception as exc:
        _safe_stderr_write(f"[claude-island] fast scan failed: {exc}")
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
            _safe_stderr_write(f"[claude-island] session_names gc failed: {exc}")
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

# Bidirectional Hooks v1: tear down hook subsystem before the registries
# / snapshotter pipeline so late on_change callbacks don't fire into a
# disposed Subject. hook_server.stop() requires daemon_threads=True
# (set in start()) so blocked PreToolUse handlers don't pin shutdown
# for the full 598 s wait window. Fixed in code review B-001 + B-002.
if hook_server is not None:
    hook_server.stop()
_evict_timer.stop()
if hook_bridge is not None:
    hook_bridge.stop()

session_discovery.stop()
file_watcher.stop()
jsonl_parser.request_stop()

sys.exit(exit_code)
