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

    Also makes the orphan filter a pass-through by default so tests
    that don't care about orphan logic stay simple. Tests for S1-S6
    override ``collect_wt_tab_titles`` / ``get_console_info`` inside
    their own ``with patch(...)`` blocks.
    """
    fake_procs: list = []

    def fake_iter(attrs=None):
        return iter(fake_procs)

    with (
        patch("claude_island.platform_.process_scanner.psutil.process_iter",
              side_effect=fake_iter),
        patch("claude_island.platform_.process_scanner.wt_uia.collect_wt_tab_titles",
              return_value=None),
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
# S1-S6: orphan filter via UIA tab title set
# ==========================================================================

def test_session_with_matching_title_is_kept(patched_process_iter):
    """S1: console title is in live WT tab titles → session kept."""
    patched_process_iter.append(_fake_proc(100, "claude.exe", cwd="/proj"))

    with (
        patch("claude_island.platform_.process_scanner.wt_uia.collect_wt_tab_titles",
              return_value={"my-proj"}),
        patch("claude_island.platform_.process_scanner.win32_console.get_console_info",
              return_value=(12345, "my-proj")),
    ):
        sessions = ProcessScanner().scan()

    assert [s.pid for s in sessions] == [100]


def test_session_with_no_matching_title_is_filtered(patched_process_iter):
    """S2: console title is NOT in live WT tab titles → session filtered.

    This is the user's actual orphan case: claude.exe still runs but
    its conPTY no longer surfaces as a visible WT tab.
    """
    patched_process_iter.append(_fake_proc(100, "claude.exe", cwd="/proj"))
    patched_process_iter.append(_fake_proc(200, "claude.exe", cwd="/live"))

    titles = {"live-proj"}

    def fake_get_info(pid):
        return {100: (1, "orphan-title"), 200: (2, "live-proj")}.get(pid)

    with (
        patch("claude_island.platform_.process_scanner.wt_uia.collect_wt_tab_titles",
              return_value=titles),
        patch("claude_island.platform_.process_scanner.win32_console.get_console_info",
              side_effect=fake_get_info),
    ):
        sessions = ProcessScanner().scan()

    assert [s.pid for s in sessions] == [200]


def test_uia_unavailable_returns_all_sessions_fail_open(patched_process_iter):
    """S3: collect_wt_tab_titles returns None (no WT, library missing,
    enumeration failure) → orphan filter is skipped, all sessions kept."""
    patched_process_iter.append(_fake_proc(100, "claude.exe", cwd="/proj"))

    with patch(
        "claude_island.platform_.process_scanner.wt_uia.collect_wt_tab_titles",
        return_value=None,
    ):
        sessions = ProcessScanner().scan()

    assert [s.pid for s in sessions] == [100]


def test_empty_titles_set_returns_all_sessions_fail_open(patched_process_iter):
    """S4: UIA found WT but reported zero tab names (degenerate state).
    Treated identically to None — skip filter rather than wipe everything."""
    patched_process_iter.append(_fake_proc(100, "claude.exe", cwd="/proj"))

    with patch(
        "claude_island.platform_.process_scanner.wt_uia.collect_wt_tab_titles",
        return_value=set(),
    ):
        sessions = ProcessScanner().scan()

    assert [s.pid for s in sessions] == [100]


def test_per_pid_console_read_failure_keeps_that_session(patched_process_iter):
    """S5: get_console_info returns None for a specific pid → that
    session is *not* filtered (per-pid fail-open). The mid-tier safety
    so a single AttachConsole failure doesn't invisibly drop a session."""
    patched_process_iter.append(_fake_proc(100, "claude.exe", cwd="/a"))
    patched_process_iter.append(_fake_proc(200, "claude.exe", cwd="/b"))

    titles = {"b-proj"}

    def fake_get_info(pid):
        return {100: None, 200: (2, "b-proj")}.get(pid, None)

    with (
        patch("claude_island.platform_.process_scanner.wt_uia.collect_wt_tab_titles",
              return_value=titles),
        patch("claude_island.platform_.process_scanner.win32_console.get_console_info",
              side_effect=fake_get_info),
    ):
        sessions = ProcessScanner().scan()

    # 100 kept (per-pid fail-open), 200 kept (matched title)
    assert {s.pid for s in sessions} == {100, 200}


def test_all_filtered_triggers_sanity_fail_open(patched_process_iter):
    """S6: every session would be filtered (e.g. user manually renamed
    every WT tab) → sanity check returns originals so the user sees
    something instead of an empty list."""
    patched_process_iter.append(_fake_proc(100, "claude.exe", cwd="/a"))
    patched_process_iter.append(_fake_proc(200, "claude.exe", cwd="/b"))

    titles = {"renamed-1", "renamed-2"}

    def fake_get_info(pid):
        # Both pids report titles that don't match any live tab.
        return {100: (1, "stale-a"), 200: (2, "stale-b")}.get(pid)

    with (
        patch("claude_island.platform_.process_scanner.wt_uia.collect_wt_tab_titles",
              return_value=titles),
        patch("claude_island.platform_.process_scanner.win32_console.get_console_info",
              side_effect=fake_get_info),
    ):
        sessions = ProcessScanner().scan()

    # Sanity tripwire: rather than 0 sessions, the originals come back.
    assert {s.pid for s in sessions} == {100, 200}


def test_mixed_match_orphan_passes_filter_normally(patched_process_iter):
    """S-bonus: 5 sessions, 3 match, 2 don't → the 3 are kept.
    This exercises the *normal* filter path (no fail-open kicks in)."""
    for pid in [10, 20, 30, 40, 50]:
        patched_process_iter.append(_fake_proc(pid, "claude.exe", cwd=f"/p{pid}"))

    titles = {"p10", "p20", "p30"}

    def fake_get_info(pid):
        return (pid, f"p{pid}")

    with (
        patch("claude_island.platform_.process_scanner.wt_uia.collect_wt_tab_titles",
              return_value=titles),
        patch("claude_island.platform_.process_scanner.win32_console.get_console_info",
              side_effect=fake_get_info),
    ):
        sessions = ProcessScanner().scan()

    assert {s.pid for s in sessions} == {10, 20, 30}
