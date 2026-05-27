# Quota Fetch — "Next Retry" Hint in Failure Log

## Problem

When the Anthropic quota endpoint returns errors (typically HTTP 429), the
stderr log line tells the user *what* failed and *when it last
attempted/succeeded*, but never *when the auto-refresher will try again*.
The user has to know the internal exponential schedule
(5/10/20/40/80 min, capped at 5h, paused after 5 failures) to interpret
how long the silence will last.

Current line:

```
[2026-05-27 17:26:04] [claude-island] anthropic quota fetch: HTTP 429 Too Many Requests — last attempt 20m ago — last success 37m ago
```

After three consecutive 429s, this line gives the same shape — there's no
signal that the next attempt is now ~40 minutes away rather than the
default 5.

## Goal

Append a one-clause "next retry" hint to the auto-fetch failure log so
the user can read the next-attempt cadence directly off the line, without
mental math against the backoff schedule.

## Non-Goals

- Manual ⟳ failure log line — out of scope. Manual failures do **not**
  bump `consecutive_failures` (`anthropic.py:155-165` returns early, never
  calls `with_failed_attempt`), so the schedule doesn't change as a result
  of a manual click. Adding a "next retry in 10m" clause would mislead by
  describing state that this click did not affect.
- UI quota card countdown — out of scope. Log-only change.
- Non-Anthropic providers (`minimax`, `zhipu`, `deepseek`) — these all
  use `log_fetch_failure` from the same module, so they pick up the new
  clause automatically; no per-provider code change is part of this work.

## Solution

### Output

**Failures 1–4** (counter goes 1→2→3→4, still under threshold):

```
[ts] [claude-island] anthropic quota fetch: HTTP 429 Too Many Requests — last attempt 20m ago — last success 37m ago — next retry in 10m
```

Values follow the schedule (`POLL_TTL << new_consecutive_failures`,
clamped at `POLL_TTL_MAX`):

| New counter | Next retry |
|---|---|
| 1 | 10m |
| 2 | 20m |
| 3 | 40m |
| 4 | 80m |

**Failure 5** (counter reaches `AUTO_REFRESH_FAILURE_THRESHOLD=5`, circuit
opens):

```
[ts] [claude-island] anthropic quota fetch: HTTP 429 Too Many Requests — last attempt 80m ago — last success 117m ago — auto-refresh paused, manual ↻ only
```

After failure 5, `is_fetch_due` returns False permanently — no further
auto-fetch happens, so no further auto log lines exist. This makes the
paused clause a one-shot announcement at the moment the circuit opens.

### Where the change lives

Only `log_fetch_failure()` in
`claude_island/platform_/providers/__init__.py:905-946`. ~10 lines added
just before the `safe_stderr_write` call.

```python
new_state = prior.with_failed_attempt(now=now)
if new_state.consecutive_failures >= AUTO_REFRESH_FAILURE_THRESHOLD:
    parts.append("auto-refresh paused, manual ↻ only")
else:
    next_window_sec = new_state._backoff_window_seconds()
    parts.append(f"next retry in {int(next_window_sec // 60)}m")
```

Rationale:

- **Compute new state inside the log function.** `prior.with_failed_attempt(now=now)`
  is a pure transition that gives us the post-failure counter without
  touching the IO path. We don't change the function signature — callers
  (`anthropic.fetch`, `minimax`, `zhipu`) keep passing `prior` and still
  call `with_failed_attempt` separately to actually update the cache.
  We're doing the same projection twice, but the alternative — changing
  the signature to `(prior, *, new_state, reason, now)` — would touch
  every call site for a 1-line computation.
- **`>=` not `==`.** Defensive against a future change that raises the
  threshold or that somehow lets `consecutive_failures` exceed it.
- **`// 60` is safe.** Every schedule value (`POLL_TTL=300`, `POLL_TTL_MAX=18000`)
  is a multiple of 60. Integer minutes match the existing `_fmt_ago`
  buckets and the schedule's natural granularity.
- **Em-dash separator** (`" — "`) matches the existing `parts.append(...)` pattern.

### What does NOT change

- `with_failed_attempt`, `with_successful_fetch`, `_backoff_window_seconds` — unchanged.
- `AUTO_REFRESH_FAILURE_THRESHOLD`, `POLL_TTL`, `POLL_TTL_MAX` — unchanged.
- `QuotaSnapshot`, UI — untouched.
- All other providers (minimax / zhipu / deepseek) — pick up the hint
  automatically through the shared helper, no per-file change.
- Manual ⟳ failure stderr line (`anthropic.py:143-146, 161-164`) — not touched.

## Correctness Argument

The hint accurately describes what will happen next *for the auto path*:

1. **Counter projection is correct.** `prior.with_failed_attempt(now=now)`
   returns a state with `consecutive_failures = prior.consecutive_failures + 1`.
   This is exactly what the caller will persist via `write_cache_state`
   immediately after `log_fetch_failure` returns (see
   `anthropic.py:169-171`).
2. **The next auto-attempt timing is computed from that new counter.**
   `_backoff_window_seconds()` reads `consecutive_failures` and returns
   `POLL_TTL << consecutive_failures` (clamped). So
   `new_state._backoff_window_seconds()` is the same window
   `is_fetch_due` will gate against on the next tick.
3. **Manual ⟳ success resets the counter** (verified at `anthropic.py:174-177`
   → `with_successful_fetch` in `__init__.py:824-841` sets
   `consecutive_failures=0`). If the user recovers via manual click
   between auto-failures, the next auto-failure starts the counter at 1
   again and the hint shows `next retry in 10m`. The hint reflects the
   *current* state at the moment of logging — it doesn't claim to predict
   user actions.
4. **`log_fetch_failure` is only called on the auto path.** The manual ⟳
   path uses `safe_stderr_write` directly (`anthropic.py:143-146,
   161-164`). So we never produce a "next retry in Xm" clause attached to
   a manual failure, which would be confusing because manual failures
   don't change the schedule.

## Testing

Add the following cases to `tests/platform_/test_providers.py` in
`TestLogFetchFailure` (which already covers the existing failure-log
behaviour):

### Happy path — relative window doubles per failure

For each `prior_failures ∈ {0, 1, 2, 3}`, construct a `QuotaCacheState`
with that counter, call `log_fetch_failure`, and assert the line ends
with the expected `next retry in {10,20,40,80}m`.

- `prior=0` → "next retry in 10m"
- `prior=1` → "next retry in 20m"
- `prior=2` → "next retry in 40m"
- `prior=3` → "next retry in 80m"

### Circuit-breaker boundary — paused copy at threshold

`prior_failures=4` (the failure being logged will push counter to 5,
which equals threshold). Assert the line contains
`"auto-refresh paused, manual ↻ only"` and does **not** contain
`"next retry in"`.

### Defensive — beyond threshold also shows paused copy

`prior_failures=10` (somehow we're way past the threshold — could happen
if threshold is lowered between releases and a cache file carries the
old high counter). Assert paused copy, no "next retry" clause.

### Negative — no manual ⟳ regression

This is covered by the existing structure (manual path doesn't call
`log_fetch_failure`), but add a sanity test: invoke `AnthropicProvider().fetch`
with `bypass_cache=True` and a mocked HTTP failure; assert the stderr
contains the existing `manual ⟳ failed:` prefix and does **not** contain
`next retry in` or `auto-refresh paused`.

### Pass criteria

All existing `TestLogFetchFailure` tests continue to pass. Five new tests
above pass. `python -m import_linter` reports no violations.

## Migration & Compatibility

None. Log-only change, no schema, no public API.

## Risks

- **Type A (cost of choosing relative time):** A user reading scrollback
  long after the failure can't compute the absolute next-attempt clock
  time without knowing when the failure landed. Mitigation: the existing
  `[YYYY-MM-DD HH:MM:SS]` prefix on the same line gives the absolute
  anchor; the user can add the window mentally if they care. The
  alternative (`next retry at HH:MM`) was rejected because it
  ambiguates across day-boundary / timezone change cases that the user
  already has to interpret for the existing relative `last attempt Xm
  ago` clause.
- **Type B (intrinsic fragility):** The hint becomes wrong if a code
  change makes `with_failed_attempt` non-pure, or if the schedule
  computation in `_backoff_window_seconds` diverges from what
  `is_fetch_due` checks. Mitigation: both are tested at unit level
  (existing tests pin the schedule; the new tests pin the hint output).
  A divergence would surface as a test failure, not just a misleading log
  line.
