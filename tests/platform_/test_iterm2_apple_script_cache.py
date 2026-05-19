"""Unit tests for AppleScriptCache state machine.

Tests cache lifecycle: UNCOMPILED → READY → FAILING → INVALIDATED →
UNCOMPILED transitions per §3 design.

Strategy: patch the module-level ``_NSAppleScript`` symbol with a
controllable fake so we can simulate compile success / failure without
hitting real Foundation. Tests run on any OS — no PyObjC required.
"""
from __future__ import annotations

from unittest import mock

import pytest

from claude_island.platform_.terminals import _iterm_fast_path as fp


# ── Fakes ─────────────────────────────────────────────────────────────


class _FakeNSAppleScript:
    """Mimics enough of NSAppleScript for cache tests.

    The compileAndReturnError_ method follows the PyObjC out-pointer
    convention (returns ``(ok, err)`` tuple). ``compile_ok`` controls
    success; ``error_dict`` is returned on failure.
    """

    def __init__(self, source: str, *, compile_ok: bool = True,
                 error_dict: dict | None = None) -> None:
        self.source = source
        self._compile_ok = compile_ok
        self._error_dict = error_dict or {
            "NSAppleScriptErrorMessage": "fake compile error",
        }

    def compileAndReturnError_(self, _err_ptr):
        if self._compile_ok:
            return (True, None)
        return (False, self._error_dict)


class _FakeNSAppleScriptFactory:
    """Stand-in for the NSAppleScript class. ``.alloc().initWithSource_(s)``
    returns a fresh ``_FakeNSAppleScript`` instance."""

    def __init__(self, *, compile_ok: bool = True,
                 error_dict: dict | None = None) -> None:
        self.compile_ok = compile_ok
        self.error_dict = error_dict
        self.compile_calls = 0

    def alloc(self):
        return self  # alloc returns the class-like; initWithSource_ then instantiates

    def initWithSource_(self, source: str) -> _FakeNSAppleScript:
        self.compile_calls += 1
        return _FakeNSAppleScript(
            source,
            compile_ok=self.compile_ok,
            error_dict=self.error_dict,
        )


# ── Fixtures ──────────────────────────────────────────────────────────


@pytest.fixture
def patch_pyobjc(monkeypatch):
    """Install a fake _NSAppleScript and mark PyObjC available.

    Returns the factory so tests can inspect compile_calls / swap
    compile_ok mid-test.
    """
    factory = _FakeNSAppleScriptFactory(compile_ok=True)
    monkeypatch.setattr(fp, "_HAS_PYOBJC", True)
    monkeypatch.setattr(fp, "_NSAppleScript", factory)
    # Reset cache singleton so each test starts UNCOMPILED.
    monkeypatch.setattr(fp, "_cache_singleton", None)
    return factory


# ── Compile / READY transitions ───────────────────────────────────────


class TestCompile:
    def test_first_get_id_compiles_once(self, patch_pyobjc):
        cache = fp.get_cache()
        handler = cache.get_id_handler()
        assert handler is not None
        assert patch_pyobjc.compile_calls == 1

    def test_second_get_id_reuses_compiled(self, patch_pyobjc):
        cache = fp.get_cache()
        h1 = cache.get_id_handler()
        h2 = cache.get_id_handler()
        assert h1 is h2
        # Still only one compile.
        assert patch_pyobjc.compile_calls == 1

    def test_id_and_tty_compile_independently(self, patch_pyobjc):
        cache = fp.get_cache()
        h_id = cache.get_id_handler()
        h_tty = cache.get_tty_handler()
        assert h_id is not None and h_tty is not None
        assert h_id is not h_tty  # distinct instances
        assert patch_pyobjc.compile_calls == 2

    def test_compile_failure_terminal_state(self, patch_pyobjc):
        patch_pyobjc.compile_ok = False
        cache = fp.get_cache()
        assert cache.get_id_handler() is None
        # Subsequent calls should NOT retry compile — terminal state.
        assert cache.get_id_handler() is None
        assert patch_pyobjc.compile_calls == 1

    def test_compile_failure_isolated_per_handler(self, patch_pyobjc):
        """Failed id compile must not poison tty compile."""
        cache = fp.get_cache()
        # Pre-compile tty when compile_ok=True (default).
        assert cache.get_tty_handler() is not None
        # Flip to failing and request id.
        patch_pyobjc.compile_ok = False
        assert cache.get_id_handler() is None
        # tty should still be cached from before.
        h_tty = cache.get_tty_handler()
        assert h_tty is not None


# ── Failure counter / invalidation ────────────────────────────────────


class TestFailureCounter:
    def test_note_failure_under_threshold_keeps_handler(self, patch_pyobjc):
        cache = fp.get_cache()
        h1 = cache.get_id_handler()
        cache.note_failure("id")
        cache.note_failure("id")
        h2 = cache.get_id_handler()
        # Same compiled instance — not invalidated yet (counter=2 < 3).
        assert h1 is h2
        assert patch_pyobjc.compile_calls == 1

    def test_note_failure_at_threshold_invalidates(self, patch_pyobjc, caplog):
        import logging
        cache = fp.get_cache()
        h1 = cache.get_id_handler()
        assert h1 is not None

        with caplog.at_level(logging.WARNING):
            cache.note_failure("id")
            cache.note_failure("id")
            invalidated = cache.note_failure("id")
        assert invalidated is True
        # Next access recompiles.
        h2 = cache.get_id_handler()
        assert h2 is not h1
        assert patch_pyobjc.compile_calls == 2
        assert any("invalidated id_handler" in r.message for r in caplog.records)

    def test_note_success_resets_counter(self, patch_pyobjc):
        cache = fp.get_cache()
        cache.get_id_handler()
        cache.note_failure("id")
        cache.note_failure("id")
        cache.note_success("id")
        # After 2 fails + 1 success, another 2 fails should NOT invalidate.
        cache.note_failure("id")
        invalidated = cache.note_failure("id")
        assert invalidated is False

    def test_id_failures_dont_affect_tty(self, patch_pyobjc):
        cache = fp.get_cache()
        cache.get_id_handler()
        cache.get_tty_handler()
        cache.note_failure("id")
        cache.note_failure("id")
        cache.note_failure("id")
        # tty counter untouched.
        invalidated_tty = cache.note_failure("tty")
        assert invalidated_tty is False

    def test_invalidate_clears_both_handlers(self, patch_pyobjc):
        cache = fp.get_cache()
        h_id_1 = cache.get_id_handler()
        h_tty_1 = cache.get_tty_handler()
        cache.invalidate()
        h_id_2 = cache.get_id_handler()
        h_tty_2 = cache.get_tty_handler()
        assert h_id_2 is not h_id_1
        assert h_tty_2 is not h_tty_1
        assert patch_pyobjc.compile_calls == 4

    def test_invalidate_clears_compile_failed_flag(self, patch_pyobjc):
        """invalidate() should also unstick a COMPILE_FAILED handler so
        a subsequent get_*_handler attempts compilation again."""
        patch_pyobjc.compile_ok = False
        cache = fp.get_cache()
        assert cache.get_id_handler() is None
        # Fix the underlying issue.
        patch_pyobjc.compile_ok = True
        # Without invalidate, terminal state holds.
        assert cache.get_id_handler() is None
        # invalidate clears the flag → next get retries.
        cache.invalidate()
        assert cache.get_id_handler() is not None


# ── Threshold value sanity ────────────────────────────────────────────


class TestThresholdConstant:
    def test_threshold_is_3(self):
        # Frozen via design § 3.1.1.
        assert fp._CACHE_FAILURE_THRESHOLD == 3

    def test_threshold_used_by_note_failure(self, patch_pyobjc):
        """Direct verification: exactly THRESHOLD failures invalidate."""
        cache = fp.get_cache()
        cache.get_id_handler()
        for i in range(fp._CACHE_FAILURE_THRESHOLD - 1):
            assert cache.note_failure("id") is False, f"failure {i+1} should not invalidate"
        assert cache.note_failure("id") is True


# ── Defensive: no NSAppleScript symbol ────────────────────────────────


class TestDefensive:
    def test_get_handler_without_pyobjc_returns_none(self, monkeypatch):
        """If _NSAppleScript was never set (defensive guard), the cache
        should refuse to compile and lock into COMPILE_FAILED."""
        monkeypatch.setattr(fp, "_NSAppleScript", None)
        monkeypatch.setattr(fp, "_cache_singleton", None)
        cache = fp.get_cache()
        assert cache.get_id_handler() is None
        # Even after re-call.
        assert cache.get_id_handler() is None

    def test_note_failure_unknown_handler_label_is_noop(self, patch_pyobjc):
        cache = fp.get_cache()
        # Should not raise; should not affect either counter.
        assert cache.note_failure("nonsense") is False
        assert cache._id_failures == 0
        assert cache._tty_failures == 0


class TestFocusSourceDeminiaturizesWindow:
    """Regression: clicking a session whose iTerm host window is
    minimized to the Dock used to silently fail — ``select w`` raises
    a window in iTerm's z-order but does not deminiaturize a window
    in the Dock. The handler sources must set ``miniaturized of w to
    false`` before ``select w``. Mirror of the same regression
    coverage in :mod:`test_iterm2_adapter` for the subprocess
    fallback path."""

    def _assert_deminiaturize_before_select_w(self, source: str) -> None:
        # Strip AppleScript ``-- ...`` comments first so a comment
        # mentioning "select w" doesn't fool the substring match.
        # The actual ``select w`` statement must come after the
        # deminiaturize mutator on its own line, otherwise the line
        # selection won't pull a Dock window forward.
        import re
        bare = re.sub(r"--[^\n]*", "", source)
        assert "set miniaturized of w to false" in bare
        i_demin = bare.index("set miniaturized of w to false")
        i_w = bare.index("select w")
        assert i_demin < i_w, (
            "deminiaturize must precede select w; select alone won't "
            "pull the window out of the Dock"
        )

    def test_by_id_source_deminiaturizes_before_select(self):
        self._assert_deminiaturize_before_select_w(fp._FOCUS_BY_ID_SOURCE)

    def test_by_tty_source_deminiaturizes_before_select(self):
        self._assert_deminiaturize_before_select_w(fp._FOCUS_BY_TTY_SOURCE)


class TestFocusSourceTimeoutClause:
    """I-1: AppleScript ``with timeout of N seconds`` wraps the inner
    tell blocks so a hung iTerm Apple Event handler can't peg the
    single-thread worker pool indefinitely (default AE timeout is 60s,
    way too long for an interactive click). On overrun AppleScript
    raises errno -1712 which our error handler treats as a normal
    failure; the AppleScriptCache counter eventually invalidates the
    compiled handler so the next click rebuilds fresh state."""

    def _assert_timeout_wraps_inner_tells(self, source: str) -> None:
        assert "with timeout of" in source, (
            "source must wrap the body in `with timeout` to bound "
            "execution time"
        )
        # Timeout must enclose BOTH the System Events frontmost call
        # AND the iTerm tell block — those are the two operations that
        # can hang on a stuck app.
        i_timeout = source.index("with timeout of")
        i_se = source.index('tell application "System Events"')
        i_iterm = source.index('tell application "iTerm"')
        i_end_timeout = source.index("end timeout")
        assert i_timeout < i_se, "timeout must enclose System Events tell"
        assert i_timeout < i_iterm, "timeout must enclose iTerm tell"
        assert i_se < i_end_timeout
        assert i_iterm < i_end_timeout

    def test_by_id_source_wraps_in_timeout(self):
        self._assert_timeout_wraps_inner_tells(fp._FOCUS_BY_ID_SOURCE)

    def test_by_tty_source_wraps_in_timeout(self):
        self._assert_timeout_wraps_inner_tells(fp._FOCUS_BY_TTY_SOURCE)

    def test_timeout_seconds_matches_subprocess_path(self):
        """The fast-path AppleScript timeout should match the subprocess
        osascript path's timeout (currently 3s). Two paths failing in
        the same envelope keeps user-visible latency consistent and
        prevents one from masking the other's hang."""
        assert fp._PANE_SELECT_APPLESCRIPT_TIMEOUT_S == 3
        for src in (fp._FOCUS_BY_ID_SOURCE, fp._FOCUS_BY_TTY_SOURCE):
            assert "with timeout of 3 seconds" in src


class TestFocusSourceGuardsRedundantMutators:
    """User-reported bug: clicking the apa-origin session caused
    iTerm to "flash to front and back". The window was already at
    iTerm index 1 and not minimized; the unconditional
    ``set miniaturized of w to false`` + ``set index of w to 1``
    calls fired iTerm-side animations even though nothing needed to
    change. Other sessions (whose windows were in different states)
    didn't flash because those mutators were actually doing real
    work — the visible transition WAS the legitimate focus change.

    Guarding both mutators behind an ``is ...`` check eliminates the
    redundant work without losing functionality when it's needed
    (minimized windows still get deminiaturized; windows behind
    iTerm's idx 1 still get pulled forward)."""

    def _assert_guarded_mutators(self, source: str) -> None:
        # set miniaturized must be wrapped by ``if miniaturized of w is true then``
        assert "if miniaturized of w is true then" in source
        i_guard_min = source.index("if miniaturized of w is true then")
        i_set_min = source.index("set miniaturized of w to false")
        assert i_guard_min < i_set_min, "guard must precede the mutator"

        # set index must be wrapped by ``if index of w is not 1 then``
        assert "if index of w is not 1 then" in source
        i_guard_idx = source.index("if index of w is not 1 then")
        i_set_idx = source.index("set index of w to 1")
        assert i_guard_idx < i_set_idx

    def test_by_id_source_guards_mutators(self):
        self._assert_guarded_mutators(fp._FOCUS_BY_ID_SOURCE)

    def test_by_tty_source_guards_mutators(self):
        self._assert_guarded_mutators(fp._FOCUS_BY_TTY_SOURCE)


class TestFocusSourceRaceTolerance:
    """Regression: iTerm's ``repeat with x in collection`` could
    raise ``errAEIllegalIndex (-1719)`` when sessions/tabs/windows
    closed mid-iteration. Caller's _try_handler would call
    ``cache.note_failure`` on the error; after 3 such failures the
    cached compiled handler was invalidated and recompiled — wasted
    work because the script was fine, only iTerm's runtime state
    was racy.

    The new scripts wrap the iTerm tell in ``try`` + ``repeat 2 times``
    so transient races are absorbed (one retry catches the typical
    case) and persistent failures return "miss" rather than raising,
    so the cache failure counter isn't tripped spuriously."""

    def _assert_retry_wraps_iterm_tell(self, source: str) -> None:
        # Retry loop must come BEFORE the iTerm tell block and the
        # on-error clause must come AFTER, wrapping the entire scan.
        assert "repeat 2 times" in source
        assert "on error" in source
        i_repeat = source.index("repeat 2 times")
        i_iterm = source.index('tell application "iTerm"')
        i_on_error = source.index("on error")
        i_end_repeat = source.rindex("end repeat")
        assert i_repeat < i_iterm, (
            "retry must wrap the iTerm tell; got repeat at {} iterm at {}".format(
                i_repeat, i_iterm,
            )
        )
        assert i_iterm < i_on_error < i_end_repeat, (
            "on error must catch the iTerm scan and live inside the "
            "retry loop"
        )

    def test_by_id_source_wraps_in_retry_try(self):
        self._assert_retry_wraps_iterm_tell(fp._FOCUS_BY_ID_SOURCE)

    def test_by_tty_source_wraps_in_retry_try(self):
        self._assert_retry_wraps_iterm_tell(fp._FOCUS_BY_TTY_SOURCE)


class TestFocusSourceSelectOrder:
    """I-8: broadest-scope first ordering (window → tab → session).
    iTerm's ``select`` mutates state on each call; if we did window
    last, an extra z-order change would happen after we'd already
    pinned tab + session. Putting the most-precise selection last
    means it wins regardless of what select w did to the in-tab
    selection."""

    def _assert_w_before_t_before_s(self, source: str) -> None:
        i_w = source.index("select w")
        i_t = source.index("select t")
        i_s = source.index("select s")
        assert i_w < i_t < i_s, (
            f"want w<t<s, got w={i_w} t={i_t} s={i_s}"
        )

    def test_by_id_source_orders_w_t_s(self):
        self._assert_w_before_t_before_s(fp._FOCUS_BY_ID_SOURCE)

    def test_by_tty_source_orders_w_t_s(self):
        self._assert_w_before_t_before_s(fp._FOCUS_BY_TTY_SOURCE)


class TestFocusSourceSetsWindowIndex:
    """I-5: ``set index of w to 1`` after ``select w`` forces iTerm's
    z-order AND in many setups pulls the window onto the current
    macOS Space (Mission Control). Not a full fix for cross-Space —
    true transport requires private CGSPrivate APIs — but resolves
    the common case where the user's preference "switch to a Space
    with open windows" is OFF.

    Must come AFTER select w (which sets the window selection inside
    iTerm) so the index assignment doesn't get reordered behind the
    selection change."""

    def _assert_index_after_select_w(self, source: str) -> None:
        assert "set index of w to 1" in source
        i_select = source.index("select w")
        i_index = source.index("set index of w to 1")
        assert i_select < i_index

    def test_by_id_source_sets_index(self):
        self._assert_index_after_select_w(fp._FOCUS_BY_ID_SOURCE)

    def test_by_tty_source_sets_index(self):
        self._assert_index_after_select_w(fp._FOCUS_BY_TTY_SOURCE)
