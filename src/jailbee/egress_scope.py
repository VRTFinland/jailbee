"""Which egress entries apply to a repo, and to one container.

Three sources feed a container's allowlist, and this module is the only
place that knows all three:

1. ``config.yaml``'s ``egress_allow`` plus jailbee's feature auto-additions
   (``Config.effective_egress_allow``) — committed, shared with the team.
2. Repo-scope overrides in ``state.sqlite`` — **host-local**: they are not in
   git, so they never reach a teammate.
3. Container-scope overrides in the container's ``user.jailbee.egress_extra``
   label — host-local *and* container-local.

Container overrides live in a label rather than the DB so they die with the
container: no orphan rows, no chance that a recreated same-named container
silently inherits the old container's grants, and they survive a wiped
``state.sqlite``.

Sources 2 and 3 are **additive only**. Neither can revoke what ``config.yaml``
grants — see the design spec for why subtractive semantics were rejected.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from sqlmodel import select

from jailbee.db.models import EgressOverride

if TYPE_CHECKING:
    from datetime import datetime

    from sqlmodel import Session

    from jailbee.config import Config
    from jailbee.egress import EgressEntry
    from jailbee.incus import Incus

EGRESS_EXTRA_KEY = "user.jailbee.egress_extra"
"""Container label holding a JSON array of raw egress entries."""

ACL_NAME_MAX = 63
"""Maximum length of an Incus network ACL name.

An assumption, not a verified fact: this environment has no running Incus
daemon, so the plan's host-verification checklist (create a 63-char and a
64-char ACL name against a real daemon) has NOT been run. Treat this value as
pending maintainer verification — if it turns out wrong, update it and
re-run the ACL-naming tests in `tests/test_egress_scope.py`.
`derive_container_name` imposes no length cap of its own, so a long branch
name can leave no room for the suffix.
"""

_ACL_SUFFIX = "-extra"
_DIGEST_LEN = 8


# ---- repo scope (state.sqlite) ------------------------------------------


def repo_extras(session: Session, prefix: str) -> list[str]:
    """Host-local override entries for one repo, sorted."""
    rows = session.exec(
        select(EgressOverride).where(EgressOverride.container_prefix == prefix)
    ).all()
    return sorted(row.entry for row in rows)


def add_repo_extra(session: Session, prefix: str, entry: str, *, now: datetime) -> bool:
    """Add one override. Returns False when it was already there."""
    if session.get(EgressOverride, (prefix, entry)) is not None:
        return False
    session.add(EgressOverride(container_prefix=prefix, entry=entry, added_at=now))
    session.commit()
    return True


def remove_repo_extra(session: Session, prefix: str, entry: str) -> bool:
    """Remove one override. Returns False when it was not there."""
    row = session.get(EgressOverride, (prefix, entry))
    if row is None:
        return False
    session.delete(row)
    session.commit()
    return True


# ---- container scope (Incus label) --------------------------------------


def container_extras(incus: Incus, name: str) -> list[str]:
    """Override entries for one container, sorted.

    A label that is absent, unparseable, or not a list of strings yields an
    empty list. Garbage warns rather than raising: a container's egress must
    not fail closed silently, and must not fail open on garbage either.
    """
    raw = incus.config_get(name, EGRESS_EXTRA_KEY)
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        _warn_bad_label(name, "not valid JSON")
        return []
    if not isinstance(parsed, list) or not all(isinstance(item, str) for item in parsed):
        _warn_bad_label(name, "not a JSON array of strings")
        return []
    return sorted(set(parsed))


def set_container_extras(incus: Incus, name: str, entries: list[str]) -> None:
    """Replace the container's override list. An empty list unsets the label."""
    cleaned = sorted(set(entries))
    if not cleaned:
        incus.config_unset(name, EGRESS_EXTRA_KEY)
        return
    incus.config_set(name, EGRESS_EXTRA_KEY, json.dumps(cleaned))


def _warn_bad_label(name: str, reason: str) -> None:
    from jailbee.tui import warn

    warn(
        f"Ignoring {EGRESS_EXTRA_KEY} on '{name}' — {reason}. "
        f"Re-add the hosts with `jailbee net egress add`."
    )


# ---- naming --------------------------------------------------------------


def extra_acl_name(container: str) -> str:
    """ACL name for one container's extra allowlist.

    ``<container>-extra`` when it fits. Otherwise the container name is
    truncated and a digest of the *full* name is spliced in, so two long
    names sharing a head do not collide on one ACL.
    """
    name = f"{container}{_ACL_SUFFIX}"
    if len(name) <= ACL_NAME_MAX:
        return name
    head_len = ACL_NAME_MAX - len(_ACL_SUFFIX) - _DIGEST_LEN - 1
    digest = hashlib.sha256(container.encode()).hexdigest()[:_DIGEST_LEN]
    return f"{container[:head_len]}-{digest}{_ACL_SUFFIX}"


# ---- composition, classification, and rendering ---------------------------


CONFIG_SOURCE = "config"
REPO_SOURCE = "repo-override"
CONTAINER_SOURCE = "container"


@dataclass(frozen=True)
class EntryRow:
    """One applicable entry and where it came from.

    ``redundant`` marks an override that ``config.yaml`` already grants —
    which is how a user sees that a promoted entry can now be removed.
    """

    entry: str
    source: str
    redundant: bool = False


def effective_repo_entries(cfg: Config, session: Session) -> list[str]:
    """Every raw entry that applies to this repo's shared ACL.

    ``cfg.effective_egress_allow()`` first (config plus jailbee's feature
    auto-additions), then host-local repo overrides. Order is preserved so
    the ACL diff stays readable; duplicates are dropped.
    """
    seen: dict[str, None] = {}
    for entry in [*cfg.effective_egress_allow(), *repo_extras(session, cfg.container_prefix)]:
        seen.setdefault(entry, None)
    return list(seen)


def repo_file_egress_allow(config_path: Path) -> list[str]:
    """The repo config file's own ``egress_allow``, read raw from that file.

    Deliberately NOT ``cfg.egress_allow``: `deep_merge` appends lists and
    ``egress_allow`` is not in ``config._HOST_LEVEL_KEYS``, so the loaded
    value already carries the *global* config's entries. Writing those into
    a committed repo config would push one machine's host-level policy to
    the whole team.
    """
    import yaml

    if not config_path.is_file():
        return []
    raw = yaml.safe_load(config_path.read_text()) or {}
    if not isinstance(raw, dict):
        return []
    value = raw.get("egress_allow") or []
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str)]


def classify_sources(
    cfg: Config,
    session: Session,
    incus: Incus,
    container: str | None = None,
) -> list[EntryRow]:
    """Every applicable entry with its source, config rows first.

    Backs both ``jb net egress ls`` and ``jb net egress export``, so the two
    can never disagree about what counts as an override — an ``export`` that
    emitted a row ``ls`` calls config-sourced would put a duplicate into the
    user's config.
    """
    config_entries = cfg.effective_egress_allow()
    config_set = set(config_entries)
    repo_override_entries = repo_extras(session, cfg.container_prefix)

    rows = [EntryRow(entry=e, source=CONFIG_SOURCE) for e in config_entries]
    rows += [
        EntryRow(entry=e, source=REPO_SOURCE, redundant=e in config_set)
        for e in repo_override_entries
    ]
    if container is not None:
        repo_set = config_set | set(repo_override_entries)
        rows += [
            EntryRow(entry=e, source=CONTAINER_SOURCE, redundant=e in repo_set)
            for e in container_extras(incus, container)
        ]
    return rows


def render_config_block(
    file_entries: list[str],
    overrides: list[str],
    *,
    prefix: str,
) -> str:
    """The complete replacement for the repo config's ``egress_allow:`` key.

    A whole-key replacement, not a fragment. Emitting only the override rows
    under an ``egress_allow:`` key would produce a duplicate mapping key when
    pasted into a config that already has one — ``yaml.safe_load`` silently
    keeps the last, so the user's existing entries would vanish with no
    error. Emitting bare list items instead is not valid YAML on its own and
    cannot be appended to the ``egress_allow: []`` that `jb config init`
    writes.

    ``file_entries`` must come from `repo_file_egress_allow`, never from
    ``cfg.effective_egress_allow()`` — the latter folds in the claude /
    github / jetbrains feature hosts, which track jailbee releases and go
    stale the moment they are frozen into a config file.
    """
    import yaml

    merged: dict[str, None] = {}
    for entry in [*file_entries, *overrides]:
        merged.setdefault(entry, None)

    if not overrides:
        return f"# jailbee: no host-local egress overrides to promote for repo '{prefix}'.\n"

    promoted = [e for e in overrides if e not in set(file_entries)]
    header = (
        f"# jailbee: replaces the `egress_allow:` key in your repo config\n"
        f"# ({len(file_entries)} existing + {len(promoted)} host-local "
        f"override(s) for repo '{prefix}').\n"
        f"# Replace the whole key, run `jailbee apply`, then drop the now-\n"
        f"# redundant overrides with `jailbee net egress rm`.\n"
    )
    body = yaml.safe_dump({"egress_allow": list(merged)}, sort_keys=False)
    return header + body


# ---- materialisation onto the container NIC ------------------------------


def resolve_entries(entries: list[str]) -> list[EgressEntry]:
    """Resolve raw entries to `EgressEntry`. Factored out for test seams.

    Public (no leading underscore): `cli.py` calls this for a fail-fast
    check before touching the container — an unresolvable host in a brand
    new `jailbee net egress add` is almost always the user's typo, and they
    can fix it on the spot — so it is part of this module's contract, not a
    private helper. Raises on the first bad host, unlike
    `_resolve_entries_tolerant` below, which `apply_container_acl` uses:
    materialising an *existing* label must not abort over one host that
    went briefly unresolvable.
    """
    from jailbee.egress import build_egress_entries

    return build_egress_entries(entries)


def _resolve_entries_tolerant(name: str, entries: list[str]) -> list[EgressEntry]:
    """Resolve raw entries to `EgressEntry`, tolerating a per-host DNS failure.

    Unlike `resolve_entries` above, a container's own extra ACL must not
    fail closed — or abort the caller — because ONE host is momentarily
    unresolvable (VPN down, a corp DNS blip). A hostname that fails to
    resolve is dropped and named in a `warn`, never silently: `entries` is
    built from whatever DID resolve. Mirrors
    `egress_pool._refresh_container_extras`, which resolves this same field
    tolerantly on the refresh timer — this closes the gap where
    `apply_container_acl` used to be the one place that instead aborted the
    whole container (and, via `jailbee apply`'s per-container loop, every
    later container in the repo) on the first failing host.
    """
    from jailbee.egress import EgressEntry, parse_egress_entry, resolve_with_status
    from jailbee.tui import warn

    specs = [parse_egress_entry(raw) for raw in entries]
    hostnames = sorted({s.target for s in specs if not s.is_literal})
    resolved, failed = resolve_with_status(hostnames)

    if failed:
        warn(
            f"'{name}': could not resolve for its egress override(s), "
            f"skipping — will retry next refresh: "
            f"{', '.join(f'{n} ({e})' for n, e in sorted(failed.items()))}"
        )

    out: list[EgressEntry] = []
    for raw, spec in zip(entries, specs, strict=True):
        destinations: list[str]
        if spec.is_literal:
            destinations = [spec.target]
        else:
            found = resolved.get(spec.target)
            if not found:
                continue
            destinations = found
        out.append(EgressEntry(destinations=destinations, port=spec.port, description=raw))
    return out


def _local_eth0(incus: Incus, name: str) -> dict[str, str] | None:
    """The container's own ``eth0`` device, or None when it inherits one.

    Reads `devices` (instance-local) from `incus list --format json`, NOT
    `expanded_devices` (profile-merged) — the whole question here is whether
    a local override already shadows the profile.
    """
    for raw in incus.list_containers():
        if raw["name"] == name:
            devices = raw.get("devices") or {}
            device = devices.get("eth0")
            return dict(device) if device else None
    return None


def _desired_eth0(cfg: Config, acl_names: list[str]) -> dict[str, str]:
    """The strict profile's ``eth0``, with the ACL list replaced.

    Derived from `profiles.net_profile_yaml`, a pure function of `cfg`, so
    the local copy cannot drift from what the profile would have given the
    container.
    """
    import yaml

    from jailbee.profiles import net_profile_yaml

    profile = yaml.safe_load(net_profile_yaml(cfg, "strict"))
    device: dict[str, str] = dict(profile["devices"]["eth0"])
    device["security.acls"] = ",".join(acl_names)
    return device


def apply_container_acl(
    cfg: Config,
    incus: Incus,
    name: str,
    *,
    mode: str,
) -> None:
    """Materialise (or tear down) one container's extra egress ACL.

    Idempotent and derived: the label is the source of truth, and this
    rebuilds the ACL and the ``eth0`` override from it every time.

    ``mode="loose"`` tears the override down. It must: `incus config device
    override` copies the profile's device onto the container, and a local
    ``eth0`` shadows whatever profile is assigned afterwards — leaving it in
    place would pin a "loose" container to `incusbr0` with the strict ACL
    still enforced, while `jailbee ls` reported loose.

    Resolution is tolerant (`_resolve_entries_tolerant`): a host that fails
    to resolve is dropped (with a warning naming it), not fatal to the whole
    call. This matters beyond this function — every caller either aborts a
    wider operation on an exception (`jailbee apply`'s per-container loop,
    `jailbee net strict`/`loose`'s `switch_network`) or retries forever
    without ever clearing the failing state (`loose_revert`'s TTL check), so
    one momentarily-unresolvable extra host must never raise here. If
    NOTHING resolves, the container's existing extra ACL (if any) is left
    completely untouched rather than pushed empty — that would cut off
    access the container already has — but the NIC/device sync below still
    runs against whatever ACL already exists, so `security.acls` stays
    correct for `mode` either way.
    """
    from jailbee.network import acl_name, extra_acl_yaml

    extras = container_extras(incus, name)
    extra_name = extra_acl_name(name)

    if mode != "strict" or not extras:
        incus.config_device_remove(name, "eth0", missing_ok=True)
        drop_container_acl(incus, name)
        _warn_if_local_eth0_survived(incus, name)
        return

    entries = _resolve_entries_tolerant(name, extras)
    acl_already_exists = incus.network_acl_exists(extra_name)
    if entries:
        if not acl_already_exists:
            incus.network_acl_create(extra_name)
        incus.network_acl_set_yaml(extra_name, extra_acl_yaml(extra_name, entries))
    elif not acl_already_exists:
        # Nothing has ever resolved for this container: there is no ACL to
        # reference on the NIC yet, and creating an empty stub just to wire
        # it in buys nothing over leaving `eth0` alone for now. The next
        # refresh that resolves something creates the ACL and wires the NIC
        # in one pass.
        return
    # else: extras exist but nothing resolved THIS time, and a previous,
    # possibly-stale ACL is still in place — left untouched above. Fall
    # through to the NIC/device sync, which only depends on the ACL NAME,
    # not its content.

    desired = _desired_eth0(cfg, [acl_name(cfg), extra_name])
    existing = _local_eth0(incus, name)
    if existing == desired:
        return
    if existing is None:
        incus.config_device_override(name, "eth0", desired)
    else:
        # In-place update: removing and re-adding would detach the NIC of a
        # running container for the duration.
        incus.config_device_set(name, "eth0", desired)


def _warn_if_local_eth0_survived(incus: Incus, name: str) -> None:
    """Warn when the ``eth0`` teardown above did not actually take.

    `config_device_remove(..., missing_ok=True)` swallows every
    `IncusError` — exhausted ETag retries, an unreachable daemon, "instance
    is busy" — not just "no such local device". Matching Incus's error text
    to narrow that swallow can't be verified without a live daemon in this
    environment, so instead: re-read the container's local `eth0` after the
    removal, and if it is still there, say so. A silently-failed teardown
    leaves a container pinned to `incusbr0` with the strict ACL enforced
    while it sits on the loose profile — `jailbee ls` would report loose
    with no hint that the wire says otherwise.
    """
    from jailbee.tui import warn

    if _local_eth0(incus, name) is not None:
        warn(
            f"'{name}' still has a local eth0 device after teardown — its "
            f"reported network mode may not match what is enforced. Try "
            f"`jailbee apply` again, or inspect with "
            f"`incus config device show {name}`."
        )


def drop_container_acl(incus: Incus, name: str) -> None:
    """Delete one container's extra ACL if it exists.

    Call only after the referencing NIC is gone — Incus refuses to delete an
    ACL still applied to an instance.
    """
    extra_name = extra_acl_name(name)
    if incus.network_acl_exists(extra_name):
        incus.network_acl_delete(extra_name)
