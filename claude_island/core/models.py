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
class UsageRecord:
    """One assistant turn parsed out of a Claude Code transcript.

    The JSONL files at ``~/.claude/projects/<hash>/<session>.jsonl`` are
    the single source of truth — there is no on-disk derived store; the
    UsageRegistry holds these in memory for the life of the process and
    rebuilds from JSONL on every start.

    ``message_id`` is the Anthropic API ``message.id`` (e.g. ``msg_014…``).
    Claude Code splits one API response across multiple JSONL lines (one
    per content block: text + each tool_use), and **every one of those
    lines repeats the same response's usage block**. Without dedup we
    multiply-count: a 5-block response is billed 5×. The registry
    discards records whose message_id it has already seen so the API
    cost ends up exactly equal to ``unique_responses × per_response_cost``.
    A None ``message_id`` (older transcript schemas without it) bypasses
    the dedup — better to risk the rare double-count than drop a real row.
    """
    timestamp: datetime
    project_path: str   # Claude Code's hashed project id (parent dir name)
    session_uuid: str   # transcript filename stem
    model: str          # raw API model id, e.g. "claude-sonnet-4-6"
    input_tokens: int
    output_tokens: int
    cache_creation_tokens: int
    cache_read_tokens: int
    message_id: str | None = None
    is_sidechain: bool = False   # True when the JSONL row was a subagent invocation


@dataclass(frozen=True)
class PricingTable:
    """Per-model pricing in USD per million tokens."""
    input_per_mtok: float
    output_per_mtok: float


# Per-Mtok input/output rates from Anthropic's official API pricing
# table (https://platform.claude.com/docs/en/about-claude/pricing,
# fetched 2026-05-01). Cache write is ×1.25 input (5-min ephemeral —
# the SDK default), cache read is ×0.1 input.
#
# Heads-up: Opus dropped from $15/$75 (3.x and 4.0/4.1) to $5/$25
# starting with 4.5 and held through 4.6 and 4.7 — if a user is on
# legacy 4.0/4.1 the rate substring match still routes to "opus" but
# the cost is silently 3× under-reported for them. Acceptable trade-off:
# Anthropic's recent versions converge on the new rate, and the substring
# match keeps working as new opus-N versions ship. Revisit if Anthropic
# fragments the 4.x family.
PRICING: dict[str, PricingTable] = {
    "haiku":  PricingTable(input_per_mtok=1.0, output_per_mtok=5.0),
    "sonnet": PricingTable(input_per_mtok=3.0, output_per_mtok=15.0),
    "opus":   PricingTable(input_per_mtok=5.0, output_per_mtok=25.0),
}
DEFAULT_PRICING = PricingTable(input_per_mtok=3.0, output_per_mtok=15.0)


@dataclass
class UsageTotals:
    period: str  # "today" | "daily" | "weekly" | "monthly"
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


@dataclass(frozen=True)
class ModelTotals:
    """Per-model aggregation inside a single time window.

    The cost is recomputed from tokens × pricing on read (same as
    UsageTotals) so price-table updates retroactively apply.
    """
    model: str   # canonical key from PRICING ("haiku" | "sonnet" | "opus") or raw model id
    input_tokens: int
    output_tokens: int
    cache_creation_tokens: int
    cache_read_tokens: int
    cost_usd: float


@dataclass(frozen=True)
class QuotaSnapshot:
    """One snapshot of Anthropic's private /api/oauth/usage endpoint.

    Mirrors what Claude Code's /status command shows: the user's
    consumer-plan 5-hour-session and 7-day usage percentages plus when
    each window resets. Reverse-engineered — Anthropic does not
    advertise this as a public API and the call may break or be
    blocked at any time, hence the ``is_stale`` flag for callers that
    want to display a degraded indicator instead of going dark.
    """
    five_hour_pct: float            # 0..100
    five_hour_resets_at: datetime
    seven_day_pct: float
    seven_day_resets_at: datetime
    fetched_at: datetime
    is_stale: bool                  # True when fetched_at is older than 3*TTL


@dataclass(frozen=True)
class SessionDetails:
    """Rich per-session metadata used to render the hover tooltip.

    All fields are optional ``None`` so the UI can render a partial
    tooltip when one source (e.g. the JSONL hasn't been fully parsed
    yet, or ~/.claude/sessions/<pid>.json is missing) hasn't yielded
    anything. Composed by the wiring layer (__main__.py) — core has
    no business reading platform-specific files.
    """
    session: Session
    name: str | None              # human slug from sessions/<pid>.json (e.g. "cc-learning")
    ai_title: str | None          # Claude-generated session title from JSONL ai-title row
    git_branch: str | None        # gitBranch field; same on every JSONL row of a session
    last_prompt: str | None       # text of the latest user message (for preview)
    started_at: datetime | None   # session start (from sessions/<pid>.json startedAt)
    status: str | None            # "idle"/"busy"/"waiting" — Claude Code's own state
    cc_version: str | None        # Claude Code version (e.g. "2.1.123")
    cost_usd: float               # cumulative cost across all turns of this session
    turn_count: int               # # assistant turns
    sidechain_count: int          # # subagent invocations
    # Per-model breakdown for the detail popup's TOKENS section.
    # Empty tuple when the composer / registry isn't wired yet — the
    # popup degrades gracefully (renders just the cumulative cost row).
    per_model: tuple[ModelTotals, ...] = ()
    # The session_uuid actually used to look up records and metadata
    # (composer resolves it from sessions/<pid>.json's ``sessionId``;
    # ``Session.session_uuid`` is often "" coming out of ProcessScanner).
    # The detail popup shows this in the ID row.
    effective_uuid: str | None = None


@dataclass(frozen=True)
class SessionUsage:
    """A snapshot of the current 5-hour session window.

    ``start_time`` / ``end_time`` are the locally-derived block
    boundaries — earliest request preceded by ≥5h of idle, plus 5h.
    Both are None when the database has never seen any usage.

    ``quota`` carries the server-authoritative percentage when
    available; None means we couldn't reach the endpoint (disabled,
    no credentials, network error, no cache yet) and the UI should
    just hide the progress bar.
    """
    start_time: datetime | None
    end_time: datetime | None
    by_model: tuple[ModelTotals, ...]
    total_cost_usd: float
    quota: QuotaSnapshot | None
