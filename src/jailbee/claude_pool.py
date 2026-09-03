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

**One account is not one login.** Two independent grants for the same account
are ordinary: `/login` as the same account after a `park` is the documented way
to add one, and two holders on a host can each be logged into it. Slot names
are derived from the account, so they collide, and a colliding name gets a
disambiguator rather than a refusal — see `Slot` for the grammar and
`_disambiguated_slot` for the rule. The invariant is one *login* per file,
never one account per file.

**No state but the filesystem.** A file in `store_dir()` is parked; the file in
`holder_dir(cfg)` is live. There is no ledger, so nothing can disagree with the
directory about what the directory contains. The two places that record *which
account* a credential belongs to — `ACCOUNT_RECORD_KEY` inside a parked file
and `ACCOUNT_NOTE_FILE` beside a live one — are not ledgers either: each
describes only the file it travels with, and each is checked against that file
before it is believed, so neither can be repaired and neither needs to be.

**What this module reads.** Account identity comes from `oauthAccount` as
Claude Code writes it — in a config home's `.claude.json`, or in the copy of
that block jailbee keeps with a grant it moved itself — never from the
credential's own contents. The credential file is parsed only to carry the
machine-shared sibling keys across a switch (see `compose_credential`) and to
fingerprint its refresh-token lineage (see `ACCOUNT_NOTE_FILE`);
`claudeAiOauth` is moved, never logged or transmitted.

**An interrupted switch is reported, not healed.** A hard kill inside
`switch`'s staging window leaves `<name>.json.activating` in the store, a file
`parked_slots()` does not list. Renaming it home automatically would require
answering "does this grant already exist somewhere else?", and it cannot be
answered from here: the store is host-wide while one `switch` sees one
holder's live credential. A wrong answer gives one refresh-token lineage two
refreshers and silently kills a login, which is the one outcome this module
exists to prevent. `jailbee doctor` names the file and the rename that
recovers it instead; nothing here moves, adopts or deletes it.
"""

from __future__ import annotations

import errno
import hashlib
import json
import logging
import os
import re
import shutil
import tempfile
from collections.abc import Callable, Collection, Sequence
from contextlib import suppress
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from jailbee.claude_locks import ClaudeLockTimeoutError, config_lock, credential_locks

if TYPE_CHECKING:
    from jailbee.config import Config
    from jailbee.global_config import GlobalConfig

log = logging.getLogger(__name__)

LIVE_UNIDENTIFIED = "(unknown)"
"""Display name for a live credential whose account cannot be identified.

The parentheses are load-bearing: they cannot appear in a slug, so this name
can never collide with a parked slot or be typed as a bare-email reference.
"""

DISAMBIGUATOR = "~"
"""Separator between a slot's derived name and whatever makes it unique.

Safe because `_SLUG_UNSAFE` replaces it inside both halves of a derived name,
so it can never appear in one by accident. See `Slot` for the full grammar.
"""

_SLOT_SUFFIX = ".json"
"""Extension every file in the store carries; a slot name is the rest."""


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

    **Slot names follow one grammar**, and this docstring is its definition:

        <email>[#<org8>][~<disambiguator>]

    - `<email>` and `<org8>` are what `slug_for` derives from the account's
      identity. Its allowed set — `[a-z0-9@._-]` — contains neither `#` nor
      `~`, which is exactly what makes both separators safe to split on. Widen
      that set and this grammar breaks.
    - `~<disambiguator>` appears only when the derived name is already taken by
      a **different** grant. One account can legitimately hold two independent
      logins: `/login` as the same account after a `park` is the documented way
      to add one, and two holders on a host can each be logged into it. See
      `_disambiguated_slot` for the park side and `_slots_for` for the live
      side, whose disambiguator is the literal `live`.
    - Two shapes carry no email at all: `LIVE_UNIDENTIFIED`, and
      `unknown-<timestamp>` from `unknown_slot_name`. Both properties below are
      None for them, disambiguator or not.

    `org_hint` is the **truncated** organization from the slot name, not a
    UUID. Never compare it with `Identity.org_uuid`, and do not treat a name as
    an identity: `slug_for(identity)` equals the *derived* part of a slot name,
    which for a disambiguated slot is not the whole of it.
    """

    name: str
    path: Path
    live: bool

    @property
    def _derived(self) -> str:
        """The name without its `~<disambiguator>` suffix."""
        return self.name.split(DISAMBIGUATOR, 1)[0]

    @property
    def email(self) -> str | None:
        """The account's email, or None for an unidentified slot.

        Display-only: a real email address that happens to start with
        `unknown-` would be misreported as unidentified. `read_identity` does
        not require an email-shaped string, so this is possible in principle.
        """
        if is_unidentified(self.name):
            return None
        return self._derived.split("#", 1)[0]

    @property
    def org_hint(self) -> str | None:
        """First 8 characters of the organization UUID, when the name has one."""
        if self.email is None:
            return None
        _, sep, tail = self._derived.partition("#")
        return tail if sep else None

    @property
    def disambiguator(self) -> str | None:
        """The `~<disambiguator>` part, or None when the name has none.

        Unlike `email` and `org_hint` this is defined for the emailless shapes
        too: an unidentified slot can collide with another just as an
        identified one can, and the suffix is then the only thing telling the
        two apart.
        """
        _, sep, tail = self.name.partition(DISAMBIGUATOR)
        return tail if sep else None

    @property
    def display_name(self) -> str:
        """The name minus the organization, for a table that has an ORG column.

        `org_hint` is parsed back out of `name`, so rendering both in one row
        repeats the same eight characters twice. This drops the `#<org8>` half
        and keeps everything else — the `~<disambiguator>` included, because
        that half is load-bearing: it is what distinguishes two grants of one
        account, and `resolve_ref` needs it typed.

        Not the reference to feed back to `jailbee claude use`: for an account
        stored under two organizations that is `name`, and the ambiguity error
        names it. This is display only, like `email` and `org_hint`.
        """
        if self.email is None:
            return self.name
        suffix = "" if self.disambiguator is None else f"{DISAMBIGUATOR}{self.disambiguator}"
        return f"{self.email}{suffix}"


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


def group_name(cfg: Config) -> str | None:
    """The credential group this repo resolves to, or None when it shares none.

    The group name is the identity users think in — it is what they typed in
    `claude_credentials`, while the directory is a path they never chose. One
    definition so `members`, `doctor` and the CLI cannot disagree about which
    half of `claude_credentials_dir` is the name.
    """
    return None if cfg.claude_credentials_dir is None else cfg.claude_credentials_dir.name


def live_credential_path(cfg: Config) -> Path:
    """The live credential file for this repo's holder."""
    return holder_dir(cfg) / ".credentials.json"


def identity_file(home: Path) -> Path:
    """The config file carrying `oauthAccount`, mirroring Claude Code's own
    resolution: the legacy `.config.json` when it exists, else `.claude.json`.
    """
    legacy = home / ".config.json"
    return legacy if legacy.exists() else home / ".claude.json"


def read_account_record(home: Path) -> dict[str, Any] | None:
    """Claude Code's own `oauthAccount` block, or None when there is none.

    Every failure — absent, unreadable, torn, or missing the block — is None.
    Callers treat an unidentified account as a fact to report, not an error:
    a fresh group has no identity anywhere until something has run.

    `UnicodeDecodeError` is in the caught set because it is a `ValueError`, not
    an `OSError`: a write torn mid-character makes `read_text` raise it, and
    that is the same "unreadable file" fact as a torn JSON document.

    Returns the block verbatim, because `Identity` is a lossy reading of it:
    it keeps the email and the organization UUID and drops the rest, and
    `slug_for` truncates the UUID further. Restoring a record after a switch
    has to put back what Claude Code wrote, not what jailbee understood.
    """
    try:
        data = json.loads(identity_file(home).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    block = data.get("oauthAccount")
    return block if isinstance(block, dict) else None


def identity_of(record: dict[str, Any]) -> Identity | None:
    """The `Identity` an `oauthAccount` block names, or None when it names none."""
    email = record.get("emailAddress")
    if not isinstance(email, str) or not email:
        return None
    org = record.get("organizationUuid")
    return Identity(email=email, org_uuid=org if isinstance(org, str) and org else None)


def read_identity(home: Path) -> Identity | None:
    """The account a config home names, or None when it names none."""
    record = read_account_record(home)
    return None if record is None else identity_of(record)


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


_UNKNOWN_PREFIX = "unknown-"


def is_unidentified(name: str) -> bool:
    """Whether a slot name says nothing about which account it holds.

    True for the two emailless shapes: `unknown_slot_name`'s output and
    `LIVE_UNIDENTIFIED`. One definition, because `Slot.email` and the CLI's
    warning have to agree on what "unidentified" means — a name that reads as
    identified but warns, or the reverse, is worse than either.
    """
    derived = name.split(DISAMBIGUATOR, 1)[0]
    return derived == LIVE_UNIDENTIFIED or derived.startswith(_UNKNOWN_PREFIX)


def unknown_slot_name(when: datetime) -> str:
    """Name for parking a credential whose account cannot be identified.

    Self-healing rather than blocking: once that account is activated and used,
    its config home carries an identity, so the *next* park writes the real
    name.
    """
    return f"{_UNKNOWN_PREFIX}{when.strftime('%Y%m%d-%H%M%S')}"


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

ACCOUNT_RECORD_KEY = "jailbeeAccount"
"""Where a parked file keeps the `oauthAccount` block of the login it holds.

**Not a ledger.** The identity of a live credential is Claude Code's to record,
in a config home's `oauthAccount` — and `switch` has to invalidate that record,
or every member repo would go on naming the previous account. That leaves a
window in which no file on disk says which account the live credential belongs
to, and a `park` landing in it can only name the file `unknown-<timestamp>`,
losing the one record of what the file contains.

So the record travels *with the grant*: parking copies Claude Code's own
`oauthAccount` into the parked file, and activating writes it back. Because it
lives inside the file whose grant it describes, it cannot be orphaned: moving or
deleting the file moves or deletes the record with it, so `store_dir()` stays
the only state and there is no manifest to repair.

It *can*, however, disagree with the one other place the account is named — the
filename — because a file renamed by hand keeps the record it was written with.
The filename wins: `trusted_record_in` restores a record only while it derives
the slot's own name, and a mismatch degrades to the pre-record behaviour rather
than writing one account's identity under another's name.

Never written to a *live* credential: that file is Claude Code's, and it stays
the shape Claude Code wrote. `compose_credential` strips this key on the way
out. A parked file without it — a login jailbee never parked, or one parked
before this key existed — falls back to invalidating the record, which is what
every switch did before.
"""


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
        unrecognized = (
            data.keys() - SHARED_CREDENTIAL_KEYS - ACCOUNT_CREDENTIAL_KEYS - {ACCOUNT_RECORD_KEY}
        )
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

    **`live_shared` is filtered, not trusted.** Its only producer,
    `shared_fields`, already returns nothing else — but this is a public
    function, and a caller that passed a whole credential through would write
    the live account's `claudeAiOauth` into the target's file: one account's
    login stored under another's identity, and two files for one lineage. The
    allowlist costs a comprehension and closes that direction for good.

    `ACCOUNT_RECORD_KEY` is stripped on every path that produces a composed
    object, `live_shared is None` included: jailbee's own bookkeeping must not
    reach a file Claude Code reads.
    """
    target = _credential_object(target_raw)
    if target is None or "claudeAiOauth" not in target:
        # Nothing to strip from a blob that is not a credential object, and no
        # shared fields to compose into one.
        return target_raw
    composed = {
        k: v
        for k, v in target.items()
        if k != ACCOUNT_RECORD_KEY and (live_shared is None or k not in SHARED_CREDENTIAL_KEYS)
    }
    if live_shared is not None:
        composed.update({k: v for k, v in live_shared.items() if k in SHARED_CREDENTIAL_KEYS})
    return json.dumps(composed)


def _record_in(raw: str) -> dict[str, Any] | None:
    """The account record a slot's blob carries, or None when it carries none.

    None for every shape that is not a credential object with a dict under
    `ACCOUNT_RECORD_KEY` — a login that entered through `/login`, or one parked
    before the key existed. Callers fall back to invalidating the members'
    record, which is what every switch did before.
    """
    data = _credential_object(raw)
    if data is None:
        return None
    record = data.get(ACCOUNT_RECORD_KEY)
    return record if isinstance(record, dict) else None


def trusted_record_in(slot: Slot, raw: str) -> dict[str, Any] | None:
    """The slot's account record, but only while it agrees with the slot's name.

    **The filename stays authoritative.** Keeping the record beside the grant
    means the account is named twice for one file — in the name and in the
    record — and two records of one fact can differ. A slot renamed by hand,
    which is how `doctor`'s recovery advice works, changes the name and not the
    record; restoring that record would write one account into the members'
    config while the user believed they activated the other, silently.

    So a mismatch is treated as no record at all: the members' recorded account
    is invalidated instead, and Claude Code repopulates it from the credential
    now live. That is the pre-record behaviour — correct, only slower to
    display — so a disagreement costs an optimisation rather than causing a
    wrong write.

    Compared on the *derived* name, so a `~<disambiguator>` is not a mismatch:
    it separates two grants of one account, which by definition share an
    identity.
    """
    record = _record_in(raw)
    if record is None:
        return None
    identity = identity_of(record)
    if identity is None or slug_for(identity) != slot._derived:
        log.debug(
            "slot %s carries a record for a different account; invalidating instead",
            slot.name,
        )
        return None
    return record


def _slot_name(path: Path) -> str:
    """The slot name a store file carries: its filename without `.json`."""
    return path.name[: -len(_SLOT_SUFFIX)]


def parked_slots() -> list[Slot]:
    """Every stored login, sorted by name. An absent store is an empty pool."""
    store = store_dir()
    try:
        files = sorted(store.glob(f"*{_SLOT_SUFFIX}"))
    except OSError:
        return []
    return [Slot(name=_slot_name(p), path=p, live=False) for p in files]


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
            f"`{wanted}` is carried by {len(exact)} files ({where}), which jailbee's "
            "slot naming is supposed to make impossible — something else has written "
            "to the store. They may be two different logins, so nothing here can say "
            "which one you meant. Compare them yourself before moving or deleting "
            "either; `jailbee doctor` reports the store's state."
        )
    if exact:
        return exact[0]

    lowered = wanted.lower()
    by_email = [s for s in slots if s.email is not None and s.email == lowered]
    if len(by_email) == 1:
        return by_email[0]
    if len(by_email) > 1:
        names = ", ".join(sorted(s.name for s in by_email))
        raise PoolError(f"`{wanted}` matches several accounts: {names}. Pass the full slot name.")

    known = ", ".join(sorted(s.name for s in slots))
    raise PoolError(
        f"no stored account matches `{wanted}`."
        + (f" Known: {known}" if known else " The pool is empty.")
    )


def resolve_interactively(
    slots: Sequence[Slot],
    ref: str | None,
    *,
    purpose: str,
    picker: Callable[[Sequence[Slot]], str | None],
    is_interactive: Callable[[], bool],
) -> str | None:
    """The reference a `claude use`/`claude rm` invocation should act on.

    Returns a *reference* rather than a `Slot`, and both commands resolve it
    again: `switch` re-lists under the credential locks, and a Slot picked out
    here is a snapshot of a store another process may have changed since. One
    resolution is authoritative — the one holding the lock — and this is only
    how a user who typed no argument names their choice.

    `None` means the user cancelled the picker, which is not an error: callers
    abort quietly. The two genuine failures raise `PoolError` — nothing to
    choose from, and no TTY to choose on. The latter names the candidates, so a
    script's author learns the references from the failure itself.

    **The live slot is never a candidate.** `switch` refuses it and `rm`
    refuses it, so offering it would be offering a guaranteed error. That also
    makes an empty candidate list meaningfully different from an empty pool:
    a holder with one login and nothing parked has nothing to switch *to*.
    """
    if ref is not None:
        return ref
    parked = [s for s in slots if not s.live]
    if not parked:
        raise PoolError(
            f"no stored login to {purpose}. `jailbee claude park` stores the one in "
            "use, and the next `/login` in a container of this holder adds another."
        )
    if not is_interactive():
        names = ", ".join(sorted(s.name for s in parked))
        raise PoolError(f"specify <email|slot> explicitly (or run in a TTY): {names}")
    return picker(parked)


def resolve_removable(ref: str, slots: Sequence[Slot]) -> Slot:
    """The slot `jailbee claude rm` should act on.

    `resolve_ref` refuses a name carried by two files, because it cannot know
    which one a *switch* meant. `rm` never deletes a live login, so when the
    pair is one live slot and one parked file the question does not arise: only
    the parked file is a candidate. Without this the corruption would have no
    in-tool escape — the very error reporting it would also block the one
    command that clears it.
    """
    exact = [s for s in slots if s.name == ref.strip()]
    if len(exact) > 1:
        parked = [s for s in exact if not s.live]
        if len(parked) == 1:
            return parked[0]
    return resolve_ref(ref, slots)


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
    return sorted(prefix for prefix, _ in _registered_repos() if _resolves_to(gcfg, prefix, group))


def members(cfg: Config, gcfg: GlobalConfig) -> tuple[list[Member], list[str]]:
    """Every repo sharing `cfg`'s holder, plus the ones we could not read.

    A repo that shares nothing is its own only member, with no registry read.
    An unreadable member is *named*, not skipped: skipping is right for a
    read-only listing (`dashboard.py:240`), but here it would leave that
    repo's `oauthAccount` stale and silently naming the wrong account.

    **The calling repo is a member only when it resolves to this holder's
    group**, which is not a given: `cli._holder_view` hands us a `Config`
    pointed at *another* group, so that `jailbee claude use -g` can fill a
    holder no repo lives in. The config home in that view is still the calling
    repo's own, and it describes the login of the group that repo really uses —
    so counting it here would read one group's account for another
    (`unknown-<timestamp>` at best, the wrong name at worst) and, on the write
    side, destroy the naming evidence for the group the repo actually uses.
    Membership is decided by `_resolves_to` rather than by a registry row: a
    repo that was never registered, or whose rows were wiped, still reads the
    holder its own config resolves to.
    """
    from jailbee.config import load_config
    from jailbee.paths import repo_config_path

    me = Member(cfg.container_prefix, config_home(cfg))
    if cfg.claude_credentials_dir is None:
        return [me], []

    group = group_name(cfg)
    assert group is not None  # the None case returned above
    found = [me] if _resolves_to(gcfg, cfg.container_prefix, group) else []
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


@dataclass(frozen=True)
class LiveAccount:
    """The holder's live login: an account, and the record that names it.

    The identity and the record come from the *same* read, which is why they
    are one object: the name a park writes and the record it stores must
    describe the same account, and two separate reads could land on two
    different files.
    """

    identity: Identity
    record: dict[str, Any]


def _member_account(
    found: Sequence[Member],
    *,
    prefer: str,
    authoritative: Collection[str],
) -> LiveAccount | None:
    """The account a member repo's config home says the holder holds.

    Read from a config home, never from the credential. `authoritative`
    names the members whose config home can be trusted to describe *this
    holder's* login: a repo whose containers span two credential groups
    shares one `~/.claude` between them, so its `oauthAccount` names
    whichever account ran most recently, and naming a parked file from it
    would store one account's grant under another's name. See
    `claude_groups.authoritative_prefixes`, which is its only producer.

    The calling repo is consulted first among the authoritative ones; any
    of them will do, since they share one login. None means no
    authoritative member names an account — a fresh group, the window a
    `switch` opens before any container has run Claude again (which is what
    `ACCOUNT_RECORD_KEY` exists to close), or a holder no repo resolves to.
    Callers want `live_account`, which falls back to the holder's own note.
    """
    usable = [m for m in found if m.container_prefix in authoritative]
    ordered = sorted(usable, key=lambda m: m.container_prefix != prefer)
    for member in ordered:
        record = read_account_record(member.config_home)
        if record is None:
            continue
        identity = identity_of(record)
        if identity is not None:
            return LiveAccount(identity=identity, record=record)
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
    updated: list[str]
    """Members whose recorded account now agrees with the holder.

    "Updated" rather than "cleared": an activation *writes* the record the slot
    was carrying (see `ACCOUNT_RECORD_KEY`), and only a `park` — or a slot with
    no record — deletes it. Both leave the member correct, which is the fact the
    CLI reports.
    """
    not_updated: list[str]
    """Members still naming the previous account, unreadable ones included."""
    live_sessions: list[str]


def _fsync_dir(path: Path) -> None:
    """Make a rename in `path` durable.

    Best-effort: not every filesystem allows opening a directory for fsync,
    and failing to harden a write is not a reason to fail the operation.
    """
    try:
        fd = os.open(str(path), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def _fsync_file(path: Path) -> None:
    """fsync a file by path. Not best-effort: callers use it where the file is
    about to become the only copy of a grant."""
    fd = os.open(str(path), os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _atomic_write(path: Path, text: str) -> None:
    """Replace `path` atomically and durably, mode 0600.

    The temporary file is created in the destination directory so the replace
    is a same-filesystem rename, and its mode is set *before* the replace so
    the final path is never briefly world-readable. The content is fsynced
    before the rename and the directory after it: once the live credential has
    been parked, this file is the only copy of that grant, so "atomic" has to
    mean "survives a crash", not merely "no torn reader".

    `mkstemp`'s descriptor is closed immediately and the file reopened by
    name rather than wrapped with `os.fdopen`: if `fdopen` itself raised, that
    descriptor would leak. `mkstemp` already creates the file at 0600 with a
    name unique to us, so reopening it by name races nothing.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    os.close(fd)
    tmp = Path(name)
    try:
        with open(tmp, "wb") as handle:
            handle.write(text.encode("utf-8"))
            handle.flush()
            os.fsync(handle.fileno())
        tmp.chmod(0o600)
        tmp.replace(path)
        _fsync_dir(path.parent)
    except BaseException:
        with suppress(OSError):
            tmp.unlink()
        raise


def _move_file(src: Path, dest: Path) -> None:
    """Move a credential file, atomically where the filesystem allows it.

    `os.replace` is atomic but raises `EXDEV` across filesystems, which is
    reachable here because `shared_dir` is user-overridable. The fallback is
    written out rather than delegated to `shutil.move` so its failure window is
    ours to close: a copy that fails leaves no partial file at the destination
    to block a later park, and the source is unlinked only once the copy is
    fsynced to disk — not merely copied, since a crash between an unsynced
    copy and the source's unlink would leave a durable directory entry
    pointing at data that was never written. The copy-then-unlink window is
    the one moment a grant exists twice, and it is unavoidable across
    filesystems.
    """
    try:
        os.replace(src, dest)
        return
    except OSError as e:
        if e.errno != errno.EXDEV:
            raise
    try:
        shutil.copy2(src, dest)
        _fsync_file(dest)
        _fsync_dir(dest.parent)
    except BaseException:
        with suppress(OSError):
            dest.unlink()
        raise
    try:
        src.unlink()
    except BaseException:
        with suppress(OSError):
            dest.unlink()
        raise


def _login_of(path: Path) -> dict[str, Any] | None:
    """The `claudeAiOauth` block of a credential file, for identity comparison.

    Read only to answer "are these two files the same grant?" — a question the
    slot name cannot answer, because a config home's `oauthAccount` is allowed
    to lag the credential beside it. Never logged, never rendered, never
    returned to anything that displays it.

    Every unreadable shape is None, `UnicodeDecodeError` included: it is a
    `ValueError` rather than an `OSError`, so a write torn mid-character would
    otherwise escape as a traceback from a call whose whole job is to answer a
    yes/no question. Callers must treat None as "cannot tell" and fail closed,
    never as "different".
    """
    try:
        return _login_block(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError):
        return None


def _login_block(raw: str | None) -> dict[str, Any] | None:
    """The `claudeAiOauth` block of credential *text* — `_login_of` for a path.

    One definition of "the login inside a credential", so the fingerprint a
    note is written with and the one it is checked against cannot be read out
    of two differently-shaped dicts.
    """
    data = _credential_object(raw)
    if data is None:
        return None
    block = data.get("claudeAiOauth")
    return block if isinstance(block, dict) else None


def _same_grant(left: dict[str, Any], right: dict[str, Any]) -> bool:
    """Whether two `claudeAiOauth` blocks are one refresh-token lineage.

    Equal blocks are trivially the same grant. A shared, non-empty
    `refreshToken` is the stronger test and the reason this is not just `==`:
    an access token rotates while the lineage behind it does not, so two blocks
    can differ field by field and still be one login in two files — the exact
    state this module exists to prevent.
    """
    if left == right:
        return True
    token = left.get("refreshToken")
    return isinstance(token, str) and bool(token) and token == right.get("refreshToken")


def holds_same_login(left: Path, right: Path) -> bool:
    """Whether two credential files carry one refresh-token lineage.

    False whenever either file is missing, unreadable or carries no login.
    Callers use this to *soften* a warning (`doctor._orphaned_stage_checks`),
    so "cannot tell" has to read as "cannot tell" and never as "yes". The
    blocks are compared and discarded; nothing about them is logged or
    returned.
    """
    a = _login_of(left)
    b = _login_of(right)
    return a is not None and b is not None and _same_grant(a, b)


ACCOUNT_NOTE_FILE = ".jailbee-account.json"
"""Where a holder keeps the account of the login *jailbee* put there.

**Why a second place at all.** Every other identity source is a config home,
and a config home belongs to a *repo*, not to a holder: one `~/.claude` is
shared by every container of the repo whatever group each reads, so it can name
only one account while such a repo has two live logins. For a group no repo
resolves to — the one `jailbee claude use -g` exists to fill — there is no
config home to read at all, and a `park` of a login jailbee had itself just
activated could only name the file `unknown-<timestamp>`, losing the one record
of what it contains (`ACCOUNT_RECORD_KEY` documents the same loss for the other
window it closes).

**Why it cannot go stale into a wrong name.** The note carries a fingerprint of
the grant it describes — a digest of `claudeAiOauth.refreshToken`, the same
refresh-token lineage `_same_grant` compares — and `note_account` returns
nothing unless it still matches the credential beside it. A `/login` in a
container mints a new lineage, so a note left over from the previous account
stops being read the moment it stops being true. The digest, never the token:
this file names an account, and a second copy of a secret is exactly what this
module refuses to make elsewhere.

**Trust.** A group holder is mounted into its containers, so a container can
write this file. That is the same trust level as the `.claude.json` every
identity read already comes from, and the account it names goes through
`slug_for` like any other, so a forged note can misname a parked file and can
do nothing else — no new surface.

Not a manifest: it describes the one directory it lives in, so it moves and
dies with the holder, and nothing has to repair it.
"""


def account_note_path(holder: Path) -> Path:
    """Where `holder` keeps its account note."""
    return holder / ACCOUNT_NOTE_FILE


def _grant_fingerprint(login: dict[str, Any] | None) -> str | None:
    """A stable id for a login's refresh-token lineage, or None for no lineage.

    Access tokens rotate; the refresh token behind them does not (the property
    `_same_grant` already rests on), so this survives every ordinary token
    refresh and changes on a fresh `/login`. Hashed so the note holds no
    secret, and truncation would only weaken a comparison that costs nothing.

    None for a credential carrying no refresh token — a managed `sk-ant-…` key,
    or any opaque shape — which leaves such a holder with no note and the
    pre-note behaviour.
    """
    token = None if login is None else login.get("refreshToken")
    if not isinstance(token, str) or not token:
        return None
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def write_account_note(holder: Path, record: dict[str, Any] | None, credential_raw: str) -> None:
    """Note that `credential_raw` — now the login in `holder` — is `record`'s.

    Removes any existing note when there is nothing to say: no record (a slot
    parked before `ACCOUNT_RECORD_KEY` existed, or one whose record contradicts
    its name) or no fingerprintable grant. Leaving the previous account's note
    in place would be harmless — the fingerprint no longer matches — but a file
    that says something untrue about the directory it sits in is worth deleting
    rather than explaining.

    Best-effort, like `_stamp_account_record` and for the same reason: the
    credential is already in place by the time this runs, and a failure costs a
    future `park` its account name, never the login.
    """
    path = account_note_path(holder)
    fingerprint = _grant_fingerprint(_login_block(credential_raw))
    if record is None or fingerprint is None:
        with suppress(OSError):
            path.unlink(missing_ok=True)
        return
    try:
        _atomic_write(path, json.dumps({"account": record, "grant": fingerprint}, indent=2))
    except OSError:
        log.debug("could not note the account of the login in %s", holder, exc_info=True)


def note_account(cfg: Config) -> LiveAccount | None:
    """The account this holder's note names, while it still describes the grant.

    None for every other case: no note, an unreadable or malformed one, one
    whose fingerprint no longer matches the credential beside it, and one whose
    record names no account. A missing credential is a mismatch too, so the
    note a `park` failed to delete cannot name a holder's next login.
    """
    try:
        data = json.loads(account_note_path(holder_dir(cfg)).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    record = data.get("account")
    grant = data.get("grant")
    if not isinstance(record, dict) or not isinstance(grant, str):
        return None
    if grant != _grant_fingerprint(_login_of(live_credential_path(cfg))):
        return None
    identity = identity_of(record)
    return None if identity is None else LiveAccount(identity=identity, record=record)


def live_account(
    cfg: Config,
    found: Sequence[Member],
    *,
    prefer: str,
    authoritative: Collection[str],
) -> LiveAccount | None:
    """The account the holder's live credential belongs to, with its record.

    Two sources, and the holder's own note comes first: it is the only one tied
    to the grant being named — `note_account` checks its fingerprint against
    the very credential a `park` is about to move — while a config home is tied
    to a repo and merely *usually* describes this holder (see
    `_member_account`). Where both speak they agree; where they disagree the
    note is the one that was verified.

    None means nothing on this host says which account the live credential
    holds. That is a fact to report, not an error: `park` then names the file
    `unknown_slot_name`, and `ls` shows `LIVE_UNIDENTIFIED`.
    """
    return note_account(cfg) or _member_account(found, prefer=prefer, authoritative=authoritative)


def live_identity(
    cfg: Config,
    found: Sequence[Member],
    *,
    prefer: str,
    authoritative: Collection[str],
) -> Identity | None:
    """The identity half of `live_account`, for callers that only display it."""
    account = live_account(cfg, found, prefer=prefer, authoritative=authoritative)
    return None if account is None else account.identity


def _disambiguated_slot(store: Path, name: str, live: Path, dest: Path, when: datetime) -> Path:
    """A free store path for a login whose derived slot name is taken.

    **A taken name is not a duplicate login.** One account can hold two
    independent grants — `/login` as the same account after a `park` is the
    documented way to add one, and two holders on a host can each be logged
    into it. Refusing the park would be wrong twice over: the second grant has
    nowhere to go, and because this check runs on every switch through the
    holder, the refusal would freeze the pool for *every other* account too
    until someone did filesystem surgery. So a differing grant gets a
    `~<timestamp>` suffix (`Slot` documents the grammar).

    Only two cases raise, and both are about the invariant rather than the
    name:

    - **the same grant is already stored** — the one case where a second file
      really would give one refresh-token lineage two refreshers;
    - **either file is unreadable** — the question is unanswerable, so this
      fails closed rather than guessing. It must not claim the two are copies.

    Every file sharing the derived name is compared, not just `dest`: after one
    disambiguation the lineage could otherwise be parked a second time under a
    third name.
    """
    live_grant = _login_of(live)
    taken = sorted({dest, *store.glob(f"{name}{DISAMBIGUATOR}*{_SLOT_SUFFIX}")})
    for other in taken:
        other_grant = _login_of(other)
        if live_grant is None or other_grant is None:
            raise PoolError(
                f"the store already holds `{_slot_name(other)}` ({other}), and jailbee "
                "could not read both files to tell whether that is the same login as "
                "the one being parked. Nothing was moved; the live credential is still "
                "in place. Compare the two files, and remove the stored one with "
                f"`jailbee claude rm {_slot_name(other)}` if it is the stale copy."
            )
        if _same_grant(live_grant, other_grant):
            raise PoolError(
                f"the login being parked is already stored as `{_slot_name(other)}` "
                f"({other}). Parking it again would leave one refresh-token lineage in "
                "two files, and the first token rotation would kill one of them. "
                f"Run `jailbee claude rm {_slot_name(other)}` first if the stored copy "
                "is not the one to keep."
            )
    stamp = when.strftime("%Y%m%d-%H%M%S")
    candidate = store / f"{name}{DISAMBIGUATOR}{stamp}{_SLOT_SUFFIX}"
    attempt = 2
    while candidate.exists():
        candidate = store / f"{name}{DISAMBIGUATOR}{stamp}-{attempt}{_SLOT_SUFFIX}"
        attempt += 1
    return candidate


def _slots_for(
    cfg: Config, found: Sequence[Member], authoritative: Collection[str]
) -> tuple[list[Slot], LiveAccount | None]:
    """Every slot for this holder, the live account alongside.

    The live slot's name is derived from an identity, so it can equal a parked
    slot's — that is precisely the state the documented add flow leaves behind:
    `park`, then `/login` as the same account. Two slots with one name make
    every `resolve_ref` for it an error, wedging the holder, so the live one
    takes the `~live` form `Slot` documents. `live` rather than a timestamp
    because there is only ever one of them, and it reads as what it is in
    `jailbee claude ls`.
    """
    account = live_account(cfg, found, prefer=cfg.container_prefix, authoritative=authoritative)
    slots = parked_slots()
    live = live_slot(cfg, None if account is None else account.identity)
    if live is not None:
        if any(s.name == live.name for s in slots):
            live = replace(live, name=f"{live.name}{DISAMBIGUATOR}live")
        slots.append(live)
    return sorted(slots, key=lambda s: (not s.live, s.name)), account


def list_slots(cfg: Config, gcfg: GlobalConfig, *, authoritative: Collection[str]) -> list[Slot]:
    """Every stored login, the live one first."""
    found, _ = members(cfg, gcfg)
    return _slots_for(cfg, found, authoritative)[0]


def invalidate_identity(home: Path) -> bool:
    """Delete `oauthAccount` so Claude Code repopulates it from the credential.

    Returns whether the config home is now consistent — True also when there
    was nothing to delete. False means the file exists but could not be read
    or written; it is **never** overwritten in that case, because a torn
    `.claude.json` still holds the user's projects and MCP servers and an
    `or {}` here would erase them.

    `UnicodeDecodeError` is caught alongside the rest for the reason
    `read_identity` gives, and matters more here: this runs from inside
    `switch` *after* the credential files have moved, so an escaping exception
    would report a failed switch that had already landed.
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
    except (ClaudeLockTimeoutError, OSError, UnicodeDecodeError, json.JSONDecodeError):
        return False
    return True


def restore_identity(home: Path, record: dict[str, Any]) -> bool:
    """Write `record` back as this config home's `oauthAccount`.

    The counterpart to `invalidate_identity`, with the same contract: True when
    the config home is consistent afterwards, False when the file exists but
    could not be read or written — and in that case it is **never** overwritten,
    because a torn `.claude.json` still holds the user's projects and MCP
    servers.

    An absent file is True and left absent, exactly as in `invalidate_identity`:
    creating Claude Code's config here would be a write nobody asked for, and
    Claude Code writes the account itself from the credential it finds.
    """
    path = identity_file(home)
    try:
        with config_lock(home):
            if not path.exists():
                return True
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                return False
            if data.get("oauthAccount") == record:
                return True
            data["oauthAccount"] = record
            _atomic_write(path, json.dumps(data, indent=2))
    except (ClaudeLockTimeoutError, OSError, UnicodeDecodeError, json.JSONDecodeError):
        return False
    return True


def _rewrite_identities(
    found: Sequence[Member],
    unreachable: Sequence[str],
    record: dict[str, Any] | None,
    authoritative: Collection[str],
) -> tuple[list[str], list[str]]:
    """Point every member's recorded account at the login now live.

    With `record`, that means writing it: the activated slot carried Claude
    Code's own block (see `ACCOUNT_RECORD_KEY`), so the members can be made
    correct immediately instead of merely not-wrong. Without one — a login
    jailbee never parked, or one parked before the key existed — the record is
    deleted and Claude Code repopulates it on its next run, which is what every
    switch did before.

    **A record is written only into an authoritative member**, for the same
    reason `live_account` reads only those: a repo whose containers span two
    groups shares one config home between them, so stamping this group's
    account into it would make that home name the wrong login for the other
    group's containers — and a later `park` of *that* group would then park it
    under the wrong name, name and record agreeing. Every other member is
    cleared instead, which is always safe: Claude Code repopulates the block
    from whichever credential the container actually reads.

    Reports which members took the change, so the caller can name the ones that
    are still naming the previous account.
    """
    done: list[str] = []
    failed: list[str] = list(unreachable)
    for member in found:
        trusted = member.container_prefix in authoritative
        ok = (
            restore_identity(member.config_home, record)
            if record is not None and trusted
            else invalidate_identity(member.config_home)
        )
        (done if ok else failed).append(member.container_prefix)
    return sorted(done), sorted(failed)


def _stamp_account_record(path: Path, record: dict[str, Any] | None) -> None:
    """Keep `record` inside the newly parked file, best-effort.

    Best-effort on purpose: the login is already safely in the store by the
    time this runs, and a failure here costs a future `park` its account name —
    the state this whole mechanism improves on, never worse than it. Raising
    would report a failed park that had in fact landed, which is the one
    outcome worth avoiding.
    """
    if record is None:
        return
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return
        data[ACCOUNT_RECORD_KEY] = record
        _atomic_write(path, json.dumps(data))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        log.debug("could not record the account of the login parked at %s", path, exc_info=True)


def _park_locked(cfg: Config, account: LiveAccount | None, when: datetime) -> Path | None:
    """Move the live credential into the store; return where it landed.

    `account` carries both halves of what a park needs: the identity that names
    the file, and Claude Code's own `oauthAccount` to keep inside it so a later
    activation can restore it (see `ACCOUNT_RECORD_KEY`). One parameter rather
    than two, so a caller cannot pair a name with another account's record.

    `_move_file` rather than `Path.replace`: `shared_dir` is user-overridable,
    so the holder and the store can live on different filesystems, where a
    rename raises `EXDEV`. Same-filesystem moves stay atomic renames, and the
    cross-filesystem fallback has its own bounded, recoverable failure window.

    Returns the path rather than the name because after `_disambiguated_slot`
    the two are no longer interchangeable: `switch`'s rollback has to move back
    the file that was actually written, not the one its name was derived from.

    The holder's account note goes with the grant it describes: the record it
    carried is now stamped inside the parked file, and the holder holds nothing
    for a note to be about. `switch` writes the incoming login's note after
    this, and restores this one if the activation fails.
    """
    live = live_credential_path(cfg)
    if not live.exists():
        return None
    name = slug_for(account.identity) if account is not None else unknown_slot_name(when)
    store = store_dir()
    store.mkdir(parents=True, exist_ok=True)
    dest = store / f"{name}{_SLOT_SUFFIX}"
    if dest.exists():
        dest = _disambiguated_slot(store, name, live, dest, when)
    _move_file(live, dest)
    _stamp_account_record(dest, None if account is None else account.record)
    with suppress(OSError):
        account_note_path(holder_dir(cfg)).unlink(missing_ok=True)
    return dest


def park(
    cfg: Config,
    gcfg: GlobalConfig,
    *,
    authoritative: Collection[str],
    now: datetime | None = None,
) -> PoolChange:
    """Store the live login and leave the holder empty.

    This is how a *new* account enters the pool: with no credential to find,
    the next `claude` in any member container prompts `/login`, and that login
    lands straight in the holder.

    Nothing is created on disk until there is something to park. Taking the
    lock would create the holder and both lock directories as a side effect of
    discovering the holder is empty — a write nobody asked for, in the one case
    where the command does nothing. The check is repeated under the lock by
    `_park_locked`, which is what makes it safe to do it early.
    """
    found, unreachable = members(cfg, gcfg)
    account = live_account(cfg, found, prefer=cfg.container_prefix, authoritative=authoritative)
    parked: Path | None = None
    if live_credential_path(cfg).exists():
        holder = holder_dir(cfg)
        holder.mkdir(parents=True, exist_ok=True)
        with credential_locks(holder):
            parked = _park_locked(cfg, account, now or datetime.now())
    if parked is None:
        return PoolChange(
            parked_as=None,
            activated=None,
            updated=[],
            not_updated=list(unreachable),
            live_sessions=[],
        )
    # No record to restore: `park` leaves the holder empty on purpose, so there
    # is no live login for the members to name.
    updated, not_updated = _rewrite_identities(found, unreachable, None, authoritative)
    return PoolChange(
        parked_as=_slot_name(parked),
        activated=None,
        updated=updated,
        not_updated=not_updated,
        live_sessions=live_session_prefixes(found),
    )


def switch(
    cfg: Config,
    gcfg: GlobalConfig,
    ref: str,
    *,
    authoritative: Collection[str],
    now: datetime | None = None,
) -> PoolChange:
    """Park the live login and activate a stored one.

    The target is renamed out of the store *before* anything else moves, so no
    failure path can leave one grant in two files. On any error both files go
    back where they were.

    A hard kill — which no `except` can catch — is the one thing that leaves
    the staging file behind. It stays exactly where it is: `jailbee doctor`
    names it and the rename that recovers it (see the module docstring for why
    this is reported rather than repaired).

    `target_raw` is read *before* the file moves and is what the members'
    recorded account is rewritten from, so an activation restores the record
    the slot was carrying rather than deleting what the previous account left.
    """
    found, unreachable = members(cfg, gcfg)
    slots, account = _slots_for(cfg, found, authoritative)
    target = resolve_ref(ref, slots)
    if target.live:
        raise PoolError(f"`{target.name}` is already the live account for this holder.")

    holder = holder_dir(cfg)
    holder.mkdir(parents=True, exist_ok=True)
    live_path = live_credential_path(cfg)
    staged = target.path.with_name(target.path.name + ".activating")

    with credential_locks(holder):
        target_raw = target.path.read_text(encoding="utf-8")
        record = trusted_record_in(target, target_raw)
        live_raw = live_path.read_text(encoding="utf-8") if live_path.exists() else None
        target.path.replace(staged)
        parked: Path | None = None
        try:
            parked = _park_locked(cfg, account, now or datetime.now())
            activated = compose_credential(target_raw, shared_fields(live_raw))
            _atomic_write(live_path, activated)
            staged.unlink()
            # Under the lock, and last: the note describes what is now in the
            # holder, so it must not exist before the credential it names does.
            write_account_note(holder, record, activated)
        except BaseException:
            # The target first: a same-directory rename cannot fail for EXDEV,
            # while putting the live credential back can, and a failure there
            # must not strand the target under a name nothing lists.
            with suppress(OSError):
                staged.replace(target.path)
            if parked is not None:
                _move_file(parked, live_path)
                # `_park_locked` took the note with the grant; both go back.
                if live_raw is not None:
                    write_account_note(
                        holder, None if account is None else account.record, live_raw
                    )
            elif live_raw is None:
                # The holder started empty and nothing was parked, so whatever
                # is at live_path is the grant we just wrote — and the target
                # is back in the store. Removing it restores the empty holder
                # and keeps one file per grant.
                with suppress(OSError):
                    live_path.unlink()
            raise

    updated, not_updated = _rewrite_identities(found, unreachable, record, authoritative)
    return PoolChange(
        parked_as=_slot_name(parked) if parked is not None else None,
        activated=target.name,
        updated=updated,
        not_updated=not_updated,
        live_sessions=live_session_prefixes(found),
    )


def live_account_refusal(name: str) -> str:
    """The one wording for "that slot is the live login, park it first".

    `cli.claude_rm_cmd` refuses before it prompts, so the user is not asked to
    confirm a deletion that was never going to happen; `remove_slot` refuses
    again because it is callable without the CLI. Two sites, one sentence.
    """
    return f"`{name}` is the live account — run `jailbee claude park` first."


def remove_slot(slot: Slot) -> None:
    """Delete a parked login permanently."""
    if slot.live:
        raise PoolError(live_account_refusal(slot.name))
    slot.path.unlink()
