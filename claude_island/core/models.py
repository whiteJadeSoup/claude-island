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
    is_sidechain: bool = False   # True when the row came from a subagent (sidechain) transcript


@dataclass(frozen=True)
class PricingTable:
    """Per-model pricing in USD per million tokens.

    ``cache_write_per_mtok`` / ``cache_read_per_mtok`` are optional.
    When None they fall back to Anthropic's standard ratios
    (write = 1.25 × input, read = 0.1 × input) — that ratio holds for
    every Anthropic model and also for MiniMax cache writes, so most
    entries can leave them unset. Override them when a provider
    publishes a different cache rate (e.g. MiniMax-M2.7's cache_read
    is $0.06/Mtok = 0.2 × input, not 0.1 ×).
    """
    input_per_mtok: float
    output_per_mtok: float
    cache_write_per_mtok: float | None = None
    cache_read_per_mtok: float | None = None

    def cw_rate(self) -> float:
        """Effective cache-write $/Mtok: explicit value or 1.25 × input."""
        if self.cache_write_per_mtok is not None:
            return self.cache_write_per_mtok
        return self.input_per_mtok * 1.25

    def cr_rate(self) -> float:
        """Effective cache-read $/Mtok: explicit value or 0.1 × input."""
        if self.cache_read_per_mtok is not None:
            return self.cache_read_per_mtok
        return self.input_per_mtok * 0.1


# Per-model pricing registry. Empty by default — provider modules
# (claude_island/platform_/providers/*.py) declare their own rates
# via :func:`register_pricing` at import time. This keeps the cost
# table declarative and provider-owned: adding a new provider means
# adding one file with its rates, no edit to core/.
#
# Lookup is length-descending substring match (see
# usage_registry._resolve_pricing): a model id like
# "MiniMax-M2.7-highspeed" matches the longest applicable key first,
# falling back to the family token (e.g. "sonnet") for unknown
# version suffixes.
PRICING: dict[str, PricingTable] = {}
DEFAULT_PRICING = PricingTable(input_per_mtok=3.0, output_per_mtok=15.0)


def register_pricing(table: dict[str, PricingTable]) -> None:
    """Merge per-model pricing entries into the global registry.

    Provider modules call this at import time to install their rates.
    Idempotent — re-registering an existing key overwrites it.
    """
    PRICING.update(table)


# Per-model display registry. Same shape as PRICING — provider modules
# self-register their entries via register_model_colors /
# register_model_short_names at import time. Keeps the model display
# table provider-owned: adding a new provider means adding one file
# with its rates AND its colour/name tokens, with no edit needed in
# core/ or ui/.
#
# Lookup uses length-descending substring match (see resolve_model_color
# / resolve_model_short_name) so a model id like "MiniMax-M2.7-highspeed"
# matches the longest applicable key first, falling back to family
# tokens for unknown version suffixes.
MODEL_COLORS: dict[str, str] = {}
MODEL_SHORT_NAMES: dict[str, str] = {}

# Neutral grey for any model not registered with a custom colour. Used
# both for unknown providers and as a sentinel; explicitly NOT a hash
# of the name so the same unknown model always renders identically
# across runs (avoids "why did it change colour?" confusion).
DEFAULT_MODEL_COLOR = "#6B7280"


def register_model_colors(table: dict[str, str]) -> None:
    """Merge per-model chip colours into the global registry.

    Provider modules call this at import time to install their tier
    palette. Idempotent — re-registering an existing key overwrites it.
    Caller passes ``{model_substring: hex_color}`` pairs; lookup is
    length-descending substring match (longest key wins)."""
    MODEL_COLORS.update(table)


def register_model_short_names(table: dict[str, str]) -> None:
    """Merge per-model short display names into the global registry.

    Used by the panel's row chip ("[Sonnet]") and the SPEND card's bar
    label. Caller passes ``{model_substring: short_name}`` pairs; same
    longest-substring-wins lookup as register_model_colors."""
    MODEL_SHORT_NAMES.update(table)


def resolve_model_color(model: str) -> str:
    """Map an API model id to its registered chip colour.

    Returns the longest matching key's value, or DEFAULT_MODEL_COLOR
    for unknown models. Case-insensitive substring match — same logic
    as usage_registry._resolve_pricing so behaviour stays consistent
    across the colour and pricing tables."""
    if not model:
        return DEFAULT_MODEL_COLOR
    lower = model.lower()
    for key in sorted(MODEL_COLORS.keys(), key=len, reverse=True):
        if key.lower() in lower:
            return MODEL_COLORS[key]
    return DEFAULT_MODEL_COLOR


def resolve_model_short_name(model: str) -> str:
    """Map an API model id to its registered short display name.

    Falls back to the first 12 characters of the raw id when the model
    has no registered short name — gives the user something recognisable
    rather than a blank chip. Empty model id ⇒ empty string (defensive;
    no model means no chip at all)."""
    if not model:
        return ""
    lower = model.lower()
    for key in sorted(MODEL_SHORT_NAMES.keys(), key=len, reverse=True):
        if key.lower() in lower:
            return MODEL_SHORT_NAMES[key]
    return model[:12]


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
    # v4c (2026-05): request count for the period.  One UsageRecord =
    # one Claude API request (one assistant message), so this is the
    # length of the records list that ``UsageRegistry.get_totals``
    # aggregated.  Surfaced in the TODAY card stats strip as
    # "N reqs" — a key headline number alongside tokens / cache.
    request_count: int = 0
    # Sidechain (subagent) subset of the headline numbers above.
    # ``request_count`` and ``cost_usd`` are STILL "all records" — they
    # are what Anthropic actually bills the user (main + subagent),
    # which is the headline the TODAY card surfaces. These two fields
    # carry the subagent-only slice of that headline so the panel can
    # render a "↳ incl. {N} subagent reqs · ${C}" annotation under
    # the main stat strip without the consumer having to call
    # get_totals twice or aggregate records a second time.
    # Reconciles the difference between island's headline ($ incl.
    # subagents) and ccusage-style tools ($ main only), which is the
    # #1 "why don't our numbers match?" question for this card.
    sidechain_request_count: int = 0
    sidechain_cost_usd: float = 0.0

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
    """One snapshot of a provider's quota API (Anthropic or MiniMax).

    Mirrors what Claude Code's /status command shows: the user's
    5-hour-session and 7-day usage percentages plus when each window
    resets. Reverse-engineered — these are private APIs and may break
    or be blocked at any time, hence the ``is_stale`` flag for callers
    that want to display a degraded indicator instead of going dark.
    """
    five_hour_pct: float            # 0..100
    five_hour_resets_at: datetime
    seven_day_pct: float
    seven_day_resets_at: datetime
    fetched_at: datetime
    is_stale: bool                  # True when fetched_at is older than 3*TTL
    provider: str = "anthropic"    # "anthropic" | "minimax"
    # Number of consecutive failed fetches at the time this snapshot was
    # produced. 0 on the happy path (last fetch succeeded). The UI uses
    # this together with ``is_auto_refresh_paused`` to render a
    # "auto-paused, N consecutive failures" hint on the quota card —
    # manual ⟳ still works in that state and is the recovery affordance.
    consecutive_failures: int = 0
    # True when the producer's circuit-breaker has tripped — auto-refresh
    # has stopped issuing HTTP for this provider. Manual ⟳ still works
    # and resets ``consecutive_failures`` on success. The UI uses this
    # bool directly so it doesn't need to know the producer's threshold.
    is_auto_refresh_paused: bool = False


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
    sidechain_count: int          # # subagent API requests (1 per sidechain assistant message, NOT # of dispatches)
    # Per-model breakdown for the detail popup's TOKENS section.
    # Empty tuple when the composer / registry isn't wired yet — the
    # popup degrades gracefully (renders just the cumulative cost row).
    per_model: tuple[ModelTotals, ...] = ()
    # The model id from the most recent UsageRecord for this session.
    # None until the JSONL parser has indexed at least one real turn.
    # Used by the row chip — when a session switches models mid-life
    # (e.g. started on Opus, switched to DeepSeek), the chip should
    # show the current model, not the highest-cumulative-cost one.
    latest_model: str | None = None
    # The session_uuid actually used to look up records and metadata
    # (composer resolves it from sessions/<pid>.json's ``sessionId``;
    # ``Session.session_uuid`` is often "" coming out of ProcessScanner).
    # The detail popup shows this in the ID row.
    effective_uuid: str | None = None
    # The Claude-Code-assigned session name, BEFORE any user override.
    # The detail popup compares this against ``name`` to detect a
    # rename, and surfaces this as the subtitle when the user renamed
    # but no AI-generated title exists. None when sessions/<pid>.json
    # had no name field (e.g. MiniMax sessions).
    original_name: str | None = None


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


@dataclass(frozen=True)
class DormantSession:
    """An offline (no live process) Claude Code session, recovered from
    the JSONL transcript on disk.

    Built by :class:`DormantSessionSource` from
    :meth:`JsonlParser.get_session_metadata` plus
    :meth:`UsageRegistry.get_session_summary`. Filtered out from a
    snapshot's ``dormant_sessions`` if the same uuid appears as a live
    or launching session — see ``Snapshotter._build_snapshot`` reconcile.

    The RecentsDrawer UI renders these one row per session, and the
    Resume button triggers a TerminalAdapter LAUNCH that re-spawns
    ``claude --resume <session_uuid>`` in this ``cwd`` carrying the
    permission-mode-derived flags.
    """
    session_uuid: str
    cwd: Path
    name: str | None             # ai_title or None — UI falls back to last_prompt[:30]
    last_prompt: str | None      # raw last user message, used as title fallback
    last_activity: datetime      # tz-aware UTC; sort key in the History drawer
    started_at: datetime | None  # earliest timestamp from the transcript
    permission_mode: str | None  # 'default' / 'acceptEdits' / 'plan' / 'bypassPermissions'
    git_branch: str | None
    cost_usd: float
    turn_count: int
