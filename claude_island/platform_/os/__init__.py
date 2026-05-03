"""Per-OS backends — implements OsBackend Protocol from core.os_backend.

One implementation per OS, picked at process start by ``get_os_backend()``
based on ``sys.platform``. The OsBackend serves OS-generic capabilities
(REVEAL_CWD, COPY_PATH) — they don't depend on which terminal hosts
the session.

Adding a new OS:
  1. Drop ``<os>.py`` in this package implementing _CapabilityProvider
     + relevant @capability methods.
  2. Add a branch to ``get_os_backend()`` keying off sys.platform.
  3. UI / dispatcher / terminal adapters need no change.
"""
from __future__ import annotations

import sys

from claude_island.core.os_backend import OsBackend


def get_os_backend() -> OsBackend:
    """Return the singleton OsBackend appropriate for the current OS.

    Raises NotImplementedError on platforms where no backend exists —
    surfaces the gap loudly at startup rather than silently producing
    a degraded UX where every OS-scoped capability returns False.
    """
    if sys.platform == "darwin":
        from .macos import MacOsBackend
        return MacOsBackend()
    if sys.platform == "win32":
        from .windows import WindowsOsBackend
        return WindowsOsBackend()
    raise NotImplementedError(
        f"No OsBackend implementation for sys.platform={sys.platform!r}. "
        f"Supported: darwin, win32."
    )
