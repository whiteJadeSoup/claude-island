"""Shared `[展开] / [收起]` link button for collapsible sections.

Two surfaces use this control:

* ``SessionDetailPopup``'s LAST PROMPT section (live-session inspector).
* ``RecentsDrawer``'s preview pane LAST PROMPT section (selector).

Both want the same visual + interaction language: a small grey link-styled
button that flips the toggle state when clicked, with the label
swapping between ``[展开]`` (collapsed) and ``[收起]`` (expanded).

What's *not* shared:

* The widgets that hold the actual collapsed / expanded content.
  SessionDetailPopup uses a custom QFontMetrics-elided QLabel +
  lazily-constructed QTextEdit + adjustSize ripple; RecentsDrawer just
  swaps a short QLabel for a wrapped QLabel inside a fixed-width drawer.
  Trying to unify those would couple two unrelated layout strategies
  through a leaky abstraction. This module deliberately stays small —
  just the toggle button + state machine.

Caller pattern:

    self._toggle = CollapsibleLinkButton()
    self._toggle.toggled.connect(self._on_toggle)
    layout.addWidget(self._toggle)

    def _on_toggle(self, expanded: bool) -> None:
        if expanded:
            self._show_full()
        else:
            self._show_collapsed()
"""
from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QPushButton

# Grey link-style — matches ``_STYLE_TEXT_LINK`` in ``expanded_window``.
# Duplicated here on purpose so this module has zero imports from
# ``expanded_window`` (it's the dependency direction we want — eventual
# consumers of this widget might be placed anywhere in ui/, and pulling
# in expanded_window.py would create a circular surface).
_STYLE = """
    QPushButton {
        color: #6b7280;
        background: transparent;
        border: none;
        font-size: 11px;
        padding: 0;
        text-decoration: none;
    }
    QPushButton:hover {
        color: #9ca3af;
        text-decoration: underline;
    }
"""

# Default labels — Chinese to match the live SessionDetailPopup's UI
# language. Callers can override via ``labels=...`` if a different
# locale or wording is needed (e.g. "[expand]" / "[collapse]").
_LABEL_COLLAPSED = "[展开]"
_LABEL_EXPANDED = "[收起]"


class CollapsibleLinkButton(QPushButton):
    """Tiny grey link button that toggles between collapsed / expanded.

    Emits ``toggled(bool)`` *with the new expanded state* on every flip.
    (We override Qt's built-in ``toggled`` semantics so the value passed
    is meaningful — ``True`` = expanded, ``False`` = collapsed.)
    """

    # Signal name shadows QAbstractButton.toggled on purpose: the meaning
    # we want is "the user clicked, state changed" rather than Qt's
    # "checkable button checked/unchecked" semantics.
    state_changed = Signal(bool)

    def __init__(
        self,
        *,
        labels: tuple[str, str] = (_LABEL_COLLAPSED, _LABEL_EXPANDED),
        parent=None,
    ) -> None:
        super().__init__(labels[0], parent)
        self._labels = labels
        self._expanded: bool = False
        self.setStyleSheet(_STYLE)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.clicked.connect(self._on_clicked)

    # ── public API ─────────────────────────────────────────────────────

    def is_expanded(self) -> bool:
        return self._expanded

    def set_expanded(self, expanded: bool) -> None:
        """Programmatic state set without firing ``state_changed``.
        Useful when the parent resets state (e.g. drawer hide → reset
        to collapsed for next open) without wanting to trigger the
        toggle handler again."""
        self._expanded = expanded
        self.setText(self._labels[1] if expanded else self._labels[0])

    # ── internal ───────────────────────────────────────────────────────

    def _on_clicked(self) -> None:
        self._expanded = not self._expanded
        self.setText(self._labels[1] if self._expanded else self._labels[0])
        self.state_changed.emit(self._expanded)
