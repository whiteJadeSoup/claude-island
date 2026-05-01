"""Tests for ProcessScanner two-pass enumeration (Q6).

Verifies the perf-critical invariant: cmdline() is only invoked for
node-named processes, never for the bulk of processes on the system.
"""

from __future__ import annotations

import time
from unittest.mock import MagicMock, patch

import pytest

from claude_island.platform_.process_scanner import ProcessScanner


def _fake_proc(pid: int, name: str, cmdline: list[str] | None = None,
               cwd: str = "/cwd"):
    """Build a MagicMock that quacks like psutil.Process for our scanner."""
    proc = MagicMock()
    proc.info = {
        "pid": pid,
        "name": name,
        "create_time": time.time(),
    }
    proc.cwd.return_value = cwd
    proc.cmdline = MagicMock(return_value=cmdline or [])
    return proc


@pytest.fixture
def patched_process_iter():
    """Yield a list that tests can populate with fake procs; patches
    psutil.process_iter to return them when scan() asks.

    Also patches _live_window_pids to return None so the orphan filter is
    skipped by default, preserving the behaviour of tests that don't care
    about orphan filtering. Tests that exercise S1–S5 supply their own
    _live_window_pids patch inside the test body, which overrides this one.
    """
    fake_procs: list = []

    def fake_iter(attrs=None):
        # The scanner only requests cheap attrs; we don't enforce that here
        # (the test for that is below), just yield our fakes.
        return iter(fake_procs)

    with (
        patch("claude_island.platform_.process_scanner.psutil.process_iter",
              side_effect=fake_iter),
        patch("claude_island.platform_.process_scanner._live_window_pids",
              return_value=None),
    ):
        yield fake_procs


# --------------------------------------------------------------------------
# Q6: cmdline only fetched for node processes
# --------------------------------------------------------------------------

def test_cmdline_not_called_for_non_node_processes(patched_process_iter):
    """The whole point of Q6: a 500-process system shouldn't pay the
    NtQueryInformationProcess cost for every chrome.exe / explorer.exe."""
    chrome = _fake_proc(1, "chrome.exe")
    explorer = _fake_proc(2, "explorer.exe")
    sshd = _fake_proc(3, "sshd")
    patched_process_iter.extend([chrome, explorer, sshd])

    ProcessScanner().scan()

    chrome.cmdline.assert_not_called()
    explorer.cmdline.assert_not_called()
    sshd.cmdline.assert_not_called()


def test_cmdline_not_called_for_direct_claude_match(patched_process_iter):
    """claude.exe is a direct name hit — no need to pull cmdline."""
    claude = _fake_proc(100, "claude.exe", cwd="/proj")
    patched_process_iter.append(claude)

    sessions = ProcessScanner().scan()

    assert len(sessions) == 1
    assert sessions[0].pid == 100
    claude.cmdline.assert_not_called()


def test_cmdline_called_only_for_node_processes(patched_process_iter):
    """node.exe is the one case where we pay the cost — to disambiguate
    Claude Code from any other Node script."""
    chrome = _fake_proc(1, "chrome.exe")
    node_claude = _fake_proc(2, "node.exe", cmdline=["node", "/path/claude/bin"])
    node_other = _fake_proc(3, "node.exe", cmdline=["node", "server.js"])
    patched_process_iter.extend([chrome, node_claude, node_other])

    ProcessScanner().scan()

    chrome.cmdline.assert_not_called()
    node_claude.cmdline.assert_called_once()
    node_other.cmdline.assert_called_once()


def test_node_with_claude_in_cmdline_is_picked_up(patched_process_iter):
    node = _fake_proc(50, "node.exe", cmdline=["node", "/path/claude/cli.js"],
                     cwd="/proj")
    patched_process_iter.append(node)

    sessions = ProcessScanner().scan()
    assert [s.pid for s in sessions] == [50]


def test_node_without_claude_in_cmdline_is_skipped(patched_process_iter):
    node = _fake_proc(60, "node.exe", cmdline=["node", "express-server.js"])
    patched_process_iter.append(node)

    assert ProcessScanner().scan() == []


def test_process_iter_requested_only_cheap_attrs():
    """Verify we don't slip 'cmdline' back into the eager attrs list —
    that would silently undo the perf win."""
    captured_attrs: list = []

    def capture(attrs=None):
        captured_attrs.append(attrs)
        return iter([])

    with patch("claude_island.platform_.process_scanner.psutil.process_iter",
               side_effect=capture):
        ProcessScanner().scan()

    assert captured_attrs, "process_iter was not called"
    requested = captured_attrs[0]
    assert "cmdline" not in (requested or []), (
        f"process_iter requested cmdline eagerly: {requested}"
    )


def test_access_denied_during_cmdline_does_not_crash(patched_process_iter):
    """A node process we can't read cmdline for is silently skipped,
    not allowed to abort the whole scan."""
    import psutil
    node = _fake_proc(70, "node.exe")
    node.cmdline.side_effect = psutil.AccessDenied
    patched_process_iter.append(node)

    assert ProcessScanner().scan() == []  # no crash, no session


# --------------------------------------------------------------------------
# S1–S5: orphan filter
# --------------------------------------------------------------------------

def test_live_session_with_window_ancestor_is_kept(patched_process_iter):
    """S1: ancestor PID 200 owns a visible window → session is not an orphan."""
    patched_process_iter.append(_fake_proc(100, "claude.exe", cwd="/proj"))

    with (
        patch("claude_island.platform_.process_scanner._live_window_pids",
              return_value={200}),
        patch("claude_island.platform_.process_scanner._ancestor_pids",
              return_value=[100, 200]),
    ):
        sessions = ProcessScanner().scan()

    assert [s.pid for s in sessions] == [100]


def test_orphan_with_no_window_ancestor_is_filtered(patched_process_iter):
    """S2: ancestor chain has no PID in live_window_pids → orphan, filtered."""
    patched_process_iter.append(_fake_proc(100, "claude.exe", cwd="/proj"))

    with (
        patch("claude_island.platform_.process_scanner._live_window_pids",
              return_value={999}),
        patch("claude_island.platform_.process_scanner._ancestor_pids",
              return_value=[100, 200, 300]),
    ):
        sessions = ProcessScanner().scan()

    assert sessions == []


def test_orphan_with_broken_ancestor_chain_is_filtered(patched_process_iter):
    """S3: parent is already dead (NoSuchProcess); _ancestor_pids returns only
    self; self is not in live_window_pids → orphan."""
    patched_process_iter.append(_fake_proc(100, "claude.exe", cwd="/proj"))

    with (
        patch("claude_island.platform_.process_scanner._live_window_pids",
              return_value={999}),
        patch("claude_island.platform_.process_scanner._ancestor_pids",
              return_value=[100]),   # chain stops at self; parent already gone
    ):
        sessions = ProcessScanner().scan()

    assert sessions == []


def test_enum_windows_failure_returns_all_sessions_fail_open(patched_process_iter):
    """S4: _live_window_pids() returns None (EnumWindows failure) → orphan
    filter is skipped entirely (fail-open)."""
    patched_process_iter.append(_fake_proc(100, "claude.exe", cwd="/proj"))

    with patch("claude_island.platform_.process_scanner._live_window_pids",
               return_value=None):
        sessions = ProcessScanner().scan()

    assert [s.pid for s in sessions] == [100]


def test_mixed_live_and_orphan_sessions(patched_process_iter):
    """S5: 3 live + 2 orphan → only the 3 live sessions returned."""
    for pid in [10, 20, 30, 40, 50]:
        patched_process_iter.append(_fake_proc(pid, "claude.exe", cwd=f"/p{pid}"))

    live_terminal_pids = {1001, 1002, 1003}

    def fake_ancestors(pid: int) -> list[int]:
        return {10: [10, 1001], 20: [20, 1002], 30: [30, 1003]}.get(pid, [pid])

    with (
        patch("claude_island.platform_.process_scanner._live_window_pids",
              return_value=live_terminal_pids),
        patch("claude_island.platform_.process_scanner._ancestor_pids",
              side_effect=fake_ancestors),
    ):
        sessions = ProcessScanner().scan()

    assert {s.pid for s in sessions} == {10, 20, 30}
