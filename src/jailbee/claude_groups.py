"""Which Claude credential group applies to a repo, and to one container.

Two sources feed a container's credential, and this module is the only
place that knows both:

1. ``global.yaml``'s ``claude_credentials`` — the repo's permanent group,
   resolved onto ``Config.claude_credentials_dir`` at load time.
2. The container's ``user.jailbee.claude_group`` label — a temporary
   override for the length of that container's life.

The container wins. Unlike ``egress_scope``'s three sources these are
*replacing*, not additive: a group is one value.

The override lives in a label rather than the database for the reason
``egress_scope`` records for ``user.jailbee.egress_extra``: it dies with
the container, so there are no orphan rows to clean up, a recreated
same-named container cannot inherit the previous one's group, and it
survives a wiped ``state.sqlite``.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from jailbee.config import _CREDENTIAL_GROUP_RE

if TYPE_CHECKING:
    from jailbee.config import Config
    from jailbee.incus import Incus

GROUP_LABEL = "user.jailbee.claude_group"
"""Container label naming this container's credential group."""

NO_GROUP = "_none"
"""Label value meaning "this container shares no group".

Not ``none``: ``_CREDENTIAL_GROUP_RE`` accepts ``none`` as a group name,
so it would collide. A leading underscore never can — the same property
``claude_pool.store_dir`` relies on for ``_parked``. The empty string is
unusable too, because ``Incus.config_get`` returns ``None`` for it and
that is indistinguishable from an absent label.
"""

RESERVED_GROUP_NAMES = frozenset({"none"})
"""Names the CLI refuses to write, because it spells "no group" that way.

Enforced only in the writing path, never in ``ClaudeCredentials``'s field
validators: a host whose ``global.yaml`` already names a group ``none``
must keep loading. ``jailbee doctor`` reports such a group instead.
"""


class GroupError(Exception):
    """A group operation cannot proceed; the message is user-facing."""


@dataclass(frozen=True)
class Override:
    """A container's own group setting. ``group is None`` means "no group"."""

    group: str | None


def validate_group_name(name: str) -> str:
    """`name`, or `GroupError` naming what is wrong with it."""
    if name in RESERVED_GROUP_NAMES:
        raise GroupError(
            f"`{name}` is a reserved word: jailbee spells 'no credential group' "
            f"as `{name}` on the command line, so it cannot also be a group. "
            "Pick another name."
        )
    if not _CREDENTIAL_GROUP_RE.match(name):
        raise GroupError(
            f"invalid credential group name {name!r}: must match "
            "[a-z0-9][a-z0-9-]* — lowercase letters, digits and hyphens, "
            "starting with a letter or digit."
        )
    return name


def group_dir(name: str) -> Path:
    """The credential directory for a group.

    Deliberately identical to `ClaudeCredentials.dir_for`'s construction.
    The two must agree, or a container override would mount a directory
    `jailbee apply` never creates.
    """
    from jailbee.paths import xdg_data_home

    return xdg_data_home() / "jailbee" / "claude-credentials" / name


def repo_group(cfg: Config) -> str | None:
    """The group this repo resolves to from `global.yaml`, or None."""
    from jailbee.claude_pool import group_name

    return group_name(cfg)


def container_override(incus: Incus, container: str) -> Override | None:
    """The container's own group setting, or None when it inherits.

    A label that is neither `NO_GROUP` nor a valid group name is ignored
    and warned about, exactly as `egress_scope.container_extras` treats a
    malformed label. Ignoring rather than raising matters here for a
    second reason: the label becomes a path component, so a hand-edited
    `../../etc` must never reach `group_dir`.
    """
    raw = incus.config_get(container, GROUP_LABEL)
    if not raw:
        return None
    if raw == NO_GROUP:
        return Override(None)
    if not _CREDENTIAL_GROUP_RE.match(raw):
        from jailbee.tui import warn

        warn(
            f"Ignoring {GROUP_LABEL} on '{container}' — {raw!r} is not a valid "
            "group name. Re-set it with `jailbee claude group use <name> "
            f"{container}`."
        )
        return None
    return Override(raw)


def effective_group(cfg: Config, incus: Incus, container: str) -> str | None:
    """The group whose credential `container` reads, or None for no group."""
    override = container_override(incus, container)
    if override is not None:
        return override.group
    return repo_group(cfg)


def ensure_group_dir(name: str) -> Path:
    """Create a group's credential directory at 0700 and return it.

    Incus rejects a disk device whose source path does not exist, so this
    runs before any device is attached. 0700 because the directory holds a
    live credential and lives outside every repo — the same mode
    `init_command._ensure_claude_credentials_dir` uses.
    """
    target = group_dir(validate_group_name(name))
    target.mkdir(parents=True, exist_ok=True)
    target.chmod(0o700)
    return target


def _creds_env_key() -> str:
    return "environment.CLAUDE_SECURESTORAGE_CONFIG_DIR"


def _creds_mount_path() -> str:
    from jailbee.config import CONTAINER_USERNAME
    from jailbee.profiles import CLAUDE_CREDS_DIRNAME

    return f"/home/{CONTAINER_USERNAME}/{CLAUDE_CREDS_DIRNAME}"


def _config_home_path() -> str:
    from jailbee.config import CONTAINER_USERNAME

    return f"/home/{CONTAINER_USERNAME}/.claude"


def _profile_has_creds_device(cfg: Config) -> bool:
    """Whether `<prefix>-binds` carries the shared-credential device.

    `profiles.py` renders it only when the repo itself resolves a group,
    and `config_device_override` fails when there is nothing to override
    (`incus.py:504`). Derived from the config rather than read back from
    Incus so it cannot disagree with what the next `jailbee apply` writes.
    """
    return cfg.claude.enabled and cfg.claude_credentials_dir is not None


def set_container_group(
    cfg: Config,
    incus: Incus,
    container: str,
    group: str | None,
) -> None:
    """Point one container at `group`, or at no group when `group is None`.

    Three instance-level writes, all of which outrank the profile, so a
    later `jailbee apply` may re-render `<prefix>-binds` freely without
    disturbing the override.

    The environment key is written **always**, not only when the repo has
    no group of its own: `profiles.claude_securestorage_dir_env` returns
    None for a group-less repo, so the profile carries no such key, and if
    the repo's group is later removed the profile would drop the key out
    from under a still-overridden container.
    """
    from jailbee.profiles import CLAUDE_CREDS_DEVICE

    if group is None:
        incus.config_device_remove(container, CLAUDE_CREDS_DEVICE, missing_ok=True)
        incus.config_set(container, _creds_env_key(), _config_home_path())
        incus.config_set(container, GROUP_LABEL, NO_GROUP)
        return

    source = str(ensure_group_dir(group))
    if _profile_has_creds_device(cfg):
        incus.config_device_override(container, CLAUDE_CREDS_DEVICE, {"source": source})
    else:
        incus.config_device_add(
            container,
            CLAUDE_CREDS_DEVICE,
            "disk",
            {"source": source, "path": _creds_mount_path()},
        )
    incus.config_set(container, _creds_env_key(), _creds_mount_path())
    incus.config_set(container, GROUP_LABEL, group)


def clear_container_group(incus: Incus, container: str) -> None:
    """Drop the override so the container inherits the repo's group again."""
    from jailbee.profiles import CLAUDE_CREDS_DEVICE

    incus.config_device_remove(container, CLAUDE_CREDS_DEVICE, missing_ok=True)
    incus.config_unset(container, _creds_env_key())
    incus.config_unset(container, GROUP_LABEL)
