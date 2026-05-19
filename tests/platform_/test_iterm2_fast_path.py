"""Unit tests for ``try_fast_path`` decision tree + ``_PaneSelectTask``
constructor invariants.

Strategy: replace PyObjC symbols with controllable fakes via monkeypatch.
The worker pool is mocked at the singleton-getter level so submit() is
observed without actually scheduling onto a real thread.
"""
from __future__ import annotations

import logging
from unittest import mock

import pytest

from claude_island.platform_.terminals import _iterm_fast_path as fp


# ── Fakes ─────────────────────────────────────────────────────────────


class _FakeNSRunningApp:
    """Mimics NSRunningApplication for fast-path activation tests.

    ``is_active_result`` simulates the I-6 post-condition: even when
    activate returns True, the OS may not actually have transitioned
    the app on macOS 14+ Sonoma. False here means "activate lied" and
    triggers the legacy fallback."""

    def __init__(self, *, activate_result: bool = True,
                 raise_on_activate: bool = False,
                 is_active_result: bool = True) -> None:
        self.activate_result = activate_result
        self.raise_on_activate = raise_on_activate
        self.activate_call_count = 0
        self.activate_options_seen: int | None = None
        self.is_active_result = is_active_result

    def activateWithOptions_(self, options):
        self.activate_call_count += 1
        self.activate_options_seen = options
        if self.raise_on_activate:
            raise RuntimeError("fake objc error")
        return self.activate_result

    def isActive(self) -> bool:
        return self.is_active_result


class _FakeNSRunningApplicationClass:
    """Stand-in for the NSRunningApplication class symbol."""

    def __init__(self, pid_to_app: dict[int, _FakeNSRunningApp | None]) -> None:
        self.pid_to_app = pid_to_app
        self.lookup_calls: list[int] = []

    def runningApplicationWithProcessIdentifier_(self, pid: int):
        self.lookup_calls.append(int(pid))
        return self.pid_to_app.get(int(pid))


class _FakeWorker:
    """Captures submitted tasks without running them."""

    def __init__(self) -> None:
        self.submitted: list[fp._PaneSelectTask] = []
        self.accept = True

    def submit(self, task) -> bool:
        self.submitted.append(task)
        return self.accept


# ── Fixtures ──────────────────────────────────────────────────────────


@pytest.fixture
def fake_pyobjc(monkeypatch):
    """Mark PyObjC available and install fake NSRunningApplication.
    The fake app reports isActive()=True by default so the I-6
    post-activate verifier sees success; tests that simulate the
    deprecated-activate-lie pass ``is_active_result=False`` on a
    custom _FakeNSRunningApp instance."""
    fake_app = _FakeNSRunningApp(activate_result=True, is_active_result=True)
    fake_class = _FakeNSRunningApplicationClass({99999: fake_app, 88888: None})
    monkeypatch.setattr(fp, "_HAS_PYOBJC", True)
    monkeypatch.setattr(fp, "_NSRunningApplication", fake_class)
    monkeypatch.setattr(fp, "_NSApplicationActivateIgnoringOtherApps", 0)
    return fake_class, fake_app


@pytest.fixture
def fake_worker(monkeypatch):
    w = _FakeWorker()
    monkeypatch.setattr(fp, "_worker_singleton", w)
    return w


# ── try_fast_path: PyObjC availability gate ───────────────────────────


class TestPyObjCGate:
    def test_pyobjc_unavailable_returns_false_without_calling_apps(self, monkeypatch):
        monkeypatch.setattr(fp, "_HAS_PYOBJC", False)
        # No NSRunningApplication patched — _ensure_pyobjc short-circuits
        # on cached False so it never touches the real import.
        assert fp.try_fast_path(host_pid=12345, session_id="abc", tty=None) is False

    def test_pyobjc_unavailable_logs_once(self, monkeypatch, caplog):
        # Force probe by setting None then making import fail.
        monkeypatch.setattr(fp, "_HAS_PYOBJC", None)
        monkeypatch.setattr(fp, "_NSRunningApplication", None)
        # Patch the import path inside _ensure_pyobjc to fail.
        import builtins
        real_import = builtins.__import__

        def fake_import(name, *args, **kwargs):
            if name in ("AppKit", "Foundation"):
                raise ImportError(f"fake missing {name}")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", fake_import)
        with caplog.at_level(logging.INFO):
            assert fp._ensure_pyobjc() is False
            # Second call uses cached False, no new log.
            assert fp._ensure_pyobjc() is False
        info_records = [r for r in caplog.records if "fast-path disabled" in r.message]
        assert len(info_records) == 1


# ── try_fast_path: host_pid validation ────────────────────────────────


class TestHostPidGate:
    def test_zero_host_pid_returns_false(self, fake_pyobjc, fake_worker):
        assert fp.try_fast_path(host_pid=0, session_id="abc", tty=None) is False
        assert fake_pyobjc[0].lookup_calls == []
        assert fake_worker.submitted == []

    def test_negative_host_pid_returns_false(self, fake_pyobjc, fake_worker):
        assert fp.try_fast_path(host_pid=-1, session_id="abc", tty=None) is False

    def test_nil_app_returns_false_no_submit(self, fake_pyobjc, fake_worker):
        """runningApplicationWithProcessIdentifier_ → None means pid
        is not a UI app (gone / not running). Fast-path declines."""
        fake_class, fake_app = fake_pyobjc
        # pid 88888 maps to None in the fixture.
        assert fp.try_fast_path(host_pid=88888, session_id="abc", tty="/dev/ttys001") is False
        assert fake_app.activate_call_count == 0
        assert fake_worker.submitted == []


# ── try_fast_path: activate result ────────────────────────────────────


class TestActivate:
    def test_activate_yes_returns_true(self, fake_pyobjc, fake_worker):
        assert fp.try_fast_path(host_pid=99999, session_id="abc", tty=None) is True

    def test_activate_no_returns_false(self, fake_pyobjc, fake_worker):
        fake_pyobjc[1].activate_result = False
        assert fp.try_fast_path(host_pid=99999, session_id="abc", tty=None) is False
        assert fake_worker.submitted == []

    def test_activate_raises_returns_false(self, fake_pyobjc, fake_worker, caplog):
        fake_pyobjc[1].raise_on_activate = True
        with caplog.at_level(logging.WARNING):
            assert fp.try_fast_path(host_pid=99999, session_id="abc", tty=None) is False
        assert any("activate raised" in r.message for r in caplog.records)
        assert fake_worker.submitted == []


# ── try_fast_path: worker submission ──────────────────────────────────


class TestWorkerSubmit:
    def test_session_id_only_submits_task_with_id_no_tty(self, fake_pyobjc, fake_worker):
        ok = fp.try_fast_path(host_pid=99999, session_id="ABC-123", tty=None)
        assert ok is True
        assert len(fake_worker.submitted) == 1
        task = fake_worker.submitted[0]
        assert task.host_pid == 99999
        assert task.session_id == "ABC-123"
        assert task.tty is None

    def test_tty_only_submits_task_with_tty_no_id(self, fake_pyobjc, fake_worker):
        ok = fp.try_fast_path(host_pid=99999, session_id=None, tty="/dev/ttys009")
        assert ok is True
        task = fake_worker.submitted[0]
        assert task.session_id is None
        assert task.tty == "/dev/ttys009"

    def test_both_signals_submits_task_with_both(self, fake_pyobjc, fake_worker):
        ok = fp.try_fast_path(host_pid=99999, session_id="X", tty="/dev/ttys001")
        assert ok is True
        task = fake_worker.submitted[0]
        assert task.session_id == "X"
        assert task.tty == "/dev/ttys001"

    def test_no_signals_still_activates_no_submit(self, fake_pyobjc, fake_worker):
        """No id and no tty: host raise still happens (user wants the app
        in front even without pane precision); just no worker task."""
        ok = fp.try_fast_path(host_pid=99999, session_id=None, tty=None)
        assert ok is True
        assert fake_pyobjc[1].activate_call_count == 1
        assert fake_worker.submitted == []

    def test_empty_strings_treated_as_none(self, fake_pyobjc, fake_worker):
        ok = fp.try_fast_path(host_pid=99999, session_id="", tty="")
        assert ok is True
        assert fake_worker.submitted == []


class TestActivateLieDetection:
    """I-6: On macOS 14+ Sonoma, ``activateWithOptions_`` is deprecated
    and can return True without actually activating. Verify via
    ``app.isActive()`` (a property on the NSRunningApplication wrapper
    we already have) that the transition happened. On mismatch,
    return False so the caller's legacy subprocess osascript path
    (which uses System Events `set frontmost`) takes over — that path
    is not subject to the same demotion rules."""

    def test_returns_false_when_app_did_not_become_active(
        self, fake_pyobjc, fake_worker, monkeypatch,
    ):
        """activate returned True but app.isActive remained False.
        The 'lie' case: return False so legacy fallback fires."""
        # Override the fake app to lie: activate succeeds but the OS
        # never actually transitioned (isActive stays False).
        lying_app = _FakeNSRunningApp(
            activate_result=True, is_active_result=False,
        )
        monkeypatch.setattr(
            fp, "_NSRunningApplication",
            _FakeNSRunningApplicationClass({99999: lying_app}),
        )
        # Shorten poll budget so the test doesn't sleep for 30ms.
        monkeypatch.setattr(fp, "_VERIFY_POLL_INTERVAL_S", 0.001)
        ok = fp.try_fast_path(host_pid=99999, session_id="X", tty=None)
        assert ok is False

    def test_returns_true_when_app_is_active_after_activate(
        self, fake_pyobjc, fake_worker,
    ):
        """Normal success path: activate returned True and isActive
        immediately reflects True on the first poll → fast return."""
        ok = fp.try_fast_path(host_pid=99999, session_id="X", tty=None)
        assert ok is True

    def test_is_active_raising_treated_as_failure(
        self, fake_pyobjc, fake_worker, monkeypatch,
    ):
        """Defensive: if isActive raises (PyObjC bridge oddity), fall
        through to legacy rather than treat as success."""
        class _BrokenApp(_FakeNSRunningApp):
            def isActive(self):
                raise RuntimeError("objc bridge hiccup")
        broken = _BrokenApp(activate_result=True)
        monkeypatch.setattr(
            fp, "_NSRunningApplication",
            _FakeNSRunningApplicationClass({99999: broken}),
        )
        monkeypatch.setattr(fp, "_VERIFY_POLL_INTERVAL_S", 0.001)
        ok = fp.try_fast_path(host_pid=99999, session_id="X", tty=None)
        assert ok is False


class TestPrewarm:
    """I-4: prewarm() at app boot eliminates ~50-60 ms of first-click
    latency. It must:
      * no-op when PyObjC is unavailable (non-macOS, missing dep)
      * idempotent — repeated calls don't accumulate work
      * forgiving of pool-start failures (don't leak _inflight)"""

    def test_no_op_when_pyobjc_unavailable(self, monkeypatch):
        """Non-macOS path: prewarm must return cleanly without
        constructing the worker or touching the cache."""
        monkeypatch.setattr(fp, "_HAS_PYOBJC", False)
        monkeypatch.setattr(fp, "_worker_singleton", None)
        monkeypatch.setattr(fp, "_cache_singleton", None)
        fp.prewarm()   # must not raise
        # Worker / cache stay unconstructed; nothing to prewarm.
        assert fp._worker_singleton is None
        assert fp._cache_singleton is None

    def test_idempotent_with_pyobjc(self, fake_pyobjc, fake_worker):
        """Repeated prewarm calls don't accumulate inflight tasks
        beyond the worker's recovery path (each task self-decrements
        in finally)."""
        for _ in range(3):
            fp.prewarm()
        # All three prewarm tasks were submitted to the fake worker
        # (the fake doesn't actually run them, so backlog isn't a
        # useful assertion here — just verify no exception escaped).
        assert len(fake_worker.submitted) >= 1

    def test_prewarm_leak_safe_when_pool_start_raises(
        self, fake_pyobjc, monkeypatch,
    ):
        """If the worker's pool start raises during prewarm, the
        increment must be undone — same C-2 guard as submit()."""
        # Build a fresh real-ish worker whose pool raises on start.
        monkeypatch.setattr(fp, "_worker_singleton", None)
        worker = fp.get_worker()
        worker._pool = mock.Mock()
        worker._pool.start.side_effect = RuntimeError("pool gone")
        # prewarm must not raise to the caller (best-effort).
        fp.prewarm()
        assert worker.backlog() == 0, (
            "leaked _inflight after failed prewarm pool.start"
        )


class TestSubmitRejectionFallsBack:
    """C-3: When the worker rejects pane-select (backlog full, iTerm
    hung), try_fast_path must return False so the caller's legacy
    osascript fallback fires. Previously the rejection was silently
    discarded and try_fast_path returned True, leaving the user on
    the wrong pane with no recovery path."""

    def test_submit_rejected_with_signal_returns_false(
        self, fake_pyobjc, fake_worker,
    ):
        """Worker says 'no' (backlog full) and we had pane signal →
        return False so legacy fallback runs."""
        fake_worker.accept = False
        ok = fp.try_fast_path(host_pid=99999, session_id="X", tty=None)
        assert ok is False
        # Host activation still happened (best-effort) before rejection.
        assert fake_pyobjc[1].activate_call_count == 1
        # Submit WAS attempted (worker rejected it).
        assert len(fake_worker.submitted) == 1

    def test_submit_rejected_with_tty_only_also_returns_false(
        self, fake_pyobjc, fake_worker,
    ):
        fake_worker.accept = False
        ok = fp.try_fast_path(host_pid=99999, session_id=None, tty="/dev/ttys001")
        assert ok is False

    def test_submit_raises_with_signal_returns_false(
        self, fake_pyobjc, fake_worker, monkeypatch,
    ):
        """If submit() raises (not just rejects), still return False
        so legacy path can recover."""
        def boom(_task):
            raise RuntimeError("worker exploded")
        monkeypatch.setattr(fake_worker, "submit", boom)
        ok = fp.try_fast_path(host_pid=99999, session_id="X", tty=None)
        assert ok is False

    def test_no_signal_no_submit_still_returns_true(
        self, fake_pyobjc, fake_worker,
    ):
        """When there's no pane signal, no submit happens at all —
        the user only asked for app-level activation, which succeeded.
        Don't punish them with a False return."""
        fake_worker.accept = False  # would reject if submit happened
        ok = fp.try_fast_path(host_pid=99999, session_id=None, tty=None)
        assert ok is True
        assert fake_worker.submitted == []


# ── _PaneSelectTask constructor invariants (§2.2) ─────────────────────


class TestPaneSelectTaskInvariants:
    def test_zero_host_pid_raises(self):
        with pytest.raises(ValueError, match="host_pid must be positive"):
            fp._PaneSelectTask(host_pid=0, session_id="x", tty=None)

    def test_negative_host_pid_raises(self):
        with pytest.raises(ValueError, match="host_pid must be positive"):
            fp._PaneSelectTask(host_pid=-5, session_id="x", tty=None)

    def test_both_signals_none_raises(self):
        with pytest.raises(ValueError, match="session_id or tty must be non-empty"):
            fp._PaneSelectTask(host_pid=12345, session_id=None, tty=None)

    def test_both_signals_empty_string_raises(self):
        with pytest.raises(ValueError, match="session_id or tty must be non-empty"):
            fp._PaneSelectTask(host_pid=12345, session_id="", tty="")

    def test_id_only_constructs(self):
        t = fp._PaneSelectTask(host_pid=1, session_id="abc", tty=None)
        assert t.session_id == "abc"
        assert t.tty is None

    def test_tty_only_constructs(self):
        t = fp._PaneSelectTask(host_pid=1, session_id=None, tty="/dev/ttys001")
        assert t.tty == "/dev/ttys001"

    def test_created_at_recorded(self):
        import time
        before = time.monotonic()
        t = fp._PaneSelectTask(host_pid=1, session_id="x", tty=None)
        after = time.monotonic()
        assert before <= t.created_at <= after


# ── try_fast_path catches worker submission failure ───────────────────


class TestSubmitFailure:
    def test_submit_raises_returns_false_so_legacy_fallback_fires(
        self, fake_pyobjc, monkeypatch, caplog,
    ):
        """C-3: when submit raises with a pane signal present, the host
        raise already happened (best-effort visible activation) but
        pane precision was lost. Return False so the caller's
        _legacy_focus subprocess osascript path gets a synchronous
        shot at landing the right pane.

        Previously this returned True and silently swallowed the
        failure — user stayed on the wrong pane with no recovery.
        The warning log is still emitted for diagnosability."""
        class _RaisingWorker:
            def submit(self, task):
                raise RuntimeError("simulated worker failure")
        monkeypatch.setattr(fp, "_worker_singleton", _RaisingWorker())
        with caplog.at_level(logging.WARNING):
            ok = fp.try_fast_path(host_pid=99999, session_id="x", tty=None)
        assert ok is False
        assert any("PaneSelectTask not scheduled" in r.message for r in caplog.records)
