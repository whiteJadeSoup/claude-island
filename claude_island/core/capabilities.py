"""Capability framework — UI/platform decoupling primitives.

This module defines the contract every UI-triggered action against a
session passes through. The point: UI doesn't know which terminal /
OS / app backend implements an action. UI just declares "I want
capability X applied to this view"; a dispatcher routes by scope.

Core primitives:

* :class:`Scope` — three categories of where a capability is fulfilled
  (terminal-specific / OS-generic / claude-island-internal).

* :class:`Capability` — every action UI can trigger gets one enum value.
  The string value MUST match the implementing method's name (the
  ``@capability`` decorator enforces this at class-build time).

* :data:`CAPABILITY_SCOPE` — the routing table. Static map from each
  Capability to its Scope; the dispatcher consults this to decide
  which port to call.

* :func:`capability` — method decorator. Marks a method as the
  implementation of a specific capability. Validates at class-build
  time that ``method.__name__ == capability.value`` so a typo or
  rename surfaces as ImportError instead of a silent dead method.

* :class:`_CapabilityProvider` — mixin for any class that implements
  capabilities. ``__init_subclass__`` walks the class body once and
  freezes a ``capabilities: frozenset[Capability]`` class attribute.
  Backends/adapters union this set into the views they emit so the
  UI knows what affordances to render.

The frozenset is computed once per class (not per instance) — runtime
overhead of capability discovery is zero after import.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import ClassVar


class Scope(StrEnum):
    """Where a capability is fulfilled.

    TERMINAL — needs terminal-specific knowledge (focus a pane in
        iTerm2, select a tab in Windows Terminal). Routed by
        ``view.adapter_id`` to a TerminalAdapter.
    OS — only needs OS knowledge (open path in file manager, copy
        to clipboard). Routed to the singleton OsBackend.
    APP — claude-island's own domain logic (rename a session,
        repair a transcript). Routed to the singleton AppBackend.
    """
    TERMINAL = "terminal"
    OS = "os"
    APP = "app"


class Capability(StrEnum):
    """Every UI-triggered action against a session.

    Adding a new capability:
      1. Add the enum value here (string matches the method name).
      2. Add ``CAPABILITY_SCOPE[Capability.X] = Scope.Y``.
      3. Implement ``def x(self, view): ...`` on at least one
         backend/adapter, decorated with ``@capability(Capability.X)``.
      4. UI checks ``Capability.X in view.capabilities`` to decide
         whether to render the affordance.
    """
    FOCUS = "focus"                   # bring the session's pane/tab/window to front
    REVEAL_CWD = "reveal_cwd"         # OS file manager: open the cwd
    COPY_PATH = "copy_path"           # OS clipboard: cwd as text
    RENAME = "rename"                 # rewrite session_names.json (kwargs: new_name=str)
    RESET_THINKING = "reset_thinking" # strip 'thinking' blocks from JSONL transcript
    LAUNCH = "launch"                 # spawn 'claude --resume <uuid> [flags]' in cwd.
                                      # VIEW-LESS — invoked via TerminalDispatcher.launch
                                      # (not dispatch()), kwargs: cwd=Path, command=tuple[str,...]


CAPABILITY_SCOPE: dict[Capability, Scope] = {
    Capability.FOCUS:          Scope.TERMINAL,
    Capability.REVEAL_CWD:     Scope.OS,
    Capability.COPY_PATH:      Scope.OS,
    Capability.RENAME:         Scope.APP,
    Capability.RESET_THINKING: Scope.APP,
    Capability.LAUNCH:         Scope.TERMINAL,  # declarative grouping; not consumed by dispatch()
}


# ── LAUNCH-specific contract types ────────────────────────────────────────
#
# These live next to Capability.LAUNCH because they are part of its
# contract: every adapter implementing @capability(Capability.LAUNCH)
# returns SpawnResult on success and raises LauncherSpawnError on
# failure. Defined in core/ so all three layers (core, platform_, ui)
# can import them — particularly the UI layer which needs the exception
# type to catch in the Resume click handler.

@dataclass(frozen=True)
class SpawnResult:
    """LAUNCH success metadata. ``terminal_pid`` is the spawned host
    process pid (wt.exe on Windows, osascript on macOS), NOT the final
    claude.exe pid — that one will be discovered later by ProcessScanner.
    The HistoryDrawer's "couldn't detect new session after 30s" toast
    surfaces ``terminal_pid`` so the user can find the right window."""
    terminal_name: str       # adapter name, e.g. 'windows-terminal'
    terminal_pid: int
    started_at: datetime     # tz-aware UTC


class LauncherSpawnError(RuntimeError):
    """LAUNCH adapter failed (process spawn raised, command not found,
    etc.). UI catches this and shows a toast; LaunchIntentRegistry is
    NOT updated — the user can immediately try Resume again."""


class FocusGranularity(StrEnum):
    """How precisely a terminal can focus a session.

    PANE — to the exact split pane (iTerm2, Kitty, WezTerm).
    TAB  — to the tab (Windows Terminal, Terminal.app).
    APP  — only to the host application (Ghostty, Alacritty, generic).

    UI may surface this as a hover hint; functionally it's just
    metadata — FOCUS still works regardless. APP-granularity FOCUS
    is the universal fallback.
    """
    PANE = "pane"
    TAB = "tab"
    APP = "app"


def capability(cap: Capability):
    """Mark a method as the implementation of ``cap``.

    Enforces at decoration time that the method's name matches
    ``cap.value`` — so a typo (e.g. ``@capability(Capability.FOCUS)``
    on a method named ``do_focus``) raises TypeError at import,
    not silently produces a dead method that the dispatcher will
    look up by ``getattr(target, "focus")`` and fail to find.

    The decorator stamps a ``_capability`` attribute on the
    function object; ``_CapabilityProvider.__init_subclass__``
    scans for it to build the frozen capability set on the class.

    Constraint on capability values
    -------------------------------
    ``Capability.value`` MUST be a valid Python identifier — the
    dispatcher resolves backend methods via ``getattr(target,
    cap.value)`` (see :class:`TerminalDispatcher`). If you ever need
    a capability whose value is not a valid identifier (e.g. a
    kebab-case string for UI display), the dispatch mechanism has
    to switch from reflection-by-name to an explicit
    ``{Capability: method}`` registration table — at the cost of one
    line of boilerplate per capability per backend. Today every
    value happens to be lowercase + underscores so the cheaper
    by-name approach works.
    """
    def deco(fn):
        if fn.__name__ != cap.value:
            raise TypeError(
                f"@capability({cap.name}): method '{fn.__name__}' must be "
                f"named '{cap.value}' (Capability.value MUST match method name "
                f"so the dispatcher's getattr lookup works)"
            )
        fn._capability = cap
        return fn
    return deco


class _CapabilityProvider:
    """Mixin: subclass to advertise capabilities via decorated methods.

    On class build, ``__init_subclass__`` walks the class body, finds
    every method tagged with ``@capability``, and freezes the union
    (this class's caps + every base class's caps) into a class
    attribute ``capabilities: frozenset[Capability]``.

    The dispatcher reads ``type(target).capabilities`` to know what
    a backend supports. Adapters/backends never compute the set
    themselves — the framework does it once at import time.

    Inheritance: a subclass that adds new methods *adds* to the set;
    overriding a method does not change membership (the cap was
    already there). Removing a capability requires deleting the
    method, not just decorating-out.
    """

    capabilities: ClassVar[frozenset[Capability]] = frozenset()

    def __init_subclass__(cls, **kw) -> None:
        super().__init_subclass__(**kw)
        own = {
            attr._capability
            for attr in cls.__dict__.values()
            if callable(attr) and hasattr(attr, "_capability")
        }
        inherited: frozenset[Capability] = frozenset()
        for base in cls.__mro__[1:]:
            inherited |= getattr(base, "capabilities", frozenset())
        cls.capabilities = frozenset(own | inherited)
