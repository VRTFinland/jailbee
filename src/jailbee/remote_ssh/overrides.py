"""CLI-only overrides of `remote.ssh` for `jb remote ssh serve`.

These exist purely so an operator can try a different SSH policy without
editing `global.yaml`: nothing here is ever written back to disk, and the
systemd unit never passes any of it (see
`templates/systemd/jailbee-ssh.service`). `apply_ssh_overrides` merges a
`ServeOverrides` onto a loaded `RemoteSSHConfig` and revalidates the result
through the very same pydantic model `global.yaml` uses, so an invalid
combination (e.g. `--shell` with `commands.mode: disabled`, or an
`--allow` leaf outside the current public CLI) is rejected exactly like a
bad config file would be.
"""

from __future__ import annotations

from dataclasses import dataclass

from pydantic import ValidationError

from jailbee.config import ConfigError
from jailbee.config.models_remote import CommandMode, RemoteSSHConfig


@dataclass(frozen=True)
class ServeOverrides:
    """One `jb remote ssh serve` invocation's command-line overrides.

    Every field defaults to `None`, meaning "not given" — the loaded
    `remote.ssh` config wins for that field. `allow`, when given at least
    once (even as a single `--allow`), REPLACES `commands.allow` entirely;
    it never appends to the configured list.
    """

    listen: str | None = None
    port: int | None = None
    dashboard: bool | None = None
    shell: bool | None = None
    exec: bool | None = None
    commands_mode: CommandMode | None = None
    allow: list[str] | None = None

    def is_empty(self) -> bool:
        """True when no flag was given at all; overrides are then a no-op."""
        return (
            self.listen is None
            and self.port is None
            and self.dashboard is None
            and self.shell is None
            and self.exec is None
            and self.commands_mode is None
            and self.allow is None
        )


def apply_ssh_overrides(config: RemoteSSHConfig, overrides: ServeOverrides) -> RemoteSSHConfig:
    """Merge `overrides` onto `config` and revalidate as a `RemoteSSHConfig`.

    Returns `config` itself, unchanged, when `overrides` is empty — so a
    plain `jb remote ssh serve` with no flags behaves identically to before
    this existed. A given field always wins over `config`; an ungiven one
    keeps following `config`.

    Validation goes through `RemoteSSHConfig.model_validate` on a merged
    dict, never a bare `model_copy(update=...)`, which would skip the
    model's cross-field validators (e.g. `shell`/`exec` requiring an
    enabled `commands.mode`). An `--allow` leaf is additionally checked
    against `known_command_paths()`, mirroring the check
    `global_config.validate_global_raw` applies to `global.yaml` itself.
    """
    if overrides.is_empty():
        return config

    merged = config.model_dump()
    if overrides.listen is not None:
        merged["listen"] = overrides.listen
    if overrides.port is not None:
        merged["port"] = overrides.port
    if overrides.dashboard is not None:
        merged["dashboard"] = overrides.dashboard
    if overrides.shell is not None:
        merged["shell"] = overrides.shell
    if overrides.exec is not None:
        merged["exec"] = overrides.exec
    if overrides.commands_mode is not None or overrides.allow is not None:
        commands = dict(merged["commands"])
        if overrides.commands_mode is not None:
            commands["mode"] = overrides.commands_mode
        if overrides.allow is not None:
            commands["allow"] = list(overrides.allow)
        merged["commands"] = commands

    try:
        validated = RemoteSSHConfig.model_validate(merged)
    except ValidationError as exc:
        raise ConfigError(f"remote ssh serve overrides are invalid:\n{exc}") from exc

    if validated.commands.allow:
        from jailbee.remote_ssh.router import known_command_paths

        unknown = sorted(set(validated.commands.allow) - known_command_paths())
        if unknown:
            joined = ", ".join(unknown)
            raise ConfigError(
                "remote ssh serve overrides are invalid:\n"
                f"unknown remote Jailbee command path(s): {joined}"
            )
    return validated


def describe_overrides(overrides: ServeOverrides) -> str | None:
    """One-line summary of active overrides, or `None` when there are none.

    Used only for the `serve` startup summary, to make it obvious the
    running policy is not (only) what `global.yaml` says.
    """
    if overrides.is_empty():
        return None
    parts: list[str] = []
    if overrides.listen is not None:
        parts.append(f"listen={overrides.listen}")
    if overrides.port is not None:
        parts.append(f"port={overrides.port}")
    if overrides.dashboard is not None:
        parts.append(f"dashboard={'on' if overrides.dashboard else 'off'}")
    if overrides.shell is not None:
        parts.append(f"shell={'on' if overrides.shell else 'off'}")
    if overrides.exec is not None:
        parts.append(f"exec={'on' if overrides.exec else 'off'}")
    if overrides.commands_mode is not None and overrides.allow is not None:
        parts.append(f"commands={overrides.commands_mode} [{', '.join(overrides.allow)}]")
    elif overrides.commands_mode is not None:
        parts.append(f"commands={overrides.commands_mode}")
    elif overrides.allow is not None:
        parts.append(f"commands.allow=[{', '.join(overrides.allow)}]")
    return "overrides (not from global.yaml): " + ", ".join(parts)
