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

from claude_island.core.models import (
    PricingTable,
    register_model_colors,
    register_model_short_names,
    register_pricing,
)


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

# Display registry — orange/amber family for DeepSeek so it visually
# stands apart from Anthropic's cool palette. Pro = deep orange (the
# pricier, slower tier); Flash = light orange (the cheaper, faster
# tier). Same intra-provider tier convention as Anthropic (deeper hue
# = more capable / more expensive).
register_model_colors({
    "deepseek-v4-pro":   "#EA580C",  # deep orange
    "deepseek-v4-flash": "#FB923C",  # light orange
    # Catch-all for unrecognised DeepSeek model variants — share the
    # Pro tone so DeepSeek family always reads orange, even if a new
    # variant name is missing from the table for a release cycle.
    "deepseek":          "#EA580C",
})
register_model_short_names({
    # Prefix "DeepSeek" so the chip reads as self-explanatory. Without
    # it, "V4 Pro" on its own is ambiguous (Google Gemini uses "Pro",
    # AMD/Nvidia have "V4" product lines, etc). Matches the MiniMax fix.
    "deepseek-v4-pro":   "DeepSeek V4 Pro",
    "deepseek-v4-flash": "DeepSeek V4 Flash",
    "deepseek":          "DeepSeek",
})
