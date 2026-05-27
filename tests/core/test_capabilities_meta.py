"""Meta-tests that catch the "added Capability, forgot to wire it" bug.

Two failure modes a casual reader of Capability + CAPABILITY_SCOPE
can introduce without noticing:

  1. Add ``Capability.FOO`` to the enum, forget the
     ``CAPABILITY_SCOPE[Capability.FOO] = Scope.X`` entry. The
     dispatcher silently routes FOO to None, the UI button does
     nothing when clicked.

  2. Add ``Capability.FOO`` and CAPABILITY_SCOPE entry, but no
     backend implements ``@capability(Capability.FOO)``. Every view's
     ``capabilities`` frozenset omits FOO; ``FOO in view.capabilities``
     is always False; the UI affordance never renders.

Both are silent failures — pytest doesn't catch them because there's
no consumer test that breaks. These metas turn both into a clear
"capability X is half-wired" assertion error at the right place."""
from __future__ import annotations

from claude_island.core.capabilities import (
    CAPABILITY_SCOPE,
    Capability,
    _CapabilityProvider,
)


def test_every_capability_has_scope_entry():
    """CAPABILITY_SCOPE is a static dict mapping each Capability to
    the Scope (TERMINAL / OS / APP) that dispatches it. A Capability
    without a Scope entry will be silently routed to no backend by
    the dispatcher — clicking the UI affordance is a no-op with no
    log line. The cost of forgetting this scales with the number of
    enum entries; the test catches it for ~µs."""
    missing = [cap for cap in Capability if cap not in CAPABILITY_SCOPE]
    assert not missing, (
        f"Capabilities missing from CAPABILITY_SCOPE: {missing}. "
        f"Add `CAPABILITY_SCOPE[Capability.{missing[0].name}] = Scope.X` "
        f"in claude_island/core/capabilities.py."
    )


def test_every_capability_implemented_by_at_least_one_provider():
    """Every Capability must be ``@capability(Capability.X)`` on at
    least one ``_CapabilityProvider`` subclass that is reachable on
    this platform — otherwise ``cap in view.capabilities`` is always
    False and the UI never offers the action.

    Import strategy: pull in the three platform package roots that
    own capability backends. Each package's ``__init__`` (or the
    platform-conditional re-exports inside it) brings in only the
    subclasses appropriate for the current OS. On macOS the
    Windows-only backends won't appear in __subclasses__; the
    assertion is that the macOS-reachable union still covers every
    Capability — none of the current capabilities are platform-locked.
    """
    # Trigger import so subclasses self-register via __init_subclass__.
    import claude_island.platform_.app_backend  # noqa: F401
    import claude_island.platform_.terminals    # noqa: F401  triggers build_registry chain
    from claude_island.platform_.os import get_os_backend
    get_os_backend()  # ensures platform-appropriate OS backend class is touched

    covered: set[Capability] = set()

    def walk(cls):
        covered.update(cls.capabilities)
        for sub in cls.__subclasses__():
            walk(sub)

    walk(_CapabilityProvider)
    missing = set(Capability) - covered
    assert not missing, (
        f"Capabilities not implemented by any _CapabilityProvider "
        f"subclass: {missing}. Either decorate a method on the "
        f"appropriate backend with `@capability(Capability.{next(iter(missing)).name})` "
        f"or remove the enum value if no backend can implement it."
    )


def test_capability_method_names_match_enum_values():
    """The @capability decorator (capabilities.py:135) enforces that
    the decorated method's name matches the Capability's string value
    AT DECORATION TIME. This test re-asserts the property at the
    *enum-side* — if anyone refactors the decorator to drop the check,
    this still catches the drift.

    Walks every _CapabilityProvider subclass and verifies that for
    each method tagged with ``_capability``, the method's __name__
    equals the enum's value string."""
    # Same import dance as above so subclasses are discoverable.
    import claude_island.platform_.app_backend  # noqa: F401
    import claude_island.platform_.terminals    # noqa: F401
    from claude_island.platform_.os import get_os_backend
    get_os_backend()

    mismatches: list[str] = []

    def walk(cls):
        for name, attr in cls.__dict__.items():
            cap = getattr(attr, "_capability", None)
            if cap is not None and name != cap.value:
                mismatches.append(
                    f"{cls.__name__}.{name} declares @capability({cap.name}) "
                    f"but Capability.{cap.name}.value == '{cap.value}'"
                )
        for sub in cls.__subclasses__():
            walk(sub)

    walk(_CapabilityProvider)
    assert not mismatches, "\n".join(mismatches)
