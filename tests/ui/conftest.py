"""Ensure a single GUI-capable Qt application exists for all `tests/ui`.

Several UI tests historically did ``QCoreApplication.instance() or
QCoreApplication([])`` at import time. That creates a *core* (non-GUI) app,
which then makes QML-loading tests (which need a ``QGuiApplication``) crash —
you cannot have both, and a QML scene cannot render under a bare
``QCoreApplication``.

conftest.py is imported before the test modules in its directory, so creating
the ``QGuiApplication`` here first guarantees the shared singleton is
GUI-capable. ``QGuiApplication`` is a strict superset of ``QCoreApplication``,
so the older ``QCoreApplication.instance()`` calls simply return this instance.
"""
from __future__ import annotations

import os

# Must be set before the application is constructed so headless CI / no-display
# environments don't fail to find a windowing platform.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtGui import QGuiApplication

_app = QGuiApplication.instance() or QGuiApplication([])
