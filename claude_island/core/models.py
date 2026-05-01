from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path


def project_hash(cwd: Path | str) -> str:
    """Convert a project working directory to Claude Code's project-id format.

    Claude Code stores per-project session files under
    ``~/.claude/projects/<hash>/<session_uuid>.jsonl``. The hash is the cwd
    string with every non-[a-zA-Z0-9._] character replaced by '-'.

    Examples:
        D:\\coding projects\\common-learn  →  D--coding-projects-common-learn
        /home/user/my.project              →  -home-user-my.project
    """
    return re.sub(r"[^a-zA-Z0-9._]", "-", str(cwd))


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
    "opus":   PricingTable(input_per_mtok=15.0, output_per_mtok=75.0),
}
DEFAULT_PRICING = PricingTable(input_per_mtok=3.0, output_per_mtok=15.0)


@dataclass
class UsageTotals:
    period: str  # "daily" | "weekly" | "monthly"
    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_tokens: int = 0
    cache_read_tokens: int = 0
    input_cost: float = 0.0
    output_cost: float = 0.0
    cache_creation_cost: float = 0.0
    cache_read_cost: float = 0.0

    @property
    def cost_usd(self) -> float:
        return (self.input_cost + self.output_cost
                + self.cache_creation_cost + self.cache_read_cost)
