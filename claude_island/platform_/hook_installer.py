"""Hook installer — manages ``~/.claude/settings.json`` and the bundled
hook script copy at ``~/.claude-island/hook.py``.

Three responsibilities:

  1. ``sync_hook_script(bundled, dest)`` — copy the pip-bundled
     ``claude_island/hook.py`` to a stable absolute path the user can
     reference from settings.json. Diffs by ``__version__`` so a no-op
     boot doesn't churn disk.

  2. ``install_if_needed(settings_path, hook_command)`` — idempotently
     add our per-event hook entries to settings.json. Preserves every
     user-authored hook entry (matches them by absence of the
     ``.claude-island`` substring in their ``command``).

  3. ``build_hook_command(python_exe, hook_script)`` — produces the
     exact ``command`` string we write into settings.json. Handles
     paths with spaces by always double-quoting.

Concurrency model: we never read settings.json while another writer is
mid-write. Production writes are atomic (tmp + os.replace). The Claude
CLI itself reads settings.json on startup, not continuously, so the
race window is narrow. We don't try to flock the file — it would be
brittle on Windows.

Per Q-Open-1 "不让卸载": no uninstall function exists. Users who really
want out edit settings.json by hand; we never remove our entries here,
even if asked. This is documented in README.
"""
from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Per-event install spec (F-6).
# Mirrors open-vibe-island's ClaudeHookInstaller.swift:49-64 layout.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class HookEventSpec:
    """One row of the install table.

    ``matcher``: when present, written as the group's ``matcher`` field
    (Claude Code uses this to limit which tools fire a per-tool hook).
    Use ``"*"`` to match any tool. ``None`` means omit the field
    entirely (which is the right shape for session-lifecycle hooks).

    ``timeout_seconds``: written as the hook's ``timeout`` field. None
    omits (Claude uses its default). PermissionRequest's short timeout
    is the only one we set — see comment on the constant table.
    """
    name: str
    matcher: str | None
    timeout_seconds: int | None


# v1 installs all 11 events. PermissionRequest gets a short 10s timeout
# because the v1 hook server returns ``{}`` immediately (we don't block
# Claude on it) — but if the server crashes mid-startup we want Claude
# to give up fast and fall back to its built-in terminal prompt rather
# than hang for Claude's default permission-hook timeout.
HOOK_EVENTS_TO_INSTALL: tuple[HookEventSpec, ...] = (
    HookEventSpec("SessionStart",       None, None),
    HookEventSpec("SessionEnd",         None, None),
    HookEventSpec("UserPromptSubmit",   None, None),
    HookEventSpec("PreToolUse",         "*",  None),
    HookEventSpec("PostToolUse",        "*",  None),
    HookEventSpec("PostToolUseFailure", "*",  None),
    HookEventSpec("Stop",               None, None),
    HookEventSpec("StopFailure",        None, None),
    HookEventSpec("PreCompact",         None, None),
    HookEventSpec("Notification",       "*",  None),
    HookEventSpec("PermissionRequest",  "*",  10),
)


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class InstallResult:
    """Outcome of install_if_needed.

    ``changed``: True iff settings.json content actually differs after
    the call. Idempotent re-runs return False.
    ``installed_events``: event names we added during this call (empty
    when already installed).
    ``user_hooks_preserved``: count of user-authored hook entries we
    kept untouched. Useful for the log line so the user can see we
    didn't trash their custom config.
    """
    changed: bool
    installed_events: tuple[str, ...]
    user_hooks_preserved: int


class InstallError(RuntimeError):
    """Raised when settings.json exists but is unreadable / not valid JSON.
    Wiring layer logs and continues (degrades to scanner-only)."""


# ---------------------------------------------------------------------------
# sync_hook_script — copy bundled hook.py to ~/.claude-island/
# ---------------------------------------------------------------------------


_VERSION_PATTERN = re.compile(r'^__version__\s*=\s*[\'"]([^\'"]+)[\'"]', re.MULTILINE)


def sync_hook_script(*, bundled_script: Path, dest: Path) -> bool:
    """Copy bundled hook.py to ``dest`` if missing or version-mismatched.

    Returns True if a write happened (caller can log "installed/updated"),
    False if dest was already up to date.

    The version key is the ``__version__`` constant inside the file
    (parsed with a regex — cheap, no exec needed). When bundled and
    dest have the same version we skip the write so disk doesn't churn
    on every boot.

    Errors are logged and re-raised — caller decides whether failing
    here is fatal. (In production, __main__ degrades to "skip hook
    install, run scanner-only".)
    """
    dest.parent.mkdir(parents=True, exist_ok=True)

    bundled_version = _read_hook_version(bundled_script)
    if dest.exists():
        dest_version = _read_hook_version(dest)
        if dest_version is not None and dest_version == bundled_version:
            return False

    # Atomic copy: write to .tmp sibling, then os.replace.
    tmp = dest.with_suffix(dest.suffix + ".tmp")
    tmp.write_bytes(bundled_script.read_bytes())
    os.replace(tmp, dest)
    log.info(
        "synced hook script to %s (version %s)",
        dest, bundled_version or "?",
    )
    return True


def _read_hook_version(path: Path) -> str | None:
    """Extract the ``__version__`` literal from a Python source file.

    Returns None if the file is missing, unreadable, or doesn't contain
    a recognizable assignment. Never executes the file — pure regex
    against the source.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    m = _VERSION_PATTERN.search(text)
    return m.group(1) if m else None


# ---------------------------------------------------------------------------
# build_hook_command — render the settings.json `command` string
# ---------------------------------------------------------------------------


def build_hook_command(*, python_exe: str, hook_script: Path) -> str:
    """Produce a shell-safe command string for settings.json.

    Always double-quotes both paths so Windows paths with spaces (the
    norm) and Unix paths with spaces (rare but possible) work without
    extra escaping. Claude Code spawns this through the OS shell, so
    quoting follows shell conventions.
    """
    py = python_exe.replace('"', '\\"')
    script = str(hook_script).replace('"', '\\"')
    return f'"{py}" "{script}"'


# ---------------------------------------------------------------------------
# install_if_needed — idempotent settings.json mutation
# ---------------------------------------------------------------------------


def install_if_needed(
    *,
    settings_path: Path,
    hook_command: str,
) -> InstallResult:
    """Ensure every event in ``HOOK_EVENTS_TO_INSTALL`` has our hook
    registered in ``settings_path``.

    Behaviour:
      * If settings.json doesn't exist → create it with only our hooks.
      * If it exists → load JSON, merge our entries in. User hook entries
        (those whose ``command`` doesn't contain "claude-island") are
        preserved. Our own entries (older copies of our command) are
        replaced with the current ``hook_command`` so a Python upgrade
        that changes the absolute path doesn't accumulate stale entries.
      * If already fully installed with the same command → no-op
        (``changed=False``).
      * If settings.json exists but is malformed → raise InstallError.

    Atomic write: tmp + os.replace.
    """
    existing_data: dict
    if settings_path.exists():
        try:
            text = settings_path.read_text(encoding="utf-8")
        except OSError as e:
            raise InstallError(f"could not read {settings_path}: {e}") from e
        if not text.strip():
            existing_data = {}
        else:
            try:
                existing_data = json.loads(text)
            except json.JSONDecodeError as e:
                raise InstallError(
                    f"{settings_path} is not valid JSON: {e}"
                ) from e
            if not isinstance(existing_data, dict):
                raise InstallError(
                    f"{settings_path} root must be a JSON object, "
                    f"got {type(existing_data).__name__}"
                )
    else:
        existing_data = {}

    # Build the new hooks tree.
    existing_hooks = existing_data.get("hooks")
    if not isinstance(existing_hooks, dict):
        existing_hooks = {}

    new_hooks: dict = {}
    installed_events: list[str] = []
    user_preserved = 0

    # Step 1: copy every existing event's groups, sanitized (drop our own
    # old entries, keep user entries).
    for event_name, groups in existing_hooks.items():
        if not isinstance(groups, list):
            continue
        kept_groups, removed = _strip_our_groups(groups)
        user_preserved += sum(_count_user_hooks(g) for g in kept_groups)
        if kept_groups:
            new_hooks[event_name] = kept_groups
        if removed and event_name not in {s.name for s in HOOK_EVENTS_TO_INSTALL}:
            # Edge case: settings.json had our hook on an event we no
            # longer install. Keep removing it but otherwise leave the
            # event untouched (don't re-add).
            pass

    # Step 2: append our managed entry for every event in the table.
    for spec in HOOK_EVENTS_TO_INSTALL:
        prior_groups = new_hooks.get(spec.name, [])
        managed_group = _managed_group_for(spec, hook_command)
        # If a prior identical managed group survives (shouldn't, _strip_our_groups
        # removes them — but defense in depth), skip.
        if not any(_equiv_group(g, managed_group) for g in prior_groups):
            installed_events.append(spec.name)
        new_hooks[spec.name] = prior_groups + [managed_group]

    # Step 3: rewrite settings.json if changed.
    new_data = dict(existing_data)
    new_data["hooks"] = new_hooks
    new_text = json.dumps(new_data, indent=2, sort_keys=True)

    if settings_path.exists():
        try:
            old_text = settings_path.read_text(encoding="utf-8")
        except OSError:
            old_text = ""
    else:
        old_text = ""

    if old_text == new_text:
        return InstallResult(
            changed=False,
            installed_events=(),
            user_hooks_preserved=user_preserved,
        )

    settings_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = settings_path.with_suffix(settings_path.suffix + ".tmp")
    tmp.write_text(new_text, encoding="utf-8")
    os.replace(tmp, settings_path)

    return InstallResult(
        changed=True,
        installed_events=tuple(installed_events),
        user_hooks_preserved=user_preserved,
    )


def is_installed(*, settings_path: Path, hook_command: str) -> bool:
    """Quick predicate: does every event in HOOK_EVENTS_TO_INSTALL have
    our hook installed with the given command?

    Used by ``--doctor`` and by tests. install_if_needed is the canonical
    truth — this is a read-only probe."""
    if not settings_path.exists():
        return False
    try:
        text = settings_path.read_text(encoding="utf-8")
        data = json.loads(text)
    except (OSError, json.JSONDecodeError):
        return False
    if not isinstance(data, dict):
        return False
    hooks = data.get("hooks")
    if not isinstance(hooks, dict):
        return False
    for spec in HOOK_EVENTS_TO_INSTALL:
        groups = hooks.get(spec.name)
        if not isinstance(groups, list):
            return False
        managed = _managed_group_for(spec, hook_command)
        if not any(_equiv_group(g, managed) for g in groups):
            return False
    return True


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _managed_group_for(spec: HookEventSpec, hook_command: str) -> dict:
    """The single hook group object representing our managed entry for
    one event in settings.json."""
    hook_obj: dict = {"type": "command", "command": hook_command}
    if spec.timeout_seconds is not None:
        hook_obj["timeout"] = spec.timeout_seconds
    group: dict = {"hooks": [hook_obj]}
    if spec.matcher is not None:
        group["matcher"] = spec.matcher
    return group


def _is_our_hook(hook_obj: dict) -> bool:
    """Heuristic: does this single hook entry look like ours?

    Matches by substring on the command field — our installed path
    always contains ``.claude-island`` (the user-home directory we
    sync the script to). Same convention as
    hook.py:_contains_our_hook (kept in sync deliberately so they
    agree on what "our" means)."""
    cmd = hook_obj.get("command")
    if not isinstance(cmd, str):
        return False
    lower = cmd.lower().replace("\\", "/")
    return ".claude-island/hook.py" in lower or "claude-island/hook.py" in lower


def _strip_our_groups(groups: list) -> tuple[list[dict], int]:
    """From a list of group objects, drop any group whose hooks list
    consists ENTIRELY of our-managed hooks. Returns (kept_groups,
    num_removed).

    Why "entirely": Claude lets a single group bundle multiple commands
    via a hooks array. If a group has 3 user commands + 1 of ours, we
    want to remove only our command and keep the rest. If a group has
    only ours, drop the whole group.
    """
    kept: list[dict] = []
    removed = 0
    for g in groups:
        if not isinstance(g, dict):
            continue
        hooks = g.get("hooks")
        if not isinstance(hooks, list):
            # Malformed entry — keep as-is, we don't second-guess the user.
            kept.append(g)
            continue
        filtered = [h for h in hooks if isinstance(h, dict) and not _is_our_hook(h)]
        if not filtered:
            # All hooks in this group were ours — drop the whole group
            removed += 1
            continue
        if len(filtered) != len(hooks):
            removed += 1
            # Rebuild the group with the filtered hooks list
            new_g = dict(g)
            new_g["hooks"] = filtered
            kept.append(new_g)
        else:
            kept.append(g)
    return kept, removed


def _count_user_hooks(group: dict) -> int:
    hooks = group.get("hooks")
    if not isinstance(hooks, list):
        return 0
    return sum(1 for h in hooks if isinstance(h, dict) and not _is_our_hook(h))


def _equiv_group(a: dict, b: dict) -> bool:
    """Two group dicts represent the same managed entry iff their
    matcher and the single hook within are equal. Used to detect a
    no-op install — if a managed entry identical to ours is already
    present we don't add a duplicate."""
    if a.get("matcher") != b.get("matcher"):
        return False
    ha = a.get("hooks") or []
    hb = b.get("hooks") or []
    if len(ha) != 1 or len(hb) != 1:
        return False
    return ha[0] == hb[0]
