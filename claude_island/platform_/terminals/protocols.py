"""TerminalAdapter Protocol — the per-terminal outbound port.

Each terminal adapter has two responsibilities:

1. **Data side** (``can_handle`` + ``group``): inspect a list of
   sessions, claim the ones it can handle, and bucket them into
   :class:`SessionGroup`\\ s. Inside each emitted view, the adapter
   sets ``adapter_id``, ``focus_granularity``, and ``capabilities``
   (just its own — the dispatcher unions OS + APP caps on top).

2. **Control side** (capability methods): implement the methods
   matching the capabilities it claims. The ``@capability``
   decorator validates the method name; the adapter signals "I
   support FOCUS" by simply defining ``def focus(self, view)``
   decorated with ``@capability(Capability.FOCUS)``.

Adapter chain priority (set via the ``@adapter("name", priority=N)``
decorator) determines the order ``TerminalDispatcher.group_sessions``
walks adapters in. Higher priority = first dibs on a session.
Generic fallback adapters use priority=0 so anything more specific
beats them.

Adapters never reach into other adapters or the OS backend. The
dispatcher merges OS/APP capabilities into views post-hoc; the
adapter only declares what it (the terminal) can do.
"""
from __future__ import annotations

from typing import ClassVar, Protocol, runtime_checkable

from claude_island.core.models import Session
from claude_island.core.snapshot import SessionGroup


@runtime_checkable
class TerminalAdapter(Protocol):
    """Per-terminal integration.

    Concrete implementations subclass ``_CapabilityProvider`` and
    expose at minimum:
      - ``name: ClassVar[str]`` (set by ``@adapter`` decorator)
      - ``_priority: int`` (set by ``@adapter`` decorator)
      - ``def can_handle(self, session: Session) -> bool``
      - ``def group(self, sessions: list[Session]) -> list[SessionGroup]``
      - capability methods decorated with ``@capability(Capability.X)``
    """
    name: ClassVar[str]
    _priority: int

    def can_handle(self, session: Session) -> bool:
        """Return True iff this adapter can group/activate this session.

        Cheap lookup; called for every session × every adapter on
        every scan tick. Implementations should rely on already-cached
        info (e.g. the session's ancestor process names, available
        via psutil) rather than spawning subprocesses.
        """
        ...

    def group(self, sessions: list[Session]) -> list[SessionGroup]:
        """Bucket the given sessions into SessionGroups.

        Pre-condition: every input session has already passed
        ``can_handle`` for this adapter — no need to re-filter.
        Post-condition: every emitted SessionView has ``adapter_id``
        equal to ``self.name``, and the group's ``adapter_id`` matches.

        The adapter sets ``capabilities`` on each view to *only* the
        terminal-specific caps it provides (e.g. ``{Capability.FOCUS}``).
        The dispatcher unions OS + APP caps on top before publishing.
        """
        ...
