"""UI text formatting — pure functions for display strings.

Lives in ``core/`` (not ``ui/``) because it's the **single source of
truth for what UI shows**, used by two distinct callers:

* UI render code (``ui/expanded_window.py``, ``ui/capsule_window.py``):
  formats values to display in widgets.

* Per-surface ``compute(snap) → tuple`` selectors (the F4 dedup
  refactor): formats values into the dedup key. dedup precision MUST
  equal display precision — same function in both call sites
  guarantees that automatically. If the format ever changes ("now"
  threshold from 5s to 10s, money threshold from $1000 to $10K),
  both UI and dedup follow it without manual sync.

These functions are intentionally pure (no Qt, no I/O, no global
state mutation) and stable across calls — `_fmt_started(ts)` returns
the same string for the same ``ts`` plus current time bucket. No
caching here: the bucket boundaries themselves provide the dedup
window; caching would only help if the same exact ``ts`` were
formatted many times per second, which it isn't.

Three-layer architecture: this module is part of ``core/`` because:

* No Qt / PySide6 dependencies (just stdlib datetime).
* No platform_ dependencies.
* Imported by both ``core/snapshot.py`` (compute selectors) and
  ``ui/*.py`` (render code) — must sit in the layer both can read.
"""
from __future__ import annotations

from datetime import datetime, timezone


def fmt_started(dt: datetime | None) -> str:
    """Compact relative-time string ("1h 45m ago").

    For very fresh activity (under 5s) returns ``"now"`` rather than
    ``"0s ago"`` / ``"3s ago"`` — those tiny numbers tick chaotically
    on every Snapshotter rebuild for an actively-running session and
    read as a bug. The pulse glyph + accent bar already convey "live
    right now"; the text just needs to NOT contradict that.

    The ``< 5s → "now"`` boundary doubles as the dedup quantisation
    edge: any ``last_activity`` change within a 5s window produces the
    same string, so dedup correctly skips no-op renders during an
    active session's microsecond-precision JSONL writes.
    """
    if dt is None:
        return "—"
    delta = datetime.now(timezone.utc) - dt.astimezone(timezone.utc)
    s = int(delta.total_seconds())
    if s < 5:
        return "now"
    if s < 60:
        return f"{s}s ago"
    if s < 3600:
        return f"{s // 60}m ago"
    if s < 86400:
        return f"{s // 3600}h {(s % 3600) // 60}m ago"
    return f"{s // 86400}d ago"


def fmt_money(amount: float) -> str:
    """Compact money formatting that switches precision by magnitude.

    < $0.01   → "$0.001" (preserve some signal)
    < $10     → "$1.23"
    < $1000   → "$123"
    otherwise → "$1.2K"

    Each magnitude band is a dedup quantisation edge: any cost change
    that lands in the same band produces the same string, so a
    streaming session's per-message $0.0001 ticks don't trigger
    re-renders.
    """
    if amount < 0.01:
        return f"${amount:.3f}"
    if amount < 10:
        return f"${amount:.2f}"
    if amount < 1000:
        return f"${amount:.0f}"
    return f"${amount / 1000:.1f}K"
