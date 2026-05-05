"""LocalAppBackend — claude-island's own domain operations.

Two capabilities at the time of writing:

- RENAME (kwargs: ``new_name=str``) — write a custom display name to
  ``~/.claude-island/session_names.json`` keyed by session UUID.
  Replaces the rename-input dialog's direct call into
  ``session_names_store.set_session_name``.

- RESET_THINKING — strip ``thinking`` blocks from the session's JSONL
  transcript. Used to repair a session that's failing the Anthropic
  signature check after being routed through a non-Anthropic provider.
  Wraps ``core.session_repair.strip_thinking_blocks``.

Both capabilities call the injected ``on_change`` callback after a
successful write so the snapshotter wakes immediately and the UI
reflects the change without waiting for the next 60s heartbeat tick.

This is the SOLE implementation of AppBackend — there's no per-OS
variation because the operations are pure file I/O via cross-platform
``pathlib`` / ``json``.
"""
from __future__ import annotations

from pathlib import Path
from typing import Callable, ClassVar, Protocol

from claude_island.core.capabilities import Capability, _CapabilityProvider, capability
from claude_island.core.models import project_hash
from claude_island.core.snapshot import SessionView


class _NamesStoreProto(Protocol):
    def set_session_name(self, uuid: str, name: str) -> None: ...


class _JsonlParserProto(Protocol):
    """Whatever the production jsonl_parser exposes — only used to
    derive the per-session JSONL path. Kept narrow so tests can fake."""
    pass


class LocalAppBackend(_CapabilityProvider):
    """Single-implementation AppBackend backed by local files.

    Constructor injects:
      names_store    — exposes ``set_session_name(uuid, name)``.
      claude_projects_dir — root of ``~/.claude/projects/`` (passed in
          rather than re-derived so tests can point at a tmp dir).
      on_change      — callable invoked after a successful mutation.
          Production wires this to ``snapshotter.wake`` so the UI
          refreshes within the debounce window without waiting for
          the heartbeat tick. Tests pass a no-op or a Mock.

    Returns False on any failure (uuid missing, file not found, OS
    error). Never raises — the dispatcher's try/except is a backstop,
    but graceful False at this layer keeps the dispatcher's log noise
    to a minimum (a missing uuid is "expected", not an error).
    """

    name: ClassVar[str] = "local"

    def __init__(
        self,
        *,
        names_store: _NamesStoreProto,
        claude_projects_dir: Path,
        on_change: Callable[[], None],
    ) -> None:
        self._names = names_store
        self._projects_dir = claude_projects_dir
        self._on_change = on_change

    @capability(Capability.RENAME)
    def rename(self, view: SessionView, *, new_name: str) -> bool:
        """Persist a custom display name for this session.

        kwargs:
            new_name: The new name. Stripped of leading/trailing
                whitespace. Empty string clears the override (relies
                on session_names_store accepting that as "delete").

        Returns True iff the session has a usable UUID and the
        write completed without raising. The grouping pipeline picks
        up the new name on the next snapshot via the names_store
        lookup in compose_session_view; on_change accelerates that.
        """
        # Read the resolved uuid off the view (NOT view.session.session_uuid
        # — that one is empty for nearly every session because ProcessScanner
        # doesn't read transcripts). compose_session_view pins the resolved
        # value at view.session_uuid for exactly this purpose.
        uuid = view.session_uuid
        if not uuid:
            return False
        try:
            self._names.set_session_name(uuid, new_name.strip())
        except OSError:
            return False
        self._on_change()
        return True

    @capability(Capability.RESET_THINKING)
    def reset_thinking(self, view: SessionView) -> bool:
        """Strip ``thinking`` blocks from this session's JSONL.

        Resolves the JSONL path the same way the rest of the app does:
        ``<projects_dir>/<project_hash(cwd)>/<uuid>.jsonl``. Returns
        False when the UUID is empty or the file doesn't exist; True
        on success (regardless of whether any blocks were actually
        stripped — the side effect is the .bak file, always written).

        Returns bool only; the granular count of stripped blocks is
        intentionally not surfaced — the .bak file beside the original
        is the durable evidence of what changed.
        """
        # Late import: keeps app_backend import-time light and avoids
        # pulling session_repair into modules that don't need it.
        from claude_island.core.session_repair import strip_thinking_blocks

        uuid = view.session_uuid  # see rename() for why not view.session.session_uuid
        if not uuid:
            return False
        jsonl_path = (
            self._projects_dir / project_hash(view.project_path) / f"{uuid}.jsonl"
        )
        try:
            strip_thinking_blocks(jsonl_path)
        except (FileNotFoundError, OSError):
            return False
        self._on_change()
        return True
