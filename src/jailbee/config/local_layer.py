"""Read-only rules for the host-local per-repo config layer.

Files live at `<config dir>/repos/<container_prefix>.yaml`; writes belong to
`config_writer.patch_local_file`.
"""

from __future__ import annotations

import stat
from typing import TYPE_CHECKING

from pydantic import ValidationError

from jailbee.config.common import _HOST_LEVEL_KEYS, _read_yaml_or_empty
from jailbee.config.errors import ConfigError
from jailbee.config.models_net import LocalCredentials

if TYPE_CHECKING:
    from pathlib import Path

LOCAL_DIR_NAME = "repos"


def local_config_dir() -> Path:
    """Return the directory holding local per-repo files."""
    from jailbee.global_config import default_global_config_path

    return default_global_config_path().parent / LOCAL_DIR_NAME


def local_config_path(prefix: str) -> Path:
    """Return the local file path for a validated container prefix."""
    return local_config_dir() / f"{prefix}.yaml"


def read_local_raw(prefix: str) -> dict[str, object]:
    """Read one local layer; missing, empty and null files are empty mappings."""
    return _read_yaml_or_empty(local_config_path(prefix))


_COMPUTED_KEYS = frozenset({"credential_group", "claude_credentials_dir"})


def _refused_keys() -> frozenset[str]:
    from jailbee.config.root import Config

    host_only = _HOST_LEVEL_KEYS - set(Config.model_fields) - {"credentials", "claude_credentials"}
    return frozenset({"container_prefix", *_COMPUTED_KEYS, *host_only})


def split_local_raw(
    raw: dict[str, object], origin: str
) -> tuple[dict[str, object], LocalCredentials | None]:
    """Validate layer-specific key rules and split credentials from overlay."""
    if "claude_credentials" in raw:
        raise ConfigError(
            f"`claude_credentials` is not allowed in {origin} — use `credentials:` "
            "(`credentials: {group: <name>}`)."
        )
    refused = sorted(k for k in raw if k in _refused_keys())
    if refused:
        listed = ", ".join(f"`{k}`" for k in refused)
        raise ConfigError(
            f"{listed} not allowed in {origin}: the host-local file overrides one repo's "
            "config, and these keys either name the file itself or describe the whole "
            "host (set them in global.yaml)."
        )
    github = raw.get("github")
    if isinstance(github, dict) and "api_tokens" in github:
        raise ConfigError(
            f"`github.api_tokens` is not allowed in {origin} — this file is already "
            "per-repo; set `github.token` instead."
        )
    overlay = {k: v for k, v in raw.items() if k != "credentials"}
    if "credentials" not in raw:
        return overlay, None
    try:
        creds = LocalCredentials.model_validate(raw["credentials"] or {})
    except ValidationError as e:
        raise ConfigError(f"Invalid `credentials` in {origin}:\n{e}") from e
    return overlay, creds


def validate_local_raw(raw: dict[str, object], origin: str) -> None:
    """Validate local-specific key rules and the repo Config schema."""
    from jailbee.config.loader import resolve_agents_raw, resolve_browsers_raw
    from jailbee.config.root import Config

    overlay, _ = split_local_raw(raw, origin)
    try:
        Config.model_validate(resolve_browsers_raw(resolve_agents_raw(overlay)))
    except (ValidationError, ConfigError) as e:
        raise ConfigError(f"Config validation failed in {origin}:\n{e}") from e


def check_token_perms(path: Path, overlay: dict[str, object]) -> None:
    """Require a local config file carrying `github.token` to be private."""
    github = overlay.get("github")
    if not (isinstance(github, dict) and github.get("token")) or not path.exists():
        return
    mode = stat.S_IMODE(path.stat().st_mode)
    if mode & 0o077:
        raise ConfigError(
            f"{path} contains github.token but has insecure perms (0{mode:03o}). "
            f"Run `chmod 600 {path}`."
        )


def local_credentials(prefix: str) -> LocalCredentials | None:
    """Read and return the credentials block for one local config file."""
    path = local_config_path(prefix)
    return split_local_raw(read_local_raw(prefix), str(path))[1]


def all_local_credential_groups() -> set[str]:
    """Return non-null groups in readable local files, skipping malformed files."""
    root = local_config_dir()
    if not root.is_dir():
        return set()
    groups: set[str] = set()
    for path in sorted(root.glob("*.yaml")):
        try:
            _, creds = split_local_raw(_read_yaml_or_empty(path), str(path))
        except ConfigError:
            continue
        if creds is not None and creds.group is not None:
            groups.add(creds.group)
    return groups
