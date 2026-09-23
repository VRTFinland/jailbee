"""Choosing *which* stored login, and whose, a pool command acts on.

`engine.py` answers "what is in one agent's pool"; this module answers the
question one layer up — across every pooled agent at once. A host can have
several agents logged in to the same holder, so a reference a user types, or a
menu they are offered, may name a login in more than one pool, and picking
between them is its own concern with its own rules:

- an exact reference wins inside an adapter before any cross-adapter question
  is asked, so an ambiguity *within* one agent is never resolved by `-a`;
- a reference matching nothing in one agent's store is ordinary — the next
  agent is tried — while one matching too much there stops the search;
- several candidates are offered on a TTY and refused off one, naming the `-a`
  values a script should have passed.

Lives in `accounts/` rather than `cli.py` because none of that is argument
parsing: it is the pool's own policy, and it was re-deriving `engine`'s
matching rules by string shape while it sat in the CLI. The TTY test and the
picker are injected — `engine.resolve_interactively` already takes that shape —
so nothing here imports the terminal and every rule is testable without one.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from jailbee.accounts.models import AccountNotFoundError, PoolError

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from jailbee.accounts.adapters.base import AccountAdapter
    from jailbee.accounts.models import Slot
    from jailbee.config import Config
    from jailbee.global_config import GlobalConfig
    from jailbee.incus import Incus


AccountChoice = tuple["AccountAdapter", "Slot"]
"""One stored login and the agent whose pool holds it."""


def adapters_for(cfg: Config, agent: str | None) -> list[AccountAdapter]:
    """The adapters a pool command acts on: `agent`'s one, or every pooled one.

    An explicit `agent` is **not** filtered on `agents.<name>.enabled`. The pool
    is host-wide — the parked store and the group holders live under
    `XDG_DATA_HOME`, not in the repo — so naming an agent this repo happens not
    to enable is a meaningful request, and refusing it broke every
    `jailbee claude ...` alias for exactly the repos most likely to run one
    (Claude off here, logins parked on the host). `None` still means "every
    enabled pooled agent", which is the repo-scoped reading.

    A name with no adapter is refused by name — a typo, or a build without that
    agent's module — so a script learns what it may pass.
    """
    from jailbee.accounts.adapters import base

    if agent is None:
        return base.pooled_adapters(cfg)
    try:
        return [base.get_adapter(agent)]
    except KeyError:
        known = ", ".join(sorted(base.ADAPTERS)) or "none"
        raise PoolError(
            f"agent `{agent}` has no account pool: jailbee has no adapter for it. Known: {known}."
        ) from None


def authoritative_for(
    adapter: AccountAdapter,
    cfg: Config,
    gcfg: GlobalConfig,
    incus: Incus | None = None,
) -> set[str]:
    """Members whose recorded account can be trusted for this repo's holder.

    One place, so `ls`, `use`, `park` and `rm` cannot disagree about which
    repos are authoritative. A repo that shares no group is its own only
    member and is trivially authoritative for itself — there is no group
    to be ambiguous about, and no `incus list` is worth paying for it.

    The adapter is a parameter because authority is asked of *that agent's*
    members: a repo's config home names one agent's account, and reading
    another agent's member list would make the wrong repos authoritative.

    `incus` is a parameter because the answer costs one `incus list`, and a
    caller looping over adapters would otherwise pay for one per agent.
    """
    from jailbee.accounts import engine, groups
    from jailbee.incus import Incus

    group = engine.repo_group(cfg)
    if group is None:
        return {cfg.container_prefix}
    found, _ = engine.members(adapter, cfg, gcfg)
    return groups.authoritative_prefixes(
        gcfg, incus or Incus(), group, [m.container_prefix for m in found]
    )


def matching_choices(
    adapters: Sequence[AccountAdapter],
    cfg: Config,
    gcfg: GlobalConfig,
    ref: str | None,
    *,
    removable: bool,
) -> list[AccountChoice]:
    """Every login `ref` names, across `adapters`; every parked one when None.

    Per adapter the rule is `engine.resolve_ref`'s: an exact slot name wins,
    then a bare email that matches exactly one account. An ambiguity *inside*
    one adapter is not filtered out — `-a` cannot solve two grants of one
    email, so `resolve_ref`'s `AmbiguousAccountError` propagates. Across
    adapters every adapter that resolved is kept, and the caller applies the
    TTY rule to the list.

    `removable` resolves through `engine.resolve_removable`, which lets `rm`
    reach the parked half of a name the live slot also carries.

    A typed `ref` matching nothing anywhere raises rather than returning an
    empty list: only the caller knows whether the reference was typed or
    picked, and only a typed one has an error worth reporting.
    """
    from jailbee.accounts import engine
    from jailbee.incus import Incus

    choices: list[AccountChoice] = []
    known: list[str] = []
    not_found: list[AccountNotFoundError] = []
    # One client for the whole loop: `authoritative_for` costs an `incus list`
    # per adapter otherwise.
    incus = Incus()
    for adapter in adapters:
        slots = engine.list_slots(
            adapter, cfg, gcfg, authoritative=authoritative_for(adapter, cfg, gcfg, incus)
        )
        known.extend(f"{s.name} ({adapter.name})" for s in slots)
        if ref is None:
            choices.extend((adapter, s) for s in slots if not s.live)
            continue
        try:
            slot = (
                engine.resolve_removable(ref, slots)
                if removable
                else engine.resolve_ref(ref, slots)
            )
        except AccountNotFoundError as e:
            # Only "this agent's store has no such login" is ordinary enough to
            # try the next agent. `AmbiguousAccountError` — candidates inside
            # this one adapter that it cannot choose between — propagates,
            # because no `-a` solves it and swallowing it would act on the
            # wrong login.
            not_found.append(e)
            continue
        choices.append((adapter, slot))
    if ref is not None and not choices:
        if len(adapters) == 1:
            raise not_found[0]
        raise PoolError(
            f"no stored account matches `{ref}`."
            + (f" Known: {', '.join(known)}" if known else " No agent has a stored login.")
        )
    return choices


def live_choices(
    adapters: Sequence[AccountAdapter], cfg: Config, gcfg: GlobalConfig
) -> list[AccountChoice]:
    """The live login of every adapter that has one, in adapter order.

    `park`'s candidates: each adapter keeps its own credential file in the
    same holder, so several can be live at once and one must be chosen.
    """
    from jailbee.accounts import engine
    from jailbee.incus import Incus

    choices: list[AccountChoice] = []
    incus = Incus()
    for adapter in adapters:
        slots = engine.list_slots(
            adapter, cfg, gcfg, authoritative=authoritative_for(adapter, cfg, gcfg, incus)
        )
        choices.extend((adapter, s) for s in slots if s.live)
    return choices


def several_choices_error(choices: Sequence[AccountChoice], ref: str | None) -> str:
    """The non-TTY refusal: name the candidates, and `-a` when it can help.

    Two grants of one email inside one adapter are not solved by `-a`, so there
    the candidates' full slot names are the only directions. Two adapters are,
    so those name the `-a` values a script should pass.
    """
    agents = list(dict.fromkeys(adapter.name for adapter, _ in choices))
    if len(agents) > 1:
        listed = ", ".join(f"{slot.name} ({adapter.name})" for adapter, slot in choices)
        directions = " or ".join(f"`-a {name}`" for name in agents)
        subject = f"`{ref}` matches logins" if ref is not None else "the candidates span logins"
        return f"{subject} of more than one agent: {listed}. Pass {directions}."
    listed = ", ".join(slot.name for _, slot in choices)
    return f"specify <email|slot> explicitly (or run in a TTY): {listed}"


def choose(
    choices: Sequence[AccountChoice],
    *,
    ref: str | None,
    nothing: str,
    message: str,
    picker: Callable[[Sequence[AccountChoice], str], tuple[str, str] | None],
    is_interactive: Callable[[], bool],
) -> AccountChoice | None:
    """The one choice to act on; None means the user cancelled the picker.

    One candidate needs no prompt. Several are offered on a TTY and refused off
    one, naming both the candidates and the `-a` values that would pick between
    them — the TTY rule every picker here follows, with the picker itself kept
    pure: it renders and returns, and never checks a TTY.

    `nothing` is the caller's error for an empty list: only it knows whether it
    was choosing a login to switch to or one to delete.

    `picker` and `is_interactive` are injected for the same reason as in
    `engine.resolve_interactively`: this module decides the rule, the CLI owns
    the terminal, and a test can exercise both branches without one.

    A cancelled picker returns None *before* any engine mutation runs: the
    choices come from a read-only listing, and the caller aborts on None.
    """
    if not choices:
        raise PoolError(nothing)
    if len(choices) == 1:
        return choices[0]
    if not is_interactive():
        raise PoolError(several_choices_error(choices, ref))
    picked = picker(choices, message)
    if picked is None:
        return None
    # A value the picker was not offered cannot be acted on; `None` is the
    # safe reading, and the same one a cancelled prompt gets.
    return {(adapter.name, slot.name): (adapter, slot) for adapter, slot in choices}.get(picked)
