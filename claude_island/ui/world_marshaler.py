"""Cross-thread marshaler for ``WorldSnapshot`` pushes.

``Snapshotter`` runs ``_do_build`` on a worker thread (reactivex's
``EventLoopScheduler``). After the build finishes, the snapshot must
reach ``world.push()`` on the **Qt main thread** so the downstream
``BehaviorSubject`` subscribers (capsule.render, expanded.render)
fire on the thread Qt widgets live on.

reactivex's own ``QtScheduler`` is not usable for this: its
``schedule_relative`` calls ``QTimer.singleShot(msec, callable)`` which
creates the timer in the calling thread, and from a non-Qt thread the
timer never fires. (Verified in ``tests/integration/
test_reactivex_qt_compat.py`` — the smoke test pinning the bug down.)

The Qt-native solution: a tiny ``QObject`` whose Signal is connected to
the slot via ``Qt.QueuedConnection``. Qt's signal mechanism queues the
slot call onto the receiver's thread's event loop — which IS the Qt
main thread when the QObject was constructed there. This is the
standard cross-thread pattern in Qt, well-documented and battle-tested.

Usage::

    marshaler = WorldMarshaler()
    snapshotter = Snapshotter(..., publish=marshaler.snap_ready.emit)
    # snap_ready.emit can be called from any thread; world.push runs
    # on the Qt main thread.
"""
from __future__ import annotations

from PySide6.QtCore import QObject, Qt, Signal

from claude_island.core.snapshot import WorldSnapshot, world


class WorldMarshaler(QObject):
    """Receives ``snap_ready`` from any thread; invokes ``world.push``
    on the Qt main thread (assuming this object was constructed on
    the main thread).

    Object lifetime: pin a reference somewhere (e.g. module-global in
    __main__.py) so the QObject isn't garbage-collected. A discarded
    QObject silently disconnects its slot.
    """

    # Payload type is ``object`` because Qt's Signal type system
    # doesn't understand frozen dataclasses without extra registration.
    # Object is the universal fallback — Qt forwards by reference, the
    # cost is just losing static type info on the connection edge.
    snap_ready: Signal = Signal(object)

    def __init__(self) -> None:
        super().__init__()
        # Direct connection to world.push — but with QueuedConnection
        # so the call always crosses to this object's thread (the Qt
        # main thread, by construction). When emit and slot are
        # already on the same thread, QueuedConnection still queues
        # via the event loop (one tick of latency, no semantic
        # difference for our use case).
        self.snap_ready.connect(
            self._on_snap_ready, Qt.ConnectionType.QueuedConnection,
        )

    def _on_snap_ready(self, snap: object) -> None:
        # Cast back to the real type for the call. Defensive:
        # ignore non-WorldSnapshot payloads rather than blow up the
        # event loop (Qt won't let arbitrary signals go through but
        # belt-and-braces against test fixtures or future callers).
        if isinstance(snap, WorldSnapshot):
            world.push(snap)
