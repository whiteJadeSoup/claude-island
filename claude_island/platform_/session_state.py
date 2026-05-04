"""Read-only access to Claude Code's per-process state files.

Claude Code writes ``~/.claude/sessions/<pid>.json`` for every running
session, with fields like ``status`` ("idle"/"busy"/"waiting"),
``name`` (the human slug), ``startedAt`` (epoch ms), ``version``, etc.
We use these for the hover tooltip — they're more authoritative than
anything we could infer from process listings or transcript timestamps.

Cheap in-memory cache keyed by pid with a 5 s TTL: tooltips fire
on every mouse hover and we don't want to re-read disk N times per
second on idle. The TTL is short enough that ``status`` flips from
"idle" to "busy" appear within a tooltip's natural delay.
"""
from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

# Default location, overridable for tests.
_DEFAULT_DIR = Path.home() / ".claude" / "sessions"
# Cache TTL for parsed state. Originally 5s — fine for tooltip-hover
# rate-limiting but visibly stale for the row-state pipeline: when
# Claude finishes a turn, the JSONL write wakes the snapshotter
# immediately, but compose_session_view re-reads state from this
# cache; if the cached "busy" was <5s old the row stayed "busy" for
# up to 5s after the user-visible event. 1s is short enough that
# busy↔idle flips appear within a tick, and ~20 sessions × ~10ms
# per disk read once per second is still cheap (well under 1% CPU).
_TTL_SECONDS = 1.0

_cache: dict[int, tuple[float, dict | None]] = {}
_cache_lock = threading.Lock()


def read_session_state(
    pid: int, *, sessions_dir: Path = _DEFAULT_DIR,
) -> dict | None:
    """Return the parsed ``sessions/<pid>.json`` for the given pid.

    Returns ``None`` when the file is missing, unreadable, or malformed
    — never raises. Callers (the hover tooltip composer) treat ``None``
    as "no extra metadata available, render what we already have".

    Cached for :data:`_TTL_SECONDS` per pid so a flurry of hover events
    on the same row doesn't re-read disk for each one.
    """
    now = time.monotonic()
    with _cache_lock:
        cached = _cache.get(pid)
        if cached is not None and (now - cached[0]) < _TTL_SECONDS:
            return cached[1]

    path = sessions_dir / f"{pid}.json"
    parsed: dict | None = None
    try:
        text = path.read_text(encoding="utf-8")
        data = json.loads(text)
    except (OSError, ValueError):
        data = None
    if isinstance(data, dict):
        parsed = data

    with _cache_lock:
        _cache[pid] = (now, parsed)
    return parsed


def parse_started_at(raw: object) -> datetime | None:
    """Convert ``startedAt`` (epoch milliseconds) to a UTC datetime.

    Tolerant: ``raw`` may be int, float, str-of-digits, or None / wrong
    type. Returns None on anything we can't parse cleanly.
    """
    try:
        ms = int(raw)  # accepts int, float, "1777620018418"
    except (TypeError, ValueError):
        return None
    if ms <= 0:
        return None
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc)


def invalidate_cache(pid: int) -> None:
    """Drop the cached state entry for a single ``pid``.

    Called by the sessions/ watchdog (wired in ``__main__.py``) when
    ``~/.claude/sessions/<pid>.json`` is modified or created — Claude
    Code writes this file on every state flip (idle → busy → idle),
    so an event-driven invalidation makes the next read return the
    fresh "busy" / "waiting" / "idle" instead of a TTL-stale "idle"
    that would keep the row marked as not-running for up to
    ``_TTL_SECONDS`` more.

    Single-pid granularity by design — never touches other pids'
    cached entries. A flip on one session must not pay the cost of
    re-reading every other session's state file on the next
    snapshotter wake; just ``pop(pid, None)`` and let the rest
    stay warm.

    Idempotent on a missing key. Thread-safe via the same module
    lock that guards ``read_session_state``."""
    with _cache_lock:
        _cache.pop(pid, None)


def reset_cache_for_tests() -> None:
    """Drop the cache. Tests use this between cases so a stale entry
    from one test doesn't leak into the next."""
    with _cache_lock:
        _cache.clear()
