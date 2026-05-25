"""Drive WindowsTerminalAdapter.focus() directly for every live
claude.exe pid and report which one triggers the tab-auto-switch
diagnostic.

This bypasses the GUI entirely (no panel click needed) so it works
regardless of where the panel is positioned on the user's screen.
The focus() path it exercises is byte-for-byte the same one the
click handler runs from inside ExpandedWindow.
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

import psutil

from claude_island.core.models import Session
from claude_island.core.snapshot import SessionView
from claude_island.core.session_phase import SessionPhase
from claude_island.core.hook_events import JumpTarget
from claude_island.platform_.process_scanner import ProcessScanner
from claude_island.platform_.session_state import read_session_state
from claude_island.platform_.terminals.windows_terminal import (
    WindowsTerminalAdapter,
)


def _canonical_uuid(session: Session) -> str:
    state = read_session_state(session.pid) or {}
    pid_json_uuid = state.get("sessionId") if isinstance(state.get("sessionId"), str) else None
    return pid_json_uuid or session.session_uuid


def main() -> None:
    sessions = ProcessScanner().scan()
    if not sessions:
        raise SystemExit("no live claude.exe processes")
    print(f"found {len(sessions)} live claude.exe sessions:")
    for s in sessions:
        uid = _canonical_uuid(s)
        print(f"  pid={s.pid} cwd={s.project_path} uuid={uid}")

    adapter = WindowsTerminalAdapter()
    for s in sessions:
        canonical_uuid = _canonical_uuid(s)
        # Compute same-cwd siblings (the focus() code uses them for
        # split-pane disambiguation).
        siblings = []
        for other in sessions:
            if other.pid != s.pid and other.project_path == s.project_path:
                other_uuid = _canonical_uuid(other)
                siblings.append(SessionView(
                    pid=other.pid,
                    name=str(other.project_path.name),
                    project_path=other.project_path,
                    project_basename=other.project_path.name or str(other.project_path),
                    last_activity=other.last_activity,
                    cost_usd=0.0,
                    is_high_cost=False,
                    latest_model=None,
                    status_word=None,
                    session_uuid=other_uuid,
                    session=other,
                    phase=SessionPhase.IDLE,
                    current_tool=None,
                    last_prompt=None,
                    last_assistant_message=None,
                    jump_target=None,
                ))
        view = SessionView(
            pid=s.pid,
            name=str(s.project_path.name),
            project_path=s.project_path,
            project_basename=s.project_path.name or str(s.project_path),
            last_activity=s.last_activity,
            cost_usd=0.0,
            is_high_cost=False,
            latest_model=None,
            status_word=None,
            session_uuid=canonical_uuid,
            session=s,
            phase=SessionPhase.IDLE,
            current_tool=None,
            last_prompt=None,
            last_assistant_message=None,
            jump_target=None,
        )
        print()
        print(f"========================= calling focus() for pid={s.pid} =========================")
        print(f"  cwd={s.project_path}")
        print(f"  uuid={canonical_uuid}")
        print(f"  siblings={[(sb.session.pid, sb.session_uuid[:12]) for sb in siblings]}")
        sys.stdout.flush()
        try:
            ok = adapter.focus(view, siblings=tuple(siblings))
            print(f"  focus returned: {ok}")
        except Exception as e:
            import traceback
            traceback.print_exc()
            print(f"  focus raised: {e}")
        sys.stdout.flush()


if __name__ == "__main__":
    main()
