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

dispatch picks the target by scope (TERMINAL → adapter_id lookup;
OS → os_backend; APP → app_backend) and delegates. Returns False on
any failure without raising.
"""
from __future__ import annotations

import collections
import logging
import time

from claude_island.core.app_backend import AppBackend
from claude_island.core.capabilities import CAPABILITY_SCOPE, Capability, Scope
from claude_island.core.os_backend import OsBackend
from claude_island.core.snapshot import SessionGroup, SessionView
from claude_island.platform_.terminals.protocols import TerminalAdapter

log = logging.getLogger(__name__)


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
        real data (if null sources are passed) or duplicate the cost."""
        groups: list[SessionGroup] = []
        remaining = list(views)
        for st in self._chain:
            if st.is_degraded():
                continue
            taken = [v for v in remaining if st.adapter.can_handle(v.session)]
            if not taken:
                continue
            try:
                raw = st.adapter.group(taken)
            except Exception as e:
                log.warning("TerminalAdapter %r raised in group(): %s", st.adapter.name, e)
                st.note_failure()
                continue
            for g in raw:
                merged_views = tuple(
                    _merge_caps(v, self._merged_caps) for v in g.views
                )
                groups.append(_replace_views(g, merged_views))
            remaining = [v for v in remaining if v not in taken]
            if not remaining:
                break
        return groups

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
