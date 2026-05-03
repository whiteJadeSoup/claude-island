"""TerminalAdapter Protocol — the per-terminal outbound port.

Each terminal adapter has two responsibilities:

1. **Data side** (``can_handle`` + ``group``): inspect a list of views,
   claim the ones it can handle, and bucket them into
   :class:`SessionGroup`\\ s. The adapter stamps each retained view
   with ``adapter_id``, ``focus_granularity``, and ``capabilities``
   (just its own — the dispatcher unions OS + APP caps on top).

   Adapters MUST NOT re-run :func:`compose_session_view` — incoming
   views are already fully resolved by the snapshotter against the
   real registries. Adapters bucket and stamp; they never re-resolve.

2. **Control side** (capability methods): implement the methods
   matching the capabilities it claims. The ``@capability``
   decorator validates the method name; the adapter signals "I
   support FOCUS" by simply defining ``def focus(self, view)``
   decorated with ``@capability(Capability.FOCUS)``.

Adapter chain priority (set via the ``@adapter("name", priority=N)``
decorator) determines the order ``TerminalDispatcher.group_sessions``
walks adapters in. Higher priority = first dibs on a view. Generic
fallback adapters use priority=0 so anything more specific beats them.

Adapters never reach into other adapters or the OS backend. The
dispatcher merges OS/APP capabilities into views post-hoc; the
adapter only declares what it (the terminal) can do.
"""
from __future__ import annotations

from typing import ClassVar, Protocol, runtime_checkable

from claude_island.core.models import Session
from claude_island.core.snapshot import SessionGroup, SessionView


@runtime_checkable
class TerminalAdapter(Protocol):
    """Per-terminal integration.

    Concrete implementations subclass ``_CapabilityProvider`` and
    expose at minimum:
      - ``name: ClassVar[str]`` (set by ``@adapter`` decorator)
      - ``_priority: int`` (set by ``@adapter`` decorator)
      - ``def can_handle(self, session: Session) -> bool``
      - ``def group(self, views: list[SessionView]) -> list[SessionGroup]``
      - capability methods decorated with ``@capability(Capability.X)``
    """
    name: ClassVar[str]
    _priority: int

    def can_handle(self, session: Session) -> bool:
        """Return True iff this adapter can group/activate this session.

        Takes the raw Session (not the resolved view) because the
        decision is a property of the underlying process — its
        ancestry, terminal host, etc. — not the rendered view.

        Cheap lookup; called for every view × every adapter on every
        scan tick. Implementations should rely on already-cached info
        (e.g. ancestor process names via psutil) rather than spawning
        subprocesses.
        """
        ...

    def group(self, views: list[SessionView]) -> list[SessionGroup]:
        """Bucket the given views into SessionGroups.

        Pre-condition: every input view's session has already passed
        ``can_handle`` for this adapter — no need to re-filter.
        Post-condition: every emitted SessionView has ``adapter_id``
        equal to ``self.name``, and the group's ``adapter_id`` matches.

        The adapter sets ``capabilities`` on each view to *only* the
        terminal-specific caps it provides (e.g. ``{Capability.FOCUS}``).
        The dispatcher unions OS + APP caps on top before publishing.
        """
        ...
