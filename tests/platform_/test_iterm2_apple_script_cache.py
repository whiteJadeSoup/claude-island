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
