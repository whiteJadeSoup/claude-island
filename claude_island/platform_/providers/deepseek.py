"""DeepSeek pricing.

DeepSeek V4 is accessed via the Anthropic-format proxy at
``https://api.deepseek.com/anthropic``; the model names that surface
in Claude Code's JSONL transcripts are ``deepseek-v4-pro`` and
``deepseek-v4-flash``.

No quota provider class here yet — DeepSeek doesn't expose a public
quota / remaining-budget endpoint comparable to Anthropic's
``/api/oauth/usage``. This module's only job is to register the per-
Mtok rates so cost rollups in the SPEND card price DeepSeek tokens
correctly.

Rates are taken verbatim from the official Model Details page
(2026-05 snapshot). Prices below assume the 75% promo discount
already applied — the source page lists ``$1.74`` original + the
promo as ``$0.435 (75% off)``; we record the discounted rate that
the user actually pays today. Update when the promo ends.
"""
from __future__ import annotations

from claude_island.core.models import PricingTable, register_pricing


# Per-Mtok rates from https://platform.deepseek.com/api-docs/pricing
# (Model Details, 2026-05). Cache write isn't separately listed for
# DeepSeek; defaults (1.25 × input) apply via PricingTable.cw_rate().
register_pricing({
    "deepseek-v4-pro": PricingTable(
        input_per_mtok=0.435,
        output_per_mtok=0.87,
        cache_read_per_mtok=0.003625,
    ),
    "deepseek-v4-flash": PricingTable(
        input_per_mtok=0.14,
        output_per_mtok=0.28,
        cache_read_per_mtok=0.0028,
    ),
})
