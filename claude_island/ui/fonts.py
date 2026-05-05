"""Platform-aware font stacks for stylesheets and direct ``QFont`` use.

Why this exists: hardcoding ``'Segoe UI'`` (Windows) or ``'Consolas'``
(Windows monospace) as the first family in a stylesheet's
``font-family`` makes Qt log a 200+ ms ``Populating font family aliases``
warning on platforms where that family doesn't exist (macOS, Linux).
The warning is informational but slows startup and clutters stderr.

Defining the first family per-platform avoids both: the chosen family
exists, no alias resolution is needed, no warning fires. The remaining
fallbacks are kept for graceful degradation if the OS lacks the
expected default (e.g. a Linux box without any of the listed families).

Used by stylesheet builders and any direct ``QFont(family, ...)`` call
that wants the same look as the surrounding stylesheet text.
"""
from __future__ import annotations

import sys

if sys.platform == "darwin":
    # ``-apple-system`` / ``BlinkMacSystemFont`` are CSS-only names
    # and aren't in Qt's font database — naming them as the first
    # family triggers the same alias-resolution warning we're trying
    # to avoid. ``Helvetica Neue`` is universally available on macOS
    # 10.x+ and visually close to the SF system font in body sizes.
    UI_FONT_FAMILY = "Helvetica Neue"
    UI_FONT_STACK = "'Helvetica Neue', 'Helvetica', sans-serif"
    MONO_FONT_FAMILY = "Menlo"
    MONO_FONT_STACK = "'Menlo', 'Monaco', monospace"
elif sys.platform == "win32":
    UI_FONT_FAMILY = "Segoe UI"
    UI_FONT_STACK = "'Segoe UI', sans-serif"
    MONO_FONT_FAMILY = "Consolas"
    MONO_FONT_STACK = "'Consolas', 'Courier New', monospace"
else:
    # Linux / BSD — these families are common across distros.
    UI_FONT_FAMILY = "DejaVu Sans"
    UI_FONT_STACK = "'Cantarell', 'Ubuntu', 'DejaVu Sans', sans-serif"
    MONO_FONT_FAMILY = "DejaVu Sans Mono"
    MONO_FONT_STACK = "'DejaVu Sans Mono', 'Liberation Mono', monospace"
