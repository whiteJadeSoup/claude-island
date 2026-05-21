"""QuestionCard widget — renders an ``ASK_QUESTION`` PendingDecisionView.

Distinct from :class:`ApprovalCard` because AskUserQuestion's semantics
aren't allow / deny — they're "pick an answer or punt to the terminal".
The card surfaces the question text + clickable option list; picking an
option (or hitting Skip) resolves the decision as ``ALLOW`` (Claude is
permitted to invoke AskUserQuestion, which then prompts in the terminal)
and the chosen label is recorded in ``Decision.reason`` for the user
record.

Why we don't relay the answer back to Claude through the hook
============================================================
The hook protocol's PermissionRequest response is allow / deny / defer
— there is **no answer channel**. Claude reads the answer from its own
terminal prompt regardless. Island's role here is:

  * surface the question (so the user sees what's being asked without
    switching to the terminal first); and
  * focus the matching terminal window when an option is picked so the
    user only has to press one digit key to commit.

A future "answer relay" protocol would let island skip the terminal
roundtrip entirely; until then, ``on_focus_terminal`` is the seam.

Threading: Qt main thread only.
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
)
from claude_island.ui.fonts import MONO_FONT_STACK, UI_FONT_STACK
from claude_island.ui.lab_palette import Color as _C, FontStack as _F
from claude_island.ui.session_color import session_accent

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Dimensions
# ---------------------------------------------------------------------------

_CARD_MIN_HEIGHT_PX = 180

# v4c: banner aesthetic — no rounded card chrome, no top stripe. The
# orange-tinted bg + 3px orange left border are the visual signal,
# matching prototype-v4c-github.html's `.decision` rule. _TOP_BAR_HEIGHT_PX
# is kept as a 0-height legacy stub so older callers don't break.
_TOP_BAR_HEIGHT_PX = 0

_BODY_MARGIN_H_PX = 14    # mirrors prototype `padding: 12px 14px`
_BODY_MARGIN_V_PX = 12
_BODY_SPACING_PX = 8
_TEXT_INDENT_PX = 24      # `margin-left: 24px` on .what / .preview / etc.

_QUESTION_TEXT_MAX_HEIGHT_PX = 60
_OPTION_LIST_MAX_HEIGHT_PX = 200
_OPTION_BUTTON_HEIGHT_PX = 30  # tighter than v3 (was 36) to read as banner row

_BUTTON_ROW_HEIGHT_PX = 30

# Small filled orange disc with a "?" glyph — mirrors the prototype's
# `.decision .head .ico` (16x16 circle, white char inside).
_QUESTION_ICON_GLYPH = "?"
_QUESTION_ICON_PX = 16

# Keycap labels for the option buttons. The numbers visually echo
# what the user types in Claude's terminal prompt (1 / 2 / 3 …) so
# they can pick here or there interchangeably.
_OPTION_KEYCAPS: tuple[str, ...] = (
    "1", "2", "3", "4", "5", "6", "7", "8", "9",
)

_HINT_TEXT = "Selecting here also focuses the terminal so Claude can read your answer."

# Decision.reason prefixes — kept short and machine-parseable so a
# future telemetry / replay tool can tell apart "user picked X in
# island UI" from "user skipped to terminal".
_REASON_PICKED_PREFIX = "picked:"
_REASON_SKIPPED = "skipped — answer in terminal"


ResolveCallback = Callable[[str, Decision], None]
FocusTerminalCallback = Callable[[str], None]


# ---------------------------------------------------------------------------
# QSS
# ---------------------------------------------------------------------------

# v4c QSS — banner aesthetic, mirrors prototype-v4c-github.html's
# `.decision` rule.  Token references go through lab_palette so a tint
# tweak in the prototype's :root block propagates here in lockstep.
_QSS = f"""
/* v4c banner: flat, no rounded chrome, only a bottom hairline.
   Mirrors prototype-v4c-github.html's `.decision` rule — dark
   orange-tinted bg + bottom border, nothing else. */
QFrame#questionCard {{
    background-color: #1f1106;
    border: none;
    border-bottom: 1px solid rgba(219, 109, 40, 0.40);
    border-radius: 0;
}}
QFrame#questionCardTopBar {{ background-color: transparent; }}
QLabel {{ color: {_C.paper}; }}

/* Small filled orange disc — same role as prototype `.decision .head .ico`. */
QLabel#questionCardIcon {{
    background-color: {_C.red_warm};
    color: white;
    border-radius: {_QUESTION_ICON_PX // 2}px;
    font-family: {_F.sans_stack};
    font-size: 11px;
    font-weight: 700;
    qproperty-alignment: AlignCenter;
}}

QLabel#questionCardHeaderTitle {{
    font-family: {_F.sans_stack};
    font-size: 13px;
    font-weight: 600;
    color: {_C.paper};
}}

/* Session chip on the right — outlined "● {{session}}" matching ApprovalCard. */
QLabel#questionCardSessionBadge {{
    font-family: {_F.mono_stack};
    font-size: 10.5px;
    color: {_C.paper_dim};
    background-color: transparent;
    border: 1px solid {_C.rule};
    border-radius: 10px;
    padding: 1px 7px;
}}

/* "1 of N" pill — same shape as approval card's queue pill so the
   visual queue indicator reads the same across kinds. */
QLabel#questionQueuePill {{
    color: {_C.paper_dim};
    background: {_C.ink};
    border: 1px solid {_C.rule};
    border-radius: 10px;
    padding: 0 9px;
    font-family: {_F.mono_stack};
    font-size: 11px;
    font-weight: 500;
}}

QLabel#questionCardText {{
    font-family: {_F.sans_stack};
    font-size: 13.5px;
    color: {_C.paper};
}}
QLabel#questionCardMeta {{
    font-family: {_F.mono_stack};
    font-size: 10.5px;
    color: {_C.paper_faint};
    letter-spacing: 0.02em;
}}

/* Hint reads as a quiet caption, not as another card section. */
QLabel#questionCardHint {{
    font-family: {_F.sans_stack};
    font-size: 10.5px;
    color: {_C.paper_faint};
    background: transparent;
    border: none;
    padding: 0;
}}

QPushButton#questionOption {{
    background-color: {_C.ink};
    /* Native button text hidden — the child labels render the visible
       content so they can word-wrap (QPushButton.text() doesn't). */
    color: transparent;
    border: 1px solid {_C.rule};
    border-radius: 6px;
    padding: 0;
    text-align: left;
}}
QPushButton#questionOption:hover {{
    background-color: {_C.surface_hi};
    border-color: {_C.rule_active};
}}
QPushButton#questionOption[picked="true"] {{
    background-color: {_C.accent};
    border-color: {_C.accent};
}}
QLabel#questionOptionKeycap {{
    color: {_C.paper_dim};
    font-family: {_F.mono_stack};
    font-size: 11px;
    font-weight: 500;
}}
QLabel#questionOptionTitle {{
    color: {_C.paper};
    font-family: {_F.sans_stack};
    font-size: 12.5px;
    font-weight: 500;
}}
QLabel#questionOptionDesc {{
    color: {_C.paper_dim};
    font-family: {_F.sans_stack};
    font-size: 11.5px;
}}
QPushButton#questionOption[picked="true"] QLabel#questionOptionTitle,
QPushButton#questionOption[picked="true"] QLabel#questionOptionDesc,
QPushButton#questionOption[picked="true"] QLabel#questionOptionKeycap {{
    color: white;
}}

QPushButton#questionSkip {{
    background-color: transparent;
    color: {_C.paper_dim};
    border: none;
    text-align: left;
    padding: 2px 0;
    font-family: {_F.sans_stack};
    font-size: 11.5px;
}}
QPushButton#questionSkip:hover {{ color: {_C.paper}; text-decoration: underline; }}

QPushButton#questionSubmit {{
    background-color: {_C.accent};
    color: white;
    border-radius: 6px;
    padding: 5px 14px;
    font-family: {_F.sans_stack};
    font-size: 12px;
    font-weight: 600;
    border: 1px solid {_C.accent};
}}
QPushButton#questionSubmit:hover {{ filter: brightness(1.05); }}
QPushButton#questionSubmit:disabled {{
    background-color: {_C.surface};
    border-color: {_C.rule};
    color: {_C.paper_faint};
}}
"""


# ---------------------------------------------------------------------------
# Option button — keeps QPushButton as the outer widget so existing
# tests (``findChildren(QPushButton, "questionOption")``) keep working,
# but internally uses a QHBoxLayout with proper word-wrapping QLabels.
# QPushButton's native text rendering doesn't wrap; we hide it via
# ``color: transparent`` in QSS and let the child labels paint the
# visible content.  ``.text()`` still returns the structured
# "[N]  title\n      description" string so tests asserting on
# ``btn.text()`` keep working without changes.
# ---------------------------------------------------------------------------


class _OptionButton(QPushButton):
    """Clickable option row with wrap-friendly title + description.

    Layout:
        [N]  Title text (bold, wraps)
             Description text (dim, wraps)
    """

    def __init__(
        self,
        *,
        index: int,
        keycap: str,
        title: str,
        description: str,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        # Native text — kept so QPushButton.text() returns content the
        # existing tests assert on.  Visually hidden via QSS
        # (color: transparent); the labels below paint the real text.
        plain = f"[{keycap}]  {title}"
        if description:
            plain += f"\n      {description}"
        self.setText(plain)
        self.setObjectName("questionOption")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Preferred,
        )
        self.setProperty("picked", False)
        self._index = index

        inner = QHBoxLayout(self)
        inner.setContentsMargins(12, 8, 12, 8)
        inner.setSpacing(10)

        keycap_lbl = QLabel(f"[{keycap}]")
        keycap_lbl.setObjectName("questionOptionKeycap")
        keycap_lbl.setFixedWidth(28)
        keycap_lbl.setAlignment(
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop,
        )
        keycap_lbl.setAttribute(
            Qt.WidgetAttribute.WA_TransparentForMouseEvents,
        )
        inner.addWidget(keycap_lbl, 0, Qt.AlignmentFlag.AlignTop)

        content = QVBoxLayout()
        content.setContentsMargins(0, 0, 0, 0)
        content.setSpacing(2)

        title_lbl = QLabel(title)
        title_lbl.setObjectName("questionOptionTitle")
        title_lbl.setWordWrap(True)
        title_lbl.setAttribute(
            Qt.WidgetAttribute.WA_TransparentForMouseEvents,
        )
        content.addWidget(title_lbl)

        if description:
            desc_lbl = QLabel(description)
            desc_lbl.setObjectName("questionOptionDesc")
            desc_lbl.setWordWrap(True)
            desc_lbl.setAttribute(
                Qt.WidgetAttribute.WA_TransparentForMouseEvents,
            )
            content.addWidget(desc_lbl)

        inner.addLayout(content, 1)


# ---------------------------------------------------------------------------
# Widget
# ---------------------------------------------------------------------------


class QuestionCard(QFrame):
    """Renders one ASK_QUESTION pending decision."""

    # Mirror ApprovalCard's signal so callers can wire either widget
    # the same way (the StackedDecisionsPanel relies on a uniform
    # ``resolved`` signal).
    resolved = Signal(str, object)   # (decision_id, Decision)

    def __init__(
        self,
        view: PendingDecisionView,
        *,
        on_resolve: ResolveCallback | None = None,
        on_focus_terminal: FocusTerminalCallback | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._view = view
        self._on_resolve = on_resolve
        self._on_focus_terminal = on_focus_terminal
        self._picked_indices: set[int] = set()
        self._option_buttons: list[QPushButton] = []
        self._submit_btn: QPushButton | None = None
        self._build_ui()

    # ── public ──────────────────────────────────────────────────────────

    @property
    def view(self) -> PendingDecisionView:
        return self._view

    @property
    def picked_indices(self) -> tuple[int, ...]:
        return tuple(sorted(self._picked_indices))

    # ── internal ───────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        self.setObjectName("questionCard")
        self.setStyleSheet(_QSS)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setMinimumHeight(_CARD_MIN_HEIGHT_PX)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        # The 0-height top-bar widget stays so legacy callers / tests
        # looking for ``questionCardTopBar`` still find it.
        outer.addWidget(self._build_top_bar())
        outer.addLayout(self._build_body())

    def _build_top_bar(self) -> QFrame:
        bar = QFrame()
        bar.setObjectName("questionCardTopBar")
        bar.setFixedHeight(_TOP_BAR_HEIGHT_PX)
        return bar

    def _build_body(self) -> QVBoxLayout:
        body = QVBoxLayout()
        body.setContentsMargins(
            _BODY_MARGIN_H_PX, _BODY_MARGIN_V_PX,
            _BODY_MARGIN_H_PX, _BODY_MARGIN_V_PX,
        )
        body.setSpacing(_BODY_SPACING_PX)

        # Row 1 — small "?" disc + topic title + session chip + queue
        # pill.  Single line, mirrors prototype `.decision .head`.
        body.addLayout(self._build_header_row())
        # Row 2 — the question itself, indented 24px to baseline-align
        # with the title text (not the icon).  Same trick the prototype
        # uses on `.what` / `.preview` / `.actions`.
        body.addLayout(self._build_question_row())
        # Row 3 — meta caption (small, mono).
        meta = self._build_meta_label()
        if meta is not None:
            body.addLayout(self._indented_row(meta))
        # Row 4..N — one option button per choice, also indented.
        for w in self._build_option_widgets():
            body.addLayout(self._indented_row(w))
        # Row N+1 — footer (skip on the left, submit on the right).
        body.addLayout(self._build_footer_row())
        # Row N+2 — quiet hint caption.
        body.addLayout(self._indented_row(self._build_hint()))
        return body

    def _indented_row(self, widget: QWidget) -> QHBoxLayout:
        """Wrap ``widget`` in an HBox indented 24px from the left edge
        so it lines up under the title text rather than the icon.
        Mirrors the prototype's `margin-left: 24px` rule on
        `.decision .what` / `.preview` / `.actions`."""
        row = QHBoxLayout()
        row.setContentsMargins(_TEXT_INDENT_PX, 0, 0, 0)
        row.setSpacing(0)
        row.addWidget(widget)
        return row

    def _build_header_row(self) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(8)

        icon = QLabel(_QUESTION_ICON_GLYPH)
        icon.setObjectName("questionCardIcon")
        icon.setFixedSize(_QUESTION_ICON_PX, _QUESTION_ICON_PX)
        row.addWidget(icon)

        # Prefer the question's header (a short topic label) over the
        # tool name — header is what the human cares about. Tool name
        # appears in the meta line below.
        title_text = self._view.question_header or self._view.tool_name or "Question"
        title = QLabel(title_text)
        title.setObjectName("questionCardHeaderTitle")
        row.addWidget(title, 1)

        badge = QLabel()
        badge.setObjectName("questionCardSessionBadge")
        accent = session_accent(self._view.session_uuid)
        badge.setTextFormat(Qt.TextFormat.RichText)
        badge.setText(
            f"<span style='color:{accent}'>●</span> "
            f"{self._view.session_name}"
        )
        row.addWidget(badge)

        # v4c: "1 of N" pill, populated by ``set_queue_position`` from
        # StackedDecisionsPanel.  Hidden when only one decision in
        # flight — see :meth:`set_queue_position`.
        self._queue_pill = QLabel("")
        self._queue_pill.setObjectName("questionQueuePill")
        self._queue_pill.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._queue_pill.setSizePolicy(
            QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed,
        )
        self._queue_pill.setVisible(False)
        row.addWidget(self._queue_pill)

        return row

    def _build_question_row(self) -> QHBoxLayout:
        label = QLabel(self._view.question_text or "")
        label.setObjectName("questionCardText")
        label.setWordWrap(True)
        label.setMaximumHeight(_QUESTION_TEXT_MAX_HEIGHT_PX)
        label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        return self._indented_row(label)

    def _build_meta_label(self) -> QLabel | None:
        bits: list[str] = []
        if self._view.tool_name:
            bits.append(f"via {self._view.tool_name}")
        if self._view.multi_select:
            bits.append("multi-select")
        if not bits:
            return None
        label = QLabel(" · ".join(bits))
        label.setObjectName("questionCardMeta")
        return label

    def _build_option_widgets(self) -> list[QPushButton]:
        widgets: list[QPushButton] = []
        descs = self._view.question_option_descriptions
        for idx, label in enumerate(self._view.question_options):
            keycap = (
                _OPTION_KEYCAPS[idx]
                if idx < len(_OPTION_KEYCAPS)
                else "·"
            )
            description = ""
            if descs and idx < len(descs) and descs[idx]:
                description = descs[idx]
            btn = _OptionButton(
                index=idx,
                keycap=keycap,
                title=label,
                description=description,
            )
            btn.clicked.connect(
                lambda _checked=False, i=idx: self._on_option(i)
            )
            widgets.append(btn)
            self._option_buttons.append(btn)
        return widgets

    def _build_footer_row(self) -> QHBoxLayout:
        row = QHBoxLayout()
        # Indent 24px to match the question/options column — keeps the
        # "Skip" link visually anchored under the question text.
        row.setContentsMargins(_TEXT_INDENT_PX, 4, 0, 0)
        row.setSpacing(8)
        row.setAlignment(Qt.AlignmentFlag.AlignVCenter)

        skip_btn = QPushButton("Skip — answer in terminal")
        skip_btn.setObjectName("questionSkip")
        skip_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        skip_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        skip_btn.clicked.connect(self._on_skip)
        row.addWidget(skip_btn, 1)

        if self._view.multi_select:
            submit_btn = QPushButton("Submit")
            submit_btn.setObjectName("questionSubmit")
            submit_btn.setCursor(Qt.CursorShape.PointingHandCursor)
            submit_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            submit_btn.setEnabled(False)
            submit_btn.setFixedHeight(_BUTTON_ROW_HEIGHT_PX - 6)
            submit_btn.clicked.connect(self._on_submit_multi)
            row.addWidget(submit_btn)
            self._submit_btn = submit_btn
        return row

    def _build_hint(self) -> QLabel:
        hint = QLabel(f"ℹ {_HINT_TEXT}")
        hint.setObjectName("questionCardHint")
        hint.setWordWrap(True)
        return hint

    # ── public API used by StackedDecisionsPanel ───────────────────────

    def set_queue_position(self, position: int, total: int) -> None:
        """v4c: surface the "1 of N" pill on the card's top-right.

        Hidden when ``total <= 1`` — the pill carries no information
        in that case, so its presence would just add chrome.  Mirrors
        :meth:`ApprovalCard.set_queue_position` so the StackedDecisions
        panel can call the same method on either card kind.
        """
        if total <= 1:
            self._queue_pill.setVisible(False)
            return
        self._queue_pill.setText(f"{position} of {total}")
        self._queue_pill.setVisible(True)

    # ── handlers ────────────────────────────────────────────────────────

    def _on_option(self, idx: int) -> None:
        if self._view.multi_select:
            self._toggle_option(idx)
            return
        # Single-select: lock the visual selection state on this button
        # then resolve immediately. The brief hold (no async delay
        # here in the widget — the StackedDecisionsPanel sequences any
        # advance animation) gives the user feedback that their click
        # registered.
        self._mark_picked(idx, exclusive=True)
        self._emit_picked([idx])

    def _toggle_option(self, idx: int) -> None:
        if idx in self._picked_indices:
            self._picked_indices.discard(idx)
            self._set_picked_visual(idx, False)
        else:
            self._picked_indices.add(idx)
            self._set_picked_visual(idx, True)
        if self._submit_btn is not None:
            self._submit_btn.setEnabled(bool(self._picked_indices))

    def _mark_picked(self, idx: int, *, exclusive: bool) -> None:
        if exclusive:
            self._picked_indices = {idx}
            for i, btn in enumerate(self._option_buttons):
                self._set_picked_visual(i, i == idx)
        else:
            self._picked_indices.add(idx)
            self._set_picked_visual(idx, True)

    def _set_picked_visual(self, idx: int, picked: bool) -> None:
        btn = self._option_buttons[idx]
        btn.setProperty("picked", picked)
        # Force style refresh — Qt doesn't re-parse QSS automatically
        # when a dynamic property changes.
        btn.style().unpolish(btn)
        btn.style().polish(btn)

    def _on_submit_multi(self) -> None:
        if not self._picked_indices:
            return
        self._emit_picked(sorted(self._picked_indices))

    def _on_skip(self) -> None:
        self._emit_decision(
            Decision(result=DecisionResult.ALLOW, reason=_REASON_SKIPPED),
            focus_terminal=True,
        )

    def _emit_picked(self, indices: list[int]) -> None:
        labels = [self._view.question_options[i] for i in indices]
        reason = f"{_REASON_PICKED_PREFIX} " + " | ".join(labels)
        # Pack the answer for the hook layer's updatedInput merge —
        # comma list mirrors open-vibe-island for multi-select.
        answer_value = ", ".join(labels)
        answers: tuple[tuple[str, str], ...] = (
            (self._view.question_text or "", answer_value),
        ) if self._view.question_text else ()
        self._emit_decision(
            Decision(
                result=DecisionResult.ALLOW,
                reason=reason,
                answers=answers,
            ),
            focus_terminal=True,
        )

    def _emit_decision(self, decision: Decision, *, focus_terminal: bool) -> None:
        try:
            if focus_terminal and self._on_focus_terminal is not None:
                self._on_focus_terminal(self._view.session_uuid)
        except Exception:
            log.exception("QuestionCard.on_focus_terminal raised")
        try:
            if self._on_resolve is not None:
                self._on_resolve(self._view.id, decision)
            self.resolved.emit(self._view.id, decision)
        except Exception:
            log.exception("QuestionCard.on_resolve raised")
