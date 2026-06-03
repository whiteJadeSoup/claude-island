from __future__ import annotations

import re
import threading
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

# Concurrency: the hardcoded provider tables register at import (one
# thread), but the live LiteLLM fetch (platform_/pricing_source.py)
# registers from a background thread while the UI thread is resolving
# prices. Guard mutation + snapshot reads with one lock, and bump
# PRICING_EPOCH on every change so resolvers can invalidate their memo.
_PRICING_LOCK = threading.RLock()
PRICING_EPOCH: int = 0

# Normalised-key index for O(1) exact lookups. Key = _norm_key(model id):
# provider prefix stripped, lowercased, '.'/'@' → '-'. Mirrors ccusage's
# key normalisation so a dirty id ("moonshot/kimi-k2.6", "aws.claude-…")
# resolves to the same entry as its canonical form.
_PRICING_BY_NORM: dict[str, PricingTable] = {}


def _norm_key(name: str) -> str:
    """Normalise a model id / pricing key for matching: drop any provider
    prefix ("moonshot/…"), lowercase, and fold '.'/'@' to '-' so
    "kimi-k2.6" == "moonshot/kimi-k2.6" and "aws.claude-haiku-4.5"
    contains "claude-haiku-4-5". Matches LiteLLM / ccusage key shapes."""
    return name.rsplit("/", 1)[-1].lower().replace(".", "-").replace("@", "-")


def register_pricing(table: dict[str, PricingTable]) -> None:
    """Merge per-model pricing entries into the global registry.

    Provider modules call this at import time to install their hardcoded
    rates; the live LiteLLM fetch calls it at startup to override / extend
    them (last write wins, like ccusage's builtin-then-live layering).
    Idempotent. Thread-safe, and bumps :data:`PRICING_EPOCH` so memoising
    resolvers drop stale entries.
    """
    global PRICING_EPOCH
    with _PRICING_LOCK:
        PRICING.update(table)
        for key, pricing in table.items():
            _PRICING_BY_NORM[_norm_key(key)] = pricing
        PRICING_EPOCH += 1


def lookup_pricing(model: str) -> PricingTable | None:
    """Resolve a raw API model id to its PricingTable, or ``None`` when no
    entry matches (the caller decides the fallback).

    Two-stage match, mirroring ccusage:
    1. Exact on the normalised id — precise, O(1); catches canonical ids
       ("claude-opus-4-7") and prefix/dotted variants ("moonshot/kimi-k2.6",
       "MiniMax-M2.7-highspeed").
    2. Longest normalised key that is a substring of the normalised id —
       handles dirty ids with no exact row ("aws.claude-haiku-4.5-nova15"
       → "claude-haiku-4-5"). Longest-first so the most specific key wins
       (the "MiniMax-M2.7 not MiniMax-M2" rule).

    The snapshot is taken under the registry lock so a concurrent
    live-pricing registration can't corrupt the iteration.
    """
    nm = _norm_key(model)
    with _PRICING_LOCK:
        exact = _PRICING_BY_NORM.get(nm)
        if exact is not None:
            return exact
        # Snapshot the pre-normalised index so the substring scan ranks by
        # NORMALISED key length (true specificity) and skips re-normalising
        # per key. Sorting by RAW key length would be wrong with the live
        # LiteLLM table: a provider-prefixed key with a long path but short
        # model name ("openrouter/anthropic/claude" → "claude") could
        # outrank a more specific shorter-raw key ("gpt-4o" → "gpt-4o").
        norm_items = list(_PRICING_BY_NORM.items())
    for norm_key, pricing in sorted(norm_items, key=lambda kv: -len(kv[0])):
        if norm_key in nm:
            return pricing
    return None


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


# Canonical Anthropic model id shape: claude-<family>-<major>[-<minor>]
# optionally followed by a -<datestamp> suffix. Matching this lets the
# resolver auto-format a NOT-yet-registered version — e.g. a fresh
# ``claude-opus-4-8`` release becomes ``opus-4.8`` — instead of silently
# degrading to the bare family name "opus" because the explicit table in
# providers/anthropic.py hasn't caught up yet. This is the future-proof
# net behind the explicit per-version entries; first-party ``claude-``
# ids are unambiguous enough to special-case here in core.
#
# The minor group is optional and bounded to 1-2 digits NOT followed by
# another digit. This is what distinguishes a real minor version from a
# trailing datestamp: ``claude-sonnet-4-5-20250929`` → minor "5" (4.5),
# but ``claude-sonnet-4-20250514`` → no minor (4), because the 8-digit
# "20250514" can't be a 1-2 digit minor and is read as the datestamp.
_ANTHROPIC_ID_RE = re.compile(r"claude-(opus|sonnet|haiku)-(\d+)(?:-(\d{1,2})(?!\d))?")
_ANTHROPIC_FAMILIES = ("opus", "sonnet", "haiku")


def resolve_model_short_name(model: str) -> str:
    """Map an API model id to its registered short display name.

    Resolution order:
      1. Explicit registry, longest key first (per-version Anthropic
         entries, other providers' custom names).
      2. Canonical Anthropic id ``claude-<family>-<maj>-<min>`` →
         ``<family>-<maj>.<min>`` (auto-formats unregistered versions).
      3. Anthropic family token alone (version-less ids) → the family.
      4. First 12 chars of the raw id — something recognisable rather
         than a blank chip.

    Empty model id ⇒ empty string (defensive; no model means no chip)."""
    if not model:
        return ""
    lower = model.lower()
    for key in sorted(MODEL_SHORT_NAMES.keys(), key=len, reverse=True):
        if key.lower() in lower:
            return MODEL_SHORT_NAMES[key]
    m = _ANTHROPIC_ID_RE.search(lower)
    if m:
        family, major, minor = m.group(1), m.group(2), m.group(3)
        return f"{family}-{major}.{minor}" if minor else f"{family}-{major}"
    for family in _ANTHROPIC_FAMILIES:
        if family in lower:
            return family
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
