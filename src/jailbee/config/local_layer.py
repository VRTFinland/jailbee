"""Read-only rules for the host-local per-repo config layer.

Files live at `<config dir>/repos/<container_prefix>.yaml`; writes belong to
`config_writer.patch_local_file`.
"""

from __future__ import annotations

import stat
from dataclasses import dataclass
from typing import TYPE_CHECKING

import yaml
from pydantic import ValidationError

from jailbee.config.common import _HOST_LEVEL_KEYS, _read_yaml_or_empty
from jailbee.config.errors import ConfigError
from jailbee.config.models_host import _PREFIX_RE
from jailbee.config.models_litellm import (
    REPO_REFUSED_KEYS,
    LiteLLMConfig,
    LiteLLMRepoOverlay,
    LiteLLMRepoView,
    input_free_lines,
)
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

    host_only = (
        _HOST_LEVEL_KEYS
        - set(Config.model_fields)
        - {
            "credentials",
            "claude_credentials",
            "litellm",
        }
    )
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
    overlay = {k: v for k, v in raw.items() if k not in ("credentials", "litellm")}
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
    local_litellm_overlay(raw, origin)
    try:
        Config.model_validate(resolve_browsers_raw(resolve_agents_raw(overlay)))
    except (ValidationError, ConfigError) as e:
        raise ConfigError(f"Config validation failed in {origin}:\n{e}") from e


def local_litellm_overlay(raw: dict[str, object], origin: str) -> LiteLLMRepoOverlay | None:
    """The layer's validated `litellm:` block; None when it has none.

    Validation errors are input-free and unchained: a key pasted into
    `api_key` must not come back in the message or a traceback's cause.
    """
    block = raw.get("litellm")
    if block is None:
        return None
    if isinstance(block, dict):
        refused = [k for k in REPO_REFUSED_KEYS if k in block]
        if refused:
            listed = ", ".join(f"`litellm.{k}`" for k in refused)
            raise ConfigError(
                f"{listed} not allowed in {origin}: the proxy and its logins are shared by "
                "every repo on this host; set it in global.yaml."
            )
    try:
        return LiteLLMRepoOverlay.model_validate(block)
    except ValidationError as e:
        raise ConfigError(
            f"Invalid `litellm` in {origin}:\n{input_free_lines(e, ('litellm',))}"
        ) from None


def repo_litellm_view(
    host: LiteLLMConfig, prefix: str, overlay: LiteLLMRepoOverlay | None, origin: str
) -> LiteLLMRepoView:
    """What `claude-jb` uses in this repo's containers (spec 4.5)."""
    if overlay is None:
        return LiteLLMRepoView(config=host)
    try:
        merged = host.with_overlay(overlay)
    except ValidationError as e:
        raise ConfigError(
            f"`litellm` in {origin} does not fit the `litellm` block in global.yaml:\n"
            f"{input_free_lines(e, ('litellm',))}"
        ) from None
    own = bool(overlay.routes or overlay.profiles)
    return LiteLLMRepoView(config=merged, scope=prefix if own else None, origin=origin)


@dataclass(frozen=True)
class LocalLiteLLMView:
    prefix: str
    view: LiteLLMRepoView


def all_local_litellm_views(host: LiteLLMConfig) -> tuple[list[LocalLiteLLMView], list[str]]:
    """Every repo override on this host, merged over `host`; broken ones reported, not raised.

    The proxy serves every repo, so one repo's broken file must not stop it;
    that repo's own commands fail at load time, naming the same file.
    """
    root = local_config_dir()
    if not root.is_dir():
        return [], []
    views: list[LocalLiteLLMView] = []
    issues: list[str] = []
    for path in sorted(root.glob("*.yaml")):
        prefix = path.stem
        if not _PREFIX_RE.match(prefix):
            continue
        try:
            raw = _read_yaml_or_empty(path)
        except ConfigError as e:
            # No `litellm` block was read, so no override is lost by name. A
            # YAML error's own text quotes the offending line, which may
            # hold a token: report the line number only.
            cause = e.__cause__
            if isinstance(cause, yaml.YAMLError):
                mark = getattr(cause, "problem_mark", None)
                where = f" (line {mark.line + 1})" if mark is not None else ""
                issues.append(f"{path} is not valid YAML{where}; skipped")
            else:
                issues.append(f"{e}; skipped")
            continue
        try:
            overlay = local_litellm_overlay(raw, str(path))
            if overlay is None:
                continue
            views.append(
                LocalLiteLLMView(prefix, repo_litellm_view(host, prefix, overlay, str(path)))
            )
        except ConfigError as e:
            issues.append(
                f"{e}\nSkipped: `claude-jb` in {prefix}'s containers cannot use its "
                "override until this is fixed."
            )
    return views, issues


def local_litellm_scopes(host: LiteLLMConfig) -> tuple[dict[str, LiteLLMConfig], list[str]]:
    """Repo prefix -> merged config, for repos the proxy serves under their own aliases."""
    views, issues = all_local_litellm_views(host)
    return {v.prefix: v.view.config for v in views if v.view.scope is not None}, issues


def scope_files(scopes: dict[str, LiteLLMConfig]) -> list[str]:
    """The repo override file behind each scope, in `scopes` order (for error messages)."""
    return [str(local_config_path(prefix)) for prefix in scopes]


def token_perms_warning(path: Path, overlay: dict[str, object]) -> str | None:
    """A warning when a local config file carrying `github.token` is not private.

    A warning, not an error: an error here made `load_repo_config` fail, and the
    dashboard then listed the repo's containers as orphans.
    """
    github = overlay.get("github")
    if not (isinstance(github, dict) and github.get("token")) or not path.exists():
        return None
    mode = stat.S_IMODE(path.stat().st_mode)
    if mode & 0o077:
        return (
            f"{path} contains github.token but has insecure perms (0{mode:03o}). "
            f"Run `chmod 600 {path}`."
        )
    return None


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
