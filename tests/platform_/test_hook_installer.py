"""Tests for hook_installer — settings.json mutation + script sync.

T4.x + T10.x families from Detail Design v2 §7. Pure unit tests — no
network, no subprocess, all file I/O against tmp_path.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from claude_island.platform_.hook_installer import (
    HOOK_EVENTS_TO_INSTALL,
    InstallError,
    InstallResult,
    _is_our_hook,
    _strip_our_groups,
    build_hook_command,
    install_if_needed,
    is_installed,
    sync_hook_script,
)


_OUR_CMD = '"C:/python.exe" "C:/Users/u/.claude-island/hook.py"'
_USER_CMD = '"some-tool" "/etc/foo"'


# ---------------------------------------------------------------------------
# build_hook_command
# ---------------------------------------------------------------------------


def test_build_hook_command_quotes_paths(tmp_path):
    cmd = build_hook_command(
        python_exe=r"C:\Python\python.exe",
        hook_script=Path(r"C:\Users\u\.claude-island\hook.py"),
    )
    assert cmd.startswith('"')
    assert "python.exe" in cmd
    assert "hook.py" in cmd
    # Two double-quoted tokens (path + path)
    assert cmd.count('"') == 4


def test_build_hook_command_handles_unix_paths():
    """On Unix, paths stay as forward-slash strings. On Windows, Path
    auto-normalizes to backslashes — we check the cross-platform invariant:
    each path appears double-quoted as a whole token."""
    cmd = build_hook_command(
        python_exe="/usr/bin/python3",
        hook_script=Path("/home/user/.claude-island/hook.py"),
    )
    assert '"/usr/bin/python3"' in cmd
    # Path string normalization is platform-specific; just check the
    # last-segment substring survives in some form.
    assert "hook.py" in cmd
    assert "claude-island" in cmd


def test_build_hook_command_escapes_embedded_quotes():
    cmd = build_hook_command(
        python_exe='C:/weird path with "quote"/python.exe',
        hook_script=Path("/normal/hook.py"),
    )
    # Embedded quote escaped as \"
    assert '\\"' in cmd


# ---------------------------------------------------------------------------
# T4.1 — settings.json doesn't exist → create with our hooks
# ---------------------------------------------------------------------------


def test_t4_1_fresh_install_creates_file(tmp_path):
    settings = tmp_path / "settings.json"
    assert not settings.exists()

    result = install_if_needed(
        settings_path=settings,
        hook_command=_OUR_CMD,
    )
    assert result.changed is True
    assert len(result.installed_events) == len(HOOK_EVENTS_TO_INSTALL)
    assert result.user_hooks_preserved == 0

    data = json.loads(settings.read_text())
    assert "hooks" in data
    for spec in HOOK_EVENTS_TO_INSTALL:
        assert spec.name in data["hooks"]
        groups = data["hooks"][spec.name]
        assert len(groups) == 1
        assert groups[0]["hooks"][0]["command"] == _OUR_CMD


# ---------------------------------------------------------------------------
# T4.2 — preserve user-authored hooks
# ---------------------------------------------------------------------------


def test_t4_2_preserves_user_hooks(tmp_path):
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({
        "hooks": {
            "SessionStart": [
                {"hooks": [{"type": "command", "command": _USER_CMD}]},
            ],
            "PreToolUse": [
                {"matcher": "Bash", "hooks": [
                    {"type": "command", "command": _USER_CMD},
                ]},
            ],
        },
        "otherStuff": "kept",
    }))

    result = install_if_needed(settings_path=settings, hook_command=_OUR_CMD)
    assert result.changed is True
    assert result.user_hooks_preserved == 2

    data = json.loads(settings.read_text())
    # User key preserved
    assert data["otherStuff"] == "kept"
    # SessionStart now has user's group + ours
    ss_groups = data["hooks"]["SessionStart"]
    assert len(ss_groups) == 2
    commands = [g["hooks"][0]["command"] for g in ss_groups]
    assert _USER_CMD in commands
    assert _OUR_CMD in commands
    # PreToolUse user's matcher="Bash" stays + ours matcher="*" added
    pt_groups = data["hooks"]["PreToolUse"]
    user_groups = [g for g in pt_groups if g["hooks"][0]["command"] == _USER_CMD]
    ours_groups = [g for g in pt_groups if g["hooks"][0]["command"] == _OUR_CMD]
    assert user_groups[0]["matcher"] == "Bash"
    assert ours_groups[0]["matcher"] == "*"


# ---------------------------------------------------------------------------
# T4.3 — idempotent re-run
# ---------------------------------------------------------------------------


def test_t4_3_install_idempotent(tmp_path):
    settings = tmp_path / "settings.json"

    r1 = install_if_needed(settings_path=settings, hook_command=_OUR_CMD)
    r2 = install_if_needed(settings_path=settings, hook_command=_OUR_CMD)

    assert r1.changed is True
    assert r2.changed is False
    assert r2.installed_events == ()


# ---------------------------------------------------------------------------
# T4.3b — install with different command replaces our entries
# ---------------------------------------------------------------------------


def test_install_with_new_command_replaces_old_managed_entry(tmp_path):
    """When the user upgrades Python and the absolute path changes, the
    new install should REPLACE old managed entries, not stack."""
    settings = tmp_path / "settings.json"

    old_cmd = '"C:/old-python.exe" "C:/Users/u/.claude-island/hook.py"'
    new_cmd = '"C:/new-python.exe" "C:/Users/u/.claude-island/hook.py"'

    install_if_needed(settings_path=settings, hook_command=old_cmd)
    install_if_needed(settings_path=settings, hook_command=new_cmd)

    data = json.loads(settings.read_text())
    for spec in HOOK_EVENTS_TO_INSTALL:
        groups = data["hooks"][spec.name]
        commands = [g["hooks"][0]["command"] for g in groups]
        # Old gone, new present, no duplicates
        assert old_cmd not in commands
        assert commands.count(new_cmd) == 1


# ---------------------------------------------------------------------------
# T4.5 — malformed settings.json → raise InstallError
# ---------------------------------------------------------------------------


def test_t4_5_malformed_json_raises(tmp_path):
    settings = tmp_path / "settings.json"
    settings.write_text("not { valid json")

    with pytest.raises(InstallError):
        install_if_needed(settings_path=settings, hook_command=_OUR_CMD)


def test_root_not_object_raises(tmp_path):
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps([1, 2, 3]))

    with pytest.raises(InstallError):
        install_if_needed(settings_path=settings, hook_command=_OUR_CMD)


def test_empty_file_treated_as_empty_settings(tmp_path):
    settings = tmp_path / "settings.json"
    settings.write_text("")
    result = install_if_needed(settings_path=settings, hook_command=_OUR_CMD)
    assert result.changed is True
    data = json.loads(settings.read_text())
    assert "hooks" in data


# ---------------------------------------------------------------------------
# T4.6 — atomic write: tmp file used
# ---------------------------------------------------------------------------


def test_t4_6_atomic_write_uses_tmp(tmp_path, monkeypatch):
    """Patch os.replace to fail and verify the original file is intact."""
    settings = tmp_path / "settings.json"
    original = json.dumps({"hooks": {}, "preserved": True})
    settings.write_text(original)

    import os
    real_replace = os.replace
    called: list = []

    def failing_replace(src, dst):
        called.append((src, dst))
        raise OSError("simulated I/O failure")

    monkeypatch.setattr(os, "replace", failing_replace)

    with pytest.raises(OSError):
        install_if_needed(settings_path=settings, hook_command=_OUR_CMD)

    # Original content NOT touched
    assert settings.read_text() == original
    # Replace was called (proves we used the tmp+replace pattern)
    assert called


# ---------------------------------------------------------------------------
# T4.7 — paths with spaces handled
# ---------------------------------------------------------------------------


def test_t4_7_space_in_path_handled(tmp_path):
    cmd = build_hook_command(
        python_exe=r"C:\Program Files\Python\python.exe",
        hook_script=Path(r"C:\Users\With Space\.claude-island\hook.py"),
    )
    assert '"C:\\Program Files\\Python\\python.exe"' in cmd
    settings = tmp_path / "settings.json"
    install_if_needed(settings_path=settings, hook_command=cmd)
    # Round-trip via JSON without error
    data = json.loads(settings.read_text())
    saved = data["hooks"]["SessionStart"][0]["hooks"][0]["command"]
    assert saved == cmd


# ---------------------------------------------------------------------------
# T4.8 — _is_our_hook detector by substring
# ---------------------------------------------------------------------------


def test_t4_8_is_our_hook_detects_substring():
    assert _is_our_hook({"command": '"py" "/home/u/.claude-island/hook.py"'}) is True
    assert _is_our_hook({"command": '"py" "C:\\u\\.claude-island\\hook.py"'}) is True
    assert _is_our_hook({"command": "some-other-tool"}) is False
    assert _is_our_hook({"command": None}) is False
    assert _is_our_hook({}) is False


# ---------------------------------------------------------------------------
# _strip_our_groups behaviour
# ---------------------------------------------------------------------------


def test_strip_drops_pure_our_group():
    groups = [{"hooks": [{"type": "command", "command": _OUR_CMD}]}]
    kept, removed = _strip_our_groups(groups)
    assert kept == []
    assert removed == 1


def test_strip_filters_inside_mixed_group():
    groups = [{"hooks": [
        {"type": "command", "command": _USER_CMD},
        {"type": "command", "command": _OUR_CMD},
    ]}]
    kept, removed = _strip_our_groups(groups)
    assert len(kept) == 1
    assert kept[0]["hooks"] == [
        {"type": "command", "command": _USER_CMD},
    ]
    assert removed == 1


def test_strip_keeps_pure_user_group():
    groups = [{"hooks": [{"type": "command", "command": _USER_CMD}]}]
    kept, removed = _strip_our_groups(groups)
    assert kept == groups
    assert removed == 0


# ---------------------------------------------------------------------------
# is_installed predicate
# ---------------------------------------------------------------------------


def test_is_installed_after_fresh_install(tmp_path):
    settings = tmp_path / "settings.json"
    install_if_needed(settings_path=settings, hook_command=_OUR_CMD)
    assert is_installed(settings_path=settings, hook_command=_OUR_CMD) is True


def test_is_installed_false_when_missing(tmp_path):
    settings = tmp_path / "settings.json"
    assert is_installed(settings_path=settings, hook_command=_OUR_CMD) is False


def test_is_installed_false_when_partial(tmp_path):
    """Only some events have our hook → is_installed returns False."""
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({
        "hooks": {
            "SessionStart": [
                {"hooks": [{"type": "command", "command": _OUR_CMD}]},
            ],
        },
    }))
    assert is_installed(settings_path=settings, hook_command=_OUR_CMD) is False


def test_is_installed_false_when_different_command(tmp_path):
    settings = tmp_path / "settings.json"
    install_if_needed(settings_path=settings, hook_command=_OUR_CMD)
    # Same file, but check with a DIFFERENT command — should be False
    other = '"D:/other.exe" "D:/other/.claude-island/hook.py"'
    assert is_installed(settings_path=settings, hook_command=other) is False


# ---------------------------------------------------------------------------
# T10.x — sync_hook_script tests
# ---------------------------------------------------------------------------


def _bundled_script(tmp_path: Path, version: str) -> Path:
    """Create a fake bundled hook.py with the given __version__."""
    path = tmp_path / "bundled.py"
    path.write_text(f'__version__ = "{version}"\nprint("hello")\n', encoding="utf-8")
    return path


def test_t10_1_dest_missing_copies(tmp_path):
    bundled = _bundled_script(tmp_path, "v1")
    dest = tmp_path / ".claude-island" / "hook.py"
    assert not dest.exists()

    wrote = sync_hook_script(bundled_script=bundled, dest=dest)
    assert wrote is True
    assert dest.exists()
    assert dest.read_text() == bundled.read_text()


def test_t10_2_same_version_no_op(tmp_path):
    bundled = _bundled_script(tmp_path, "v1")
    dest = tmp_path / "hook.py"
    sync_hook_script(bundled_script=bundled, dest=dest)
    # Add some marker to dest after first sync — second sync shouldn't touch it
    # (well, it WOULD, because we copy bytes-for-bytes. The no-op is "we don't
    # rewrite when version matches"; verify by checking timestamp doesn't change.)
    mtime_before = dest.stat().st_mtime_ns
    import time
    time.sleep(0.05)  # ensure mtime resolution clears
    wrote = sync_hook_script(bundled_script=bundled, dest=dest)
    assert wrote is False
    assert dest.stat().st_mtime_ns == mtime_before


def test_t10_3_different_version_overwrites(tmp_path):
    bundled_v1 = _bundled_script(tmp_path, "v1")
    bundled_v2_path = tmp_path / "bundled_v2.py"
    bundled_v2_path.write_text('__version__ = "v2"\nprint("hi v2")\n')
    dest = tmp_path / "hook.py"

    sync_hook_script(bundled_script=bundled_v1, dest=dest)
    wrote = sync_hook_script(bundled_script=bundled_v2_path, dest=dest)
    assert wrote is True
    assert 'v2' in dest.read_text()


def test_sync_creates_parent_dir(tmp_path):
    bundled = _bundled_script(tmp_path, "v1")
    dest = tmp_path / "nested" / "deeper" / "hook.py"

    sync_hook_script(bundled_script=bundled, dest=dest)
    assert dest.exists()


def test_sync_corrupt_dest_overwrites(tmp_path):
    """If dest has no parseable __version__, we overwrite (safer)."""
    bundled = _bundled_script(tmp_path, "v1")
    dest = tmp_path / "hook.py"
    dest.write_text("# corrupt content with no version")
    wrote = sync_hook_script(bundled_script=bundled, dest=dest)
    assert wrote is True
    assert dest.read_text() == bundled.read_text()


# ---------------------------------------------------------------------------
# Integration: bundled real hook.py syncs correctly
# ---------------------------------------------------------------------------


def test_real_bundled_hook_py_can_sync(tmp_path):
    """The real bundled claude_island/hook.py must have a parseable
    __version__ — otherwise sync_hook_script would loop overwriting it
    every boot (because _read_hook_version returns None → version
    mismatch → write)."""
    from claude_island import hook as hook_module
    bundled_path = Path(hook_module.__file__)
    dest = tmp_path / "hook.py"

    wrote1 = sync_hook_script(bundled_script=bundled_path, dest=dest)
    assert wrote1 is True
    # Second call should be no-op (versions match)
    wrote2 = sync_hook_script(bundled_script=bundled_path, dest=dest)
    assert wrote2 is False, "sync_hook_script must be idempotent on no version change"
