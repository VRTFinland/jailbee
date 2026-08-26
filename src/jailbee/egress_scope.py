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
from typing import TYPE_CHECKING

from sqlmodel import select

from jailbee.db.models import EgressOverride

if TYPE_CHECKING:
    from datetime import datetime

    from sqlmodel import Session

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
