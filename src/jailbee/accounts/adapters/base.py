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

    def config_home(self, cfg: Config) -> Path: ...

    def holder_override(self, cfg: Config) -> Path | None:
        """The credential directory this repo shares, or None for its own.

        Phase 1 reads `Config.claude_credentials_dir`; phase 2 replaces every
        implementation with the agent-agnostic `Config.credential_group`.
        """
        ...

    def grant_block(self, raw: str | None) -> dict[str, Any] | None: ...

    def compose(self, target_raw: str, live_raw: str | None) -> str: ...

    def locks(self, holder: Path) -> AbstractContextManager[None]: ...

    def live_account(
        self,
        cfg: Config,
        found: Sequence[Member],
        *,
        prefer: str,
        authoritative: Collection[str],
    ) -> LiveAccount | None: ...

    def record_for(self, slot: Slot, raw: str) -> dict[str, Any] | None: ...

    def on_park(
        self, cfg: Config, holder: Path, parked: Path, account: LiveAccount | None
    ) -> None: ...

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
    ) -> tuple[list[str], list[str]]: ...

    def sessions(self, found: Sequence[Member]) -> list[str]: ...

    def blockers(self, cfg: Config, incus: Incus, containers: Sequence[str]) -> list[str]: ...

    def wiring(self, cfg: Config, group_dir: Path | None) -> Wiring: ...

    def prepare_config_home(self, cfg: Config, home: Path) -> None: ...


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
