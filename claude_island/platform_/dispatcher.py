"""TerminalDispatcher — aggregates the two outbound ports into single
data-flow and control-flow callables that Snapshotter + UI consume.

Three ports (TerminalAdapter chain, OsBackend singleton, AppBackend
singleton) are held here. The dispatcher exposes:

  group_sessions(sessions) → list[SessionGroup]  (data)
  dispatch(view, cap, **kw) → bool               (control)

group_sessions walks the adapter chain by priority. Each adapter that
claims sessions produces groups; remaining unclaimed sessions fall to
the next adapter. OS + APP capabilities are merged into every emitted
view (union of what the terminal provides + what the backend provides).

Adapter routing — open-vibe-island alignment (2026-05-14):
When ``view.jump_target.terminal_app`` is set (the hook captured the
host terminal at SessionStart time), routing skips the ``can_handle``
psutil walk and goes straight to the matching adapter via the
``_TERMINAL_APP_TO_ADAPTER`` map. This is the same model open-vibe-island
uses: the hook running INSIDE the claude.exe knows exactly which
terminal hosts it (WT_SESSION → WindowsTerminal, TERM_PROGRAM=iTerm.app
→ iterm2, etc.), so click-time disambiguation degrades to a string
lookup. Sessions without a jump_target (pre-hook-install, hook capture
failed, or older hook.py versions) fall through to the legacy
``can_handle`` chain — full backward compatibility.

dispatch picks the target by scope (TERMINAL → adapter_id lookup;
OS → os_backend; APP → app_backend) and delegates. Returns False on
any failure without raising.
"""
from __future__ import annotations

import collections
import logging
import time

from pathlib import Path

from claude_island.core.app_backend import AppBackend
from claude_island.core.capabilities import (
    CAPABILITY_SCOPE,
    Capability,
    LauncherSpawnError,
    Scope,
    SpawnResult,
)
from claude_island.core.os_backend import OsBackend
from claude_island.core.snapshot import SessionGroup, SessionView
from claude_island.platform_.terminals.protocols import TerminalAdapter

log = logging.getLogger(__name__)


# ── Hook terminal_app string → adapter name mapping ────────────────────────
# Mirrors open-vibe-island's inferTerminalApp + per-adapter routing. The
# hook captures TERM_PROGRAM / WT_SESSION inside the claude.exe process
# (see claude_island/hook.py::_build_jump_target) and normalizes them to
# one of these tokens. At click/group time we route directly without
# walking psutil ancestors.
#
# Unknown terminal_app strings (rare 3rd-party terminals) fall through
# to the legacy can_handle chain — that path still works, this map is
# strictly additive.
_TERMINAL_APP_TO_ADAPTER: dict[str, str] = {
    # Windows side
    "WindowsTerminal": "windows-terminal",
    "ConsoleHost": "generic-windows",
    # macOS side — match what TERM_PROGRAM literally reports
    "iTerm.app": "iterm2",
    "Apple_Terminal": "terminal-app",
    "Terminal": "terminal-app",
}


class _AdapterState:
    """Per-adapter degraded-state tracking."""

    def __init__(self, adapter: TerminalAdapter) -> None:
        self.adapter = adapter
        self._failures: collections.deque[float] = collections.deque(maxlen=3)
        self._degraded_until: float | None = None
        self._warned_missing: bool = False

    def note_failure(self) -> None:
        self._failures.append(time.monotonic())
        if len(self._failures) >= self._failures.maxlen:
            last = self._failures[0]
            if time.monotonic() - last < 60.0:
                self._degraded_until = time.monotonic() + 60.0
                log.warning(
                    "TerminalAdapter %r degraded for 60s (%d failures in <60s)",
                    self.adapter.name, len(self._failures),
                )

    def is_degraded(self) -> bool:
        if self._degraded_until is None:
            return False
        if time.monotonic() > self._degraded_until:
            self._degraded_until = None
            self._failures.clear()
            return False
        return True

    def clear_degraded(self) -> None:
        self._degraded_until = None
        self._failures.clear()


class TerminalDispatcher:
    """Holds the three ports; exposes data + control endpoint callables."""

    def __init__(
        self,
        *,
        terminals: dict[str, TerminalAdapter],
        os_backend: OsBackend,
        app_backend: AppBackend,
    ) -> None:
        self._terminals = terminals
        self._chain = [
            _AdapterState(a)
            for a in sorted(terminals.values(), key=lambda a: a._priority, reverse=True)
        ]
        self._os = os_backend
        self._app = app_backend
        self._merged_caps = type(os_backend).capabilities | type(app_backend).capabilities

    # ── Data flow ───────────────────────────────────────────────────────

    def group_sessions(self, views: list[SessionView]) -> list[SessionGroup]:
        """Walk adapter chain; bucket views into groups; inject OS/APP caps.

        Snapshotter injects this into its _build_snapshot pipeline. Note
        the input is a list of SessionViews (already resolved by
        compose_session_view in the snapshotter); adapters just bucket
        and stamp adapter_id/focus_granularity/capabilities onto them.
        Adapters MUST NOT re-run compose_session_view — that path is
        the snapshotter's exclusive responsibility, with the real
        registries; re-running it inside an adapter would either drop
        real data (if null sources are passed) or duplicate the cost.

        Routing priority (open-vibe-island alignment, 2026-05-14):
          1. ``view.jump_target.terminal_app`` known in
             ``_TERMINAL_APP_TO_ADAPTER`` → route directly to that adapter.
             Skips the ``can_handle`` psutil walk and works for views
             with placeholder pid (hook arrived before scanner).
          2. Legacy ``can_handle`` chain — fallback for views with no
             jump_target (pre-hook session, capture failed, older hook.py).

        Group flattening (open-vibe-island alignment, 2026-05-14):
        At return time every multi-view SessionGroup is exploded into N
        singleton groups so the UI renders "one session per row" instead
        of cards. Adapters still group internally for orphan-filter +
        sentinel-write side effects in WT's case; flattening happens
        AFTER those side effects run.
        """
        # Phase 1: route by jump_target.terminal_app first, then by
        # legacy can_handle. Bucket views per adapter so each adapter's
        # group() is called exactly once with all its claimed views.
        buckets: dict[str, list[SessionView]] = {}
        remaining: list[SessionView] = []
        for v in views:
            target_name = self._route_by_jump_target(v)
            if target_name and target_name in self._terminals:
                buckets.setdefault(target_name, []).append(v)
            else:
                remaining.append(v)

        groups: list[SessionGroup] = []

        # Phase 1a: invoke each adapter once with its jump_target-routed
        # views. Walk in chain order so degraded adapters are skipped.
        for st in self._chain:
            if st.is_degraded():
                continue
            claimed = buckets.pop(st.adapter.name, None)
            if not claimed:
                continue
            try:
                raw = st.adapter.group(claimed)
            except Exception as e:
                log.warning(
                    "TerminalAdapter %r raised in group() [jump_target route]: %s",
                    st.adapter.name, e,
                )
                st.note_failure()
                # Push these views back into remaining so the legacy
                # chain can take a swing.
                remaining.extend(claimed)
                continue
            for g in raw:
                merged_views = tuple(
                    _merge_caps(v, self._merged_caps) for v in g.views
                )
                groups.append(_replace_views(g, merged_views))

        # Any leftover bucket entries (adapter not in chain, e.g. ran
        # on the wrong platform) fall back to legacy can_handle below.
        for leftover in buckets.values():
            remaining.extend(leftover)

        # Phase 1b: legacy can_handle chain for views without a valid
        # jump_target route (older hook, ConsoleHost fallthrough, etc).
        for st in self._chain:
            if not remaining:
                break
            if st.is_degraded():
                continue
            taken = [v for v in remaining if st.adapter.can_handle(v.session)]
            if not taken:
                continue
            try:
                raw = st.adapter.group(taken)
            except Exception as e:
                log.warning(
                    "TerminalAdapter %r raised in group() [legacy route]: %s",
                    st.adapter.name, e,
                )
                st.note_failure()
                continue
            for g in raw:
                merged_views = tuple(
                    _merge_caps(v, self._merged_caps) for v in g.views
                )
                groups.append(_replace_views(g, merged_views))
            remaining = [v for v in remaining if v not in taken]

        # Phase 2: explode multi-view groups into singletons (user
        # decision 2026-05-14: "每个会话展示一行").
        return _explode_to_singletons(groups)

    @staticmethod
    def _route_by_jump_target(view: SessionView) -> str | None:
        """Return the adapter name dictated by ``view.jump_target``, or
        None if the view has no jump_target / unknown terminal_app.

        Pure lookup — no syscalls, no psutil. Returns the adapter ``name``
        token that matches ``@adapter("name", ...)`` registration.
        """
        jt = view.jump_target
        if jt is None:
            return None
        return _TERMINAL_APP_TO_ADAPTER.get(jt.terminal_app or "")

    # ── Control flow ────────────────────────────────────────────────────

    def dispatch(self, view: SessionView, cap: Capability, **kwargs) -> bool:
        # Observable no-ops: each early-return path logs at INFO so
        # "I clicked but nothing happened" is diagnosable from stderr
        # without having to add print() to UI / popup. INFO (not
        # WARNING) because legitimate "capability not advertised"
        # cases (like dispatch from a test stub) are common; WARNING
        # is reserved for "method existed and raised mid-flight".
        if cap not in view.capabilities:
            log.info(
                "dispatch: %s not in view.capabilities (adapter_id=%r)",
                cap, view.adapter_id,
            )
            return False
        scope = CAPABILITY_SCOPE.get(cap)
        if scope is None:
            log.info("dispatch: no scope mapping for %s", cap)
            return False
        try:
            target = self._resolve_target(scope, view.adapter_id)
        except Exception as e:
            log.warning("dispatch: resolve_target raised for %s: %s", cap, e)
            return False
        if target is None:
            log.info(
                "dispatch: no target for scope=%s, adapter_id=%r",
                scope, view.adapter_id,
            )
            return False
        method = getattr(target, cap.value, None)
        if method is None:
            log.info(
                "dispatch: %s.%s not implemented",
                type(target).__name__, cap.value,
            )
            return False
        try:
            ok = bool(method(view, **kwargs))
        except Exception as e:
            log.warning("%s.%s failed: %s", type(target).__name__, cap.value, e)
            return False
        if not ok:
            log.info("dispatch: %s.%s returned False", type(target).__name__, cap.value)
        return ok

    # ── View-less control flow (LAUNCH) ─────────────────────────────────

    def adapters_with(
        self, cap: Capability,
    ) -> tuple[tuple[str, TerminalAdapter], ...]:
        """List ``(name, adapter)`` pairs that implement ``cap``.

        Sorted by adapter ``_priority`` desc (same order as the chain
        used by ``group_sessions``). Degraded adapters are skipped.

        Used by view-less capabilities (currently only LAUNCH) where
        the caller has no SessionView to drive ``view.adapter_id``
        routing — typically the RecentsDrawer asking "which terminals
        can spawn ``claude --resume``?" before letting the user (or
        v1: the highest-priority adapter) pick one.

        Returns empty tuple when no adapter advertises the capability
        (e.g. macOS Linux box where neither WindowsTerminal nor iTerm2
        is present, only generic adapters which don't implement LAUNCH)."""
        return tuple(
            (st.adapter.name, st.adapter)
            for st in self._chain
            if not st.is_degraded()
            and cap in type(st.adapter).capabilities
        )

    def launch(
        self,
        adapter_name: str,
        *,
        cwd: Path,
        command: tuple[str, ...],
        session_uuid: str | None = None,
    ) -> SpawnResult:
        """View-less LAUNCH dispatch.

        Caller flow:
          1. ``cands = dispatcher.adapters_with(Capability.LAUNCH)``
          2. ``name, _ = cands[0]``    # or user-picked
          3. ``result = dispatcher.launch(name, cwd=..., command=..., session_uuid=...)``
          4. ``launch_intent.add(LaunchIntent(...result.terminal_pid...))``

        ``session_uuid`` is forwarded to adapters that can use it for
        Plan-L tab-title locking (currently only WT — see its
        ``launch`` docstring). Adapters that don't take it accept it
        as a no-op kwarg via ``**kwargs`` or by signature default.

        Raises ``LauncherSpawnError`` if (a) ``adapter_name`` is unknown,
        (b) the adapter doesn't implement LAUNCH, or (c) the underlying
        spawn raises. Caller should toast and *not* update the
        LaunchIntentRegistry on failure.

        Why not via :meth:`dispatch`? ``dispatch(view, cap)`` returns
        bool (FOCUS / RENAME contract); LAUNCH has no view + needs to
        return SpawnResult. Forcing it through would either break the
        bool contract or wrap with out-of-band state passing — both
        worse than a 5-line dedicated method."""
        adapter = self._terminals.get(adapter_name)
        if adapter is None:
            raise LauncherSpawnError(
                f"unknown terminal adapter: {adapter_name!r}"
            )
        if Capability.LAUNCH not in type(adapter).capabilities:
            raise LauncherSpawnError(
                f"adapter {adapter_name!r} does not implement LAUNCH"
            )
        # Only WT cares about session_uuid right now (Plan L). Other
        # adapters take it as a kwarg they happily ignore (signature
        # default = None) so we don't have to feature-detect here.
        return adapter.launch(
            cwd=cwd, command=command, session_uuid=session_uuid,
        )

    # ── Internal ────────────────────────────────────────────────────────

    def _resolve_target(self, scope: Scope, adapter_id: str):
        if scope is Scope.TERMINAL:
            return self._terminals.get(adapter_id)
        if scope is Scope.OS:
            return self._os
        if scope is Scope.APP:
            return self._app
        return None

    def clear_degraded(self) -> None:
        for st in self._chain:
            st.clear_degraded()


# ── module-level helpers ──────────────────────────────────────────────────

def _merge_caps(view: SessionView, extra: frozenset[Capability]) -> SessionView:
    """Union ``extra`` capabilities into the view, returning a new
    (frozen) instance."""
    from dataclasses import replace
    return replace(view, capabilities=view.capabilities | extra)


def _replace_views(group: SessionGroup, views: tuple[SessionView, ...]) -> SessionGroup:
    from dataclasses import replace
    return replace(group, views=views)


def _explode_to_singletons(groups: list[SessionGroup]) -> list[SessionGroup]:
    """Flatten every multi-view ``SessionGroup`` into multiple singleton
    groups so the UI renders one row per session.

    Why: open-vibe-island doesn't visually group sessions (each row is
    one AgentSession). User opted in to the same convention here
    (2026-05-14). Done at dispatcher level rather than adapter level so
    adapter-internal grouping (e.g. WT's wt_hwnd bucketing for sentinel
    reconciliation) still happens — we just don't expose the bucket
    structure to the UI.

    Singleton groups pass through untouched. Multi-view groups are
    replaced by N singletons; each singleton inherits adapter_id and
    title_hint from the parent, and gets a unique ``group_id`` derived
    from the parent's id + session uuid/pid (the parent's id is
    typically the wt_hwnd or similar shared identifier, so suffixing
    keeps uniqueness without losing the parent's diagnostic value)."""
    from dataclasses import replace
    flat: list[SessionGroup] = []
    for g in groups:
        if len(g.views) <= 1:
            flat.append(g)
            continue
        for v in g.views:
            # Stable per-view id: prefer session_uuid (real identity),
            # fall back to pid (uuid not yet resolved). Empty string
            # safety: pid alone is enough since adapters set adapter_id
            # uniquely per view.
            suffix = v.session_uuid or str(v.session.pid)
            flat.append(replace(
                g,
                group_id=f"{g.group_id}:{suffix}",
                views=(v,),
            ))
    return flat
