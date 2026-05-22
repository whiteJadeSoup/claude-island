"""Unit tests for ``_elide_path_segments`` — the path-aware elision
helper that powers the cwd label in expanded session rows.

These tests exercise the algorithm directly with ``len`` as the
``advance`` function so widths can be expressed in characters and the
expected outputs are deterministic, no QFontMetrics or QApplication
needed.

Why the helper exists: char-level ``ElideRight`` on a typical cwd
truncates the basename (``~/workProject/origin/made`` →
``~/workProject/origin/ma…``), which is exactly the segment users
need to read to identify a session. Segment-aware elision keeps the
basename and collapses the middle (``~/workProject/…/made``).
"""
from __future__ import annotations

from claude_island.ui.expanded_window import _elide_path_segments


def _advance(s: str) -> int:
    """Char-count "width" — one unit per code point."""
    return len(s)


def test_returns_full_when_it_fits():
    assert _elide_path_segments("~/wp/made", width=999, advance=_advance) == "~/wp/made"


def test_empty_string_is_returned_unchanged():
    assert _elide_path_segments("", width=0, advance=_advance) == ""


def test_keeps_max_leading_segments_that_fit():
    # 4 segments. Full = 24 chars, doesn't fit at 22.
    # keep=2 → "~/workProject/…/made" = 20 chars — fits.
    out = _elide_path_segments(
        "~/workProject/origin/made", width=22, advance=_advance,
    )
    assert out == "~/workProject/…/made"


def test_drops_to_minimal_form_when_tight():
    # Same path, width=15. keep=2 (20 chars) doesn't fit; keep=1
    # ("~/…/made" = 8 chars) does.
    out = _elide_path_segments(
        "~/workProject/origin/made", width=15, advance=_advance,
    )
    assert out == "~/…/made"


def test_returns_none_when_even_minimal_form_overflows():
    # Width=5 can't hold "~/…/made" (8 chars). Caller falls back to
    # char-level middle elision.
    out = _elide_path_segments(
        "~/workProject/origin/made", width=5, advance=_advance,
    )
    assert out is None


def test_returns_none_for_paths_under_three_segments():
    # No middle to elide — caller should fall back to ElideMiddle.
    assert _elide_path_segments("a/b", width=1, advance=_advance) is None
    assert _elide_path_segments("~", width=0, advance=_advance) is None


def test_absolute_path_preserves_leading_slash():
    # "/opt/meituan/made/dependency".split("/") = ["", "opt", "meituan",
    # "made", "dependency"]. Full = 28 chars. At width=27, keep=3 →
    # "/opt/meituan/…/dependency" = 25 chars fits, and the leading
    # slash is preserved because parts[0] is "".
    out = _elide_path_segments(
        "/opt/meituan/made/dependency", width=27, advance=_advance,
    )
    assert out == "/opt/meituan/…/dependency"


def test_picks_longest_leading_prefix_that_fits():
    # Same 5-segment path, width=20. keep=3 (25 chars) doesn't fit;
    # keep=2 → "/opt/…/dependency" = 17 chars fits. Loop must NOT
    # stop at the first fit on the way up — it iterates max-to-min so
    # the first fit IS the longest.
    out = _elide_path_segments(
        "/opt/meituan/made/dependency", width=20, advance=_advance,
    )
    assert out == "/opt/…/dependency"


def test_short_path_that_already_fits_is_unchanged():
    # Realistic case from the screenshot: third row already fits.
    out = _elide_path_segments(
        "~/workProject/claude-island", width=999, advance=_advance,
    )
    assert out == "~/workProject/claude-island"
