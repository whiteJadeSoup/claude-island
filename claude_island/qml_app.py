"""QML walking-skeleton 入口(与 python -m claude_island 并存,不影响现有 app)。"""
from __future__ import annotations

import os
import sys
from pathlib import Path

from PySide6.QtGui import QGuiApplication
from PySide6.QtQml import QQmlApplicationEngine

_QML = Path(__file__).parent / "ui" / "qml" / "Main.qml"


def main() -> int:
    app = QGuiApplication(sys.argv)

    # ── 最小后端管线(照 __main__.py 的构造,省略可选 dep)──
    from pathlib import Path as _P
    from platformdirs import user_data_dir
    from claude_island.core.jsonl_parser import JsonlParser
    from claude_island.core.session_registry import SessionRegistry
    from claude_island.core.snapshot import Snapshotter, world
    from claude_island.core.usage_registry import UsageRegistry
    from claude_island.core.pending_decisions import PendingDecisionRegistry, build_request, DecisionKind
    from claude_island.core.dormant_source import DormantSessionSource
    from claude_island.core.launch_intent import LaunchIntentRegistry
    from claude_island.core.notify import NotifyEventQueue
    from claude_island.platform_ import session_state as session_state_reader
    from claude_island.platform_ import session_names as session_names_store
    from claude_island.platform_.process_scanner import ProcessScanner
    from claude_island.platform_.file_watcher import FileWatcher
    from claude_island.platform_.session_discovery import SessionDiscovery
    from claude_island.platform_.providers import ProviderEngine
    from claude_island.platform_.notify import (
        MacOsNotifyBackend,
        NoopNotifyBackend,
        WindowsNotifyBackend,
    )
    from claude_island.ui.world_marshaler import WorldMarshaler
    from claude_island.ui.world_view_model import WorldViewModel
    from claude_island.ui.notification_dispatcher import NotificationDispatcher
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

    # ── focus_fn ─────────────────────────────────────────────────────────────
    # Best-effort placeholder; full terminal-tab focus requires the
    # TerminalDispatcher / adapter stack wired in Plan 3. For now we log
    # so QML testers can see the signal fired without crashing.
    def focus_fn(session_id: str) -> None:
        print(f"[island] focus requested: {session_id}", file=sys.stderr)

    # ── resume_fn ────────────────────────────────────────────────────────────
    # DONE_WITH_CONCERNS: Real resume requires the TerminalDispatcher /
    # LaunchIntentRegistry / DormantSession → claude --resume flow (the full
    # RecentsDrawer path in __main__.py). That heavyweight wiring is deferred
    # to a later plan step. For now, log the intent so callers can verify the
    # signal fires without crashing.
    def resume_fn(uuid: str) -> None:
        print(f"[island] resume requested: {uuid}", file=sys.stderr)

    marshaler = WorldMarshaler()  # snap_ready → world.push (QueuedConnection, 内部已接)
    snapshotter = Snapshotter(
        session_source=session_registry,
        state_reader=session_state_reader,
        metadata_provider=jsonl_parser,
        usage_registry=usage_registry,
        names_store=session_names_store,
        get_quota=lambda: quota_engine.get(provider_name="anthropic"),
        get_available_providers=lambda: ["anthropic"],
        get_selected_provider=lambda: "anthropic",
        publish=marshaler.snap_ready.emit,
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

    vm = WorldViewModel(
        resolve_fn=pending_registry.resolve,
        focus_fn=focus_fn,
        get_totals=usage_registry.get_totals,
        get_totals_by_model=usage_registry.get_totals_by_model,
        # refresh_quota_fn: force-fetch the anthropic provider's quota and
        # immediately poke the snapshotter so the fresh value lands in the
        # next rendered snapshot. Mirrors _force_refresh_selected in __main__.
        refresh_quota_fn=lambda: (
            quota_engine.force_refresh(provider_name="anthropic"),
            snapshotter.wake(),
        ),
        resume_fn=resume_fn,
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
    engine.load(str(_QML))
    if not engine.rootObjects():
        print("QML failed to load", file=sys.stderr)
        return 1

    snapshotter.start()
    file_watcher.start()
    import threading
    threading.Thread(target=session_discovery.start, daemon=True).start()
    marshaler.snap_ready.emit(snapshotter.build_now())   # 首帧

    # ── Demo-decision injector ───────────────────────────────────────────────
    # Set CISLAND_DEMO_DECISION=1 to inject two sample decisions for visual
    # verification of approval/question cards in QML.
    #
    # Real hook-sourced decisions come when qml_app becomes the sole entry
    # point and a HookServer is wired in (Plan 3); we deliberately skip that
    # here to avoid port conflicts with the existing __main__.py hook server.
    if os.environ.get("CISLAND_DEMO_DECISION"):
        try:
            req1 = build_request(
                kind=DecisionKind.PRE_TOOL_USE,
                session_name="db-migrate",
                tool_name="Bash",
                tool_input_preview="kubectl apply -f prod.yaml",
                timeout_s=3600,
                cwd=_P.home(),
                hook_event="PreToolUse",
                session_uuid="demo-1",
            )
            pending_registry.register(req1)

            req2 = build_request(
                kind=DecisionKind.ASK_QUESTION,
                session_name="cc-learning",
                question_text="用哪个库做日期处理?",
                question_options=("date-fns", "Day.js", "Luxon"),
                question_option_descriptions=("轻量 tree-shakeable", "2KB Moment 兼容", "时区最强"),
                multi_select=False,
                timeout_s=3600,
                cwd=_P.home(),
                hook_event="UserPromptSubmit",
                session_uuid="demo-2",
                tool_name="AskUserQuestion",
            )
            pending_registry.register(req2)

            snapshotter.wake()
            print("[island] demo decisions injected (CISLAND_DEMO_DECISION=1)", file=sys.stderr)
        except Exception as exc:
            print(f"[island] demo injector error (non-fatal): {exc}", file=sys.stderr)

    code = app.exec()
    snapshotter.stop(); session_discovery.stop(); file_watcher.stop()
    jsonl_parser.request_stop()
    return code


if __name__ == "__main__":
    raise SystemExit(main())
