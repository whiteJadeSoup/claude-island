"""Live model-pricing source (LiteLLM) — mirrors ccusage's primary feed.

ccusage prices tokens from BerriAI/litellm's ``model_prices_and_context_
window.json`` (with a models.dev fallback). This module fetches the same
LiteLLM file, converts its per-TOKEN rates to island's per-MILLION-token
``PricingTable``, and injects them into the core ``PRICING`` registry via
``register_pricing`` — so non-Anthropic / newly-released models get real
prices instead of the Sonnet-rate fallback.

Layering: this is a platform_ concern (network + disk IO). core stays
pure — it only owns the registry and the resolver. The hardcoded provider
tables (anthropic / minimax / deepseek) remain registered at import as the
OFFLINE BASELINE; live entries override / extend them, so a fetch failure
degrades to those rather than to nothing.

Why no models.dev fallback (ccusage has one): it timed out from the
target network during testing, whereas the LiteLLM raw URL responds in
~1s. The hardcoded baseline is the more reliable local fallback here.

Out of scope (island stays simpler than ccusage): >200k tiered rates and
the fast/priority multiplier. Current Claude models don't set either and
the observed traffic is all standard-speed, so flat per-token rates match
ccusage's output for this data (verified). Revisit if those appear.
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path

from claude_island.core.models import PricingTable, register_pricing
from claude_island.core.safe_stderr import safe_stderr_write

# Same source ccusage fetches (its CostMode pricing feed).
LITELLM_URL = (
    "https://raw.githubusercontent.com/BerriAI/litellm/main/"
    "model_prices_and_context_window.json"
)
FETCH_TIMEOUT = 8.0          # seconds; the file is ~1.5 MB / ~1s on a good link
CACHE_FILE = "pricing_cache.json"
CACHE_TTL_S = 24 * 3600      # prices change rarely; one fetch per day is plenty


def _num(v: object) -> float | None:
    """Return v as a float only if it's a real number (reject bool/str)."""
    if isinstance(v, bool):
        return None
    return float(v) if isinstance(v, (int, float)) else None


def parse_litellm(data: dict) -> dict[str, PricingTable]:
    """Convert LiteLLM's per-token rates to island per-Mtok PricingTables.

    Skips entries without both input & output costs (the file mixes in
    non-chat rows and a ``sample_spec`` template). cache_* costs are
    optional — when absent, island's defaults (write = 1.25×input,
    read = 0.1×input) apply, matching ccusage's own defaults.
    """
    out: dict[str, PricingTable] = {}
    for name, entry in (data or {}).items():
        if name == "sample_spec" or not isinstance(entry, dict):
            continue
        inp = _num(entry.get("input_cost_per_token"))
        outp = _num(entry.get("output_cost_per_token"))
        if inp is None or outp is None:
            continue
        cw = _num(entry.get("cache_creation_input_token_cost"))
        cr = _num(entry.get("cache_read_input_token_cost"))
        # per-token → per-million-token; cache fields stay None when absent
        out[name] = PricingTable(
            input_per_mtok=inp * 1_000_000,
            output_per_mtok=outp * 1_000_000,
            cache_write_per_mtok=(cw * 1_000_000 if cw is not None else None),
            cache_read_per_mtok=(cr * 1_000_000 if cr is not None else None),
        )
    return out


def _fetch(url: str = LITELLM_URL, timeout: float = FETCH_TIMEOUT) -> dict | None:
    """GET + JSON-parse the LiteLLM file. Returns None on any failure
    (network, non-200, bad body) — the caller falls back to cache/baseline."""
    req = urllib.request.Request(url, headers={"User-Agent": "claude-island"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status = getattr(resp, "status", 200)
            if status != 200:
                safe_stderr_write(f"[claude-island] pricing fetch: HTTP {status}")
                return None
            return json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError) as e:
        safe_stderr_write(f"[claude-island] pricing fetch failed: {type(e).__name__}")
        return None


# ── disk cache: {fetched_at: epoch, models: {name: [in,out,cw,cr]}} ──────────

def _read_cache(path: Path) -> tuple[dict[str, PricingTable] | None, float | None]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None, None
    if not isinstance(data, dict) or not isinstance(data.get("models"), dict):
        return None, None
    tables: dict[str, PricingTable] = {}
    for name, row in data["models"].items():
        if isinstance(row, list) and len(row) == 4:
            tables[name] = PricingTable(
                input_per_mtok=row[0], output_per_mtok=row[1],
                cache_write_per_mtok=row[2], cache_read_per_mtok=row[3],
            )
    fetched = data.get("fetched_at")
    return (tables or None), (float(fetched) if isinstance(fetched, (int, float)) else None)


def _write_cache(path: Path, tables: dict[str, PricingTable], now_epoch: float) -> None:
    payload = {
        "fetched_at": now_epoch,
        "models": {
            name: [t.input_per_mtok, t.output_per_mtok,
                   t.cache_write_per_mtok, t.cache_read_per_mtok]
            for name, t in tables.items()
        },
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload), encoding="utf-8")
    os.replace(tmp, path)


def load_and_register(*, cache_dir, now_epoch: float, fetch=None) -> str:
    """Populate the pricing registry from the live source, with fallback.

    Order: fresh disk cache → live fetch (then cache it) → stale disk
    cache → nothing (the import-time hardcoded baseline stays in effect).
    Returns the source used ("cache-fresh" | "live" | "cache-stale" |
    "none") for logging / tests. ``fetch`` is injectable for tests.
    """
    fetch = fetch or _fetch
    path = Path(cache_dir) / CACHE_FILE
    cached, fetched_at = _read_cache(path)
    if cached and fetched_at is not None and (now_epoch - fetched_at) < CACHE_TTL_S:
        register_pricing(cached)
        return "cache-fresh"
    raw = fetch()
    if raw is not None:
        tables = parse_litellm(raw)
        if tables:
            register_pricing(tables)
            try:
                _write_cache(path, tables, now_epoch)
            except OSError as e:
                safe_stderr_write(f"[claude-island] pricing cache write failed: {e}")
            return "live"
    if cached:
        register_pricing(cached)
        return "cache-stale"
    return "none"


def install(cache_dir) -> None:
    """Background entry point: load live pricing, never raise. Designed to
    run on a daemon thread at startup so a slow/blocked fetch can't stall
    the UI; costs are recomputed on read, so a late registration applies to
    the next query automatically."""
    try:
        source = load_and_register(cache_dir=cache_dir, now_epoch=time.time())
        safe_stderr_write(f"[claude-island] model pricing source: {source}")
    except Exception as e:  # never let the daemon thread crash the app
        safe_stderr_write(
            f"[claude-island] pricing install failed: {type(e).__name__}: {e}"
        )
