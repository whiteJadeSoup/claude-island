"""Terminal adapter registry — self-registering via ``@adapter``.

Pattern: each adapter file decorates its class with ``@adapter("name",
priority=N, platform="win"|"mac"|"linux")``. The decorator filters by
current OS at import time; adapters for other OSes leave their class
defined but don't enter the registry.

To trigger registration, this ``__init__.py`` imports every adapter
module at the bottom (after the decorator is defined). New adapter =
new file + decorator + one import line at the bottom of this file.
``build_registry()`` returns the populated dict; the dispatcher
sorts by priority on construction.

Filtering by platform here (rather than at the dispatcher) keeps the
imports clean: a Linux user never instantiates a Windows-specific
adapter that would fail to import its dependencies.
"""
from __future__ import annotations

import sys
from typing import Literal

from .protocols import TerminalAdapter

# Internal registry. Keyed by adapter name (== view.adapter_id token);
# values are instantiated singletons (one per process).
_REGISTRY: dict[str, TerminalAdapter] = {}


def _short_platform() -> str:
    """Map sys.platform to the short tokens used by ``@adapter``."""
    if sys.platform == "darwin":
        return "mac"
    if sys.platform == "win32":
        return "win"
    return "linux"


def adapter(
    name: str,
    *,
    priority: int,
    platform: Literal["mac", "win", "linux"],
):
    """Self-registering decorator.

    Parameters:
      name: Stable identifier; becomes ``view.adapter_id`` for views
          this adapter emits. Must be unique across all adapters.
      priority: Higher = first dibs on a session in the dispatcher
          chain. Generic fallback adapters use 0; specific terminals
          use 50–100.
      platform: One of "mac" / "win" / "linux". Only registers when
          ``sys.platform`` matches; on other platforms the class
          stays defined but isn't instantiated, so its (potentially
          OS-specific) imports can stay safely lazy.

    The decorator instantiates the class with no arguments. Adapters
    needing wired dependencies (e.g. for activation calls) lazy-init
    inside their methods.
    """
    def deco(cls):
        if platform != _short_platform():
            return cls
        if name in _REGISTRY:
            raise RuntimeError(
                f"@adapter({name!r}): name already registered by "
                f"{type(_REGISTRY[name]).__module__}"
            )
        inst = cls()
        # Stamp identity on the instance — the protocol's class-var
        # name/_priority are just type hints; the dispatcher reads
        # them off the instance.
        inst.name = name
        inst._priority = priority
        _REGISTRY[name] = inst
        return cls
    return deco


def build_registry() -> dict[str, TerminalAdapter]:
    """Return a snapshot of the registered adapters as a fresh dict.

    Imports of the individual adapter modules at the bottom of this
    file ensure the @adapter decorators have run by the time anyone
    calls build_registry() (Python guarantees this — module import
    runs all top-level statements before returning).
    """
    return dict(_REGISTRY)


# ---------------------------------------------------------------------------
# Trigger @adapter registration. New adapter file = new line here.
# Order doesn't affect priority (priority is set by @adapter); it only
# affects insertion order in the dict — ties at the same priority are
# resolved by insertion order. Keep specific terminals before generic
# fallback to make that ordering explicit.
# ---------------------------------------------------------------------------
from . import windows_terminal  # noqa: E402,F401
from . import generic_windows   # noqa: E402,F401
from . import iterm2            # noqa: E402,F401
from . import terminal_app      # noqa: E402,F401
from . import generic_mac       # noqa: E402,F401
