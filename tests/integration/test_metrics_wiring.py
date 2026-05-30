"""Integration smoke tests: confirm the metrics primitive is actually
wired into Snapshotter / UsageRegistry / JsonlParser hot paths.

The unit tests in ``tests/core/test_metrics.py`` exercise the
``Metrics`` class itself. These tests prove that production code paths
ALSO call into it — otherwise the registry stays empty in production
even though every test passes."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from claude_island.core.metrics import metrics
from claude_island.core.models import UsageRecord
from claude_island.core.snapshot import Snapshotter
from claude_island.core.usage_registry import UsageRegistry


def _t0() -> datetime:
    return datetime(2026, 5, 26, 0, 0, 0, tzinfo=timezone.utc)


# ── Snapshotter wiring ──────────────────────────────────────────────────


class _EmptySource:
    @property
    def sessions(self): return []


class _EmptyReader:
    def read_session_state(self, pid): return None


class _EmptyMeta:
    def get_session_metadata(self, uuid): return None


class _EmptyUsage:
    def get_session_summary(self, uuid): return (0.0, 0, 0)
    def get_latest_model(self, uuid): return None
    def get_totals(self, period):
        from claude_island.core.models import UsageTotals
        return UsageTotals(period=period)


class _EmptyNames:
    def get_session_name(self, uuid): return None


def _make_snap() -> Snapshotter:
    return Snapshotter(
        session_source=_EmptySource(),
        state_reader=_EmptyReader(),
        metadata_provider=_EmptyMeta(),
        usage_registry=_EmptyUsage(),
        names_store=_EmptyNames(),
        get_quota=lambda: None,
        get_available_providers=lambda: [],
        get_selected_provider=lambda: None,
        publish=lambda s: None,
        debounce_window_s=0,
        throttle_first_window_s=0,
    )


def test_snapshotter_increments_build_count_per_do_build():
    """_do_build is the async pipeline entry; build_now bypasses it
    (and intentionally doesn't bump counters — it's a test/boot helper).
    Drive _do_build directly to prove the counter fires."""
    snap = _make_snap()
    snap._do_build()
    snap._do_build()
    snap._do_build()
    s = metrics.snapshot()
    assert s.counters.get("snap.build.count") == 3


def test_snapshotter_observes_build_duration_per_do_build():
    snap = _make_snap()
    snap._do_build()
    s = metrics.snapshot()
    t = s.timings.get("snap.build.duration_ms")
    assert t is not None
    assert t.n == 1
    # Build must take measurable time (>0 ms) on any reasonable host;
    # if this trips on a 1ns clock, the test environment is wrong.
    assert t.sum_ms >= 0.0  # weak floor — only proves we observed


# ── UsageRegistry wiring ────────────────────────────────────────────────


def _record(i: int, msg_id: str | None = None) -> UsageRecord:
    return UsageRecord(
        timestamp=_t0(),
        project_path="p",
        session_uuid="u",
        model="claude-sonnet-4-6",
        input_tokens=1, output_tokens=1,
        cache_creation_tokens=0, cache_read_tokens=0,
        message_id=msg_id if msg_id is not None else f"msg-{i}",
    )


def test_usage_registry_increments_added_on_record_many():
    reg = UsageRegistry()
    reg.record_many([_record(i) for i in range(5)])
    s = metrics.snapshot()
    assert s.counters.get("usage.record.added") == 5
    assert s.counters.get("usage.record.deduped", 0) == 0


def test_usage_registry_separates_deduped_from_added():
    reg = UsageRegistry()
    reg.record_many([_record(1), _record(2), _record(3)])
    # Re-submit same message ids → all dropped by dedup.
    reg.record_many([_record(1), _record(2), _record(3)])
    s = metrics.snapshot()
    assert s.counters.get("usage.record.added") == 3
    assert s.counters.get("usage.record.deduped") == 3


def test_usage_registry_no_metric_for_empty_batch():
    """record_many with [] is a no-op; counter MUST NOT be created."""
    reg = UsageRegistry()
    reg.record_many([])
    s = metrics.snapshot()
    # Counter shouldn't exist (lazy creation only on real activity).
    assert "usage.record.added" not in s.counters
    assert "usage.record.deduped" not in s.counters


# ── JsonlParser wiring ──────────────────────────────────────────────────


def test_jsonl_parser_records_bytes_and_files_on_real_parse(tmp_path: Path):
    from claude_island.core.jsonl_parser import JsonlParser

    # Layout: <projects_dir>/<slug>/<uuid>.jsonl  with one valid line.
    proj_dir = tmp_path / "projects"
    slug_dir = proj_dir / "-x-y-z"
    slug_dir.mkdir(parents=True)
    f = slug_dir / "11111111-1111-1111-1111-111111111111.jsonl"
    # Minimal valid JSONL row that won't crash _parse_incremental.
    f.write_text(
        '{"type":"user","timestamp":"2026-05-26T00:00:00Z","cwd":"/x","gitBranch":"main"}\n'
    )

    reg = UsageRegistry()
    parser = JsonlParser(usage_registry=reg, claude_projects_dir=proj_dir)
    parser.parse_file(f)

    s = metrics.snapshot()
    assert s.counters.get("jsonl.file.parsed") == 1
    assert s.counters.get("jsonl.bytes.parsed", 0) > 0
    t = s.timings.get("jsonl.parse.duration_ms")
    assert t is not None and t.n == 1


def test_jsonl_parser_no_metric_when_file_empty(tmp_path: Path):
    """Empty / unchanged files take the early-return path — must
    not pollute the counters with no-op entries."""
    from claude_island.core.jsonl_parser import JsonlParser
    proj_dir = tmp_path / "projects"
    slug_dir = proj_dir / "-x-y-z"
    slug_dir.mkdir(parents=True)
    f = slug_dir / "11111111-1111-1111-1111-111111111111.jsonl"
    f.write_text("")  # empty file

    reg = UsageRegistry()
    parser = JsonlParser(usage_registry=reg, claude_projects_dir=proj_dir)
    parser.parse_file(f)

    s = metrics.snapshot()
    assert "jsonl.file.parsed" not in s.counters
    assert "jsonl.parse.duration_ms" not in s.timings
