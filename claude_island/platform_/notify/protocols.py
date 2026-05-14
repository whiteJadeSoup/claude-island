"""Re-exports of NotifyBackend Protocol + NotifyKindHint from
``claude_island.core.notify``.

Protocol + hint enum live in ``core/`` so UI + platform_ both depend
only on core (matches existing ``platform_/protocols.py`` ↔
``core/`` discipline). This module exists for historical / cohesion
reasons — backends import from here so the per-OS implementation file
doesn't reach all the way into core.
"""
from __future__ import annotations

from claude_island.core.notify import NotifyBackend, NotifyKindHint

__all__ = ["NotifyBackend", "NotifyKindHint"]
