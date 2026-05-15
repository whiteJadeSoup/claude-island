from __future__ import annotations

from transitions import Machine

from PySide6.QtCore import QObject, Signal

from claude_island.core.models import Session


class IslandController(QObject):
    """UI state machine: DOT ↔ COLLAPSED ↔ EXPANDED.

    States
    ------
    dot        Visible-but-quiet indicator when no active sessions.
               Clickable: opens the expanded panel so the user can still
               reach Recents / Spend / Quota even with zero live sessions.
    collapsed  Small capsule showing session count.
    expanded   Full panel with session list and usage stats.

    The ``state_changed`` signal fires after every transition so windows can
    update their visibility without polling.

    Transition map::

        sessions_found      :  dot ─────────────→ collapsed
        sessions_lost       :  collapsed/expanded ─────→ dot
        user_expand         :  collapsed/dot ─────→ expanded
        user_collapse       :  expanded ─────────→ collapsed   (sessions > 0)
        user_dismiss_to_dot :  expanded ─────────→ dot         (sessions == 0)
    """

    state_changed = Signal(str)  # new state name

    _states = ["dot", "collapsed", "expanded"]

    _transitions = [
        {"trigger": "sessions_found",      "source": "dot",                "dest": "collapsed"},
        # ``sessions_lost`` only auto-degrades from ``collapsed`` (the
        # auto-state that mirrors live session count). ``expanded`` is a
        # user-driven state — the user explicitly opened the panel and
        # may still be reading Recents / Spend / Quota; we shouldn't
        # yank it closed mid-read because the last claude turn ended.
        # Dismissal from expanded is exclusively via toggle_expanded.
        {"trigger": "sessions_lost",       "source": "collapsed",          "dest": "dot"},
        # ``user_expand`` accepts both ``collapsed`` and ``dot`` so the user
        # can open the panel even when there are zero live sessions — the
        # panel renders Recents / Spend / Quota plus an "No active sessions"
        # placeholder for the empty list.
        {"trigger": "user_expand",         "source": ["collapsed", "dot"], "dest": "expanded"},
        {"trigger": "user_collapse",       "source": "expanded",           "dest": "collapsed"},
        # ``user_dismiss_to_dot`` is the dot-flavoured counterpart of
        # ``user_collapse``: collapsing the expanded panel when no sessions
        # exist should land back on dot (not on the full-pill collapsed),
        # to preserve the "0 sessions ⇒ quiet dot" invariant.
        {"trigger": "user_dismiss_to_dot", "source": "expanded",           "dest": "dot"},
    ]

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._sessions: list[Session] = []
        self.state: str = "dot"  # overwritten by Machine
        Machine(
            model=self,
            states=self._states,
            transitions=self._transitions,
            initial="dot",
            ignore_invalid_triggers=True,
        )

    # ------------------------------------------------------------------
    # Called from the WorldSnapshot subscription on the Qt main thread
    # (WorldMarshaler delivers snap → on_world_snap → on_sessions_updated)
    # ------------------------------------------------------------------

    def on_sessions_updated(self, sessions: list[Session]) -> None:
        self._sessions = sessions
        prev = self.state
        if sessions and self.state == "dot":
            self.sessions_found()  # type: ignore[attr-defined]
        elif not sessions and self.state == "collapsed":
            # Only auto-degrade from collapsed. expanded is user-driven;
            # see the transition-table comment in _transitions.
            self.sessions_lost()  # type: ignore[attr-defined]
        if self.state != prev:
            self.state_changed.emit(self.state)

    # ------------------------------------------------------------------
    # Called by UI (capsule click)
    # ------------------------------------------------------------------

    def toggle_expanded(self) -> None:
        prev = self.state
        if self.state in ("collapsed", "dot"):
            # Both collapsed and dot expand to the same full panel. The
            # panel handles the zero-session case via its placeholder
            # row, so there's no special branch needed here.
            self.user_expand()  # type: ignore[attr-defined]
        elif self.state == "expanded":
            # Collapse back. When sessions exist we return to the
            # session-count capsule; with zero sessions we return to
            # the quiet dot so the user isn't left with an oddly empty
            # full pill on screen.
            if self._sessions:
                self.user_collapse()  # type: ignore[attr-defined]
            else:
                self.user_dismiss_to_dot()  # type: ignore[attr-defined]
        if self.state != prev:
            self.state_changed.emit(self.state)

    # ------------------------------------------------------------------
    # Read-only access for windows
    # ------------------------------------------------------------------

    @property
    def sessions(self) -> list[Session]:
        return list(self._sessions)
