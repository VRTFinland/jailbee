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
    from jailbee.incus import Incus

EGRESS_EXTRA_KEY = "user.jailbee.egress_extra"
"""Container label holding a JSON array of raw egress entries."""

ACL_NAME_MAX = 63
"""Maximum length of an Incus network ACL name.

Verified against a live Incus daemon during implementation (the plan's
host-verification checklist); `derive_container_name` imposes no length cap
of its own, so a long branch name can leave no room for the suffix.
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

    rows = [EntryRow(entry=e, source=CONFIG_SOURCE) for e in config_entries]
    rows += [
        EntryRow(entry=e, source=REPO_SOURCE, redundant=e in config_set)
        for e in repo_extras(session, cfg.container_prefix)
    ]
    if container is not None:
        repo_set = config_set | set(repo_extras(session, cfg.container_prefix))
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
        return (
            f"# jailbee: no host-local egress overrides to promote for repo "
            f"'{prefix}'.\n"
        )

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
