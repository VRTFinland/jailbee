"""jailbee's own Claude Code account pool.

A holder — a credential *group* directory, or a single repo's config home when
it shares none — has at most one live login. Every other stored login sits in a
host-wide store as a plain file. Switching moves one file out and another in,
then deletes the `oauthAccount` block from each member repo's `.claude.json`
so Claude Code repopulates it from the credential it now finds.

**The generic half of this module now lives in `accounts/engine.py`**, which is
parameterised by an `AccountAdapter`. Every name below that the engine owns is
kept here as a binding of that engine to `CLAUDE`, so callers and tests keep the
module they have always used. What stays is what is Claude's alone — identity,
the account note, credential composition, the member rewrite — until Task 4
moves it into `accounts/adapters/claude.py`.

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

import json
import logging
from collections.abc import Callable, Collection, Sequence
from contextlib import suppress
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from jailbee.accounts import engine
from jailbee.accounts.adapters.claude import CLAUDE

# The engine's filesystem primitives under this module's old names, re-bound
# rather than wrapped like every other moved name below: they take no adapter,
# so there is nothing to bind, and a wrapper would only add a frame a caller
# that captures one could recurse through. What is left here calls
# `engine._atomic_write` qualified rather than through this binding, so the
# engine's is the single name that has to be patched to intercept a write —
# on either side of the split, as one patch of `claude_pool`'s own once did.
from jailbee.accounts.engine import _atomic_write as _atomic_write
from jailbee.accounts.engine import _fsync_dir as _fsync_dir
from jailbee.accounts.engine import _fsync_file as _fsync_file
from jailbee.accounts.engine import _move_file as _move_file
from jailbee.accounts.models import DISAMBIGUATOR as DISAMBIGUATOR
from jailbee.accounts.models import LIVE_UNIDENTIFIED as LIVE_UNIDENTIFIED
from jailbee.accounts.models import Identity as Identity
from jailbee.accounts.models import LiveAccount as LiveAccount
from jailbee.accounts.models import Member as Member
from jailbee.accounts.models import PoolChange as PoolChange
from jailbee.accounts.models import PoolError as PoolError
from jailbee.accounts.models import Slot as Slot
from jailbee.accounts.models import _SLOT_SUFFIX as _SLOT_SUFFIX
from jailbee.accounts.models import is_unidentified as is_unidentified
from jailbee.accounts.models import slug_for as slug_for
from jailbee.accounts.models import unknown_slot_name as unknown_slot_name
from jailbee.claude_locks import ClaudeLockTimeoutError, config_lock

if TYPE_CHECKING:
    from jailbee.config import Config
    from jailbee.global_config import GlobalConfig

log = logging.getLogger(__name__)


def store_dir() -> Path:
    """The host-wide parked-credential store."""
    return engine.store_dir(CLAUDE)


def config_home(cfg: Config) -> Path:
    """This repo's Claude config home on the host — never shared."""
    assert cfg.shared_dir is not None  # set by load_config
    return cfg.shared_dir / "claude"


def holder_dir(cfg: Config) -> Path:
    """The directory whose `.credentials.json` this repo's containers read."""
    return engine.holder_dir(CLAUDE, cfg)


def group_name(cfg: Config) -> str | None:
    """The credential group this repo resolves to, or None when it shares none.

    The group name is the identity users think in — it is what they typed in
    `claude_credentials`, while the directory is a path they never chose. One
    definition so `members`, `doctor` and the CLI cannot disagree about which
    half of `claude_credentials_dir` is the name.
    """
    return engine.repo_group(cfg)


CREDENTIAL_FILE = ".credentials.json"
"""The filename Claude Code reads a login from, inside whichever holder it uses."""


def credential_in(holder: Path) -> Path:
    """The live credential file inside `holder`."""
    return engine.credential_in(CLAUDE, holder)


def live_credential_path(cfg: Config) -> Path:
    """The live credential file for this repo's holder."""
    return engine.live_credential_path(CLAUDE, cfg)


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
    return engine._slot_name(path)


def parked_slots() -> list[Slot]:
    """Every stored login, sorted by name. An absent store is an empty pool."""
    return engine.parked_slots(CLAUDE)


def live_slot_at(holder: Path, identity: Identity | None) -> Slot | None:
    """`holder`'s live login, or None when nothing is logged in there."""
    return engine.live_slot_at(CLAUDE, holder, identity)


def live_slot(cfg: Config, identity: Identity | None) -> Slot | None:
    """The holder's live login, or None when nothing is logged in."""
    return engine.live_slot(CLAUDE, cfg, identity)


def resolve_ref(ref: str, slots: Sequence[Slot]) -> Slot:
    """The slot a user-typed reference names."""
    return engine.resolve_ref(ref, slots)


def resolve_interactively(
    slots: Sequence[Slot],
    ref: str | None,
    *,
    purpose: str,
    picker: Callable[[Sequence[Slot]], str | None],
    is_interactive: Callable[[], bool],
) -> str | None:
    """The reference a `claude use`/`claude rm` invocation should act on."""
    return engine.resolve_interactively(
        slots, ref, purpose=purpose, picker=picker, is_interactive=is_interactive
    )


def resolve_removable(ref: str, slots: Sequence[Slot]) -> Slot:
    """The slot `jailbee claude rm` should act on."""
    return engine.resolve_removable(ref, slots)


def registered_repos() -> list[tuple[str, Path]]:
    """Every registered repo as (container_prefix, repo_root)."""
    return engine.registered_repos()


def _resolves_to(gcfg: GlobalConfig, prefix: str, group: str) -> bool:
    """Whether `prefix` resolves to `group` under this host's config."""
    return engine._resolves_to(gcfg, prefix, group)


def group_member_prefixes(gcfg: GlobalConfig, group: str) -> list[str]:
    """Registered repos resolving to `group`, sorted, including the caller."""
    return engine.group_member_prefixes(gcfg, group)


def members(cfg: Config, gcfg: GlobalConfig) -> tuple[list[Member], list[str]]:
    """Every repo sharing `cfg`'s holder, plus the ones we could not read."""
    return engine.members(CLAUDE, cfg, gcfg)


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
    """Members that look like they have a Claude Code session running."""
    return engine.live_session_prefixes(found)


def _login_of(path: Path) -> dict[str, Any] | None:
    """The `claudeAiOauth` block of a credential file, for identity comparison."""
    return engine._login_of(CLAUDE, path)


def _login_block(raw: str | None) -> dict[str, Any] | None:
    """The `claudeAiOauth` block of credential *text* — `_login_of` for a path.

    One definition of "the login inside a credential", so the fingerprint a
    note is written with and the one it is checked against cannot be read out
    of two differently-shaped dicts.

    This is what `ClaudeAdapter.grant_block` answers with, and through it every
    lineage comparison the engine makes; Task 4 moves the body into the adapter.
    """
    data = _credential_object(raw)
    if data is None:
        return None
    block = data.get("claudeAiOauth")
    return block if isinstance(block, dict) else None


def _same_grant(left: dict[str, Any], right: dict[str, Any]) -> bool:
    """Whether two `claudeAiOauth` blocks are one refresh-token lineage."""
    return engine._same_grant(CLAUDE, left, right)


def holds_same_login(left: Path, right: Path) -> bool:
    """Whether two credential files carry one refresh-token lineage."""
    return engine.holds_same_login(CLAUDE, left, right)


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
    """A stable id for a login's refresh-token lineage, or None for no lineage."""
    return engine._grant_fingerprint(CLAUDE, login)


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
        engine._atomic_write(path, json.dumps({"account": record, "grant": fingerprint}, indent=2))
    except OSError:
        log.debug("could not note the account of the login in %s", holder, exc_info=True)


def note_account_at(holder: Path) -> LiveAccount | None:
    """The account `holder`'s note names, while it still describes the grant.

    None for every other case: no note, an unreadable or malformed one, one
    whose fingerprint no longer matches the credential beside it, and one whose
    record names no account. A missing credential is a mismatch too, so the
    note a `park` failed to delete cannot name a holder's next login.
    """
    try:
        data = json.loads(account_note_path(holder).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    record = data.get("account")
    grant = data.get("grant")
    if not isinstance(record, dict) or not isinstance(grant, str):
        return None
    if grant != _grant_fingerprint(_login_of(credential_in(holder))):
        return None
    identity = identity_of(record)
    return None if identity is None else LiveAccount(identity=identity, record=record)


def note_account(cfg: Config) -> LiveAccount | None:
    """The account this repo's holder notes, while it still describes the grant."""
    return note_account_at(holder_dir(cfg))


def account_of(
    holder: Path,
    found: Sequence[Member],
    *,
    prefer: str,
    authoritative: Collection[str],
) -> LiveAccount | None:
    """The account `holder`'s live credential belongs to, with its record.

    Two sources, and the holder's own note comes first: it is the only one tied
    to the grant being named — `note_account_at` checks its fingerprint against
    the very credential a `park` is about to move — while a config home is tied
    to a repo and merely *usually* describes this holder (see
    `_member_account`). Where both speak they agree; where they disagree the
    note is the one that was verified.

    None means nothing on this host says which account the live credential
    holds. That is a fact to report, not an error: `park` then names the file
    `unknown_slot_name`, and `ls` shows `LIVE_UNIDENTIFIED`.
    """
    return note_account_at(holder) or _member_account(
        found, prefer=prefer, authoritative=authoritative
    )


def live_account(
    cfg: Config,
    found: Sequence[Member],
    *,
    prefer: str,
    authoritative: Collection[str],
) -> LiveAccount | None:
    """The account this repo's holder holds, with its record."""
    return account_of(holder_dir(cfg), found, prefer=prefer, authoritative=authoritative)


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
    """A free store path for a login whose derived slot name is taken."""
    return engine._disambiguated_slot(CLAUDE, store, name, live, dest, when)


def _slots_for(
    cfg: Config, found: Sequence[Member], authoritative: Collection[str]
) -> tuple[list[Slot], LiveAccount | None]:
    """Every slot for this holder, the live account alongside."""
    return engine._slots_for(CLAUDE, cfg, found, authoritative)


def list_slots(cfg: Config, gcfg: GlobalConfig, *, authoritative: Collection[str]) -> list[Slot]:
    """Every stored login, the live one first."""
    return engine.list_slots(CLAUDE, cfg, gcfg, authoritative=authoritative)


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
            engine._atomic_write(path, json.dumps(data, indent=2))
    except (ClaudeLockTimeoutError, OSError, UnicodeDecodeError, json.JSONDecodeError):
        return False
    return True


ONBOARDING_KEYS = ("hasCompletedOnboarding", "oauthAccount")
"""The keys whose presence means Claude Code has already run in a config home.

Either one is enough. `hasCompletedOnboarding` is what Claude Code's own
first-run wizard is gated on, and `oauthAccount` is written the moment a login
lands — a config home carrying either is the user's state, never a seed, and
`mark_onboarded` refuses to touch it.
"""


def mark_onboarded(home: Path, *, repo_dir: str) -> bool:
    """Record in `home` that the first-run wizard need not run, and why it can.

    Claude Code's wizard is gated on `hasCompletedOnboarding` alone: it asks
    for a login even when a valid credential is mounted at
    `CLAUDE_SECURESTORAGE_CONFIG_DIR`, because it never looks. A fresh config
    home therefore sends the user through `/login` for an account they are
    already logged into — the whole cost of a new container in a repo whose
    credential group holds a login. Deciding *whether* that credential exists
    is the caller's job (`init_command._seed_claude_json`); this function only
    writes.

    `repo_dir` is the repo's path **inside the container**, where Claude Code
    resolves it; keyed by the host path the trust dialog would still be there
    to answer. Trust is seeded with the same flag as the onboarding skip
    because the two are one decision: the container is the isolation boundary
    the dialog asks about, and one config home is shared by every container of
    the repo, so the first manual accept already covers the rest.

    Returns whether the config home now carries the state. False means the
    file was there but is not a seed — Claude Code has run here, and the
    contract of `invalidate_identity` applies: a file holding the user's
    projects and MCP servers is **never** overwritten. False also covers an
    unreadable file, a lost lock, and a torn one; the caller falls back to the
    `{}` seed it has always written.
    """
    path = identity_file(home)
    try:
        with config_lock(home):
            data: dict[str, Any] = {}
            if path.exists():
                data = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(data, dict):
                    return False
                if any(key in data for key in ONBOARDING_KEYS):
                    return False
            projects = data.get("projects")
            projects = projects if isinstance(projects, dict) else {}
            entry = projects.get(repo_dir)
            entry = entry if isinstance(entry, dict) else {}
            data["projects"] = {**projects, repo_dir: {**entry, "hasTrustDialogAccepted": True}}
            data["hasCompletedOnboarding"] = True
            engine._atomic_write(path, json.dumps(data, indent=2))
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
            engine._atomic_write(path, json.dumps(data, indent=2))
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
        engine._atomic_write(path, json.dumps(data))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        log.debug("could not record the account of the login parked at %s", path, exc_info=True)


def _park_locked(cfg: Config, account: LiveAccount | None, when: datetime) -> Path | None:
    """Move the live credential into the store; return where it landed."""
    return engine._park_locked(CLAUDE, cfg, account, when)


def park(
    cfg: Config,
    gcfg: GlobalConfig,
    *,
    authoritative: Collection[str],
    now: datetime | None = None,
) -> PoolChange:
    """Store the live login and leave the holder empty."""
    return engine.park(CLAUDE, cfg, gcfg, authoritative=authoritative, now=now)


def switch(
    cfg: Config,
    gcfg: GlobalConfig,
    ref: str,
    *,
    authoritative: Collection[str],
    now: datetime | None = None,
) -> PoolChange:
    """Park the live login and activate a stored one."""
    return engine.switch(CLAUDE, cfg, gcfg, ref, authoritative=authoritative, now=now)


def live_account_refusal(name: str) -> str:
    """The one wording for "that slot is the live login, park it first"."""
    return engine.live_account_refusal(name)


def remove_slot(slot: Slot) -> None:
    """Delete a parked login permanently."""
    engine.remove_slot(slot)
