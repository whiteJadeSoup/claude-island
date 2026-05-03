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

Also: an autouse fixture resets the global ``world`` BehaviorSubject
between tests so subscribers / pushes from one test never leak into
the next. This is the safety net that makes a module-level singleton
viable for testing — without it, test order would matter and failures
would be tricky to reproduce.
"""
from __future__ import annotations

import pytest

# Side-effect import: triggers the @provider decorator AND every
# register_pricing() call inside each provider module.
import claude_island.platform_.providers  # noqa: F401


@pytest.fixture(autouse=True)
def _reset_world_between_tests():
    """Wipe the global ``world`` BehaviorSubject after every test so
    subscribers / current-value state from one test cannot bleed into
    the next. autouse means every test is automatically isolated; no
    test author needs to remember to opt in."""
    yield
    from claude_island.core.snapshot import world
    world.reset_for_testing()
