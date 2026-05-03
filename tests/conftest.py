"""Test session bootstrap.

The PRICING registry in ``claude_island.core.models`` is empty by
design — every provider declares its own rates via ``register_pricing``
at import time. For tests, importing the providers package once at
session start makes the same rates available without each test file
having to wire it up. This mirrors what ``__main__.py`` does in the
real app (provider sub-modules import as a side effect of importing
``claude_island.platform_.providers``).

Tests that want to exercise the empty-registry fallback can clear it
inside their own fixture; this conftest only seeds the common case.
"""
from __future__ import annotations

# Side-effect import: triggers the @provider decorator AND every
# register_pricing() call inside each provider module.
import claude_island.platform_.providers  # noqa: F401
