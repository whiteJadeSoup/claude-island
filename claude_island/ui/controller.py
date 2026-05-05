from __future__ import annotations

from transitions import Machine

from PySide6.QtCore import QObject, Signal

from claude_island.core.models import Session


class IslandController(QObject):
    """UI state machine: DOT ↔ COLLAPSED ↔ EXPANDED.

    States
    ------
    dot        Minimal 1-px presence; no active sessions.
    collapsed  Small capsule showing session count.
    expanded   Full panel with session list and usage stats.

    The ``state_changed`` signal fires after every transition so windows can
    update their visibility without polling.
    """

    state_changed = Signal(str)  # new state name

    _states = ["dot", "collapsed", "expanded"]

    _transitions = [
        {"trigger": "sessions_found", "source": "dot",                    "dest": "collapsed"},
        {"trigger": "sessions_lost",  "source": ["collapsed", "expanded"], "dest": "dot"},
        {"trigger": "user_expand",    "source": "collapsed",               "dest": "expanded"},
        {"trigger": "user_collapse",  "source": "expanded",                "dest": "collapsed"},
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
        elif not sessions and self.state in ("collapsed", "expanded"):
            self.sessions_lost()  # type: ignore[attr-defined]
        if self.state != prev:
            self.state_changed.emit(self.state)

    # ------------------------------------------------------------------
    # Called by UI (capsule click)
    # ------------------------------------------------------------------

    def toggle_expanded(self) -> None:
        prev = self.state
        if self.state == "collapsed":
            self.user_expand()  # type: ignore[attr-defined]
        elif self.state == "expanded":
            self.user_collapse()  # type: ignore[attr-defined]
        if self.state != prev:
            self.state_changed.emit(self.state)

    # ------------------------------------------------------------------
    # Read-only access for windows
    # ------------------------------------------------------------------

    @property
    def sessions(self) -> list[Session]:
        return list(self._sessions)
