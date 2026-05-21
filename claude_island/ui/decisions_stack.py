"""StackedDecisionsPanel — renders pending decisions as a pile of cards.

UX model (`design/2026-05-approval-card-v2/prototype.html`):

* Only the **head** of the queue is interactive — a full ApprovalCard
  or QuestionCard. The user must decide it before any other can be
  reached. Forcing serial resolution prevents the user from
  cherry-picking the easy decisions and stranding hard ones.

* The next up-to-``_MAX_PEEKS`` decisions appear above the active card
  as thin slivers (~18–22 px) showing session + tool + risk. Users
  see queue depth at a glance without being able to interact with
  the queue.

* Anything beyond the visible peeks collapses into a "+N more queued
  behind" label so vertical footprint stays bounded regardless of
  registry size.

Threading: Qt main thread only. Driven by
``world.observable().pipe(distinct_until_changed(render_key)).subscribe(panel.render)``
from ``__main__.py``.
"""
from __future__ import annotations

import logging
from typing import Callable, Iterable

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QFrame,
    QGraphicsOpacityEffect,
    QHBoxLayout,
    QLabel,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from claude_island.core.pending_decisions import (
    Decision,
    DecisionKind,
    PendingDecisionView,
    RiskLevel,
)
from claude_island.ui.approval_card import ApprovalCard
from claude_island.ui.fonts import MONO_FONT_STACK, UI_FONT_STACK
from claude_island.ui.lab_palette import Color as _C, FontStack as _F
from claude_island.ui.question_card import QuestionCard
from claude_island.ui.session_color import session_accent

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Spec — kept in one place so future width / count tweaks don't require
# hunting through three files.
# ---------------------------------------------------------------------------

# Maximum number of peek slivers shown behind the active card. Beyond
# this, an "+N more queued behind" label takes over (see prototype
# scenario "Overflow (6 total)").
_MAX_PEEKS = 3

# Sliver heights — one per depth. depth 1 sits just above the active
# card (most visible), depth 3 is the deepest (most faded). Earlier
# slivers are taller so the visual progression reads as "fading
# into the distance".
_PEEK_HEIGHTS_PX: tuple[int, int, int] = (22, 20, 18)
# Horizontal inset per depth — deeper slivers tuck further inside.
_PEEK_INSETS_PX: tuple[int, int, int] = (8, 14, 20)
# Opacity per depth — depth 1 is opaque; deeper fades.
_PEEK_OPACITIES: tuple[float, float, float] = (1.0, 0.8, 0.6)

# Pile up the active card slightly over the lowest peek so the stack
# reads as physically layered, not as a list.
_ACTIVE_CARD_NEGATIVE_TOP_PX = 6

_PEEK_BAR_HEIGHT_PX = 2
_PEEK_BODY_PADDING_H_PX = 10
_PEEK_BODY_PADDING_V_PX = 2

_HEADER_LABEL = "PENDING DECISIONS"
_OVERFLOW_LABEL_TEMPLATE = "+{n} more queued behind"
_COUNTER_TEMPLATE = "decide one at a time · {queued} queued"

# Decision kind → card factory. Keeps the dispatch open for future
# kinds (e.g. USER_PROMPT_SUBMIT review card) without growing an
# if/elif chain inside this module.
ResolveCallback = Callable[[str, Decision], None]
FocusTerminalCallback = Callable[[str], None]


# Risk → (background tint, text colour) for the small risk pill on each
# peek sliver.  v3 keeps the three semantic colours (red/amber/green for
# high/medium/low) because the user has learned what they mean, but
# routes them through lab_palette so the pill matches the row strip's
# tint family across surfaces.  Background tints stay rgba with low
# alpha — a stronger fill would scream more than the v3 quiet aesthetic
# wants on a tiny pill.
_RISK_PILL_COLORS: dict[RiskLevel, tuple[str, str]] = {
    RiskLevel.HIGH:   ("rgba(196,106,85,0.18)",  _C.red_warm),
    RiskLevel.MEDIUM: ("rgba(212,164,96,0.18)",  _C.amber),
    RiskLevel.LOW:    ("rgba(109,181,128,0.18)", _C.phosphor),
}


# ---------------------------------------------------------------------------
# QSS — v3 lab-console tokens throughout.  No rounded corners on the
# header counter / badge (rounded reads as v2 chrome); the badge becomes
# an outlined square chip so it sits as a tally mark, not a notification
# bubble.
# ---------------------------------------------------------------------------

_QSS = f"""
QWidget#stackedDecisionsPanel {{
    background: transparent;
}}
QLabel#stackedDecisionsHeader {{
    color: {_C.paper_dim};
    font-family: {_F.mono_stack};
    font-size: 11px;
    text-transform: uppercase;
    letter-spacing: 0.18em;
}}
QLabel#stackedDecisionsBadge {{
    background-color: transparent;
    color: {_C.amber};
    border: 1px solid {_C.amber_dim};
    border-radius: 0;
    padding: 1px 6px;
    font-family: {_F.mono_stack};
    font-size: 10px;
    font-weight: 600;
    letter-spacing: 0.04em;
}}
QLabel#stackedDecisionsCounter {{
    color: {_C.paper_faint};
    font-family: {_F.mono_stack};
    font-size: 10px;
    letter-spacing: 0.08em;
}}
QFrame#peekSliver {{
    background-color: {_C.surface};
    border: 1px solid {_C.rule};
    border-bottom: none;
    border-top-left-radius: 4px;
    border-top-right-radius: 4px;
}}
QLabel#peekSessionTag {{
    color: {_C.paper};
    font-family: {_F.mono_stack};
    font-size: 10px;
}}
QLabel#peekToolName {{
    color: {_C.paper_dim};
    font-family: {_F.mono_stack};
    font-size: 10px;
    letter-spacing: 0.02em;
}}
QLabel#peekRiskPill {{
    font-family: {_F.mono_stack};
    font-size: 8px;
    font-weight: 600;
    padding: 0 5px;
    border-radius: 0;
    letter-spacing: 0.08em;
}}
QLabel#stackOverflowLabel {{
    color: {_C.paper_faint};
    font-family: {_F.mono_stack};
    font-size: 10px;
    letter-spacing: 0.04em;
}}
"""


# ---------------------------------------------------------------------------
# Peek sliver — one for each queued decision behind the active card.
# ---------------------------------------------------------------------------


class _PeekSliver(QFrame):
    """Thin teaser row for one queued (non-active) pending decision.

    Deliberately non-interactive — the user can't reach the queue
    without finishing the active card. The sliver communicates "this
    is who's next" but not "click me".
    """

    def __init__(
        self,
        view: PendingDecisionView,
        *,
        depth: int,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        assert 1 <= depth <= _MAX_PEEKS, f"depth out of range: {depth}"
        self._view = view
        self._depth = depth
        self._build_ui()

    def _build_ui(self) -> None:
        self.setObjectName("peekSliver")
        self.setStyleSheet(_QSS)
        self.setFixedHeight(_PEEK_HEIGHTS_PX[self._depth - 1])
        self.setContentsMargins(0, 0, 0, 0)

        accent = session_accent(self._view.session_uuid)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        bar = QFrame()
        bar.setFixedHeight(_PEEK_BAR_HEIGHT_PX)
        bar.setStyleSheet(f"background-color: {accent};")
        outer.addWidget(bar)

        body = QHBoxLayout()
        body.setContentsMargins(
            _PEEK_BODY_PADDING_H_PX, _PEEK_BODY_PADDING_V_PX,
            _PEEK_BODY_PADDING_H_PX, _PEEK_BODY_PADDING_V_PX,
        )
        body.setSpacing(6)

        # Inline accent dot — same colour as the bar, repeats the
        # signal so a glance at the row identifies the session.
        dot = QLabel("●")
        dot.setStyleSheet(f"color: {accent}; font-size: 11px;")
        body.addWidget(dot)

        session_lbl = QLabel(self._view.session_name)
        session_lbl.setObjectName("peekSessionTag")
        body.addWidget(session_lbl)

        body.addWidget(self._build_separator())

        tool_lbl = QLabel(self._format_tool_text())
        tool_lbl.setObjectName("peekToolName")
        body.addWidget(tool_lbl, 1)

        body.addWidget(self._build_risk_pill())

        outer.addLayout(body)
        self._apply_opacity()

    def _build_separator(self) -> QLabel:
        sep = QLabel("·")
        sep.setStyleSheet("color: #555;")
        return sep

    def _format_tool_text(self) -> str:
        if self._view.kind is DecisionKind.ASK_QUESTION and self._view.question_header:
            return self._view.question_header
        return self._view.tool_name or "(decision)"

    def _build_risk_pill(self) -> QLabel:
        bg, fg = _RISK_PILL_COLORS[self._view.risk_level]
        pill = QLabel(self._view.risk_level.value.upper())
        pill.setObjectName("peekRiskPill")
        pill.setStyleSheet(f"background-color: {bg}; color: {fg};")
        return pill

    def _apply_opacity(self) -> None:
        opacity = _PEEK_OPACITIES[self._depth - 1]
        if opacity >= 1.0:
            return
        effect = QGraphicsOpacityEffect(self)
        effect.setOpacity(opacity)
        self.setGraphicsEffect(effect)


# ---------------------------------------------------------------------------
# Panel
# ---------------------------------------------------------------------------


def _build_card(
    view: PendingDecisionView,
    *,
    on_resolve: ResolveCallback,
    on_focus_terminal: FocusTerminalCallback | None,
    on_terminal_answer_default: "Callable[[str], None] | None" = None,
) -> QWidget:
    """Pick the right card widget for a decision kind."""
    if view.kind is DecisionKind.ASK_QUESTION:
        return QuestionCard(
            view,
            on_resolve=on_resolve,
            on_focus_terminal=on_focus_terminal,
        )
    return ApprovalCard(
        view,
        on_resolve=on_resolve,
        # Same plumbing as QuestionCard — Allow/Deny focuses the
        # terminal so the user can verify Claude proceeded (or type
        # 1 / 2 manually if Claude is still waiting on its own prompt).
        on_focus_terminal=on_focus_terminal,
        # On Allow, also inject "1\n" into the target session's pane
        # via iTerm AppleScript so Claude's terminal prompt for
        # sensitive ops (which the hook ``allow`` alone doesn't
        # dismiss) gets cleared automatically. See
        # ApprovalCard._on_allow + platform_.terminals.iterm2.
        # ``write_text_to_iterm_session``.
        on_terminal_answer_default=on_terminal_answer_default,
    )


class StackedDecisionsPanel(QWidget):
    """Container that renders the pending-decisions stack.

    Subscribe ``render(views: tuple[PendingDecisionView, ...])`` to the
    pending-decisions slice of WorldSnapshot. The panel hides itself
    when the queue is empty so the rest of the expanded window
    reclaims the space.
    """

    def __init__(
        self,
        *,
        on_resolve: ResolveCallback,
        on_focus_terminal: FocusTerminalCallback | None = None,
        on_terminal_answer_default: "Callable[[str], None] | None" = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._on_resolve = on_resolve
        self._on_focus_terminal = on_focus_terminal
        self._on_terminal_answer_default = on_terminal_answer_default
        self.setObjectName("stackedDecisionsPanel")
        self.setStyleSheet(_QSS)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.setSpacing(0)
        self._header_widget: QWidget | None = None
        self._active_card: QWidget | None = None
        self._peek_widgets: list[_PeekSliver] = []
        self._queued_count: int = 0
        self._overflow_label: QLabel | None = None
        self.hide()

    # ── public ──────────────────────────────────────────────────────────

    def render(self, views: Iterable[PendingDecisionView]) -> None:
        view_tuple = tuple(views)
        self._clear()
        if not view_tuple:
            self.hide()
            return
        self.show()

        self._layout.addWidget(self._build_header(len(view_tuple)))
        # v4c: peek slivers hidden — the prototype's "Queued · N more"
        # list replaces them as a sibling INSIDE the active card.
        active = self._build_active(view_tuple[0])
        # Wire the queue-position pill on the active card so it reads
        # "1 of N" — only the head card carries the pill.
        if hasattr(active, "set_queue_position"):
            active.set_queue_position(1, len(view_tuple))
        elif hasattr(self._active_card, "set_queue_position"):
            self._active_card.set_queue_position(1, len(view_tuple))
        self._layout.addWidget(active)
        # v4c: queued-list under the active card (replaces v3 peeks
        # above it). Shows session · tool · elapsed for each queued
        # decision so the user reads queue depth at a glance.
        if len(view_tuple) > 1:
            queued_rest = view_tuple[1:]
            # v4c keeps the v3 _MAX_PEEKS soft cap so the panel can't
            # grow without bound when many decisions pile up. Anything
            # past the cap rolls into the "+N more queued behind"
            # overflow label below the queued list, matching v3 UX.
            visible = queued_rest[:_MAX_PEEKS]
            overflow = len(queued_rest) - len(visible)
            self._queued_count = len(visible)
            self._layout.addWidget(self._build_queued_list(visible))
            if overflow > 0:
                self._layout.addWidget(self._build_overflow_label(overflow))

    @property
    def active_card(self) -> QWidget | None:
        """Visible-for-tests handle to the currently rendered head card."""
        return self._active_card

    @property
    def peek_count(self) -> int:
        """In v3 this returned the count of peek slivers above the
        active card.  In v4c there are no slivers — the prototype puts
        a "Queued · N more" mini-list **inside** the active card
        instead.  The public attribute name is kept (so external
        tests and callers don't have to be renamed in lockstep), but
        the value now reads as "how many queued decisions are
        surfaced visually below the active card".
        """
        return self._queued_count

    # ── internal ───────────────────────────────────────────────────────

    def _clear(self) -> None:
        while self._layout.count():
            item = self._layout.takeAt(0)
            w = item.widget() if item is not None else None
            if w is not None:
                w.setParent(None)
                w.deleteLater()
        self._header_widget = None
        self._active_card = None
        self._peek_widgets = []
        self._queued_count = 0
        self._overflow_label = None

    def _build_header(self, total: int) -> QWidget:
        # v4c: hide the all-caps "PENDING DECISIONS [N]" header that v3
        # rendered above the active card.  Prototype-v4c-github.html
        # puts the decision card directly under the sessions list with
        # no parent caption — the orange left border + "X of Y" pill
        # inside the card already signal "this is a decision queue".
        # The widget is kept (so tests that reach for _header_widget
        # still compile) but its content is empty and the layout
        # collapses to zero height.
        host = QWidget()
        host.setVisible(False)
        host.setFixedHeight(0)
        self._header_widget = host
        return host

    def _build_peeks(
        self, views: tuple[PendingDecisionView, ...],
    ) -> list[_PeekSliver]:
        # Peeks are the next-up decisions, capped at _MAX_PEEKS. They
        # render top-down: depth-3 (deepest) first, depth-1 (just
        # above active) last — so visually the deepest sliver is at
        # the top of the pile.
        queued = views[1 : 1 + _MAX_PEEKS]
        slivers: list[_PeekSliver] = []
        for offset_from_active, view in enumerate(queued, start=1):
            depth = offset_from_active  # 1, 2, 3
            slivers.append(_PeekSliver(view, depth=depth))
        # Wrap each sliver in a host with proper horizontal inset and
        # then return them in the order we want them stacked.
        wrapped = [
            self._inset_sliver(s, depth=i + 1)
            for i, s in enumerate(slivers)
        ]
        # Reverse so the deepest (depth=3) lands first in the layout
        # ↑ top of the stack.
        wrapped.reverse()
        self._peek_widgets = slivers
        return wrapped

    def _inset_sliver(self, sliver: _PeekSliver, *, depth: int) -> QWidget:
        """Wrap a sliver in a host that applies the per-depth horizontal
        inset. Doing this with a wrapper widget rather than the
        sliver's own contentsMargins keeps the sliver geometry simple
        and lets the wrappers be swapped out independently."""
        inset = _PEEK_INSETS_PX[depth - 1]
        host = QWidget()
        host.setContentsMargins(inset, 0, inset, 0)
        wlayout = QVBoxLayout(host)
        wlayout.setContentsMargins(inset, 0, inset, 0)
        wlayout.setSpacing(0)
        wlayout.addWidget(sliver)
        return host

    def _build_queued_list(
        self, rest: tuple[PendingDecisionView, ...],
    ) -> QWidget:
        """v4c: render the "Queued · N more" list that sits below the
        active card (mirrors prototype-v4c-github.html).  Each row
        names the session, the tool/preview, and the elapsed time —
        gives the user queue depth without forcing them to click
        through.
        """
        from datetime import datetime, timezone
        from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QVBoxLayout
        from claude_island.ui.lab_palette import Color as _C, FontStack as _F

        host = QFrame()
        host.setObjectName("decisionsQueuedList")
        host.setStyleSheet(
            f"QFrame#decisionsQueuedList {{"
            f"  background: rgba(219, 109, 40, 0.06);"
            f"  border: 1px solid rgba(219, 109, 40, 0.20);"
            f"  border-top: none;"
            f"  border-radius: 0 0 4px 4px;"
            f"}}"
        )
        layout = QVBoxLayout(host)
        layout.setContentsMargins(14, 8, 14, 10)
        layout.setSpacing(4)

        # Header "Queued · N more"
        header = QLabel(f"Queued · {len(rest)} more")
        header.setStyleSheet(
            f"color: {_C.paper_dim}; font-size: 10.5px; "
            f"font-weight: 600; letter-spacing: 0.04em;"
        )
        layout.addWidget(header)

        # One row per queued decision.
        now = datetime.now(timezone.utc)
        for v in rest:
            row_h = QHBoxLayout()
            row_h.setContentsMargins(0, 0, 0, 0)
            row_h.setSpacing(8)
            sess = v.session_name or "session"
            tool = v.tool_name or ""
            preview = (v.tool_input_preview or "").splitlines()[0][:50] if v.tool_input_preview else ""
            text = f"<b>{sess}</b>"
            if tool:
                text += f" <span style='color:{_C.paper_dim}'>{tool}</span>"
            if preview:
                text += f" <span style='color:{_C.paper_faint}'>{preview}</span>"
            label = QLabel(text)
            label.setStyleSheet(
                f"color: {_C.paper}; font-size: 11.5px;"
            )
            label.setTextFormat(Qt.TextFormat.RichText)
            row_h.addWidget(label, 1)

            # Elapsed time on the right (red_warm).
            delta = v.expires_at - now
            # invert (we want elapsed from received, not remaining) —
            # but PendingDecisionView only carries expires_at; assume
            # a 600s window and compute elapsed = 600 - remaining.
            remaining = max(0, int(delta.total_seconds()))
            window = 600
            elapsed = max(0, window - remaining)
            if elapsed < 60:
                elapsed_str = f"{elapsed}s"
            else:
                m = elapsed // 60
                s = elapsed % 60
                elapsed_str = f"{m}m {s}s" if s else f"{m}m"
            time_label = QLabel(elapsed_str)
            time_label.setStyleSheet(
                f"color: {_C.red_warm}; font-size: 11.5px; "
                f"font-weight: 600;"
            )
            row_h.addWidget(time_label)

            layout.addLayout(row_h)

        return host

    def _build_active(self, view: PendingDecisionView) -> QWidget:
        card = _build_card(
            view,
            on_resolve=self._on_resolve,
            on_focus_terminal=self._on_focus_terminal,
            on_terminal_answer_default=self._on_terminal_answer_default,
        )
        # Slight upward bleed so the card overlaps the lowest sliver
        # ⇒ the stack reads as a pile, not a list. Achieved by
        # putting the card in a host with a negative top margin.
        host = QWidget()
        host_layout = QVBoxLayout(host)
        host_layout.setContentsMargins(0, -_ACTIVE_CARD_NEGATIVE_TOP_PX, 0, 0)
        host_layout.setSpacing(0)
        host_layout.addWidget(card)
        self._active_card = card
        return host

    def _compute_overflow(
        self, views: tuple[PendingDecisionView, ...],
    ) -> int:
        # Total queued behind the head minus those we already render
        # as peek slivers.
        queued = max(0, len(views) - 1)
        return max(0, queued - _MAX_PEEKS)

    def _build_overflow_label(self, overflow: int) -> QLabel:
        label = QLabel(_OVERFLOW_LABEL_TEMPLATE.format(n=overflow))
        label.setObjectName("stackOverflowLabel")
        label.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        label.setContentsMargins(0, 8, 0, 0)
        self._overflow_label = label
        return label
