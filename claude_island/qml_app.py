"""QML island entry point — used by `python -m claude_island`."""
from __future__ import annotations

import sys
from pathlib import Path

from PySide6.QtGui import QGuiApplication
from PySide6.QtQml import QQmlApplicationEngine

_QML = Path(__file__).parent / "ui" / "qml" / "Main.qml"


# ---------------------------------------------------------------------------
# Pre-Qt helpers (macOS dock-hide, Qt message filter)
# Copied verbatim from the original __main__.py and moved here so that
# qml_app.main() is the sole entry point and these helpers run in the
# correct order (dock-hide BEFORE QGuiApplication, filter BEFORE first Qt
# message, macOS accessory policy AFTER QGuiApplication).
# ---------------------------------------------------------------------------

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
    """Call ``NSApp.setActivationPolicy_(Accessory)`` after QGuiApplication
    has been created.

    The infoDictionary mutation in ``_hide_from_macos_dock`` runs BEFORE
    NSApplication is loaded so the early activation policy decision is
    correct, but on some macOS versions the launcher's cached state
    overrides our infoDict mutation and the app still ends up in the
    default Regular policy. The runtime ``setActivationPolicy_`` call
    is authoritative — once QGuiApplication has constructed NSApplication,
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


def _make_qt_message_filter():
    """Return a Qt message handler that suppresses known-harmless noise.

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
    from claude_island.core.safe_stderr import safe_stderr_write as _write

    suppressed_substrings = (
        "QFont::setPointSize",
        "This plugin does not support raise()",
        "QWindowsWindow::setGeometry: Unable to set geometry",
    )

    def _qt_message_filter(msg_type, _ctx, message: str) -> None:
        text = str(message) if message is not None else ""
        if any(s in text for s in suppressed_substrings):
            return
        _write(text)

    return _qt_message_filter


def main() -> int:
    # ── Pre-Qt setup (must run BEFORE QGuiApplication is created) ──────────
    # stderr noise filter: catches C-level FD 2 writes from Qt + pyobjc
    # before they reach the terminal. No-op on non-darwin platforms.
    from claude_island.platform_.stderr_noise_filter import install as _install_stderr_filter
    _install_stderr_filter()

    # macOS dock-hide: seeds NSBundle.infoDictionary so the early
    # activation policy is set before Qt constructs NSApplication.
    # No-op on Windows / Linux.
    _hide_from_macos_dock()

    # Qt log noise suppression: intercept Qt's message handler BEFORE
    # creating QGuiApplication so the first Qt messages are filtered.
    from PySide6.QtCore import qInstallMessageHandler
    qInstallMessageHandler(_make_qt_message_filter())

    # Set Basic style BEFORE creating the QML engine (and right after QGuiApplication)
    # so that our custom ScrollBar contentItem/background work without the
    # "does not support customization" warnings that the native platform style emits.
    # Basic is the only fully-customizable built-in style; the entire UI is custom-drawn
    # so there is no visual regression from switching.
    from PySide6.QtQuickControls2 import QQuickStyle
    QQuickStyle.setStyle("Basic")

    # Crisp small UI text on Windows. QML Text defaults to QtRendering
    # (distance-field glyph atlas), which is built for smooth scaling/rotation
    # but looks soft/blurry for static small-pixel UI labels at 1.0–1.5× DPI —
    # the "字糊了" the detail page showed. NativeRendering uses the platform's
    # hinted rasteriser, giving sharp text at our sizes. Set globally BEFORE
    # any Text item is created so every surface inherits it. Trade-off: native
    # text doesn't animate scale as smoothly, but we never scale text.
    from PySide6.QtQuick import QQuickWindow
    QQuickWindow.setTextRenderType(QQuickWindow.TextRenderType.NativeRendering)

    app = QGuiApplication(sys.argv)

    # macOS accessory policy (post-app-creation path): runtime call that
    # overrides any cached launcher state that ignored our infoDict seed.
    # No-op on Windows / Linux.
    _apply_macos_accessory_policy()

    # ── 最小后端管线(照 __main__.py 的构造,省略可选 dep)──
    from pathlib import Path as _P
    from platformdirs import user_data_dir
    from claude_island.core.jsonl_parser import JsonlParser
    from claude_island.core.session_registry import SessionRegistry
    from claude_island.core.snapshot import Snapshotter, world
    from claude_island.core.usage_registry import UsageRegistry
    from claude_island.core.pending_decisions import PendingDecisionRegistry
    from claude_island.core.dormant_source import DormantSessionSource
    from claude_island.core.launch_intent import LaunchIntentRegistry
    from claude_island.core.notify import NotifyEventQueue
    from claude_island.platform_ import session_state as session_state_reader
    from claude_island.platform_ import session_names as session_names_store
    from claude_island.platform_.process_scanner import ProcessScanner, resume_uuid_for_pid
    from claude_island.core.session_state_machine import SessionStateMachine
    from claude_island.platform_ import hook_installer
    from claude_island.platform_.hook_server import HookServer, HookServerStartError
    from claude_island.platform_.hook_session_bridge import HookSessionBridge
    from importlib import resources
    from claude_island.platform_.file_watcher import FileWatcher
    from claude_island.platform_.session_discovery import SessionDiscovery
    from claude_island.platform_.providers import ProviderEngine
    from claude_island.platform_.app_backend import LocalAppBackend
    from claude_island.platform_.dispatcher import TerminalDispatcher
    from claude_island.platform_.terminals import build_registry
    from claude_island.platform_.os import get_os_backend
    from claude_island.core.capabilities import Capability
    from claude_island.platform_.notify import (
        MacOsNotifyBackend,
        NoopNotifyBackend,
        WindowsNotifyBackend,
    )
    from claude_island.ui.world_marshaler import WorldMarshaler
    from claude_island.ui.world_view_model import WorldViewModel
    from claude_island.ui.notification_dispatcher import NotificationDispatcher
    from claude_island.core.session_permissions import SessionPermissionCache
    import reactivex.operators as ops

    claude_projects = _P.home() / ".claude" / "projects"
    claude_projects.mkdir(parents=True, exist_ok=True)

    session_registry = SessionRegistry()
    usage_registry = UsageRegistry()
    jsonl_parser = JsonlParser(usage_registry=usage_registry, claude_projects_dir=claude_projects)
    jsonl_parser.start_backfill_pool()
    process_scanner = ProcessScanner()
    file_watcher = FileWatcher()
    session_discovery = SessionDiscovery(scanner=process_scanner, registry=session_registry)

    # ── ProviderEngine (quota) ───────────────────────────────────────────────
    # Mirrors __main__.py: cache_dir comes from platformdirs so it persists
    # across runs in the OS-appropriate user-data directory.
    quota_engine = ProviderEngine(
        cache_dir=_P(user_data_dir("ClaudeIsland", appauthor=False)),
    )

    # ── PendingDecisionRegistry + NotifyEventQueue ───────────────────────────
    # on_change uses lazy lookup (globals().get) so constructing these before
    # the snapshotter (like __main__.py does) never hits a NameError.
    def _wake_if_ready() -> None:
        snap = globals().get("snapshotter")
        if snap is not None:
            snap.wake()

    pending_registry = PendingDecisionRegistry(on_change=_wake_if_ready)
    notify_queue = NotifyEventQueue(on_change=_wake_if_ready)

    # ── resume-offline sources ───────────────────────────────────────────────
    # DormantSessionSource is a thin view over JsonlParser + UsageRegistry;
    # LaunchIntentRegistry is a short-lived store of pending Resume intents.
    # Both are passed to Snapshotter so reconcile can populate dormant /
    # launching_sessions in the snapshot. Construction is essentially free.
    dormant_source = DormantSessionSource(
        jsonl_parser=jsonl_parser,
        usage_registry=usage_registry,
    )
    launch_intent = LaunchIntentRegistry()

    # ── TerminalDispatcher (real focus + other capabilities) ─────────────────
    # Mirrors __main__.py construction: app_backend wraps the names store and
    # JSONL dir; os_backend + terminal adapters are platform-specific.
    # Constructed before Snapshotter so its group_sessions can be injected
    # (wired below at Snapshotter(group_sessions=_dispatcher.group_sessions);
    # without it views carry no capabilities and FOCUS dispatch always fails).
    _claude_projects = _P.home() / ".claude" / "projects"
    _app_backend = LocalAppBackend(
        names_store=session_names_store,
        claude_projects_dir=_claude_projects,
        # Wake snapshotter on app-backend changes (renames, etc.).
        on_change=lambda: globals().get("snapshotter") and globals()["snapshotter"].wake(),
    )
    _dispatcher = TerminalDispatcher(
        terminals=build_registry(),
        os_backend=get_os_backend(),
        app_backend=_app_backend,
    )

    # ── focus_fn ─────────────────────────────────────────────────────────────
    # Real terminal focus via TerminalDispatcher.  Looks up the matching
    # SessionView from the current world snapshot and dispatches FOCUS.
    # Falls back to a log on any failure so the UI stays alive.
    # Note: _view_for is defined later in this function — the closure captures
    # it by reference, so call order doesn't matter at definition time.
    def focus_fn(session_id: str) -> None:
        try:
            target_view = _view_for(session_id)
            if target_view is None:
                print(
                    f"[island] focus: no view for session_id={session_id!r}",
                    file=sys.stderr,
                )
                return
            ok = _dispatcher.dispatch(target_view, Capability.FOCUS)
            if not ok:
                print(
                    f"[island] focus: dispatch returned False for {session_id!r} "
                    "(terminal may not support FOCUS or window is gone)",
                    file=sys.stderr,
                )
        except Exception as exc:
            print(f"[island] focus_fn error: {exc}", file=sys.stderr)

    # ── get_session_details ───────────────────────────────────────────────────
    # Replicate _build_session_details from __main__.py using the same sources
    # available in qml_app.  Returns a SessionDetails object.  The VM maps
    # it to a QML-friendly dict via sessionDetail().
    def _get_session_details(session):
        from claude_island.core.models import SessionDetails
        state = session_state_reader.read_session_state(session.pid) or {}
        pid_json_uuid = (
            state.get("sessionId")
            if isinstance(state.get("sessionId"), str)
            else None
        )
        try:
            cmdline_resume_uuid = resume_uuid_for_pid(
                session.pid,
                names_lookup=session_names_store.get_uuid_by_name,
            )
        except Exception:
            cmdline_resume_uuid = None
        sess_uuid = cmdline_resume_uuid or pid_json_uuid or session.session_uuid
        meta = jsonl_parser.get_session_metadata(sess_uuid) or {}
        cost, turns, sides = usage_registry.get_session_summary(sess_uuid)
        per_model = usage_registry.get_session_per_model(sess_uuid)
        started_at = session_state_reader.parse_started_at(state.get("startedAt"))
        if started_at is None:
            started_at = meta.get("started_at")
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

    # ── permission_cache (review-mode toggle) ────────────────────────────────
    # In-memory only; eviction is session-end-driven. Constructed here so
    # it can be injected into the VM without the VM importing core directly.
    permission_cache = SessionPermissionCache(
        on_change=lambda: globals().get("snapshotter") and globals()["snapshotter"].wake(),
    )

    # ── _view_for: shared view lookup used by focus_fn and open_folder_fn ──
    # Extracted so the two callers don't duplicate the world.current walk.
    def _view_for(session_id: str):
        """Return the SessionView matching session_id (uuid), or None."""
        from claude_island.core.snapshot import world as _world
        snap = _world.current
        for grp in snap.session_groups:
            for v in grp.views:
                if v.session_uuid == session_id:
                    return v
        return None

    # ── open_folder_fn ───────────────────────────────────────────────────────
    # Dispatches REVEAL_CWD for the session. Same lookup as focus_fn but uses
    # Capability.REVEAL_CWD instead of FOCUS.
    def open_folder_fn(session_id: str) -> None:
        try:
            view = _view_for(session_id)
            if view is None:
                print(
                    f"[island] openFolder: no view for session_id={session_id!r}",
                    file=sys.stderr,
                )
                return
            ok = _dispatcher.dispatch(view, Capability.REVEAL_CWD)
            if not ok:
                print(
                    f"[island] openFolder: dispatch returned False for {session_id!r}",
                    file=sys.stderr,
                )
        except Exception as exc:
            print(f"[island] open_folder_fn error: {exc}", file=sys.stderr)

    # ── reset_thinking_fn ────────────────────────────────────────────────────
    # Dispatches RESET_THINKING (strips thinking blocks from the JSONL
    # transcript) via AppBackend. Destructive — a .bak backup is created by
    # the backend before modifying the file.
    def reset_thinking_fn(session_id: str) -> None:
        try:
            view = _view_for(session_id)
            if view is None:
                print(
                    f"[island] resetThinking: no view for session_id={session_id!r}",
                    file=sys.stderr,
                )
                return
            ok = _dispatcher.dispatch(view, Capability.RESET_THINKING)
            if not ok:
                print(
                    f"[island] resetThinking: dispatch returned False for {session_id!r}",
                    file=sys.stderr,
                )
        except Exception as exc:
            print(f"[island] reset_thinking_fn error: {exc}", file=sys.stderr)

    # ── resume_fn ────────────────────────────────────────────────────────────
    # Real resume via TerminalDispatcher LAUNCH + LaunchIntentRegistry.
    # Mirrors RecentsDrawer._on_resume exactly:
    #   1. Find the DormantSession by uuid in the current world snapshot.
    #   2. Build flags from permission_mode + command = ("claude", "--resume", uuid, *flags).
    #   3. Pick the first LAUNCH-capable adapter via dispatcher.adapters_with(LAUNCH).
    #   4. Call dispatcher.launch(adapter_name, cwd=..., command=..., session_uuid=...).
    #   5. Register a LaunchIntent so the next snapshot shows the ⏳ launching row.
    #   6. Wake the snapshotter so the UI updates within ~100 ms.
    # On any failure, logs to stderr and returns — best-effort, UI stays alive.
    def resume_fn(uuid: str) -> None:
        from claude_island.core.snapshot import world as _world
        from claude_island.core.capabilities import Capability as _Cap, LauncherSpawnError
        from claude_island.core.launch_intent import LaunchIntent
        from claude_island.ui.recents_drawer import _flags_for_mode

        try:
            # Step 1: locate the DormantSession for this uuid.
            dormant = None
            for d in _world.current.dormant_sessions:
                if d.session_uuid == uuid:
                    dormant = d
                    break
            if dormant is None:
                print(
                    f"[island] resume: no dormant session found for uuid={uuid!r}",
                    file=sys.stderr,
                )
                return

            # Step 2: build flags + command (same logic as RecentsDrawer).
            flags = _flags_for_mode(dormant.permission_mode)
            command = ("claude", "--resume", dormant.session_uuid, *flags)

            # Step 3: pick the first LAUNCH-capable terminal adapter.
            candidates = _dispatcher.adapters_with(_Cap.LAUNCH)
            if not candidates:
                print(
                    "[island] resume: no terminal launcher available "
                    "(install Windows Terminal or iTerm2)",
                    file=sys.stderr,
                )
                return
            adapter_name, _ = candidates[0]

            # Step 4: spawn the terminal.
            result = _dispatcher.launch(
                adapter_name,
                cwd=dormant.cwd,
                command=command,
                session_uuid=dormant.session_uuid,
            )

            # Step 5: register the intent so the next snapshot shows ⏳.
            launch_intent.add(LaunchIntent(
                session_uuid=dormant.session_uuid,
                cwd=dormant.cwd,
                flags=flags,
                terminal_name=result.terminal_name,
                terminal_pid=result.terminal_pid,
                requested_at=result.started_at,
            ))

            # Step 6: wake snapshotter — launching row appears in ~100 ms.
            snapshotter.wake()

        except LauncherSpawnError as exc:
            print(f"[island] resume: launcher failed: {exc}", file=sys.stderr)
        except Exception as exc:
            print(f"[island] resume_fn error: {exc}", file=sys.stderr)

    # ── _resume_uuid_reader ───────────────────────────────────────────────────
    # Mirrors __main__.py: wraps resume_uuid_for_pid so HookServer, Snapshotter,
    # and compose_session_view all share one consistent uuid-recovery path.
    # Two-step resolution: cmdline --resume <UUID> → direct; --resume <name>
    # → reverse-lookup via session_names_store.
    def _resume_uuid_reader(pid: int) -> str | None:
        return resume_uuid_for_pid(
            pid, names_lookup=session_names_store.get_uuid_by_name,
        )

    # ── Hook subsystem ────────────────────────────────────────────────────────
    # Mirrors __main__.py's hook block (lines 707–845).
    # On failure we degrade gracefully: state_machine still exists (pure
    # in-memory), hook_server and hook_bridge remain None, and the UI
    # continues working from pid.json scanner data.
    state_machine = SessionStateMachine()
    hook_server: HookServer | None = None
    hook_bridge: HookSessionBridge | None = None

    try:
        # Step 1: sync bundled hook.py to ~/.claude-island/hook.py
        with resources.as_file(
            resources.files("claude_island") / "hook.py"
        ) as bundled_hook:
            dest_hook = Path.home() / ".claude-island" / "hook.py"
            try:
                hook_installer.sync_hook_script(
                    bundled_script=Path(bundled_hook), dest=dest_hook,
                )
            except OSError as e:
                print(
                    f"[island] could not sync hook.py to {dest_hook}: {e}; "
                    f"hooks disabled this session",
                    file=sys.stderr,
                )
                raise

        # Step 2: idempotently merge hook entries into ~/.claude/settings.json
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
                print(
                    f"[island] installed Claude Code hooks "
                    f"({len(result.installed_events)} events); "
                    f"preserved {result.user_hooks_preserved} user hook(s)",
                    file=sys.stderr,
                )
        except hook_installer.InstallError as e:
            print(
                f"[island] could not install hooks in settings.json: {e}",
                file=sys.stderr,
            )
            # Continue — listener still works if user pre-installed hooks

        # Step 4: start the HTTP listener
        hook_server = HookServer(
            state_machine,
            pending_registry=pending_registry,
            permission_cache=permission_cache,
            notify_queue=notify_queue,
            resume_uuid_reader=_resume_uuid_reader,
        )
        try:
            bound_port = hook_server.start()
            print(
                f"[island] hook listener bound on 127.0.0.1:{bound_port}",
                file=sys.stderr,
            )
        except HookServerStartError as e:
            print(
                f"[island] hook listener failed to start: {e}; "
                f"degrading to scanner-only (phase will come from pid.json)",
                file=sys.stderr,
            )
            hook_server = None

        # Step 5: wire registry ↔ state_machine
        hook_bridge = HookSessionBridge(
            registry=session_registry, state_machine=state_machine,
        )
    except Exception as e:
        print(
            f"[island] hook subsystem failed to initialize ({e!r}); "
            f"running scanner-only",
            file=sys.stderr,
        )

    marshaler = WorldMarshaler()  # snap_ready → world.push (QueuedConnection, 内部已接)
    snapshotter = Snapshotter(
        session_source=session_registry,
        state_reader=session_state_reader,
        metadata_provider=jsonl_parser,
        usage_registry=usage_registry,
        names_store=session_names_store,
        # Real-time phase from HookServer (falls back to pid.json if None).
        live_state_reader=state_machine.read,
        # OLD-uuid recovery so UsageRegistry lookups hit the right key after
        # --resume; mirrors __main__.py's identical injection.
        resume_uuid_reader=_resume_uuid_reader,
        get_quota=lambda: quota_engine.get(provider_name="anthropic"),
        get_available_providers=lambda: ["anthropic"],
        get_selected_provider=lambda: "anthropic",
        publish=marshaler.snap_ready.emit,
        # Route views through the adapter chain so each SessionView carries
        # real capabilities (FOCUS/REVEAL_CWD/RESET_THINKING) + adapter_id +
        # focus_granularity. Without this the default singleton grouping yields
        # empty capabilities and dispatch(view, FOCUS) always returns False
        # (the "terminal may not support FOCUS" symptom). Mirrors __main__.
        group_sessions=_dispatcher.group_sessions,
        # Wire PendingDecisionRegistry so snapshots carry decisions.
        pending_decisions=pending_registry,
        # resume-offline sources (History drawer population).
        dormant_source=dormant_source,
        launch_intent=launch_intent,
        # Bidirectional Hooks v1: notification events.
        notify_queue=notify_queue,
    )

    session_registry.sessions_changed.subscribe(lambda _: snapshotter.wake())
    usage_registry.totals_changed.subscribe(lambda _: snapshotter.wake())
    file_watcher.watch(claude_projects, jsonl_parser.parse_file)

    # Step 6: hook events drive snapshotter wakes.  live_state_changed fires
    # on the HookServer worker thread; snapshotter.wake() is thread-safe and
    # debounces internally.
    state_machine.live_state_changed.subscribe(
        on_next=lambda _: snapshotter.wake(),
        on_error=lambda e: print(
            f"[island] live_state_changed subscription died: {e!r}",
            file=sys.stderr,
        ),
    )

    vm = WorldViewModel(
        resolve_fn=pending_registry.resolve,
        focus_fn=focus_fn,
        get_totals=usage_registry.get_totals,
        get_totals_by_model=usage_registry.get_totals_by_model,
        get_sidechain_totals=usage_registry.get_sidechain_totals,
        # refresh_quota_fn: force-fetch the anthropic provider's quota and
        # immediately poke the snapshotter so the fresh value lands in the
        # next rendered snapshot. Mirrors _force_refresh_selected in __main__.
        refresh_quota_fn=lambda: (
            quota_engine.force_refresh(provider_name="anthropic"),
            snapshotter.wake(),
        ),
        resume_fn=resume_fn,
        get_session_details=_get_session_details,
        # R7 action callbacks — injected so VM stays in the UI layer and never
        # imports platform_ or core permission code directly.
        rename_fn=session_names_store.set_session_name,
        open_folder_fn=open_folder_fn,
        reset_thinking_fn=reset_thinking_fn,
    )
    world.observable().subscribe(
        on_next=vm.update,
        on_error=lambda e: print(f"vm subscription error: {e}", file=sys.stderr),
    )

    # ── NotificationDispatcher ───────────────────────────────────────────────
    # Subscribed to world.observable like every other UI surface. Dedup key
    # is the sequence of notify_event ids — a snapshot without new/removed
    # events doesn't re-deliver. Backend is per-platform (mirrors __main__).
    if sys.platform == "darwin":
        _notify_backend = MacOsNotifyBackend()
    elif sys.platform == "win32":
        # tray_icon=None → no tray fallback (no tray icon in QML path yet).
        _notify_backend = WindowsNotifyBackend(tray_icon=None)
    else:
        _notify_backend = NoopNotifyBackend()

    _notification_dispatcher = NotificationDispatcher(backend=_notify_backend)
    # Pin subscription so GC doesn't collect it.
    _notify_subscription = (
        world.observable()
        .pipe(
            ops.distinct_until_changed(
                key_mapper=lambda s: tuple(e.id for e in s.notify_events),
            ),
        )
        .subscribe(
            on_next=_notification_dispatcher.on_snapshot,
            on_error=lambda e: print(
                f"[island] notify pipeline error: {e}", file=sys.stderr
            ),
        )
    )

    engine = QQmlApplicationEngine()
    engine.rootContext().setContextProperty("worldVm", vm)
    # isMac controls window flags in Main.qml: Qt.Tool is dropped on darwin
    # because NSPanel refuses to paint transparent windows (same rationale as
    # CapsuleWindow._setup_window — see capsule_window.py for the full comment).
    engine.rootContext().setContextProperty("isMac", sys.platform == "darwin")
    engine.load(str(_QML))
    if not engine.rootObjects():
        print("QML failed to load", file=sys.stderr)
        return 1

    snapshotter.start()
    file_watcher.start()
    import threading
    threading.Thread(target=session_discovery.start, daemon=True).start()
    marshaler.snap_ready.emit(snapshotter.build_now())   # 首帧

    # ── Bug 5 fix: force-refresh quota at startup so the first rendered snapshot
    # carries real quota data (quota_engine.get() returns None on a fresh process
    # until force_refresh() fetches it).  Also install a 60 s heartbeat so quota
    # stays fresh for the lifetime of the process, mirroring __main__.py's usage
    # heartbeat intent.  The timer is stored to prevent GC and cancelled on shutdown.
    def _quota_refresh_once() -> None:
        try:
            quota_engine.force_refresh(provider_name="anthropic")
        except Exception as exc:
            print(f"[island] quota force_refresh error: {exc}", file=sys.stderr)
        snapshotter.wake()

    # Initial force-refresh on a daemon thread so startup is non-blocking.
    threading.Thread(target=_quota_refresh_once, daemon=True, name="quota-init").start()

    _quota_timer: list[threading.Timer] = []   # list so closure can mutate it

    def _quota_heartbeat() -> None:
        _quota_refresh_once()
        # Re-arm unless app is shutting down (timer reference will be cleared
        # on shutdown — the lambda check guards against a final spurious fire).
        if _quota_timer:
            t = threading.Timer(60.0, _quota_heartbeat)
            t.daemon = True
            _quota_timer[0] = t
            t.start()

    # Arm first heartbeat 60 s after startup.
    _first_timer = threading.Timer(60.0, _quota_heartbeat)
    _first_timer.daemon = True
    _quota_timer.append(_first_timer)
    _first_timer.start()

    code = app.exec()
    # Shutdown order mirrors __main__.py: stop snapshotter first so in-flight
    # wakes don't fire into a torn-down pipeline, then tear down the hook
    # subsystem before the registries it writes into.
    # Cancel the quota heartbeat timer so it doesn't fire into a torn-down pipeline.
    if _quota_timer:
        _quota_timer[0].cancel()
        _quota_timer.clear()
    snapshotter.stop()
    if hook_server is not None:
        hook_server.stop()
    if hook_bridge is not None:
        hook_bridge.stop()
    session_discovery.stop()
    file_watcher.stop()
    jsonl_parser.request_stop()
    return code


if __name__ == "__main__":
    raise SystemExit(main())
