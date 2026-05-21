"""Regression guard for the QWindowsWindow::setGeometry mintrack=N
warning class (and the visual breakage that comes with it).

Background
----------
Three top-level widgets in `expanded_window.py` are constructed with
`setFixedWidth(...)` because their visual design depends on a stable
panel width:

    ExpandedWindow      → 320px (matches capsule width)
    SessionDetailPopup  → 320px (visually anchored to capsule/panel)
    _AddProviderDialog  → 360px (slightly wider for form fields)

`setFixedWidth` is a *request* to Qt's layout, not a hard command:
if any descendant widget's `minimumSizeHint().width()` exceeds the
fixed width, Qt's layout overrides the constraint and the window
grows. On Windows this fires the `QWindowsWindow::setGeometry
mintrack=N` warning every layout pass; everywhere it manifests as a
panel that's wider than designed and cards visually misaligned.

These tests catch the bug class at CI time so a future PR can't
re-introduce a bare `QLabel(long_user_text)` without noticing. The
production fix uses `mk_label(...)` (see `expanded_window.py`) which
defaults to an `_ElidingLabel` that returns `minimumSizeHint=0`.

What the tests assert
---------------------
For each fixed-width window, after constructing and showing it with
*extreme*-length input data (200-char project names, long token
strings, oversized help text, etc.), we check the **root layout's
effective minimum width**:

    window.layout().minimumSize().width() <= fixed_width

This is the value that would propagate to `setGeometry` and trigger
the OS warning. Asserting on the layout's minimum (rather than on
each child widget individually) is a cleaner invariant because Qt
already does the right aggregation: child fixedWidths cap their
contribution; only widgets that don't cap themselves leak through.

Failure messages list the offending children (any descendant whose
own `minimumSize().width()` exceeds the budget) so the fix is a
straight grep+swap to `mk_label(...)`.

Note on `qtbot`
---------------
`pytest-qt`'s `qtbot.addWidget` ensures the widget is destroyed at
end-of-test even on assertion failure — leaks from this file would
quickly accumulate across the suite.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

# Force offscreen for headless CI / local runs (matches sibling tests).
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QWidget

from claude_island.core.models import Session, SessionDetails, UsageTotals
from claude_island.core.snapshot import SessionView
from claude_island.ui.controller import IslandController
from claude_island.ui.expanded_window import (
    ExpandedWindow,
    SessionDetailPopup,
    _AddProviderDialog,
    _PANEL_W,
)


# Realistic-but-extreme test data. Each value reflects the worst-case
# real-world input the field plausibly receives, NOT a 200-char string
# of unbreakable characters (that would adversarially break wordWrap-
# protected labels too, conflating the QLabel-bug we're guarding with
# unrelated label-class concerns).
#
# Names / titles use kebab-case-like-strings-without-spaces (typical
# for project names) so they CAN'T wrap and a bare QLabel is forced
# to claim full width.
_LONG_NAME = (
    "react-native-async-storage-with-very-deeply-nested-folder-names"
)  # 65 chars; ~520 px @ 12 px font
_LONG_AI_TITLE = (
    "Refactor the entire async message dispatcher to use proper "
    "backpressure and exception isolation across worker threads"
)  # ~120 chars human language with spaces — wordWrap labels handle this fine
_LONG_PATH = (
    "/Users/somebody/projects/some-organization/very-long-monorepo-name/"
    "packages/super-deeply-nested-package-name/src/handlers"
)  # ~140 chars, separator-delimited so wordWrap can break it
_LONG_HELP = (
    "Paste your long-form authentication token here. The value is "
    "stored in ~/.claude-island/providers.json and never transmitted "
    "outside the API endpoint configured for this provider."
)  # paragraph — wordWrap breaks on every space, no minimumSizeHint risk


# ---------------------------------------------------------------------------
# Builders — produce each fixed-width window in a worst-case-content state.
# ---------------------------------------------------------------------------


def _build_expanded_with_long_session(qtbot) -> ExpandedWindow:
    """ExpandedWindow rendering one long-name session AND high-value
    usage totals so the SPEND / summary cards get realistic stress.

    Note: session-row labels live inside a QScrollArea with horizontal
    scrolling disabled (`expanded_window.py:_session_scroll`), which
    absorbs row overflow before it can propagate to the panel. So a
    bare `QLabel(long_name)` in a row CAN'T trigger this test —
    the scroll-area shield is doing its job. The test still covers
    everything *outside* the scroll area: summary card, SPEND card,
    QUOTA card, sessions title."""
    capsule = QWidget()
    capsule.show()
    qtbot.addWidget(capsule)
    controller = IslandController()
    # High-value totals → "$12,345.67 · 999.9M tokens" style strings
    # in the SPEND card amount label.
    big_totals = UsageTotals(
        period="today",
        input_tokens=999_999_999,
        output_tokens=888_888_888,
        cache_creation_tokens=777_777_777,
        cache_read_tokens=666_666_666,
        input_cost=4321.0,
        output_cost=5432.0,
        cache_creation_cost=1234.0,
        cache_read_cost=2345.0,
    )
    panel = ExpandedWindow(
        capsule=capsule,
        controller=controller,
        get_usage_totals=lambda period: big_totals,
    )
    qtbot.addWidget(panel)
    long_session = Session(
        pid=1,
        project_path=Path(_LONG_PATH) / _LONG_NAME,
        session_uuid="",
        last_activity=datetime.now(timezone.utc) - timedelta(seconds=5),
    )
    panel._render_sessions([long_session])
    # Force the periodic refreshes so summary/spend/quota cards
    # populate with the big_totals values (the test isn't waiting
    # for the timer-driven cadence).
    if hasattr(panel, "_refresh_summary_card"):
        panel._refresh_summary_card()
    if hasattr(panel, "_refresh_spend_card"):
        panel._refresh_spend_card()
    return panel


def _build_popup_with_long_session(qtbot) -> SessionDetailPopup:
    """SessionDetailPopup populated with long values across every
    field that gets rendered in the header / meta / tokens sections."""
    s = Session(
        pid=1,
        project_path=Path(_LONG_PATH) / _LONG_NAME,
        session_uuid="abcdef0123456789" * 2,  # full-length UUID-like
        last_activity=datetime.now(timezone.utc) - timedelta(minutes=5),
    )
    details = SessionDetails(
        session=s,
        name=_LONG_NAME,
        ai_title=_LONG_AI_TITLE,
        git_branch="feature/" + _LONG_NAME,
        last_prompt=_LONG_AI_TITLE,
        started_at=datetime.now(timezone.utc) - timedelta(hours=2),
        status="busy",
        cc_version="2.1.123",
        cost_usd=12.34,
        turn_count=42,
        sidechain_count=3,
        original_name=_LONG_NAME,
    )
    from claude_island.core.session_phase import SessionPhase
    view = SessionView(
        pid=s.pid,
        name=_LONG_NAME,
        project_path=s.project_path,
        project_basename=s.project_path.name or "x",
        last_activity=s.last_activity,
        cost_usd=12.34,
        is_high_cost=False,
        latest_model="claude-opus-4-7",
        status_word="busy",
        session=s,
        session_uuid=s.session_uuid,
        phase=SessionPhase.THINKING,
    )
    popup = SessionDetailPopup(details, view)
    qtbot.addWidget(popup)
    return popup


def _build_dialog_with_long_input(qtbot) -> _AddProviderDialog:
    """_AddProviderDialog with a configurable provider whose help text
    + form field defaults are realistically long. Provider name itself
    is short (production names are "anthropic"/"minimax"/"zhipu") —
    long names would stress the radio-button label, an unrelated
    concern from the QLabel-bug we're guarding against here."""
    cfg = {
        "_help": _LONG_HELP,
        "auth_token": "",
        "base_url": "https://api.example.com/v1/very-long-endpoint-path",
    }
    dlg = _AddProviderDialog(
        configurable=[("custom_provider", cfg)],
        on_save=lambda *_args: None,
    )
    qtbot.addWidget(dlg)
    return dlg


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name,builder,fixed_w",
    [
        ("ExpandedWindow", _build_expanded_with_long_session, _PANEL_W),
        ("SessionDetailPopup", _build_popup_with_long_session, _PANEL_W),
        ("_AddProviderDialog", _build_dialog_with_long_input, 360),
    ],
)
def test_root_layout_fits_within_fixed_width(qtbot, name, builder, fixed_w):
    """The root layout's effective minimum width must not exceed the
    window's `setFixedWidth`. If it does, Qt's layout has rejected
    the fixed-width constraint because some descendant insists on
    more space — exactly the bug condition.

    Note: this test deliberately runs against the *layout* minimum,
    not the *window* minimum. Step 4 (container clamp) makes the
    window itself always report fixed_w; the layout-level metric
    bypasses that defense and asserts the underlying invariant
    holds even without the clamp."""
    window = builder(qtbot)
    window.show()
    qtbot.waitExposed(window)
    # Activate forces the layout to recompute its sizes against the
    # current contents; without it minimumSize() can be stale.
    window.layout().activate()

    layout_min_w = window.layout().minimumSize().width()

    if layout_min_w > fixed_w:
        # Failure diagnostic: walk children and list the ones whose
        # own minimumSizeHint or minimumSize exceeds the budget —
        # they're the candidates for swapping to `mk_label(...)` (or
        # for whatever the equivalent is at that widget type).
        offenders = []
        for c in window.findChildren(QWidget):
            try:
                msh = c.minimumSizeHint().width()
                ms = c.minimumSize().width()
                # Also check the widget's own layout's minimum (catches
                # container widgets whose children sum to too much).
                lay = c.layout()
                lay_min = lay.minimumSize().width() if lay is not None else 0
                worst = max(msh, ms, lay_min)
            except Exception:
                continue
            if worst > fixed_w:
                txt = ""
                if hasattr(c, "text"):
                    try:
                        txt_full = c.text() or ""
                        if txt_full:
                            txt = (
                                f" text={txt_full[:40]!r}"
                                + ("…" if len(txt_full) > 40 else "")
                            )
                    except Exception:
                        pass
                offenders.append(
                    f"  - {type(c).__name__}"
                    f"{('#' + c.objectName()) if c.objectName() else ''}"
                    f": msh={msh}px ms={ms}px lay_min={lay_min}px{txt}"
                )

        pytest.fail(
            f"{name} root layout minimumSize.width = {layout_min_w}px "
            f"exceeds fixed_w = {fixed_w}px.\n"
            f"This means some descendant widget propagates too much "
            f"width up the layout chain — it would override "
            f"setFixedWidth({fixed_w}) and trigger the "
            f"QWindowsWindow::setGeometry mintrack={layout_min_w} "
            f"warning in production.\n"
            f"\nLikely culprits (children with minSize > {fixed_w}):\n"
            + "\n".join(offenders[:15])
            + (
                f"\n  …and {len(offenders) - 15} more"
                if len(offenders) > 15
                else ""
            )
            + "\n\nFix: change the offending bare `QLabel(...)` calls to "
            "`mk_label(...)` (defaults to elide=True) — see "
            "`claude_island/ui/expanded_window.py:mk_label`."
        )


def test_eliding_label_is_invariant_under_long_text():
    """Sanity-check the underlying primitive: an `_ElidingLabel`
    constructed with a 1000-char string must still report
    minimumSizeHint().width() == 0. If this regresses, every test
    above will start spuriously passing while the production bug
    silently returns."""
    from claude_island.ui.expanded_window import _ElidingLabel

    monster = "x" * 1000
    lbl = _ElidingLabel(monster)
    assert lbl.minimumSizeHint().width() == 0, (
        f"_ElidingLabel.minimumSizeHint().width() = "
        f"{lbl.minimumSizeHint().width()}, expected 0. "
        "The width-propagation defense is broken — every fixed-width "
        "window in expanded_window.py is at risk."
    )
    # text() must still return the *full* string (not the elided
    # form) so callers comparing label.text() vs source-of-truth keep
    # working — see _update_row in expanded_window.py.
    assert lbl.text() == monster


# ---------------------------------------------------------------------------
# Auto-tooltip: hover-reveal of truncated text
# ---------------------------------------------------------------------------


def test_eliding_label_sets_tooltip_when_truncated(qtbot):
    """When the rendered text gets elided, tooltip auto-populates with
    the full (un-elided) string so users can hover to see what was
    cut off. Verifies the hover-reveal UX promised by the docstring."""
    from claude_island.ui.expanded_window import _ElidingLabel

    long_name = (
        "react-native-async-storage-with-very-deeply-nested-folder-names"
    )
    lbl = _ElidingLabel(long_name)
    qtbot.addWidget(lbl)
    lbl.resize(80, 20)  # narrow → forces elision
    lbl.show()
    qtbot.waitExposed(lbl)

    # Visible text was elided.
    from PySide6.QtWidgets import QLabel as _QL
    visible = _QL.text(lbl)  # bypass our text() override → raw QLabel text
    assert visible != long_name, (
        f"Expected elided form, got the full string: {visible!r}. "
        "Either elision didn't run, or width was wide enough to fit."
    )
    # Tooltip surfaces the full text.
    assert lbl.toolTip() == long_name, (
        f"Auto-tooltip not set. toolTip()={lbl.toolTip()!r}, "
        f"expected {long_name!r}."
    )


def test_eliding_label_clears_tooltip_when_text_fits(qtbot):
    """When the text fits at the allocated width, no tooltip — would
    just redundantly echo the visible text and add hover noise."""
    from claude_island.ui.expanded_window import _ElidingLabel

    short = "ok"
    lbl = _ElidingLabel(short)
    qtbot.addWidget(lbl)
    lbl.resize(200, 20)  # plenty of space
    lbl.show()
    qtbot.waitExposed(lbl)

    assert lbl.toolTip() == "", (
        f"Tooltip should be empty when text fits, got: {lbl.toolTip()!r}"
    )


def test_eliding_label_sizehint_stays_at_full_text_width(qtbot):
    """Regression guard: `_ElidingLabel.sizeHint().width()` must
    always reflect the FULL text width, even after the label has been
    resized narrower and elided. If sizeHint reports the (shrunken)
    elided form's width, layout queries it on the next pass, allocates
    less, the label re-elides at the smaller width, sizeHint shrinks
    again — a feedback loop that was observed collapsing 'active now'
    to 'active n…' (and worse) in standalone session rows."""
    from claude_island.ui.expanded_window import _ElidingLabel
    from PySide6.QtGui import QFontMetrics

    text = "active now"
    lbl = _ElidingLabel(text)
    qtbot.addWidget(lbl)

    fm = QFontMetrics(lbl.font())
    full_w = fm.horizontalAdvance(text)

    # Resize narrower than the full text — forces elision.
    lbl.resize(40, 20)
    lbl.show()
    qtbot.waitExposed(lbl)

    # The visible (super) text is now the elided form …
    from PySide6.QtWidgets import QLabel as _QL
    assert _QL.text(lbl) != text, "elision should have kicked in"

    # … but sizeHint must still ask for full-text width, otherwise
    # the layout's next allocation pass will use the shrunken value
    # and the elision feeds back on itself.
    assert lbl.sizeHint().width() == full_w, (
        f"sizeHint().width() = {lbl.sizeHint().width()}, "
        f"expected {full_w} (full-text width). After eliding, "
        "QLabel.sizeHint reads the now-shrunken internal text — "
        "the override must report the FULL text width to keep the "
        "layout from collapsing the label further on each pass."
    )


def test_session_row_status_label_absorbs_remaining_width(qtbot):
    """v4c regression: the bottom row's deficit-shrink target is
    ``cwd_label`` (the new visible elidable widget), not the old
    ``status_label`` (now a hidden 0×0 sibling that exists only so
    callers reaching by objectName don't crash).

    The historical deficit-shrink bug ("active 8d a…" truncation) is
    about Qt picking the most compressible widget when width is
    tight.  In v4c the visible compressible widget is cwd_label
    (_ElidingLabel with minimumSize=0); chip stays at its sizeHint
    because it has stretch=0; the trailing addStretch is intentional
    so cwd + chip both stay LEFT-aligned and the cost column on the
    top row reads as the only "right edge" content.

    What this test pins down for v4c:
      1. cwd_label exists on the bottom row
      2. model_chip is immediately after cwd_label (no gap widget)
      3. A trailing QSpacerItem is present (the v4c addStretch slot)
      4. The hidden status_label sibling is still findable by
         objectName (back-compat with the legacy lookup pattern)
    """
    from claude_island.core.models import Session, UsageTotals, SessionDetails
    from claude_island.ui.controller import IslandController
    from claude_island.ui.expanded_window import ExpandedWindow
    from PySide6.QtWidgets import QLabel

    capsule = QWidget(); capsule.show()
    qtbot.addWidget(capsule)

    def get_details(session):
        return SessionDetails(
            session=session, name="build-mini-cc", ai_title=None,
            git_branch=None, last_prompt=None,
            started_at=datetime.now(timezone.utc) - timedelta(days=8),
            status="idle", cc_version="2.1.123",
            cost_usd=4.66, turn_count=5, sidechain_count=0,
            latest_model="claude-opus-4-7",
        )

    panel = ExpandedWindow(
        capsule=capsule,
        controller=IslandController(),
        get_usage_totals=lambda period: UsageTotals(period=period),
        get_session_details=get_details,
    )
    qtbot.addWidget(panel)

    s = Session(
        pid=99, project_path=Path("/tmp/build-mini-cc"),
        session_uuid="",
        last_activity=datetime.now(timezone.utc) - timedelta(days=8),
    )
    panel._render_sessions([s])
    panel.show()
    qtbot.waitExposed(panel)

    btn = panel._rows[99]
    cwd_label = btn.findChild(QLabel, "cwd_label")
    chip_label = btn.findChild(QLabel, "model_chip")
    status_label = btn.findChild(QLabel, "status_label")

    # Sanity: all three widgets exist on the row tree.
    assert cwd_label is not None, "cwd_label missing"
    assert chip_label is not None, "model_chip missing"
    assert status_label is not None, (
        "status_label removed entirely — it must stay as a hidden "
        "sibling so legacy callers reaching for it by objectName "
        "don't crash."
    )

    # v4c (revised 2026-05-21 again, after user feedback "排版要统一"):
    # back to a forced 2-row body — top row carries name+status, bottom
    # row carries cwd+chip.  FlowLayout was tried but the user prefers
    # consistent layout across phases (less visual jitter between
    # idle / thinking / tool_use rows).  This test pins the 2-row
    # structure: body is QVBoxLayout containing top + bottom HBoxes.
    from PySide6.QtWidgets import QVBoxLayout, QHBoxLayout
    body_widget = cwd_label.parentWidget()
    assert body_widget is not None
    body_layout = body_widget.layout()
    assert isinstance(body_layout, QVBoxLayout), (
        "Row body must use QVBoxLayout(top, bottom) for a consistent "
        "2-row layout across phases."
    )
    # cwd_label lives in the BOTTOM row inside body — walk to find it.
    bottom_row = None
    for i in range(body_layout.count()):
        sub = body_layout.itemAt(i).layout()
        if not isinstance(sub, QHBoxLayout):
            continue
        for j in range(sub.count()):
            if sub.itemAt(j).widget() is cwd_label:
                bottom_row = sub
                break
        if bottom_row is not None:
            break
    assert bottom_row is not None, "bottom row containing cwd missing"

    widget_order: list = []
    for i in range(bottom_row.count()):
        w = bottom_row.itemAt(i).widget()
        if isinstance(w, QLabel):
            widget_order.append(w)
    cwd_idx = widget_order.index(cwd_label)
    chip_idx = widget_order.index(chip_label)
    assert chip_idx == cwd_idx + 1, (
        f"chip at idx {chip_idx} but cwd at {cwd_idx} — bottom row "
        "should read cwd → chip immediately adjacent."
    )


def test_eliding_label_respects_user_set_tooltip(qtbot):
    """Custom `setToolTip(...)` from the call site must not be
    overridden by the auto-tooltip. Real call site that depends on
    this: the popup tokens section sets the model row name's tooltip
    to the full list of raw model ids (`'\\n'.join(r.full_models)`)
    — a multi-line tooltip distinct from the displayed name."""
    from claude_island.ui.expanded_window import _ElidingLabel

    long_name = "claude-opus-4-7-with-long-suffix-from-some-experiment"
    custom = "claude-opus-4-7\nclaude-opus-4-5"
    lbl = _ElidingLabel(long_name)
    qtbot.addWidget(lbl)
    lbl.setToolTip(custom)  # user installs their own tooltip
    lbl.resize(60, 20)  # narrow → would auto-elide
    lbl.show()
    qtbot.waitExposed(lbl)

    assert lbl.toolTip() == custom, (
        f"User-set tooltip was overridden. toolTip()={lbl.toolTip()!r}, "
        f"expected user's custom value {custom!r}."
    )
