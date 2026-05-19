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
from PySide6.QtCore import QSize

from claude_island.core.pending_decisions import (
    Decision,
    DecisionResult,
    PendingDecisionView,
)
from claude_island.ui.fonts import MONO_FONT_STACK, UI_FONT_STACK
from claude_island.ui.session_color import session_accent

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Dimensions
# ---------------------------------------------------------------------------

_CARD_MIN_HEIGHT_PX = 180
_TOP_BAR_HEIGHT_PX = 3

_BODY_MARGIN_PX = 12
_BODY_SPACING_PX = 6

_QUESTION_TEXT_MAX_HEIGHT_PX = 60
_OPTION_LIST_MAX_HEIGHT_PX = 200  # scrolls internally past this
_OPTION_ROW_MIN_HEIGHT_PX = 36
_OPTION_BUTTON_HEIGHT_PX = 36

_BUTTON_ROW_HEIGHT_PX = 36

# The ASK_QUESTION top-bar colour is fixed (medium / amber) — questions
# have no risk gradient like Bash/Read do.
_QUESTION_TOP_BAR_COLOR = "#f59e0b"

# Decoration only — question kind icon.
_QUESTION_TOOL_ICON = "❓"

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

_QSS = f"""
QFrame#questionCard {{
    background-color: #1f1f1f;
    border-radius: 10px;
    border: 1px solid #2a2a2a;
}}
QFrame#questionCardTopBar {{
    background-color: {_QUESTION_TOP_BAR_COLOR};
    border-top-left-radius: 10px;
    border-top-right-radius: 10px;
}}
QLabel {{ color: #e8e8e8; }}
QLabel#questionCardHeaderTitle {{
    font-family: {UI_FONT_STACK};
    font-size: 13px;
    font-weight: 600;
    color: #fff;
}}
QLabel#questionCardSessionBadge {{
    font-family: {MONO_FONT_STACK};
    font-size: 10px;
    color: #ddd;
    background-color: rgba(255,255,255,0.06);
    border-radius: 9px;
    padding: 2px 8px;
}}
QLabel#questionCardText {{
    font-family: {UI_FONT_STACK};
    font-size: 13px;
    color: #fff;
}}
QLabel#questionCardMeta {{
    font-family: {MONO_FONT_STACK};
    font-size: 10px;
    color: #888;
}}
QLabel#questionCardHint {{
    font-family: {UI_FONT_STACK};
    font-size: 10px;
    color: #888;
    background-color: #161616;
    border-top: 1px solid #2a2a2a;
    padding: 6px 10px;
    border-bottom-left-radius: 10px;
    border-bottom-right-radius: 10px;
}}
/* Option container — clickable QPushButton with no text of its own.
   Inner labels carry the visible content so we get word-wrap on the
   description (QPushButton.text does not wrap). All inner labels run
   with WA_TransparentForMouseEvents so clicks reach the button. */
QPushButton#questionOption {{
    background-color: #0e0e0e;
    border: 1px solid #2a2a2a;
    border-radius: 8px;
    padding: 0;          /* container has its own inner layout margins */
    text-align: left;
}}
QPushButton#questionOption:hover {{
    background-color: #161616;
    border-color: #3a3a3a;
}}
/* Inner labels inherit colour from the button via dynamic property
   `picked`, but Qt won't cascade :hover into child QLabel selectors
   reliably, so we colour them explicitly per-state. */
QPushButton#questionOption QLabel#questionOptionKeycap {{
    font-family: {MONO_FONT_STACK};
    font-size: 11px;
    font-weight: 600;
    color: {_QUESTION_TOP_BAR_COLOR};
    background-color: rgba(245, 158, 11, 0.10);
    border: 1px solid rgba(245, 158, 11, 0.28);
    border-radius: 5px;
    padding: 2px 7px;
    min-width: 12px;
}}
QPushButton#questionOption QLabel#questionOptionLabel {{
    font-family: {UI_FONT_STACK};
    font-size: 13px;
    font-weight: 600;
    color: #ffffff;
}}
QPushButton#questionOption QLabel#questionOptionDesc {{
    font-family: {UI_FONT_STACK};
    font-size: 11.5px;
    color: #a5a5a5;
    line-height: 1.45;
}}
QPushButton#questionOption[picked="true"] {{
    background-color: #1d4ed8;
    border-color: #1d4ed8;
}}
QPushButton#questionOption[picked="true"]:hover {{
    background-color: #2563eb;
}}
QPushButton#questionOption[picked="true"] QLabel#questionOptionKeycap {{
    color: #ffffff;
    background-color: rgba(255, 255, 255, 0.18);
    border-color: rgba(255, 255, 255, 0.30);
}}
QPushButton#questionOption[picked="true"] QLabel#questionOptionLabel {{
    color: #ffffff;
}}
QPushButton#questionOption[picked="true"] QLabel#questionOptionDesc {{
    color: rgba(255, 255, 255, 0.82);
}}
QPushButton#questionSkip {{
    background-color: transparent;
    color: #888;
    border: none;
    text-align: left;
    padding: 4px 0;
    font-family: {UI_FONT_STACK};
    font-size: 11px;
}}
QPushButton#questionSkip:hover {{ color: #ccc; }}
QPushButton#questionSubmit {{
    background-color: #1d4ed8;
    color: white;
    border-radius: 6px;
    padding: 6px 14px;
    font-family: {UI_FONT_STACK};
    font-size: 12px;
    font-weight: 600;
    border: none;
}}
QPushButton#questionSubmit:hover {{ background-color: #2563eb; }}
QPushButton#questionSubmit:disabled {{
    background-color: #2a2a2a;
    color: #666;
}}
"""


# ---------------------------------------------------------------------------
# _OptionButton — QPushButton subclass that defers sizing to its layout.
# ---------------------------------------------------------------------------


class _OptionButton(QPushButton):
    """A QPushButton that takes its size from its inner QLayout instead
    of from its (empty) ``text`` property.

    The default ``QPushButton.sizeHint()`` is computed from the text
    metric of ``self.text()``. We don't set text on the button — the
    visible content lives in child QLabels held by a QHBoxLayout — so
    the default sizeHint would collapse to the no-text minimum (~24 px
    tall) and the wrapped description label would render outside the
    button's drawn frame. Forwarding sizeHint to the layout makes the
    button grow vertically as the wrapped description grows.

    ``minimumSizeHint`` is forwarded the same way so layout managers
    don't squash the button below the wrapped content height.

    ``heightForWidth`` is enabled so parent layouts that allocate
    narrower-than-preferred widths (the decisions stack at very small
    panel widths) still get the correct wrapped height.
    """

    def sizeHint(self) -> QSize:  # type: ignore[override]
        lay = self.layout()
        if lay is None:
            return super().sizeHint()
        h = lay.sizeHint().height()
        w = lay.sizeHint().width()
        # Honour the floor so a description-less option still feels
        # like a real button, not a single-line text strip.
        return QSize(w, max(h, _OPTION_BUTTON_HEIGHT_PX))

    def minimumSizeHint(self) -> QSize:  # type: ignore[override]
        lay = self.layout()
        if lay is None:
            return super().minimumSizeHint()
        return QSize(lay.minimumSize().width(), lay.minimumSize().height())

    def hasHeightForWidth(self) -> bool:  # type: ignore[override]
        return True

    def heightForWidth(self, w: int) -> int:  # type: ignore[override]
        lay = self.layout()
        if lay is None or not lay.hasHeightForWidth():
            return super().heightForWidth(w)
        return max(lay.heightForWidth(w), _OPTION_BUTTON_HEIGHT_PX)


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

        outer.addWidget(self._build_top_bar())
        outer.addLayout(self._build_body())
        outer.addWidget(self._build_hint())

    def _build_top_bar(self) -> QFrame:
        bar = QFrame()
        bar.setObjectName("questionCardTopBar")
        bar.setFixedHeight(_TOP_BAR_HEIGHT_PX)
        return bar

    def _build_body(self) -> QVBoxLayout:
        body = QVBoxLayout()
        body.setContentsMargins(
            _BODY_MARGIN_PX, _BODY_MARGIN_PX - 2,
            _BODY_MARGIN_PX, _BODY_MARGIN_PX - 2,
        )
        body.setSpacing(_BODY_SPACING_PX)

        body.addLayout(self._build_header_row())
        body.addWidget(self._build_question_label())
        body.addWidget(self._build_meta_label())
        for w in self._build_option_widgets():
            body.addWidget(w)
        body.addLayout(self._build_footer_row())
        return body

    def _build_header_row(self) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(8)

        icon = QLabel(_QUESTION_TOOL_ICON)
        icon.setFixedWidth(18)
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
        return row

    def _build_question_label(self) -> QLabel:
        label = QLabel(self._view.question_text or "")
        label.setObjectName("questionCardText")
        label.setWordWrap(True)
        label.setMaximumHeight(_QUESTION_TEXT_MAX_HEIGHT_PX)
        label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        return label

    def _build_meta_label(self) -> QLabel:
        bits: list[str] = []
        if self._view.tool_name:
            bits.append(f"via {self._view.tool_name}")
        if self._view.multi_select:
            bits.append("multi-select")
        label = QLabel(" · ".join(bits) if bits else "")
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
            desc = descs[idx] if descs and idx < len(descs) else ""
            btn = self._make_option_button(idx, keycap, label, desc)
            widgets.append(btn)
            self._option_buttons.append(btn)
        return widgets

    def _make_option_button(
        self, idx: int, keycap: str, label: str, desc: str,
    ) -> QPushButton:
        """Build one option as a clickable QPushButton with an inner
        layout. The button itself carries no text (QPushButton.text does
        not word-wrap, which is why long descriptions used to clip);
        instead, child QLabels carry the visible content and the
        description label sets ``setWordWrap(True)`` so it grows
        vertically to fit. All inner labels are
        ``WA_TransparentForMouseEvents`` so clicks reach the button.

        Layout::

            ┌──────────────────────────────────────────────┐
            │ ┌──┐  Label (semibold, white)                │
            │ │1·│  Description wraps across as many       │
            │ └──┘  lines as needed, never clips           │
            └──────────────────────────────────────────────┘

        The keycap is top-aligned against the label so multi-line
        descriptions don't visually drag the number off the title row.
        """
        btn = _OptionButton()
        btn.setObjectName("questionOption")
        btn.setCursor(Qt.CursorShape.PointingHandCursor)
        btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        btn.setProperty("picked", False)
        btn.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)

        outer = QHBoxLayout(btn)
        # Inner padding mirrors the previous text padding (7px 10px)
        # so visual rhythm vs. the question text above stays unchanged.
        outer.setContentsMargins(10, 8, 12, 9)
        outer.setSpacing(10)
        outer.setAlignment(Qt.AlignmentFlag.AlignTop)

        keycap_lbl = QLabel(keycap)
        keycap_lbl.setObjectName("questionOptionKeycap")
        keycap_lbl.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        keycap_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        keycap_lbl.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        outer.addWidget(keycap_lbl, 0, Qt.AlignmentFlag.AlignTop)

        text_col = QVBoxLayout()
        text_col.setContentsMargins(0, 0, 0, 0)
        text_col.setSpacing(3)

        label_lbl = QLabel(label)
        label_lbl.setObjectName("questionOptionLabel")
        label_lbl.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        # Wrap the label too — short labels stay on one line; rare long
        # labels (some agents emit option names ≥ 60 chars) wrap rather
        # than push the keycap off-screen.
        label_lbl.setWordWrap(True)
        label_lbl.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        text_col.addWidget(label_lbl)

        if desc:
            desc_lbl = QLabel(desc)
            desc_lbl.setObjectName("questionOptionDesc")
            desc_lbl.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
            desc_lbl.setWordWrap(True)
            desc_lbl.setSizePolicy(
                QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred,
            )
            text_col.addWidget(desc_lbl)

        outer.addLayout(text_col, 1)
        btn.clicked.connect(lambda _checked=False, i=idx: self._on_option(i))
        return btn

    def _build_footer_row(self) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setContentsMargins(0, 4, 0, 0)
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
