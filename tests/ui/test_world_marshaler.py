"""Tests for WorldMarshaler — the Qt Signal bridge that marshals
``Snapshotter._do_build`` results from the worker thread back to the
Qt main thread before invoking ``world.push``.
"""
from __future__ import annotations

import os
import threading
from datetime import datetime, timezone

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from claude_island.core.snapshot import WorldSnapshot, world
from claude_island.ui.world_marshaler import WorldMarshaler


class TestWorldMarshaler:
    def test_emit_from_main_thread_pushes_to_world(self, qtbot):
        marshaler = WorldMarshaler()
        received: list[WorldSnapshot] = []
        world.observable().subscribe(received.append)
        # First emit: drop the initial empty replay.
        baseline = len(received)

        snap = WorldSnapshot(
            sessions=(), today_cost_usd=5.0, quota=None,
            available_providers=(), selected_provider=None,
            fetched_at=datetime.now(timezone.utc),
        )
        marshaler.snap_ready.emit(snap)
        # QueuedConnection always queues — even same-thread emits.
        # qtbot.wait spins the event loop so the queued call fires.
        qtbot.wait(50)
        assert len(received) == baseline + 1
        assert received[-1].today_cost_usd == 5.0

    def test_emit_from_worker_thread_marshals_to_main(self, qtbot):
        """End-to-end: a Snapshotter-style emit from a worker thread
        must land on world.push on the Qt main thread. Without this
        contract, capsule/expanded render() would run on the worker
        thread and crash Qt widget operations."""
        marshaler = WorldMarshaler()
        main_id = threading.get_ident()
        received: list[tuple[float, int]] = []

        def capture(snap: WorldSnapshot) -> None:
            received.append((snap.today_cost_usd, threading.get_ident()))

        world.observable().subscribe(capture)
        baseline = len(received)

        snap = WorldSnapshot(
            sessions=(), today_cost_usd=42.0, quota=None,
            available_providers=(), selected_provider=None,
            fetched_at=datetime.now(timezone.utc),
        )

        def worker():
            marshaler.snap_ready.emit(snap)

        t = threading.Thread(target=worker)
        t.start()
        t.join()
        qtbot.wait(50)

        # Subscriber must have run on the main thread, not the worker.
        new_entries = received[baseline:]
        matches = [e for e in new_entries if e[0] == 42.0]
        assert len(matches) == 1, f"got {received}"
        assert matches[0][1] == main_id, (
            f"world.push subscriber ran on thread {matches[0][1]}, "
            f"expected main {main_id}"
        )

    def test_emit_with_non_worldsnapshot_is_ignored(self, qtbot):
        """Belt-and-braces: garbage payloads do not crash the event
        loop. Should never happen in production (Snapshotter only
        emits WorldSnapshots) but we don't want to take the app down
        because of a stray test or future caller."""
        marshaler = WorldMarshaler()
        received: list[WorldSnapshot] = []
        world.observable().subscribe(received.append)
        baseline = len(received)

        marshaler.snap_ready.emit("not a snapshot")  # type: ignore[arg-type]
        qtbot.wait(50)
        # No new push happened.
        assert len(received) == baseline
