"""Tests for ProcessScanner.

Two concerns covered:

- **Q6** (perf): cmdline() is only invoked for node-named processes,
  never for the bulk of processes on the system.
- **S1-S6** (orphan filter): a claude.exe whose console title is not
  rendered as any visible WT tab is dropped, with three layers of
  fail-open safety so we never wipe the user's session list silently.
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

    Also stubs the orphan-filter helpers so tests that don't care about
    orphan logic stay simple:
    - ``get_console_info`` returns a dummy success value so every session
      survives the AttachConsole probe.
    - ``walk_to_visible_host`` returns None so ``window_handle`` stays
      None (matching the legacy behaviour the older tests assumed).
    Tests that exercise the new orphan / wt_hwnd behaviour override
    these inside their own ``with patch(...)`` blocks.
    """
    fake_procs: list = []

    def fake_iter(attrs=None):
        return iter(fake_procs)

    with (
        patch("claude_island.platform_.process_scanner.psutil.process_iter",
              side_effect=fake_iter),
        patch("claude_island.platform_.process_scanner.win32_console.get_console_info",
              return_value=(1, "any-title")),
        patch("claude_island.platform_.process_scanner.window_activator.walk_to_visible_host",
              return_value=None)
    ):
        yield fake_procs


# ==========================================================================
# Q6: cmdline only fetched for node processes
# ==========================================================================

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
    """Verify we don't slip 'cmdline' back into the eager attrs list."""
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
    """A node process we can't read cmdline for is silently skipped."""
    import psutil
    node = _fake_proc(70, "node.exe")
    node.cmdline.side_effect = psutil.AccessDenied
    patched_process_iter.append(node)

    assert ProcessScanner().scan() == []  # no crash, no session


# ==========================================================================
# S1-S4: orphan filter via AttachConsole-success probe
# ==========================================================================
#
# Why AttachConsole-success rather than UIA tab-title matching: tab
# titles have known false positives — a split pane only exposes the
# *active* pane's title via TabItem.Name, so the inactive pane's
# claude.exe title would never match. AttachConsole-success treats
# every still-attached process as live regardless of pane visibility.

def test_attached_session_is_kept(patched_process_iter):
    """S1: get_console_info returns a tuple (AttachConsole succeeded) →
    session kept regardless of title contents."""
    patched_process_iter.append(_fake_proc(100, "claude.exe", cwd="/proj"))

    with patch(
        "claude_island.platform_.process_scanner.win32_console.get_console_info",
        return_value=(12345, "any-title")
    ):
        sessions = ProcessScanner().scan()

    assert [s.pid for s in sessions] == [100]


def test_unattached_session_is_filtered(patched_process_iter):
    """S2: get_console_info returns None (AttachConsole failed → process
    has no console) → session filtered. This is the genuine orphan case."""
    patched_process_iter.append(_fake_proc(100, "claude.exe", cwd="/orphan"))
    patched_process_iter.append(_fake_proc(200, "claude.exe", cwd="/live"))

    def fake_get_info(pid):
        return {100: None, 200: (2, "live-title")}.get(pid)

    with patch(
        "claude_island.platform_.process_scanner.win32_console.get_console_info",
        side_effect=fake_get_info
    ):
        sessions = ProcessScanner().scan()

    assert [s.pid for s in sessions] == [200]


def test_split_pane_inactive_with_attached_console_is_kept(patched_process_iter):
    """S3 (regression): a split-pane inactive session has a live console
    even though its title doesn't match any TabItem.Name. The simpler
    AttachConsole-success judge must keep it (the previous title-set
    judge dropped it as a false positive)."""
    patched_process_iter.append(_fake_proc(100, "claude.exe", cwd="/proj"))

    # Even though the title would never match a TabItem (inactive pane),
    # AttachConsole still works — keep the session.
    with patch(
        "claude_island.platform_.process_scanner.win32_console.get_console_info",
        return_value=(99, "✳ inactive-pane-title-not-in-any-tab")
    ):
        sessions = ProcessScanner().scan()

    assert [s.pid for s in sessions] == [100]


def test_all_filtered_triggers_sanity_fail_open(patched_process_iter):
    """S4: every session reports None (all consoles unavailable —
    system-wide AttachConsole brokenness or scan-thread race with our
    own console state). Sanity tripwire returns originals untouched."""
    patched_process_iter.append(_fake_proc(100, "claude.exe", cwd="/a"))
    patched_process_iter.append(_fake_proc(200, "claude.exe", cwd="/b"))

    with patch(
        "claude_island.platform_.process_scanner.win32_console.get_console_info",
        return_value=None
    ):
        sessions = ProcessScanner().scan()

    # Sanity tripwire: rather than 0 sessions, the originals come back.
    assert {s.pid for s in sessions} == {100, 200}


def test_mixed_attached_and_orphan_passes_normal_filter(patched_process_iter):
    """S-bonus: 5 sessions, 3 attached + 2 orphan → the 3 attached
    survive. Exercises the normal filter path (no tripwire)."""
    for pid in [10, 20, 30, 40, 50]:
        patched_process_iter.append(_fake_proc(pid, "claude.exe", cwd=f"/p{pid}"))

    def fake_get_info(pid):
        # Three live, two orphan
        return {10: (1, "a"), 20: (2, "b"), 30: (3, "c"),
                40: None, 50: None}.get(pid)

    with patch(
        "claude_island.platform_.process_scanner.win32_console.get_console_info",
        side_effect=fake_get_info
    ):
        sessions = ProcessScanner().scan()

    assert {s.pid for s in sessions} == {10, 20, 30}


# ==========================================================================
# Note: window_handle labelling tests removed in PR2.
# ProcessScanner no longer fills window_handle on Session — that responsibility
# moved to TerminalAdapter.group() (see WindowsTerminalAdapter for the
# AttachConsole + walk_to_visible_host code, now adapter-internal).
# Adapter-level grouping is covered by tests/platform_/test_dispatcher.py.
# ==========================================================================
