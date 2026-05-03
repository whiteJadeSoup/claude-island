"""PySide6 + reactivex compatibility smoke test.

Pins down the two cross-library contracts our state-broadcast
architecture depends on:

1. **BehaviorSubject replay** — a fresh subscriber receives the cached
   current value immediately on ``subscribe()``, not just future
   ``on_next`` pushes. The UI relies on this to render the instant it
   subscribes, without waiting for the next Snapshotter tick.

2. **Qt Signal QueuedConnection cross-thread marshaling** — a Signal
   emitted from a worker thread is delivered to its slot on the slot's
   owning thread (the Qt main thread, for our marshaler). This is what
   actually crosses the worker → main thread boundary — *not* reactivex
   ``observe_on(QtScheduler)``.

Why not ``observe_on(QtScheduler)``: the QtScheduler implementation
calls ``QTimer.singleShot(msec, callable)`` which creates the timer in
the calling thread. From a worker thread (no Qt event loop), the timer
never fires. Verified by reading
``reactivex.scheduler.mainloop.qtscheduler:48`` and reproducing the
"value never arrives" behavior. Architecture switched to a small
QObject marshaler with QueuedConnection — Qt's native cross-thread
mechanism, more robust and Qt-idiomatic than reactivex's GUI schedulers.

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
