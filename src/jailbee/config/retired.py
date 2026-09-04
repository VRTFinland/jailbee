"""Retired and removed config key checks.

Raised early, on the raw dict, so users get a clear "moved to" or "removed"
message instead of a confusing `extra="forbid"` validation error from
Pydantic once the key reaches a model.
"""

from __future__ import annotations

from pathlib import Path

from jailbee.config.errors import ConfigError

_RETIRED_KEYS_TOP_LEVEL: dict[str, str] = {
    "ide": "jetbrains.ide",
    "chrome_url": "chrome.url",
    "seed_ssh_from_host": "ssh.seed_from_host",
    "jetbrains_userprefs_from_host": "jetbrains.userprefs_from_host",
}

_RETIRED_KEYS_AUTOSTART: dict[str, str] = {
    "open_ide": "jetbrains.ide + jetbrains.autostart",
    "open_chrome": "chrome.autostart",
    "chrome_dark_mode": "chrome.dark_mode",
}

# Renamed with the project itself. Accepted as a validation alias with a
# deprecation warning through 1.0.x; retired in 1.1.0.
_RETIRED_KEYS_CLAUDE: dict[str, str] = {
    "install_gie_skills": "claude.install_jailbee_skills",
}

# Removed-without-replacement keys. Surface the same ConfigError as the
# moved-key maps above, but with a human-readable reason instead of a
# new location.
_CLAUDE_SEED_REMOVED_MSG = (
    "claude.seed_from_host has been removed — jailbee no longer seeds "
    "~/.claude from the host. The container starts with an empty "
    "<shared_dir>/claude and Claude Code runs its onboarding flow on "
    "first launch. Remove this key from your config."
)
_REMOVED_KEYS_TOP_LEVEL: dict[str, str] = {
    "seed_claude_from_host": _CLAUDE_SEED_REMOVED_MSG,
}
_REMOVED_KEYS_CLAUDE: dict[str, str] = {
    "seed_from_host": _CLAUDE_SEED_REMOVED_MSG,
}


def _check_retired_keys(raw: dict[str, object]) -> None:
    """Raise ConfigError if YAML contains keys retired in the host-tooling
    config restructure. Names the new location in the error message.
    """
    for old, new in _RETIRED_KEYS_TOP_LEVEL.items():
        if old in raw:
            raise ConfigError(
                f"Unknown field `{old}` in config: moved to `{new}`. "
                f"See docs/config.md for the new schema."
            )
    for old, reason in _REMOVED_KEYS_TOP_LEVEL.items():
        if old in raw:
            raise ConfigError(reason)
    autostart = raw.get("autostart", {})
    if isinstance(autostart, dict):
        for old, new in _RETIRED_KEYS_AUTOSTART.items():
            if old in autostart:
                raise ConfigError(
                    f"Unknown field `autostart.{old}` in config: moved to "
                    f"`{new}`. See docs/config.md for the new schema."
                )
    # Checked under both spellings: the legacy top-level `claude:` block and
    # its `agents.claude` successor. A user who has already migrated to
    # `agents.claude` still deserves the same retired-key error, not a
    # confusing "unknown field" from Pydantic's `extra="forbid"`.
    claude_blocks: list[tuple[str, object]] = [("claude", raw.get("claude", {}))]
    agents = raw.get("agents", {})
    if isinstance(agents, dict):
        claude_blocks.append(("agents.claude", agents.get("claude", {})))
    for label, claude in claude_blocks:
        if not isinstance(claude, dict):
            continue
        for old, reason in _REMOVED_KEYS_CLAUDE.items():
            if old in claude:
                raise ConfigError(reason)
        for old, new in _RETIRED_KEYS_CLAUDE.items():
            if old in claude:
                raise ConfigError(
                    f"Unknown field `{label}.{old}` in config: renamed to "
                    f"`{new}`. See docs/config.md for the new schema."
                )


def _check_pull_migration(
    global_raw: dict[str, object],
    repo_raw: dict[str, object],
    global_path: Path,
    repo_path: Path,
) -> None:
    """Raise ConfigError if either layer still uses the legacy `merge:` key.

    The block was renamed when `jailbee git merge` became `jailbee git pull`.
    Reports every file that carries the legacy key in a single message.
    """
    legacy_paths: list[Path] = []
    if "merge" in global_raw:
        legacy_paths.append(global_path)
    if "merge" in repo_raw:
        legacy_paths.append(repo_path)
    if not legacy_paths:
        return
    paths_listed = "\n  ".join(str(p) for p in legacy_paths)
    raise ConfigError(
        f"Config key 'merge:' was renamed to 'pull:' "
        f"(the 'jailbee git merge' command was renamed to 'jailbee git pull'). "
        f"Update:\n  {paths_listed}"
    )


def _check_agents_spelling(
    global_raw: dict[str, object],
    repo_raw: dict[str, object],
    global_path: Path,
    repo_path: Path,
) -> None:
    """Raise ConfigError if the legacy `claude:` and `agents.claude` spellings
    are both in play across the two layers.

    `resolve_agents_raw` catches the same conflict on the merged dict, but by
    then the layers are indistinguishable and the surrounding message in
    `_build_config_from_dict` can only name the repo config. The likely shape
    of this conflict for an existing user is a `claude:` block left in
    `global.yaml` (what the old template wrote) meeting an `agents.claude` in
    a repo config — so the file the user must edit is precisely the one that
    message would *not* name. Report every file that carries either spelling,
    the way `_check_pull_migration` does for the renamed `merge:` key.
    """

    def _has_agents_claude(raw: dict[str, object]) -> bool:
        agents = raw.get("agents")
        return isinstance(agents, dict) and "claude" in agents

    legacy = [p for raw, p in ((global_raw, global_path), (repo_raw, repo_path)) if "claude" in raw]
    modern = [
        p
        for raw, p in ((global_raw, global_path), (repo_raw, repo_path))
        if _has_agents_claude(raw)
    ]
    if not legacy or not modern:
        return
    listed = "\n  ".join(f"{p} ({label})" for p, label in _label_spellings(legacy, modern))
    raise ConfigError(
        "Config defines both the legacy `claude:` block and `agents.claude` — "
        "keep one. `agents.claude` is the preferred spelling; the `claude:` "
        f"block is a supported legacy alias. Files involved:\n  {listed}"
    )


def _label_spellings(legacy: list[Path], modern: list[Path]) -> list[tuple[Path, str]]:
    """`(path, "claude:" / "agents.claude" / both)` for each file involved,
    in `legacy`-then-`modern` file order without repeating a path."""
    labels: dict[Path, list[str]] = {}
    for path in legacy:
        labels.setdefault(path, []).append("claude:")
    for path in modern:
        labels.setdefault(path, []).append("agents.claude")
    return [(path, " and ".join(spellings)) for path, spellings in labels.items()]
