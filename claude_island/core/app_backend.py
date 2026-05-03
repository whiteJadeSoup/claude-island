"""AppBackend Protocol — outbound port for claude-island domain operations.

Single implementation (currently :class:`LocalAppBackend` in
:mod:`claude_island.platform_.app_backend`). These actions are
cross-platform and don't need OS or terminal knowledge — they
manipulate claude-island's own state files (session names override,
JSONL transcripts).

The Protocol lives in core for the same reason :class:`OsBackend`
does: :class:`SessionView` declares capabilities without importing
platform code.

Concrete implementations subclass ``_CapabilityProvider`` and
decorate methods with ``@capability(Capability.X)``.
"""
from __future__ import annotations

from typing import ClassVar, Protocol, runtime_checkable


@runtime_checkable
class AppBackend(Protocol):
    """Bundle of claude-island domain operations.

    Capability methods are added by the concrete subclass with the
    ``@capability`` decorator (e.g. ``@capability(Capability.RENAME)
    def rename(self, view, *, new_name)``). The dispatcher reads
    ``type(backend).capabilities`` to know what's available.

    name: Stable identifier ("local"). Not a routing key — there's
        only one AppBackend per process, injected at app startup.
    """
    name: ClassVar[str]
