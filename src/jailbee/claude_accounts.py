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

from sqlmodel import select

from jailbee.db.models import ClaudeAccountAllow, ClaudeAccountHolding, RegisteredRepo

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

    from sqlmodel import Session

    from jailbee.cswap import Account

CLAIMING = "claiming"
"""A holding reserved but not yet confirmed by a successful `cswap switch`."""

HELD = "held"
"""A confirmed holding."""

Identity = tuple[str, str]
"""``(email, organizationUuid)`` — the stable key for an account."""


class PoolError(Exception):
    """A policy refusal. The message is already formatted for the terminal."""


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
