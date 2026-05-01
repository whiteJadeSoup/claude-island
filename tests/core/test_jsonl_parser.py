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
    """Fresh UsageRegistry + JsonlParser pointed at tmp_path/projects/<hash>/."""
    db_path = tmp_path / "usage.db"
    projects_dir = tmp_path / "projects"
    project_hash_dir = projects_dir / "proj-hash"
    project_hash_dir.mkdir(parents=True)
    jsonl = project_hash_dir / "session-uuid.jsonl"
    jsonl.write_bytes(b"")  # empty file

    registry = UsageRegistry(db_path=db_path)
    parser = JsonlParser(usage_registry=registry, claude_projects_dir=projects_dir)
    yield registry, parser, jsonl
    registry.close()


def _row_count(reg: UsageRegistry) -> int:
    with reg._lock:
        return reg._conn.execute("SELECT COUNT(*) FROM usage_records").fetchone()[0]


def _input_tokens_total(reg: UsageRegistry) -> int:
    with reg._lock:
        row = reg._conn.execute(
            "SELECT COALESCE(SUM(input_tokens), 0) FROM usage_records"
        ).fetchone()
    return row[0]


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
    assert reg.get_offset(str(jsonl)) == expected_offset

    # Finish line3.
    with open(jsonl, "ab") as f:
        f.write(line3[half3:])
    parser.parse_file(jsonl)
    assert _row_count(reg) == 3
    assert _input_tokens_total(reg) == 60


def test_chunk_ending_exactly_on_newline_advances_to_eof(env):
    """Sanity: if the chunk ends on \\n, offset advances normally."""
    reg, parser, jsonl = env
    line = _line("2025-01-01T00:00:00Z", 100, 50)
    jsonl.write_bytes(line)
    parser.parse_file(jsonl)
    assert reg.get_offset(str(jsonl)) == len(line)
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
    offset_after_phase1 = reg.get_offset(str(jsonl))
    assert offset_after_phase1 == len(big)

    # Phase 2: simulate truncation — file shrinks to one fresh line.
    fresh = _line("2025-01-02T00:00:00Z", 100, 50)
    jsonl.write_bytes(fresh)  # write_bytes truncates and rewrites
    parser.parse_file(jsonl)

    # The new line must have been parsed (would be missed without the fix).
    assert _row_count(reg) == 6
    # Stored offset should now reflect the new (smaller) file size.
    assert reg.get_offset(str(jsonl)) == len(fresh)


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
    assert reg.get_offset(str(jsonl)) == 0


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
