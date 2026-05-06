"""Sentinel tab-title format for binding claude sessions to WT tabs.

Why this exists: Windows Terminal exposes only ``TabItem.Name`` (not
PID, not NativeWindowHandle, not AutomationId) in its UIA tree, and
the Microsoft team has Won't-Fix'd requests to expose more (issue
#5694). So the only path "external app → specific WT tab" is to make
each tab's Name globally unique and have UIA match by Name.

We achieve uniqueness by writing the session_uuid into the console
title via ``SetConsoleTitleW`` — WT mirrors the console title into
TabItem.Name. Format::

    ci:{uuid_hex_no_dashes}     e.g. "ci:a1b2c3d4e5f67890abcdef1234567890"

The ``ci:`` prefix lets reconcile distinguish "we set this" from
"someone else (claude topic shift, default profile name) set this".
The uuid is the SessionView's ``session_uuid``, with dashes stripped
for compactness — still 32 hex chars, collisions effectively zero.

Display name (the friendly label users see in the panel) is NOT in
the tab title — it lives in ``names_store`` and only feeds the panel
UI. The tab title is purely an opaque session identifier; users
don't read it from the WT tab strip (where it shows as 35 chars of
hex). This keeps panel rename and WT title fully decoupled.
"""
from __future__ import annotations


_PREFIX = "ci:"


def sentinel_title(session_uuid: str) -> str | None:
    """Return the sentinel title for a session, or ``None`` if uuid is empty.

    Empty uuid means we have no stable identity to bind to (degraded
    SessionView, scanner caught the process before its JSONL was
    parsed, etc.) — caller should skip reconcile in that case.
    """
    if not session_uuid:
        return None
    return f"{_PREFIX}{session_uuid.replace('-', '')}"


def is_sentinel(title: str) -> bool:
    """True iff the title was set by us (or matches our format).

    Used by reconcile to decide whether the current tab title is
    already correct and skip the AttachConsole + SetConsoleTitleW
    syscall. Match by prefix only — uuid uniqueness means a partial
    "ci:..." we didn't set wouldn't collide with anything we'd
    actually need to overwrite.
    """
    return bool(title) and title.startswith(_PREFIX)
