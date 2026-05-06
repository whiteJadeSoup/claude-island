"""Track which sentinel-titled sessions share a Windows Terminal tab.

The split-pane disambiguation problem
-------------------------------------
WT renders 2+ panes inside one tab as a single TabItem whose
``Name`` reflects only the *active* pane's console title (mirrored
from OSC 0/2). The inactive pane's sentinel never appears in any
TabItem.Name, so an external ``select_tab_by_title(inactive_sentinel)``
matches nothing and the click silently does nothing.

Microsoft has Won't-Fix'd exposing the conpty→TabItem mapping (issue
#5694), and WinUI3 lazy-loads inactive *tabs* — so the only stable
external observation we get is: while a tab is the active tab, the
UIA tree under it contains every TermControl in that tab (including
the inactive panes), each carrying its own sentinel as TermControl.Name.

This module exploits that observation. Whenever a tab becomes the
active tab and we get a chance to look (ProcessScanner wake, or
click-time fire-and-forget), we enumerate that tab's TermControl
sentinels and record the pairwise sibling relationships. Future
clicks on a sentinel that no longer matches any TabItem.Name (because
its pane became inactive in its tab) try its cached siblings instead;
one of them is the active sibling whose sentinel IS in TabItem.Name,
and selecting it brings WT to the right tab.

Cache discipline
----------------
- **Full replace on update** — never union. If a pane gets closed,
  the next observation of its tab will see only the surviving
  sentinels and overwrite the stale entry. Union would leave dead
  sentinels lingering forever.
- **In-memory only** — rebuilt from observation as the user
  interacts with WT. WT process restart drops every cached pane
  anyway (claude.exe inside it dies too), so persistence buys
  nothing.
- **Lock-protected** — `update_from_active_tab` runs on the
  snapshotter worker thread; `siblings_of` runs on the Qt main
  thread (during click). A simple `threading.Lock` serialises
  read-vs-write of the dict.
- **Single-flight refresh** — `schedule_update` is fire-and-forget;
  if a refresh is already in flight, additional calls are dropped.
  Click bursts thus produce at most one outstanding refresh thread,
  not N.
"""
from __future__ import annotations

import os
import sys
import threading
from collections.abc import Callable
from typing import Protocol


# Set CLAUDE_ISLAND_FOCUS_DEBUG=1 to dump every cache update + click
# decision to stderr. Used to trace why a click didn't land — DO NOT
# leave on in production (one stderr line per wake per WT window).
_DEBUG = os.environ.get("CLAUDE_ISLAND_FOCUS_DEBUG") == "1"

# PowerShell on Windows defaults stderr to UTF-16 LE — when redirected
# (`2> file.log`) the text comes out with U+0000 spacing every other
# byte and is unreadable. Force stderr to UTF-8 so logs are usable.
if _DEBUG and sys.platform == "win32":
    try:
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass


def _dbg(msg: str) -> None:
    if _DEBUG:
        print(f"[ci-focus] {msg}", file=sys.stderr, flush=True)


class _EnumerateProto(Protocol):
    """Signature of the UIA enumeration callable."""
    def __call__(self, hwnd: int) -> set[str]: ...


class PaneSiblingTracker:
    """Sentinel → set of co-tab sibling sentinels, learned by
    observing active tabs over time."""

    def __init__(
        self,
        enumerate_fn: _EnumerateProto | None = None,
    ) -> None:
        # Default: real UIA enumeration. Tests inject a stub.
        if enumerate_fn is None:
            from claude_island.platform_ import wt_uia
            enumerate_fn = wt_uia.enumerate_active_tab_sentinels
        self._enumerate = enumerate_fn

        # Read on Qt main thread (siblings_of from focus path); write
        # on snapshotter worker (update_from_active_tab from group())
        # AND on a fire-and-forget refresh thread (schedule_update).
        # Lock serialises all dict access.
        self._lock = threading.Lock()
        self._siblings: dict[str, set[str]] = {}

        # Single-flight: ensure at most one fire-and-forget refresh
        # is in flight per process. Bursts of clicks at the same
        # row don't spawn N threads. acquire(blocking=False) returns
        # False when one is already running, in which case we drop.
        self._refresh_lock = threading.Lock()

    def siblings_of(self, sentinel: str) -> set[str]:
        """Return the cached set of sibling sentinels for *sentinel*.

        Returns a fresh copy so the caller can iterate without holding
        the lock; the original set may be replaced by a concurrent
        update_from_active_tab. Empty set on cache miss.
        """
        with self._lock:
            result = set(self._siblings.get(sentinel, ()))
            cache_size = len(self._siblings)
        _dbg(
            f"siblings_of({sentinel!r}) → {result!r} "
            f"(cache has {cache_size} sentinels)"
        )
        return result

    def update_from_active_tab(self, wt_hwnd: int) -> None:
        """Synchronously enumerate *wt_hwnd*'s active tab and rewrite
        the sibling entries for every sentinel observed.

        Full replace, no union — closed panes drop out as soon as
        their tab is observed. If the active tab contains zero
        ``ci:*`` sentinels (e.g. PowerShell tab, or a non-claude
        tab is currently active), no entries are touched — we don't
        know anything new.

        Safe to call from any thread; the underlying UIA library
        is COM-marshalled.
        """
        sentinels = self._enumerate(wt_hwnd)
        _dbg(f"update_from_active_tab(hwnd={hex(wt_hwnd)}) → {sentinels!r}")
        if not sentinels:
            return
        with self._lock:
            for s in sentinels:
                self._siblings[s] = sentinels - {s}

    def schedule_update(self, wt_hwnd: int) -> None:
        """Fire-and-forget refresh of *wt_hwnd*'s active tab on a
        background thread. Returns immediately.

        Used at click time when the cache turned out stale: we don't
        want to block the Qt event loop on a 50–150ms UIA BFS, but
        we DO want the cache fresh for the next click. Single-flight
        prevents click bursts from spawning multiple threads.
        """
        if not self._refresh_lock.acquire(blocking=False):
            return  # already running; another caller covers us
        def _run() -> None:
            try:
                self.update_from_active_tab(wt_hwnd)
            except Exception as exc:
                # Don't let a UIA hiccup tear down the daemon thread
                # silently — surface it for diagnosis.
                print(
                    f"[claude-island] PaneSiblingTracker refresh failed: {exc}",
                    file=sys.stderr,
                )
            finally:
                self._refresh_lock.release()
        threading.Thread(target=_run, daemon=True).start()
