"""DormantSessionSource — the third source feeding ``Snapshotter``.

The other two sources are:

* ``SessionRegistry``  — driven by ``ProcessScanner``; lists live processes.
* ``LaunchIntentRegistry`` — short-lived store of "user just hit Resume"
  intents waiting to upgrade into live sessions.

This source is the **dormant** counterpart: every Claude Code session
that ever wrote a JSONL transcript on disk but isn't currently a live
process. The user needs them to recover after a reboot or terminal close,
when the only way to ``claude --resume <uuid>`` is to know the uuid.

Why a pure view layer (no IO) — JsonlParser.backfill_all already reads
every transcript into ``_session_meta`` at startup, and watchdog keeps
it warm afterwards. Re-globbing the projects dir here would duplicate
work and could disagree with the parser's view. So:

    DormantSessionSource.sessions =
        for each uuid in JsonlParser.known_session_uuids():
            meta = JsonlParser.get_session_metadata(uuid)
            cost, turns, _ = UsageRegistry.get_session_summary(uuid)
            yield DormantSession(...)  if meta has cwd + last_activity

Sessions whose meta is incomplete (no cwd or no last_activity) are
*dropped*: without those we can't safely Resume (no cwd to spawn in)
or sort (no recency key). This matches the design — silently degrading
on a half-parsed transcript beats showing a broken card the user can't
do anything with.
"""
from __future__ import annotations

from pathlib import Path
from typing import Protocol

from .models import DormantSession


class _MetadataProviderProto(Protocol):
    def known_session_uuids(self) -> set[str]: ...
    def get_session_metadata(self, uuid: str) -> dict: ...


class _UsageRegistryProto(Protocol):
    def get_session_summary(self, uuid: str) -> tuple[float, int, int]: ...


class DormantSessionSource:
    """Lists every JSONL-backed session as a ``DormantSession``.

    The ``Snapshotter`` later filters this list against live + launching
    uuids before publishing — this class is unaware of that reconcile.
    Pure read of the parser's existing in-memory state; safe to call
    from any thread (the parser's reads are lock-protected internally).
    """

    def __init__(
        self,
        *,
        jsonl_parser: _MetadataProviderProto,
        usage_registry: _UsageRegistryProto,
    ) -> None:
        self._parser = jsonl_parser
        self._usage = usage_registry

    @property
    def sessions(self) -> list[DormantSession]:
        out: list[DormantSession] = []
        for uuid in self._parser.known_session_uuids():
            try:
                meta = self._parser.get_session_metadata(uuid)
            except Exception:
                continue
            cwd_str = meta.get("cwd")
            last_activity = meta.get("last_activity")
            # Drop sessions we can't safely act on:
            # - no cwd: we can't spawn `claude --resume <uuid>` anywhere
            #   (resume requires running in the original cwd).
            # - no last_activity: can't sort, and the transcript may be
            #   so empty there's nothing to resume to.
            if not isinstance(cwd_str, str) or not cwd_str:
                continue
            if last_activity is None:
                continue

            try:
                cost, turns, _sides = self._usage.get_session_summary(uuid)
            except Exception:
                cost, turns = 0.0, 0

            out.append(DormantSession(
                session_uuid=uuid,
                cwd=Path(cwd_str),
                name=meta.get("ai_title") if isinstance(meta.get("ai_title"), str) else None,
                last_prompt=meta.get("last_prompt") if isinstance(meta.get("last_prompt"), str) else None,
                last_activity=last_activity,
                started_at=meta.get("started_at"),
                permission_mode=meta.get("permission_mode") if isinstance(meta.get("permission_mode"), str) else None,
                git_branch=meta.get("git_branch") if isinstance(meta.get("git_branch"), str) else None,
                cost_usd=float(cost),
                turn_count=int(turns),
            ))
        return out
