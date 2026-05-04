"""Pure sort + filter helpers for the Recents drawer.

These live in ``ui/`` (not ``core/``) on purpose. Sort key and matched
fields are *presentation decisions* — the user could plausibly switch
to "by cost" or "only search cwd" tomorrow, and that change shouldn't
ripple through core's data contracts. They operate on core types
(``DormantSession``) but encode no domain rules.

Written as module-level pure functions, not methods on the widget,
so:

* tests can exercise them directly without ``pytest-qt``
* the widget code reads as pure layout / event wiring (no business logic
  hidden in private helpers)
* future moves (e.g. into a ``ui/preferences/`` module when these become
  user settings) are a one-line import change
"""
from __future__ import annotations

from typing import Iterable

from claude_island.core.models import DormantSession


def sort_by_recency(
    dormant: Iterable[DormantSession],
) -> list[DormantSession]:
    """Newest ``last_activity`` first. Stable for ties.

    Returns a new list; the input is not mutated. Stability matters for
    ties because ``last_activity`` resolution is millisecond-grained — two
    sessions that finished within the same millisecond should preserve
    whatever order ``DormantSessionSource`` produced (typically filesystem
    glob order, which is at least deterministic per-platform).
    """
    return sorted(dormant, key=lambda d: d.last_activity, reverse=True)


def search_haystack(d: DormantSession) -> str:
    """Concatenated text the search query is matched against.

    Exposed so tests + future ranking algorithms can reason about the
    matched-fields decision without re-implementing it. Field choice:

    * ``name`` — the user's mental anchor when they remember the session
    * ``last_prompt`` — the most natural recall pattern ("the one where
      I asked about X")
    * ``cwd`` — narrows to a project; lowercased so case-folded match works
    * ``git_branch`` — "all my work-on-feat-X sessions"
    * ``session_uuid`` — full uuid (not just first 8) so power users can
      paste a uuid from logs and find the row

    Concatenation with single-space separators is enough — the query
    itself never contains substrings spanning a separator (no real query
    has a space-then-letter pattern that crosses fields).
    """
    return " ".join([
        (d.name or "").lower(),
        (d.last_prompt or "").lower(),
        str(d.cwd).lower(),
        (d.git_branch or "").lower(),
        d.session_uuid.lower(),
    ])


def filter_by_query(
    dormant: Iterable[DormantSession],
    query: str,
) -> list[DormantSession]:
    """Case-insensitive substring filter.

    * Empty / whitespace-only ``query`` returns the input unchanged
      (as a list copy — same semantics as the no-filter render path).
    * Order is preserved: callers usually pass already-sorted input
      and expect the filter to be a pure subset-in-place operation.
    """
    q = query.strip().lower()
    if not q:
        return list(dormant)
    return [d for d in dormant if q in search_haystack(d)]
