"""Which credential group applies to a repo, and to one container.

Two sources feed a container's credential, and this module is the only
place that knows both:

1. ``global.yaml``'s ``credentials`` (or its legacy spelling
   ``claude_credentials``) — the repo's permanent group, resolved onto
   ``Config.credential_group`` at load time.
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

from collections.abc import Collection, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

from jailbee.config.models_net import _CREDENTIAL_GROUP_RE

if TYPE_CHECKING:
    from jailbee.config import Config
    from jailbee.global_config import GlobalConfig
    from jailbee.incus import Incus

GROUP_LABEL = "user.jailbee.claude_group"
"""Container label naming this container's credential group."""

NO_GROUP = "_none"
"""Label value meaning "this container shares no group".

Not ``none``: ``_CREDENTIAL_GROUP_RE`` accepts ``none`` as a group name,
so it would collide. A leading underscore never can — the same property
``accounts.engine.store_dir`` relies on for ``_parked``. The empty string is
unusable too, because ``Incus.config_get`` returns ``None`` for it and
that is indistinguishable from an absent label.
"""


class _Inherit:
    """Type of the `INHERIT` sentinel; exists so mypy can name it."""


INHERIT: Final = _Inherit()
"""Returned by `_label_group` when a container has no usable override.

Distinct from `None`, which is the *presence* of an override saying "no
group". Collapsing the two would make an unlabelled container in a
group-less repo indistinguishable from one deliberately opted out, and
`redundant_overrides` would then list every container of such a repo.
"""

RESERVED_GROUP_NAMES = frozenset({"none"})
"""Names the CLI refuses to write, because it spells "no group" that way.

Enforced only in the writing path, never in ``Credentials``'s field
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


def group_dir(agent: str, name: str) -> Path:
    """The credential directory one agent keeps for a group.

    Deliberately identical to `engine.group_dir`'s construction. The two must
    agree, or a container override would mount a directory `jailbee apply`
    never creates.
    """
    from jailbee.accounts.engine import group_dir as _dir

    return _dir(agent, name)


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
    from jailbee.accounts.engine import repo_group

    override = container_override(incus, container)
    if override is not None:
        return override.group
    return repo_group(cfg)


def ensure_group_dir(agent: str, name: str) -> Path:
    """Create a group's credential directory at 0700 and return it.

    Incus rejects a disk device whose source path does not exist, so this
    runs before any device is attached. 0700 because the directory holds a
    live credential and lives outside every repo — the same mode
    `ClaudeAdapter.prepare_config_home` uses.
    """
    target = group_dir(agent, validate_group_name(name))
    target.mkdir(parents=True, exist_ok=True)
    target.chmod(0o700)
    return target


def set_container_group(
    cfg: Config,
    incus: Incus,
    container: str,
    group: str | None,
) -> None:
    """Point one container at `group`, or at no group when `group is None`.

    The group is one value shared by every pooled agent, so each adapter wires
    its own instance-local device and env key, and the shared label is written
    **once, after** every adapter has been asked: a reader must never see a
    label naming a holder whose device is not mounted yet.

    Every write is instance-level, so it outranks the profile: a later
    `jailbee apply` may re-render `<prefix>-binds` freely without disturbing
    the override.

    `group is None` is an explicit "no group" override, not a clear. Each
    adapter is told to point the container back at its own config home — for
    Claude that means removing `claude-creds` and setting secure storage to
    `~/.claude`, which omitting the env value alone would not achieve. The
    label becomes `NO_GROUP`, so an unlabelled container still means "inherit".
    """
    from jailbee.accounts.adapters import base

    adapters = base.pooled_adapters(cfg)
    if group is None:
        for adapter in adapters:
            adapter.set_container_group(cfg, incus, container, None)
        incus.config_set(container, GROUP_LABEL, NO_GROUP)
        return

    name = validate_group_name(group)
    for adapter in adapters:
        adapter.set_container_group(cfg, incus, container, ensure_group_dir(adapter.name, name))
    incus.config_set(container, GROUP_LABEL, name)


def override_is_redundant(cfg: Config, group: str | None) -> bool:
    """Whether an override naming `group` only repeats the repo's own setting.

    Such an override is not a preference but leftover state, and leaving it
    in place is not harmless: the label outranks the profile, so the *next*
    change to the repo's group would silently leave that one container
    behind on the old one.

    A named group is redundant only when the profile really carries the same
    device. With ``claude.enabled: false`` it carries none (no pooled adapter
    claims it), so the label is the only thing mounting the credential and
    dropping it would change what the container reads. ``None`` — the
    explicit "no group" override — is redundant whenever the repo shares no
    group either: neither side then mounts anything, and the env key the
    label writes names the config home Claude Code defaults to.
    """
    from jailbee.accounts.adapters import base
    from jailbee.accounts.engine import repo_group

    if group != repo_group(cfg):
        return False
    adapters = base.pooled_adapters(cfg)
    return group is None or any(adapter.profile_has_group(cfg) for adapter in adapters)


def redundant_overrides(cfg: Config, incus: Incus) -> list[str]:
    """This repo's containers whose override only repeats the repo's group.

    Read from `incus.list_containers()`: a container appears here only when
    its override repeats the repo's group, and is left out when it deviates
    or carries no usable label at all.
    """
    out: list[str] = []
    for row in incus.list_containers():
        name = str(row.get("name", ""))
        if not name.startswith(f"{cfg.container_prefix}-"):
            continue
        label = _label_group(row.get("config") or {})
        if label is INHERIT:
            continue
        if override_is_redundant(cfg, label):  # type: ignore[arg-type] # narrowed by sentinel
            out.append(name)
    return sorted(out)


def clear_container_group(cfg: Config, incus: Incus, container: str) -> None:
    """Drop the override so the container inherits the repo's group again.

    Every pooled adapter is asked to remove its own instance-local wiring
    first; the shared label goes last, so no reader can see a label naming a
    holder whose device has already been torn down.
    """
    from jailbee.accounts.adapters import base

    for adapter in base.pooled_adapters(cfg):
        adapter.clear_container_group(cfg, incus, container)
    incus.config_unset(container, GROUP_LABEL)


def _label_group(raw_config: dict[str, str]) -> str | _Inherit | None:
    """Read the group out of an `incus list` payload's config dict."""
    raw = raw_config.get(GROUP_LABEL)
    if not raw:
        return INHERIT
    if raw == NO_GROUP:
        return None
    if not _CREDENTIAL_GROUP_RE.match(raw):
        return INHERIT
    return raw


def groups_by_prefix_from(
    gcfg: GlobalConfig,
    rows: Sequence[dict[str, Any]],
    prefixes: Collection[str],
) -> dict[str, set[str | None]]:
    """For each prefix, the set of groups its containers use.

    Takes the `incus list` payload rather than fetching it, so a caller
    answering the same question for many groups — `accounts.overview` — pays
    for one `incus list` instead of one per group.

    A container with no override counts as its repo's resolved group; a
    prefix with **no containers at all** falls back to `{repo's resolved
    group}`, because with nothing writing the shared config home the repo's
    own group is the best evidence there is — and because that keeps
    behaviour identical for every repo that never uses an override.

    Stopped containers count: a stopped container keeps its label and will
    write the config home again when it next runs.
    """
    result: dict[str, set[str | None]] = {}
    for prefix in prefixes:
        repo = gcfg.credentials.group_for(prefix)
        found: set[str | None] = set()
        for row in rows:
            name = str(row.get("name", ""))
            if not name.startswith(f"{prefix}-"):
                continue
            label = _label_group(row.get("config") or {})
            found.add(repo if label is INHERIT else label)  # type: ignore[arg-type] # narrowed by sentinel
        result[prefix] = found or {repo}
    return result


def authoritative_prefixes(
    gcfg: GlobalConfig,
    incus: Incus,
    group: str,
    prefixes: Collection[str],
) -> set[str]:
    """`authoritative_prefixes_from` for a caller holding an `Incus`."""
    return authoritative_prefixes_from(gcfg, incus.list_containers(), group, prefixes)


def authoritative_prefixes_from(
    gcfg: GlobalConfig,
    rows: Sequence[dict[str, Any]],
    group: str | None,
    prefixes: Collection[str],
) -> set[str]:
    """The prefixes whose `oauthAccount` can be trusted to describe `group`.

    `authoritative_in` applied to a fresh `groups_by_prefix_from`.
    """
    return authoritative_in(groups_by_prefix_from(gcfg, rows, prefixes), group)


def authoritative_in(
    by_prefix: dict[str, set[str | None]],
    group: str | None,
) -> set[str]:
    """The prefixes in `by_prefix` whose `oauthAccount` describes `group`.

    A repo is authoritative for a group only when *every* group its
    containers use is that one. A repo spanning two groups shares one
    `~/.claude` between them, so its `oauthAccount` names whichever
    account ran most recently — see `accounts.adapters.claude.account_of`.

    `group=None` asks the same question of a repo's *own* config home,
    which is a holder like any other: a repo with one container moved into
    a group can no longer name the login it keeps for itself.

    Takes the mapping rather than building one, so a caller resolving many
    holders — `accounts.overview` — computes it once for the whole host.
    """
    return {prefix for prefix, groups in by_prefix.items() if groups == {group}}


def container_groups(
    gcfg: GlobalConfig,
    rows: Sequence[dict[str, Any]],
    prefixes: Collection[str],
) -> list[tuple[str, str, str | None]]:
    """`(container, prefix, group)` for every container of a known prefix.

    `group` is the container's own label where it has a usable one, else its
    repo's resolved group. A `None` group is **not** one shared holder: such
    a container reads its own repo's config home, which is why the prefix is
    part of every triple.

    A container whose prefix is not in `prefixes` is skipped: nothing on this
    host says which group an unregistered repo resolves to, and an inherited
    group guessed from the wrong repo would file a container under a login it
    never reads. Where two prefixes both match — `app` and `app-web` for
    `app-web-x` — the longest wins, since that is the one whose
    `jailbee new` really created it.
    """
    ordered = sorted(prefixes, key=len, reverse=True)
    out: list[tuple[str, str, str | None]] = []
    for row in rows:
        name = str(row.get("name", ""))
        prefix = next((p for p in ordered if name.startswith(f"{p}-")), None)
        if prefix is None:
            continue
        resolved = gcfg.credentials.group_for(prefix)
        label = _label_group(row.get("config") or {})
        group = resolved if label is INHERIT else label
        out.append((name, prefix, group))  # type: ignore[arg-type] # narrowed by sentinel
    # By container name only: a `None` group would make a whole-tuple sort
    # raise as soon as two entries shared a name and a prefix.
    return sorted(out, key=lambda triple: triple[0])


def agent_running(cfg: Config, incus: Incus, container: str, *, command: str) -> bool | None:
    """Whether `command`'s binary looks to be running in `container`.

    `None` means the probe could not run — a stopped container, an Incus
    error — and callers must treat it as "cannot tell", never as "no".

    `pgrep -x`, not `-f`: `-f` matches the whole command line and would match
    the `sh -c` wrapper running the probe itself, so the answer would always be
    yes. The shell wrapper turns pgrep's exit code into stdout, because
    `Incus.exec` raises on a non-zero exit and pgrep exits 1 for the perfectly
    ordinary "no match".
    """
    import shlex

    from jailbee.config import CONTAINER_USERNAME
    from jailbee.incus import IncusError

    command = Path(command.split()[0]).name if command.strip() else "claude"
    script = (
        f"pgrep -u {CONTAINER_USERNAME} -x {shlex.quote(command)} "
        ">/dev/null && echo running || echo idle"
    )
    try:
        out = incus.exec(container, ["sh", "-c", script], timeout=15)
    except (IncusError, OSError):
        return None
    stripped = out.strip()
    if stripped == "running":
        return True
    if stripped == "idle":
        return False
    return None
