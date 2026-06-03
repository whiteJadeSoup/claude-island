"""Tests for the live LiteLLM pricing source.

Covers the pure conversion (parse_litellm) and the load/cache/fallback
ladder (load_and_register) with an injected fetch — no network. The
hardcoded provider baseline is already registered via conftest's
providers import, so these tests only add extra (uniquely-named) entries
and assert they resolve through ``models.lookup_pricing``.
"""
from __future__ import annotations

import json

import pytest

from claude_island.core.models import PricingTable, lookup_pricing
from claude_island.platform_ import pricing_source as ps


# ── parse_litellm: per-token → per-Mtok, optional cache, skips junk ──────────

def test_parse_litellm_converts_per_token_to_per_mtok():
    out = ps.parse_litellm({
        "claude-fake-xx": {
            "input_cost_per_token": 3e-6,
            "output_cost_per_token": 15e-6,
            "cache_creation_input_token_cost": 3.75e-6,
            "cache_read_input_token_cost": 0.3e-6,
        },
    })
    t = out["claude-fake-xx"]
    assert t.input_per_mtok == pytest.approx(3.0)
    assert t.output_per_mtok == pytest.approx(15.0)
    assert t.cache_write_per_mtok == pytest.approx(3.75)
    assert t.cache_read_per_mtok == pytest.approx(0.3)


def test_parse_litellm_leaves_cache_none_when_absent():
    out = ps.parse_litellm({
        "m": {"input_cost_per_token": 1e-6, "output_cost_per_token": 2e-6},
    })
    t = out["m"]
    assert t.input_per_mtok == pytest.approx(1.0)
    assert t.cache_write_per_mtok is None and t.cache_read_per_mtok is None
    # falls back to island defaults (write = 1.25× input, read = 0.1× input)
    assert t.cw_rate() == pytest.approx(1.0 * 1.25)
    assert t.cr_rate() == pytest.approx(1.0 * 0.1)


def test_parse_litellm_skips_sample_spec_and_costless_rows():
    out = ps.parse_litellm({
        "sample_spec": {"input_cost_per_token": 1e-6, "output_cost_per_token": 2e-6},
        "embeddings-x": {"input_cost_per_token": 1e-6},   # no output cost
        "not-a-dict": 123,
        "good": {"input_cost_per_token": 1e-6, "output_cost_per_token": 2e-6},
    })
    assert set(out) == {"good"}


def test_parse_litellm_rejects_bool_costs():
    out = ps.parse_litellm({"b": {"input_cost_per_token": True, "output_cost_per_token": 2e-6}})
    assert out == {}


def test_parse_litellm_handles_none_and_empty():
    # `(data or {})` guard — a None body (fetch returned nothing) or an empty
    # dict must yield {} rather than raise, so callers can pass it through.
    assert ps.parse_litellm(None) == {}
    assert ps.parse_litellm({}) == {}


# ── load_and_register: live / cache-fresh / cache-stale / none ───────────────

def _fake_litellm(name: str) -> dict:
    return {name: {"input_cost_per_token": 0.95e-6, "output_cost_per_token": 4e-6,
                   "cache_read_input_token_cost": 0.16e-6}}


def test_load_live_registers_and_writes_cache(tmp_path):
    name = "moonshot/kimi-zz-live"
    src = ps.load_and_register(cache_dir=tmp_path, now_epoch=1000.0,
                               fetch=lambda: _fake_litellm(name))
    assert src == "live"
    # prefix-stripped, dotted-normalised id resolves to the live entry
    t = lookup_pricing("kimi-zz-live")
    assert t is not None and t.input_per_mtok == pytest.approx(0.95)
    # cache file written with the model
    cached = json.loads((tmp_path / ps.CACHE_FILE).read_text())
    assert name in cached["models"] and cached["fetched_at"] == 1000.0


def test_load_uses_fresh_cache_without_fetching(tmp_path):
    name = "fresh-cache-model-zz"
    (tmp_path / ps.CACHE_FILE).write_text(json.dumps({
        "fetched_at": 5000.0,
        "models": {name: [7.0, 8.0, None, None]},
    }))

    def _boom():
        raise AssertionError("fetch must not be called when cache is fresh")

    src = ps.load_and_register(cache_dir=tmp_path, now_epoch=5000.0 + 60, fetch=_boom)
    assert src == "cache-fresh"
    assert lookup_pricing(name).input_per_mtok == pytest.approx(7.0)


def test_load_falls_back_to_stale_cache_when_fetch_fails(tmp_path):
    name = "stale-cache-model-zz"
    (tmp_path / ps.CACHE_FILE).write_text(json.dumps({
        "fetched_at": 1.0,  # ancient → expired
        "models": {name: [11.0, 22.0, None, None]},
    }))
    src = ps.load_and_register(cache_dir=tmp_path,
                               now_epoch=1.0 + ps.CACHE_TTL_S + 1,
                               fetch=lambda: None)
    assert src == "cache-stale"
    assert lookup_pricing(name).input_per_mtok == pytest.approx(11.0)


def test_load_returns_none_when_no_cache_and_fetch_fails(tmp_path):
    src = ps.load_and_register(cache_dir=tmp_path, now_epoch=1.0, fetch=lambda: None)
    assert src == "none"
