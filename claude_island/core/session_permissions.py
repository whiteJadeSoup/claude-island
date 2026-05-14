"""Session-scoped permission cache (G7) + per-session "review prompts"
toggle (G8).

Why a single module: both pieces share the lifecycle ("evict on
SessionEnd, evict on 4 h hard TTL") and the same in-memory storage
discipline. Having two registries with the same eviction triggers would
just be code duplication.

Granularity: per ``(session_uuid, tool_name)``. Coarser than per-tool-
input (e.g. per Bash command); deliberately coarse — see Detail Design
§Non-Goals for rationale ("but I allowed `npm test`, why is `npm test
--watch` asking?"). Cost: trusting Bash for a session unlocks ALL Bash
calls in that session — UI must surface this prominently for HIGH-risk
tools (see ``ApprovalCard`` warning logic).

In-memory only — no disk persistence. Restarting the app or hitting the
4 h TTL invalidates all grants, which matches the "session-scoped"
contract: a grant lives no longer than the session that earned it.
"""
from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable

log = logging.getLogger(__name__)


# Hard TTL in addition to SessionEnd eviction. Defends against a missed
# SessionEnd hook (claude.exe crashed, hook listener was down at the
# crash moment, etc.) leaving stale grants forever.
DEFAULT_TTL_S = 4 * 60 * 60   # 4 hours


@dataclass(frozen=True, slots=True)
class SessionPermissionGrant:
    """A single (session, tool) grant. Frozen for safe sharing.

    Stored only inside SessionPermissionCache; never reaches the UI
    snapshot directly (the ApprovalCard fast-path elides it entirely).
    """
    session_uuid: str
    tool_name: str
    granted_at: datetime
    expires_at: datetime


OnChangeCallback = Callable[[], None]


class SessionPermissionCache:
    """Thread-safe in-memory store of grants + review-mode toggles.

    No disk persistence; no observable surface (UI never reads grants
    directly — the cache mediates a fast-path on the HookServer side).
    The on_change hook is included for symmetry with
    PendingDecisionRegistry but currently fires only when state changes
    that could affect render (the per-session toggle is reflected in
    SessionDetailPopup; grants themselves aren't shown to the UI in v1).
    """

    def __init__(
        self,
        *,
        ttl_s: float = DEFAULT_TTL_S,
        on_change: OnChangeCallback | None = None,
    ) -> None:
        self._ttl = timedelta(seconds=ttl_s)
        self._grants: dict[tuple[str, str], SessionPermissionGrant] = {}
        self._review_modes: dict[str, bool] = {}
        self._lock = threading.Lock()
        self._on_change = on_change or (lambda: None)

    # ── grants ──────────────────────────────────────────────────────────

    def check(self, session_uuid: str, tool_name: str) -> bool:
        """Fast-path query for HookServer. Returns True iff a non-expired
        grant exists for ``(uuid, tool_name)``.

        Lazily evicts the entry on a TTL miss so a long-running listener
        doesn't accumulate stale tombstones.
        """
        if not session_uuid or not tool_name:
            return False
        key = (session_uuid, tool_name)
        with self._lock:
            grant = self._grants.get(key)
            if grant is None:
                return False
            if grant.expires_at <= datetime.now(timezone.utc):
                self._grants.pop(key, None)
                return False
            return True

    def grant(
        self,
        session_uuid: str,
        tool_name: str,
        *,
        now: datetime | None = None,
    ) -> None:
        """Record a (session, tool) grant. Overwrites any existing grant
        for the same key (renews the TTL). No-op for empty inputs.
        """
        if not session_uuid or not tool_name:
            return
        ts = now or datetime.now(timezone.utc)
        new_grant = SessionPermissionGrant(
            session_uuid=session_uuid,
            tool_name=tool_name,
            granted_at=ts,
            expires_at=ts + self._ttl,
        )
        with self._lock:
            self._grants[(session_uuid, tool_name)] = new_grant
        log.info(
            "session-perm grant: uuid=%s tool=%s ttl=%ds",
            session_uuid[:8], tool_name, int(self._ttl.total_seconds()),
        )

    def evict_session(self, session_uuid: str) -> int:
        """Drop every grant + review-mode entry for a session.

        Called from HookSessionBridge on SessionEnd. Returns count of
        entries dropped (grants + 1 if review-mode was set).
        """
        if not session_uuid:
            return 0
        with self._lock:
            grant_keys = [k for k in self._grants if k[0] == session_uuid]
            for k in grant_keys:
                del self._grants[k]
            had_review = self._review_modes.pop(session_uuid, None) is not None
        dropped = len(grant_keys) + (1 if had_review else 0)
        if dropped:
            self._on_change()
            log.debug("evict_session uuid=%s dropped=%d", session_uuid[:8], dropped)
        return dropped

    def evict_expired(self, *, now: datetime | None = None) -> int:
        """Drop grants past their TTL. Idempotent. Returns count dropped.

        Called by a periodic timer in __main__ (60 s) so the cache stays
        bounded even if SessionEnd hooks were missed.
        """
        ts = now or datetime.now(timezone.utc)
        with self._lock:
            stale = [k for k, g in self._grants.items() if g.expires_at <= ts]
            for k in stale:
                del self._grants[k]
        if stale:
            log.info("evict_expired: dropped %d grants past TTL", len(stale))
        return len(stale)

    # ── review-prompts toggle ───────────────────────────────────────────

    def is_review(self, session_uuid: str) -> bool:
        """Returns True iff per-session "Review prompts" is enabled.
        Default False on missing key."""
        if not session_uuid:
            return False
        with self._lock:
            return self._review_modes.get(session_uuid, False)

    def set_review(self, session_uuid: str, enabled: bool) -> None:
        """Set or clear the per-session review-prompts toggle. The
        eviction lifecycle (SessionEnd) clears alongside grants."""
        if not session_uuid:
            return
        with self._lock:
            prev = self._review_modes.get(session_uuid, False)
            if enabled:
                self._review_modes[session_uuid] = True
            else:
                self._review_modes.pop(session_uuid, None)
        if prev != enabled:
            self._on_change()
            log.info(
                "review-prompts toggle: uuid=%s enabled=%s",
                session_uuid[:8], enabled,
            )

    # ── introspection ──────────────────────────────────────────────────

    def grant_count(self) -> int:
        """For tests + diagnostics. O(1)."""
        with self._lock:
            return len(self._grants)

    def review_count(self) -> int:
        """For tests + diagnostics. O(1)."""
        with self._lock:
            return len(self._review_modes)
