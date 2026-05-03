from __future__ import annotations

from datetime import datetime, timezone
from typing import Callable

from PySide6.QtCore import (
    QEasingCurve,
    QPoint,
    QPropertyAnimation,
    Qt,
)
from PySide6.QtGui import QColor, QPainter, QPainterPath
from PySide6.QtWidgets import (
    QApplication,
    QGraphicsOpacityEffect,
    QLabel,
    QMenu,
    QWidget,
)

from claude_island.core.models import QuotaSnapshot, Session, SessionDetails
from .controller import IslandController

# Two widths: normal (no quota mini-bar) vs warning (quota mini-bar
# appended on the right). Switching between them happens on every
# refresh that crosses the warn threshold; the resize is unobtrusive
# at this scale and re-centring keeps the pill anchored to the top.
_CAPSULE_W = 200
_CAPSULE_W_WITH_QUOTA = 290
_CAPSULE_H = 36
_DOT_W = 12
_DOT_H = 12
_TOP_MARGIN = 8

_DOT_LEFT_PAD = 12  # px from pill's left edge to dot's left edge
_DOT_LABEL_W = 14   # px reserved for the "●" glyph
_TEXT_LEFT = _DOT_LEFT_PAD + _DOT_LABEL_W + 4  # 4px gap between dot and text

# Mini quota progress bar dimensions. 56 px is roughly the same density
# as the iOS battery widget — recognisable as a progress indicator
# without competing with the text for attention.
_QUOTA_BAR_W = 56
_QUOTA_BAR_H = 6
_QUOTA_RIGHT_PAD = 12  # px from pill's right edge to bar's right edge

# Threshold % at which the mini quota bar starts surfacing on the
# capsule. Below the threshold the bar is hidden so the pill doesn't
# carry a green-only "everything's fine" indicator that adds noise
# without information. Once the user is approaching the rate-limit
# cliff the indicator becomes useful.
_QUOTA_WARN_THRESHOLD = 70
_QUOTA_CRITICAL_THRESHOLD = 90

# Heuristic for "this session is currently doing something". An active
# Claude Code session writes a JSONL row at least every few seconds
# while the model is streaming or the tool loop is iterating; once the
# user has stopped typing and the model is done, writes pause. 30 s
# gives a comfortable margin for one tool round-trip without flapping
# the breathing animation off mid-burst.
_ACTIVE_THRESHOLD_SECONDS = 30

# Breathing animation parameters. Tuned more aggressively after user
# feedback that the original 2.0 s / 0.55 floor cycle was too easy
# to miss in peripheral vision. 1.2 s round-trip (0.6 in, 0.6 out)
# + 0.2 floor make the pulse genuinely "alive" without crossing into
# distracting strobe territory. 0.2 (not 0) keeps the dot just barely
# visible at the dimmest point — going to 0 reads as "the dot
# disappeared" which is wrong (the session is still there).
_BREATH_PERIOD_MS = 1200
_BREATH_OPACITY_FLOOR = 0.20
_BREATH_OPACITY_PEAK = 1.0

_STYLE_LABEL = "color: white; font-size: 12px; font-family: 'Segoe UI', sans-serif;"
# Dot colours: green when at least one session is active, neutral grey
# when all idle. Matches the in-card status-dot semantics so the user
# learns one mapping ("green = something is happening") globally.
# Active dot uses a brighter, more saturated green + bumped font size
# (16 vs 14) so the pulsing animation reads as a meaningful signal in
# peripheral vision rather than as ambient noise.
_STYLE_DOT_ACTIVE = "color: #22c55e; font-size: 16px; font-weight: bold;"
_STYLE_DOT_IDLE = "color: #6b7280; font-size: 14px;"
_BG_COLOR = QColor(18, 18, 18, 230)
# Capsule background swap when quota crosses the critical threshold —
# amber that reads "warning, not failure" against the dark text. The
# RGB matches the row chip's amber so the warning vocabulary stays
# consistent across panel + pill.
_BG_COLOR_WARN = QColor(120, 53, 15, 230)
_BG_COLOR_CRITICAL = QColor(127, 29, 29, 230)
_DOT_COLOR = QColor(80, 80, 80, 200)
# Mini quota bar pen / brush colours. Threshold-driven, mirroring the
# summary card's progress bar palette so the user sees the same colour
# story in both places.
_QUOTA_BAR_TRACK = QColor(255, 255, 255, 40)
_QUOTA_BAR_FILL_WARN = QColor(250, 204, 21)     # amber
_QUOTA_BAR_FILL_CRITICAL = QColor(248, 113, 113)  # bright red
_STYLE_QUOTA_PCT = "color: #fde68a; font-size: 11px; font-weight: 600;"
_STYLE_QUOTA_PCT_CRITICAL = "color: #fee2e2; font-size: 11px; font-weight: 600;"


def _fmt_money(amount: float) -> str:
    """Compact $ formatter — duplicated from expanded_window._fmt_money.
    Keep in sync; extract to a shared util at the 3rd usage."""
    if amount < 0.01:
        return f"${amount:.3f}"
    if amount < 10:
        return f"${amount:.2f}"
    if amount < 1000:
        return f"${amount:.0f}"
    return f"${amount / 1000:.1f}K"

_STYLE_MENU = """
    QMenu {
        background: #1e1e1e;
        color: #e0e0e0;
        border: 1px solid #333;
        padding: 4px;
        font-size: 12px;
    }
    QMenu::item { padding: 6px 18px; border-radius: 4px; }
    QMenu::item:selected { background: #2e2e2e; }
    QMenu::separator { height: 1px; background: #333; margin: 4px 6px; }
"""


class CapsuleWindow(QWidget):
    """Frameless, always-on-top pill anchored to the top-centre of the screen.

    Clicking toggles the expanded panel via the controller.
    Resizes to a small dot when there are no active sessions.
    """

    def __init__(
        self,
        controller: IslandController,
        *,
        get_today_cost: Callable[[], float] | None = None,
        get_session_details: Callable[[Session], SessionDetails | None] | None = None,
        get_quota_snapshot: Callable[[], QuotaSnapshot | None] | None = None,
    ) -> None:
        super().__init__()
        self._controller = controller
        self._is_dot = True
        # Once the user picks "Hide" from the right-click menu the capsule
        # stays gone until the next process restart — there is no tray icon
        # to bring it back, so all auto-show paths must respect this flag.
        self._hidden_by_user = False
        # Pull today's spend on demand (closure → main wires it to
        # usage_registry.get_totals('today').cost_usd). None means the
        # capsule omits the $ field — keeps the constructor optional so
        # existing tests instantiating CapsuleWindow(controller) still work.
        self._get_today_cost = get_today_cost
        self._cost_cache: float = 0.0
        # Used to render the running-session's name (custom rename ↦
        # ai_title ↦ project basename) in place of "N sessions" when
        # exactly one session is active. None ⇒ capsule falls back to
        # the count-only label.
        self._get_session_details = get_session_details
        # Quota snapshot getter for the mini quota bar. Same pattern
        # as get_today_cost — closure-injected by main so the panel's
        # provider-tab state is honoured. None ⇒ capsule never shows
        # the quota bar (multi-provider was never wired).
        self._get_quota_snapshot = get_quota_snapshot
        # Latest 5h % cached so paintEvent can render the bar without
        # re-fetching. None ⇒ no snapshot yet (or below threshold);
        # paint code skips the bar entirely.
        self._quota_pct_cache: float | None = None

        self._setup_window()

        # Two labels rather than one so the "●" can pulse independently
        # of the text. A QGraphicsOpacityEffect on the dot label is the
        # cleanest way to animate just one part of the pill — animating
        # the whole label would fade the text in/out, which is
        # distracting when the user is trying to read the cost.
        self._dot_label = QLabel("●", self)
        self._dot_label.setAlignment(
            Qt.AlignmentFlag.AlignCenter | Qt.AlignmentFlag.AlignVCenter
        )
        self._dot_label.setStyleSheet(_STYLE_DOT_IDLE)
        self._dot_opacity = QGraphicsOpacityEffect(self._dot_label)
        self._dot_opacity.setOpacity(1.0)
        self._dot_label.setGraphicsEffect(self._dot_opacity)

        self._label = QLabel("", self)
        self._label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._label.setStyleSheet(_STYLE_LABEL)

        # Animation is created lazily-armed (target set up front, but
        # the loop only starts when refresh_sessions detects an active
        # session). Keeping a single instance avoids a leaking
        # animation-per-tick pattern.
        self._breath_anim = QPropertyAnimation(self._dot_opacity, b"opacity", self)
        self._breath_anim.setDuration(_BREATH_PERIOD_MS)
        self._breath_anim.setStartValue(_BREATH_OPACITY_PEAK)
        self._breath_anim.setKeyValueAt(0.5, _BREATH_OPACITY_FLOOR)
        self._breath_anim.setEndValue(_BREATH_OPACITY_PEAK)
        self._breath_anim.setEasingCurve(QEasingCurve.Type.InOutSine)
        self._breath_anim.setLoopCount(-1)
        self._is_breathing = False

        controller.state_changed.connect(self._on_state_changed)
        self._apply_dot()

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
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)

    def _center_top(self, w: int, h: int) -> None:
        screen = QApplication.primaryScreen()
        geom = screen.geometry()
        x = geom.center().x() - w // 2
        self.setGeometry(x, geom.top() + _TOP_MARGIN, w, h)

    # ------------------------------------------------------------------
    # State handlers
    # ------------------------------------------------------------------

    def _on_state_changed(self, state: str) -> None:
        if self._hidden_by_user:
            return
        # Three controller states collapse to two capsule visuals: 'dot' is
        # the minimal presence; both 'collapsed' and 'expanded' show the
        # full pill (the expanded panel is a separate window stacked below).
        if state == "dot":
            self._apply_dot()
        else:
            self._apply_capsule()

    def _apply_dot(self) -> None:
        self._is_dot = True
        self._center_top(_DOT_W, _DOT_H)
        self._label.hide()
        self._dot_label.hide()
        self._stop_breathing()
        self.update()
        self.show()

    def _apply_capsule(self) -> None:
        self._is_dot = False
        self._label.setText(self._compose_label_text())
        # Width grows when a quota mini-bar should be rendered (cache
        # populated AND ≥ warn threshold). Variable width keeps the
        # pill compact when there's nothing to flag.
        showing_quota = self._should_show_quota_bar()
        target_w = _CAPSULE_W_WITH_QUOTA if showing_quota else _CAPSULE_W
        self._center_top(target_w, _CAPSULE_H)
        # Dot sits in a fixed slot on the left; text label takes the
        # rest of the width minus the right pad (which holds the mini
        # quota bar when shown, or just empty space when not).
        self._dot_label.setGeometry(_DOT_LEFT_PAD, 0, _DOT_LABEL_W, _CAPSULE_H)
        right_pad = (
            (_QUOTA_RIGHT_PAD + _QUOTA_BAR_W + 36)  # 36 px for "78%"
            if showing_quota
            else _DOT_LEFT_PAD + _DOT_LABEL_W      # symmetric blank
        )
        self._label.setGeometry(
            _TEXT_LEFT, 0, target_w - _TEXT_LEFT - right_pad, _CAPSULE_H,
        )
        self._dot_label.show()
        self._label.show()
        self._refresh_active_state()
        self.update()
        self.show()

    def _should_show_quota_bar(self) -> bool:
        """True when the cached 5h % crossed the warning threshold and
        we have a getter wired. Below the threshold the indicator is
        hidden — a green-only "you're at 12 %" reading would be noise."""
        if self._get_quota_snapshot is None:
            return False
        if self._quota_pct_cache is None:
            return False
        return self._quota_pct_cache >= _QUOTA_WARN_THRESHOLD

    def _compose_label_text(self) -> str:
        """Render the pill text — combines session count or running
        session name with today's cumulative spend.

        Text logic:
          - Exactly one *active* session AND we can resolve its name
            ⇒ show that name (so the user can tell which session is
            burning their $ at a glance).
          - All other cases (zero / multiple active, or name unknown)
            ⇒ show "N sessions" so the digit is at least informative.

        Cost suffix is appended when the getter is wired and the
        cumulative is > 0. ``$0`` is suppressed (no records yet) so a
        fresh first launch reads cleanly as "1 session" instead of
        "1 session  $0".
        """
        cost_suffix = ""
        if self._get_today_cost is not None and self._cost_cache > 0:
            cost_suffix = f"  {_fmt_money(self._cost_cache)}"

        active = self._active_sessions()
        if len(active) == 1 and self._get_session_details is not None:
            name = self._resolve_session_name(active[0])
            if name:
                return f"{name}{cost_suffix}"

        count = len(self._controller.sessions)
        noun = "session" if count == 1 else "sessions"
        return f"{count} {noun}{cost_suffix}"

    def _resolve_session_name(self, session: Session) -> str | None:
        """Return the best human label for ``session``.

        Falls through user-rename → AI title → project directory name.
        Mirrors the resolution order used in ExpandedWindow's row
        rendering so "what the panel calls this session" matches "what
        the pill calls it" for the running-session case."""
        try:
            details = self._get_session_details(session)  # type: ignore[misc]
        except Exception:
            return None
        if details is not None:
            if details.name:
                return details.name
            if details.ai_title:
                return details.ai_title
        basename = session.project_path.name
        return basename or None

    def _active_sessions(self) -> list[Session]:
        """Sessions that are currently doing something.

        Status takes precedence over the activity heuristic:
          * ``status == "busy" / "waiting"`` → running (authoritative,
            comes from Claude Code's own state file).
          * ``status == "idle"`` → NOT running, even if last_activity is
            recent. This filters out cases like a "<synthetic>" session
            that just got a /compact summary written to its JSONL —
            Claude Code marks it idle, but the JSONL bump made the old
            heuristic count it as active and that masked the *real*
            running session in the count check below.
          * ``status`` unknown (no state file, e.g. non-Anthropic
            provider) → fall back to the activity heuristic so we
            still surface obviously-busy sessions.

        Fast path: when the details composer is unwired, just use the
        heuristic — keeps tests + minimal setups working as before."""
        now = datetime.now(timezone.utc)
        result: list[Session] = []
        for s in self._controller.sessions:
            status_word: str | None = None
            if self._get_session_details is not None:
                try:
                    d = self._get_session_details(s)
                    if d is not None and isinstance(d.status, str):
                        status_word = d.status.lower()
                except Exception:
                    pass

            if status_word == "idle":
                # Authoritative "not running" — skip even if JSONL
                # was just written (synthetic / compaction churn).
                continue
            if status_word in ("busy", "waiting"):
                result.append(s)
                continue

            # status_word is None (unknown) — fall back to the activity
            # heuristic so the pill still works for providers that
            # don't write a sessions/<pid>.json state file.
            try:
                age = (now - s.last_activity).total_seconds()
            except (TypeError, ValueError):
                continue
            if age < _ACTIVE_THRESHOLD_SECONDS:
                result.append(s)
        return result

    def _refresh_active_state(self) -> None:
        """Synchronise dot colour + breathing animation with whether
        any session is currently active. Idempotent — safe to call from
        every refresh tick; only restarts the animation on transitions."""
        active = bool(self._active_sessions())
        self._dot_label.setStyleSheet(
            _STYLE_DOT_ACTIVE if active else _STYLE_DOT_IDLE
        )
        if active:
            self._start_breathing()
        else:
            self._stop_breathing()

    def _start_breathing(self) -> None:
        if self._is_breathing:
            return
        self._is_breathing = True
        self._breath_anim.start()

    def _stop_breathing(self) -> None:
        if not self._is_breathing:
            return
        self._is_breathing = False
        self._breath_anim.stop()
        # Snap back to fully visible so the dot doesn't get stranded at
        # 0.55 alpha after the animation cuts off mid-cycle.
        self._dot_opacity.setOpacity(1.0)

    def refresh_sessions(self, sessions: object) -> None:
        """Called by bridge when sessions list changes (updates count label)."""
        if self._hidden_by_user:
            return
        if not self._is_dot:
            self._apply_capsule()

    def refresh_quota(self, _: object = None) -> None:
        """Pull the latest quota snapshot and re-render the pill if the
        warn-threshold crossing changed visibility.

        Wired into the same heartbeat that drives ``refresh_usage_bar``
        in __main__ so the pill picks up quota changes without needing
        its own timer. No-op when the getter is unwired (the pill
        simply never shows the quota bar)."""
        if self._hidden_by_user or self._get_quota_snapshot is None:
            return
        previous_visible = self._should_show_quota_bar()
        snap: QuotaSnapshot | None = None
        try:
            snap = self._get_quota_snapshot()
        except Exception as exc:
            import sys as _sys
            print(f"[claude-island] capsule quota fetch failed: {exc}",
                  file=_sys.stderr)
            return
        self._quota_pct_cache = (
            float(snap.five_hour_pct) if snap is not None else None
        )
        # Visibility may have flipped — re-applying the capsule will
        # resize and reposition. If the visible state is unchanged,
        # just trigger a repaint so the bar % updates in place.
        now_visible = self._should_show_quota_bar()
        if not self._is_dot and previous_visible != now_visible:
            self._apply_capsule()
        else:
            self.update()

    def refresh_cost(self, _: object = None) -> None:
        """Called by bridge when usage totals change. Pulls today's
        cost via the injected getter and refreshes the pill text + the
        breathing-dot state.

        Why refresh active state here too: ``totals_changed`` fires on
        every JSONL write, which is the most reliable signal that "a
        session is currently producing turns" — much more responsive
        than waiting for the 10-s process scan cycle to re-run
        ``refresh_sessions``. Without this hook, the breathing dot
        would lag activity by up to 10 s.
        """
        if self._hidden_by_user or self._get_today_cost is None:
            return
        try:
            self._cost_cache = float(self._get_today_cost())
        except Exception:
            # Cost is presentational — never let a registry hiccup crash
            # the UI. Keep the previous cached value rather than zeroing
            # (which would briefly flash "$0" between refreshes).
            return
        if not self._is_dot:
            self._label.setText(self._compose_label_text())
            self._refresh_active_state()

    # ------------------------------------------------------------------
    # Paint + events
    # ------------------------------------------------------------------

    def paintEvent(self, event: object) -> None:  # type: ignore[override]
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        path = QPainterPath()
        r = self.height() / 2

        # Background colour reflects quota severity. Critical (≥ 90 %)
        # gets a deep red wash; warning (≥ 70 %) gets amber. Below the
        # warn threshold (and in dot mode) we use the standard dark
        # grey so normal operation looks unobtrusive.
        if self._is_dot:
            color = _DOT_COLOR
        elif self._quota_pct_cache is not None and self._quota_pct_cache >= _QUOTA_CRITICAL_THRESHOLD:
            color = _BG_COLOR_CRITICAL
        elif self._quota_pct_cache is not None and self._quota_pct_cache >= _QUOTA_WARN_THRESHOLD:
            color = _BG_COLOR_WARN
        else:
            color = _BG_COLOR

        path.addRoundedRect(0, 0, self.width(), self.height(), r, r)
        painter.fillPath(path, color)

        # Mini quota bar — drawn directly on the pill (no QWidget
        # overhead) for two reasons: it shares lifecycle with the pill
        # background, and a child widget here would have to fight the
        # frameless / translucent setup for layering. Skipped entirely
        # when below threshold or the cache has never been populated.
        if not self._is_dot and self._should_show_quota_bar():
            self._paint_quota_bar(painter)

    def _paint_quota_bar(self, painter: QPainter) -> None:
        """Draw the right-side mini quota progress + "78%" caption."""
        pct = max(0.0, min(100.0, float(self._quota_pct_cache or 0)))
        critical = pct >= _QUOTA_CRITICAL_THRESHOLD

        # Layout: [bar] gap [pct text]  flush right against _QUOTA_RIGHT_PAD.
        bar_x = self.width() - _QUOTA_RIGHT_PAD - 36 - _QUOTA_BAR_W
        bar_y = (self.height() - _QUOTA_BAR_H) // 2

        # Track
        track_path = QPainterPath()
        track_path.addRoundedRect(
            bar_x, bar_y, _QUOTA_BAR_W, _QUOTA_BAR_H,
            _QUOTA_BAR_H / 2, _QUOTA_BAR_H / 2,
        )
        painter.fillPath(track_path, _QUOTA_BAR_TRACK)

        # Fill — width = pct * bar_w / 100
        fill_w = max(1, int(_QUOTA_BAR_W * pct / 100))
        fill_path = QPainterPath()
        fill_path.addRoundedRect(
            bar_x, bar_y, fill_w, _QUOTA_BAR_H,
            _QUOTA_BAR_H / 2, _QUOTA_BAR_H / 2,
        )
        painter.fillPath(
            fill_path,
            _QUOTA_BAR_FILL_CRITICAL if critical else _QUOTA_BAR_FILL_WARN,
        )

        # "78%" caption to the right of the bar. Native QPainter draw
        # so we don't have to manage another QLabel + opacity effect.
        from PySide6.QtGui import QFont
        text = f"{int(pct)}%"
        font = QFont("Segoe UI", 9, QFont.Weight.DemiBold)
        painter.setFont(font)
        text_color = (
            QColor(254, 226, 226) if critical else QColor(253, 230, 138)
        )
        painter.setPen(text_color)
        text_x = bar_x + _QUOTA_BAR_W + 4
        text_y = self.height() // 2 + 4  # rough vertical centre for QFont
        painter.drawText(text_x, text_y, text)

    def mousePressEvent(self, event) -> None:  # type: ignore[override]
        if event.button() == Qt.MouseButton.RightButton:
            self._show_context_menu(event.globalPosition().toPoint())
            return
        if event.button() == Qt.MouseButton.LeftButton:
            self._controller.toggle_expanded()

    def _show_context_menu(self, global_pos: QPoint) -> None:
        menu = QMenu(self)
        menu.setStyleSheet(_STYLE_MENU)
        menu.addAction("Hide until restart", self._hide_until_restart)
        menu.addSeparator()
        menu.addAction("Quit ClaudeIsland", QApplication.instance().quit)
        menu.exec(global_pos)

    def _hide_until_restart(self) -> None:
        self._hidden_by_user = True
        # Collapse first so the expanded panel also disappears via its own
        # state_changed handler.
        if self._controller.state == "expanded":
            self._controller.toggle_expanded()
        self.hide()
