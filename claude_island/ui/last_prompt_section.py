"""Reusable LAST PROMPT section — shared between SessionDetailPopup
(online sessions) and RecentsDrawer's preview pane (offline sessions).

Behaviour:
- Collapsed: single-line preview, elided to fit the host pane width.
- Expanded: read-only QTextEdit with WrapAnywhere + scrolling. Long
  unbroken tokens (URLs, API keys) wrap. Hard caps at 2000 chars so
  a multi-MB paste can't blow up Qt's text engine or the popup height.
- Toggle button is hidden when the full text already fits collapsed.

The widget owns its own state (expanded flag, lazy QTextEdit). Parent
hooks two things:
- ``refit(width)`` from ``resizeEvent`` so the ellipsis tracks pane
  resizes.
- ``expansion_changed`` Signal — SessionDetailPopup connects this to
  ``adjustSize()`` so the popup grows when the user expands. RecentsDrawer
  doesn't need to react (its preview lives in a fixed-width column).

Pre-extraction this lived inline in SessionDetailPopup as ~80 lines
of mixed state + helper methods. Extracting kept both surfaces visually
identical while letting the prompt section evolve in one place.
"""
from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QFontMetrics, QTextOption
from PySide6.QtWidgets import (
    QFrame, QHBoxLayout, QLabel, QSizePolicy, QTextEdit, QVBoxLayout, QWidget,
)

from claude_island.ui.collapsible import CollapsibleLinkButton

_TITLE_STYLE = "color: #888; font-size: 10px; letter-spacing: 1px;"
_BODY_STYLE = "color: #c9c9c9; font-size: 12px;"

# Minimum width reserved for the [展开] / [收起] toggle button. CJK
# rendering in 11 px QPushButton without explicit width tends to ask
# for ~32 px from QFontMetrics — Qt then clips to "[展" in a narrow
# preview column (the RecentsDrawer at 220 px). 56 px covers both
# labels plus a couple px of padding without ever overflowing.
_TOGGLE_MIN_W = 56

# Hard cap on what we'll render in expanded mode. A pasted multi-MB
# transcript blob would otherwise hang Qt's text shaping. Diagnostic
# value past this length is near zero — the user can open the .jsonl
# directly via the Transcript row if they need the full content.
_MAX_EXPANDED_CHARS = 2000

# Collapsed-line truncation: take the first newline-terminated line
# then cap to 80 chars. The QFontMetrics elide pass below trims it
# further to fit the actual pane width.
_COLLAPSED_FIRST_LINE_CHARS = 80


def collapse_prompt(text: str) -> str:
    """One-line preview: first line, capped at 80 chars, ``…`` if elided.

    Public so callers that don't need the whole widget (a row's hover
    tooltip, for example) can still produce the same preview text."""
    if not text:
        return ""
    first = text.split("\n", 1)[0]
    elided = (first != text) or len(first) > _COLLAPSED_FIRST_LINE_CHARS
    if len(first) > _COLLAPSED_FIRST_LINE_CHARS:
        first = first[:_COLLAPSED_FIRST_LINE_CHARS - 1]
    return first + ("…" if elided else "")


class LastPromptSection(QWidget):
    """LAST PROMPT block with click-to-expand toggle.

    ``available_width`` is the expected rendered width for QFontMetrics
    eliding — pass the host pane's inner width. Call ``refit(width)``
    from the parent's resizeEvent if the pane can resize. ``title``
    overrideable so a future surface (Activity log, Snippets pane)
    could use the same widget under a different label.
    """

    expansion_changed = Signal(bool)  # True when expanded, False when collapsed

    def __init__(
        self,
        prompt_text: str,
        *,
        available_width: int,
        title: str = "LAST PROMPT",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._raw_prompt = prompt_text or ""
        self._available_width = max(40, available_width)
        self._expanded = False
        self._collapsed_text = collapse_prompt(self._raw_prompt)

        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        self._layout = layout

        # Header: title + toggle (toggle hidden until we know we're truncating).
        # ``setMinimumWidth`` on the toggle is critical — without it the
        # CJK font metrics for "[展开]" / "[收起]" undersize the button
        # in narrow preview columns and Qt clips it to "[展" (visible
        # bug in the RecentsDrawer preview at 220 px width). Fix-the-
        # -size at the lower bound of what the widest label ("[收起]")
        # actually needs at the configured 11 px font.
        head = QHBoxLayout()
        title_lbl = QLabel(title)
        title_lbl.setStyleSheet(_TITLE_STYLE)
        head.addWidget(title_lbl)
        head.addStretch()
        self._toggle = CollapsibleLinkButton()
        self._toggle.setMinimumWidth(_TOGGLE_MIN_W)
        self._toggle.clicked.connect(self._on_toggle)
        self._toggle.hide()
        head.addWidget(self._toggle)
        layout.addLayout(head)

        # Collapsed body — single line, no wrap. Width-driven elide
        # in _set_collapsed_text. Ignored h-policy so unbroken tokens
        # (URLs, API keys) can't push the host pane wider via
        # minimumSizeHint = "longest run" semantics of QLabel(wrap=True).
        self._body = QLabel("")
        self._body.setStyleSheet(_BODY_STYLE)
        self._body.setWordWrap(False)
        self._body.setSizePolicy(
            QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred,
        )
        layout.addWidget(self._body)
        self._set_collapsed_text()

        # Expanded view is built lazily on first toggle — collapsed-only
        # is the common path, no point paying the QTextEdit cost upfront.
        self._full_view: QTextEdit | None = None

    # ── public API ──────────────────────────────────────────────────────

    def refit(self, available_width: int | None = None) -> None:
        """Re-elide the collapsed preview. Call from parent resizeEvent
        / showEvent so the ellipsis tracks pane width changes (and the
        first-show case where pre-show QFontMetrics under-reports CJK)."""
        if available_width is not None:
            self._available_width = max(40, available_width)
        self._set_collapsed_text()

    def is_expanded(self) -> bool:
        return self._expanded

    # ── internal ────────────────────────────────────────────────────────

    def _set_collapsed_text(self) -> None:
        fm = QFontMetrics(self._body.font())
        elided = fm.elidedText(
            self._collapsed_text,
            Qt.TextElideMode.ElideRight,
            self._available_width,
        )
        self._body.setText(elided)
        # Show the toggle iff content was actually clipped or has
        # additional newlines past the first line.
        truncated = (elided != self._collapsed_text) or ("\n" in self._raw_prompt)
        if not self._expanded:
            self._toggle.setVisible(truncated)

    def _on_toggle(self) -> None:
        self._expanded = not self._expanded
        if self._expanded:
            full = self._raw_prompt
            if len(full) > _MAX_EXPANDED_CHARS:
                full = full[:_MAX_EXPANDED_CHARS - 3] + "…"
            if self._full_view is None:
                self._full_view = self._build_full_view()
                self._layout.addWidget(self._full_view)
            self._full_view.setPlainText(full)
            self._full_view.show()
            self._body.hide()
            self._toggle.set_expanded(True)
        else:
            if self._full_view is not None:
                self._full_view.hide()
            self._set_collapsed_text()  # re-elide in case width changed
            self._body.show()
            self._toggle.set_expanded(False)
        self.expansion_changed.emit(self._expanded)

    def _build_full_view(self) -> QTextEdit:
        # Critical settings: see expanded_window comments pre-extraction.
        # WidgetWidth + minWidth=0 + Ignored h-policy together prevent
        # the QTextEdit from driving the host pane wider via its content
        # sizeHint. Fixed (not max) height so the popup's adjustSize
        # doesn't over-allocate vertical space when collapsed.
        view = QTextEdit()
        view.setReadOnly(True)
        view.setFrameStyle(QFrame.Shape.NoFrame)
        view.setWordWrapMode(QTextOption.WrapMode.WrapAnywhere)
        view.setLineWrapMode(QTextEdit.LineWrapMode.WidgetWidth)
        view.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        view.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        view.setMinimumWidth(0)
        view.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        view.setFixedHeight(180)
        view.setStyleSheet(
            "QTextEdit {"
            "    color: #d4d4d4;"
            "    background: transparent;"
            "    border: none;"
            "    padding: 0;"
            "    font-size: 12px;"
            "}"
            "QTextEdit QScrollBar:vertical {"
            "    background: transparent;"
            "    width: 6px;"
            "    margin: 2px;"
            "}"
            "QTextEdit QScrollBar::handle:vertical {"
            "    background: #3a3a3a;"
            "    border-radius: 3px;"
            "    min-height: 20px;"
            "}"
            "QTextEdit QScrollBar::add-line:vertical,"
            "QTextEdit QScrollBar::sub-line:vertical {"
            "    height: 0;"
            "}"
        )
        return view

    def showEvent(self, event):  # type: ignore[override]
        super().showEvent(event)
        # First-show refit — pre-show QFontMetrics under-reports CJK
        # glyph widths by ~40%, so any pre-show eliding skips the
        # toggle for prompts that DO need to be elided once realised.
        self._set_collapsed_text()
