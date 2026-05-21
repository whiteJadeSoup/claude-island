"""ApprovalCard widget — renders a single ``PendingDecisionView`` for
PreToolUse (Allow/Deny + remember) decisions.

v2 design (2026-05) — fixes the v1 bug where buttons were clipped by
the surrounding ScrollArea when the preview wrapped to several lines.
The v1 ``QSizePolicy.Maximum`` policy let the parent layout compress
the card past its sizeHint and shear the footer off.

Key invariants:

* The footer (Deny + Allow buttons) is a **fixed-height** row and the
  card overall declares ``QSizePolicy.Fixed`` on the vertical axis +
  ``setMinimumHeight(_CARD_MIN_HEIGHT_PX)`` so no parent layout can
  compress the buttons out of view.

* The preview is collapsed by default to a single elided line
  (``_PREVIEW_FOLDED_HEIGHT_PX``). Clicking the header toggles it
  to a scroll-capped expanded form (``_PREVIEW_EXPANDED_HEIGHT_PX``).

* A session-coloured accent runs along the left edge (matching the
  peek slivers in :mod:`decisions_stack`) so the user can scan the
  pile and tell which session each card belongs to.

Threading: Qt main thread only. ``on_resolve`` is invoked
synchronously on click; AppBackend routes to PendingDecisionRegistry.
"""
from __future__ import annotations

import logging
from typing import Callable

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from claude_island.core.pending_decisions import (
    Decision,
    DecisionResult,
    PendingDecisionView,
    RiskLevel,
)
from claude_island.ui.fonts import MONO_FONT_STACK, UI_FONT_STACK
from claude_island.ui.session_color import session_accent
from claude_island.ui.tooltip_style import TOOLTIP_QSS

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Dimensions — all in pixels. Named so future tweaks happen in one place.
# ---------------------------------------------------------------------------

# Minimum vertical footprint. Sum of (top_bar 3 + header ~28 + folded
# preview 22 + footer 40 + body paddings) — a card never compresses
# below this even if the parent layout is starved for space, so the
# footer buttons are always reachable.
_CARD_MIN_HEIGHT_PX = 110

# Top accent bar — tinted by risk level (red / amber / green).
_TOP_BAR_HEIGHT_PX = 3

# Body layout paddings + spacing.
_BODY_MARGIN_PX = 12
_BODY_SPACING_PX = 6
_FOOTER_SPACING_PX = 8

# Preview QLabel: folded shows a single elided line; expanded clamps
# to a small scroll viewport so a long bash command can't blow up
# the card height.
_PREVIEW_FOLDED_HEIGHT_PX = 22
_PREVIEW_EXPANDED_HEIGHT_PX = 80

# Warning row appears only for HIGH risk. Two lines max — anything
# longer wraps and the user has to scroll the preview to read more.
_WARNING_MAX_HEIGHT_PX = 30

# Footer (button row) — kept Fixed so parent compression never eats it.
_BUTTON_ROW_HEIGHT_PX = 40
_BUTTON_PADDING_H_PX = 14

# Chevron glyphs — Unicode arrows; intentionally tiny so they read as
# affordance not as primary action.
_CHEVRON_COLLAPSED = "▾"
_CHEVRON_EXPANDED = "▴"

# Risk → accent colour for the top bar. Same vocabulary as the rest
# of the panel (capsule, status dot).
from claude_island.ui.lab_palette import Color as _C, FontStack as _F
_TOP_BAR_COLOR_BY_RISK: dict[RiskLevel, str] = {
    RiskLevel.HIGH:   _C.red_warm,
    RiskLevel.MEDIUM: _C.amber,
    RiskLevel.LOW:    _C.phosphor,
}

# Risk → tool-icon glyph displayed in the header. Mostly decorative —
# user reads the tool name; the icon is a quick visual cue.
_TOOL_ICON_BY_RISK: dict[RiskLevel, str] = {
    RiskLevel.HIGH:   "⚡",
    RiskLevel.MEDIUM: "🛠",
    RiskLevel.LOW:    "📖",
}

_HIGH_RISK_WARNING_TEMPLATE = (
    "⚠ Allowing this will permit ALL future {tool} calls "
    "in this session without asking."
)

_NO_PREVIEW_PLACEHOLDER = "(no preview)"


# Callback signature: (decision_id, decision)
ResolveCallback = Callable[[str, Decision], None]


# ---------------------------------------------------------------------------
# QSS — pulled out so tests assert on widget structure, not strings.
# ---------------------------------------------------------------------------

# v3 approval card — outlined surfaces, square corners, mono throughout.
# The "Allow" button is the only saturated element on the card (paper bg,
# ink fg) so the primary action stays loudly readable; "Deny" is outline-
# only with red_warm tint so the secondary action reads as deliberate
# (the user is rejecting something).
_CARD_QSS = f"""
QFrame#approvalCard {{
    background-color: rgba(219, 109, 40, 0.08);
    border-radius: 4px;
    border: 1px solid rgba(219, 109, 40, 0.30);
    border-left: 3px solid {_C.red_warm};
}}
QFrame#approvalCardTopBar {{
    border-top-left-radius: 4px;
    border-top-right-radius: 4px;
}}
QLabel {{ color: {_C.paper}; }}
QLabel#approvalCardTitle {{
    font-family: {_F.mono_stack};
    font-size: 13px;
    font-weight: 600;
    letter-spacing: 0.02em;
}}
QLabel#approvalCardSessionBadge {{
    font-family: {_F.mono_stack};
    font-size: 10px;
    color: {_C.paper_dim};
    background-color: transparent;
    border: 1px solid {_C.rule};
    border-radius: 0;
    padding: 2px 8px;
}}
QLabel#approvalCardChevron {{
    color: {_C.paper_faint};
    font-size: 11px;
}}
QLabel#approvalCardPreview {{
    font-family: {_F.mono_stack};
    font-size: 11px;
    color: {_C.paper};
    background-color: {_C.ink};
    padding: 6px 10px;
    border-radius: 0;
    border: 1px solid {_C.rule};
    border-left: 2px solid {_C.rule_bright};
}}
QLabel#approvalCardWarning {{
    font-family: {_F.mono_stack};
    font-size: 10px;
    color: {_C.amber};
    font-weight: 600;
    letter-spacing: 0.02em;
}}
QPushButton#approvalAllow {{
    background-color: {_C.phosphor};
    color: #062911;
    border-radius: 6px;
    padding: 7px {_BUTTON_PADDING_H_PX}px;
    font-size: 12px;
    font-weight: 600;
    letter-spacing: 0.02em;
    border: 1px solid {_C.phosphor};
}}
QPushButton#approvalAllow:hover {{
    background-color: #4ec766;
    border-color: #4ec766;
}}
QPushButton#approvalDeny {{
    background-color: {_C.ink};
    color: {_C.paper};
    border-radius: 6px;
    padding: 7px 12px;
    font-size: 12px;
    letter-spacing: 0.02em;
    border: 1px solid {_C.rule_bright};
}}
QPushButton#approvalDeny:hover {{
    background-color: {_C.surface_hi};
    border-color: {_C.rule_active};
}}
""" + TOOLTIP_QSS


# ---------------------------------------------------------------------------
# Widget
# ---------------------------------------------------------------------------


class ApprovalCard(QFrame):
    """Renders one PreToolUse approval prompt (v2)."""

    # Signal kept for callers that prefer Qt-style wiring; the
    # ``on_resolve`` constructor callback is the recommended path.
    resolved = Signal(str, object)   # (decision_id, Decision)

    def __init__(
        self,
        view: PendingDecisionView,
        *,
        on_resolve: ResolveCallback | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._view = view
        self._on_resolve = on_resolve
        self._expanded = False
        self._build_ui()

    # ── public ──────────────────────────────────────────────────────────

    @property
    def view(self) -> PendingDecisionView:
        return self._view

    @property
    def is_expanded(self) -> bool:
        return self._expanded

    def toggle_expanded(self) -> None:
        """Programmatic equivalent of clicking the header chevron."""
        self._expanded = not self._expanded
        self._preview.setMaximumHeight(
            _PREVIEW_EXPANDED_HEIGHT_PX
            if self._expanded
            else _PREVIEW_FOLDED_HEIGHT_PX
        )
        self._chevron.setText(
            _CHEVRON_EXPANDED if self._expanded else _CHEVRON_COLLAPSED
        )
        self._preview.setWordWrap(self._expanded)

    # ── internal ───────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        self.setObjectName("approvalCard")
        self.setStyleSheet(_CARD_QSS)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        # Fixed vertical policy + min-height is the load-bearing pair
        # for "buttons always visible" — see docstring.
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setMinimumHeight(_CARD_MIN_HEIGHT_PX)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        outer.addWidget(self._build_top_bar())
        outer.addLayout(self._build_body())

    def _build_top_bar(self) -> QFrame:
        bar = QFrame()
        bar.setObjectName("approvalCardTopBar")
        bar.setFixedHeight(_TOP_BAR_HEIGHT_PX)
        bar.setStyleSheet(
            "#approvalCardTopBar { background-color: "
            f"{_TOP_BAR_COLOR_BY_RISK[self._view.risk_level]}; }}"
        )
        return bar

    def _build_body(self) -> QVBoxLayout:
        body = QVBoxLayout()
        body.setContentsMargins(
            _BODY_MARGIN_PX, _BODY_MARGIN_PX - 2,
            _BODY_MARGIN_PX, _BODY_MARGIN_PX - 2,
        )
        body.setSpacing(_BODY_SPACING_PX)

        body.addLayout(self._build_header_row())
        body.addWidget(self._build_preview())
        warning = self._maybe_build_warning()
        if warning is not None:
            body.addWidget(warning)
        body.addLayout(self._build_footer_row())
        return body

    def _build_header_row(self) -> QHBoxLayout:
        header = QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        header.setSpacing(8)

        # v4c: header reads "{session} wants permission for {tool}"
        # — same as prototype-v4c-github.html.  Risk icon stays in the
        # leftmost slot but the title is now a richer phrase.
        icon = QLabel(_TOOL_ICON_BY_RISK[self._view.risk_level])
        icon.setFixedWidth(18)
        header.addWidget(icon)

        sess = self._view.session_name or "session"
        tool = self._view.tool_name or "(unknown tool)"
        title = QLabel()
        title.setObjectName("approvalCardTitle")
        title.setTextFormat(Qt.TextFormat.RichText)
        title.setText(
            f"<span style='font-weight:600'>{sess}</span> "
            f"<span style='color:{_C.paper_dim}'>wants permission for</span> "
            f"<span style='font-weight:600'>{tool}</span>"
        )
        title.setTextInteractionFlags(Qt.TextInteractionFlag.NoTextInteraction)
        header.addWidget(title, 1)

        # v4c: queue-position pill ("1 of 3") on the right of the
        # header — replaces v3's chevron.  Populated by
        # ``set_queue_position`` which StackedDecisionsPanel calls when
        # the active card is rebuilt.  Hidden when there's only one
        # decision in flight.
        self._queue_pill = QLabel("")
        self._queue_pill.setObjectName("approvalQueuePill")
        self._queue_pill.setFixedHeight(20)
        self._queue_pill.setSizePolicy(
            QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed,
        )
        self._queue_pill.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._queue_pill.setStyleSheet(
            f"QLabel#approvalQueuePill {{"
            f"  color: {_C.paper_dim};"
            f"  background: {_C.ink};"
            f"  border: 1px solid {_C.rule};"
            f"  border-radius: 10px;"
            f"  padding: 0 9px;"
            f"  font-size: 11px;"
            f"  font-weight: 500;"
            f"}}"
        )
        self._queue_pill.setVisible(False)
        header.addWidget(self._queue_pill)

        # v3 chevron kept as a 0-width placeholder so any test still
        # looking up "approvalCardChevron" doesn't crash.
        self._chevron = QLabel("")
        self._chevron.setObjectName("approvalCardChevron")
        self._chevron.setFixedSize(0, 0)
        header.addWidget(self._chevron)

        # Make the whole header row clickable for toggling. Implemented
        # by giving the row its own QWidget host below so we can hook
        # mousePressEvent — Qt doesn't expose click events on layouts.
        # NOTE: kept here in code so a re-builder doesn't accidentally
        # add intermediate widgets that swallow the click.
        host = _ClickableRow(on_click=self.toggle_expanded)
        host.setLayout(header)
        host.setCursor(Qt.CursorShape.PointingHandCursor)
        # Wrap host in the same layout slot — done by the caller via
        # returning a layout. Repackage:
        wrapper = QHBoxLayout()
        wrapper.setContentsMargins(0, 0, 0, 0)
        wrapper.addWidget(host)
        return wrapper

    def _build_session_badge(self) -> QLabel:
        badge = QLabel(self._format_session_badge())
        badge.setObjectName("approvalCardSessionBadge")
        accent = session_accent(self._view.session_uuid)
        # Render the colour dot as text-prefix HTML so we don't need
        # an extra widget. font-size matched to badge text so the dot
        # baseline-aligns visually.
        badge.setTextFormat(Qt.TextFormat.RichText)
        badge.setText(
            f"<span style='color:{accent}'>●</span> "
            f"{self._view.session_name}"
        )
        return badge

    def _format_session_badge(self) -> str:
        return f"● {self._view.session_name}"

    def _build_preview(self) -> QLabel:
        self._preview = QLabel(
            self._view.tool_input_preview or _NO_PREVIEW_PLACEHOLDER
        )
        self._preview.setObjectName("approvalCardPreview")
        self._preview.setMaximumHeight(_PREVIEW_FOLDED_HEIGHT_PX)
        # Default folded form is single-line elided; expand toggles
        # wordWrap on so the user can read the rest.
        self._preview.setWordWrap(False)
        self._preview.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        return self._preview

    def _maybe_build_warning(self) -> QLabel | None:
        if self._view.risk_level is not RiskLevel.HIGH:
            return None
        warning = QLabel(
            _HIGH_RISK_WARNING_TEMPLATE.format(
                tool=self._view.tool_name or "this"
            )
        )
        warning.setObjectName("approvalCardWarning")
        warning.setWordWrap(True)
        warning.setMaximumHeight(_WARNING_MAX_HEIGHT_PX)
        return warning

    def _build_footer_row(self) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setContentsMargins(0, 2, 0, 0)
        row.setSpacing(_FOOTER_SPACING_PX)

        # v4c: countdown lives on the footer's leftmost slot, mirrors
        # prototype-v4c-github.html's "1m 4s · Deny · Allow once".
        # Updated by a 1 Hz QTimer below; format from
        # expires_at - now().
        self._countdown_label = QLabel("")
        self._countdown_label.setObjectName("approvalCountdown")
        self._countdown_label.setStyleSheet(
            f"color: {_C.red_warm}; font-size: 12px; font-weight: 600;"
        )
        row.addWidget(self._countdown_label)
        self._update_countdown()
        from PySide6.QtCore import QTimer as _QTimer
        self._countdown_timer = _QTimer(self)
        self._countdown_timer.setInterval(1000)
        self._countdown_timer.timeout.connect(self._update_countdown)
        self._countdown_timer.start()

        # v4c: the "Auto-allow {tool} in this session" checkbox that
        # v3 painted on the card is gone — prototype-v4c-github.html
        # shows only countdown + Deny + Allow once, no checkbox.  The
        # remember-decision flow stays available on the Decision model
        # (other surfaces can still set remember=True), it's only the
        # one-tap-from-this-card path that's removed.
        row.addStretch(1)

        # v4c: prototype labels show "Deny D" and "Allow once A" with
        # the keyboard shortcut printed inline.  Plain text — no
        # rich-text key chip widget — keeps QPushButton happy and
        # still surfaces the keys for muscle memory.
        deny_btn = QPushButton("Deny  D")
        deny_btn.setObjectName("approvalDeny")
        deny_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        deny_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        deny_btn.setFixedHeight(_BUTTON_ROW_HEIGHT_PX - 8)
        deny_btn.clicked.connect(self._on_deny)
        row.addWidget(deny_btn)

        allow_btn = QPushButton("Allow once  A")
        allow_btn.setObjectName("approvalAllow")
        allow_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        allow_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        allow_btn.setFixedHeight(_BUTTON_ROW_HEIGHT_PX - 8)
        allow_btn.clicked.connect(self._on_allow)
        row.addWidget(allow_btn)

        return row

    def _update_countdown(self) -> None:
        """Refresh "1m 4s" countdown text from expires_at - now."""
        from datetime import datetime, timezone
        delta = self._view.expires_at - datetime.now(timezone.utc)
        secs = int(delta.total_seconds())
        if secs <= 0:
            text = "expired"
        elif secs < 60:
            text = f"{secs}s"
        elif secs < 3600:
            m = secs // 60
            s = secs % 60
            text = f"{m}m {s}s" if s else f"{m}m"
        else:
            h = secs // 3600
            m = (secs % 3600) // 60
            text = f"{h}h {m}m" if m else f"{h}h"
        self._countdown_label.setText(text)

    # ── handlers ────────────────────────────────────────────────────────

    def _on_allow(self) -> None:
        # v4c: no remember checkbox on this card — always one-shot allow.
        # Decision.remember stays False; persistence belongs to a future
        # settings surface, not this banner.
        self._emit(Decision(result=DecisionResult.ALLOW, remember=False))

    def _on_deny(self) -> None:
        # v1 kept Deny one-click — no reason text box. Empty reason
        # would violate Decision invariant; supply a sensible default.
        self._emit(Decision(
            result=DecisionResult.DENY,
            reason="denied by user",
        ))

    def _emit(self, decision: Decision) -> None:
        try:
            if self._on_resolve is not None:
                self._on_resolve(self._view.id, decision)
            self.resolved.emit(self._view.id, decision)
        except Exception:
            log.exception("ApprovalCard.on_resolve raised")

    def set_queue_position(self, position: int, total: int) -> None:
        """v4c: surface the "1 of 3" pill on the card's top-right.

        ``position`` is 1-indexed (so the head reads as "1 of N").
        Hidden when there's only one decision in flight (total == 1
        means there's nothing to queue behind — the pill would just
        add chrome without information).
        """
        if total <= 1:
            self._queue_pill.setVisible(False)
            return
        self._queue_pill.setText(f"{position} of {total}")
        self._queue_pill.setVisible(True)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


class _ClickableRow(QFrame):
    """QFrame that fires a callback on mousePressEvent — used for the
    header so the whole row toggles the preview, not just the
    chevron. Qt layouts don't expose mouse events; widget does."""

    def __init__(
        self,
        *,
        on_click: Callable[[], None],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._on_click = on_click
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)

    def mousePressEvent(self, event) -> None:  # type: ignore[override]
        if event.button() == Qt.MouseButton.LeftButton:
            try:
                self._on_click()
            except Exception:
                log.exception("ApprovalCard header click handler raised")
            event.accept()
            return
        super().mousePressEvent(event)
