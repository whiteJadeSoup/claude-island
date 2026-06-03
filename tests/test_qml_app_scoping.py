"""Static guards for claude_island.qml_app.main().

main() launches the Qt UI, so it can't run headless in CI — these checks use
symtable to catch a specific class of regression without executing it.

Regression: a function-LOCAL `import threading` in main() made `threading` a
local for the whole function, so an earlier `threading.Thread(...)` (added by
the live-pricing wiring) raised UnboundLocalError at startup. The fix promotes
threading to a module-level import. This test fails if anyone reintroduces a
local import that shadows it.
"""
from __future__ import annotations

import pathlib
import symtable


def _main_symbol(name: str):
    src = pathlib.Path("claude_island/qml_app.py").read_text(encoding="utf-8")
    st = symtable.symtable(src, "qml_app.py", "exec")
    main = next(c for c in st.get_children() if c.get_name() == "main")
    return main.lookup(name)


def test_threading_is_module_global_in_main():
    """`threading` must resolve to the module global inside main(), never a
    function-local — otherwise a use-before-local-import is an UnboundLocalError
    at app startup (the bug this guards)."""
    sym = _main_symbol("threading")
    assert sym.is_global() and not sym.is_local(), (
        "threading is function-local in qml_app.main() -- a local `import "
        "threading` would shadow the module global and UnboundLocalError on "
        "any earlier threading.Thread(...) call. Import it at module scope."
    )
