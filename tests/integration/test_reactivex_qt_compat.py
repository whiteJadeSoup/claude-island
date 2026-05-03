"""PySide6 + reactivex compatibility smoke test.

Pins down THREE cross-library contracts our state-broadcast
architecture depends on AND one anti-contract (the failure mode
that drove us away from reactivex's QtScheduler).

POSITIVE contracts (must keep working):

1. **BehaviorSubject replay** — a fresh subscriber receives the cached
   current value immediately on ``subscribe()``, not just future
   ``on_next`` pushes. The UI relies on this to render the instant it
   subscribes, without waiting for the next Snapshotter tick.

2. **Subject same-thread dispatch** — ``Subject.on_next`` synchronously
   invokes every subscriber on the calling thread. No intrinsic
   thread marshaling. This is why we MUST use the WorldMarshaler
   pattern: a Subject called from the worker thread fires subscribers
   on the worker thread, which would crash Qt widgets.

3. **Qt Signal QueuedConnection cross-thread marshaling** — a Signal
   emitted from a worker thread is delivered to its slot on the slot's
   owning thread (the Qt main thread, for our marshaler). This is what
   actually crosses the worker → main thread boundary in our app.

NEGATIVE contract (the documented anti-pattern):

4. **observe_on(QtScheduler) is xfail cross-thread** — calling
   ``QTimer.singleShot(msec, callable)`` from a worker thread (no Qt
   event loop) silently never fires. We added an xfail-marked test
   below that demonstrates this on the current reactivex/PySide6 pair
   so a future reactivex release that fixes the QtScheduler will
   trigger an XPASS and prompt us to revisit the WorldMarshaler.

This file is an integration test (boundary between two third-party
libraries, not our own code) — sits under ``tests/integration/``.
"""
from __future__ import annotations

import os
import threading

# Force offscreen Qt — same convention as tests/ui/.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QObject, Qt, Signal
from reactivex.subject import BehaviorSubject


def test_behavior_subject_replays_initial_value_to_new_subscriber():
    """BehaviorSubject contract: a fresh subscriber receives the current
    cached value immediately on subscribe (not just future on_next
    pushes). This is the property our _WorldStore relies on so the UI
    can render the moment it subscribes, without waiting for the next
    Snapshotter tick."""
    subj: BehaviorSubject[int] = BehaviorSubject(42)
    received: list[int] = []
    subj.subscribe(received.append)
    assert received == [42]


def test_subject_on_next_runs_subscribers_synchronously_on_calling_thread():
    """reactivex Subject contract: ``on_next`` synchronously invokes
    every subscriber on the thread that called ``on_next``. There is no
    intrinsic thread marshaling. This is why we need a Qt Signal +
    QueuedConnection marshaler in the wiring layer — to ensure
    ``world.push()`` is always called from the Qt main thread, so
    subscribers (renders) run on the main thread.

    If reactivex changed this and started dispatching async, our render
    correctness would silently break."""
    subj: BehaviorSubject[int] = BehaviorSubject(0)
    received: list[int] = []
    main_id = threading.get_ident()
    callback_thread_id: list[int] = []

    def on_next(v: int) -> None:
        received.append(v)
        callback_thread_id.append(threading.get_ident())

    subj.subscribe(on_next)

    def worker():
        subj.on_next(99)

    t = threading.Thread(target=worker)
    t.start()
    t.join()

    # The 99 must have been delivered, and from the WORKER thread —
    # proving Subject does not marshal.
    assert 99 in received
    worker_callback_id = callback_thread_id[received.index(99)]
    assert worker_callback_id != main_id


def test_qt_signal_queued_connection_marshals_slot_to_receivers_thread(qtbot):
    """End-to-end: a Qt Signal connected with ``Qt.QueuedConnection``
    posts the slot call onto the receiver QObject's thread's event
    loop. This is the mechanism our wiring uses to marshal
    ``Snapshotter._do_build`` results from the worker thread back to
    the Qt main thread before invoking ``world.push``.

    The test creates a QObject on the main thread, emits its signal
    from a worker thread, and asserts the slot ran on the main
    thread."""
    main_id = threading.get_ident()
    received: list[tuple[int, int]] = []  # (value, thread_id)

    class Marshaler(QObject):
        snap_ready = Signal(int)

        def on_snap(self, value: int) -> None:
            received.append((value, threading.get_ident()))

    marshaler = Marshaler()
    # AutoConnection becomes Queued when emit and slot are on different
    # threads — we set it explicitly so the contract is testable.
    marshaler.snap_ready.connect(marshaler.on_snap, Qt.ConnectionType.QueuedConnection)

    def worker_emit():
        marshaler.snap_ready.emit(7)

    t = threading.Thread(target=worker_emit)
    t.start()
    t.join()

    # The slot is queued — must spin the Qt event loop for it to run.
    qtbot.wait(50)

    matches = [e for e in received if e[0] == 7]
    assert len(matches) == 1, (
        f"Slot was not invoked after emit from worker thread; "
        f"received: {received}"
    )
    assert matches[0][1] == main_id, (
        f"QueuedConnection failed to marshal to main thread: slot ran "
        f"on {matches[0][1]}, expected {main_id}"
    )


import pytest


@pytest.mark.xfail(
    reason=(
        "reactivex's QtScheduler.schedule_relative calls "
        "QTimer.singleShot(msec, callable) which creates the timer in "
        "the calling thread. From a worker thread (no Qt event loop), "
        "the timer never fires and the value never reaches the "
        "subscriber. This xfail documents the bug — if a future "
        "reactivex release fixes it, this test XPASSES and prompts us "
        "to revisit the WorldMarshaler workaround in ui/world_marshaler.py."
    ),
    strict=True,
)
def test_observe_on_qt_scheduler_marshals_from_worker_thread_xfail(qtbot):
    """If this ever XPASSes, reactivex finally made QtScheduler
    cross-thread safe — at which point our WorldMarshaler can be
    replaced with a one-liner ``ops.observe_on(QtScheduler(QtCore))``
    and the wiring layer simplifies. Until then, expect failure."""
    import reactivex.operators as ops
    from reactivex.scheduler.mainloop import QtScheduler
    from reactivex.subject import Subject

    qt_scheduler = QtScheduler(QtCore)
    main_id = threading.get_ident()
    received: list[tuple[int, int]] = []
    subj: Subject[int] = Subject()

    subj.pipe(
        ops.observe_on(qt_scheduler),
    ).subscribe(
        on_next=lambda v: received.append((v, threading.get_ident())),
    )

    def worker_push():
        subj.on_next(42)

    t = threading.Thread(target=worker_push)
    t.start()
    t.join()
    qtbot.wait(100)

    matches = [e for e in received if e[0] == 42]
    assert len(matches) == 1, "QtScheduler dropped the value (the bug)"
    assert matches[0][1] == main_id
