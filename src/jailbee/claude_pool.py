"""jailbee's own Claude Code account pool.

A holder — a credential *group* directory, or a single repo's config home when
it shares none — has at most one live login. Every other stored login sits in a
host-wide store as a plain file. Switching moves one file out and another in,
then deletes the `oauthAccount` block from each member repo's `.claude.json`
so Claude Code repopulates it from the credential it now finds.

**Move, never copy.** Copying one credential blob to two places gives one
refresh-token lineage two refreshers, and the first rotation silently logs the
other out. Every operation here is a rename or an atomic replace, so exactly
one file holds any given grant.

**No state but the filesystem.** A file in `store_dir()` is parked; the file in
`holder_dir(cfg)` is live. There is no ledger, so nothing can disagree with the
directory about what the directory contains.

**What this module reads.** Account identity comes from `oauthAccount` in a
config home's `.claude.json`, never from the credential. The credential file
itself is parsed only to carry the machine-shared sibling keys across a switch
(see `compose_credential`) — `claudeAiOauth` is moved, never read, logged or
transmitted.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import tempfile
from collections.abc import Sequence
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from jailbee.claude_locks import ClaudeLockTimeout, config_lock, credential_locks

if TYPE_CHECKING:
    from jailbee.config import Config
    from jailbee.global_config import GlobalConfig

log = logging.getLogger(__name__)

LIVE_UNIDENTIFIED = "(unknown)"
"""Display name for a live credential whose account cannot be identified.

The parentheses are load-bearing: they cannot appear in a slug, so this name
can never collide with a parked slot or be typed as a bare-email reference.
"""


class PoolError(Exception):
    """A pool operation cannot proceed; the message is user-facing."""


@dataclass(frozen=True)
class Identity:
    """A Claude account as its config home names it."""

    email: str
    org_uuid: str | None = None


@dataclass(frozen=True)
class Slot:
    """One stored login: a parked file, or the live credential.

    `org_hint` is the **truncated** organization from the slot name, not a
    UUID. Never compare it with `Identity.org_uuid` — match a live identity to
    a slot by comparing `slug_for(identity)` with `Slot.name`.
    """

    name: str
    path: Path
    live: bool

    @property
    def email(self) -> str | None:
        """The account's email, or None for an unidentified slot.

        Display-only: a real email address that happens to start with
        `unknown-` would be misreported as unidentified. `read_identity` does
        not require an email-shaped string, so this is possible in principle.
        """
        if self.name == LIVE_UNIDENTIFIED or self.name.startswith("unknown-"):
            return None
        return self.name.split("#", 1)[0]

    @property
    def org_hint(self) -> str | None:
        """First 8 characters of the organization UUID, when the name has one."""
        if self.email is None:
            return None
        _, sep, tail = self.name.partition("#")
        return tail if sep else None


def store_dir() -> Path:
    """The host-wide parked-credential store.

    A sibling of the group directories rather than a child of one: an account
    parked from `work` must be activatable into `personal`. Safe from
    collision because `_CREDENTIAL_GROUP_RE` (`config.py`) forbids a group name
    starting with `_`.
    """
    from jailbee.paths import xdg_data_home

    return xdg_data_home() / "jailbee" / "claude-credentials" / "_parked"


def config_home(cfg: Config) -> Path:
    """This repo's Claude config home on the host — never shared."""
    assert cfg.shared_dir is not None  # set by load_config
    return cfg.shared_dir / "claude"


def holder_dir(cfg: Config) -> Path:
    """The directory whose `.credentials.json` this repo's containers read."""
    return cfg.claude_credentials_dir or config_home(cfg)


def live_credential_path(cfg: Config) -> Path:
    """The live credential file for this repo's holder."""
    return holder_dir(cfg) / ".credentials.json"


def identity_file(home: Path) -> Path:
    """The config file carrying `oauthAccount`, mirroring Claude Code's own
    resolution: the legacy `.config.json` when it exists, else `.claude.json`.
    """
    legacy = home / ".config.json"
    return legacy if legacy.exists() else home / ".claude.json"


def read_identity(home: Path) -> Identity | None:
    """The account a config home names, or None when it names none.

    Every failure — absent, unreadable, torn, or missing the block — is None.
    Callers treat an unidentified account as a fact to report, not an error:
    a fresh group has no identity anywhere until something has run.
    """
    try:
        data = json.loads(identity_file(home).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    block = data.get("oauthAccount")
    if not isinstance(block, dict):
        return None
    email = block.get("emailAddress")
    if not isinstance(email, str) or not email:
        return None
    org = block.get("organizationUuid")
    return Identity(email=email, org_uuid=org if isinstance(org, str) and org else None)


_SLUG_UNSAFE = re.compile(r"[^a-z0-9@._-]")


def slug_for(identity: Identity) -> str:
    """The slot name for an account: `<email>` or `<email>#<org8>`.

    The organization is in the name whenever the account has one, not only on
    collision: detecting a collision after the fact would mean knowing an
    existing slot's organization, which without a manifest is not knowable.

    **Both halves are sanitized, and the result is a single path component.**
    Identity comes from a `.claude.json` that containers write, so it is
    untrusted: an unsanitized organization carrying `/` would let a slot path
    escape the store. Leading dots are stripped as hygiene — a slot file is not
    meant to be hidden — though `Path.glob` would still list one.
    """
    email = _SLUG_UNSAFE.sub("-", identity.email.strip().lower())
    slug = email
    if identity.org_uuid:
        org = _SLUG_UNSAFE.sub("-", identity.org_uuid.strip().lower())[:8]
        slug = f"{email}#{org}"
    return slug.lstrip(".") or "unnamed"


def unknown_slot_name(when: datetime) -> str:
    """Name for parking a credential whose account cannot be identified.

    Self-healing rather than blocking: once that account is activated and used,
    its config home carries an identity, so the *next* park writes the real
    name.
    """
    return f"unknown-{when.strftime('%Y%m%d-%H%M%S')}"


SHARED_CREDENTIAL_KEYS = frozenset(
    {"mcpOAuth", "mcpOAuthClientConfig", "mcpXaaIdp", "mcpXaaIdpConfig", "pluginSecrets"}
)
"""Siblings of `claudeAiOauth` that belong to the machine, not to an account.

They hold OAuth integrations that rotate independently of any login, so on
activation the live copy is authoritative. The list is cswap's
(claude-swap, MIT) `SHARED_CREDENTIAL_KEYS`.
"""

ACCOUNT_CREDENTIAL_KEYS = frozenset({"claudeAiOauth", "trustedDeviceToken"})
"""Account-scoped siblings we know about, named so the probe below does not
flag them. `trustedDeviceToken` is enrolled per (device, account) at login."""


def _credential_object(raw: str | None) -> dict[str, Any] | None:
    """Parse a credential file's text, or None when it is not a JSON object.

    A managed `sk-ant-…` API key and any opaque legacy shape land here as
    None, which every caller treats as "activate verbatim".
    """
    if raw is None:
        return None
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None
    return data if isinstance(data, dict) else None


def shared_fields(raw: str | None) -> dict[str, Any] | None:
    """The machine-shared fields of a live credential.

    A dict — including `{}` — is authoritative for every allowlisted key: one
    absent here is absent from the machine's current state and must not be
    resurrected from a slot's snapshot. None means there was no JSON credential
    object to read.
    """
    data = _credential_object(raw)
    if data is None:
        return None
    if "claudeAiOauth" in data:
        # An unknown sibling defaults to slot-owned, which fails safe but
        # silently: if Claude Code grows a new *shared* key, that default
        # quietly reintroduces the stale-restore papercut for it. Leave a
        # trace so it gets noticed.
        unrecognized = data.keys() - SHARED_CREDENTIAL_KEYS - ACCOUNT_CREDENTIAL_KEYS
        if unrecognized:
            log.debug(
                "credential has sibling keys jailbee does not recognize "
                "(a newer Claude Code?), treating them as account-owned: %s",
                sorted(unrecognized),
            )
    return {key: data[key] for key in SHARED_CREDENTIAL_KEYS if key in data}


def compose_credential(target_raw: str, live_shared: dict[str, Any] | None) -> str:
    """The credential to activate, composed from its two owners.

    Shared keys come from `live_shared`; everything else comes from the slot.
    A target that is not a JSON object carrying a login activates unchanged, as
    does any target when there is nothing live to take shared fields from.
    """
    if live_shared is None:
        return target_raw
    target = _credential_object(target_raw)
    if target is None or "claudeAiOauth" not in target:
        return target_raw
    composed = {k: v for k, v in target.items() if k not in SHARED_CREDENTIAL_KEYS}
    composed.update(live_shared)
    return json.dumps(composed)


def parked_slots() -> list[Slot]:
    """Every stored login, sorted by name. An absent store is an empty pool."""
    store = store_dir()
    try:
        files = sorted(store.glob("*.json"))
    except OSError:
        return []
    return [Slot(name=p.name[: -len(".json")], path=p, live=False) for p in files]


def live_slot(cfg: Config, identity: Identity | None) -> Slot | None:
    """The holder's live login, or None when nothing is logged in."""
    path = live_credential_path(cfg)
    if not path.exists():
        return None
    name = slug_for(identity) if identity is not None else LIVE_UNIDENTIFIED
    return Slot(name=name, path=path, live=True)


def resolve_ref(ref: str, slots: Sequence[Slot]) -> Slot:
    """The slot a user-typed reference names.

    An exact slot name wins; otherwise a bare email must match exactly one
    account. Nothing is guessed — an ambiguous or unknown reference is an
    error naming the candidates.
    """
    wanted = ref.strip()
    exact = [s for s in slots if s.name == wanted]
    if len(exact) > 1:
        where = ", ".join(str(s.path) for s in sorted(exact, key=lambda s: str(s.path)))
        raise PoolError(
            f"`{wanted}` names {len(exact)} files ({where}). One login must exist "
            "in exactly one place — remove or rename all but one before switching."
        )
    if exact:
        return exact[0]

    lowered = wanted.lower()
    by_email = [s for s in slots if s.email is not None and s.email == lowered]
    if len(by_email) == 1:
        return by_email[0]
    if len(by_email) > 1:
        names = ", ".join(sorted(s.name for s in by_email))
        raise PoolError(
            f"`{ref}` matches several accounts: {names}. Pass the full slot name."
        )

    known = ", ".join(sorted(s.name for s in slots))
    raise PoolError(
        f"no stored account matches `{ref}`."
        + (f" Known: {known}" if known else " The pool is empty.")
    )


@dataclass(frozen=True)
class Member:
    """One repo sharing a holder, and the config home whose identity it owns."""

    container_prefix: str
    config_home: Path


def _registered_repos() -> list[tuple[str, Path]]:
    """Every registered repo as (container_prefix, repo_root).

    Raises rather than degrading to empty: for a mutation, an unreadable
    registry must not look like "this holder has no other members".
    """
    from sqlmodel import Session, select

    from jailbee.db import get_engine
    from jailbee.db.models import RegisteredRepo

    with Session(get_engine()) as session:
        rows = session.exec(select(RegisteredRepo)).all()
    return [(row.container_prefix, Path(row.repo_root)) for row in rows]


def _resolves_to(gcfg: GlobalConfig, prefix: str, group: str) -> bool:
    """Whether `prefix` resolves to `group` under this host's config."""
    resolved = gcfg.claude_credentials.dir_for(prefix)
    return resolved is not None and resolved.name == group


def group_member_prefixes(gcfg: GlobalConfig, group: str) -> list[str]:
    """Registered repos resolving to `group`, sorted, including the caller.

    The single implementation of the group-matching rule; `doctor.py` filters
    the caller out of it for display.
    """
    return sorted(
        prefix for prefix, _ in _registered_repos() if _resolves_to(gcfg, prefix, group)
    )


def members(cfg: Config, gcfg: GlobalConfig) -> tuple[list[Member], list[str]]:
    """Every repo sharing this repo's holder, plus the ones we could not read.

    A repo that shares nothing is its own only member, with no registry read.
    An unreadable member is *named*, not skipped: skipping is right for a
    read-only listing (`dashboard.py:240`), but here it would leave that
    repo's `oauthAccount` stale and silently naming the wrong account.
    """
    from jailbee.config import load_config
    from jailbee.paths import repo_config_path

    me = Member(cfg.container_prefix, config_home(cfg))
    if cfg.claude_credentials_dir is None:
        return [me], []

    group = cfg.claude_credentials_dir.name
    found = [me]
    unreachable: list[str] = []
    for prefix, repo_root in _registered_repos():
        if prefix == cfg.container_prefix or not _resolves_to(gcfg, prefix, group):
            continue
        path = repo_config_path(repo_root)
        if path is None:
            unreachable.append(prefix)
            continue
        try:
            other = load_config(path)
        except Exception:  # ConfigError, OSError, YAML/Pydantic — all mean "unreadable"
            unreachable.append(prefix)
            continue
        found.append(Member(prefix, config_home(other)))
    return sorted(found, key=lambda m: m.container_prefix), sorted(unreachable)


def live_identity(found: Sequence[Member], *, prefer: str) -> Identity | None:
    """The account the holder's live credential belongs to.

    Read from a config home, never from the credential. The calling repo is
    consulted first; any member will do, since they share one login. None
    means no member names an account yet — a fresh group, not an error.
    """
    ordered = sorted(found, key=lambda m: m.container_prefix != prefer)
    for member in ordered:
        identity = read_identity(member.config_home)
        if identity is not None:
            return identity
    return None


def live_session_prefixes(found: Sequence[Member]) -> list[str]:
    """Members that look like they have a Claude Code session running.

    Claude Code writes `<config home>/sessions/<pid>.json` per session. The
    PIDs belong to container namespaces the host cannot check, so a leftover
    file reads as live — this is a warning input, never a refusal.
    """
    busy: list[str] = []
    for member in found:
        try:
            if any((member.config_home / "sessions").glob("*.json")):
                busy.append(member.container_prefix)
        except OSError:
            continue
    return sorted(busy)


@dataclass(frozen=True)
class PoolChange:
    """What one pool operation did, for the CLI to report."""

    parked_as: str | None
    activated: str | None
    cleared: list[str]
    not_cleared: list[str]
    live_sessions: list[str]


def _atomic_write(path: Path, text: str) -> None:
    """Replace `path` atomically, mode 0600.

    The temporary file is created in the destination directory so the replace
    is a same-filesystem rename, and its mode is set *before* the replace so
    the final path is never briefly world-readable.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    tmp = Path(name)
    try:
        try:
            os.write(fd, text.encode("utf-8"))
        finally:
            os.close(fd)
        tmp.chmod(0o600)
        tmp.replace(path)
    except BaseException:
        with suppress(OSError):
            tmp.unlink()
        raise


def _slots_for(cfg: Config, found: Sequence[Member]) -> tuple[list[Slot], Identity | None]:
    """Every slot for this holder, live last-resolved identity alongside."""
    identity = live_identity(found, prefer=cfg.container_prefix)
    slots = parked_slots()
    live = live_slot(cfg, identity)
    if live is not None:
        slots.append(live)
    return sorted(slots, key=lambda s: (not s.live, s.name)), identity


def list_slots(cfg: Config, gcfg: GlobalConfig) -> list[Slot]:
    """Every stored login, the live one first."""
    found, _ = members(cfg, gcfg)
    return _slots_for(cfg, found)[0]


def invalidate_identity(home: Path) -> bool:
    """Delete `oauthAccount` so Claude Code repopulates it from the credential.

    Returns whether the config home is now consistent — True also when there
    was nothing to delete. False means the file exists but could not be read
    or written; it is **never** overwritten in that case, because a torn
    `.claude.json` still holds the user's projects and MCP servers and an
    `or {}` here would erase them.
    """
    path = identity_file(home)
    try:
        with config_lock(home):
            if not path.exists():
                return True
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                return False
            if "oauthAccount" not in data:
                return True
            del data["oauthAccount"]
            _atomic_write(path, json.dumps(data, indent=2))
    except (ClaudeLockTimeout, OSError, json.JSONDecodeError):
        return False
    return True


def _clear_identities(found: Sequence[Member], unreachable: Sequence[str]) -> tuple[list[str], list[str]]:
    """Delete every member's recorded account; report which ones took."""
    cleared: list[str] = []
    not_cleared: list[str] = list(unreachable)
    for member in found:
        target = cleared if invalidate_identity(member.config_home) else not_cleared
        target.append(member.container_prefix)
    return sorted(cleared), sorted(not_cleared)


def _park_locked(cfg: Config, identity: Identity | None, when: datetime) -> str | None:
    """Move the live credential into the store. Caller holds the lock.

    `shutil.move` rather than `Path.replace`: `shared_dir` is user-overridable,
    so the holder and the store can live on different filesystems, where a
    rename raises `EXDEV`. Same-filesystem moves stay atomic renames.
    """
    live = live_credential_path(cfg)
    if not live.exists():
        return None
    name = slug_for(identity) if identity is not None else unknown_slot_name(when)
    store = store_dir()
    store.mkdir(parents=True, exist_ok=True)
    dest = store / f"{name}.json"
    if dest.exists():
        raise PoolError(
            f"the store already holds `{name}` ({dest}). Two files would be two "
            "copies of one login, and the first token rotation would kill the "
            "other — remove or rename one of them first."
        )
    shutil.move(str(live), str(dest))
    return name


def park(cfg: Config, gcfg: GlobalConfig, *, now: datetime | None = None) -> PoolChange:
    """Store the live login and leave the holder empty.

    This is how a *new* account enters the pool: with no credential to find,
    the next `claude` in any member container prompts `/login`, and that login
    lands straight in the holder.
    """
    found, unreachable = members(cfg, gcfg)
    identity = live_identity(found, prefer=cfg.container_prefix)
    holder = holder_dir(cfg)
    holder.mkdir(parents=True, exist_ok=True)
    with credential_locks(holder):
        parked_as = _park_locked(cfg, identity, now or datetime.now())
    if parked_as is None:
        return PoolChange(None, None, [], list(unreachable), [])
    cleared, not_cleared = _clear_identities(found, unreachable)
    return PoolChange(parked_as, None, cleared, not_cleared, live_session_prefixes(found))


def switch(
    cfg: Config, gcfg: GlobalConfig, ref: str, *, now: datetime | None = None
) -> PoolChange:
    """Park the live login and activate a stored one.

    The target is renamed out of the store *before* anything else moves, so no
    failure path can leave one grant in two files. On any error both files go
    back where they were.
    """
    found, unreachable = members(cfg, gcfg)
    slots, identity = _slots_for(cfg, found)
    target = resolve_ref(ref, slots)
    if target.live:
        raise PoolError(f"`{target.name}` is already the live account for this holder.")

    holder = holder_dir(cfg)
    holder.mkdir(parents=True, exist_ok=True)
    live_path = live_credential_path(cfg)
    staged = target.path.with_name(target.path.name + ".activating")

    with credential_locks(holder):
        target_raw = target.path.read_text(encoding="utf-8")
        live_raw = live_path.read_text(encoding="utf-8") if live_path.exists() else None
        target.path.replace(staged)
        parked_as: str | None = None
        try:
            parked_as = _park_locked(cfg, identity, now or datetime.now())
            _atomic_write(live_path, compose_credential(target_raw, shared_fields(live_raw)))
        except BaseException:
            if parked_as is not None:
                shutil.move(str(store_dir() / f"{parked_as}.json"), str(live_path))
            staged.replace(target.path)
            raise
        staged.unlink()

    cleared, not_cleared = _clear_identities(found, unreachable)
    return PoolChange(
        parked_as, target.name, cleared, not_cleared, live_session_prefixes(found)
    )


def remove_slot(slot: Slot) -> None:
    """Delete a parked login permanently."""
    if slot.live:
        raise PoolError(
            f"`{slot.name}` is the live account — run `jailbee claude park` first."
        )
    slot.path.unlink()
