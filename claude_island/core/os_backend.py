"""OsBackend Protocol — outbound port for OS-generic capabilities.

One implementation per OS (singleton). Lives in :mod:`claude_island.platform_.os`.
The Protocol stays in core so :class:`SessionView` can declare
capabilities without importing platform code.

Capabilities served here are OS-only — they don't care which
terminal hosts the session. A session opened in iTerm2 reveals its
cwd in Finder the same way a session opened in Terminal.app does;
both go through ``MacOsBackend.reveal_cwd``.

Implementations subclass ``_CapabilityProvider`` (the mixin in
:mod:`claude_island.core.capabilities`) and decorate each method
with ``@capability(Capability.X)``. The dispatcher uses
``type(backend).capabilities`` to know what's available.
"""
from __future__ import annotations

from typing import ClassVar, Protocol, runtime_checkable

# SessionView lives in core.snapshot — both modules in core, no
# layering violation. Forward-quoted to avoid an import cycle
# (snapshot also imports nothing from here, but quoting future-proofs).


@runtime_checkable
class OsBackend(Protocol):
    """Per-OS bundle of generic capabilities.

    Methods are NOT declared here — the actual capability methods
    are added by concrete subclasses with the ``@capability``
    decorator. This Protocol exists so the dispatcher can type-hint
    what it accepts; the capability-set lives on the class via
    ``_CapabilityProvider.__init_subclass__``.

    name: Stable identifier (e.g. "macos", "windows"). Used in
        diagnostics; not a routing key (there's only one OsBackend
        per process, picked by ``get_os_backend()``).
    """
    name: ClassVar[str]
