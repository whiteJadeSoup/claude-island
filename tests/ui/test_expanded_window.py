"""Tests for ExpandedWindow's diff-based row update (B6).

Uses pytest-qt's qtbot to create a real QApplication. Verifies that:
- repeated refresh with the same pids reuses widgets (no flicker)
- removed pids destroy their widget; new pids insert a fresh one
- text updates happen in-place without recreating the widget
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

# Force offscreen for headless CI / local runs.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QWidget

from claude_island.core.models import Session, UsageTotals
from claude_island.ui.controller import IslandController
from claude_island.ui.expanded_window import ExpandedWindow


def _session(
    pid: int, cwd: str, ago_minutes: int = 0,
    window_handle: int | None = None,  # accepted but ignored — was the WT HWND;
                                       # adapter-internal now (PR2)
) -> Session:
    del window_handle  # signature kept for back-compat with old grouping tests
    return Session(
        pid=pid,
        project_path=Path(cwd),
        session_uuid="",
        last_activity=datetime.now(timezone.utc) - timedelta(minutes=ago_minutes),
    )


@pytest.fixture
def panel(qtbot):
    capsule = QWidget()  # stand-in capsule for positioning
    capsule.show()
    controller = IslandController()
    panel = ExpandedWindow(
        capsule=capsule,
        controller=controller,
        get_usage_totals=lambda period: UsageTotals(period=period)
    )
    qtbot.addWidget(panel)
    qtbot.addWidget(capsule)
    yield panel


def _panel_with_session(qtbot, get_session_usage):
    """Build a panel wired with a stub get_session_usage."""
    from claude_island.ui.expanded_window import ExpandedWindow
    capsule = QWidget()
    capsule.show()
    controller = IslandController()
    p = ExpandedWindow(
        capsule=capsule,
        controller=controller,
        get_usage_totals=lambda period: UsageTotals(period=period),
        get_session_usage=get_session_usage
    )
    qtbot.addWidget(p)
    qtbot.addWidget(capsule)
    return p


def test_first_refresh_creates_one_row_per_session(panel):
    panel._render_sessions([_session(1, "/a"), _session(2, "/b")])
    assert set(panel._rows.keys()) == {1, 2}
    assert panel._placeholder is None


def test_repeated_refresh_with_same_pids_reuses_widgets(panel):
    """The core B6 invariant: same pid set must NOT recreate widgets,
    otherwise hover state and any user interaction is lost on every tick."""
    panel._render_sessions([_session(1, "/a"), _session(2, "/b")])
    btn1_before = panel._rows[1]
    btn2_before = panel._rows[2]

    panel._render_sessions([_session(1, "/a"), _session(2, "/b")])
    assert panel._rows[1] is btn1_before  # same widget instance
    assert panel._rows[2] is btn2_before


def test_removed_pid_drops_its_widget(panel):
    panel._render_sessions([_session(1, "/a"), _session(2, "/b")])
    panel._render_sessions([_session(1, "/a")])  # 2 gone

    assert set(panel._rows.keys()) == {1}


def test_added_pid_inserts_new_widget(panel):
    panel._render_sessions([_session(1, "/a")])
    btn1 = panel._rows[1]

    panel._render_sessions([_session(1, "/a"), _session(2, "/b")])

    assert set(panel._rows.keys()) == {1, 2}
    assert panel._rows[1] is btn1  # 1's widget preserved


def test_existing_row_meta_updates_in_place(panel):
    """The right-side meta slot used to show age; it now shows cost.
    The label is still updated in-place across refreshes (no widget
    rebuild)."""
    from PySide6.QtWidgets import QLabel
    panel._render_sessions([_session(1, "/a")])
    btn = panel._rows[1]
    label = btn.findChild(QLabel, "meta_label")
    assert label is not None
    # Without a get_session_details composer the meta reads "—".
    assert label.text() == "—"
    # The widget itself isn't recreated on the next refresh.
    panel._render_sessions([_session(1, "/a")])
    assert btn.findChild(QLabel, "meta_label") is label


def test_empty_sessions_shows_placeholder(panel):
    panel._render_sessions([])
    assert panel._placeholder is not None
    assert panel._rows == {}


def test_placeholder_disappears_when_sessions_arrive(panel):
    panel._render_sessions([])
    assert panel._placeholder is not None

    panel._render_sessions([_session(1, "/a")])
    assert panel._placeholder is None
    assert set(panel._rows.keys()) == {1}


def test_session_click_dispatches_focus_with_latest_view(qtbot):
    """Property carrier (_session) on the button must be refreshed on each
    update so a click after activity changed dispatches the new view.
    PR2: actions flow through the injected dispatch callable."""
    from claude_island.core.capabilities import Capability
    received: list = []

    capsule = QWidget(); capsule.show()
    controller = IslandController()
    panel = ExpandedWindow(
        capsule=capsule,
        controller=controller,
        get_usage_totals=lambda period: UsageTotals(period=period),
        dispatch=lambda v, cap, **kw: (received.append((v, cap, kw)) or True),
    )
    qtbot.addWidget(panel); qtbot.addWidget(capsule)

    panel._render_sessions([_session(1, "/a", ago_minutes=10)])
    btn = panel._rows[1]
    fresh = _session(1, "/a", ago_minutes=0)
    panel._render_sessions([fresh])

    btn.click()

    assert len(received) == 1
    view, cap, kwargs = received[0]
    assert cap == Capability.FOCUS
    # siblings is always passed (empty for singleton groups) — see
    # WindowsTerminalAdapter.focus for why this kwarg is required.
    assert kwargs == {"siblings": []}
    assert view.last_activity == fresh.last_activity


# --------------------------------------------------------------------------
# Same-tab grouping
# --------------------------------------------------------------------------
# Grouping logic moved out of the UI layer in PR2: WindowsTerminalAdapter
# (claude_island/platform_/terminals/windows_terminal.py) is now responsible
# for bucketing sessions by wt_hwnd / cwd, and it returns SessionGroup
# objects to the UI. The UI just renders one card per group.
#
# Coverage moved to:
# - tests/platform_/test_dispatcher.py — adapter chain + capability merging
# - The grouping rules themselves (wt_hwnd, normalisation, worktree collapse)
#   are tested at the adapter level once mac/win adapters get richer
#   integration tests.
# --------------------------------------------------------------------------

def _top_level_widgets(panel) -> list:
    """Return widgets at the session_box top level (cards or standalone
    buttons), in layout order. Kept because non-grouping tests below
    still use it."""
    box = panel._session_box
    return [box.itemAt(i).widget() for i in range(box.count())]


def test_row_widget_preserved_across_renders(panel):
    """Cached row widgets survive renders so hover/pressed state isn't lost."""
    panel._render_sessions([_session(1, "/proj"), _session(2, "/proj")])
    btn1 = panel._rows[1]
    panel._render_sessions([_session(1, "/proj")])
    assert panel._rows[1] is btn1


# --------------------------------------------------------------------------
# F2: card frame cache (_cards) — multi-view group QFrames are reused
# across renders so the per-snap rebuild churn (new QFrame + parsed CSS
# + Qt object polish) is paid only once per group_id, not per snap.
# --------------------------------------------------------------------------

def _multi_group(group_id: str, *sessions) -> "object":
    """Build a multi-view SessionGroup from raw Sessions for tests
    that exercise the _render_session_groups path directly.

    The shim _render_sessions only ever builds singleton groups, so
    we construct multi-view groups here explicitly. Each Session is
    wrapped in a SessionView via _degraded_view (no resolved metadata
    needed for cache-identity tests)."""
    from claude_island.core.snapshot import (
        SessionGroup as _SG,
        _degraded_view as _dv,
    )
    from dataclasses import replace as _replace
    from claude_island.core.capabilities import (
        Capability as _Cap,
        FocusGranularity as _FG,
    )
    views = tuple(
        _replace(
            _dv(s),
            adapter_id="test",
            focus_granularity=_FG.APP,
            capabilities=frozenset({_Cap.FOCUS}),
        )
        for s in sessions
    )
    return _SG(
        group_id=group_id, title_hint=None,
        adapter_id="test", views=views,
    )


def test_card_reused_across_renders_with_same_group_id(panel):
    """Same group_id × 2 renders must yield the same QFrame instance —
    proves we skip the new QFrame() + setStyleSheet() rebuild path."""
    g = _multi_group("g:1", _session(1, "/a"), _session(2, "/a"))
    panel._render_session_groups((g,))
    card_first = panel._cards["g:1"]

    g2 = _multi_group("g:1", _session(1, "/a"), _session(2, "/a"))
    panel._render_session_groups((g2,))
    assert panel._cards["g:1"] is card_first


def test_card_layout_repopulated_with_current_views(panel):
    """A cached card whose view set changed must end up with the
    right number of rows in its inner layout. Catches the bug where
    we forget to clear the layout before re-adding rows."""
    g = _multi_group("g:1", _session(1, "/a"), _session(2, "/a"))
    panel._render_session_groups((g,))
    card = panel._cards["g:1"]
    assert card.layout().count() == 2

    # Add a third session to the same group.
    g2 = _multi_group("g:1",
                      _session(1, "/a"), _session(2, "/a"), _session(3, "/a"))
    panel._render_session_groups((g2,))
    assert panel._cards["g:1"] is card
    assert card.layout().count() == 3


def test_card_evicted_when_group_id_disappears(panel):
    """A group_id absent from the latest snap must be GC'd from
    _cards. Mirrors _gc_rows discipline so the cache stays bounded."""
    g = _multi_group("g:1", _session(1, "/a"), _session(2, "/a"))
    panel._render_session_groups((g,))
    assert "g:1" in panel._cards

    panel._render_session_groups(())  # no groups at all
    assert "g:1" not in panel._cards


def test_single_view_group_does_not_create_card(panel):
    """Single-view groups skip the card wrapper — they return the row
    directly. _cards must stay empty for single-view-only renders."""
    panel._render_sessions([_session(1, "/a")])  # shim builds singletons
    assert panel._cards == {}


def test_card_evicted_when_group_collapses_to_single_view(panel):
    """Multi-view group at tick 1 → single-view at tick 2 (one pane
    closed). The card from tick 1 must be GC'd because the new
    rendering of that group_id (now 1 view) takes the row-only path
    and never registers in _cards anyway."""
    g = _multi_group("g:1", _session(1, "/a"), _session(2, "/a"))
    panel._render_session_groups((g,))
    assert "g:1" in panel._cards

    g_single = _multi_group("g:1", _session(1, "/a"))  # 1 view = single
    panel._render_session_groups((g_single,))
    # Single-view group goes through the row-only path; the old card
    # has no place in the new render and must be evicted.
    assert "g:1" not in panel._cards


def test_empty_groups_clears_card_cache(panel):
    """Placeholder path (no groups) wipes both _rows and _cards so
    the next non-empty render rebuilds from a clean slate."""
    g = _multi_group("g:1", _session(1, "/a"), _session(2, "/a"))
    panel._render_session_groups((g,))
    assert panel._cards
    assert panel._rows

    panel._render_session_groups(())
    assert panel._cards == {}
    assert panel._rows == {}


# --------------------------------------------------------------------------
# USAGE region: session card + period card (U1-U7)
# --------------------------------------------------------------------------

from claude_island.core.models import (
    ModelTotals as _ModelTotals,
    QuotaSnapshot as _QuotaSnapshot,
    SessionUsage as _SessionUsage
)


def _make_session_usage(
    *,
    start_offset_h: float | None = 1.0,   # hours ago
    end_offset_h: float | None = 4.0,     # hours from now (positive=future)
    quota: _QuotaSnapshot | None = None,
    by_model: tuple[_ModelTotals, ...] = (),
    total_cost: float = 2.67
):
    now = datetime.now(timezone.utc)
    start = (now - timedelta(hours=start_offset_h)) if start_offset_h is not None else None
    end = (now + timedelta(hours=end_offset_h)) if end_offset_h is not None else None
    return _SessionUsage(
        start_time=start,
        end_time=end,
        by_model=by_model,
        total_cost_usd=total_cost,
        quota=quota
    )


def _panel_with_quota(qtbot, *, quota=None, totals=None, by_model=None):
    """Build a panel wired with the new SPEND + QUOTA APIs.

    ``quota``    — drives the QUOTA card (bars / pct / reset).
    ``totals``   — drives the SPEND card amount + I/O lines.
    ``by_model`` — drives the SPEND card per-model breakdown.

    Use this helper instead of ``_panel_with_session`` for any test
    that exercises the post-A2 layout. The legacy helper passes
    ``get_session_usage`` which the new cards no longer read."""
    capsule = QWidget()
    capsule.show()
    p = ExpandedWindow(
        capsule=capsule,
        controller=IslandController(),
        get_usage_totals=lambda period: (totals or UsageTotals(period=period)),
        get_totals_by_model=(lambda _period: by_model) if by_model is not None else None,
        get_quota_snapshot=(lambda: quota) if quota is not None else None
    )
    qtbot.addWidget(p)
    qtbot.addWidget(capsule)
    return p


def _make_quota(*, five_pct: float = 53.0, is_stale: bool = False):
    now = datetime.now(timezone.utc)
    return _QuotaSnapshot(
        five_hour_pct=five_pct,
        five_hour_resets_at=now + timedelta(hours=3, minutes=25),
        seven_day_pct=17.0,
        seven_day_resets_at=now + timedelta(days=4),
        fetched_at=now if not is_stale else now - timedelta(hours=1),
        is_stale=is_stale
    )


def test_quota_card_with_quota_shows_bar_and_pct(qtbot):
    """U1: quota snapshot present → combined inline status visible,
    rich text contains both 5h and Weekly percentages.

    The 5h bar moved to the top summary in P1.1; both 5h and Weekly
    bars in the QUOTA card got collapsed into a single rich-text
    line ("5h N% · M m │ Weekly N% · M m") per user feedback that
    QUOTA needed to feel as light as SPEND now does."""
    p = _panel_with_quota(qtbot, quota=_make_quota(five_pct=53.0))
    p._render_cards()

    # _make_quota fixes seven_day_pct = 17.0; five_pct = 53.0
    assert not p._quota_inline.isHidden()
    text = p._quota_inline.text()
    assert "53%" in text
    assert "17%" in text
    assert "5h" in text
    assert "Weekly" in text


def test_quota_card_without_quota_hides_bars(qtbot):
    """U2: no quota snapshot → combined inline status hidden."""
    p = _panel_with_quota(qtbot, quota=None,
                          totals=UsageTotals(period="today", input_tokens=10))
    p._render_cards()

    assert p._quota_inline.isHidden()


def test_quota_card_stale_marks_warning(qtbot):
    """U3: quota.is_stale=True → combined inline rich text contains
    ⚠ marker (one per side: 5h and Weekly each get their own marker
    because each half is rendered with its own threshold colour)."""
    p = _panel_with_quota(qtbot, quota=_make_quota(five_pct=20.0, is_stale=True))
    p._render_cards()
    assert "⚠" in p._quota_inline.text()


def test_quota_card_no_quota_dot_gray(qtbot):
    """U4: no remote quota → live-dot greys out (no signal we can
    derive freshness from). Replaces the old "expired session" test
    that relied on SessionUsage.end_time."""
    p = _panel_with_quota(qtbot, quota=None,
                          totals=UsageTotals(period="today"))
    p._render_cards()
    assert "#52525b" in p._quota_dot.styleSheet()


def test_spend_card_empty_totals_shows_zero(qtbot):
    """U5: empty totals → spend amount shows $0 (or fallback rendering),
    combined quota inline stays hidden because quota is None."""
    p = _panel_with_quota(qtbot, quota=None,
                          totals=UsageTotals(period="today"))
    p._render_cards()
    # _fmt_money(0) returns "$0.001" or "$0.00" (sub-cent path); either way "$" is present
    assert "$" in p._spend_amount.text()
    assert p._quota_inline.isHidden()


def test_period_toggle_updates_spend_card(qtbot):
    """U6: switching period (Today → Weekly) calls get_usage_totals with
    the new key and the spend amount updates."""
    capsule = QWidget()
    capsule.show()

    calls: list[str] = []

    def fake_totals(period):
        calls.append(period)
        # Different cost per period so the amount label changes.
        return UsageTotals(
            period=period,
            input_tokens=1000 if period == "5h" else 9999,
            output_tokens=2000,
            input_cost=1.0 if period == "5h" else 9.99
        )

    p = ExpandedWindow(
        capsule=capsule,
        controller=IslandController(),
        get_usage_totals=fake_totals
    )
    qtbot.addWidget(p)
    qtbot.addWidget(capsule)
    p._render_cards()

    # Default period is "5h" (most actionable window); first refresh
    # populates that. Switching to weekly should re-fetch + re-render.
    initial_amount = p._spend_amount.text()
    p._on_period("weekly")
    weekly_amount = p._spend_amount.text()

    assert initial_amount != weekly_amount
    assert "5h" in calls
    assert "weekly" in calls


def test_period_selector_includes_5h(qtbot):
    """The SPEND selector exposes 5h alongside Today/Last 7/Last 30 so
    the most-actionable rate-limit window isn't hidden in a separate
    card. ``daily`` was dropped in P1.2 (semantic overlap with
    ``today``) and the strip became a single dropdown — keys live on
    the combo's items rather than a button dict."""
    p = _panel_with_quota(qtbot, totals=UsageTotals(period="today"))
    keys = {key for _label, key in p._period_combo_items}
    assert keys == {"5h", "today", "weekly", "monthly"}


@pytest.mark.parametrize("pct,expected_color", [
    (0.0,   "#4ade80"),   # green tier — bottom of range
    (10.0,  "#4ade80"),
    (59.0,  "#4ade80"),   # still green just below threshold
    (60.0,  "#facc15"),   # yellow tier — at threshold
    (75.0,  "#facc15"),
    (84.0,  "#facc15"),   # still yellow just below threshold
    (85.0,  "#ef4444"),   # red tier — at threshold
    (99.0,  "#ef4444"),
    (100.0, "#ef4444"),
])
def test_quota_bar_color_thresholds(qtbot, pct, expected_color):
    """U8: threshold colour escalates green → yellow → red at 60% / 85%.
    Verified on the combined inline rich-text label — each half (5h
    and Weekly) carries its own colour span, so the right colour for
    the seven_day_pct must appear in the rich text.

    Driven via seven_day_pct."""
    p = _panel_with_quota(qtbot, quota=_make_quota(five_pct=10.0))
    snap = p._get_quota_snapshot()
    from dataclasses import replace
    p._get_quota_snapshot = lambda: replace(snap, seven_day_pct=pct)
    p._render_cards()
    # Rich text embeds the colour as a CSS span; substring check on
    # the rendered HTML is enough to verify the right hex landed.
    assert expected_color in p._quota_inline.text()


@pytest.mark.parametrize("pct", [0.0, 59.0, 59.6, 60.0, 75.0, 84.0, 84.6, 85.0, 99.0, 100.0])
def test_summary_and_quota_card_use_same_color_for_same_pct(qtbot, pct):
    """Regression for the c685bb7 / A-001 bug: same five_hour_pct
    must produce the same threshold colour everywhere it's rendered.

    Pre-fix, summary used ``int(pct)`` while QUOTA used
    ``int(round(pct))`` — at 84.6 the summary path bucketed to 84
    (yellow) while QUOTA bucketed to 85 (red). User saw two
    different colours for one snapshot.

    Boundary values 59.6 / 84.6 are deliberately included — those
    are the inputs that hit the truncate-vs-round disagreement."""
    snap = _make_quota(five_pct=pct)
    p = _panel_with_quota(qtbot, quota=snap)
    p._render_cards()
    # The summary card stores the chunk colour in the bar's
    # stylesheet AND in the caption label's stylesheet.
    summary_bar_css = p._summary_quota_bar.styleSheet()
    summary_caption_css = p._summary_caption.styleSheet()
    # The QUOTA card surfaces it through the rich-text inline label.
    quota_inline_text = p._quota_inline.text()
    # All three must contain the same hex.
    from claude_island.ui.expanded_window import _quota_color
    expected = _quota_color(int(round(pct)), stale=False)
    assert expected in summary_bar_css, (
        f"summary bar at pct={pct} expected {expected}, got {summary_bar_css}"
    )
    assert expected in summary_caption_css, (
        f"summary caption at pct={pct} expected {expected}, got {summary_caption_css}"
    )
    assert expected in quota_inline_text, (
        f"quota inline at pct={pct} expected {expected}, got {quota_inline_text}"
    )


def test_quota_bar_stale_overrides_red(qtbot):
    """U9: stale data wins over the percent-based colour — we want
    "I don't trust this" to surface before "you're at the limit",
    so a stale 95% reads gray, not red. Verified on the combined
    inline rich-text label."""
    snap = _make_quota(five_pct=10.0, is_stale=True)
    from dataclasses import replace
    snap = replace(snap, seven_day_pct=95.0)
    p = _panel_with_quota(qtbot, quota=snap)
    p._render_cards()
    # Stale grey should appear; danger red should not, because the
    # stale handler short-circuits the threshold colour pick.
    assert "#6b7280" in p._quota_inline.text()
    assert "#ef4444" not in p._quota_inline.text()


def _make_full_details(s, **overrides):
    """Helper: SessionDetails with sensible defaults; tests override
    only what they care about."""
    from claude_island.core.models import (
        ModelTotals as _MT,
        SessionDetails as _SD
    )
    base = dict(
        session=s,
        name="cc-learning",
        ai_title="Refactor scanner to async iter",
        git_branch="feat-async",
        last_prompt="please refactor this scanner",
        started_at=datetime.now(timezone.utc) - timedelta(hours=2),
        status="busy",
        cc_version="2.1.123",
        cost_usd=2.67,
        turn_count=42,
        sidechain_count=3,
        per_model=(
            _MT(model="claude-sonnet-4-6", input_tokens=1000,
                output_tokens=20000, cache_creation_tokens=300000,
                cache_read_tokens=5000000, cost_usd=2.40),
            _MT(model="claude-haiku-4-5", input_tokens=100,
                output_tokens=200, cache_creation_tokens=1000,
                cache_read_tokens=8000, cost_usd=0.27)
        ),
        effective_uuid="abc12345-6789-0000-0000-000000000000"
    )
    base.update(overrides)
    return _SD(**base)


def test_row_meta_shows_cost_when_details_available(qtbot):
    """The user wanted the right side of the row to show *cumulative
    session cost* instead of an age string. Title should also pick up
    the human ``name`` from the composer."""
    from claude_island.ui.expanded_window import ExpandedWindow
    from PySide6.QtWidgets import QLabel as _QL

    s = _session(1, "/some/path/foo")
    details = _make_full_details(s)

    capsule = QWidget()
    capsule.show()
    panel = ExpandedWindow(
        capsule=capsule,
        controller=IslandController(),
        get_usage_totals=lambda p: UsageTotals(period=p),
        get_session_details=lambda _s: details
    )
    qtbot.addWidget(panel)
    qtbot.addWidget(capsule)
    panel._render_sessions([s])

    btn = panel._rows[1]
    assert btn.findChild(_QL, "name_label").text() == "cc-learning"
    assert btn.findChild(_QL, "meta_label").text() == "$2.67"
    # The hover tooltip is now intentionally empty — replaced by
    # right-click popup. Avoids two competing surfaces for the same info.
    assert btn.toolTip() == ""


def test_row_meta_renders_dash_when_no_details(qtbot):
    """Composer unwired → meta shows ``—`` (not an age fallback)."""
    from PySide6.QtWidgets import QLabel as _QL
    panel = _panel_with_session(qtbot, lambda: None)
    panel._render_sessions([_session(7, "/proj/foo")])
    btn = panel._rows[7]
    assert btn.findChild(_QL, "meta_label").text() == "—"


def test_row_has_custom_context_menu_policy(qtbot):
    """Right-click is wired via Qt.CustomContextMenu so we own the
    event. Without this, Qt would either show its built-in
    text-context-menu or do nothing."""
    panel = _panel_with_session(qtbot, lambda: None)
    panel._render_sessions([_session(1, "/a")])
    btn = panel._rows[1]
    from PySide6.QtCore import Qt as _Qt
    assert btn.contextMenuPolicy() == _Qt.ContextMenuPolicy.CustomContextMenu


def test_detail_popup_renders_all_sections(qtbot):
    """Build a popup directly with full details and verify every
    section's text is present. Doesn't simulate the right-click event;
    that path is exercised by _show_detail_popup which we test
    separately."""
    from claude_island.ui.expanded_window import SessionDetailPopup
    s = _session(1, "/some/path/foo")
    details = _make_full_details(s)

    popup = SessionDetailPopup(details, s)
    qtbot.addWidget(popup)

    # Walk all child labels and concatenate their text — easiest way
    # to assert "this string appears somewhere in the popup".
    from PySide6.QtWidgets import QLabel as _QL
    text = " | ".join(
        lbl.text() for lbl in popup.findChildren(_QL) if lbl.text()
    )
    # Header card
    assert "cc-learning" in text
    assert "Refactor scanner to async iter" in text
    # Status pill removed by design — idle/waiting/busy carry low signal
    # for popup users; no longer rendered.
    # Meta card
    assert "abc12345" in text                    # short uuid
    assert "feat-async" in text
    assert "2.1.123" in text                     # cc version (header subtitle)
    assert "foo" in text                         # cwd basename
    # Tokens card
    assert "TOKENS" in text
    assert "Sonnet" in text and "Haiku" in text
    assert "$2.40" in text                       # per-model cost
    assert "42 turns" in text
    assert "3 subagent" in text
    # Prompt card — collapsed view elides to popup-inner-width to
    # keep popup at _PANEL_W (was: full string visible until elide
    # was added in the dense-inspector pass).
    assert "LAST PROMPT" in text
    assert "please refactor" in text


def test_detail_popup_skips_prompt_card_when_empty(qtbot):
    """No last_prompt → don't render an empty 'LAST PROMPT' card."""
    from claude_island.ui.expanded_window import SessionDetailPopup
    s = _session(1, "/a")
    details = _make_full_details(s, last_prompt=None)
    popup = SessionDetailPopup(details, s)
    qtbot.addWidget(popup)
    from PySide6.QtWidgets import QLabel as _QL
    text = " | ".join(l.text() for l in popup.findChildren(_QL) if l.text())
    assert "LAST PROMPT" not in text


def test_detail_popup_uses_main_panel_style_tokens(qtbot):
    """Visual-consistency safety net. The dense-inspector design uses
    flat sections separated by `_STYLE_SEP` dividers (no sub-cards),
    so check that the standard separator colour is in use."""
    from claude_island.ui.expanded_window import SessionDetailPopup, _STYLE_SEP
    s = _session(1, "/a")
    popup = SessionDetailPopup(_make_full_details(s), s)
    qtbot.addWidget(popup)
    from PySide6.QtWidgets import QFrame as _QF
    frames = popup.findChildren(_QF)
    # At least one section divider should carry the shared sep style.
    assert any(_STYLE_SEP in (f.styleSheet() or "") for f in frames)


def test_show_detail_popup_constructs_and_holds_reference(qtbot):
    """End-to-end-ish: ExpandedWindow._show_detail_popup constructs a
    popup from the composer's details and keeps a reference so Qt
    doesn't immediately GC it."""
    from claude_island.ui.expanded_window import (
        ExpandedWindow, SessionDetailPopup
    )
    s = _session(1, "/a")
    details = _make_full_details(s)

    capsule = QWidget()
    capsule.show()
    panel = ExpandedWindow(
        capsule=capsule, controller=IslandController(),
        get_usage_totals=lambda p: UsageTotals(period=p),
        get_session_details=lambda _s: details
    )
    qtbot.addWidget(panel)
    qtbot.addWidget(capsule)
    panel._render_sessions([s])

    btn = panel._rows[1]
    # Drive the slot directly (bypassing Qt's mouse-event plumbing).
    from PySide6.QtCore import QPoint
    panel._show_detail_popup(btn, QPoint(5, 5))

    assert isinstance(panel._active_detail_popup, SessionDetailPopup)


def test_detail_popup_uuid_short_display_full_copy(qtbot):
    """ID row shows the first-8-char prefix for scanability; clicking it
    copies the *full* 36-char UUID to the clipboard so ``claude --resume``
    paste-flow still works. Click also shows 'Copied' feedback."""
    from claude_island.ui.expanded_window import SessionDetailPopup, _CopyableIdLabel
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QLabel, QApplication
    s = _session(1, "/a")
    full_uuid = "abc12345-6789-0000-0000-000000000000"
    details = _make_full_details(s, effective_uuid=full_uuid)

    popup = SessionDetailPopup(details, s)
    qtbot.addWidget(popup)
    popup.show()   # required before event delivery

    copyable = popup.findChild(_CopyableIdLabel)
    assert copyable is not None, "ID row should contain a _CopyableIdLabel"
    # The visible label is the 8-char prefix, NOT the full UUID.
    uuid_label = copyable.findChild(QLabel)
    assert uuid_label is not None
    assert uuid_label.text() == "abc12345"
    assert full_uuid not in uuid_label.text()

    # Click → clipboard contains the FULL uuid, "Copied" feedback shows.
    from PySide6.QtGui import QMouseEvent
    from PySide6.QtCore import QPointF
    copyable.mousePressEvent(QMouseEvent(
        QMouseEvent.Type.MouseButtonPress,
        QPointF(), QPointF(), QPointF(),
        Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier
    ))
    qtbot.wait(50)
    assert QApplication.clipboard().text() == full_uuid
    copied_label = next(
        (lbl for lbl in copyable.findChildren(QLabel) if lbl.text() == "Copied"), None
    )
    assert copied_label is not None
    assert copied_label.isVisible()


def test_spend_card_model_breakdown_shows_top_models(qtbot):
    """U7: get_totals_by_model populated → proportional bar rows shown for top models."""
    by_model = (
        _ModelTotals(model="claude-sonnet-4-5", input_tokens=0, output_tokens=0,
                     cache_creation_tokens=0, cache_read_tokens=0, cost_usd=2.54),
        _ModelTotals(model="claude-haiku-4-5", input_tokens=0, output_tokens=0,
                     cache_creation_tokens=0, cache_read_tokens=0, cost_usd=0.13)
    )
    # Provide non-zero totals so the bar container is shown (cost_usd > 0 gate)
    # cost_usd is derived from input_cost + output_cost + cache_creation_cost + cache_read_cost
    totals = UsageTotals(
        period="5h",
        input_tokens=0, output_tokens=0,
        cache_creation_tokens=0, cache_read_tokens=0,
        input_cost=1.0, output_cost=1.0,
        cache_creation_cost=0.4, cache_read_cost=0.27
    )
    p = _panel_with_quota(qtbot, totals=totals, by_model=by_model)
    p._render_cards()

    # Spend bar container should be shown (cost > 0 and by_model wired).
    # Use isHidden() not isVisible() — isVisible() returns False when the
    # top-level window is hidden, but isHidden() correctly reflects whether
    # show() was called on this widget regardless of parent visibility.
    assert not p._spend_bar_container.isHidden()
    # First row should show Sonnet with its cost
    first_row = p._spend_bar_rows[0]
    assert not first_row.isHidden()
    assert first_row._spend_name.text() == "Sonnet"
    assert "$" in first_row._spend_cost.text()


# ============================================================================
# Multi-provider tabs (5h card pill switcher)
# ============================================================================

def _build_panel_with_tabs(
    qtbot,
    *,
    available,
    selected="anthropic",
    on_provider_selected=None,
    get_session_usage=None
):
    capsule = QWidget()
    capsule.show()
    controller = IslandController()
    p = ExpandedWindow(
        capsule=capsule,
        controller=controller,
        get_usage_totals=lambda period: UsageTotals(period=period),
        get_session_usage=get_session_usage,
        available_providers=available,
        selected_provider=selected,
        on_provider_selected=on_provider_selected
    )
    qtbot.addWidget(p)
    qtbot.addWidget(capsule)
    return p


def test_single_provider_renders_one_pill(qtbot):
    """Single-provider state still renders the one pill (e.g. just
    [Anthropic]) — earlier the single-provider branch dropped to a
    static "ANTHROPIC QUOTA" text label, which read as a section
    header rather than a current-selection indicator and confused
    users. With the always-pill design the user sees a uniform
    selected-tab affordance regardless of provider count."""
    p = _build_panel_with_tabs(qtbot, available=["anthropic"])
    assert set(p._provider_btns.keys()) == {"anthropic"}
    assert p._provider_btns["anthropic"].isChecked() is True


def test_no_tabs_rendered_when_zero_providers(qtbot):
    """Empty list / None → no tabs (legacy callers, tests)."""
    p = _build_panel_with_tabs(qtbot, available=[], selected=None)
    assert p._provider_btns == {}


def test_tabs_rendered_for_two_providers(qtbot):
    p = _build_panel_with_tabs(qtbot, available=["anthropic", "minimax"])
    assert set(p._provider_btns.keys()) == {"anthropic", "minimax"}
    assert p._provider_btns["anthropic"].isChecked() is True
    assert p._provider_btns["minimax"].isChecked() is False


def test_clicking_tab_updates_state_and_notifies(qtbot):
    """Click → selected_provider changes, callback fires once with the
    new name, refresh_usage_bar runs, and only the clicked pill is
    checked."""
    fired: list[str] = []
    p = _build_panel_with_tabs(
        qtbot,
        available=["anthropic", "minimax"],
        on_provider_selected=fired.append
    )
    p._provider_btns["minimax"].click()

    assert fired == ["minimax"]
    assert p.selected_provider_name() == "minimax"
    assert p._provider_btns["anthropic"].isChecked() is False
    assert p._provider_btns["minimax"].isChecked() is True


def test_reclicking_active_tab_is_noop(qtbot):
    fired: list[str] = []
    p = _build_panel_with_tabs(
        qtbot,
        available=["anthropic", "minimax"],
        selected="anthropic",
        on_provider_selected=fired.append
    )
    p._provider_btns["anthropic"].click()
    assert fired == []
    assert p.selected_provider_name() == "anthropic"


def test_tab_callback_failure_does_not_crash_ui(qtbot):
    """A persistence failure (disk full, permission error) must not
    take down the UI thread."""
    def boom(_: str) -> None:
        raise OSError("disk full")

    p = _build_panel_with_tabs(
        qtbot,
        available=["anthropic", "minimax"],
        on_provider_selected=boom
    )
    p._provider_btns["minimax"].click()  # must not raise
    # Selection still flips in-process even though persistence failed.
    assert p.selected_provider_name() == "minimax"


# ============================================================================
# Detail popup: REPAIR card (strip thinking blocks for cross-provider rescue)
# ============================================================================

def test_repair_icon_renders_when_session_has_uuid(qtbot):
    """The repair ⟲ icon appears in the popup header whenever the
    session has an effective uuid (i.e., a transcript on disk to
    operate on). The tooltip carries the full explanation."""
    from claude_island.ui.expanded_window import SessionDetailPopup
    s = _session(1, "/some/path/foo")
    details = _make_full_details(s)
    popup = SessionDetailPopup(details, s)
    qtbot.addWidget(popup)

    assert popup._repair_icon is not None
    # Now an icon, not a text-link button.
    assert popup._repair_icon.text() == "⟲"
    assert "thinking" in popup._repair_icon.toolTip().lower()
    assert "backup" in popup._repair_icon.toolTip().lower()


def test_repair_icon_hidden_when_no_uuid(qtbot):
    """Synthetic / orphan sessions have no transcript file to repair —
    don't render the ⚙ icon at all (it would be useless)."""
    from claude_island.ui.expanded_window import SessionDetailPopup
    from PySide6.QtWidgets import QLabel as _QL
    s = _session(1, "/p")
    details = _make_full_details(s, effective_uuid="")
    popup = SessionDetailPopup(details, s)
    qtbot.addWidget(popup)

    # No repair icon when there's no UUID
    assert popup._repair_icon is None
    # Status label is hidden (created but kept for layout consistency)
    assert not popup._repair_status.isVisible()


def test_repair_button_calls_callback_and_disables_self_on_success(qtbot):
    """Click → on_strip_thinking callback fires → status reads success,
    button becomes disabled with text "Done". The popup itself does
    no JSONL/file work — that's the callback's job (which routes via
    the dispatcher to LocalAppBackend.reset_thinking)."""
    from claude_island.ui.expanded_window import SessionDetailPopup

    s = _session(1, "C:/X")
    full_uuid = "abc12345-6789-0000-0000-000000000000"
    details = _make_full_details(s, effective_uuid=full_uuid)

    callback_calls: list[bool] = []
    def on_strip():
        callback_calls.append(True)
        return True   # success

    popup = SessionDetailPopup(details, s, on_strip_thinking=on_strip)
    qtbot.addWidget(popup)
    popup._on_strip_thinking()

    assert callback_calls == [True]
    assert "Stripped" in popup._repair_status.text()
    assert popup._repair_icon.isEnabled() is False
    assert popup._repair_icon.text() == "Done"


def test_repair_button_shows_error_on_callback_failure(qtbot):
    """Callback returns False (e.g. transcript not found, session has
    no uuid) → popup shows the failure message and KEEPS the button
    enabled so the user can retry after fixing the underlying issue."""
    from claude_island.ui.expanded_window import SessionDetailPopup

    s = _session(1, "C:/X")
    details = _make_full_details(s, effective_uuid="abc12345-6789-0000-0000-000000000000")

    popup = SessionDetailPopup(details, s, on_strip_thinking=lambda: False)
    qtbot.addWidget(popup)
    popup._on_strip_thinking()

    assert "Failed" in popup._repair_status.text()
    assert popup._repair_icon.isEnabled() is True


def test_repair_button_handles_unwired_callback(qtbot):
    """When the popup is constructed without on_strip_thinking (e.g.
    legacy test fixture, isolated unit test), clicking the button
    surfaces a clear "not wired" message instead of silently no-oping."""
    from claude_island.ui.expanded_window import SessionDetailPopup

    s = _session(1, "C:/X")
    details = _make_full_details(s, effective_uuid="abc12345-6789-0000-0000-000000000000")
    popup = SessionDetailPopup(details, s)  # on_strip_thinking=None
    qtbot.addWidget(popup)
    popup._on_strip_thinking()

    assert "not wired" in popup._repair_status.text()


# ── Transcript row ────────────────────────────────────────────────────

def test_transcript_row_shows_full_jsonl_path(qtbot):
    """The Transcript row in the meta section displays the full
    ``~/.claude/projects/<hash>/<uuid>.jsonl`` path computed from the
    session's cwd + effective uuid. Pinned because the path is what the
    user clicks on; eliding or shortening it would hide which file
    they're about to open."""
    from claude_island.ui.expanded_window import (
        SessionDetailPopup, _transcript_path_for_display,
    )
    from PySide6.QtWidgets import QLabel as _QL

    s = _session(1, "D:/proj/foo")
    uuid = "abc12345-6789-0000-0000-000000000000"
    details = _make_full_details(s, effective_uuid=uuid)
    popup = SessionDetailPopup(details, s)
    qtbot.addWidget(popup)

    expected = str(_transcript_path_for_display(s.project_path, uuid))
    # Find the QLabel holding the transcript path. Filter to labels
    # that look like .jsonl paths so the assertion isn't fooled by an
    # unrelated label that happens to match a substring.
    matches = [
        lbl for lbl in popup.findChildren(_QL)
        if lbl.text() == expected
    ]
    assert len(matches) == 1, (
        f"expected exactly one label showing the transcript path, "
        f"got {[lbl.text() for lbl in matches]}"
    )


def test_transcript_row_suppressed_when_uuid_missing(qtbot):
    """No effective uuid → no Transcript row at all. The displayed
    path would be ``<project>/.jsonl`` (an obviously broken file) and
    clicking it would always fail; suppressing the row avoids leading
    the user to a dead action."""
    from claude_island.ui.expanded_window import SessionDetailPopup
    from PySide6.QtWidgets import QLabel as _QL

    s = _session(1, "D:/proj/foo")
    details = _make_full_details(s, effective_uuid="")
    popup = SessionDetailPopup(details, s)
    qtbot.addWidget(popup)

    # No label should carry the "Transcript" key text.
    keys = [lbl.text() for lbl in popup.findChildren(_QL) if lbl.text() == "Transcript"]
    assert keys == []


def test_transcript_open_calls_callback_on_success(qtbot):
    """Click → on_open_transcript fires → status shows success."""
    from claude_island.ui.expanded_window import SessionDetailPopup

    s = _session(1, "D:/proj/foo")
    details = _make_full_details(s, effective_uuid="abc12345-6789-0000-0000-000000000000")

    calls: list[bool] = []
    def on_open():
        calls.append(True)
        return True

    popup = SessionDetailPopup(details, s, on_open_transcript=on_open)
    qtbot.addWidget(popup)
    popup._on_open_transcript()

    assert calls == [True]
    assert "Opened" in popup._repair_status.text()


def test_transcript_open_shows_error_on_callback_failure(qtbot):
    """Callback returns False (file missing, OS launcher failed) →
    popup surfaces a failure message rather than silent no-op."""
    from claude_island.ui.expanded_window import SessionDetailPopup

    s = _session(1, "D:/proj/foo")
    details = _make_full_details(s, effective_uuid="abc12345-6789-0000-0000-000000000000")
    popup = SessionDetailPopup(details, s, on_open_transcript=lambda: False)
    qtbot.addWidget(popup)
    popup._on_open_transcript()

    assert "Failed" in popup._repair_status.text()


def test_transcript_open_handles_unwired_callback(qtbot):
    """No on_open_transcript wired → click surfaces "not wired" instead
    of silently no-oping."""
    from claude_island.ui.expanded_window import SessionDetailPopup

    s = _session(1, "D:/proj/foo")
    details = _make_full_details(s, effective_uuid="abc12345-6789-0000-0000-000000000000")
    popup = SessionDetailPopup(details, s)  # on_open_transcript=None
    qtbot.addWidget(popup)
    popup._on_open_transcript()

    assert "not wired" in popup._repair_status.text()


# ============================================================================
# Detail popup: Dense Inspector redesign — aggregation, hiding rules, footer
# ============================================================================

def test_aggregate_per_model_dedupes_by_display_label():
    """Two raw model ids that share a display label (both 'Opus') get
    merged into one row with summed cost/tokens, sorted by cost desc."""
    from claude_island.ui.expanded_window import _aggregate_per_model_for_display
    from claude_island.core.models import ModelTotals as _MT
    rows = _aggregate_per_model_for_display((
        _MT(model="claude-opus-4-5", input_tokens=100, output_tokens=200,
            cache_creation_tokens=10, cache_read_tokens=20, cost_usd=10.0),
        _MT(model="claude-opus-4-6", input_tokens=200, output_tokens=300,
            cache_creation_tokens=30, cache_read_tokens=40, cost_usd=15.0),
        _MT(model="claude-sonnet-4-6", input_tokens=50, output_tokens=60,
            cache_creation_tokens=5, cache_read_tokens=6, cost_usd=2.0)
    ))
    # Two Opus + one Sonnet input → one Opus + one Sonnet output.
    labels = [r.label for r in rows]
    assert labels == ["Opus", "Sonnet"]   # sorted by cost desc
    opus = rows[0]
    assert opus.cost_usd == 25.0
    assert opus.input_tokens == 300
    assert opus.output_tokens == 500
    assert opus.cache_creation_tokens == 40
    assert opus.cache_read_tokens == 60


def test_aggregate_per_model_drops_zero_cost_zero_token_rows():
    """``<synthetic>`` and similar all-zero placeholders disappear
    entirely — they're noise, not data."""
    from claude_island.ui.expanded_window import _aggregate_per_model_for_display
    from claude_island.core.models import ModelTotals as _MT
    rows = _aggregate_per_model_for_display((
        _MT(model="<synthetic>", input_tokens=0, output_tokens=0,
            cache_creation_tokens=0, cache_read_tokens=0, cost_usd=0.0),
        _MT(model="claude-haiku-4-5", input_tokens=10, output_tokens=20,
            cache_creation_tokens=0, cache_read_tokens=0, cost_usd=0.5)
    ))
    labels = [r.label for r in rows]
    assert "<synthetic>" not in labels
    assert labels == ["Haiku"]


def test_detail_popup_dedupes_models_in_render(qtbot):
    """End-to-end check: feeding two Opus rows produces ONE 'Opus'
    label in the rendered popup (visual de-dup, not just data-level)."""
    from claude_island.ui.expanded_window import SessionDetailPopup
    from claude_island.core.models import ModelTotals as _MT
    from PySide6.QtWidgets import QLabel as _QL
    s = _session(1, "/x")
    details = _make_full_details(s, per_model=(
        _MT(model="claude-opus-4-5", input_tokens=100, output_tokens=200,
            cache_creation_tokens=10, cache_read_tokens=20, cost_usd=65.0),
        _MT(model="claude-opus-4-6", input_tokens=200, output_tokens=300,
            cache_creation_tokens=30, cache_read_tokens=40, cost_usd=64.0)
    ))
    popup = SessionDetailPopup(details, s)
    qtbot.addWidget(popup)
    # Count "Opus" labels — must be exactly 1 (the merged row).
    opus_labels = [
        l for l in popup.findChildren(_QL) if l.text() == "Opus"
    ]
    assert len(opus_labels) == 1


def test_detail_popup_hides_synthetic_zero_cost_row(qtbot):
    """``<synthetic>`` placeholder gets dropped from the rendered popup
    so the user doesn't see meaningless ``<synthetic>  $0.000`` noise."""
    from claude_island.ui.expanded_window import SessionDetailPopup
    from claude_island.core.models import ModelTotals as _MT
    from PySide6.QtWidgets import QLabel as _QL
    s = _session(1, "/x")
    details = _make_full_details(s, per_model=(
        _MT(model="<synthetic>", input_tokens=0, output_tokens=0,
            cache_creation_tokens=0, cache_read_tokens=0, cost_usd=0.0),
        _MT(model="claude-sonnet-4-6", input_tokens=100, output_tokens=200,
            cache_creation_tokens=0, cache_read_tokens=0, cost_usd=1.0)
    ))
    popup = SessionDetailPopup(details, s)
    qtbot.addWidget(popup)
    text = " | ".join(l.text() for l in popup.findChildren(_QL) if l.text())
    assert "<synthetic>" not in text
    assert "Sonnet" in text  # the real row still rendered


def test_detail_popup_hides_branch_when_head(qtbot):
    """``HEAD`` is git's detached-state placeholder, not a useful
    branch name — suppress the row to avoid visual noise."""
    from claude_island.ui.expanded_window import SessionDetailPopup
    from PySide6.QtWidgets import QLabel as _QL
    s = _session(1, "/x")
    details = _make_full_details(s, git_branch="HEAD")
    popup = SessionDetailPopup(details, s)
    qtbot.addWidget(popup)
    keys = [l.text() for l in popup.findChildren(_QL) if l.text() == "Branch"]
    assert keys == []


def test_detail_popup_prompt_collapsed_by_default(qtbot):
    """A long multi-line prompt renders as a single truncated preview
    by default; the toggle expands it to the full body."""
    from claude_island.ui.expanded_window import SessionDetailPopup
    s = _session(1, "/x")
    long_prompt = "first line of prompt\n" + ("x" * 200)
    details = _make_full_details(s, last_prompt=long_prompt)
    popup = SessionDetailPopup(details, s)
    qtbot.addWidget(popup)

    # Default: collapsed → first line + "…" so the user knows more
    # content exists beneath. The popup delegates to a shared
    # LastPromptSection widget — its internals are addressed via
    # popup._prompt_section.
    section = popup._prompt_section
    assert section is not None
    assert section.is_expanded() is False
    assert section._body.text() == "first line of prompt…"
    assert section._toggle.text() == "[展开]"

    # Toggle → expanded: collapsed QLabel hides, expanded QTextEdit
    # appears with the full body. Toggle text flips to "[收起]".
    section._on_toggle()
    assert section.is_expanded() is True
    assert section._full_view is not None
    assert section._body.isHidden()
    assert not section._full_view.isHidden()
    full_text = section._full_view.toPlainText()
    assert "first line of prompt" in full_text
    assert "x" * 100 in full_text  # original tail present
    assert section._toggle.text() == "[收起]"

    # Toggle back → collapsed: full view hidden, label visible again.
    section._on_toggle()
    assert section.is_expanded() is False
    assert section._full_view.isHidden()
    assert not section._body.isHidden()
    assert section._toggle.text() == "[展开]"


def test_detail_popup_short_prompt_no_toggle(qtbot):
    """Short single-line prompts that fit fully render without the
    expand toggle — collapsing them would be confusing.

    Toggle is always *created* (hidden by default) and shown only after
    the post-show font-metrics check confirms elision actually happens.
    For "quick question" no elision → toggle stays hidden."""
    from claude_island.ui.expanded_window import SessionDetailPopup
    s = _session(1, "/x")
    details = _make_full_details(s, last_prompt="quick question")
    popup = SessionDetailPopup(details, s)
    qtbot.addWidget(popup)
    popup.show()
    section = popup._prompt_section
    assert section is not None
    assert not section._toggle.isVisible()
    assert section._body.text() == "quick question"


def test_detail_popup_cjk_long_prompt_shows_toggle(qtbot):
    """CJK glyphs render at ~14px each in 12px-stylesheet labels —
    30+ chars exceed the 288px collapsed budget. Earlier `len > 80`
    heuristic missed this; the post-show metrics check catches it."""
    from claude_island.ui.expanded_window import SessionDetailPopup
    s = _session(1, "/x")
    # User's real prompt from screenshot — 53 chars but ~439px wide.
    long_cjk = "我已经merge了 checkout到master pull最新代码，然后把改动同步到~/.claude下"
    details = _make_full_details(s, last_prompt=long_cjk)
    popup = SessionDetailPopup(details, s)
    qtbot.addWidget(popup)
    popup.show()
    section = popup._prompt_section
    assert section is not None
    assert section._toggle.isVisible(), (
        "toggle should be visible because the CJK prompt is wider "
        "than the popup-inner-width and was elided"
    )
    # The displayed text should include the elide marker.
    assert "…" in section._body.text()


def test_detail_popup_header_action_icons(qtbot):
    """Header right side carries three icon buttons:
        ⧉ Copy ID  ↗ Open folder  ⟲ Reset thinking blocks
    The destructive reset uses a separate style (amber hover) but
    sits next to the safe actions for visual consistency."""
    from claude_island.ui.expanded_window import SessionDetailPopup
    s = _session(1, "/x")
    details = _make_full_details(s)
    popup = SessionDetailPopup(details, s)
    qtbot.addWidget(popup)
    assert popup._copy_id_btn is not None
    assert popup._copy_id_btn.text() == "⧉"
    assert "Copy" in popup._copy_id_btn.toolTip()
    assert popup._open_folder_btn is not None
    assert popup._open_folder_btn.text() == "↗"
    assert "Open" in popup._open_folder_btn.toolTip()
    assert popup._repair_btn is not None
    assert popup._repair_btn.text() == "⟲"
    assert "thinking" in popup._repair_btn.toolTip().lower()


def test_detail_popup_no_actions_when_no_uuid(qtbot):
    """No uuid → no transcript to repair AND no id to copy. Copy ID
    and Reset hide; Open folder (works without uuid) stays."""
    from claude_island.ui.expanded_window import SessionDetailPopup
    s = _session(1, "/x")
    details = _make_full_details(s, effective_uuid="")
    popup = SessionDetailPopup(details, s)
    qtbot.addWidget(popup)
    assert popup._copy_id_btn is None
    assert popup._repair_btn is None
    assert popup._open_folder_btn is not None  # always present


def test_detail_popup_status_pill_never_renders(qtbot):
    """Status pill (idle / waiting / busy) was removed by design — the
    information is low-value for popup users (who came here to inspect,
    not monitor) and the chip created visual noise next to the action
    icons. None of the three states should render a pill."""
    from claude_island.ui.expanded_window import SessionDetailPopup
    from PySide6.QtWidgets import QLabel as _QL
    s = _session(1, "/x")
    for state in ("idle", "waiting", "busy"):
        details = _make_full_details(s, status=state)
        popup = SessionDetailPopup(details, s)
        qtbot.addWidget(popup)
        text = " | ".join(l.text() for l in popup.findChildren(_QL) if l.text())
        assert state not in text, f"status '{state}' should not render"


def test_detail_popup_copy_id_action_writes_clipboard(qtbot):
    """Clicking the footer 'Copy ID' button puts the full UUID on the
    clipboard and surfaces a confirmation in the status line."""
    from claude_island.ui.expanded_window import SessionDetailPopup
    from PySide6.QtWidgets import QApplication
    s = _session(1, "/x")
    full_uuid = "deadbeef-0000-0000-0000-000000000000"
    details = _make_full_details(s, effective_uuid=full_uuid)
    popup = SessionDetailPopup(details, s)
    qtbot.addWidget(popup)
    popup._on_copy_id()
    assert QApplication.clipboard().text() == full_uuid
    assert "deadbeef" in popup._repair_status.text()


def test_detail_popup_path_click_invokes_open_folder_callback(qtbot):
    """Path value is clickable and routes through the on_open_folder
    callback (which the wiring layer connects to dispatcher → OS
    backend's REVEAL_CWD)."""
    from PySide6.QtCore import Qt, QPointF
    from PySide6.QtGui import QMouseEvent
    from claude_island.ui.expanded_window import SessionDetailPopup
    s = _session(1, "/some/proj/path")
    details = _make_full_details(s)

    called: list[bool] = []
    popup = SessionDetailPopup(
        details, s, on_open_folder=lambda: (called.append(True) or True),
    )
    qtbot.addWidget(popup)
    popup.show()

    from PySide6.QtWidgets import QLabel
    path_label = None
    for lbl in popup.findChildren(QLabel):
        if lbl.text() == str(s.project_path):
            path_label = lbl
            break
    assert path_label is not None
    assert path_label.toolTip() == "Open project folder in file explorer"

    path_label.mousePressEvent(QMouseEvent(
        QMouseEvent.Type.MouseButtonPress,
        QPointF(), QPointF(), QPointF(),
        Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier
    ))
    assert called == [True]


def test_detail_popup_header_layout_stable_when_prompt_toggles(qtbot):
    """Regression: when prompt is expanded, header section's geometry
    must NOT change. Earlier bugs let layout surplus stretch the
    header, leaving a visible gap between subtitle and ID row."""
    from claude_island.ui.expanded_window import SessionDetailPopup
    long_prompt = "first line\n" + ("x" * 500)
    s = _session(1, "/x")
    details = _make_full_details(s, last_prompt=long_prompt)
    popup = SessionDetailPopup(details, s)
    qtbot.addWidget(popup)
    popup.show()

    root = popup.layout()
    geom_before = root.itemAt(0).widget().geometry()
    popup._prompt_section._on_toggle()
    geom_after = root.itemAt(0).widget().geometry()
    # Allow 2px tolerance for font-baseline rounding; the original bug
    # stretched header by 30+ px so this still catches it definitively.
    height_diff = abs(geom_after.height() - geom_before.height())
    assert height_diff <= 2, (
        f"header section height changed by {height_diff}px on prompt "
        f"expand (was {geom_before.height()}, now {geom_after.height()})"
    )
    assert geom_before.topLeft() == geom_after.topLeft()


def test_detail_popup_open_folder_invokes_callback(qtbot):
    """Clicking 'Open folder' fires the on_open_folder callback (which
    the wiring layer routes to dispatcher → REVEAL_CWD). Popup itself
    has no platform knowledge."""
    from claude_island.ui.expanded_window import SessionDetailPopup
    s = _session(1, "/test/proj")
    details = _make_full_details(s)

    called: list[bool] = []
    popup = SessionDetailPopup(
        details, s, on_open_folder=lambda: (called.append(True) or True),
    )
    qtbot.addWidget(popup)

    popup._on_open_folder()
    assert called == [True]
    assert "Opened" in popup._repair_status.text()


def test_detail_popup_open_folder_unwired_shows_error(qtbot):
    """Without the on_open_folder callback wired (test fixture / dev
    setup), clicking surfaces the not-wired status — no silent no-op."""
    from claude_island.ui.expanded_window import SessionDetailPopup
    s = _session(1, "/test/proj")
    details = _make_full_details(s)
    popup = SessionDetailPopup(details, s)  # no on_open_folder
    qtbot.addWidget(popup)
    popup._on_open_folder()
    assert "not wired" in popup._repair_status.text()


# ============================================================================
# Inline session rename — ✎ button on the detail popup header swaps the
# title for a QLineEdit; Enter commits via on_rename callback, Esc cancels.
# ============================================================================

class TestDetailPopupRename:
    """Edit affordance on the detail popup header."""

    @staticmethod
    def _popup(qtbot, *, with_rename: bool):
        from claude_island.ui.expanded_window import SessionDetailPopup
        s = _session(1, "/proj")
        details = _make_full_details(s, effective_uuid="uuid-1")
        renames: list = []
        # Callback returns True on success — required for the popup to
        # commit the rename. Old contract returned None (truthy ≠ True);
        # new dispatch contract uses bool.
        popup = SessionDetailPopup(
            details, s,
            on_rename=(lambda u, n: renames.append((u, n)) or True) if with_rename else None
        )
        qtbot.addWidget(popup)
        return popup, renames

    def test_edit_button_visible_when_callback_wired_and_uuid_present(self, qtbot):
        popup, _ = self._popup(qtbot, with_rename=True)
        assert popup._edit_btn is not None
        assert popup._edit_btn.text() == "✎"
        assert "Rename" in popup._edit_btn.toolTip()

    def test_edit_button_hidden_without_callback(self, qtbot):
        # Tests / detached use that don't supply on_rename → no edit
        # affordance, popup stays read-only.
        popup, _ = self._popup(qtbot, with_rename=False)
        assert popup._edit_btn is None

    def test_enter_rename_mode_swaps_label_for_lineedit(self, qtbot):
        popup, _ = self._popup(qtbot, with_rename=True)
        from PySide6.QtWidgets import QLineEdit
        assert popup._name_edit is None
        popup._enter_rename_mode()
        assert popup._name_edit is not None
        assert isinstance(popup._name_edit, QLineEdit)
        # Edit button disabled while editing so the user can't double-fire.
        assert popup._edit_btn is not None and not popup._edit_btn.isEnabled()
        # Original label preserved (hidden) so cancel can restore it.
        assert popup._name_label is not None and popup._name_label.isHidden()

    def test_enter_rename_mode_idempotent(self, qtbot):
        # Double-clicking ✎ shouldn't create a second QLineEdit.
        popup, _ = self._popup(qtbot, with_rename=True)
        popup._enter_rename_mode()
        first_edit = popup._name_edit
        popup._enter_rename_mode()
        assert popup._name_edit is first_edit

    def test_commit_rename_invokes_callback_and_exits_edit(self, qtbot):
        popup, renames = self._popup(qtbot, with_rename=True)
        popup._enter_rename_mode()
        popup._name_edit.setText("frontend refactor")
        popup._commit_rename()
        # Per-session callback: uuid + new name. Project_path is no
        # longer threaded through (the dual-key design bled renames
        # across siblings).
        assert renames == [("uuid-1", "frontend refactor")]
        # Edit mode torn down, label restored, edit button re-enabled.
        # Use isHidden() not isVisible() — the popup itself is never
        # shown in tests, so isVisible would always return False.
        # isHidden() checks the explicit hide() flag.
        assert popup._name_edit is None
        assert popup._edit_btn.isEnabled()
        assert not popup._name_label.isHidden()
        assert popup._name_label.text() == "frontend refactor"

    def test_commit_empty_clears_override(self, qtbot):
        # Saving an empty string is the "restore default" gesture —
        # the callback receives "" and the platform layer translates
        # it into a delete.
        popup, renames = self._popup(qtbot, with_rename=True)
        popup._enter_rename_mode()
        popup._name_edit.setText("   ")
        popup._commit_rename()
        assert renames == [("uuid-1", "")]

    def test_cancel_rename_does_not_invoke_callback(self, qtbot):
        popup, renames = self._popup(qtbot, with_rename=True)
        original = popup._name_label.text()
        popup._enter_rename_mode()
        popup._name_edit.setText("typed but cancelled")
        popup._cancel_rename()
        assert renames == []
        # Label text untouched.
        assert popup._name_label.text() == original
        assert popup._name_edit is None

    def test_callback_exception_keeps_popup_alive(self, qtbot):
        # If on_rename raises (e.g. disk full), surface a status line
        # and exit edit mode — but don't crash the popup.
        from claude_island.ui.expanded_window import SessionDetailPopup
        s = _session(1, "/proj")
        details = _make_full_details(s, effective_uuid="uuid-1")
        def boom(u, n):
            raise RuntimeError("disk full")
        popup = SessionDetailPopup(details, s, on_rename=boom)
        qtbot.addWidget(popup)
        popup._enter_rename_mode()
        popup._name_edit.setText("anything")
        popup._commit_rename()
        # Popup still alive, edit mode exited, error in status.
        assert popup._name_edit is None
        assert "Rename failed" in popup._repair_status.text()


class TestDetailPopupSubtitle:
    """Italic subtitle in the detail popup header. Two-tier resolution:
    prefer ai_title (AI-generated, descriptive), fall back to
    original_name (Claude Code's auto name) only when the user
    renamed but the AI never assigned a title."""

    @staticmethod
    def _label_with_text(popup, text: str):
        from PySide6.QtWidgets import QLabel
        for lbl in popup.findChildren(QLabel):
            if lbl.text() == text:
                return lbl
        return None

    @staticmethod
    def _count_labels(popup, text: str) -> int:
        from PySide6.QtWidgets import QLabel
        return sum(
            1 for lbl in popup.findChildren(QLabel) if lbl.text() == text
        )

    def test_ai_title_shown_when_renamed_and_differs(self, qtbot):
        # Classic case: AI generated a title, user renamed → italic
        # surfaces the AI title underneath.
        from claude_island.ui.expanded_window import SessionDetailPopup
        s = _session(1, "/proj")
        details = _make_full_details(
            s, name="learning python",
            ai_title="Learn Python and TypeScript basics",
            original_name="cc-learning"
        )
        popup = SessionDetailPopup(details, s)
        qtbot.addWidget(popup)
        assert self._label_with_text(
            popup, "Learn Python and TypeScript basics"
        ) is not None

    def test_original_name_shown_when_renamed_and_no_ai_title(self, qtbot):
        # Session has no ai_title (Claude Code never set one) but DOES
        # have a state.name that the user renamed away from. Italic
        # falls back to the state.name so the user can still see what
        # the session used to be called.
        from claude_island.ui.expanded_window import SessionDetailPopup
        s = _session(1, "/proj")
        details = _make_full_details(
            s, name="claude md prompt coding",
            ai_title=None,
            original_name="claude md prompt"
        )
        popup = SessionDetailPopup(details, s)
        qtbot.addWidget(popup)
        assert self._label_with_text(popup, "claude md prompt") is not None

    def test_original_name_NOT_shown_when_no_rename(self, qtbot):
        # Without a rename, original_name == name == title. Surfacing
        # original_name as subtitle would just echo the title — noise.
        # Verify by counting: only one label with that text (the
        # title), not two (title + would-be-subtitle).
        from claude_island.ui.expanded_window import SessionDetailPopup
        s = _session(1, "/proj")
        details = _make_full_details(
            s, name="cc-learning",
            ai_title=None,
            original_name="cc-learning"
        )
        popup = SessionDetailPopup(details, s)
        qtbot.addWidget(popup)
        assert self._count_labels(popup, "cc-learning") == 1

    def test_no_subtitle_when_no_ai_title_and_no_rename(self, qtbot):
        # Same as above but with original_name=None too — guards the
        # "MiniMax-style session with neither ai_title nor state.name"
        # path. Title degrades to project basename; no subtitle.
        from claude_island.ui.expanded_window import SessionDetailPopup
        s = _session(1, "/proj-name")
        details = _make_full_details(
            s, name=None,
            ai_title=None,
            original_name=None
        )
        popup = SessionDetailPopup(details, s)
        qtbot.addWidget(popup)
        # No phantom italic line of any kind.
        assert self._count_labels(popup, "proj-name") == 1

    def test_ai_title_preferred_over_original_when_both_present(self, qtbot):
        # When both candidates differ from the renamed title, ai_title
        # wins — it's typically more descriptive than the auto name.
        from claude_island.ui.expanded_window import SessionDetailPopup
        s = _session(1, "/proj")
        details = _make_full_details(
            s, name="custom",
            ai_title="The AI Generated Title",
            original_name="The Original Name"
        )
        popup = SessionDetailPopup(details, s)
        qtbot.addWidget(popup)
        assert self._label_with_text(
            popup, "The AI Generated Title"
        ) is not None
        assert self._label_with_text(popup, "The Original Name") is None

    def test_falls_through_to_original_when_rename_matches_ai_title(self, qtbot):
        # User renamed to the same string ai_title would have produced
        # → ai_title would echo the title. Fall through to
        # original_name (which still differs) so the user can see what
        # the auto-detected name was.
        from claude_island.ui.expanded_window import SessionDetailPopup
        s = _session(1, "/proj")
        details = _make_full_details(
            s, name="Refactor",
            ai_title="Refactor",
            original_name="cc-learning"
        )
        popup = SessionDetailPopup(details, s)
        qtbot.addWidget(popup)
        # Title appears once; "cc-learning" appears as the italic
        # subtitle (fall-through after ai_title echoed).
        assert self._count_labels(popup, "Refactor") == 1
        assert self._label_with_text(popup, "cc-learning") is not None

    def test_no_subtitle_when_all_candidates_echo_title(self, qtbot):
        # ai_title and original_name both match the rename, AND the
        # project basename also matches → nothing to add. Subtitle
        # suppressed, only the title shown.
        from claude_island.ui.expanded_window import SessionDetailPopup
        s = _session(1, "/Same")  # path basename also "Same"
        details = _make_full_details(
            s, name="Same",
            ai_title="Same",
            original_name="Same"
        )
        popup = SessionDetailPopup(details, s)
        qtbot.addWidget(popup)
        assert self._count_labels(popup, "Same") == 1

    def test_basename_fallback_when_renamed_and_no_other_names(self, qtbot):
        # The bug fix: user renamed but state.name was never written
        # AND no ai_title exists. Without this fallback the popup
        # would show only the custom title and nothing underneath —
        # leaving the user wondering what Claude would have called it.
        # The project basename is the last-ditch "natural" name.
        from claude_island.ui.expanded_window import SessionDetailPopup
        s = _session(1, "/home/me/claude-island")
        details = _make_full_details(
            s, name="claude-island-dev",
            ai_title=None,
            original_name=None
        )
        popup = SessionDetailPopup(details, s)
        qtbot.addWidget(popup)
        assert self._label_with_text(popup, "claude-island") is not None


# ============================================================================
# Add-provider dialog (in-app + button) — frameless popup that lets the
# user paste credentials and have a new tab appear without restart.
# ============================================================================

class TestAddProviderDialog:
    """Pure-UI tests: dialog renders fields from each provider's
    default_config(), Save validates + invokes the callback. The
    actual file write is tested separately at the platform layer."""

    @staticmethod
    def _zhipu_cfg():
        return {
            "_help": "Paste your Z.AI key here.",
            "auth_token": "",
            "base_url": "https://api.z.ai",
        }

    @staticmethod
    def _minimax_cfg():
        return {
            "_help": "Paste your MiniMax key.",
            "auth_token": "",
            "base_url": "https://api.minimaxi.com",
        }

    def test_renders_radio_for_each_configurable(self, qtbot):
        from claude_island.ui.expanded_window import _AddProviderDialog
        dlg = _AddProviderDialog(
            configurable=[("zhipu", self._zhipu_cfg()),
                          ("minimax", self._minimax_cfg())],
            on_save=lambda *a: None
        )
        qtbot.addWidget(dlg)
        # Radio strip has one button per configurable provider.
        assert "zhipu" in dlg._radio_btns
        assert "minimax" in dlg._radio_btns
        assert len(dlg._radio_btns) == 2

    def test_renders_form_fields_from_default_config(self, qtbot):
        from claude_island.ui.expanded_window import _AddProviderDialog
        from PySide6.QtWidgets import QLineEdit
        dlg = _AddProviderDialog(
            configurable=[("zhipu", self._zhipu_cfg())],
            on_save=lambda *a: None
        )
        qtbot.addWidget(dlg)
        # auth_token + base_url QLineEdits both registered for the active form.
        keys = [k for k, _ in dlg._inputs["zhipu"]]
        assert keys == ["auth_token", "base_url"]
        # auth_token is password-mode (echo hides input).
        token_edit = dict(dlg._inputs["zhipu"])["auth_token"]
        assert token_edit.echoMode() == QLineEdit.EchoMode.Password
        assert token_edit.text() == ""  # always empty initial regardless of seed
        # base_url pre-filled from default_config.
        url_edit = dict(dlg._inputs["zhipu"])["base_url"]
        assert url_edit.text() == "https://api.z.ai"

    def test_save_with_empty_token_shows_error_no_callback(self, qtbot):
        from claude_island.ui.expanded_window import _AddProviderDialog
        called: list = []
        dlg = _AddProviderDialog(
            configurable=[("zhipu", self._zhipu_cfg())],
            on_save=lambda *a: called.append(a)
        )
        qtbot.addWidget(dlg)
        # auth_token starts empty; click Save without typing.
        dlg._on_save_clicked()
        assert called == []                  # callback NOT invoked
        # isVisible() returns False when the widget isn't realised
        # (qtbot.addWidget doesn't show the parent). isHidden() reflects
        # only the explicit hide()/show() state regardless of ancestry.
        assert not dlg._status.isHidden()
        assert "auth_token" in dlg._status.text()

    def test_save_invokes_callback_with_field_values(self, qtbot):
        from claude_island.ui.expanded_window import _AddProviderDialog
        captured: list[tuple[str, dict]] = []
        dlg = _AddProviderDialog(
            configurable=[("zhipu", self._zhipu_cfg())],
            on_save=lambda n, f: captured.append((n, f))
        )
        qtbot.addWidget(dlg)
        # Fill the form: paste a token, change base_url.
        inputs = dict(dlg._inputs["zhipu"])
        inputs["auth_token"].setText("z-test-key")
        inputs["base_url"].setText("https://api.z.ai")
        dlg._on_save_clicked()
        assert captured == [("zhipu", {"auth_token": "z-test-key",
                                       "base_url": "https://api.z.ai"})]

    def test_radio_switch_changes_active_form(self, qtbot):
        from claude_island.ui.expanded_window import _AddProviderDialog
        dlg = _AddProviderDialog(
            configurable=[("zhipu", self._zhipu_cfg()),
                          ("minimax", self._minimax_cfg())],
            on_save=lambda *a: None
        )
        qtbot.addWidget(dlg)
        # Default-selected is the first provider in the list.
        assert dlg._active == "zhipu"
        assert not dlg._form_widgets["zhipu"].isHidden()
        assert dlg._form_widgets["minimax"].isHidden()
        # Click the minimax radio.
        dlg._select_provider("minimax")
        assert dlg._active == "minimax"
        assert dlg._form_widgets["zhipu"].isHidden()
        assert not dlg._form_widgets["minimax"].isHidden()

    def test_empty_state_when_nothing_configurable(self, qtbot):
        from claude_island.ui.expanded_window import _AddProviderDialog
        from PySide6.QtWidgets import QLabel
        dlg = _AddProviderDialog(
            configurable=[],
            on_save=lambda *a: None
        )
        qtbot.addWidget(dlg)
        # No radio buttons, no inputs, no save button.
        assert not hasattr(dlg, "_radio_btns") or not dlg._radio_btns
        assert dlg._inputs == {}
        # Empty-state message is rendered as a QLabel containing the
        # word "configured" — check by text.
        labels = [l.text() for l in dlg.findChildren(QLabel)]
        assert any("configured" in t for t in labels)

    def test_callback_exception_surfaces_in_status(self, qtbot):
        # The dialog should not crash when on_save raises; instead it
        # shows the error in the status slot so the user can retry.
        from claude_island.ui.expanded_window import _AddProviderDialog

        def boom(_n, _f):
            raise RuntimeError("simulated write failure")

        dlg = _AddProviderDialog(
            configurable=[("zhipu", self._zhipu_cfg())],
            on_save=boom
        )
        qtbot.addWidget(dlg)
        dict(dlg._inputs["zhipu"])["auth_token"].setText("k")
        dlg._on_save_clicked()
        assert not dlg._status.isHidden()
        assert "simulated write failure" in dlg._status.text()


# ============================================================================
# ExpandedWindow.set_available_providers — runtime tab strip rebuild
# ============================================================================

def _panel_with_providers(qtbot, available: list[str], selected: str | None = None,
                          on_provider_config_changed=None,
                          list_configurable_providers=None,
                          save_provider_settings=None,
                          delete_provider_settings=None):
    capsule = QWidget()
    capsule.show()
    panel = ExpandedWindow(
        capsule=capsule,
        controller=IslandController(),
        get_usage_totals=lambda period: UsageTotals(period=period),
        available_providers=available,
        selected_provider=selected or (available[0] if available else None),
        on_provider_config_changed=on_provider_config_changed,
        list_configurable_providers=list_configurable_providers,
        save_provider_settings=save_provider_settings,
        delete_provider_settings=delete_provider_settings,
    )
    qtbot.addWidget(panel)
    qtbot.addWidget(capsule)
    return panel


# Realistic configurable-provider list used by tests that exercise the +
# button. Mirrors what __main__.py builds from platform_.providers but is
# hard-coded here so tests don't depend on the platform module.
_DEFAULT_CONFIGURABLE = [
    ("zhipu", {"auth_token": "", "base_url": "https://api.z.ai"}),
    ("minimax", {"auth_token": "", "base_url": "https://api.minimax.chat"}),
    ("deepseek", {"auth_token": "", "base_url": "https://api.deepseek.com"}),
]


class TestSetAvailableProviders:
    def test_adds_new_tab(self, qtbot):
        # Start with anthropic only — single-provider state still
        # renders one pill (always-pill design, see
        # test_single_provider_renders_one_pill).
        panel = _panel_with_providers(qtbot, ["anthropic"])
        assert set(panel._provider_btns.keys()) == {"anthropic"}
        # User adds zhipu via the + dialog → wiring layer pushes the
        # updated list back; tab strip rebuilds.
        panel.set_available_providers(["anthropic", "zhipu"], selected="anthropic")
        assert set(panel._provider_btns.keys()) == {"anthropic", "zhipu"}
        # Selected pill is checked.
        assert panel._provider_btns["anthropic"].isChecked()
        assert not panel._provider_btns["zhipu"].isChecked()

    def test_removes_obsolete_tab(self, qtbot):
        panel = _panel_with_providers(qtbot, ["anthropic", "zhipu"], "zhipu")
        assert set(panel._provider_btns.keys()) == {"anthropic", "zhipu"}
        # Token removed → wiring pushes a smaller list.
        panel.set_available_providers(["anthropic"])
        assert "zhipu" not in panel._provider_btns

    def test_falls_back_when_selected_removed(self, qtbot):
        # Selected provider is dropped; selection must reset to the
        # first remaining (since no explicit override is passed).
        panel = _panel_with_providers(qtbot, ["anthropic", "zhipu"], "zhipu")
        panel.set_available_providers(["anthropic"])
        assert panel._selected_provider == "anthropic"

    def test_explicit_selected_arg_wins(self, qtbot):
        panel = _panel_with_providers(qtbot, ["anthropic"])
        panel.set_available_providers(["anthropic", "zhipu", "minimax"], selected="minimax")
        assert panel._selected_provider == "minimax"


class TestPlusButtonVisibility:
    def test_plus_visible_when_provider_addable(self, qtbot):
        # anthropic is registered, others (minimax, zhipu) have
        # default_config() and are NOT in available → + should appear.
        called: list = []
        panel = _panel_with_providers(
            qtbot, ["anthropic"],
            on_provider_config_changed=lambda: called.append(1),
            list_configurable_providers=lambda: list(_DEFAULT_CONFIGURABLE),
        )
        # The + button is held on the panel as _add_provider_btn.
        assert panel._add_provider_btn is not None
        assert panel._add_provider_btn.text() == "+"

    def test_plus_hidden_when_callback_not_wired(self, qtbot):
        # Without on_provider_config_changed (e.g. tests that don't
        # care about the dialog), the + button stays out of the strip.
        panel = _panel_with_providers(qtbot, ["anthropic"])
        assert getattr(panel, "_add_provider_btn", None) is None

    def test_plus_hidden_when_all_configured(self, qtbot):
        # All providers in available → nothing left to add → no +.
        panel = _panel_with_providers(
            qtbot, ["anthropic", "minimax", "zhipu", "deepseek"], "anthropic",
            on_provider_config_changed=lambda: None,
            list_configurable_providers=lambda: list(_DEFAULT_CONFIGURABLE),
        )
        assert getattr(panel, "_add_provider_btn", None) is None

    def test_dialog_save_persists_then_triggers_callback(self, qtbot):
        # End-to-end through the panel: filling the form and clicking
        # Save calls the injected save_provider_settings AND invokes the
        # on_provider_config_changed callback exactly once.
        #
        # Inject fake callbacks instead of monkeypatching platform_ —
        # the panel takes its persistence layer via DI (import-linter
        # contract: ui must not import platform).
        writes: list[tuple[str, dict]] = []
        refreshes: list = []

        panel = _panel_with_providers(
            qtbot, ["anthropic"],
            on_provider_config_changed=lambda: refreshes.append(1),
            save_provider_settings=lambda name, fields: writes.append((name, fields)),
        )
        # Drive the save handler directly with realistic args.
        panel._on_dialog_save("zhipu", {"auth_token": "k", "base_url": "https://api.z.ai"})
        assert writes == [("zhipu", {"auth_token": "k", "base_url": "https://api.z.ai"})]
        assert refreshes == [1]


class TestProviderTabContextMenu:
    """Right-click on a non-anthropic quota tab → Delete option that
    removes the provider's block from providers.json and triggers a
    rebuild via the on_provider_config_changed callback."""

    def test_anthropic_tab_has_no_context_menu(self, qtbot):
        """Anthropic is the always-available baseline; the wiring layer
        must not install a delete affordance on its pill."""
        panel = _panel_with_providers(
            qtbot, ["anthropic", "minimax"], "anthropic",
            on_provider_config_changed=lambda: None
        )
        anth_btn = panel._provider_btns["anthropic"]
        assert anth_btn.contextMenuPolicy() != Qt.ContextMenuPolicy.CustomContextMenu

    def test_non_anthropic_tab_has_custom_context_menu(self, qtbot):
        """MiniMax / Zhipu tabs opt into custom context menus so the
        Delete action can be wired."""
        panel = _panel_with_providers(
            qtbot, ["anthropic", "minimax"], "anthropic",
            on_provider_config_changed=lambda: None
        )
        mm_btn = panel._provider_btns["minimax"]
        assert mm_btn.contextMenuPolicy() == Qt.ContextMenuPolicy.CustomContextMenu

    def test_context_menu_skipped_when_callback_not_wired(self, qtbot):
        """Without on_provider_config_changed the rebuild can't happen,
        so don't install the menu — would silently corrupt user state."""
        panel = _panel_with_providers(qtbot, ["anthropic", "minimax"], "anthropic")
        mm_btn = panel._provider_btns["minimax"]
        assert mm_btn.contextMenuPolicy() != Qt.ContextMenuPolicy.CustomContextMenu

    def test_delete_invokes_platform_helper_and_callback(self, qtbot):
        """Driving _on_delete_provider_clicked directly: it must call
        the injected delete_provider_settings(name) AND fire the rebuild
        callback exactly once. Injected via DI rather than monkeypatch
        (UI doesn't import platform layer)."""
        deletes: list[str] = []
        refreshes: list = []
        panel = _panel_with_providers(
            qtbot, ["anthropic", "minimax"], "anthropic",
            on_provider_config_changed=lambda: refreshes.append(1),
            delete_provider_settings=lambda name: deletes.append(name),
        )
        panel._on_delete_provider_clicked("minimax")
        assert deletes == ["minimax"]
        assert refreshes == [1]


# ============================================================================
# Row v2 layout — dot + name + cost on top, model chip + status below
# (P0.3 — added with the Dynamic Island redesign)
# ============================================================================


class TestRowStatusLine:
    """The new two-line row layout adds three labels on top of the
    existing name/meta pair: an activity dot (left), a model chip
    (bottom-left), and a status text (bottom-centre). These tests pin
    the contract that those labels exist with the expected objectNames
    and that update_row populates them from SessionDetails."""

    def test_row_has_new_status_widgets(self, panel):
        """Every row must expose the status glyph + model_chip +
        status_label widgets the row update path looks up by name.
        Status glyph is the equalizer/dot/⚡ tri-state widget that
        replaced the old dot_label QLabel."""
        from PySide6.QtWidgets import QLabel
        panel._render_sessions([_session(1, "/a")])
        btn = panel._rows[1]
        assert getattr(btn, "_status_glyph", None) is not None
        assert btn.findChild(QLabel, "model_chip") is not None
        assert btn.findChild(QLabel, "status_label") is not None

    def test_row_height_grew_to_fit_two_lines(self, panel):
        """Row height jumped from 36 to 52 when the status row was
        added. Locking the value down so a future code change can't
        accidentally squash the bottom line and clip descenders."""
        from claude_island.ui.expanded_window import _ROW_HEIGHT
        panel._render_sessions([_session(1, "/a")])
        assert _ROW_HEIGHT == 52
        assert panel._rows[1].height() == 52

    def test_chip_hidden_when_no_per_model_data(self, qtbot):
        """A freshly-discovered session has no UsageRecords yet ⇒
        per_model is empty ⇒ rendering "[]" or a blank pill would
        read as a bug. The chip must hide entirely."""
        from PySide6.QtWidgets import QLabel
        from claude_island.core.models import SessionDetails

        def details(session):
            return SessionDetails(
                session=session, name="x", ai_title=None, git_branch=None,
                last_prompt=None, started_at=None, status=None,
                cc_version=None, cost_usd=0.0, turn_count=0,
                sidechain_count=0, per_model=()
            )

        capsule = QWidget()
        capsule.show()
        controller = IslandController()
        p = ExpandedWindow(
            capsule=capsule,
            controller=controller,
            get_usage_totals=lambda period: UsageTotals(period=period),
            get_session_details=details
        )
        qtbot.addWidget(p)
        qtbot.addWidget(capsule)

        p._render_sessions([_session(1, "/a")])
        chip = p._rows[1].findChild(QLabel, "model_chip")
        assert chip is not None
        assert chip.isHidden()

    def test_chip_shows_model_short_name_and_color(self, qtbot):
        """When per_model has entries, the chip uses the highest-cost
        model's short name + its tier-coded colour. Sonnet → blue is
        the canonical Anthropic mid-tier mapping."""
        from PySide6.QtWidgets import QLabel
        from claude_island.core.models import (
            ModelTotals, SessionDetails
        )

        def details(session):
            return SessionDetails(
                session=session, name="x", ai_title=None, git_branch=None,
                last_prompt=None, started_at=None, status=None,
                cc_version=None, cost_usd=10.0, turn_count=2,
                sidechain_count=0,
                per_model=(
                    ModelTotals(
                        model="claude-sonnet-4-6",
                        input_tokens=1, output_tokens=1,
                        cache_creation_tokens=0, cache_read_tokens=0,
                        cost_usd=8.0
                    ),
                )
            )

        capsule = QWidget()
        capsule.show()
        controller = IslandController()
        p = ExpandedWindow(
            capsule=capsule,
            controller=controller,
            get_usage_totals=lambda period: UsageTotals(period=period),
            get_session_details=details
        )
        qtbot.addWidget(p)
        qtbot.addWidget(capsule)

        p._render_sessions([_session(1, "/a")])
        chip = p._rows[1].findChild(QLabel, "model_chip")
        assert chip is not None
        assert chip.text() == "Sonnet"
        assert "3B82F6" in chip.styleSheet()
        assert not chip.isHidden()

    def test_status_label_shows_state_and_age(self, qtbot):
        """status="busy" should render as "running" (user-friendly verb,
        matches the capsule's vocabulary), suffixed with a relative
        ``Nm ago`` derived from session.last_activity."""
        from PySide6.QtWidgets import QLabel
        from claude_island.core.models import SessionDetails

        def details(session):
            return SessionDetails(
                session=session, name="x", ai_title=None, git_branch=None,
                last_prompt=None, started_at=None, status="busy",
                cc_version=None, cost_usd=0.0, turn_count=0,
                sidechain_count=0
            )

        capsule = QWidget()
        capsule.show()
        controller = IslandController()
        p = ExpandedWindow(
            capsule=capsule,
            controller=controller,
            get_usage_totals=lambda period: UsageTotals(period=period),
            get_session_details=details
        )
        qtbot.addWidget(p)
        qtbot.addWidget(capsule)

        p._render_sessions([_session(1, "/a", ago_minutes=5)])
        status = p._rows[1].findChild(QLabel, "status_label")
        assert status is not None
        # Status row dropped the literal "running" / "idle" word —
        # animation conveys that. What remains is just the relative
        # activity time ("Nm ago").
        text = status.text()
        assert text.endswith("ago")
        assert "m" in text or "h" in text or "s" in text


class TestModelHelpers:
    """Inline model-name + colour resolvers (precursor to the full
    declarative registry coming in P2). Length-descending substring
    match — same algorithm as ``usage_registry._resolve_pricing`` so
    long keys like ``minimax-m2.7`` win over short family tokens."""

    def test_short_name_anthropic_families(self):
        from claude_island.ui.expanded_window import _resolve_model_short_name
        assert _resolve_model_short_name("claude-opus-4-7") == "Opus"
        assert _resolve_model_short_name("claude-sonnet-4-6") == "Sonnet"
        assert _resolve_model_short_name("claude-haiku-4-5-20251001") == "Haiku"

    def test_short_name_deepseek_tiers(self):
        from claude_island.ui.expanded_window import _resolve_model_short_name
        assert _resolve_model_short_name("deepseek-v4-pro") == "DeepSeek V4 Pro"
        assert _resolve_model_short_name("deepseek-v4-flash") == "DeepSeek V4 Flash"
        assert _resolve_model_short_name("deepseek-r1") == "DeepSeek"

    def test_short_name_minimax_versions(self):
        from claude_island.ui.expanded_window import _resolve_model_short_name
        assert _resolve_model_short_name("MiniMax-M2.7-highspeed") == "MiniMax M2.7"
        assert _resolve_model_short_name("MiniMax-M2.5") == "MiniMax M2.5"
        assert _resolve_model_short_name("MiniMax") == "MiniMax"

    def test_short_name_unknown_falls_back_to_prefix(self):
        from claude_island.ui.expanded_window import _resolve_model_short_name
        # Unknown families get a safe truncation rather than empty —
        # the user at least sees something they can recognise.
        assert _resolve_model_short_name("brand-new-llama-4-405b") == "brand-new-ll"
        # Empty string → empty (defensive; no model id ⇒ no chip).
        assert _resolve_model_short_name("") == ""

    def test_color_known_families(self):
        from claude_island.ui.expanded_window import (
            _resolve_model_color, _MODEL_COLOR_FALLBACK
        )
        assert _resolve_model_color("claude-opus-4-7") == "#8B5CF6"
        assert _resolve_model_color("deepseek-v4-flash") == "#FB923C"
        assert _resolve_model_color("GLM-Air") == "#22D3EE"
        # Unknown returns neutral grey, NOT a randomised colour — design
        # rules out hash-based colour assignment so the same unknown
        # model always renders identically across runs.
        assert _resolve_model_color("gpt-mystery") == _MODEL_COLOR_FALLBACK
        assert _resolve_model_color("") == _MODEL_COLOR_FALLBACK


class TestRowStatusText:
    """Composes the bottom-line text. The literal "running"/"idle"
    word was dropped — the row's left-edge pulse animation conveys
    that signal now. Helper just returns the relative time of the
    most recent JSONL write (e.g. "5m ago")."""

    def test_returns_relative_time_with_active_prefix(self):
        """The "active" prefix disambiguates this from the popup's
        "Created" line — both used to format as plain "<N>m ago"."""
        from claude_island.ui.expanded_window import _row_status_text
        from claude_island.core.snapshot import _degraded_view
        view = _degraded_view(_session(1, "/a", ago_minutes=5))
        text = _row_status_text(view)
        assert text.startswith("active ")
        assert text.endswith("ago")
        assert "m" in text or "h" in text

    def test_returns_active_now_for_fresh_activity(self):
        """Activity within 5 seconds renders as "active now" — the old
        "0s ago" / "3s ago" jittered chaotically for running sessions
        on every Snapshotter rebuild and read like a bug."""
        from claude_island.ui.expanded_window import _row_status_text
        from claude_island.core.snapshot import _degraded_view
        view = _degraded_view(_session(1, "/a", ago_minutes=0))
        text = _row_status_text(view)
        assert text == "active now"


# ============================================================================
# Summary card (P1.1) — top focus area with today $ + 5h quota bar
# ============================================================================


def _quota_snap(pct: float):
    """Minimal QuotaSnapshot — only five_hour_pct + reset matter for
    summary card rendering."""
    from datetime import datetime, timedelta, timezone
    from claude_island.core.models import QuotaSnapshot
    now = datetime.now(timezone.utc)
    return QuotaSnapshot(
        five_hour_pct=pct,
        five_hour_resets_at=now + timedelta(hours=4, minutes=47),
        seven_day_pct=10.0,
        seven_day_resets_at=now + timedelta(days=2),
        fetched_at=now,
        is_stale=False
    )


class TestSummaryCard:
    """The new top focus card. Pins:
       - Today's $ comes from get_usage_totals('today')
       - Subtitle reads "<Provider> · resets in <countdown>"
       - 5h bar hides when no snapshot is available"""

    def test_summary_amount_from_today_total(self, qtbot):
        from claude_island.core.models import UsageTotals
        capsule = QWidget()
        capsule.show()
        controller = IslandController()
        p = ExpandedWindow(
            capsule=capsule,
            controller=controller,
            get_usage_totals=lambda period: UsageTotals(
                period=period,
                input_cost=80.0, output_cost=6.42
            )
        )
        qtbot.addWidget(p); qtbot.addWidget(capsule)
        p._refresh_summary_card()
        # _fmt_money rounds 86.42 → "$86" (≥10 cuts cents)
        assert p._summary_amount.text() == "$86"

    def test_summary_hides_quota_bar_when_no_snapshot(self, qtbot):
        from claude_island.core.models import UsageTotals
        capsule = QWidget()
        capsule.show()
        controller = IslandController()
        p = ExpandedWindow(
            capsule=capsule,
            controller=controller,
            get_usage_totals=lambda period: UsageTotals(period=period),
            get_quota_snapshot=lambda: None
        )
        qtbot.addWidget(p); qtbot.addWidget(capsule)
        p._refresh_summary_card()
        assert p._summary_quota_bar.isHidden()
        assert p._summary_caption.isHidden()
        # Subtitle still names the provider so the user can tell which
        # provider's quota we tried (and failed) to fetch.
        assert "unavailable" in p._summary_subtitle.text().lower()

    def test_summary_shows_quota_bar_when_snapshot_present(self, qtbot):
        from claude_island.core.models import UsageTotals
        capsule = QWidget()
        capsule.show()
        controller = IslandController()
        p = ExpandedWindow(
            capsule=capsule,
            controller=controller,
            get_usage_totals=lambda period: UsageTotals(period=period),
            get_quota_snapshot=lambda: _quota_snap(78.0)
        )
        qtbot.addWidget(p); qtbot.addWidget(capsule)
        p._refresh_summary_card()
        # ``isVisible`` requires the parent to be shown — use isHidden
        # negation, which is the offscreen-test-safe equivalent.
        assert not p._summary_quota_bar.isHidden()
        assert p._summary_quota_bar.value() == 78
        assert "78%" in p._summary_caption.text()
        assert "5h limit" in p._summary_caption.text()
        assert "resets in" in p._summary_subtitle.text()


# ============================================================================
# High-cost row alert (P2.3) — cumulative spend ≥ threshold flips dot
# ============================================================================


class TestHighCostRowAlert:
    def test_high_cost_idle_row_paints_yellow_cost_label(self, qtbot):
        """An IDLE session whose cumulative cost exceeds the alert
        threshold renders the cost label in yellow (+ bold). The
        glyph stays in IDLE — the cost number IS the warning, no
        separate icon needed."""
        from PySide6.QtWidgets import QLabel
        from claude_island.core.models import SessionDetails
        from claude_island.ui.expanded_window import _RowStatusGlyph

        def details(session):
            return SessionDetails(
                session=session, name="x", ai_title=None, git_branch=None,
                last_prompt=None, started_at=None, status=None,
                cc_version=None, cost_usd=132.0,  # well above $50
                turn_count=10, sidechain_count=0
            )

        capsule = QWidget(); capsule.show()
        controller = IslandController()
        p = ExpandedWindow(
            capsule=capsule, controller=controller,
            get_usage_totals=lambda period: __import__(
                "claude_island.core.models", fromlist=["UsageTotals"]
            ).UsageTotals(period=period),
            get_session_details=details
        )
        qtbot.addWidget(p); qtbot.addWidget(capsule)
        p._render_sessions([_session(1, "/a", ago_minutes=10)])
        btn = p._rows[1]
        # Left-side glyph is IDLE — high-cost no longer hijacks it.
        assert btn._status_glyph.state() == _RowStatusGlyph.STATE_IDLE
        # Cost label is yellow + bold.
        meta = btn.findChild(QLabel, "meta_label")
        assert meta is not None
        css = meta.styleSheet()
        assert "facc15" in css
        assert "600" in css  # font-weight bold-ish
        # Tooltip lives on the row.
        assert "high cumulative spend" in btn.toolTip().lower()

    def test_running_high_cost_independent_signals(self, qtbot):
        """A session that is BOTH running AND high-cost runs the
        equalizer on the LEFT and renders cost in yellow on the
        RIGHT — two independent visual channels, no overlap."""
        from PySide6.QtWidgets import QLabel
        from claude_island.core.models import SessionDetails
        from claude_island.ui.expanded_window import _RowStatusGlyph

        def details(session):
            return SessionDetails(
                session=session, name="x", ai_title=None, git_branch=None,
                last_prompt=None, started_at=None, status="busy",
                cc_version=None, cost_usd=132.0,
                turn_count=10, sidechain_count=0
            )

        capsule = QWidget(); capsule.show()
        controller = IslandController()
        p = ExpandedWindow(
            capsule=capsule, controller=controller,
            get_usage_totals=lambda period: __import__(
                "claude_island.core.models", fromlist=["UsageTotals"]
            ).UsageTotals(period=period),
            get_session_details=details
        )
        qtbot.addWidget(p); qtbot.addWidget(capsule)
        p._render_sessions([_session(1, "/a", ago_minutes=10)])
        btn = p._rows[1]
        # Left runs the equalizer; right colours the cost yellow.
        assert btn._status_glyph.state() == _RowStatusGlyph.STATE_RUNNING
        assert btn._running is True
        meta = btn.findChild(QLabel, "meta_label")
        assert "facc15" in meta.styleSheet()
        # Glyph bar colour stays standard green — cost colour now
        # owns the high-cost signal, the equalizer doesn't.
        assert btn._status_glyph._bar_color == "#22c55e"

    def test_low_cost_idle_keeps_default_cost_color(self, qtbot):
        """Low-cost session: cost label uses the default dim grey,
        no warning state."""
        from PySide6.QtWidgets import QLabel
        from claude_island.core.models import SessionDetails
        from claude_island.ui.expanded_window import _STYLE_COST_DEFAULT

        def details(session):
            return SessionDetails(
                session=session, name="x", ai_title=None, git_branch=None,
                last_prompt=None, started_at=None, status=None,
                cc_version=None, cost_usd=4.50,
                turn_count=2, sidechain_count=0
            )

        capsule = QWidget(); capsule.show()
        controller = IslandController()
        p = ExpandedWindow(
            capsule=capsule, controller=controller,
            get_usage_totals=lambda period: __import__(
                "claude_island.core.models", fromlist=["UsageTotals"]
            ).UsageTotals(period=period),
            get_session_details=details
        )
        qtbot.addWidget(p); qtbot.addWidget(capsule)
        p._render_sessions([_session(1, "/a", ago_minutes=10)])
        btn = p._rows[1]
        meta = btn.findChild(QLabel, "meta_label")
        assert meta.styleSheet() == _STYLE_COST_DEFAULT
        assert btn.toolTip() == ""

    def test_low_cost_dot_keeps_default_glyph(self, qtbot):
        """Cost below threshold ⇒ glyph stays in IDLE state (single
        static dot), tooltip cleared."""
        from claude_island.core.models import SessionDetails
        from claude_island.ui.expanded_window import _RowStatusGlyph

        def details(session):
            return SessionDetails(
                session=session, name="x", ai_title=None, git_branch=None,
                last_prompt=None, started_at=None, status=None,
                cc_version=None, cost_usd=4.50,
                turn_count=2, sidechain_count=0
            )

        capsule = QWidget(); capsule.show()
        controller = IslandController()
        p = ExpandedWindow(
            capsule=capsule, controller=controller,
            get_usage_totals=lambda period: __import__(
                "claude_island.core.models", fromlist=["UsageTotals"]
            ).UsageTotals(period=period),
            get_session_details=details
        )
        qtbot.addWidget(p); qtbot.addWidget(capsule)
        # ago_minutes=10 keeps the session out of the "currently
        # running" window so the running-state path doesn't fire —
        # the test isolates the low-cost vs high-cost alert behaviour.
        p._render_sessions([_session(1, "/a", ago_minutes=10)])
        glyph = p._rows[1]._status_glyph
        assert glyph.state() == _RowStatusGlyph.STATE_IDLE
        assert glyph.toolTip() == ""


# ============================================================================
# _RowStatusGlyph — equalizer / ⚡ / dot tri-state widget
# ============================================================================


class TestRowStatusGlyph:
    """The widget that lives in each row's leftmost slot. Exposes
    set_state() with three states and runs internal animations only
    while in RUNNING state."""

    def test_default_state_is_idle(self, qtbot):
        from claude_island.ui.expanded_window import _RowStatusGlyph
        g = _RowStatusGlyph()
        qtbot.addWidget(g)
        assert g.state() == _RowStatusGlyph.STATE_IDLE

    def test_set_state_running_starts_animations(self, qtbot):
        """Transitioning to RUNNING must start every per-bar animation
        — that's what produces the wave effect. Each animation should
        report State.Running afterwards."""
        from PySide6.QtCore import QAbstractAnimation
        from claude_island.ui.expanded_window import _RowStatusGlyph
        g = _RowStatusGlyph()
        qtbot.addWidget(g)
        g.set_state(_RowStatusGlyph.STATE_RUNNING)
        assert g.state() == _RowStatusGlyph.STATE_RUNNING
        for anim in g._anims:
            assert anim.state() == QAbstractAnimation.State.Running

    def test_leaving_running_stops_animations(self, qtbot):
        """RUNNING → IDLE / HIGH_COST must stop the equalizer animations
        — leaving them running off-screen would burn CPU pointlessly."""
        from PySide6.QtCore import QAbstractAnimation
        from claude_island.ui.expanded_window import _RowStatusGlyph
        g = _RowStatusGlyph()
        qtbot.addWidget(g)
        g.set_state(_RowStatusGlyph.STATE_RUNNING)
        g.set_state(_RowStatusGlyph.STATE_IDLE)
        for anim in g._anims:
            assert anim.state() == QAbstractAnimation.State.Stopped

    def test_idempotent_set_state(self, qtbot):
        """Calling set_state with the same state twice is a no-op for
        the animations — _update_row fires every refresh tick so we
        can't restart animations on every call (would visibly reset
        the wave phase).

        Stronger assertion than ``>=`` — currentTime must have
        advanced by more than the wait window, otherwise a buggy
        restart-from-0 would slip through (currentTime() == 0 still
        satisfies >= 0)."""
        from claude_island.ui.expanded_window import _RowStatusGlyph
        g = _RowStatusGlyph()
        qtbot.addWidget(g)
        g.set_state(_RowStatusGlyph.STATE_RUNNING)
        anim = g._anims[0]
        time_before = anim.currentTime()
        # Wait long enough that a no-restart anim definitely advances
        # past time_before + 20 ms but a restart-from-0 wouldn't.
        qtbot.wait(80)
        g.set_state(_RowStatusGlyph.STATE_RUNNING)
        assert anim.currentTime() > time_before + 20

    def test_idle_dot_color_picked_by_caller(self, qtbot):
        """IDLE state respects the caller-supplied freshness colour —
        the row uses _activity_color(last_activity) so old sessions
        get the grey dot and recent ones get green."""
        from claude_island.ui.expanded_window import _RowStatusGlyph
        g = _RowStatusGlyph()
        qtbot.addWidget(g)
        g.set_state(_RowStatusGlyph.STATE_IDLE, dot_color="#facc15")
        assert g._dot_color == "#facc15"

    def test_idle_visible_default_true(self, qtbot):
        """Default constructor leaves the IDLE-state dot visible —
        capsule pill needs the slot to never read empty."""
        from claude_island.ui.expanded_window import _RowStatusGlyph
        g = _RowStatusGlyph()
        qtbot.addWidget(g)
        assert g._idle_visible is True

    def test_set_idle_visible_false(self, qtbot):
        """Row mode toggles IDLE dot off — paintEvent's IDLE branch
        becomes a no-op so the leftmost slot stays empty unless the
        glyph transitions to RUNNING. Widget keeps its 12 px width
        either way so the row layout doesn't shift."""
        from claude_island.ui.expanded_window import _RowStatusGlyph
        g = _RowStatusGlyph()
        qtbot.addWidget(g)
        g.set_idle_visible(False)
        assert g._idle_visible is False
        # Width is the layout-alignment contract — must stay constant
        # regardless of idle visibility.
        assert g.width() >= _RowStatusGlyph._MIN_SLOT_W

    def test_row_glyph_starts_with_idle_invisible(self, qtbot, panel):
        """The panel rows opt in to "scheme 2" by calling
        set_idle_visible(False) at construction time — the only
        sessions with anything painted in the leftmost slot are the
        ones that are currently running."""
        panel._render_sessions([_session(1, "/a", ago_minutes=10)])
        glyph = panel._rows[1]._status_glyph
        assert glyph._idle_visible is False


# TestRefreshRowStates removed — refresh_row_states was a Phase E
# workaround for the bug fixed by the Snapshotter architecture
# (Phase G1+). Equivalent coverage now lives in
# tests/ui/test_render_snap.py:
#   - test_render_replaces_session_list_on_subsequent_call:
#       successive renders update row state correctly
#   - test_render_preserves_row_widget_when_pid_persists:
#       cached HoverRow instance survives across renders


class TestHoverRevealRowLayoutStability:
    """Hover-reveal must not shift the surrounding layout.

    The naive ``widget.hide()`` Qt does pulls the hidden widget out
    of layout sizing (default ``QSizePolicy.retainSizeWhenHidden=False``),
    so on hover the sibling label loses ~20 px and — if it's a wrapped
    label sitting near the wrap boundary — reflows onto a second line,
    growing the row taller and pushing every widget below it down.
    The visible symptom is "the panel stretches and the Resume button
    jumps when I mouse over a row".

    ``_HoverRevealRow.register_reveal`` must therefore flip
    ``retainSizeWhenHidden`` to True on every registered widget, so
    its slot stays reserved at all times and only visibility — not
    layout — changes on hover.
    """

    def test_registered_widget_retains_size_when_hidden(self, qtbot):
        from PySide6.QtWidgets import QPushButton
        from claude_island.ui.expanded_window import _HoverRevealRow

        row = _HoverRevealRow()
        qtbot.addWidget(row)
        btn = QPushButton("↗", parent=row)
        btn.setFixedWidth(16)

        row.register_reveal(btn)

        assert btn.isHidden()
        assert btn.sizePolicy().retainSizeWhenHidden() is True

    def test_recents_drawer_reveal_buttons_retain_size(self, qtbot):
        """End-to-end: every hover-reveal button in the RecentsDrawer
        preview (cwd ↗ + uuid ⧉) must have retainSizeWhenHidden set,
        so hovering over a row never shifts the Resume button or
        stretches the panel. Pin the contract on the actual surface
        users see — not just the helper class — so a future hover
        row added without going through ``register_reveal`` would
        be caught by this test as well."""
        from PySide6.QtWidgets import QPushButton
        from claude_island.ui.recents_drawer import RecentsDrawer
        from claude_island.core.launch_intent import LaunchIntentRegistry
        # Reuse fixtures from the recents drawer test module — same
        # dispatcher and dormant-session helpers as everywhere else.
        from tests.ui.test_recents_drawer import (
            _dormant, _empty_snap, _FakeDispatcher,
        )

        parent = QWidget()
        qtbot.addWidget(parent)
        d = RecentsDrawer(
            expanded=parent,
            dispatcher=_FakeDispatcher(),
            launch_intent=LaunchIntentRegistry(),
            on_wake=lambda: None,
        )
        qtbot.addWidget(d)
        d.render(_empty_snap(dormant=[_dormant("u1")]))
        d._select_uuid("u1")

        # The two glyph buttons live inside _HoverRevealRow instances
        # in the preview — find them via their pinned glyph text.
        glyph_btns = [
            b for b in d._preview_container.findChildren(QPushButton)
            if b.text() in ("↗", "⧉")
        ]
        # ↗ on cwd row + ⧉ on uuid row.
        assert len(glyph_btns) == 2
        for b in glyph_btns:
            assert b.sizePolicy().retainSizeWhenHidden() is True, (
                f"reveal button {b.text()!r} loses its layout slot when "
                "hidden — hover will reflow surrounding widgets"
            )


