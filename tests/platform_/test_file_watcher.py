"""Tests for FileWatcher (Q4): stop() must be safe even if start() never ran."""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from claude_island.platform_.file_watcher import FileWatcher


def test_stop_without_start_does_not_raise(tmp_path):
    """Q4 regression: a fresh FileWatcher whose start() was never called
    must not raise RuntimeError on stop. __main__.py's cleanup is now
    unconditional, so this idempotency is required."""
    fw = FileWatcher()
    fw.stop()  # must not raise


def test_stop_after_watch_but_before_start(tmp_path):
    """Calling watch() schedules a path on the observer but does not start
    it; stop() should still be safe."""
    fw = FileWatcher()
    fw.watch(tmp_path, callback=lambda p: None)
    fw.stop()  # must not raise


def test_normal_lifecycle_still_works(tmp_path):
    """Sanity: watch + start + stop with a real file event delivers."""
    fw = FileWatcher()
    received = []
    fw.watch(tmp_path, callback=lambda p: received.append(p))
    fw.start()
    try:
        (tmp_path / "test.jsonl").write_bytes(b"hello\n")
        # Watchdog observer delivers events asynchronously; poll briefly.
        for _ in range(20):
            if received:
                break
            time.sleep(0.05)
        assert received, "watchdog never delivered the file event"
    finally:
        fw.stop()


def test_double_stop_is_safe(tmp_path):
    fw = FileWatcher()
    fw.watch(tmp_path, callback=lambda p: None)
    fw.start()
    fw.stop()
    fw.stop()  # must not raise


def test_suffix_filter_excludes_other_extensions(tmp_path):
    """Default ``suffix=".jsonl"`` must NOT fire for a .json file —
    proves the filter discriminates so the new sessions/ watcher
    (which uses ``suffix=".json"``) won't see jsonl events and
    vice versa."""
    fw = FileWatcher()
    received: list[Path] = []
    fw.watch(tmp_path, callback=lambda p: received.append(p))  # default .jsonl
    fw.start()
    try:
        # Write a .json file — must NOT trigger the .jsonl handler.
        (tmp_path / "ignored.json").write_bytes(b"{}")
        # Then write a .jsonl file — MUST trigger.
        (tmp_path / "kept.jsonl").write_bytes(b"line\n")
        for _ in range(20):
            if received:
                break
            time.sleep(0.05)
        assert received, "no .jsonl event delivered"
        assert all(str(p).endswith(".jsonl") for p in received), (
            f".json leaked through .jsonl filter: {received}"
        )
    finally:
        fw.stop()


def test_explicit_suffix_routes_only_matching_files(tmp_path):
    """``suffix=".json"`` (the sessions/ watcher use case) must
    receive .json events and IGNORE .jsonl events on the same dir."""
    fw = FileWatcher()
    received: list[Path] = []
    fw.watch(tmp_path, callback=lambda p: received.append(p), suffix=".json")
    fw.start()
    try:
        (tmp_path / "ignored.jsonl").write_bytes(b"line\n")
        (tmp_path / "kept.json").write_bytes(b"{}")
        for _ in range(20):
            if received:
                break
            time.sleep(0.05)
        assert received, "no .json event delivered"
        assert all(str(p).endswith(".json") and not str(p).endswith(".jsonl")
                   for p in received), (
            f".jsonl leaked through .json filter: {received}"
        )
    finally:
        fw.stop()
