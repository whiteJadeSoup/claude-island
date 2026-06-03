"""Tests for the normalised pricing matcher (models.lookup_pricing) and the
memoised resolver (usage_registry._resolve_pricing).

The hardcoded provider baseline is registered via conftest's providers
import; these tests register a few extra uniquely-named entries to exercise
the exact / substring / prefix-stripping / unknown paths and the epoch-based
memo invalidation. Global registry pollution is harmless (unique names).
"""
from __future__ import annotations

import pytest

from claude_island.core.models import (
    PricingTable,
    lookup_pricing,
    register_pricing,
)


def test_exact_normalised_match_on_canonical_id():
    register_pricing({"claude-zztest-4-9": PricingTable(5.0, 25.0)})
    t = lookup_pricing("claude-zztest-4-9")
    assert t is not None and t.input_per_mtok == 5.0


def test_prefix_and_dot_normalisation_for_kimi_style_id():
    # LiteLLM keys non-anthropic models with a provider prefix + dots.
    register_pricing({"moonshot/kimi-zztest.9": PricingTable(0.95, 4.0)})
    # The raw API id in transcripts is the bare, dotted form.
    t = lookup_pricing("kimi-zztest.9")
    assert t is not None and t.input_per_mtok == pytest.approx(0.95)


def test_longest_substring_match_for_dirty_id():
    # No exact row for the dirty bedrock-style id; the longest registered
    # key that is a normalised substring wins.
    register_pricing({
        "claude-zzhaiku-9-9": PricingTable(1.0, 5.0),
        "claude-zzhaiku": PricingTable(99.0, 99.0),   # shorter, must lose
    })
    t = lookup_pricing("aws.claude-zzhaiku-9.9-nova15")
    assert t is not None and t.input_per_mtok == 1.0  # the longer, specific key


def test_unknown_model_returns_none():
    assert lookup_pricing("totally-unknown-model-zzz-0000") is None


def test_resolve_pricing_falls_back_to_default_for_unknown():
    from claude_island.core.usage_registry import _resolve_pricing
    from claude_island.core.models import DEFAULT_PRICING
    assert _resolve_pricing("totally-unknown-model-zzz-1111") is DEFAULT_PRICING


def test_resolve_pricing_memo_invalidates_on_reregister():
    """_resolve_pricing memoises, but a later register_pricing (e.g. the live
    LiteLLM fetch) bumps PRICING_EPOCH and must override the cached value."""
    from claude_island.core.usage_registry import _resolve_pricing
    register_pricing({"zz-memo-model": PricingTable(1.0, 2.0)})
    assert _resolve_pricing("zz-memo-model").input_per_mtok == 1.0
    register_pricing({"zz-memo-model": PricingTable(9.0, 9.0)})  # live override
    assert _resolve_pricing("zz-memo-model").input_per_mtok == 9.0
