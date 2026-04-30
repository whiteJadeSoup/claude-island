from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path


@dataclass(frozen=True)
class Session:
    pid: int
    project_path: Path
    session_uuid: str
    window_handle: int | None  # None on macOS or if not yet resolved
    last_activity: datetime


@dataclass(frozen=True)
class PricingTable:
    """Per-model pricing in USD per million tokens."""
    input_per_mtok: float
    output_per_mtok: float


# Prices as of mid-2025; cache write is ×1.25 input, cache read is ×0.1 input.
PRICING: dict[str, PricingTable] = {
    "haiku":  PricingTable(input_per_mtok=1.0,  output_per_mtok=5.0),
    "sonnet": PricingTable(input_per_mtok=3.0,  output_per_mtok=15.0),
    "opus":   PricingTable(input_per_mtok=5.0,  output_per_mtok=25.0),
}
DEFAULT_PRICING = PricingTable(input_per_mtok=3.0, output_per_mtok=15.0)


@dataclass
class UsageTotals:
    period: str  # "daily" | "weekly" | "monthly"
    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_tokens: int = 0
    cache_read_tokens: int = 0
    cost_usd: float = 0.0
