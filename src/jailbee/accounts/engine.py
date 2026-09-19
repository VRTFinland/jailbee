"""The generic half of an agent's account pool.

A holder — a credential *group* directory, or a single repo's config home when
it shares none — has at most one live login. Every other stored login sits in a
host-wide store as a plain file. Switching moves one file out and another in,
then lets the adapter point each member repo's recorded account at the login
now live.

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

**No state but the filesystem.** A file in `store_dir(adapter)` is parked; the
file in `holder_dir(adapter, cfg)` is live. There is no ledger, so nothing can
disagree with the directory about what the directory contains.

**Nothing here knows which agent it is serving.** The store root, the
credential filename, how a login is recognized inside a credential file and
what has to be recorded beside it all come from the `AccountAdapter` passed in
as the first argument. Moved out of jailbee's original, Claude-only account
pool, whose Claude half now lives in `adapters/claude.py`.

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
import os
import shutil
import tempfile
from collections.abc import Callable, Collection, Sequence
from contextlib import suppress
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from jailbee.accounts.models import (
    _SLOT_SUFFIX,
    DISAMBIGUATOR,
    LIVE_UNIDENTIFIED,
    Identity,
    LiveAccount,
    Member,
    PoolChange,
    PoolError,
    Slot,
    slug_for,
    unknown_slot_name,
)

if TYPE_CHECKING:
    from jailbee.accounts.adapters.base import AccountAdapter
    from jailbee.config import Config
    from jailbee.global_config import GlobalConfig
    from jailbee.incus import Incus


def store_dir(adapter: AccountAdapter) -> Path:
    """The host-wide parked-credential store for one agent.

    A sibling of the group directories rather than a child of one: an account
    parked from `work` must be activatable into `personal`. Safe from
    collision because `_CREDENTIAL_GROUP_RE` (`config.py`) forbids a group name
    starting with `_`.
    """
    from jailbee.paths import xdg_data_home

    return xdg_data_home() / "jailbee" / f"{adapter.name}-credentials" / "_parked"


def group_dir(agent: str, name: str) -> Path:
    """The credential directory one agent keeps for a group."""
    from jailbee.paths import xdg_data_home

    return xdg_data_home() / "jailbee" / f"{agent}-credentials" / name


def repo_group(cfg: Config) -> str | None:
    """The credential group this repo resolves to, or None."""
    return cfg.credential_group


def holder_dir(adapter: AccountAdapter, cfg: Config) -> Path:
    """The directory whose credential this repo's containers read.

    `holder_override`, not `group_dir(adapter.name, repo_group(cfg))`: an
    adapter may derive its holder from the group name (`ClaudeAdapter` does),
    but a test that wants an arbitrary directory patches `holder_override`
    directly rather than pointing `credential_group` at one — the group is a
    bare name, not a path.
    """
    return adapter.holder_override(cfg) or adapter.config_home(cfg)


def credential_in(adapter: AccountAdapter, holder: Path) -> Path:
    """The live credential file inside `holder`.

    The path-shaped half of `live_credential_path`, for callers holding a
    holder directory rather than a `Config` that names it: `accounts.overview`
    walks the credential store and reads groups no repo resolves to.
    """
    return holder / adapter.credential_file


def live_credential_path(adapter: AccountAdapter, cfg: Config) -> Path:
    """The live credential file for this repo's holder."""
    return credential_in(adapter, holder_dir(adapter, cfg))


def _slot_name(path: Path) -> str:
    """The slot name a store file carries: its filename without `.json`."""
    return path.name[: -len(_SLOT_SUFFIX)]


def parked_slots(adapter: AccountAdapter) -> list[Slot]:
    """Every stored login, sorted by name. An absent store is an empty pool."""
    store = store_dir(adapter)
    try:
        files = sorted(store.glob(f"*{_SLOT_SUFFIX}"))
    except OSError:
        return []
    return [Slot(name=_slot_name(p), path=p, live=False) for p in files]


def live_slot_at(adapter: AccountAdapter, holder: Path, identity: Identity | None) -> Slot | None:
    """`holder`'s live login, or None when nothing is logged in there."""
    path = credential_in(adapter, holder)
    if not path.exists():
        return None
    name = slug_for(identity) if identity is not None else LIVE_UNIDENTIFIED
    return Slot(name=name, path=path, live=True)


def live_slot(adapter: AccountAdapter, cfg: Config, identity: Identity | None) -> Slot | None:
    """The holder's live login, or None when nothing is logged in."""
    return live_slot_at(adapter, holder_dir(adapter, cfg), identity)


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
    adapter: AccountAdapter,
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
            f"no stored login to {purpose}. `jailbee {adapter.name} park` stores the one in "
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


def registered_repos() -> list[tuple[str, Path]]:
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
    resolved = gcfg.credentials.group_for(prefix)
    return resolved is not None and resolved == group


def group_member_prefixes(gcfg: GlobalConfig, group: str) -> list[str]:
    """Registered repos resolving to `group`, sorted, including the caller.

    The single implementation of the group-matching rule; `doctor.py` filters
    the caller out of it for display.
    """
    return sorted(prefix for prefix, _ in registered_repos() if _resolves_to(gcfg, prefix, group))


def members(
    adapter: AccountAdapter, cfg: Config, gcfg: GlobalConfig
) -> tuple[list[Member], list[str]]:
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
    from jailbee.config import load_repo_config

    me = Member(cfg.container_prefix, adapter.config_home(cfg))
    if cfg.credential_group is None:
        return [me], []

    group = repo_group(cfg)
    assert group is not None  # the None case returned above
    found = [me] if _resolves_to(gcfg, cfg.container_prefix, group) else []
    unreachable: list[str] = []
    for prefix, repo_root in registered_repos():
        if prefix == cfg.container_prefix or not _resolves_to(gcfg, prefix, group):
            continue
        if not repo_root.is_dir():
            # A registration whose directory is gone stays unreachable, as it
            # was when "no config file" was the test: the synthesizing loader
            # would happily build a config for a path that does not exist and
            # report a member whose real `shared_dir` nobody can know.
            unreachable.append(prefix)
            continue
        try:
            # `load_repo_config`, not `load_config(repo_config_path(...))`:
            # a registered scratch repo has no config file, and treating
            # "no file" as unreachable would report a perfectly readable
            # member as unreachable in `jailbee claude ls`. The loader
            # synthesizes it instead, and still raises (into the `except`
            # below, as before) when the directory is gone or
            # `scratch.enabled` is false — the cases "unreachable" is for.
            other = load_repo_config(repo_root)
        except Exception:  # ConfigError, OSError, YAML/Pydantic — all mean "unreadable"
            unreachable.append(prefix)
            continue
        found.append(Member(prefix, adapter.config_home(other)))
    return sorted(found, key=lambda m: m.container_prefix), sorted(unreachable)


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


def atomic_write(path: Path, text: str) -> None:
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

    Public because an adapter needs it: every file an adapter writes beside a
    credential holds or names a secret, and reimplementing this per agent would
    lose the reasoning above one adapter at a time. See `adapters/base.py` for
    the list of engine helpers an adapter may use.
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


def login_of(adapter: AccountAdapter, path: Path) -> dict[str, Any] | None:
    """The login block of a credential file, for identity comparison.

    "Login block" is whatever `adapter.grant_block` says it is — Claude's
    `claudeAiOauth`, another agent's something else. Public so an adapter can
    ask the same question of a file it is about to describe; see
    `adapters/base.py` for the engine helpers an adapter may use.

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
        return _login_block(adapter, path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError):
        return None


def _login_block(adapter: AccountAdapter, raw: str | None) -> dict[str, Any] | None:
    """The login block of credential *text* — `login_of` for a path.

    One definition of "the login inside a credential", so the fingerprint a
    note is written with and the one it is checked against cannot be read out
    of two differently-shaped dicts.
    """
    return adapter.grant_block(raw)


def _same_grant(adapter: AccountAdapter, left: dict[str, Any], right: dict[str, Any]) -> bool:
    """Whether two `claudeAiOauth` blocks are one refresh-token lineage.

    Equal blocks are trivially the same grant. A shared, non-empty
    `refreshToken` is the stronger test and the reason this is not just `==`:
    an access token rotates while the lineage behind it does not, so two blocks
    can differ field by field and still be one login in two files — the exact
    state this module exists to prevent.
    """
    if left == right:
        return True
    token = left.get(adapter.refresh_token_key)
    return isinstance(token, str) and bool(token) and token == right.get(adapter.refresh_token_key)


def holds_same_login(adapter: AccountAdapter, left: Path, right: Path) -> bool:
    """Whether two credential files carry one refresh-token lineage.

    False whenever either file is missing, unreadable or carries no login.
    Callers use this to *soften* a warning (`doctor._orphaned_stage_checks`),
    so "cannot tell" has to read as "cannot tell" and never as "yes". The
    blocks are compared and discarded; nothing about them is logged or
    returned.
    """
    a = login_of(adapter, left)
    b = login_of(adapter, right)
    return a is not None and b is not None and _same_grant(adapter, a, b)


def grant_fingerprint(adapter: AccountAdapter, login: dict[str, Any] | None) -> str | None:
    """A stable id for a login's refresh-token lineage, or None for no lineage.

    Access tokens rotate; the refresh token behind them does not (the property
    `_same_grant` already rests on), so this survives every ordinary token
    refresh and changes on a fresh `/login`. Hashed so the note holds no
    secret, and truncation would only weaken a comparison that costs nothing.

    None for a credential carrying no refresh token — a managed `sk-ant-…` key,
    or any opaque shape — which leaves such a holder with no note and the
    pre-note behaviour.

    Public because only an adapter has a use for it: an agent that keeps a note
    beside its credential needs the note to stop being read once the grant it
    describes is gone, and this is how it says so without storing a secret. See
    `adapters/base.py` for the engine helpers an adapter may use.
    """
    token = None if login is None else login.get(adapter.refresh_token_key)
    if not isinstance(token, str) or not token:
        return None
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _disambiguated_slot(
    adapter: AccountAdapter, store: Path, name: str, live: Path, dest: Path, when: datetime
) -> Path:
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
    live_grant = login_of(adapter, live)
    taken = sorted({dest, *store.glob(f"{name}{DISAMBIGUATOR}*{_SLOT_SUFFIX}")})
    for other in taken:
        other_grant = login_of(adapter, other)
        if live_grant is None or other_grant is None:
            raise PoolError(
                f"the store already holds `{_slot_name(other)}` ({other}), and jailbee "
                "could not read both files to tell whether that is the same login as "
                "the one being parked. Nothing was moved; the live credential is still "
                "in place. Compare the two files, and remove the stored one with "
                f"`jailbee {adapter.name} rm {_slot_name(other)}` if it is the stale copy."
            )
        if _same_grant(adapter, live_grant, other_grant):
            raise PoolError(
                f"the login being parked is already stored as `{_slot_name(other)}` "
                f"({other}). Parking it again would leave one refresh-token lineage in "
                "two files, and the first token rotation would kill one of them. "
                f"Run `jailbee {adapter.name} rm {_slot_name(other)}` first if the stored copy "
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
    adapter: AccountAdapter,
    cfg: Config,
    found: Sequence[Member],
    authoritative: Collection[str],
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
    account = adapter.account_at(
        holder_dir(adapter, cfg), found, prefer=cfg.container_prefix, authoritative=authoritative
    )
    slots = parked_slots(adapter)
    live = live_slot(adapter, cfg, None if account is None else account.identity)
    if live is not None:
        if any(s.name == live.name for s in slots):
            live = replace(live, name=f"{live.name}{DISAMBIGUATOR}live")
        slots.append(live)
    return sorted(slots, key=lambda s: (not s.live, s.name)), account


def list_slots(
    adapter: AccountAdapter,
    cfg: Config,
    gcfg: GlobalConfig,
    *,
    authoritative: Collection[str],
) -> list[Slot]:
    """Every stored login, the live one first."""
    found, _ = members(adapter, cfg, gcfg)
    return _slots_for(adapter, cfg, found, authoritative)[0]


def _park_locked(
    adapter: AccountAdapter, cfg: Config, account: LiveAccount | None, when: datetime
) -> Path | None:
    """Move the live credential into the store; return where it landed.

    `account` carries both halves of what a park needs: the identity that names
    the file, and the agent's own record of the account to keep inside it so a
    later activation can restore it. One parameter rather than two, so a caller
    cannot pair a name with another account's record.

    `_move_file` rather than `Path.replace`: `shared_dir` is user-overridable,
    so the holder and the store can live on different filesystems, where a
    rename raises `EXDEV`. Same-filesystem moves stay atomic renames, and the
    cross-filesystem fallback has its own bounded, recoverable failure window.

    Returns the path rather than the name because after `_disambiguated_slot`
    the two are no longer interchangeable: `switch`'s rollback has to move back
    the file that was actually written, not the one its name was derived from.

    What else travels with a parked grant is the adapter's business: `on_park`
    gets the file it landed in and the holder it left, which is where an agent
    whose record travels with the grant (see `ACCOUNT_RECORD_KEY`) stamps it and
    retires whatever the holder was carrying.
    """
    live = live_credential_path(adapter, cfg)
    if not live.exists():
        return None
    name = slug_for(account.identity) if account is not None else unknown_slot_name(when)
    store = store_dir(adapter)
    store.mkdir(parents=True, exist_ok=True)
    dest = store / f"{name}{_SLOT_SUFFIX}"
    if dest.exists():
        dest = _disambiguated_slot(adapter, store, name, live, dest, when)
    _move_file(live, dest)
    adapter.on_park(cfg, holder_dir(adapter, cfg), dest, account)
    return dest


def _required_incus(adapter: AccountAdapter, incus: Incus | None) -> Incus:
    """`incus`, for the one code path that cannot do without it.

    `park` and `switch` take `incus` optionally because an adapter with
    `live_switch = True` — every adapter today — never consults it. An adapter
    that *cannot* survive a live switch does, and a caller reaching that branch
    without one is a jailbee bug, not a user error.

    Not an `assert`: `python -O` strips those, and the stripped version would
    call `adapter.blockers(cfg, None, ...)` — handing the one adapter kind this
    guard exists for a `None` where it expects a daemon connection. A refusal
    that silently stops refusing is worse than no refusal at all, so this
    raises unconditionally. `TypeError` rather than `PoolError` on purpose:
    `PoolError` is rendered to the user as something they can act on, and
    nothing a user does can cause this.
    """
    if incus is None:
        raise TypeError(
            f"the `{adapter.name}` adapter has live_switch=False, so park/switch must be "
            "given an `incus` to check its holder's containers with."
        )
    return incus


def park(
    adapter: AccountAdapter,
    cfg: Config,
    gcfg: GlobalConfig,
    *,
    authoritative: Collection[str],
    now: datetime | None = None,
    incus: Incus | None = None,
    holder_users: Sequence[str] = (),
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

    `incus` and `holder_users` name the containers to check for a running agent,
    as in `switch`: taking the credential out from under one is the same hazard
    as swapping it. Phase 3 wires them from the CLI.
    """
    found, unreachable = members(adapter, cfg, gcfg)
    account = adapter.account_at(
        holder_dir(adapter, cfg), found, prefer=cfg.container_prefix, authoritative=authoritative
    )
    parked: Path | None = None
    if live_credential_path(adapter, cfg).exists():
        # Inside the `exists()` check, not above it: a park with nothing to park
        # does nothing, and refusing a no-op would be noise. Everything below
        # this line writes.
        if not adapter.live_switch:
            blocking = adapter.blockers(cfg, _required_incus(adapter, incus), holder_users)
            if blocking:
                raise PoolError(
                    f"{adapter.name} is running in: {', '.join(blocking)}. "
                    f"Stop it there first — a park under a running {adapter.name} "
                    "takes away the credential it is holding, and its next token "
                    "refresh can write that account's tokens into the holder it "
                    "no longer owns."
                )
        holder = holder_dir(adapter, cfg)
        holder.mkdir(parents=True, exist_ok=True)
        with adapter.locks(holder):
            parked = _park_locked(adapter, cfg, account, now or datetime.now())
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
    updated, not_updated = adapter.on_switch(found, unreachable, None, authoritative)
    return PoolChange(
        parked_as=_slot_name(parked),
        activated=None,
        updated=updated,
        not_updated=not_updated,
        live_sessions=adapter.sessions(found),
    )


def switch(
    adapter: AccountAdapter,
    cfg: Config,
    gcfg: GlobalConfig,
    ref: str,
    *,
    authoritative: Collection[str],
    now: datetime | None = None,
    incus: Incus | None = None,
    holder_users: Sequence[str] = (),
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
    found, unreachable = members(adapter, cfg, gcfg)
    slots, account = _slots_for(adapter, cfg, found, authoritative)
    target = resolve_ref(ref, slots)
    if target.live:
        raise PoolError(f"`{target.name}` is already the live account for this holder.")

    if not adapter.live_switch:
        # Reachable only for an agent whose running session cannot survive a
        # switch, and such a caller has an `Incus` already — it is what named
        # the holder's containers. An adapter with `live_switch = True` never
        # gets here, which is why the parameter can default to None at all.
        blocking = adapter.blockers(cfg, _required_incus(adapter, incus), holder_users)
        if blocking:
            raise PoolError(
                f"{adapter.name} is running in: {', '.join(blocking)}. "
                f"Stop it there first — a switch under a running {adapter.name} "
                "can write one account's tokens into another account's file."
            )

    holder = holder_dir(adapter, cfg)
    holder.mkdir(parents=True, exist_ok=True)
    live_path = live_credential_path(adapter, cfg)
    staged = target.path.with_name(target.path.name + ".activating")

    with adapter.locks(holder):
        target_raw = target.path.read_text(encoding="utf-8")
        record = adapter.record_for(target, target_raw)
        live_raw = live_path.read_text(encoding="utf-8") if live_path.exists() else None
        target.path.replace(staged)
        parked: Path | None = None
        try:
            parked = _park_locked(adapter, cfg, account, now or datetime.now())
            activated = adapter.compose(target_raw, live_raw)
            atomic_write(live_path, activated)
            staged.unlink()
            # Under the lock, and last: what the adapter records here describes
            # what is now in the holder, so it must not exist before the
            # credential it names does.
            adapter.on_activate(holder, record, activated)
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
                    adapter.on_activate(
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

    updated, not_updated = adapter.on_switch(found, unreachable, record, authoritative)
    return PoolChange(
        parked_as=_slot_name(parked) if parked is not None else None,
        activated=target.name,
        updated=updated,
        not_updated=not_updated,
        live_sessions=adapter.sessions(found),
    )


def live_account_refusal(adapter: AccountAdapter, name: str) -> str:
    """The one wording for "that slot is the live login, park it first".

    `cli.claude_rm_cmd` refuses before it prompts, so the user is not asked to
    confirm a deletion that was never going to happen; `remove_slot` refuses
    again because it is callable without the CLI. Two sites, one sentence.
    """
    return f"`{name}` is the live account — run `jailbee {adapter.name} park` first."


def remove_slot(adapter: AccountAdapter, slot: Slot) -> None:
    """Delete a parked login permanently."""
    if slot.live:
        raise PoolError(live_account_refusal(adapter, slot.name))
    slot.path.unlink()
