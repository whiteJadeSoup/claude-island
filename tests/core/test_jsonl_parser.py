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
