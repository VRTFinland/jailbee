"""What an agent must tell the engine about its credentials.

The engine owns everything that is the same for every agent: the store, the
slot names, and the rule that a switch moves files rather than copying them.
An adapter owns what differs — where the credential lives, how an account is
named, which parts of the file belong to the machine rather than the account,
and whether a running session survives a switch.

**What an adapter may borrow from the engine.** The dependency runs both ways:
`engine.py` calls an adapter through the protocol below, and an adapter calls
back into these engine helpers, which are public precisely so it can. They are
the sanctioned set, and a new adapter should reach for them rather than
reimplement them:

- `engine.atomic_write(path, text)` — write a file beside a credential
  atomically, durably and at mode 0600. Every such file holds or names a
  secret; its docstring carries the fsync-and-rename reasoning that a
  per-adapter reimplementation would quietly drop.
- `engine.login_of(adapter, path)` — the login block of a credential file, as
  this adapter's own `grant_block` defines it. None means "cannot tell", never
  "different".
- `engine.grant_fingerprint(adapter, login)` — a stable id for a login's
  refresh-token lineage, for an adapter that keeps a note beside the credential
  and needs it to stop being read once that grant is gone.
- `engine.credential_in(adapter, holder)` and `engine.holder_dir(adapter, cfg)`
  — where a holder's live credential is, and which holder a repo reads.

Anything else in `engine.py` spelled with a leading underscore is the engine's
own business and is not part of this contract.
"""

from __future__ import annotations

from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from collections.abc import Collection, Sequence
    from pathlib import Path

    from jailbee.accounts.models import LiveAccount, Member, Slot
    from jailbee.config import Config
    from jailbee.incus import Incus


@dataclass(frozen=True)
class Wiring:
    """What a container needs so the agent reads this holder's credential.

    `devices` are Incus disk devices keyed by device name; `env` are
    `environment.*` values. `profiles.py` renders both. An adapter that needs
    nothing returns empty dicts, which is the no-group case for every agent.
    """

    devices: dict[str, dict[str, str]] = field(default_factory=dict)
    env: dict[str, str] = field(default_factory=dict)


@runtime_checkable
class AccountAdapter(Protocol):
    name: str
    """The agent's name; also the store root prefix (`<name>-credentials`)."""

    credential_file: str
    """Filename the agent reads a login from, inside its holder."""

    refresh_token_key: str
    """Key inside `grant_block` naming the refresh token.

    The engine compares and fingerprints refresh-token lineages; it must never
    need to know the shape of a particular agent's credential to do it.
    """

    live_switch: bool
    """Whether a running session survives a switch.

    False means the engine refuses a switch while `blockers` names anything.
    """

    def config_home(self, cfg: Config) -> Path:
        """Where this agent keeps *one repo's* own state, on the host.

        A repo's, never a holder's: one config home is shared by every
        container of the repo whatever credential group each reads, which is
        why it can describe the live login only "usually" (see `account_at`).
        It is also the holder of last resort — `engine.holder_dir` falls back
        to it for a repo that shares no credential group.

        Must be derivable from `cfg` alone and must not touch the filesystem:
        the engine calls it for every registered repo when building a
        host-wide listing, including repos whose directory no longer exists.
        """
        ...

    def holder_override(self, cfg: Config) -> Path | None:
        """The credential directory this repo shares, or None for its own.

        Derived from the agent-agnostic `Config.credential_group`: a group name
        is one value shared by every pooled agent, and each adapter turns it
        into its own holder directory. `ClaudeAdapter` uses
        `engine.group_dir`, which is why the on-disk Claude tree is unchanged.
        """
        ...

    def grant_block(self, raw: str | None) -> dict[str, Any] | None:
        """The login inside credential *text*, or None when there is none.

        The engine never parses a credential itself: it asks for this dict and
        then only ever reads `refresh_token_key` out of it, to tell one
        refresh-token lineage from another and to fingerprint it. Return the
        block verbatim rather than a summary — the engine compares whole
        blocks for equality before falling back to the token.

        **Every unreadable shape is None**, `raw is None` included: a torn
        file, a managed API key, an opaque legacy blob. None means "cannot
        tell", and every caller fails closed on it; it must never be returned
        for a credential whose login could have been read.
        """
        ...

    def compose(self, target_raw: str, live_raw: str | None) -> str:
        """The credential text to activate, from the slot and what is live.

        Most of a credential belongs to the account and travels with the slot.
        Some of it belongs to the *machine* — an agent's OAuth integrations,
        say — and rotates independently of any login; those fields come from
        `live_raw`, which is None when the holder was empty. Anything jailbee
        wrote into the parked file for its own bookkeeping is stripped here:
        what this returns is handed straight to the agent.

        Pure: it is called under the credential locks, between the target
        leaving the store and the new login being written, and must not touch
        the filesystem or raise for a blob it does not recognize — activate
        such a target verbatim.
        """
        ...

    def locks(self, holder: Path) -> AbstractContextManager[None]:
        """Exclude everything else that writes `holder`'s credential.

        Held across the whole move — park, compose, write, `on_activate` —
        so no agent process in any container can refresh a token into a file
        that is being replaced. Must cover whatever lock the agent itself
        takes for its own rotation, or the exclusion is one-sided. An agent
        with no such lock returns `nullcontext()`.

        The engine creates `holder` before entering the context, and never
        calls this when it has nothing to write.
        """
        ...

    def account_at(
        self,
        holder: Path,
        found: Sequence[Member],
        *,
        prefer: str,
        authoritative: Collection[str],
    ) -> LiveAccount | None:
        """Which account `holder`'s live credential belongs to, with its record.

        `holder` is a directory, not a `Config`: the engine asks this about
        holders the calling repo does not resolve to — another group's, or
        another repo's config home — and `accounts.overview` asks it about
        every holder on the host in one pass.

        `found` are the member repos sharing the holder and `prefer` names the
        one to consult first; `authoritative` is the subset whose config home
        may be trusted to describe *this* holder, because a repo whose
        containers span two groups has one config home naming whichever
        account ran last. Reading a non-authoritative member would park one
        account's grant under another's name.

        The engine uses the answer for two things: to name a parked file, and
        to show the live account in a listing. **None is a fact, not an
        error** — a fresh group, or the window a switch opens before any
        container has run the agent again — and the engine then names the file
        `unknown-<timestamp>`. Reads only; nothing here may write.
        """
        ...

    def record_for(self, slot: Slot, raw: str) -> dict[str, Any] | None:
        """The account record a slot carries, if it can be trusted.

        Read before the slot moves, and handed back to `on_activate` and
        `on_switch` so an activation restores what the login was carrying
        instead of merely invalidating what the previous one left. None when
        the slot carries no record — a login jailbee never parked — and the
        engine falls back to invalidating.

        The **slot's name stays authoritative**: a file renamed by hand keeps
        the record it was written with, so an implementation that stores a
        record must return None when the two disagree rather than write one
        account's identity under another's name.
        """
        ...

    def on_park(self, cfg: Config, holder: Path, parked: Path, account: LiveAccount | None) -> None:
        """The login has just moved from `holder` into the file `parked`.

        Called under the credential locks. Where an agent keeps anything
        describing the grant, this is where it travels with it: Claude stamps
        the account into `parked` and deletes the note `holder` no longer has
        a credential for.

        **Best-effort, and must not raise.** The login is already safely in
        the store by the time this runs; an exception here would report a
        failed park that had in fact landed.
        """
        ...

    def on_activate(self, holder: Path, record: dict[str, Any] | None, credential_raw: str) -> None:
        """Record, beside the credential just written, which account it holds.

        The counterpart of `on_park`, called under the credential locks once
        the new login is in place — and again on a rollback, with the login
        being restored. Claude writes its account note here; an agent that
        keeps nothing beside the credential does nothing.

        Separate from `on_switch`, which runs outside the locks and rewrites
        *members*: this one describes the holder itself, and must not exist
        before the credential it names does.
        """
        ...

    def on_switch(
        self,
        found: Sequence[Member],
        unreachable: Sequence[str],
        record: dict[str, Any] | None,
        authoritative: Collection[str],
    ) -> tuple[list[str], list[str]]:
        """Point every member repo's recorded account at the login now live.

        Runs *outside* the locks, after the credential is in place, and is the
        counterpart of `on_activate`: that one describes the holder, this one
        rewrites the repos. Without it every member goes on naming the
        previous account. `record` is `record_for`'s answer — write it where
        it can be trusted, and where it cannot (a member outside
        `authoritative`, or no record at all) clear the member's instead, so
        the agent repopulates it from the credential it actually reads.

        Returns `(updated, not_updated)` as container prefixes, so the caller
        can name the repos still naming the previous account. `unreachable`
        — members whose config could not be loaded — belongs in
        `not_updated`. **Must not raise**: the switch has already landed.
        """
        ...

    def sessions(self, found: Sequence[Member]) -> list[str]:
        """Members that look like they have a session of this agent running.

        A warning input, never a refusal: the engine puts the prefixes in
        `PoolChange.live_sessions` so the user can be told their session may
        now be holding a credential that has moved. `blockers` is the one that
        refuses. Evidence the host can read without asking the daemon — a file
        the agent leaves behind, say — so a stale one reads as live, which is
        the right way round for a warning. An agent that leaves no such trace
        returns `[]`.
        """
        ...

    def blockers(self, cfg: Config, incus: Incus, containers: Sequence[str]) -> list[str]:
        """Containers whose running agent makes a park or switch unsafe.

        Consulted **only** when `live_switch` is False, and then before
        anything is written: a non-empty list aborts the operation naming
        these containers. An agent whose process re-reads its credential —
        Claude — returns `[]` and never blocks.

        `containers` are the holder's containers, already resolved by the
        caller; `incus` is how to look inside them and is guaranteed non-None
        on this path (`engine._required_incus` makes sure of it). Reads only.

        A container the daemon will not answer about is the case to decide
        deliberately: calling it a blocker makes an unreachable container
        wedge the pool, and not calling it one lets a running session through.
        Whichever an implementation picks, it should say so here.
        """
        ...

    def wiring(self, cfg: Config, group_dir: Path | None) -> Wiring:
        """What a container needs so this agent reads `group_dir`'s credential.

        `profiles.py` renders the result into the repo's Incus profiles, so
        this is the one thing here that is not about the store at all. It is
        also the only protocol method whose output reaches a *file jailbee
        writes for the user*: changing it changes what `jb apply` produces,
        which is an `UPGRADE_NOTES` entry.

        `group_dir` is None when the repo shares no credential — return an
        empty `Wiring` then, and whenever the agent is disabled. An env value
        that would be empty is omitted rather than written: an empty variable
        and an unset one are rarely the same thing to an agent.
        """
        ...

    def prepare_config_home(self, cfg: Config, home: Path) -> None:
        """Make `home` usable before a container first runs the agent.

        The seam for whatever an agent needs seeded into a fresh config home —
        an onboarding flag, a trust record — so the user is not sent through a
        first-run wizard for an account they are already logged into.

        **Nothing calls this yet**: Claude's seed still lives in
        `init_command`, and the Claude implementation is a no-op. It is
        declared here so the seam is the adapter's when that moves. An
        implementation must never overwrite a config home the agent has
        already written to.
        """
        ...


ADAPTERS: dict[str, AccountAdapter] = {}
"""Name → adapter. Populated by each adapter module at import time."""


def register(adapter: AccountAdapter) -> None:
    ADAPTERS[adapter.name] = adapter


def get_adapter(name: str) -> AccountAdapter:
    """The adapter for `name`, importing the module that registers it.

    Imported here rather than at module level so `base` stays free of every
    adapter's dependencies, and so a caller naming an agent that has no pool
    gets a `KeyError` it can turn into a user-facing message.
    """
    if name not in ADAPTERS:
        import importlib

        try:
            importlib.import_module(f"jailbee.accounts.adapters.{name}")
        except ModuleNotFoundError:
            pass
    return ADAPTERS[name]


def pooled_adapters(cfg: Config) -> list[AccountAdapter]:
    """Every enabled agent that has an adapter, `claude` first.

    `claude` first because it is the one every existing user has; the order is
    what `jailbee account ls` renders and what a picker offers.
    """
    found: list[AccountAdapter] = []
    for name in sorted(cfg.agents, key=lambda n: (n != "claude", n)):
        if not cfg.agents[name].enabled:
            continue
        try:
            found.append(get_adapter(name))
        except KeyError:
            continue
    return found
