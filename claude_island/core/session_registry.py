from __future__ import annotations

import threading
from dataclasses import replace as _replace

from reactivex.subject import Subject

from .models import Session


# Sentinel pid value used for sessions inserted via ``upsert`` before
# the process scanner has confirmed the real pid. update() (called by
# the scanner) merges placeholders into real entries by matching cwd.
PLACEHOLDER_PID = -1


class SessionRegistry:
    """Live set of Claude Code sessions as observed by ProcessScanner.

    Holds the canonical list (pid, project_path/cwd, baseline
    last_activity = process create_time) and emits ``sessions_changed``
    when the list itself changes. Per-session JSONL-derived activity is
    NOT merged here — it lives uuid-keyed on
    ``JsonlParser._session_meta`` and is folded into ``SessionView`` by
    ``compose_session_view``. Keeping that path out of the registry
    prevents two sessions sharing a cwd from cross-contaminating each
    other's last_activity (the bug a previous project-keyed override
    used to cause).

    Two write paths:
      * ``update(sessions)`` — scanner full-replace tick. Merges any
        existing hook-placed placeholders (pid=PLACEHOLDER_PID,
        uuid!="") into the scanner output by cwd match (carries the
        placeholder's uuid onto the real pid entry).
      * ``upsert(session)`` — bridge inserts/refreshes a single entry.
        Used when a hook event arrives before the scanner has seen
        the process (G1 latency target).
    """

    def __init__(self) -> None:
        # on_next(list[Session]) synchronously notifies subscribers on
        # the calling thread. Subscribers wake the snapshotter; nobody
        # reads the data shape from this signal.
        self.sessions_changed: Subject[list[Session]] = Subject()
        self._sessions: list[Session] = []
        # Sentinel != any real list (None never compares equal to a list)
        # so the first update() always emits.
        self._last_emitted: list[Session] | None = None
        self._lock = threading.Lock()

    def update(self, sessions: list[Session]) -> None:
        """Replace the session list (typically called by the process scanner).

        Merge rule for hook-placed placeholders:
          * For each existing placeholder (pid=PLACEHOLDER_PID, uuid!=""):
            if the incoming list has a session with the same cwd and
            an empty uuid (scanner output), graft the placeholder's
            uuid onto it (so the merged entry has real pid + real uuid).
          * If no incoming session matches, KEEP the placeholder.
            (Bridge will tombstone it via state_machine.tombstone later
            after consecutive scanner misses — see HookSessionBridge.)

        Always emits sessions_changed — HookSessionBridge relies on
        receiving one emit per scanner tick so its miss counter can
        advance even when the session list hasn't changed shape. The
        snapshot pipeline downstream (Snapshotter._build_snapshot +
        distinct_until_changed on WorldSnapshot) handles dedup on the
        actual visible content.
        """
        with self._lock:
            merged = self._merge_with_placeholders(sessions)
            self._sessions = merged
            self._last_emitted = list(self._sessions)
            snapshot = list(self._sessions)
        self.sessions_changed.on_next(snapshot)

    def upsert(self, session: Session) -> None:
        """Insert or update a single Session by identity.

        Matching priority:
          1. Same session_uuid → replace
          2. Same cwd + existing pid==PLACEHOLDER_PID → replace
             (hook placeholder being upgraded by scanner — but in practice
             scanner uses update(), so this case fires only when the bridge
             posts a follow-up upsert for an existing placeholder)
          3. Same (cwd, pid) → replace
          4. Else → append

        Emits sessions_changed only if the list actually changed (a re-upsert
        with identical fields is dropped)."""
        with self._lock:
            existing = self._sessions
            new_list = list(existing)
            replaced = False
            # Try uuid match first
            if session.session_uuid:
                for i, s in enumerate(new_list):
                    if s.session_uuid and s.session_uuid == session.session_uuid:
                        if s == session:
                            return  # No-op
                        new_list[i] = session
                        replaced = True
                        break
            if not replaced:
                # Try (cwd, pid==placeholder) match
                for i, s in enumerate(new_list):
                    if (
                        s.pid == PLACEHOLDER_PID
                        and s.project_path == session.project_path
                    ):
                        if s == session:
                            return
                        new_list[i] = session
                        replaced = True
                        break
            if not replaced:
                # Try (cwd, pid) exact match
                for i, s in enumerate(new_list):
                    if s.pid == session.pid and s.project_path == session.project_path:
                        if s == session:
                            return
                        new_list[i] = session
                        replaced = True
                        break
            if not replaced:
                new_list.append(session)

            if new_list == self._last_emitted:
                return
            self._sessions = new_list
            self._last_emitted = list(new_list)
            snapshot = list(new_list)
        self.sessions_changed.on_next(snapshot)

    @property
    def sessions(self) -> list[Session]:
        with self._lock:
            return list(self._sessions)

    def remove_by_uuid(self, uuid: str) -> bool:
        """Remove all entries with the given session_uuid. Returns True if
        anything was removed.

        Used by HookSessionBridge when state_machine tombstones a uuid:
        any lingering placeholder (or scanner entry tagged with the same
        uuid via merge) should disappear from the UI immediately, not
        wait for the scanner to drop it.
        """
        if not uuid:
            return False
        with self._lock:
            before = len(self._sessions)
            self._sessions = [s for s in self._sessions if s.session_uuid != uuid]
            removed = len(self._sessions) != before
            if removed:
                self._last_emitted = list(self._sessions)
                snapshot = list(self._sessions)
            else:
                snapshot = None
        if removed:
            self.sessions_changed.on_next(snapshot)
        return removed

    # -- internals --------------------------------------------------------

    def _merge_with_placeholders(
        self,
        incoming: list[Session],
    ) -> list[Session]:
        """Take scanner output + the registry's current state, and
        produce the merged list per the rules in update()'s docstring.

        Must be called with self._lock held.

        Two source of "extra info" we preserve across the scanner's
        empty-uuid output:

        1. Placeholders (``pid == PLACEHOLDER_PID``) — hook arrived
           before scanner; we hold the uuid waiting for the real pid.
           Indexed by cwd.
        2. Real-pid entries with a uuid — the bridge upserted via
           ``jt.host_pid`` (macOS always sets this to the claude pid),
           OR a prior merge grafted a uuid here. The scanner output
           always carries ``session_uuid=""`` (process_scanner doesn't
           read transcripts), so without this preservation every scanner
           tick would wipe the uuid from real-pid entries. Indexed by
           (cwd, pid).

        After /clear, only (2) keeps NEW_UUID attached to the (cwd, pid)
        entry between bridge upsert and the next compose pass. Without
        it, ``compose_session_view`` has to recover the uuid via
        ``pid.json`` every tick — fragile when pid.json hasn't yet been
        rewritten to reflect the new in-memory uuid.
        """
        # Index existing placeholders by cwd. Two placeholders for the
        # same cwd is unusual (two hooks fired for the same project
        # without scanner ever seeing them); keep the most recently
        # added one (overwriting in the dict).
        placeholders_by_cwd: dict = {}
        # Real-pid entries indexed by (cwd, pid) so we can preserve
        # their uuid when the scanner re-emits the same (cwd, pid)
        # with an empty uuid.
        real_uuids_by_cwd_pid: dict = {}
        for s in self._sessions:
            if s.pid == PLACEHOLDER_PID and s.session_uuid:
                placeholders_by_cwd[s.project_path] = s
            elif s.pid > 0 and s.session_uuid:
                real_uuids_by_cwd_pid[(s.project_path, s.pid)] = s.session_uuid

        merged: list[Session] = []
        consumed_placeholders: set = set()

        for new in incoming:
            # Priority for grafting onto an empty-uuid scanner entry:
            #   1. Placeholder for the same cwd (hook-then-scanner race).
            #   2. Real-pid entry for the same (cwd, pid) (preserve uuid
            #      across scanner ticks).
            # When both apply (rare: placeholder + a sibling real entry
            # in the same cwd), the placeholder wins so its pending
            # graft completes — matches the prior behavior.
            placeholder = placeholders_by_cwd.get(new.project_path)
            if (
                placeholder is not None
                and not new.session_uuid
                and new.pid > 0
            ):
                merged.append(_replace(new, session_uuid=placeholder.session_uuid))
                consumed_placeholders.add(placeholder.session_uuid)
                continue

            preserved_uuid = real_uuids_by_cwd_pid.get((new.project_path, new.pid))
            if preserved_uuid and not new.session_uuid and new.pid > 0:
                merged.append(_replace(new, session_uuid=preserved_uuid))
                continue

            merged.append(new)

        # Keep any placeholder the scanner didn't pick up. (Scanner might
        # not yet see a freshly-spawned claude.exe; placeholder bridges
        # the gap until it does — or until tombstone fires.)
        for cwd, placeholder in placeholders_by_cwd.items():
            if placeholder.session_uuid not in consumed_placeholders:
                merged.append(placeholder)

        return merged
