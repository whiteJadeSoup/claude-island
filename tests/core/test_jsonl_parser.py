"""Regression tests for JsonlParser incremental offset handling."""

from __future__ import annotations

import tempfile
from datetime import datetime, timezone
from pathlib import Path

import pytest

from claude_island.core.jsonl_parser import JsonlParser
from claude_island.core.usage_registry import UsageRegistry


def _line(timestamp: str, input_tokens: int, output_tokens: int) -> bytes:
    """Build a minimal Claude-Code-shaped assistant turn JSONL line (with \\n)."""
    payload = (
        '{"type":"assistant","timestamp":"' + timestamp + '",'
        '"message":{"model":"claude-sonnet-4-5",'
        '"usage":{"input_tokens":' + str(input_tokens)
        + ',"output_tokens":' + str(output_tokens)
        + ',"cache_creation_input_tokens":0,"cache_read_input_tokens":0}}}\n'
    )
    return payload.encode("utf-8")


@pytest.fixture
def env(tmp_path):
    """Fresh in-memory UsageRegistry + JsonlParser pointed at
    tmp_path/projects/<hash>/. The registry needs no path arg now —
    JSONL is the only source of truth and the registry is just a
    Python list rebuilt at startup."""
    projects_dir = tmp_path / "projects"
    project_hash_dir = projects_dir / "proj-hash"
    project_hash_dir.mkdir(parents=True)
    jsonl = project_hash_dir / "session-uuid.jsonl"
    jsonl.write_bytes(b"")  # empty file

    registry = UsageRegistry()
    parser = JsonlParser(usage_registry=registry, claude_projects_dir=projects_dir)
    yield registry, parser, jsonl


def _row_count(reg: UsageRegistry) -> int:
    with reg._lock:
        return len(reg._records)


def _input_tokens_total(reg: UsageRegistry) -> int:
    with reg._lock:
        return sum(r.input_tokens for r in reg._records)


# --------------------------------------------------------------------------
# Existing UTF-8 fix regression
# --------------------------------------------------------------------------

def test_utf8_explicit_decode_does_not_crash_on_leading_null_bytes(env):
    """A JSONL line whose first bytes look like utf-32-be must not crash the parser.

    Regression for commit b82469d: passing raw bytes to json.loads triggers
    heuristic encoding detection that sometimes picks utf-32-be and raises
    UnicodeDecodeError. We must catch it and skip the line.
    """
    _, parser, jsonl = env
    # Carefully craft a junk line whose first 2 bytes are NUL (defeating the
    # heuristic) followed by something json.loads would also reject.
    bad = b"\x00\x00garbage that is not json\n"
    good = _line("2025-01-01T00:00:00Z", 100, 50)
    jsonl.write_bytes(bad + good)

    parser.parse_file(jsonl)  # must not raise

    # The good line still got parsed.
    reg, _, _ = env
    assert _row_count(reg) == 1
    assert _input_tokens_total(reg) == 100


# --------------------------------------------------------------------------
# B1: partial-line race
# --------------------------------------------------------------------------

def test_partial_line_does_not_advance_offset_past_unfinished_data(env):
    """If the file ends mid-line (writer mid-flush), the offset must NOT
    advance past the trailing partial fragment. Otherwise the second half
    of that line is read as orphan junk on the next call and silently lost.
    """
    reg, parser, jsonl = env

    # First half of a JSONL line, no trailing newline (writer is mid-flush).
    line = _line("2025-01-01T00:00:00Z", 100, 50)
    half = len(line) // 2
    jsonl.write_bytes(line[:half])

    parser.parse_file(jsonl)
    assert _row_count(reg) == 0  # nothing complete yet

    # Writer finishes the line.
    with open(jsonl, "ab") as f:
        f.write(line[half:])

    parser.parse_file(jsonl)

    # After the second call, the FULL line must have been parsed exactly once.
    assert _row_count(reg) == 1
    assert _input_tokens_total(reg) == 100


def test_partial_line_offset_rewinds_to_last_complete_boundary(env):
    """A chunk with [complete\\n][complete\\n][partial] should commit offset
    to the start of [partial], not to fh.tell()."""
    reg, parser, jsonl = env

    line1 = _line("2025-01-01T00:00:00Z", 10, 5)
    line2 = _line("2025-01-01T00:00:01Z", 20, 5)
    line3 = _line("2025-01-01T00:00:02Z", 30, 5)
    half3 = len(line3) // 2

    jsonl.write_bytes(line1 + line2 + line3[:half3])
    parser.parse_file(jsonl)
    assert _row_count(reg) == 2  # only the complete lines

    # Stored offset should equal len(line1+line2), not full file size.
    expected_offset = len(line1) + len(line2)
    assert parser._offsets.get(str(jsonl), 0) == expected_offset

    # Finish line3.
    with open(jsonl, "ab") as f:
        f.write(line3[half3:])
    parser.parse_file(jsonl)
    assert _row_count(reg) == 3
    assert _input_tokens_total(reg) == 60


def test_session_metadata_extracted_from_special_rows(env):
    """JsonlParser tracks per-session metadata from rows that don't
    carry usage themselves: ``ai-title`` / ``last-prompt`` / any row
    with gitBranch + version. Used by the hover tooltip."""
    reg, parser, jsonl = env
    rows = [
        # An assistant turn with usage — also carries gitBranch + version.
        b'{"type":"assistant","timestamp":"2025-01-01T00:00:00Z",'
        b'"gitBranch":"feat-x","version":"2.1.123",'
        b'"message":{"model":"claude-sonnet-4-5",'
        b'"id":"msg_001",'
        b'"usage":{"input_tokens":1,"output_tokens":1,'
        b'"cache_creation_input_tokens":0,"cache_read_input_tokens":0}}}\n',
        # Special rows without usage but with metadata.
        b'{"type":"ai-title","aiTitle":"Refactor scanner to async iter",'
        b'"sessionId":"session-uuid"}\n',
        b'{"type":"last-prompt","lastPrompt":"please refactor this",'
        b'"sessionId":"session-uuid"}\n',
    ]
    jsonl.write_bytes(b"".join(rows))
    parser.parse_file(jsonl)

    meta = parser.get_session_metadata("session-uuid")
    assert meta["ai_title"] == "Refactor scanner to async iter"
    assert meta["last_prompt"] == "please refactor this"
    assert meta["git_branch"] == "feat-x"
    assert meta["version"] == "2.1.123"


def test_session_metadata_unknown_session_returns_empty(env):
    reg, parser, _ = env
    assert parser.get_session_metadata("nope") == {}


def test_chunk_ending_exactly_on_newline_advances_to_eof(env):
    """Sanity: if the chunk ends on \\n, offset advances normally."""
    reg, parser, jsonl = env
    line = _line("2025-01-01T00:00:00Z", 100, 50)
    jsonl.write_bytes(line)
    parser.parse_file(jsonl)
    assert parser._offsets.get(str(jsonl), 0) == len(line)
    assert _row_count(reg) == 1


# --------------------------------------------------------------------------
# B4: parser uses record_many → exactly one totals_changed per parse
# --------------------------------------------------------------------------

def test_parsing_chunk_with_many_lines_emits_totals_once(env):
    """A chunk holding 100 assistant turns must trigger a single
    totals_changed (via record_many), not 100 emits. Otherwise backfill
    floods the UI with redundant SELECT-and-redraw cycles."""
    reg, parser, jsonl = env
    received = []
    reg.totals_changed.subscribe(received.append)

    lines = b"".join(
        _line(f"2025-01-01T00:00:{i:02d}Z", 10, 5)
        for i in range(60)
    )
    jsonl.write_bytes(lines)
    parser.parse_file(jsonl)

    assert _row_count(reg) == 60
    assert len(received) == 1


# --------------------------------------------------------------------------
# B3: backfill cooperative cancellation
# --------------------------------------------------------------------------

def test_request_stop_aborts_backfill_at_next_file(env, tmp_path):
    """If shutdown signals stop while backfill is iterating, it must bail
    at the next file boundary — otherwise we close() the SQLite connection
    while the daemon thread is still mid-write, and it raises
    ProgrammingError (or worse, leaves inconsistent rows + offsets behind)."""
    reg, parser, _ = env

    # Drop several JSONL files into the projects dir so backfill has work.
    project_dir = parser._projects_dir / "proj-hash"
    for i in range(20):
        f = project_dir / f"sess-{i}.jsonl"
        f.write_bytes(_line(f"2025-01-01T00:00:{i:02d}Z", 10, 5))

    # Stop BEFORE backfill runs — it should process zero files.
    parser.request_stop()
    parser.backfill_all()
    assert _row_count(reg) == 0


def test_backfill_runs_to_completion_when_not_stopped(env):
    """Sanity: without request_stop, backfill processes everything."""
    reg, parser, _ = env

    project_dir = parser._projects_dir / "proj-hash"
    for i in range(5):
        f = project_dir / f"sess-{i}.jsonl"
        f.write_bytes(_line(f"2025-01-01T00:00:{i:02d}Z", 10, 5))

    parser.backfill_all()
    assert _row_count(reg) == 5


def test_request_stop_is_idempotent(env):
    """Calling stop twice should not raise. Shutdown code may be defensive."""
    _, parser, _ = env
    parser.request_stop()
    parser.request_stop()  # must not raise


# --------------------------------------------------------------------------
# Q1: file truncation / rotation
# --------------------------------------------------------------------------

def test_truncation_resets_offset_and_reparses(env):
    """User deletes ~/.claude/projects to clean up; a new session re-creates
    the same path with a fresh, smaller file. Without truncation detection,
    the stored offset would still point past the new file's EOF, and all
    future writes (until size > old offset) would be silently lost."""
    reg, parser, jsonl = env

    # Phase 1: write 5 lines, parse, advance offset.
    big = b"".join(_line(f"2025-01-01T00:00:{i:02d}Z", 10, 5) for i in range(5))
    jsonl.write_bytes(big)
    parser.parse_file(jsonl)
    assert _row_count(reg) == 5
    offset_after_phase1 = parser._offsets.get(str(jsonl), 0)
    assert offset_after_phase1 == len(big)

    # Phase 2: simulate truncation — file shrinks to one fresh line.
    fresh = _line("2025-01-02T00:00:00Z", 100, 50)
    jsonl.write_bytes(fresh)  # write_bytes truncates and rewrites
    parser.parse_file(jsonl)

    # The new line must have been parsed (would be missed without the fix).
    assert _row_count(reg) == 6
    # Stored offset should now reflect the new (smaller) file size.
    assert parser._offsets.get(str(jsonl), 0) == len(fresh)


def test_truncation_to_empty_file_is_safe(env):
    reg, parser, jsonl = env

    jsonl.write_bytes(_line("2025-01-01T00:00:00Z", 10, 5))
    parser.parse_file(jsonl)
    assert _row_count(reg) == 1

    # Truncate to empty.
    jsonl.write_bytes(b"")
    parser.parse_file(jsonl)  # must not raise

    # No new rows; offset reset to 0.
    assert _row_count(reg) == 1
    assert parser._offsets.get(str(jsonl), 0) == 0


def test_file_growth_without_truncation_unchanged(env):
    """Sanity: a normal append (file grows, doesn't shrink) must NOT
    trigger the reset path."""
    reg, parser, jsonl = env

    jsonl.write_bytes(_line("2025-01-01T00:00:00Z", 10, 5))
    parser.parse_file(jsonl)

    # Append a second line.
    with open(jsonl, "ab") as f:
        f.write(_line("2025-01-01T00:00:01Z", 20, 10))
    parser.parse_file(jsonl)

    assert _row_count(reg) == 2  # both lines, no double-counting


# --------------------------------------------------------------------------
# Q7: timestamp normalisation
# --------------------------------------------------------------------------

def _line_with_ts(ts: str, input_tokens: int = 10) -> bytes:
    """Like _line but lets the test pick the raw timestamp string."""
    payload = (
        '{"type":"assistant","timestamp":"' + ts + '",'
        '"message":{"model":"claude-sonnet-4-5",'
        '"usage":{"input_tokens":' + str(input_tokens)
        + ',"output_tokens":5,"cache_creation_input_tokens":0,'
        '"cache_read_input_tokens":0}}}\n'
    )
    return payload.encode("utf-8")


def test_parse_ts_handles_z_suffix():
    from claude_island.core.jsonl_parser import _parse_ts
    ts = _parse_ts({"timestamp": "2025-01-01T12:34:56.789Z"})
    assert ts is not None
    assert ts.tzinfo is not None
    assert ts.utcoffset().total_seconds() == 0


def test_parse_ts_handles_explicit_offset():
    from claude_island.core.jsonl_parser import _parse_ts
    ts = _parse_ts({"timestamp": "2025-01-01T12:34:56.789+02:00"})
    assert ts is not None
    # Normalised to UTC: 12:34 +02:00 → 10:34 UTC
    assert ts.hour == 10
    assert ts.utcoffset().total_seconds() == 0


def test_parse_ts_assumes_utc_for_naive_input():
    """Q7 trap (b): naive datetime stored as ISO without +00:00 suffix
    sorts BEFORE timestamps that have the suffix in lex order, so
    'today' rows would silently fall outside the daily window."""
    from claude_island.core.jsonl_parser import _parse_ts
    ts = _parse_ts({"timestamp": "2025-01-01T12:34:56"})
    assert ts is not None
    assert ts.tzinfo is not None
    # The serialised form must include the offset so str >= filtering works.
    assert "+00:00" in ts.isoformat()


def test_parse_ts_truncates_excess_fractional_digits():
    """Q7 trap (a): fromisoformat rejects 7+ fractional digits."""
    from claude_island.core.jsonl_parser import _parse_ts
    # Nanosecond precision (9 digits): would raise without the truncation.
    ts = _parse_ts({"timestamp": "2025-01-01T12:34:56.123456789Z"})
    assert ts is not None
    # Truncated to microseconds (6 digits).
    assert ts.microsecond == 123456


def test_parse_ts_returns_none_for_invalid():
    from claude_island.core.jsonl_parser import _parse_ts
    assert _parse_ts({"timestamp": "not a timestamp"}) is None
    assert _parse_ts({"timestamp": None}) is None
    assert _parse_ts({}) is None


def test_naive_timestamp_row_is_included_in_daily_totals(env):
    """End-to-end: a JSONL line with a naive timestamp (no Z, no offset)
    parsed today must show up in get_totals('daily'). Without Q7 it would
    silently drop out due to lex-order comparison against the +00:00 cutoff."""
    reg, parser, jsonl = env

    # Use today's date (so the row falls inside the 24h window) without a Z.
    now = datetime.now(timezone.utc).replace(microsecond=0)
    naive_iso = now.replace(tzinfo=None).isoformat()  # no +00:00
    jsonl.write_bytes(_line_with_ts(naive_iso, input_tokens=42))

    parser.parse_file(jsonl)

    totals = reg.get_totals("daily")
    assert totals.input_tokens == 42, (
        "naive-timestamp row dropped from daily totals due to lex-order bug"
    )


def test_earliest_timestamp_becomes_started_at(env):
    """get_session_metadata returns the earliest timestamp as 'started_at',
    enabling the detail popup to show 'Created' even when
    ~/.claude/sessions/<pid>.json is absent (MiniMax sessions)."""
    reg, parser, jsonl = env

    ts_old = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    ts_mid = datetime(2026, 1, 1, 14, 0, 0, tzinfo=timezone.utc)
    ts_new = datetime(2026, 1, 1, 16, 0, 0, tzinfo=timezone.utc)
    jsonl.write_bytes(
        _line_with_ts(ts_old.isoformat(), input_tokens=10) + b"\n"
        + _line_with_ts(ts_mid.isoformat(), input_tokens=20) + b"\n"
        + _line_with_ts(ts_new.isoformat(), input_tokens=30) + b"\n"
    )

    parser.parse_file(jsonl)

    meta = parser.get_session_metadata(jsonl.stem)
    assert meta.get("started_at") is not None
    assert meta["started_at"] == ts_old  # earliest, not latest


# --------------------------------------------------------------------------
# Phase 1 (resume-offline): last_activity / cwd / permission_mode capture
# --------------------------------------------------------------------------

def test_latest_timestamp_becomes_last_activity(env):
    """get_session_metadata returns the LATEST timestamp as 'last_activity',
    so DormantSessionSource can sort offline sessions by recency without
    re-stat'ing each .jsonl file."""
    reg, parser, jsonl = env

    ts_old = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    ts_mid = datetime(2026, 1, 1, 14, 0, 0, tzinfo=timezone.utc)
    ts_new = datetime(2026, 1, 1, 16, 0, 0, tzinfo=timezone.utc)
    # Write rows out-of-order to ensure tracking is by max(), not last-row.
    jsonl.write_bytes(
        _line_with_ts(ts_mid.isoformat(), input_tokens=10) + b"\n"
        + _line_with_ts(ts_new.isoformat(), input_tokens=20) + b"\n"
        + _line_with_ts(ts_old.isoformat(), input_tokens=30) + b"\n"
    )

    parser.parse_file(jsonl)

    meta = parser.get_session_metadata(jsonl.stem)
    assert meta.get("last_activity") == ts_new  # latest, not last-written


def test_last_activity_advances_across_incremental_parses(env):
    """When a transcript grows (later append), last_activity must advance.
    Important for 'session became active again' transitions in the UI."""
    reg, parser, jsonl = env

    ts_first = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    jsonl.write_bytes(_line_with_ts(ts_first.isoformat()))
    parser.parse_file(jsonl)
    assert parser.get_session_metadata(jsonl.stem)["last_activity"] == ts_first

    ts_later = datetime(2026, 1, 1, 18, 0, 0, tzinfo=timezone.utc)
    with open(jsonl, "ab") as f:
        f.write(_line_with_ts(ts_later.isoformat()))
    parser.parse_file(jsonl)
    assert parser.get_session_metadata(jsonl.stem)["last_activity"] == ts_later


def test_cwd_captured_from_first_row(env):
    """get_session_metadata returns 'cwd' from the first row carrying it.
    DormantSessionSource needs cwd to pass to TerminalLauncher.spawn()."""
    reg, parser, jsonl = env
    rows = [
        b'{"type":"user","timestamp":"2026-01-01T12:00:00Z",'
        b'"cwd":"D:\\\\projects\\\\foo",'
        b'"sessionId":"session-uuid",'
        b'"message":{"role":"user","content":"hi"}}\n',
        # A later row with a different cwd (shouldn't happen in practice
        # but verifies first-wins semantics).
        b'{"type":"user","timestamp":"2026-01-01T13:00:00Z",'
        b'"cwd":"D:\\\\projects\\\\bar",'
        b'"sessionId":"session-uuid",'
        b'"message":{"role":"user","content":"hi2"}}\n',
    ]
    jsonl.write_bytes(b"".join(rows))
    parser.parse_file(jsonl)

    meta = parser.get_session_metadata(jsonl.stem)
    assert meta.get("cwd") == "D:\\projects\\foo"  # first wins


def test_permission_mode_captured_from_inline_field(env):
    """permissionMode rides on regular user/assistant rows; latest-wins so
    the value reflects what the user had set when they last interacted."""
    reg, parser, jsonl = env
    rows = [
        b'{"type":"user","timestamp":"2026-01-01T12:00:00Z",'
        b'"permissionMode":"default","sessionId":"session-uuid",'
        b'"message":{"role":"user","content":"a"}}\n',
        b'{"type":"user","timestamp":"2026-01-01T13:00:00Z",'
        b'"permissionMode":"bypassPermissions","sessionId":"session-uuid",'
        b'"message":{"role":"user","content":"b"}}\n',
    ]
    jsonl.write_bytes(b"".join(rows))
    parser.parse_file(jsonl)

    meta = parser.get_session_metadata(jsonl.stem)
    assert meta.get("permission_mode") == "bypassPermissions"  # latest wins


def test_permission_mode_captured_from_dedicated_flip_row(env):
    """Claude Code writes a dedicated {type:'permission-mode'} row when the
    user toggles modes mid-session (Shift+Tab). Must be picked up too."""
    reg, parser, jsonl = env
    rows = [
        b'{"type":"user","timestamp":"2026-01-01T12:00:00Z",'
        b'"permissionMode":"default","sessionId":"session-uuid",'
        b'"message":{"role":"user","content":"a"}}\n',
        b'{"type":"permission-mode","permissionMode":"plan",'
        b'"sessionId":"session-uuid"}\n',
    ]
    jsonl.write_bytes(b"".join(rows))
    parser.parse_file(jsonl)

    meta = parser.get_session_metadata(jsonl.stem)
    assert meta.get("permission_mode") == "plan"


# --------------------------------------------------------------------------
# Subagent handling
#
# Claude Code stores subagent transcripts at
#   <projects_dir>/<slug>/<parent-sid>/subagents/agent-<aid>.jsonl
# (and a workflows/<runId>/ subdir variant). We must:
#   1. Not register agent-<aid> as a standalone session in HISTORY.
#   2. Roll the subagent's UsageRecords up to the parent's session_uuid
#      so the parent's cost / sidechain_count are correct.
#   3. Bump activity under the project slug (not "subagents") so
#      SessionRegistry's per-project join routes the bump to the parent.
#   4. Not pollute the parent's metadata dict with subagent fields.
# --------------------------------------------------------------------------


def _subagent_env(tmp_path):
    """Like the `env` fixture, but lays out one parent jsonl + one
    subagent jsonl + one workflow-nested subagent jsonl, all under a
    single project slug. Returns (registry, parser, parent_jsonl,
    subagent_jsonl, workflow_subagent_jsonl)."""
    projects_dir = tmp_path / "projects"
    slug_dir = projects_dir / "proj-hash"
    slug_dir.mkdir(parents=True)
    parent_uuid = "parent-sid"
    parent_jsonl = slug_dir / f"{parent_uuid}.jsonl"
    parent_jsonl.write_bytes(b"")
    sub_dir = slug_dir / parent_uuid / "subagents"
    sub_dir.mkdir(parents=True)
    subagent_jsonl = sub_dir / "agent-aaa.jsonl"
    subagent_jsonl.write_bytes(b"")
    wf_dir = sub_dir / "workflows" / "run-1"
    wf_dir.mkdir(parents=True)
    workflow_jsonl = wf_dir / "agent-bbb.jsonl"
    workflow_jsonl.write_bytes(b"")

    registry = UsageRegistry()
    parser = JsonlParser(usage_registry=registry, claude_projects_dir=projects_dir)
    return registry, parser, parent_jsonl, subagent_jsonl, workflow_jsonl


def test_known_session_uuids_excludes_subagents(tmp_path):
    """HISTORY drawer is built from known_session_uuids — subagent
    files showing up here would render as broken pseudo-sessions
    (this is exactly the regression the subagent fix was written for)."""
    _, parser, parent_jsonl, subagent_jsonl, workflow_jsonl = _subagent_env(tmp_path)
    parent_jsonl.write_bytes(_line("2025-01-01T00:00:00Z", 1, 1))
    subagent_jsonl.write_bytes(_line("2025-01-01T00:00:01Z", 1, 1))
    workflow_jsonl.write_bytes(_line("2025-01-01T00:00:02Z", 1, 1))

    uuids = parser.known_session_uuids()
    assert uuids == {"parent-sid"}
    # Both subagent shapes (direct + workflow-nested) must be excluded.
    assert "agent-aaa" not in uuids
    assert "agent-bbb" not in uuids


def test_subagent_cost_attributed_to_parent_session_uuid(tmp_path):
    """The parent's UsageRegistry totals must include subagent cost.
    Tested by inspecting the records written into the registry — a
    subagent record's session_uuid must equal the parent's, so the
    detail popup's per-session aggregation rolls them up."""
    registry, parser, parent_jsonl, subagent_jsonl, _ = _subagent_env(tmp_path)
    parent_jsonl.write_bytes(_line("2025-01-01T00:00:00Z", 100, 50))
    subagent_jsonl.write_bytes(
        _line("2025-01-01T00:00:01Z", 200, 80)
        + _line("2025-01-01T00:00:02Z", 300, 90)
    )

    parser.parse_file(parent_jsonl)
    parser.parse_file(subagent_jsonl)

    with registry._lock:
        per_uuid: dict[str, int] = {}
        for r in registry._records:
            per_uuid[r.session_uuid] = per_uuid.get(r.session_uuid, 0) + r.input_tokens
    # All three rows must end up under "parent-sid" — none under
    # "agent-aaa" (which would be the bug pre-fix).
    assert per_uuid == {"parent-sid": 100 + 200 + 300}


def test_subagent_records_marked_is_sidechain(tmp_path):
    """Records sourced from a subagent file must carry is_sidechain=True
    so the parent's sidechain_count (computed in UsageRegistry) is
    accurate. Forced True regardless of what the row's own isSidechain
    flag says — the file's location IS the proof."""
    registry, parser, _, subagent_jsonl, _ = _subagent_env(tmp_path)
    # _line() doesn't stamp isSidechain — verify the file location
    # alone is enough to flip the flag.
    subagent_jsonl.write_bytes(_line("2025-01-01T00:00:00Z", 10, 5))
    parser.parse_file(subagent_jsonl)

    with registry._lock:
        records = list(registry._records)
    assert len(records) == 1
    assert records[0].is_sidechain is True


def test_subagent_does_not_pollute_parent_metadata(tmp_path):
    """Subagent transcripts may carry their own ai-title / git-branch
    rows. Those must NOT overwrite the parent's _session_meta entry
    — the parent owns its identity. Verified by writing an ai-title
    only in the subagent file and asserting the parent's meta is
    still empty for that key."""
    _, parser, parent_jsonl, subagent_jsonl, _ = _subagent_env(tmp_path)
    parent_jsonl.write_bytes(_line("2025-01-01T00:00:00Z", 1, 1))
    subagent_jsonl.write_bytes(
        b'{"type":"ai-title","aiTitle":"subagent-title",'
        b'"timestamp":"2025-01-01T00:00:01Z"}\n'
    )

    parser.parse_file(parent_jsonl)
    parser.parse_file(subagent_jsonl)

    parent_meta = parser.get_session_metadata("parent-sid")
    assert parent_meta.get("ai_title") is None
    # The subagent stem is NOT a real session uuid; meta lookup returns
    # the empty default (we never write anything under it).
    sub_meta = parser.get_session_metadata("agent-aaa")
    assert sub_meta == {}


def test_subagent_activity_emits_under_project_slug_not_subagents_dir(tmp_path):
    """`activity_updated` payload must carry the project slug (so
    SessionRegistry's per-project join routes the bump to live
    sessions in that project), NOT "subagents" / "<runId>" — the
    parent dir name of a subagent file. Without this fix the parent
    session never sees the activity bump and stays "idle" while a
    subagent is mid-run."""
    _, parser, _, subagent_jsonl, workflow_jsonl = _subagent_env(tmp_path)
    received: list[tuple[str, datetime]] = []
    parser.activity_updated.subscribe(lambda payload: received.append(payload))

    subagent_jsonl.write_bytes(_line("2025-01-01T00:00:00Z", 1, 1))
    parser.parse_file(subagent_jsonl)

    workflow_jsonl.write_bytes(_line("2025-01-01T00:00:00Z", 1, 1))
    parser.parse_file(workflow_jsonl)

    # Both emissions must use the slug, not the immediate parent dir
    # ("subagents" or the workflow runId).
    slugs_emitted = {payload[0] for payload in received}
    assert slugs_emitted == {"proj-hash"}


def test_subagent_path_helpers_are_path_only(tmp_path):
    """Pin the contract: classification is purely structural — no
    file I/O, no content lookup, decided from path alone. Important
    because backfill races (subagent parsed before parent, or vice
    versa) must not change classification."""
    from claude_island.core.jsonl_parser import (
        _is_main_session_file, _project_slug, _subagent_parent_uuid,
    )
    projects_dir = tmp_path / "projects"
    main = projects_dir / "slug" / "main-uuid.jsonl"
    direct = projects_dir / "slug" / "main-uuid" / "subagents" / "agent-x.jsonl"
    workflow = projects_dir / "slug" / "main-uuid" / "subagents" / "workflows" / "r1" / "agent-y.jsonl"
    outside = tmp_path / "elsewhere" / "stray.jsonl"

    assert _is_main_session_file(main, projects_dir) is True
    assert _is_main_session_file(direct, projects_dir) is False
    assert _is_main_session_file(workflow, projects_dir) is False
    assert _is_main_session_file(outside, projects_dir) is False  # not under projects_dir

    assert _subagent_parent_uuid(main, projects_dir) is None
    assert _subagent_parent_uuid(direct, projects_dir) == "main-uuid"
    assert _subagent_parent_uuid(workflow, projects_dir) == "main-uuid"
    assert _subagent_parent_uuid(outside, projects_dir) is None

    assert _project_slug(main, projects_dir) == "slug"
    assert _project_slug(direct, projects_dir) == "slug"
    assert _project_slug(workflow, projects_dir) == "slug"
    assert _project_slug(outside, projects_dir) is None
