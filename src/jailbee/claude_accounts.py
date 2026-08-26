"""Policy for the Claude account pool: who may hold what, and who does.

`cswap.py` owns the binary; this module owns every decision. It takes a
:class:`~sqlmodel.Session` and a :class:`~jailbee.cswap.Cswap` as explicit
arguments — no global state, and no `Incus`: switching an account is a
credential-file replacement that running containers pick up on their own, so
nothing here touches a container.

The invariant
-------------
Two `/login`s to the *same* Anthropic account each mint their own refresh-token
lineage; they are independent and both stay logged in. What breaks is copying
one credential blob to two places: one lineage, two refreshers, and the first
rotation silently logs the other out. So:

    One stored grant may be live in one place at a time.

The ledger restricts the handing-out of *stored blobs* only. It places no limit
on how many places an account is independently logged into. Enforcement is the
composite primary key of ``claude_account_holding`` plus the refusals here.

Never `/logout` to switch: current Claude Code may revoke the refresh token of
the account being left, killing any stored copy of that same grant. Switching
is always a credential replacement in place, which is what `cswap switch` does.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from sqlalchemy.exc import IntegrityError
from sqlmodel import select

from jailbee.cswap import CswapError
from jailbee.db.models import ClaudeAccountAllow, ClaudeAccountHolding, RegisteredRepo

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Sequence
    from datetime import datetime

    from sqlmodel import Session

    from jailbee.cswap import Account, Cswap, LiveAccount, SwitchResult

CLAIMING = "claiming"
"""A holding reserved but not yet confirmed by a successful `cswap switch`."""

HELD = "held"
"""A confirmed holding."""

Identity = tuple[str, str]
"""``(email, organizationUuid)`` — the stable key for an account."""


class PoolError(Exception):
    """A policy refusal. The message is already formatted for the terminal."""


class PoolCancelledError(PoolError):
    """The user declined a confirmation prompt. Nothing was changed.

    A subclass so that a caller which only knows ``PoolError`` still handles
    it, while the CLI can report a cancellation as information rather than as
    a failure the user should read twice.
    """


@dataclass(frozen=True)
class Holder:
    """The repo that holds one account."""

    container_prefix: str
    repo_root: str | None
    state: str


@dataclass(frozen=True)
class AccountRow:
    """One account as `jailbee claude ls` and the `use` picker see it."""

    account: Account
    holder: Holder | None
    allowed: bool
    mine: bool

    @property
    def blocked_reason(self) -> str | None:
        """Why this repo cannot switch to this account, or None.

        This repo's own holding is never blocked — re-selecting it is a no-op,
        not a conflict.
        """
        if not self.allowed:
            return "not allowed for this repo"
        if self.holder is None or self.mine:
            return None
        if self.holder.state == CLAIMING:
            return f"claiming by {self.holder.container_prefix}"
        return f"held by {self.holder.container_prefix}"

    @property
    def holder_cell(self) -> str:
        """The HOLDER column: the repo, `-` for free, `<-` marks this repo."""
        if self.holder is None:
            return "-"
        cell = self.holder.container_prefix
        if self.holder.state == CLAIMING:
            cell = f"{cell} (claiming)"
        return f"{cell} <-" if self.mine else cell


# ---- allowlist -----------------------------------------------------------


def allowed_identities(session: Session, prefix: str) -> set[Identity]:
    """This repo's allowlist. An **empty set means every account**.

    "No rows" is the default and has to mean "unrestricted": a repo that has
    never been narrowed must not be locked out of the pool.
    """
    rows = session.exec(
        select(ClaudeAccountAllow).where(ClaudeAccountAllow.container_prefix == prefix)
    ).all()
    return {(row.email, row.org_uuid) for row in rows}


def set_allowed(session: Session, prefix: str, identities: Iterable[Identity]) -> None:
    """Replace this repo's allowlist. An empty iterable clears it (= all).

    Replace, not append: `jailbee claude allow a b` states the whole list, and
    the picker's "uncheck everything" has to be able to mean "no restriction".

    ``identities`` is deduplicated before insert: the primary key is
    ``(container_prefix, email, org_uuid)``, so a caller passing a list with a
    repeated identity would otherwise hit an integrity error instead of a
    coalesced set.
    """
    for row in session.exec(
        select(ClaudeAccountAllow).where(ClaudeAccountAllow.container_prefix == prefix)
    ).all():
        session.delete(row)
    for email, org_uuid in set(identities):
        session.add(ClaudeAccountAllow(container_prefix=prefix, email=email, org_uuid=org_uuid))
    session.commit()


# ---- holdings ------------------------------------------------------------


def holders(session: Session) -> dict[Identity, Holder]:
    """Every holding, keyed by account identity, with the holder's repo root.

    ``repo_root`` comes from ``registered_repo`` and is None when that row is
    gone — a checkout that was deleted without releasing. The refusal still
    names the prefix, and `jailbee claude release <ref>` is the way out.
    """
    roots = {
        row.container_prefix: row.repo_root for row in session.exec(select(RegisteredRepo)).all()
    }
    out: dict[Identity, Holder] = {}
    for row in session.exec(select(ClaudeAccountHolding)).all():
        out[(row.email, row.org_uuid)] = Holder(
            container_prefix=row.container_prefix,
            repo_root=roots.get(row.container_prefix),
            state=row.state,
        )
    return out


def account_rows(session: Session, accounts: Sequence[Account], *, prefix: str) -> list[AccountRow]:
    """Join the pool against the ledger and this repo's allowlist."""
    held = holders(session)
    allowed = allowed_identities(session, prefix)
    rows: list[AccountRow] = []
    for account in accounts:
        holder = held.get(account.identity)
        rows.append(
            AccountRow(
                account=account,
                holder=holder,
                allowed=not allowed or account.identity in allowed,
                mine=holder is not None and holder.container_prefix == prefix,
            )
        )
    return rows


# ---- reference resolution ------------------------------------------------


def resolve_ref(accounts: Sequence[Account], ref: str) -> Account:
    """Resolve a slot number, alias or email to exactly one account.

    Every CLI reference goes through this *before* the ledger is touched:
    slots move and aliases change, so the ledger only ever sees
    ``(email, org_uuid)``.

    Raises PoolError on an unknown reference, on an email that matches more
    than one slot — one address can be both a personal account and a member
    of an organization, and picking one silently would switch to the wrong
    quota — and on a digit that is ALSO another account's alias, or two
    accounts sharing one alias: nothing in jailbee or cswap forbids a numeric
    alias, so "3" could mean slot 3 or the account aliased "3", and silently
    preferring the slot would burn the wrong account's quota.
    """
    if not accounts:
        raise PoolError(
            "No accounts in the pool yet.\n"
            "Log in to Claude Code in a container, then run: jailbee claude add"
        )
    wanted = ref.strip()
    if wanted.isdigit():
        slot_match = next((a for a in accounts if a.number == int(wanted)), None)
        if slot_match is None:
            raise PoolError(f"No account in slot {wanted}. {_available(accounts)}")
        # A DIFFERENT account aliased with this same digit is a genuine
        # collision; the same account being both slot N and aliased "N" is
        # not — there is only one answer either way.
        alias_collision = next(
            (
                a
                for a in accounts
                if a.alias
                and a.alias.lower() == wanted.lower()
                and a.identity != slot_match.identity
            ),
            None,
        )
        if alias_collision is not None:
            raise PoolError(
                f"'{wanted}' is ambiguous: slot {wanted} is account {slot_match.number} "
                f"({slot_match.label}), but '{wanted}' is also the alias of account "
                f"{alias_collision.number} ({alias_collision.label}).\n"
                "Pass the email instead, or rename the alias with `cswap alias`."
            )
        return slot_match

    lowered = wanted.lower()
    by_alias = [a for a in accounts if a.alias and a.alias.lower() == lowered]
    if len(by_alias) > 1:
        slots = ", ".join(f"{a.number} ({a.org_name or 'personal'})" for a in by_alias)
        raise PoolError(
            f"'{wanted}' matches more than one account's alias: {slots}.\n"
            "Use the slot number or email instead."
        )
    if len(by_alias) == 1:
        return by_alias[0]
    by_email = [a for a in accounts if a.email.lower() == lowered]
    if len(by_email) == 1:
        return by_email[0]
    if len(by_email) > 1:
        slots = ", ".join(f"{a.number} ({a.org_name or 'personal'})" for a in by_email)
        raise PoolError(
            f"'{wanted}' matches more than one account: {slots}.\nUse the slot number instead."
        )
    raise PoolError(f"No account matches '{wanted}'. {_available(accounts)}")


def _available(accounts: Sequence[Account]) -> str:
    listed = ", ".join(f"{a.number} ({a.label})" for a in accounts)
    return f"Available: {listed}"


def current_account(accounts: Sequence[Account]) -> Account | None:
    """The pooled account this repo's live login is, if it is one.

    Read from cswap's own ``active`` flag, which it derives from the live
    config under ``CLAUDE_CONFIG_DIR`` — so it is this repo's answer, not
    whichever repo switched last.
    """
    for account in accounts:
        if account.active:
            return account
    return None


# ---- the two-phase claim -------------------------------------------------


def use(
    session: Session,
    cswap: Cswap,
    accounts: Sequence[Account],
    *,
    prefix: str,
    ref: str,
    now: datetime,
    force: bool = False,
    confirm: Callable[[LiveAccount], bool] | None = None,
) -> str:
    """Switch this repo to ``ref``. Returns the message to print.

    The claim is two-phase because the database transaction is deliberately
    **not** held across the `cswap switch` subprocess — that would hold
    SQLite's write lock for seconds while a network fetch and a credential
    rewrite happen. So the row is committed as ``claiming`` first, the
    subprocess runs unlocked, and the row is then confirmed or deleted.

    A ``claiming`` row that outlives its command is a crash artifact:
    `jailbee doctor` reports it and `jailbee claude release <ref>` clears it.

    ``confirm`` is the ``--force`` prompt, supplied by the CLI and called
    **here** rather than before this function: the ledger refusals below must
    come first, so the user is never asked to accept a risk on a command that
    then declines. It also means only one ``cswap.status()`` is ever run.
    """
    target = resolve_ref(accounts, ref)

    allowed = allowed_identities(session, prefix)
    if allowed and target.identity not in allowed:
        raise PoolError(
            f"Account {target.number} ({target.label}) is not allowed for "
            f"`{prefix}`.\n"
            f"Widen the list:  jailbee claude allow"
        )

    held = holders(session)
    holder = held.get(target.identity)
    if holder is not None and holder.container_prefix != prefix:
        raise PoolError(_held_elsewhere_message(holder, target=target))

    live = cswap.status()
    if holder is not None and holder.state == HELD and live.identity == target.identity:
        # A true no-op: this repo already holds this account. `cswap move`
        # can renumber slots after the fact though, so the display-only
        # `slot` column is still refreshed — without a switch, a lock, or a
        # keychain touch.
        row = session.get(ClaudeAccountHolding, target.identity)
        if row is not None and row.slot != str(target.number):
            row.slot = str(target.number)
            session.add(row)
            session.commit()
        return f"Already on account {target.number} ({target.label}) — nothing to switch."

    if live.email is not None and not live.managed:
        if not force:
            raise PoolError(
                f"This repo's current login ({live.email}) is not in the pool.\n"
                f"Switching replaces it: cswap stashes the credential first, but "
                f"getting it back is a manual recovery.\n"
                f"Save it first:  jailbee claude add --alias <name>\n"
                f"Or switch anyway:  jailbee claude use {ref} --force"
            )
        if confirm is not None and not confirm(live):
            raise PoolCancelledError("Cancelled — nothing was switched.")

    # Phase 1: reserve, and commit so the reservation is visible to a
    # concurrent `jailbee claude use` in another repo.
    row = session.get(ClaudeAccountHolding, target.identity)
    # What to put back if the switch fails. None means "this invocation
    # created the row", i.e. deleting it is the correct undo.
    previous: tuple[str, str, str, datetime] | None = None
    if row is None:
        row = ClaudeAccountHolding(
            email=target.email,
            org_uuid=target.org_uuid,
            container_prefix=prefix,
            slot=str(target.number),
            state=CLAIMING,
            since=now,
        )
    else:
        # Ours already (a `held` re-selection whose live login drifted, or a
        # stale `claiming` row from a crashed run). Slot is display-only and
        # refreshed here, since `cswap move` renumbers.
        previous = (row.state, row.container_prefix, row.slot, row.since)
        row.container_prefix = prefix
        row.slot = str(target.number)
        row.state = CLAIMING
        row.since = now
    session.add(row)
    try:
        session.commit()
    except IntegrityError as e:
        # Another `jailbee claude use` inserted this identity between the
        # `holders()` read above and this INSERT. The composite primary key is
        # what stopped the double-hold — the invariant held, and all that is
        # left is to say so instead of showing a traceback.
        session.rollback()
        winner = holders(session).get(target.identity)
        if winner is not None and winner.container_prefix != prefix:
            raise PoolError(_held_elsewhere_message(winner, target=target)) from e
        raise PoolError(
            f"Account {target.number} ({target.label}) was claimed by another "
            f"repo first — nothing was switched.\n"
            f"`jailbee claude ls` shows who holds it now."
        ) from e

    def rollback() -> None:
        """Undo phase 1 without discarding a holding that predates it.

        Deleting unconditionally is wrong when the row already existed as
        *this repo's* ``held`` row and only the live login had drifted: a
        failed switch would then mark the account free while this repo may
        still be live on it. A row this invocation created, or a stale
        ``claiming`` row it reclaimed, is genuinely garbage and goes.
        """
        if previous is None or previous[0] == CLAIMING:
            session.delete(row)
        else:
            row.state, row.container_prefix, row.slot, row.since = previous
            session.add(row)
        session.commit()

    # Phase 2: the subprocess, with no transaction held.
    try:
        result = cswap.switch(str(target.number))
    except Exception:
        rollback()
        raise

    mismatch = _switch_mismatch(target, result)
    if mismatch is not None:
        rollback()
        raise CswapError(mismatch)

    row.state = HELD
    row.since = now
    session.add(row)
    for other in session.exec(
        select(ClaudeAccountHolding).where(ClaudeAccountHolding.container_prefix == prefix)
    ).all():
        if (other.email, other.org_uuid) != target.identity:
            session.delete(other)
    session.commit()
    return result.message


def _switch_mismatch(target: Account, result: SwitchResult) -> str | None:
    """Why the account cswap landed on is not the one that was asked for.

    Cheap insurance against a slot renumbered (`cswap move`) between the
    listing that resolved ``target`` and the switch: the ledger's central
    claim is "this repo holds *that* account", and it must be verified rather
    than assumed. The email is the discriminator — the slot number cswap
    echoes back is the one it was handed, so it always "matches".
    """
    if result.email is not None:
        if result.email.lower() == target.email.lower():
            return None
    elif result.number is None or result.number == target.number:
        return None
    landed = result.email or f"slot {result.number}"
    return (
        f"cswap switched to {landed}, not to account {target.number} "
        f"({target.label}) — the slot may have been renumbered since the "
        f"listing was read.\n"
        f"The pool ledger was NOT updated. Run `jailbee claude ls` to see "
        f"what this repo is on now, then `jailbee claude use <ref>` with the "
        f"account you meant."
    )


def _held_elsewhere_message(
    holder: Holder,
    *,
    target: Account | None = None,
    subject: str | None = None,
    release_ref: str | None = None,
    lead: str | None = None,
) -> str:
    """The held-elsewhere refusal, with a directly runnable fix.

    ``target`` is the resolved account when a pool listing was read (`use`,
    `rm`); ``subject``/``release_ref`` cover the case where only the live
    login's email is known (`add`, which never lists).

    A **switch** is offered before a release, because the two are not
    equivalent: `release_repo` is pure bookkeeping — the credential file stays
    behind and that repo's Claude keeps rotating it — while a switch checks
    the rotated credential back in first. Handing a still-live grant to
    another repo is what logs one of them out.

    ``registered_repo.repo_root`` makes the fix a command the user can paste.
    When that row is gone — a checkout deleted without releasing — fall back
    to the account-scoped form, which works from anywhere.
    """
    if target is not None:
        subject = f"Account {target.number} ({target.label})"
        release_ref = release_ref or str(target.number)
    assert subject is not None and release_ref is not None, (
        "pass either `target` or both `subject` and `release_ref`"
    )
    lines = [f"{subject} is held by repo `{holder.container_prefix}`."]
    if lead:
        lines.append(lead)
    if holder.state == CLAIMING:
        lines.append(
            "That holding is still `claiming` — either a switch is running "
            "right now, or one crashed."
        )
    if holder.repo_root:
        lines.append(
            f"Switch that repo to another account (a switch checks its rotated "
            f"credential back in):\n"
            f"  cd {holder.repo_root} && jailbee claude use <other>\n"
            f"Or release it if it is done with this one:\n"
            f"  cd {holder.repo_root} && jailbee claude release"
        )
    else:
        lines.append(
            f"That repo is not registered on this host (checkout gone?).\n"
            f"Release it from here:  jailbee claude release {release_ref}"
        )
    return "\n".join(lines)


# ---- capture (`jailbee claude add`) --------------------------------------


@dataclass(frozen=True)
class CaptureRecord:
    """What :func:`claim_captured` wrote, for the CLI to report.

    ``taken_from`` is the repo whose holding this capture displaced, when the
    pre-capture check could not see it coming. It is None in the normal case.
    """

    slot: str
    identity: Identity
    taken_from: str | None


def ensure_capture_allowed(session: Session, identity: Identity, *, prefix: str) -> None:
    """Refuse to capture a login whose stored copy another repo holds.

    Re-capturing overwrites the pool's stored blob with *this* repo's
    credential lineage, so the holding repo's row would then point at a grant
    it never received — and the moment it switched back to that account, the
    two would be live on one lineage. Refusing is the more important half of
    the fix: recording a holding afterwards cannot undo an overwrite.

    The case this catches squarely is a re-capture of an account that *is*
    already in the pool: ``cswap status`` reports it as ``managed`` and hands
    over the full identity, so the ledger lookup is exact. A login cswap
    cannot match to a stored account has no pool identity to match a row
    against at all — and if ``status`` omits its ``organizationUuid``, the
    identity assembled from it is ``(email, "")``, which will not match a row
    stored under a real org uuid. :func:`claim_captured` closes that gap after
    the fact: once the capture lands, cswap knows the true identity, and the
    row is written under *that* — with a warning naming whichever repo the
    holding was taken from.

    Read-only, and called before the interactive ``cswap add`` subprocess.
    The caller must let this session close before running it: no database
    transaction may be open across a subprocess that can block on a human at
    a ``[y/N]`` prompt.
    """
    holder = holders(session).get(identity)
    if holder is None or holder.container_prefix == prefix:
        return
    raise PoolError(
        _held_elsewhere_message(
            holder,
            subject=identity[0],
            release_ref=identity[0],
            lead=(
                "Capturing it again would overwrite the stored copy that repo "
                "is holding, leaving its ledger row pointing at a credential "
                "it never got."
            ),
        )
    )


def claim_captured(
    session: Session,
    cswap: Cswap,
    identity: Identity,
    *,
    prefix: str,
    slot: int | None,
    now: datetime,
) -> CaptureRecord:
    """Record that this repo holds the stored copy of a freshly captured login.

    Called after a successful ``cswap add``. Without this row the invariant is
    unenforced for that account until this repo's first `use`: ``holders()``
    returns nothing, every refusal passes, and the next repo to `use` it lands
    a *second* live copy of one stored grant — which is the harm the whole
    ledger exists to prevent.

    ``cswap status`` is re-read once, and it settles two things the caller
    cannot know before the capture:

    * **The identity.** Before the capture the login may be unmatched, and the
      identity assembled from ``status`` can then be ``(email, "")`` even for
      an organization account. Afterwards cswap matches it to the stored
      account and reports the real ``organizationUuid``. Writing the row under
      the guessed pair would key it on an account that does not exist, and
      ``holders()`` would never match the real one — the C1 hole by another
      route. So the landed identity wins whenever its email agrees.
    * **The slot**, which is display-only and falls back to the ``--slot`` the
      user passed.

    A failure to read it back is not worth failing the command over: the row
    itself is what matters. The subprocess runs *before* any write here, never
    inside one.

    Returns a :class:`CaptureRecord` rather than the row: the row is expired by
    the commit, so a caller reading it after the session closes would get a
    ``DetachedInstanceError``.
    """
    landed: LiveAccount | None
    try:
        landed = cswap.status()
    except CswapError:
        landed = None

    recorded = identity
    slot_text = "" if slot is None else str(slot)
    if landed is not None and landed.identity is not None:
        if landed.identity[0].lower() == identity[0].lower():
            recorded = landed.identity
            if landed.number is not None:
                slot_text = str(landed.number)

    row = session.get(ClaudeAccountHolding, recorded)
    taken_from: str | None = None
    if row is None:
        row = ClaudeAccountHolding(
            email=recorded[0],
            org_uuid=recorded[1],
            container_prefix=prefix,
            slot=slot_text,
            state=HELD,
            since=now,
        )
    else:
        # Ours already, or another repo's — either because the pre-capture
        # check could not resolve the org uuid, or in the window between it and
        # here. The stored blob is now this repo's credential lineage, so
        # recording this repo is the truth: leaving the row pointing elsewhere
        # would hand that repo a grant this one is live on. It is reported so
        # the user can tell that repo to re-run `jailbee claude use`.
        if row.container_prefix != prefix:
            taken_from = row.container_prefix
        row.container_prefix = prefix
        row.slot = slot_text
        row.state = HELD
        row.since = now
    session.add(row)
    session.commit()
    return CaptureRecord(slot=slot_text, identity=recorded, taken_from=taken_from)


# ---- release and remove --------------------------------------------------


def release_repo(session: Session, prefix: str) -> list[ClaudeAccountHolding]:
    """Free **every** holding of this repo. Returns the freed rows.

    Clears a ``claiming`` row as readily as a ``held`` one: from the user's
    side both mean "this repo is holding an account it should not be".

    One repo normally has at most one row, but not always: a crash between
    phase 1 and phase 2 of :func:`use` leaves this repo with both its old
    ``held`` row and the new ``claiming`` row. Deleting one arbitrarily (and
    reporting success) would leave the other stranded, so all of them go and
    all of them are reported. Refusing instead was considered and rejected:
    the no-ref `release` is the one recovery that works with no `cswap` on
    PATH, so it must not be the thing that needs a healthy ledger.

    Bookkeeping only — see the caveat the CLI prints. The credential file in
    ``<shared_dir>/claude`` stays behind, and this repo's Claude keeps using
    and rotating it.
    """
    rows = list(
        session.exec(
            select(ClaudeAccountHolding).where(ClaudeAccountHolding.container_prefix == prefix)
        ).all()
    )
    for row in rows:
        session.delete(row)
    if rows:
        session.commit()
    return rows


def release_identity(session: Session, identity: Identity) -> ClaudeAccountHolding | None:
    """Free one account's holding wherever it is held.

    The escape hatch: a repo whose checkout is gone can no longer run
    `jailbee claude release` from its own directory, and a `claiming` row
    left by a crash has no owner to clear it.
    """
    row = session.get(ClaudeAccountHolding, identity)
    if row is None:
        return None
    session.delete(row)
    session.commit()
    return row


def remove(session: Session, cswap: Cswap, account: Account) -> None:
    """Remove an account from the pool and drop its ledger rows.

    cswap first: if it refuses (or the user cancels its prompt) the account is
    still in the pool, and a pool entry with no holding row would be a lie the
    ledger tells about who may use it. Deleting the rows only after the
    subprocess succeeds keeps the two in step.

    That ordering is load-bearing for a second reason: ``cswap remove`` is
    interactive and can sit at a ``[y/N]`` prompt for as long as the human
    takes. Running it as the **first** statement is what guarantees no
    database transaction is open across it. Do not add a ledger read above
    this line — move it after the subprocess, or take it in a separate
    session that closes first (`jailbee claude add` does the latter).
    """
    cswap.remove(str(account.number))
    row = session.get(ClaudeAccountHolding, account.identity)
    if row is not None:
        session.delete(row)
    for allow in session.exec(
        select(ClaudeAccountAllow).where(
            ClaudeAccountAllow.email == account.email,
            ClaudeAccountAllow.org_uuid == account.org_uuid,
        )
    ).all():
        session.delete(allow)
    session.commit()


def stale_claims(session: Session) -> list[ClaudeAccountHolding]:
    """Every ``claiming`` row. Each one is a crash artifact.

    Not time-based: a claim only exists for the duration of one `cswap
    switch`, and there is no reliable clock to compare against across
    machines that suspend. `jailbee doctor` reports these; `jailbee claude
    release <ref>` clears one.
    """
    return list(
        session.exec(
            select(ClaudeAccountHolding).where(ClaudeAccountHolding.state == CLAIMING)
        ).all()
    )
