"""Smoke test: verify the test harness runs and core layer imports."""


def test_import_core() -> None:
    from claude_island.core import models, session_registry, snapshot, usage_registry, jsonl_parser
    assert models.Session is not None
    assert session_registry.SessionRegistry is not None
    assert usage_registry.UsageRegistry is not None
    assert jsonl_parser.JsonlParser is not None
    assert snapshot.WorldSnapshot is not None
