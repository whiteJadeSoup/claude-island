from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPainterPath
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from claude_island.core.models import Session, SessionDetails, SessionUsage, UsageTotals
from .controller import IslandController

_PANEL_W = 320
_GAP = 6  # px gap between capsule bottom and panel top

_STYLE_PANEL = """
    color: white;
    font-family: 'Segoe UI', sans-serif;
"""
_STYLE_TITLE = "color: #888; font-size: 10px; letter-spacing: 1px;"
_STYLE_SEP = "background: #2a2a2a;"

# --------------------------------------------------------------------------
# Session row + card visual language
# --------------------------------------------------------------------------
#
# Three elements have to read well at a glance:
# 1. activity dot (●) on the left — green / yellow / gray = recent / today /
#    older, so the user doesn't have to read the time text to tell which
#    sessions are "live"
# 2. group ↔ standalone — group cards sit on a darker background (#181818)
#    than standalone rows (#1e1e1e); the contrast tells the eye "these rows
#    are bracketed together" without explicit chrome
# 3. one row per line — name on the left, age on the right, no second line;
#    rows are 36px tall so the whole list scans top-to-bottom in one motion

_BG_SINGLE = "#1e1e1e"
_BG_GROUP = "#181818"          # darker → "this is a container, not a row"
_BG_HOVER_SINGLE = "#2a2a2a"
_BG_HOVER_IN_GROUP = "#222222"  # subtle hover that keeps the card identity
_BG_PRESSED = "#333333"

_DOT_GREEN = "#4ade80"   # < 1h since last activity
_DOT_YELLOW = "#facc15"  # < 24h
_DOT_GRAY = "#52525b"    # ≥ 24h

_ROW_HEIGHT = 36
_ROW_PAD_H = 12

_STYLE_SINGLE_ROW = f"""
    QPushButton {{
        background: {_BG_SINGLE};
        border: none;
        border-radius: 8px;
        text-align: left;
    }}
    QPushButton:hover {{ background: {_BG_HOVER_SINGLE}; }}
    QPushButton:pressed {{ background: {_BG_PRESSED}; }}
"""
_STYLE_GROUP_CARD = f"""
    QFrame#group_card {{
        background: {_BG_GROUP};
        border-radius: 8px;
    }}
"""
_STYLE_GROUP_ROW = f"""
    QPushButton {{
        background: transparent;
        border: none;
        text-align: left;
    }}
    QPushButton:hover {{ background: {_BG_HOVER_IN_GROUP}; }}
    QPushButton:pressed {{ background: {_BG_PRESSED}; }}
"""
# Separator between rows of the same group. With the group sitting on
# #181818, a #2a2a2a hairline is just barely visible — enough to read
# as "two distinct rows" without competing with the dot/name typography.
_STYLE_GROUP_ROW_SEP = "background: #2a2a2a; margin-left: 12px; margin-right: 12px;"
# Px gap between top-level entries (cards / standalone rows). Bigger than
# the in-group row spacing so "next card" reads as a different chunk.
_GROUP_GAP = 8

_STYLE_DOT = "color: {color}; font-size: 11px;"
_STYLE_NAME = "color: #e8e8e8; font-size: 13px;"
_STYLE_AGE = "color: #6b7280; font-size: 11px;"
_STYLE_PERIOD_BTN = """
    QPushButton {
        color: #666;
        background: transparent;
        border: 1px solid #333;
        border-radius: 8px;
        padding: 2px 10px;
        font-size: 10px;
    }
    QPushButton:hover { color: #aaa; border-color: #555; }
    QPushButton:checked { color: white; border-color: #888; background: #2a2a2a; }
"""

# --------------------------------------------------------------------------
# USAGE region typography + cards
# --------------------------------------------------------------------------
#
# Two stacked cards: the (highlighted) 5h-session card on top, the period
# card below. Same bg-contrast language as the session list (group bg
# darker than standalone) so both regions speak the same visual dialect.

_STYLE_USAGE_SESSION_CARD = f"""
    QFrame#usage_session_card {{
        background: {_BG_GROUP};
        border-radius: 8px;
    }}
"""
_STYLE_USAGE_PERIOD_CARD = f"""
    QFrame#usage_period_card {{
        background: {_BG_SINGLE};
        border-radius: 8px;
    }}
"""
_STYLE_USAGE_AMOUNT = "color: #f5f5f5; font-size: 18px; font-weight: 500;"
_STYLE_USAGE_HEADER = "color: #c9c9c9; font-size: 11px; letter-spacing: 0.5px;"
_STYLE_USAGE_RESET = "color: #6b7280; font-size: 11px;"
_STYLE_USAGE_PCT = "color: #9ca3af; font-size: 11px;"
_STYLE_USAGE_PCT_STALE = "color: #facc15; font-size: 11px;"  # ⚠ tone
_STYLE_USAGE_MODEL = "color: #6b7280; font-size: 11px;"
_STYLE_USAGE_PERIOD_NAME = "color: #c9c9c9; font-size: 12px;"
_STYLE_USAGE_PERIOD_TOTAL = "color: #f5f5f5; font-size: 13px; font-weight: 500;"
_STYLE_USAGE_TOKEN_ROW = "color: #9ca3af; font-size: 11px;"

# Quota progress-bar colour thresholds. The bar's chunk colour escalates
# from green to yellow to red as the user's 5h quota fills, so the user
# can read "how worried should I be" at a glance without parsing the
# percentage text. Stale data (cache > 3×TTL) overrides any colour to
# gray — we don't want to alarm (or reassure) on a number we can't trust.
#
#   < 60%        green   — plenty of runway, large operations are fine
#   60–85%       yellow  — over half spent, start sizing requests
#   ≥ 85%        red     — only 15% headroom, defer big tasks past reset
#   stale (any%) gray    — endpoint quiet for >15 min, value is old
_BAR_GREEN  = "#4ade80"  # matches _DOT_GREEN
_BAR_YELLOW = "#facc15"  # matches _DOT_YELLOW
_BAR_RED    = "#ef4444"  # Tailwind red-500
_BAR_STALE  = "#6b7280"

_STYLE_REFRESH_BTN = """
    QPushButton {
        color: #888;
        background: transparent;
        border: 1px solid #333;
        border-radius: 10px;
        font-size: 12px;
        padding: 0;
    }
    QPushButton:hover   { color: #ddd; border-color: #555; }
    QPushButton:pressed { color: white; background: #2a2a2a; }
"""

_PROGRESS_BAR_TPL = """
    QProgressBar {{
        border: none;
        background: #2a2a2a;
        border-radius: 3px;
        height: 6px;
        text-align: center;
    }}
    QProgressBar::chunk {{
        background: {color};
        border-radius: 3px;
    }}
"""


def _fmt_tokens(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.0f}K"
    return str(n)


def _fmt_ago(dt: datetime) -> str:
    """Compact age label used on the right side of each row.
    No "ago" suffix — a single column of "5m / 19h / 6d" reads as a
    list of values, so the unit-only form is faster to compare."""
    delta = datetime.now(timezone.utc) - dt.astimezone(timezone.utc)
    s = int(delta.total_seconds())
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m"
    if s < 86400:
        return f"{s // 3600}h"
    return f"{s // 86400}d"


def _activity_color(dt: datetime) -> str:
    """Traffic-light dot color encoding how recent ``dt`` is.
    Thresholds are chosen so a daily user sees mostly green (active),
    yellow at the end of the day, and gray for stale sessions."""
    delta = datetime.now(timezone.utc) - dt.astimezone(timezone.utc)
    h = delta.total_seconds() / 3600.0
    if h < 1:
        return _DOT_GREEN
    if h < 24:
        return _DOT_YELLOW
    return _DOT_GRAY


def _fmt_reset(end_time: datetime | None) -> str:
    """Render the time-until-reset for the 5h session header.

    end_time None     → "—" (no DB activity yet)
    end_time in past  → "expired"
    < 60s remaining   → "in <Ns>" (special-case so the user sees the rollover)
    < 1h remaining    → "in Xm"
    otherwise         → "Xh Ym"
    """
    if end_time is None:
        return "—"
    now = datetime.now(timezone.utc)
    remaining = (end_time.astimezone(timezone.utc) - now).total_seconds()
    if remaining <= 0:
        return "expired"
    if remaining < 60:
        return f"in {int(remaining)}s"
    if remaining < 3600:
        return f"in {int(remaining // 60)}m"
    h = int(remaining // 3600)
    m = int((remaining % 3600) // 60)
    return f"{h}h {m}m"


def _fmt_model_label(model: str) -> str:
    """Friendly model label for the per-model breakdown line.

    Picks up the canonical family name (sonnet/haiku/opus) when the
    raw id contains it, otherwise truncates an unknown id so it
    doesn't overflow the row.
    """
    lower = (model or "").lower()
    for known in ("haiku", "sonnet", "opus"):
        if known in lower:
            return known.capitalize()
    if not model:
        return "?"
    return model[:12] + ("…" if len(model) > 12 else "")


def _fmt_started(dt: datetime | None) -> str:
    """Wall-clock duration string for the tooltip header."""
    if dt is None:
        return "—"
    delta = datetime.now(timezone.utc) - dt.astimezone(timezone.utc)
    s = int(delta.total_seconds())
    if s < 60:
        return f"started {s}s ago"
    if s < 3600:
        return f"started {s // 60}m ago"
    if s < 86400:
        return f"started {s // 3600}h {(s % 3600) // 60}m ago"
    return f"started {s // 86400}d ago"


def _escape_html(text: str) -> str:
    """Minimal HTML escape for tooltip-safe rendering of user content."""
    return (text.replace("&", "&amp;")
                .replace("<", "&lt;")
                .replace(">", "&gt;"))


def _fmt_local_dt(dt: datetime | None) -> str:
    """Local-timezone datetime for the popup's Created field.
    e.g. ``"2026-05-01 13:00"``. Returns "—" on None."""
    if dt is None:
        return "—"
    return dt.astimezone().strftime("%Y-%m-%d %H:%M")


def _quota_color(pct: float, stale: bool) -> str:
    """Pick the progress-bar / pct-text colour for a 5h quota reading.

    Stale data wins regardless of percentage — we surface "I don't
    trust this" before "how full is it". Thresholds documented at the
    _BAR_* constants above.
    """
    if stale:
        return _BAR_STALE
    if pct >= 85:
        return _BAR_RED
    if pct >= 60:
        return _BAR_YELLOW
    return _BAR_GREEN


def _fmt_money(amount: float) -> str:
    """Compact money formatting that switches precision by magnitude.

    < $0.01   → "$0.001" (preserve some signal)
    < $10     → "$1.23"
    < $1000   → "$123"
    otherwise → "$1.2K"
    """
    if amount < 0.01:
        return f"${amount:.3f}"
    if amount < 10:
        return f"${amount:.2f}"
    if amount < 1000:
        return f"${amount:.0f}"
    return f"${amount / 1000:.1f}K"


# --------------------------------------------------------------------------
# Same-tab grouping (PR2)
# --------------------------------------------------------------------------
#
# Two sessions are visually merged into one rounded card when their
# (window_handle, project_path) pair matches. window_handle is the WT
# main HWND populated by ProcessScanner; project_path is the cwd of the
# claude.exe at scan time. The pair stands in for "same WT tab" because
# WT's UIA tree doesn't expose pid→tab for inactive panes — and panes in
# one tab almost always share both wt_hwnd and cwd.
#
# Sessions whose window_handle is None (couldn't resolve to a WT host —
# pythonw, sandboxed shells, non-Windows) are always rendered standalone
# so they don't accidentally collapse together.

def _group_key(s: Session) -> tuple[int, str] | None:
    if s.window_handle is None:
        return None
    return (s.window_handle, _normalize_project_path(s.project_path))


def _normalize_project_path(path) -> str:
    """Collapse Claude Code worktree paths back to their parent project.

    Claude Code creates per-feature git worktrees under
    ``<repo>/.claude/worktrees/<branch-name>``. Users routinely run
    one claude session in the main repo and another in a worktree,
    side-by-side as split panes in the same WT tab. With raw cwds the
    grouping heuristic sees two different paths and fails to merge
    them. Normalising the worktree back to the repo root restores the
    "same tab" grouping (and, downstream, lets the activator find a
    sibling whose console title IS in the WT TabItem set, fixing
    click-to-switch on the inactive worktree pane).

    Non-worktree paths pass through unchanged.
    """
    parts = path.parts
    for i in range(len(parts) - 1):
        if parts[i] == ".claude" and parts[i + 1] == "worktrees":
            return str(Path(*parts[:i]))
    return str(path)


def _session_sort_key(s: Session) -> tuple:
    """Sort sessions so that members of the same group are adjacent.
    Ungroupable sessions (window_handle=None) sort to the end and stay
    in pid order so their position is stable across refreshes."""
    key = _group_key(s)
    if key is None:
        return (1, 0, "", s.pid)
    wt, path = key
    return (0, wt, path, s.pid)


def _consecutive_groups(sessions: list[Session]) -> list[list[Session]]:
    """Collapse consecutive same-key sessions into a sublist; ungroupable
    (None-key) sessions form singleton sublists each."""
    out: list[list[Session]] = []
    cur: list[Session] = []
    cur_key: object = object()  # sentinel that no real key matches
    for s in sessions:
        key = _group_key(s)
        if key is None:
            if cur:
                out.append(cur)
                cur = []
            out.append([s])
            cur_key = object()
        elif key == cur_key:
            cur.append(s)
        else:
            if cur:
                out.append(cur)
            cur = [s]
            cur_key = key
    if cur:
        out.append(cur)
    return out


class SessionDetailPopup(QFrame):
    """Frameless rounded popup that shows everything we know about
    one session — opened by right-clicking a session row.

    Visual language is intentionally borrowed from the main panel:
    same outer ``_BG_GROUP`` rounded card, same inner ``_BG_SINGLE``
    sub-cards, same ``_STYLE_NAME`` / ``_STYLE_AGE`` typography,
    same dot tokens. Reads like a zoomed-in version of a row, not a
    Qt-generic dialog.

    Uses ``Qt.WindowType.Popup`` so Qt closes us automatically when
    the user clicks anywhere outside, matching how a context menu
    behaves on every desktop platform.
    """

    def __init__(
        self,
        details: SessionDetails | None,
        fallback: Session,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        # Frameless + Popup = "I behave like a context menu":
        # no titlebar, on top, dismissed by clicks outside.
        self.setWindowFlags(
            Qt.WindowType.Popup
            | Qt.WindowType.FramelessWindowHint
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setFixedWidth(_PANEL_W)

        self._details = details
        self._fallback = fallback

        root = QVBoxLayout(self)
        root.setContentsMargins(14, 14, 14, 14)
        root.setSpacing(_GROUP_GAP)

        root.addWidget(self._build_header_card())
        root.addWidget(self._build_meta_card())
        root.addWidget(self._build_tokens_card())
        prompt_card = self._build_prompt_card()
        if prompt_card is not None:
            root.addWidget(prompt_card)

        self.setStyleSheet(_STYLE_PANEL)
        self.adjustSize()

    # paintEvent borrows from ExpandedWindow's: rounded translucent
    # outer fill so the corners look like the main panel's instead
    # of OS-default square ones.
    def paintEvent(self, event: object) -> None:  # type: ignore[override]
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        path = QPainterPath()
        path.addRoundedRect(0, 0, self.width(), self.height(), 16, 16)
        painter.fillPath(path, QColor(18, 18, 18, 240))

    # ------------------------------------------------------------------
    # Section builders — each returns a rounded ``_BG_SINGLE`` sub-card
    # ------------------------------------------------------------------

    def _build_header_card(self) -> QFrame:
        d = self._details
        title = self._title_text()
        subtitle = (d.ai_title if d and d.ai_title and d.ai_title != title else None)

        card = self._sub_card()
        layout = QVBoxLayout(card)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(2)

        # First line: ● dot + title + (status pill on the right).
        head = QHBoxLayout()
        head.setSpacing(8)
        dot = QLabel("●")
        dot.setStyleSheet(_STYLE_DOT.format(color=_activity_color(self._fallback.last_activity)))
        head.addWidget(dot)
        name = QLabel(title)
        name.setStyleSheet(_STYLE_NAME)
        head.addWidget(name, 1)
        if d and d.status:
            pill = QLabel(d.status)
            pill.setStyleSheet(
                "color: #c9c9c9; font-size: 10px; "
                "background: #2a2a2a; border-radius: 6px; "
                "padding: 2px 8px;"
            )
            pill.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            head.addWidget(pill)
        layout.addLayout(head)

        if subtitle:
            sub = QLabel(subtitle)
            sub.setStyleSheet("color: #9ca3af; font-size: 11px; font-style: italic;")
            sub.setWordWrap(True)
            layout.addWidget(sub)
        return card

    def _build_meta_card(self) -> QFrame:
        """ID / Path / Branch / Created / Version block."""
        d = self._details
        sess_uuid = self._effective_uuid()

        card = self._sub_card()
        layout = QVBoxLayout(card)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(4)

        layout.addWidget(self._kv_row("ID", _shorten_uuid(sess_uuid) or "—"))
        layout.addWidget(self._kv_row("Path", str(self._fallback.project_path)))
        if d and d.git_branch:
            layout.addWidget(self._kv_row("Branch", d.git_branch))
        if d and d.started_at is not None:
            layout.addWidget(self._kv_row(
                "Created",
                f"{_fmt_local_dt(d.started_at)}  ({_fmt_started(d.started_at)})",
            ))
        if d and d.cc_version:
            layout.addWidget(self._kv_row("Version", f"Claude Code {d.cc_version}"))
        return card

    def _build_tokens_card(self) -> QFrame:
        """TOKENS section — total + per-model breakdown."""
        d = self._details
        card = self._sub_card()
        layout = QVBoxLayout(card)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(6)

        # Section title: "TOKENS  ·  N turns" right-aligned tail.
        head = QHBoxLayout()
        title_lbl = QLabel("TOKENS")
        title_lbl.setStyleSheet(_STYLE_TITLE)
        head.addWidget(title_lbl)
        head.addStretch()
        if d:
            extras: list[str] = []
            if d.turn_count:
                extras.append(f"{d.turn_count} turn{'s' if d.turn_count != 1 else ''}")
            if d.sidechain_count:
                extras.append(f"{d.sidechain_count} subagent")
            if extras:
                tail = QLabel(" · ".join(extras))
                tail.setStyleSheet(_STYLE_AGE)
                tail.setAlignment(Qt.AlignmentFlag.AlignRight)
                head.addWidget(tail)
        layout.addLayout(head)

        sep = QFrame()
        sep.setFixedHeight(1)
        sep.setStyleSheet(_STYLE_SEP)
        layout.addWidget(sep)

        if d and d.per_model:
            for m in d.per_model:
                layout.addLayout(self._model_block(m))
        elif d:
            # Have details but zero per-model rows (composer unwired
            # or session never recorded usage). Still show the total
            # so the section isn't empty.
            total = QLabel(_fmt_money(d.cost_usd))
            total.setStyleSheet(_STYLE_USAGE_AMOUNT)
            layout.addWidget(total)
        else:
            empty = QLabel("—")
            empty.setStyleSheet(_STYLE_AGE)
            layout.addWidget(empty)
        return card

    def _build_prompt_card(self) -> QFrame | None:
        d = self._details
        if not d or not d.last_prompt:
            return None
        snippet = d.last_prompt
        # Allow more text than the old tooltip — popup has room.
        if len(snippet) > 600:
            snippet = snippet[:597] + "…"

        card = self._sub_card()
        layout = QVBoxLayout(card)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(4)
        head = QLabel("LAST PROMPT")
        head.setStyleSheet(_STYLE_TITLE)
        layout.addWidget(head)
        body = QLabel(snippet)
        body.setStyleSheet("color: #c9c9c9; font-size: 12px;")
        body.setWordWrap(True)
        layout.addWidget(body)
        return card

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _sub_card(self) -> QFrame:
        """One inner ``_BG_SINGLE`` rounded sub-card. Same recipe
        used elsewhere in the panel — keeps the language consistent."""
        f = QFrame()
        f.setStyleSheet(
            "QFrame { background: " + _BG_SINGLE + "; border-radius: 8px; }"
        )
        return f

    def _kv_row(self, key: str, value: str) -> QWidget:
        """Two-column ``Key   value`` row. Key is fixed-width gray,
        value is white and wraps if long."""
        row = QWidget()
        h = QHBoxLayout(row)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(8)
        k = QLabel(key)
        k.setStyleSheet("color: #6b7280; font-size: 11px;")
        k.setFixedWidth(54)
        k.setAlignment(Qt.AlignmentFlag.AlignTop)
        h.addWidget(k)
        v = QLabel(value)
        v.setStyleSheet("color: #e8e8e8; font-size: 12px;")
        v.setWordWrap(True)
        h.addWidget(v, 1)
        return row

    def _model_block(self, m) -> QVBoxLayout:
        """One model's three-line block: name + cost / in·out / cw·cr."""
        block = QVBoxLayout()
        block.setSpacing(2)
        head = QHBoxLayout()
        name = QLabel(_fmt_model_label(m.model))
        name.setStyleSheet("color: #e8e8e8; font-size: 12px;")
        head.addWidget(name)
        head.addStretch()
        cost = QLabel(_fmt_money(m.cost_usd))
        cost.setStyleSheet("color: #e8e8e8; font-size: 12px;")
        cost.setAlignment(Qt.AlignmentFlag.AlignRight)
        head.addWidget(cost)
        block.addLayout(head)

        io = QLabel(
            f"  in {_fmt_tokens(m.input_tokens)}  ·  "
            f"out {_fmt_tokens(m.output_tokens)}"
        )
        io.setStyleSheet(_STYLE_AGE)
        block.addWidget(io)

        cache = QLabel(
            f"  cw {_fmt_tokens(m.cache_creation_tokens)}  ·  "
            f"cr {_fmt_tokens(m.cache_read_tokens)}"
        )
        cache.setStyleSheet(_STYLE_AGE)
        block.addWidget(cache)
        return block

    def _title_text(self) -> str:
        d = self._details
        return (
            (d.name if d and d.name else None)
            or (d.ai_title if d and d.ai_title else None)
            or self._fallback.project_path.name
            or str(self._fallback.project_path)
        )

    def _effective_uuid(self) -> str:
        """The session_uuid the composer actually used. Falls back to
        the Session's own session_uuid field (often empty when coming
        straight from ProcessScanner)."""
        if self._details and self._details.effective_uuid:
            return self._details.effective_uuid
        return self._fallback.session_uuid or ""


def _shorten_uuid(uuid: str) -> str:
    """First 8 chars + ellipsis. UUIDs are unique enough at 8 chars
    for the user's eye to spot them; full uuid in a tooltip would
    just look like noise."""
    if not uuid:
        return ""
    return uuid[:8] + ("…" if len(uuid) > 8 else "")


class ExpandedWindow(QWidget):
    """Floating panel that appears below the capsule when expanded.

    Shows the session list (clicking activates the terminal) and a usage
    summary with a period selector (Daily / Weekly / Monthly).

    ``session_activated`` is connected in __main__.py to WindowActivator.activate
    so the UI layer never imports platform code directly.
    """

    # Args: clicked Session, siblings (list[Session] of other group members,
    # may be empty). Siblings let the activator try sibling console titles
    # as fallback when the clicked row is an inactive split pane.
    session_activated: Signal = Signal(Session, list)

    def __init__(
        self,
        capsule: QWidget,
        controller: IslandController,
        get_usage_totals: Callable[[str], UsageTotals],
        get_session_usage: Callable[[], SessionUsage] | None = None,
        on_refresh_clicked: Callable[[], None] | None = None,
        get_session_details: Callable[[Session], SessionDetails] | None = None,
    ) -> None:
        super().__init__()
        self._capsule = capsule
        self._controller = controller
        self._get_usage_totals = get_usage_totals
        # Optional: when wired (in __main__.py), provides the 5h session
        # block + remote quota for the top USAGE card. When None
        # (e.g. legacy callers, tests), the session card renders an
        # empty placeholder so existing tests aren't broken.
        self._get_session_usage = get_session_usage
        # Optional manual-refresh hook. Wired in __main__ to bypass the
        # QuotaProvider's TTL and force an immediate fetch — gives the
        # user an out when the auto-refresh hasn't caught the latest
        # state yet (cache TTL is 5 min, heartbeat is 60 s, so worst
        # case the displayed % can lag 5 min behind reality).
        self._on_refresh_clicked = on_refresh_clicked
        # Per-session detail composer. Wired in __main__ to combine
        # JSONL metadata (aiTitle / gitBranch / lastPrompt / version),
        # process state (status / name / startedAt from
        # ~/.claude/sessions/<pid>.json), and aggregate usage (cost /
        # turn count / sidechain count) into one SessionDetails record
        # for the row's hover tooltip. None → tooltip falls back to a
        # minimal "<cwd>" string.
        self._get_session_details = get_session_details
        self._period = "today"
        # Diff-based row update: keep widget references keyed by pid so that
        # session ticks (every ~10s) don't tear down rows the user might be
        # hovering. The placeholder widget (no sessions) is tracked separately
        # — its presence is mutually exclusive with any row.
        self._rows: dict[int, QPushButton] = {}
        self._placeholder: QLabel | None = None

        self._setup_window()
        self._build_ui()

        controller.state_changed.connect(self._on_state_changed)

    # ------------------------------------------------------------------
    # Window setup
    # ------------------------------------------------------------------

    def _setup_window(self) -> None:
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        # Intentionally NOT setting WA_ShowWithoutActivating: that attribute
        # sets WS_EX_NOACTIVATE, which makes WM_MOUSEACTIVATE return
        # MA_NOACTIVATE — clicks deliver but never make us the foreground
        # process, so SetForegroundWindow on the target terminal then fails
        # the "calling process must be foreground" rule.
        self.setFixedWidth(_PANEL_W)
        self.hide()

    def _position(self) -> None:
        cap = self._capsule.frameGeometry()
        x = cap.center().x() - self.width() // 2
        y = cap.bottom() + _GAP
        self.move(x, y)

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(14, 14, 14, 14)
        root.setSpacing(8)

        # Title
        title = QLabel("CLAUDE SESSIONS")
        title.setStyleSheet(_STYLE_TITLE)
        root.addWidget(title)

        # Session list container
        self._session_box = QVBoxLayout()
        self._session_box.setSpacing(_GROUP_GAP)
        root.addLayout(self._session_box)

        # Separator
        sep = QFrame()
        sep.setFixedHeight(1)
        sep.setStyleSheet(_STYLE_SEP)
        root.addWidget(sep)

        # Usage title row: "USAGE" + manual refresh button on the right.
        usage_header = QHBoxLayout()
        usage_header.setSpacing(6)
        usage_title = QLabel("USAGE")
        usage_title.setStyleSheet(_STYLE_TITLE)
        usage_header.addWidget(usage_title)
        usage_header.addStretch()
        self._refresh_btn = QPushButton("↻")
        self._refresh_btn.setStyleSheet(_STYLE_REFRESH_BTN)
        self._refresh_btn.setFixedSize(20, 20)
        self._refresh_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._refresh_btn.setToolTip("Refresh quota now")
        self._refresh_btn.clicked.connect(self._on_manual_refresh)
        usage_header.addWidget(self._refresh_btn)
        root.addLayout(usage_header)

        # ── 5h session card (top, highlighted) ────────────────────────
        self._session_card = self._build_session_card()
        root.addWidget(self._session_card)

        # ── Period card (bottom, switches with period buttons) ────────
        self._period_card = self._build_period_card()
        root.addWidget(self._period_card)

        # Period selector
        period_row = QHBoxLayout()
        period_row.setSpacing(6)
        self._period_btns: dict[str, QPushButton] = {}
        for label, key in [
            ("Today",   "today"),
            ("Daily",   "daily"),
            ("Weekly",  "weekly"),
            ("Monthly", "monthly"),
        ]:
            btn = QPushButton(label)
            btn.setCheckable(True)
            btn.setStyleSheet(_STYLE_PERIOD_BTN)
            btn.setChecked(key == self._period)
            btn.clicked.connect(lambda _, k=key: self._on_period(k))
            period_row.addWidget(btn)
            self._period_btns[key] = btn
        period_row.addStretch()
        root.addLayout(period_row)

        self.setStyleSheet(_STYLE_PANEL)

    # ------------------------------------------------------------------
    # Slots (called by QtBridge / signals)
    # ------------------------------------------------------------------

    def refresh_sessions(self, sessions: list[Session]) -> None:
        """Render sessions as a list of cards.

        Sessions sharing the same ``(window_handle, project_path)`` key
        are merged into one rounded card with thin internal separators —
        the iOS-Settings-style group. A session whose ``window_handle``
        is None (couldn't resolve to a WT host) is rendered as its own
        standalone rounded button. Single-session groups also render as
        standalone buttons (visually identical to the flat-list mode).

        Why ``(window_handle, project_path)`` rather than tab id: WT's
        UIA tree only exposes the *active* pane's title in
        TabItem.Name, so we cannot directly determine which tab an
        inactive split-pane belongs to. The wt_hwnd + cwd pair is a
        practical proxy: split panes inside one tab almost always
        share both. The known false-positive — same project opened in
        two separate tabs of the same WT window — is rare in practice.

        Row widgets are cached by pid to preserve hover/pressed state
        across the 10s scan tick. Cards are rebuilt every refresh
        (cheap; just QFrame + QVBoxLayout).
        """
        self._clear_session_layout()

        if not sessions:
            self._show_placeholder()
            self._gc_rows(set())
            self.adjustSize()
            self._position()
            return

        self._hide_placeholder()

        sorted_sessions = sorted(sessions, key=_session_sort_key)
        groups = _consecutive_groups(sorted_sessions)

        needed_pids: set[int] = set()
        for group in groups:
            self._session_box.addWidget(self._make_group_widget(group))
            for s in group:
                needed_pids.add(s.pid)

        self._gc_rows(needed_pids)

        self.adjustSize()
        self._position()

    def refresh_usage_bar(self, _: object = None) -> None:
        """Refresh both USAGE cards. Kept the legacy method name so the
        existing ``totals_changed`` signal wire-up in __main__.py
        continues to fire this on every DB change."""
        self._refresh_session_card()
        self._refresh_period_card()

    def _on_manual_refresh(self) -> None:
        """User clicked the ↻ button. Force a quota fetch (bypassing
        the QuotaProvider's TTL) and immediately redraw the cards.

        The fetch is synchronous on the Qt main thread — we live with
        the ~3s worst-case HTTP timeout (matches the rest of the
        provider) because the user is staring at the UI waiting for it
        to update; an async dance with a spinner is overkill here."""
        if self._on_refresh_clicked is not None:
            try:
                self._on_refresh_clicked()
            except Exception as exc:
                # Manual refresh must never crash the UI; the worst
                # case is "you press it and nothing changes".
                import sys as _sys
                print(f"[claude-island] manual refresh failed: {exc}",
                      file=_sys.stderr)
        self.refresh_usage_bar()

    # ------------------------------------------------------------------
    # USAGE: session card (top, 5h Anthropic block)
    # ------------------------------------------------------------------

    def _build_session_card(self) -> QFrame:
        card = QFrame()
        card.setObjectName("usage_session_card")
        card.setStyleSheet(_STYLE_USAGE_SESSION_CARD)

        layout = QVBoxLayout(card)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(4)

        # Header row: ● dot · "Current 5h session" · stretch · "Resets …"
        hdr = QHBoxLayout()
        hdr.setSpacing(8)
        self._session_dot = QLabel("●")
        self._session_dot.setStyleSheet(_STYLE_DOT.format(color=_DOT_GRAY))
        hdr.addWidget(self._session_dot)
        hdr_label = QLabel("Current 5h session")
        hdr_label.setStyleSheet(_STYLE_USAGE_HEADER)
        hdr.addWidget(hdr_label)
        hdr.addStretch()
        self._session_reset = QLabel("—")
        self._session_reset.setStyleSheet(_STYLE_USAGE_RESET)
        self._session_reset.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )
        hdr.addWidget(self._session_reset)
        layout.addLayout(hdr)

        # Amount row: $X.XX · stretch · [progress bar] · "53% used"
        amt = QHBoxLayout()
        amt.setSpacing(8)
        self._session_amount = QLabel("—")
        self._session_amount.setStyleSheet(_STYLE_USAGE_AMOUNT)
        amt.addWidget(self._session_amount)
        amt.addStretch()
        self._session_bar = QProgressBar()
        self._session_bar.setRange(0, 100)
        self._session_bar.setFixedWidth(110)
        self._session_bar.setFixedHeight(6)
        self._session_bar.setTextVisible(False)
        # Initial colour is green; _refresh_session_card replaces this
        # with the real threshold-based colour as soon as quota arrives.
        self._session_bar.setStyleSheet(_PROGRESS_BAR_TPL.format(color=_BAR_GREEN))
        self._session_bar.hide()      # hidden until quota arrives
        amt.addWidget(self._session_bar)
        self._session_pct = QLabel("")
        self._session_pct.setStyleSheet(_STYLE_USAGE_PCT)
        self._session_pct.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )
        amt.addWidget(self._session_pct)
        layout.addLayout(amt)

        # Per-model breakdown one-liner
        self._session_models = QLabel("")
        self._session_models.setStyleSheet(_STYLE_USAGE_MODEL)
        layout.addWidget(self._session_models)

        return card

    def _refresh_session_card(self) -> None:
        if self._get_session_usage is None:
            # No provider wired — leave the card in its placeholder state.
            return
        s = self._get_session_usage()

        if s.start_time is None:
            # Empty DB — render a "no active session" state.
            self._session_dot.setStyleSheet(_STYLE_DOT.format(color=_DOT_GRAY))
            self._session_reset.setText("—")
            self._session_amount.setText("No active session")
            self._session_bar.hide()
            self._session_pct.setText("")
            self._session_models.setText("")
            return

        # Dot color: green when the session window is still open. Trust
        # the Anthropic endpoint's resets_at when we have it (it's the
        # authoritative server boundary); otherwise fall back to the
        # local-derived end_time. Without this fallback, a session that
        # crossed 5h since its first JSONL entry but is still active per
        # Anthropic's accounting would render as gray.
        now = datetime.now(timezone.utc)
        if s.quota is not None:
            active = s.quota.five_hour_resets_at > now
        else:
            active = s.end_time is not None and s.end_time > now
        self._session_dot.setStyleSheet(
            _STYLE_DOT.format(color=_DOT_GREEN if active else _DOT_GRAY)
        )

        # Reset countdown — prefer the (more authoritative) Anthropic
        # endpoint reset time when we have it, fall back to the local
        # session_end (start_time + 5h) otherwise.
        if s.quota is not None:
            self._session_reset.setText(
                "Resets " + _fmt_reset(s.quota.five_hour_resets_at)
            )
        else:
            self._session_reset.setText("Resets " + _fmt_reset(s.end_time))

        # Main amount
        self._session_amount.setText(_fmt_money(s.total_cost_usd))

        # Quota progress bar — only when the provider returned something.
        # Bar chunk + pct text share the same colour so the signal reads
        # in either direction (eyes hit either the bar or the text first).
        if s.quota is not None:
            pct = max(0, min(100, int(round(s.quota.five_hour_pct))))
            color = _quota_color(pct, stale=s.quota.is_stale)
            self._session_bar.setValue(pct)
            self._session_bar.setStyleSheet(_PROGRESS_BAR_TPL.format(color=color))
            self._session_bar.show()
            stale_marker = " ⚠" if s.quota.is_stale else ""
            self._session_pct.setStyleSheet(f"color: {color}; font-size: 11px;")
            self._session_pct.setText(f"{pct}% used{stale_marker}")
        else:
            self._session_bar.hide()
            self._session_pct.setText("")

        # Model breakdown — top 3 by spend, joined with "·"
        if s.by_model:
            top = s.by_model[:3]
            chunks = [f"{_fmt_model_label(m.model)} {_fmt_money(m.cost_usd)}"
                      for m in top]
            self._session_models.setText("  ·  ".join(chunks))
        else:
            self._session_models.setText("")

    # ------------------------------------------------------------------
    # USAGE: period card (bottom, today / 24h / 7d / 30d)
    # ------------------------------------------------------------------

    def _build_period_card(self) -> QFrame:
        card = QFrame()
        card.setObjectName("usage_period_card")
        card.setStyleSheet(_STYLE_USAGE_PERIOD_CARD)

        layout = QVBoxLayout(card)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(6)

        # Header: period name · stretch · total
        hdr = QHBoxLayout()
        hdr.setSpacing(8)
        self._period_name_label = QLabel("Today")
        self._period_name_label.setStyleSheet(_STYLE_USAGE_PERIOD_NAME)
        hdr.addWidget(self._period_name_label)
        hdr.addStretch()
        self._period_total_label = QLabel("—")
        self._period_total_label.setStyleSheet(_STYLE_USAGE_PERIOD_TOTAL)
        self._period_total_label.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )
        hdr.addWidget(self._period_total_label)
        layout.addLayout(hdr)

        # Thin separator
        sep = QFrame()
        sep.setFixedHeight(1)
        sep.setStyleSheet(_STYLE_SEP)
        layout.addWidget(sep)

        # Two compact token rows
        self._period_tokens_io = QLabel("")
        self._period_tokens_io.setStyleSheet(_STYLE_USAGE_TOKEN_ROW)
        layout.addWidget(self._period_tokens_io)
        self._period_tokens_cache = QLabel("")
        self._period_tokens_cache.setStyleSheet(_STYLE_USAGE_TOKEN_ROW)
        layout.addWidget(self._period_tokens_cache)

        return card

    def _refresh_period_card(self) -> None:
        t = self._get_usage_totals(self._period)
        self._period_name_label.setText(self._period_label())
        self._period_total_label.setText(_fmt_money(t.cost_usd))
        self._period_tokens_io.setText(
            f"Input  {_fmt_tokens(t.input_tokens)}  ·  "
            f"Output  {_fmt_tokens(t.output_tokens)}"
        )
        self._period_tokens_cache.setText(
            f"Cache W {_fmt_tokens(t.cache_creation_tokens)}  ·  "
            f"Cache R {_fmt_tokens(t.cache_read_tokens)}"
        )

    def _period_label(self) -> str:
        """Friendly name for the current period selection."""
        if self._period == "today":
            return "Today"
        if self._period == "daily":
            return "Past 24 hours"
        if self._period == "weekly":
            return "Past 7 days"
        return "Past 30 days"

    def _on_state_changed(self, state: str) -> None:
        if state == "expanded":
            self.refresh_sessions(self._controller.sessions)
            self.refresh_usage_bar()
            self._position()
            self.show()
            self.raise_()
            # Explicitly take foreground so subsequent SetForegroundWindow
            # calls from row clicks are allowed by Win32.
            self.activateWindow()
        else:
            self.hide()

    def _on_period(self, period: str) -> None:
        self._period = period
        for key, btn in self._period_btns.items():
            btn.setChecked(key == period)
        self.refresh_usage_bar()

    # ------------------------------------------------------------------
    # Session row factory
    # ------------------------------------------------------------------

    def _make_row(self, session: Session) -> QPushButton:
        """Build a click-target row with a 3-element horizontal layout:
        ``● name ............... cost``.

        The QPushButton supplies the click target, hover/pressed
        backgrounds, and rounded background. A QHBoxLayout inside the
        button positions three QLabels (dot / name / meta). Each label
        has WA_TransparentForMouseEvents so clicks anywhere on the row
        fall through to the button.

        Right-click opens a SessionDetailPopup with the rich metadata
        (id / cwd / created / per-model tokens / last prompt). Left
        click activates the WT tab as before.
        """
        btn = QPushButton()
        btn.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        btn.setFixedHeight(_ROW_HEIGHT)
        btn.setProperty("_session", session)
        btn.setProperty("_siblings", [])

        layout = QHBoxLayout(btn)
        layout.setContentsMargins(_ROW_PAD_H, 0, _ROW_PAD_H, 0)
        layout.setSpacing(10)

        dot = QLabel("●")
        dot.setObjectName("activity_dot")
        dot.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        layout.addWidget(dot)

        name_label = QLabel()
        name_label.setObjectName("name_label")
        name_label.setStyleSheet(_STYLE_NAME)
        name_label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        name_label.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        layout.addWidget(name_label, 1)

        # Right-side meta slot. Used to be ``age_label`` (e.g. "19h");
        # the user asked for cumulative session cost instead. Object
        # name kept neutral (``meta_label``) so the slot is reusable
        # if we ever want to put something else there.
        meta_label = QLabel()
        meta_label.setObjectName("meta_label")
        meta_label.setStyleSheet(_STYLE_AGE)
        meta_label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        meta_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        layout.addWidget(meta_label)

        self._update_row(btn, session)
        btn.clicked.connect(lambda: self._on_row_clicked(
            btn.property("_session"),
            btn.property("_siblings") or [],
        ))
        # Right-click → detail popup at cursor. CustomContextMenu lets
        # us bypass Qt's default text-context-menu and route the event
        # to our handler, which opens the rich SessionDetailPopup.
        btn.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        btn.customContextMenuRequested.connect(
            lambda local_pos: self._show_detail_popup(btn, local_pos)
        )
        return btn

    def _update_row(self, btn: QPushButton, session: Session) -> None:
        """Refresh dot color, name, and right-side cost on every
        refresh tick. The cumulative session cost replaces the old
        "Xh ago" so a glance at the row tells you who's spending
        most. Activity recency stays encoded in the dot colour, just
        no longer in the text."""
        details: SessionDetails | None = None
        if self._get_session_details is not None:
            try:
                details = self._get_session_details(session)
            except Exception as exc:
                # Detail composition is enrichment, not load-bearing —
                # never let a composer hiccup take down the row update.
                import sys as _sys
                print(f"[claude-island] session details failed: {exc}",
                      file=_sys.stderr)
        title = (
            (details.name if details and details.name else None)
            or (details.ai_title if details and details.ai_title else None)
            or session.project_path.name
            or str(session.project_path)
        )
        # "—" when details are unavailable rather than falling back to
        # an age string — keeps the meta slot semantically consistent
        # ("this column is always cost").
        meta_text = _fmt_money(details.cost_usd) if details is not None else "—"

        dot = btn.findChild(QLabel, "activity_dot")
        if dot is not None:
            dot.setStyleSheet(_STYLE_DOT.format(color=_activity_color(session.last_activity)))

        name_label = btn.findChild(QLabel, "name_label")
        if name_label is not None and name_label.text() != title:
            name_label.setText(title)

        meta_label = btn.findChild(QLabel, "meta_label")
        if meta_label is not None and meta_label.text() != meta_text:
            meta_label.setText(meta_text)

        # Right-click triggers the rich popup; tooltip would compete
        # for the same surface, so it's gone. Explicit empty string
        # in case Qt cached a value from an earlier build of the row.
        btn.setToolTip("")

        btn.setProperty("_session", session)

    # ------------------------------------------------------------------
    # Card composition (PR2: same-tab grouping)
    # ------------------------------------------------------------------

    def _make_group_widget(self, group: list[Session]) -> QWidget:
        """One group → one widget. Single-session groups render as a
        standalone rounded button; multi-session groups render as a
        rounded card with flat internal rows + thin separators."""
        if len(group) == 1:
            row = self._get_or_create_row(group[0], group, in_card=False)
            row.setParent(None)  # detach from any prior parent
            return row
        return self._make_multi_card(group)

    def _make_multi_card(self, sessions: list[Session]) -> QFrame:
        card = QFrame()
        card.setObjectName("group_card")
        card.setStyleSheet(_STYLE_GROUP_CARD)
        layout = QVBoxLayout(card)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        for i, session in enumerate(sessions):
            if i > 0:
                sep = QFrame()
                sep.setFixedHeight(1)
                sep.setStyleSheet(_STYLE_GROUP_ROW_SEP)
                layout.addWidget(sep)
            row = self._get_or_create_row(session, sessions, in_card=True)
            row.setParent(None)
            layout.addWidget(row)
        return card

    def _get_or_create_row(
        self, session: Session, group: list[Session], *, in_card: bool
    ) -> QPushButton:
        """Cached factory: same pid keeps the same QPushButton across
        refreshes (preserves hover/pressed state). Style is reapplied
        each call because a row can move between standalone (rounded)
        and in-card (flat) layouts as group membership changes.

        ``group`` is the full list of sessions in this row's group
        (including the row itself). Stored on the button so the click
        handler can pass siblings to the activator — needed for the
        inactive-pane case where the row's own console title doesn't
        appear in any TabItem.Name and we have to fall back to one of
        the siblings' titles to actually switch the WT tab.
        """
        btn = self._rows.get(session.pid)
        if btn is None:
            btn = self._make_row(session)
            self._rows[session.pid] = btn
        btn.setStyleSheet(_STYLE_GROUP_ROW if in_card else _STYLE_SINGLE_ROW)
        self._update_row(btn, session)
        siblings = [s for s in group if s.pid != session.pid]
        btn.setProperty("_siblings", siblings)
        return btn

    # ------------------------------------------------------------------
    # Layout housekeeping
    # ------------------------------------------------------------------

    def _clear_session_layout(self) -> None:
        """Remove every top-level item from session_box. Cached row
        buttons are detached (kept alive in self._rows for reuse);
        cards and the placeholder are deleted."""
        cached = set(self._rows.values())
        while self._session_box.count():
            item = self._session_box.takeAt(0)
            widget = item.widget()
            if widget is None:
                continue
            if widget in cached:
                widget.setParent(None)
                continue
            # Card: detach any cached rows inside before deleting it,
            # so they survive for the next group composition.
            for child in widget.findChildren(QPushButton):
                if child in cached:
                    child.setParent(None)
            if widget is self._placeholder:
                self._placeholder = None
            widget.deleteLater()

    def _show_placeholder(self) -> None:
        # Drop any cached rows: there are no sessions to back them.
        for btn in self._rows.values():
            btn.deleteLater()
        self._rows.clear()
        if self._placeholder is None:
            self._placeholder = QLabel("No active sessions")
            self._placeholder.setStyleSheet("color: #555; font-size: 12px;")
        self._session_box.addWidget(self._placeholder)

    def _hide_placeholder(self) -> None:
        if self._placeholder is not None:
            self._placeholder.deleteLater()
            self._placeholder = None

    def _gc_rows(self, needed_pids: set[int]) -> None:
        for pid in list(self._rows.keys()):
            if pid not in needed_pids:
                self._rows.pop(pid).deleteLater()

    def _show_detail_popup(self, btn: QPushButton, local_pos) -> None:
        """Build a SessionDetailPopup with fresh data and pop it at the
        cursor. Called from the row's customContextMenuRequested signal.

        We compose details on demand (not from a cached value) so the
        popup always reflects the latest cost / status. Failures in the
        composer are logged and the popup still opens with whatever
        partial data is available — falling back to None when the
        composer is unwired or the lookup raises."""
        session = btn.property("_session")
        if session is None:
            return
        details: SessionDetails | None = None
        if self._get_session_details is not None:
            try:
                details = self._get_session_details(session)
            except Exception as exc:
                import sys as _sys
                print(f"[claude-island] detail popup composer failed: {exc}",
                      file=_sys.stderr)
        popup = SessionDetailPopup(details, session, parent=self)
        # Map the row-local right-click position to global screen
        # coordinates. ``btn.mapToGlobal`` does the right thing across
        # multi-monitor / DPI setups.
        popup.move(btn.mapToGlobal(local_pos))
        popup.show()
        # Hold a reference so Qt's GC doesn't tear the popup down
        # before the user gets to interact with it. Replacing the slot
        # on each open is fine — Qt's Popup flag closes the previous
        # instance when the new one shows.
        self._active_detail_popup = popup

    def _on_row_clicked(self, session: Session, siblings: list[Session]) -> None:
        # Activate first, then collapse — order matters: while our panel is
        # still on top (StaysOnTopHint) we are the foreground process, which
        # is the only state in which SetForegroundWindow is allowed to
        # surface another process's window.
        self.session_activated.emit(session, siblings)
        self._controller.toggle_expanded()

    # ------------------------------------------------------------------
    # Paint
    # ------------------------------------------------------------------

    def paintEvent(self, event: object) -> None:  # type: ignore[override]
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        path = QPainterPath()
        path.addRoundedRect(0, 0, self.width(), self.height(), 16, 16)
        painter.fillPath(path, QColor(18, 18, 18, 240))
