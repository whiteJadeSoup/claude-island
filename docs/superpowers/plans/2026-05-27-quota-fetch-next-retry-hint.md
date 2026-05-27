# Quota Fetch — "Next Retry" Hint Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Append a "next retry in Xm" (or "auto-refresh paused, manual ⟳ only" at the circuit-breaker boundary) clause to the auto-fetch failure stderr line, so users can read the next-attempt cadence directly off the log.

**Architecture:** Single-function change inside `log_fetch_failure()`. Project the post-failure state by calling the pure transition `prior.with_failed_attempt(now=now)`, branch on `consecutive_failures >= AUTO_REFRESH_FAILURE_THRESHOLD`, append one parts entry, done. No signature changes, no new public surface, no UI work.

**Tech Stack:** Python, pytest, `capsys` fixture, `safe_stderr_write` (stderr write), `import_linter` (architecture gate).

**Spec:** `docs/superpowers/specs/2026-05-27-quota-fetch-next-retry-hint-design.md`

---

## File Structure

**Modify:**

- `claude_island/platform_/providers/__init__.py` — add ~10 lines inside `log_fetch_failure()` (lines 905-946). The new lines go between the existing `parts.append("last success ...")` and `safe_stderr_write(" — ".join(parts))`.
- `tests/platform_/test_providers.py` — extend `TestLogFetchFailure` (starts at line 1480) with 5 new test cases.

**No new files.**

---

## Task 1: TDD red — failing test for "next retry in 10m" on first failure

**Files:**
- Modify: `tests/platform_/test_providers.py:1480-1589` (TestLogFetchFailure class — append a new method)

- [ ] **Step 1: Add the failing test**

Append this method inside the `TestLogFetchFailure` class (after the existing `test_log_runs_before_cache_bumps`):

```python
    def test_log_appends_next_retry_window_for_first_failure(self, capsys):
        """First auto failure (consecutive_failures: 0 → 1): hint should
        read 'next retry in 10m' (POLL_TTL × 2). This is the most common
        line a user sees, so it gets the dedicated test."""
        from claude_island.platform_.providers import (
            log_fetch_failure, QuotaCacheState,
        )
        now = datetime(2026, 5, 5, 12, 0, 0, tzinfo=timezone.utc)
        prior = QuotaCacheState(
            provider="anthropic",
            fetched_at=datetime(2026, 5, 5, 11, 30, 0, tzinfo=timezone.utc),
            last_attempt_at=datetime(2026, 5, 5, 11, 55, 0, tzinfo=timezone.utc),
            five_hour=None, seven_day=None,
            consecutive_failures=0,
        )
        log_fetch_failure(prior, reason="HTTP 429", now=now)
        line = capsys.readouterr().err.strip()
        assert "next retry in 10m" in line, f"missing hint: {line!r}"
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `pytest tests/platform_/test_providers.py::TestLogFetchFailure::test_log_appends_next_retry_window_for_first_failure -v`

Expected: FAIL with `AssertionError: missing hint: '...'` — current log line has no `next retry in 10m` clause.

---

## Task 2: TDD green — minimal implementation

**Files:**
- Modify: `claude_island/platform_/providers/__init__.py:905-946` (log_fetch_failure)

- [ ] **Step 1: Add the hint-appending code**

Locate `log_fetch_failure` (line 905). Find these lines near the end of the function (around line 941-946):

```python
    parts.append(
        f"last success {_fmt_ago(now - prior.fetched_at)} ago"
        if prior.fetched_at is not None
        else "no prior success"
    )
    safe_stderr_write(" — ".join(parts))
```

Insert the new block between them so the function ends like this:

```python
    parts.append(
        f"last success {_fmt_ago(now - prior.fetched_at)} ago"
        if prior.fetched_at is not None
        else "no prior success"
    )

    # Project the post-failure counter so the hint reflects the state
    # the cache will hold immediately after this call. with_failed_attempt
    # is a pure transition (no IO); the caller writes the same projected
    # state to disk right after we return.
    projected = prior.with_failed_attempt(now=now)
    if projected.consecutive_failures >= AUTO_REFRESH_FAILURE_THRESHOLD:
        # Circuit just opened (or is already open in a defensive edge
        # case). is_fetch_due will gate auto-fetch off permanently until
        # a manual ⟳ success resets the counter — surface that here so
        # the user doesn't wait for a 10/20/40/80m window that will
        # never come.
        parts.append("auto-refresh paused, manual ⟳ only")
    else:
        next_window_sec = projected._backoff_window_seconds()
        # POLL_TTL and POLL_TTL_MAX are both multiples of 60, so integer
        # minutes lose no precision and match the existing _fmt_ago
        # buckets the user is reading on the same line.
        parts.append(f"next retry in {int(next_window_sec // 60)}m")

    safe_stderr_write(" — ".join(parts))
```

- [ ] **Step 2: Run the new test to verify it passes**

Run: `pytest tests/platform_/test_providers.py::TestLogFetchFailure::test_log_appends_next_retry_window_for_first_failure -v`

Expected: PASS.

- [ ] **Step 3: Run all existing TestLogFetchFailure tests to verify no regression**

Run: `pytest tests/platform_/test_providers.py::TestLogFetchFailure -v`

Expected: ALL PASS (the existing 4 tests do `assert "X" in line` style checks — they don't pin the absence of new clauses, so the appended hint won't break them).

---

## Task 3: Parameterized test for the full schedule (failures 1/2/3 → 20/40/80m)

**Files:**
- Modify: `tests/platform_/test_providers.py` (TestLogFetchFailure class — append)

- [ ] **Step 1: Add the parameterized test**

Append this method to `TestLogFetchFailure`:

```python
    @pytest.mark.parametrize("prior_failures,expected_minutes", [
        (0, 10),  # 0 → 1, POLL_TTL × 2  = 600s  = 10m
        (1, 20),  # 1 → 2, POLL_TTL × 4  = 1200s = 20m
        (2, 40),  # 2 → 3, POLL_TTL × 8  = 2400s = 40m
        (3, 80),  # 3 → 4, POLL_TTL × 16 = 4800s = 80m
    ])
    def test_log_next_retry_window_follows_backoff_schedule(
        self, capsys, prior_failures, expected_minutes,
    ):
        """Each step on the doubling ladder gets its own hint value.
        Pins the formula and the schedule simultaneously — a change to
        either POLL_TTL or the shift expression in
        _backoff_window_seconds will surface here."""
        from claude_island.platform_.providers import (
            log_fetch_failure, QuotaCacheState,
        )
        now = datetime(2026, 5, 5, 12, 0, 0, tzinfo=timezone.utc)
        prior = QuotaCacheState(
            provider="anthropic",
            fetched_at=None,
            last_attempt_at=datetime(2026, 5, 5, 11, 55, 0, tzinfo=timezone.utc),
            five_hour=None, seven_day=None,
            consecutive_failures=prior_failures,
        )
        log_fetch_failure(prior, reason="HTTP 429", now=now)
        line = capsys.readouterr().err.strip()
        assert f"next retry in {expected_minutes}m" in line, \
            f"failures={prior_failures}: missing hint, got {line!r}"
```

Also ensure `import pytest` is at the top of the file (it likely already is — check first).

- [ ] **Step 2: Verify pytest is already imported**

Run: `grep -n "^import pytest\|^from pytest" tests/platform_/test_providers.py | head -3`

Expected: at least one match (the file already uses fixtures, so `pytest` is imported). If not, add `import pytest` at the top with the other imports.

- [ ] **Step 3: Run the parameterized test**

Run: `pytest tests/platform_/test_providers.py::TestLogFetchFailure::test_log_next_retry_window_follows_backoff_schedule -v`

Expected: 4 PASS (one per parameter set).

---

## Task 4: Test for circuit-breaker boundary (failure 5 → paused copy)

**Files:**
- Modify: `tests/platform_/test_providers.py` (TestLogFetchFailure class — append)

- [ ] **Step 1: Add the boundary test**

Append:

```python
    def test_log_uses_paused_copy_when_failure_reaches_threshold(self, capsys):
        """Failure that pushes counter to AUTO_REFRESH_FAILURE_THRESHOLD
        (= 5) is the last auto failure that ever gets logged — the next
        is_fetch_due check will return False permanently. Surface the
        circuit-breaker state inline so the user doesn't wait silently
        for a window that will never come."""
        from claude_island.platform_.providers import (
            log_fetch_failure, QuotaCacheState,
            AUTO_REFRESH_FAILURE_THRESHOLD,
        )
        now = datetime(2026, 5, 5, 12, 0, 0, tzinfo=timezone.utc)
        prior = QuotaCacheState(
            provider="anthropic",
            fetched_at=None,
            last_attempt_at=datetime(2026, 5, 5, 11, 55, 0, tzinfo=timezone.utc),
            five_hour=None, seven_day=None,
            # prior=4 → projected=5 == THRESHOLD → paused
            consecutive_failures=AUTO_REFRESH_FAILURE_THRESHOLD - 1,
        )
        log_fetch_failure(prior, reason="HTTP 429", now=now)
        line = capsys.readouterr().err.strip()
        assert "auto-refresh paused, manual ⟳ only" in line, \
            f"missing paused copy: {line!r}"
        assert "next retry in" not in line, \
            f"paused state must not advertise a retry window: {line!r}"
```

- [ ] **Step 2: Run the test**

Run: `pytest tests/platform_/test_providers.py::TestLogFetchFailure::test_log_uses_paused_copy_when_failure_reaches_threshold -v`

Expected: PASS.

---

## Task 5: Defensive test for counter beyond threshold

**Files:**
- Modify: `tests/platform_/test_providers.py` (TestLogFetchFailure class — append)

- [ ] **Step 1: Add the defensive test**

Append:

```python
    def test_log_uses_paused_copy_when_counter_already_beyond_threshold(
        self, capsys,
    ):
        """Edge: a cache file carries a high consecutive_failures from a
        prior release where the threshold was higher, and we're decoding
        it with the current (lower) threshold. The hint must still
        choose the paused copy rather than format an out-of-range
        backoff window."""
        from claude_island.platform_.providers import (
            log_fetch_failure, QuotaCacheState,
        )
        now = datetime(2026, 5, 5, 12, 0, 0, tzinfo=timezone.utc)
        prior = QuotaCacheState(
            provider="anthropic",
            fetched_at=None,
            last_attempt_at=datetime(2026, 5, 5, 11, 55, 0, tzinfo=timezone.utc),
            five_hour=None, seven_day=None,
            consecutive_failures=10,  # well past THRESHOLD=5
        )
        log_fetch_failure(prior, reason="HTTP 429", now=now)
        line = capsys.readouterr().err.strip()
        assert "auto-refresh paused, manual ⟳ only" in line
        assert "next retry in" not in line
```

- [ ] **Step 2: Run the test**

Run: `pytest tests/platform_/test_providers.py::TestLogFetchFailure::test_log_uses_paused_copy_when_counter_already_beyond_threshold -v`

Expected: PASS.

---

## Task 6: Regression test for manual ⟳ failure path

**Files:**
- Modify: `tests/platform_/test_providers.py` (TestLogFetchFailure class — append)

- [ ] **Step 1: Add the manual-path regression test**

Append:

```python
    def test_manual_refresh_failure_does_not_carry_next_retry_hint(
        self, tmp_path, capsys,
    ):
        """Manual ⟳ failure goes through safe_stderr_write directly
        (anthropic.py:155-165), not log_fetch_failure, so it must not
        gain the 'next retry in Xm' clause. Manual failures don't bump
        consecutive_failures and don't change the schedule — quoting a
        retry window would describe state the click didn't affect."""
        from claude_island.platform_.providers import anthropic as anth
        cache_path = tmp_path / "anthropic-quota.json"
        cache_path.write_text(json.dumps({"provider": "anthropic"}))
        creds = tmp_path / "credentials.json"
        creds.write_text(json.dumps({
            "claudeAiOauth": {"accessToken": "fake"},
        }))
        with patch.object(anth, "_CREDENTIALS_PATH", creds), \
             patch.object(anth, "_fetch_http", return_value=(None, "HTTP 429")):
            anth.AnthropicProvider().fetch(cache_dir=tmp_path, bypass_cache=True)
        line = capsys.readouterr().err.strip()
        assert "manual ⟳ failed" in line, \
            f"manual path log prefix changed: {line!r}"
        assert "next retry in" not in line, \
            f"manual failure must not advertise a retry window: {line!r}"
        assert "auto-refresh paused" not in line, \
            f"manual failure must not advertise paused state: {line!r}"
```

- [ ] **Step 2: Run the test**

Run: `pytest tests/platform_/test_providers.py::TestLogFetchFailure::test_manual_refresh_failure_does_not_carry_next_retry_hint -v`

Expected: PASS.

---

## Task 7: Run the full provider test suite + import linter

- [ ] **Step 1: Run the entire test_providers.py file**

Run: `pytest tests/platform_/test_providers.py -v`

Expected: all tests pass, including the existing `TestQuotaCacheState`, `TestCircuitBreaker`, `TestLogFetchFailure`, plus the 5 new tests added in Tasks 1, 3, 4, 5, 6. No failures.

- [ ] **Step 2: Run the full test suite**

Run: `pytest tests/ -v --tb=short`

Expected: all tests pass. Watch for any cross-file test that asserts the exact content/line-count of `log_fetch_failure`'s output — there shouldn't be any (the existing test_providers.py tests use `assert "..." in line` style), but verify.

- [ ] **Step 3: Run import-linter to verify architecture layering is intact**

Run: `python -m import_linter`

Expected: all contracts pass. We didn't add new cross-layer imports — `AUTO_REFRESH_FAILURE_THRESHOLD` and `_backoff_window_seconds()` are already in the same module — so this should be a no-op verification.

---

## Task 8: Manual smoke test (optional but recommended)

The unit tests pin the format, but a quick visual check confirms the line renders correctly with the actual `safe_stderr_write` (which handles encoding edge cases) and the actual datetime formatting.

- [ ] **Step 1: Run a one-liner that prints a sample failure line**

Run:

```bash
python -c "
from datetime import datetime, timezone
from claude_island.platform_.providers import log_fetch_failure, QuotaCacheState
now = datetime.now(timezone.utc)
prior = QuotaCacheState(
    provider='anthropic',
    fetched_at=None,
    last_attempt_at=None,
    five_hour=None, seven_day=None,
    consecutive_failures=2,  # next will be 3 → 40m
)
log_fetch_failure(prior, reason='HTTP 429 Too Many Requests', now=now)
"
```

Expected stderr output (one line, with current local timestamp):

```
[YYYY-MM-DD HH:MM:SS] [claude-island] anthropic quota fetch: HTTP 429 Too Many Requests — first attempt — no prior success — next retry in 40m
```

The em-dashes, the `next retry in 40m` clause, and the trailing newline should all be present. If anything looks off (extra spaces, wrong separator, wrong window value), stop and re-check Task 2 step 1.

---

## Task 9: Commit

- [ ] **Step 1: Verify only the intended files changed**

Run: `git status`

Expected: exactly two modified files:
- `claude_island/platform_/providers/__init__.py`
- `tests/platform_/test_providers.py`

Plus two new untracked files (the spec and this plan):
- `docs/superpowers/specs/2026-05-27-quota-fetch-next-retry-hint-design.md`
- `docs/superpowers/plans/2026-05-27-quota-fetch-next-retry-hint.md`

If anything else is modified, stop and investigate.

- [ ] **Step 2: Stage the implementation + spec + plan**

Run:

```bash
git add claude_island/platform_/providers/__init__.py \
        tests/platform_/test_providers.py \
        docs/superpowers/specs/2026-05-27-quota-fetch-next-retry-hint-design.md \
        docs/superpowers/plans/2026-05-27-quota-fetch-next-retry-hint.md
```

- [ ] **Step 3: Commit**

Run:

```bash
git commit -m "$(cat <<'EOF'
feat(quota): show "next retry in Xm" on auto-fetch failure log

Until now the failure stderr line told the user *what* broke and *when
it last attempted/succeeded*, but never *when the auto-refresher will
try again*. With exponential backoff capped at 5h and a circuit breaker
that opens at 5 consecutive failures, the silence between auto retries
can be anywhere from 5 min (happy) to forever (paused) — and the user
had to know the internal schedule to interpret which.

Append one clause to log_fetch_failure() inside providers/__init__.py:

  failures 1-4 → "next retry in 10m" / "20m" / "40m" / "80m"
  failure 5    → "auto-refresh paused, manual ⟳ only"

The hint is computed from the projected post-failure state
(prior.with_failed_attempt — a pure transition we already trust to
drive is_fetch_due gating) so the reported window matches exactly what
the next auto-tick will gate against. _backoff_window_seconds()'s
output is divided by 60 cleanly because POLL_TTL and POLL_TTL_MAX are
both multiples of 60.

Manual ⟳ failure is intentionally untouched — it goes through
safe_stderr_write directly (anthropic.py:155-165) and does NOT call
log_fetch_failure or with_failed_attempt. Manual failures don't bump
consecutive_failures (verified path), so quoting a retry window would
describe state the click did not affect. Regression test pins this.

Tests:
  * test_log_appends_next_retry_window_for_first_failure
    — pins the most common happy line.
  * test_log_next_retry_window_follows_backoff_schedule (parametrized,
    failures=0..3 → 10/20/40/80m)
    — pins the full ladder; will fail if POLL_TTL or the shift formula
      in _backoff_window_seconds drifts.
  * test_log_uses_paused_copy_when_failure_reaches_threshold
    — boundary at AUTO_REFRESH_FAILURE_THRESHOLD-1 → THRESHOLD.
  * test_log_uses_paused_copy_when_counter_already_beyond_threshold
    — defensive against an old cache file with a higher counter.
  * test_manual_refresh_failure_does_not_carry_next_retry_hint
    — regression pin: manual ⟳ stderr keeps its existing shape.

Spec: docs/superpowers/specs/2026-05-27-quota-fetch-next-retry-hint-design.md
Plan: docs/superpowers/plans/2026-05-27-quota-fetch-next-retry-hint.md

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

- [ ] **Step 4: Verify the commit**

Run: `git log -1 --stat`

Expected: the commit lists the four files above, with a small line-count delta on the two modified files and the two new doc files at full size.

---

## Self-Review Summary

- **Spec coverage:** every Goal/Non-Goal/Solution element in the spec maps to at least one task above. Failure-1 hint → Task 1. Schedule ladder → Task 3. Circuit-breaker → Task 4. Defensive edge → Task 5. Manual ⟳ non-regression → Task 6. Migration / compatibility → not needed (log-only). ✓
- **Placeholder scan:** no TBD/TODO. Every code block contains the actual code. ✓
- **Type consistency:** `AUTO_REFRESH_FAILURE_THRESHOLD`, `QuotaCacheState`, `log_fetch_failure`, `with_failed_attempt`, `_backoff_window_seconds` — all match the names in `claude_island/platform_/providers/__init__.py`. ✓
