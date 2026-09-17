"""jailbee's own Claude Code account pool.

A holder — a credential *group* directory, or a single repo's config home when
it shares none — has at most one live login. Every other stored login sits in a
host-wide store as a plain file. Switching moves one file out and another in,
then deletes the `oauthAccount` block from each member repo's `.claude.json`
so Claude Code repopulates it from the credential it now finds.

**This module is a binding, not an implementation.** The generic half lives in
`accounts/engine.py`, parameterised by an `AccountAdapter`; Claude's own half —
identity, the account note, credential composition, the member rewrite — lives
in `accounts/adapters/claude.py`. Every engine name below is that engine bound
to `CLAUDE`, and every Claude name is re-exported from the adapter, so callers
and tests keep the module they have always used.

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

from collections.abc import Callable, Collection, Sequence
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from jailbee.accounts import engine

# Claude's own half, under the names it has always had here. `import X as X`
# throughout because mypy strict forbids an implicit re-export.
from jailbee.accounts.adapters.claude import ACCOUNT_CREDENTIAL_KEYS as ACCOUNT_CREDENTIAL_KEYS
from jailbee.accounts.adapters.claude import ACCOUNT_NOTE_FILE as ACCOUNT_NOTE_FILE
from jailbee.accounts.adapters.claude import ACCOUNT_RECORD_KEY as ACCOUNT_RECORD_KEY
from jailbee.accounts.adapters.claude import CLAUDE
from jailbee.accounts.adapters.claude import CREDENTIAL_FILE as CREDENTIAL_FILE
from jailbee.accounts.adapters.claude import ONBOARDING_KEYS as ONBOARDING_KEYS
from jailbee.accounts.adapters.claude import SHARED_CREDENTIAL_KEYS as SHARED_CREDENTIAL_KEYS
from jailbee.accounts.adapters.claude import _credential_object as _credential_object
from jailbee.accounts.adapters.claude import _login_block as _login_block
from jailbee.accounts.adapters.claude import _member_account as _member_account
from jailbee.accounts.adapters.claude import _record_in as _record_in
from jailbee.accounts.adapters.claude import _rewrite_identities as _rewrite_identities
from jailbee.accounts.adapters.claude import _stamp_account_record as _stamp_account_record
from jailbee.accounts.adapters.claude import account_note_path as account_note_path
from jailbee.accounts.adapters.claude import account_of as account_of
from jailbee.accounts.adapters.claude import compose_credential as compose_credential
from jailbee.accounts.adapters.claude import identity_file as identity_file
from jailbee.accounts.adapters.claude import identity_of as identity_of
from jailbee.accounts.adapters.claude import invalidate_identity as invalidate_identity
from jailbee.accounts.adapters.claude import live_account as live_account
from jailbee.accounts.adapters.claude import live_identity as live_identity
from jailbee.accounts.adapters.claude import live_session_prefixes as live_session_prefixes
from jailbee.accounts.adapters.claude import mark_onboarded as mark_onboarded
from jailbee.accounts.adapters.claude import note_account as note_account
from jailbee.accounts.adapters.claude import note_account_at as note_account_at
from jailbee.accounts.adapters.claude import read_account_record as read_account_record
from jailbee.accounts.adapters.claude import read_identity as read_identity
from jailbee.accounts.adapters.claude import restore_identity as restore_identity
from jailbee.accounts.adapters.claude import shared_fields as shared_fields
from jailbee.accounts.adapters.claude import trusted_record_in as trusted_record_in
from jailbee.accounts.adapters.claude import write_account_note as write_account_note

# The engine's filesystem primitives under this module's old names, re-bound
# rather than wrapped like every other moved name below: they take no adapter,
# so there is nothing to bind, and a wrapper would only add a frame a caller
# that captures one could recurse through. The half that writes — now
# `adapters/claude.py` — calls `engine._atomic_write` qualified rather than
# through this binding, so the engine's is the single name that has to be
# patched to intercept a write — on either side of the split, as one patch of
# `claude_pool`'s own once did.
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

if TYPE_CHECKING:
    from jailbee.config import Config
    from jailbee.global_config import GlobalConfig


def store_dir() -> Path:
    """The host-wide parked-credential store."""
    return engine.store_dir(CLAUDE)


def config_home(cfg: Config) -> Path:
    """This repo's Claude config home on the host — never shared."""
    return CLAUDE.config_home(cfg)


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


def credential_in(holder: Path) -> Path:
    """The live credential file inside `holder`."""
    return engine.credential_in(CLAUDE, holder)


def live_credential_path(cfg: Config) -> Path:
    """The live credential file for this repo's holder."""
    return engine.live_credential_path(CLAUDE, cfg)


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
        CLAUDE, slots, ref, purpose=purpose, picker=picker, is_interactive=is_interactive
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


def _login_of(path: Path) -> dict[str, Any] | None:
    """The `claudeAiOauth` block of a credential file, for identity comparison."""
    return engine._login_of(CLAUDE, path)


def _same_grant(left: dict[str, Any], right: dict[str, Any]) -> bool:
    """Whether two `claudeAiOauth` blocks are one refresh-token lineage."""
    return engine._same_grant(CLAUDE, left, right)


def holds_same_login(left: Path, right: Path) -> bool:
    """Whether two credential files carry one refresh-token lineage."""
    return engine.holds_same_login(CLAUDE, left, right)


def _grant_fingerprint(login: dict[str, Any] | None) -> str | None:
    """A stable id for a login's refresh-token lineage, or None for no lineage."""
    return engine._grant_fingerprint(CLAUDE, login)


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
    return engine.live_account_refusal(CLAUDE, name)


def remove_slot(slot: Slot) -> None:
    """Delete a parked login permanently."""
    engine.remove_slot(CLAUDE, slot)
