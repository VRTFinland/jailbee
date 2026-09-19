"""Shell-completion callbacks for jailbee's CLI arguments.

Typer/Click runs these inside a fresh `jailbee` process on every TAB press
(`_JAILBEE_COMPLETE=complete_bash`; each installed console script — `jailbee`
and `jb` — gets its own variable derived from its invoked name, e.g.
`_JB_COMPLETE`, per Click's convention, not something this code chooses, and
the *value* is Typer's own `complete_bash`, not vendored Click's
`bash_complete` — Typer installs its own drivers and rejects Click's spelling
with "Shell complete not supported."), which dictates three rules:

* **Never raise.** An exception escaping a completion callback prints a
  traceback into the middle of the user's command line. An empty list is the
  honest answer to "what could this be?" when we cannot tell, so every
  completer must return `[]` on any failure rather than propagate one.

  This is enforced structurally, not just by convention: `_completion_guard`
  wraps each public completer and turns any escaping exception into `[]`.
  The narrower `except` clauses inside `_load`, `_container_names` and
  `complete_snapshot` stay — they catch the *expected* failure modes
  (`ConfigError`, `IncusError`, a malformed JSON payload) at the point of
  failure, which keeps that reasoning visible in the code and in the tests
  that assert on it. `_completion_guard` is the backstop for everything else —
  e.g. a payload shape `json.loads` accepts but a completer's own
  comprehension does not (`AttributeError`, `KeyError`, `TypeError`) — so a
  shape nobody has imagined yet still degrades to `[]` instead of a
  traceback. A whole-branch review found three such shapes escaping through
  `complete_snapshot` alone; see `tests/test_completion.py`.
* **Never print.** Stdout *is* the completion protocol — bash evaluates the
  process's stdout inside `COMPREPLY=( $(...) )` — so a line a completer
  happens to print becomes a bogus candidate offered alongside the real ones,
  and stderr is painted straight over the half-typed command line. Neither is
  hypothetical: every completer that calls `_load` runs the full config
  loader, which emits `tui.warn_plain` for a repo still on the pre-1.0
  `.gie/` directory and `tui.hint` for a legacy `chrome:` block, and the
  modules behind the other completers (`lifecycle`, `pool`) print advisories
  of their own. Silencing them one call site at a time would be a standing
  invitation to regress, so `_completion_guard` discards both streams for the
  duration of the callback instead — the second half of the same contract.

  Python-level redirection (`contextlib.redirect_stdout`) rather than an
  fd-level `dup2`: Rich resolves `sys.stdout`/`sys.stderr` lazily at print
  time, so the module-level `tui` consoles follow it, and every subprocess
  reachable from a completer (`incus.py`, `git.py`) already captures its
  child's output. An `incus`/`git` call that ever inherits fd 1 would escape
  this, which is one more reason completion sticks to the capturing wrappers.
* **Bounded per query.** Each Incus query carries a timeout (`QUERY_TIMEOUT`
  below), so a wedged daemon costs a bounded pause rather than an indefinite
  stuck shell — not "never blocks" overall: `complete_snapshot` issues two
  sequential queries (up to `2 * QUERY_TIMEOUT`), and `_load()`'s
  `git symbolic-ref` (via `detect_default_branch`) and `complete_branch`'s
  `git for-each-ref` are untimed subprocess calls.

Callback parameters are matched by **annotation**, not by name:
``typer.main.get_param_completion`` inspects each callback's resolved type
hints and binds ``typer.Context`` to the context parameter and ``str`` to the
incomplete-value parameter; only a parameter Typer cannot match by annotation
falls back to being matched by the literal name ``ctx``/``args``/``incomplete``.
This module always annotates, so the fallback path is never exercised — but it
is why the annotations must be *real, resolvable* types at introspection time
(see the ``import typer`` note below), and why a parameter here must keep
either its annotation or its ``ctx``/``incomplete`` name: dropping the
annotation *and* renaming the parameter is the one combination that breaks
things, and it does so loudly — ``get_param_completion`` raises
``click.ClickException("Invalid autocompletion callback parameters: ...")`` at
command-build time, failing every `CliRunner` test in the suite, not silently.

Imports are deliberately function-local, as in `cli.py`, EXCEPT `typer`:
`cli.py` already imports it unconditionally at module scope to build
`app = typer.Typer(...)`, so it costs nothing extra here. It must be a real
(not `TYPE_CHECKING`-only) import: every `autocompletion=` callback gets
introspected by ``typer.main.get_param_completion`` via
``inspect.signature(func, eval_str=True)``, which resolves this module's
(string, thanks to ``from __future__ import annotations``) annotations
against ``func.__globals__``. A ``typer.Context`` annotation left under
``TYPE_CHECKING`` is invisible at runtime, so that eval raises
``NameError: name 'typer' is not defined`` — Task 4's tests never caught this
because they call the completers directly, bypassing Typer's own
introspection of the callback.

The remaining imports stay function-local: they pull in `config`/`incus`,
which are comparatively heavy.
"""

from __future__ import annotations

import contextlib
import io
from functools import wraps
from typing import TYPE_CHECKING

import typer

if TYPE_CHECKING:
    from collections.abc import Callable

    from jailbee.accounts.adapters.base import AccountAdapter
    from jailbee.config import Config
    from jailbee.incus import Incus

# Bound on a single completion query. The measured cost of
# `incus list --format json --fast` is 17-29 ms (Incus 6.0.5, 11 instances), so
# this is not a performance knob — it is the ceiling on how long a TAB press can
# hang when the daemon is unresponsive.
QUERY_TIMEOUT = 2


def _completion_guard[**P](fn: Callable[P, list[str]]) -> Callable[P, list[str]]:
    """Make both completion contracts structural instead of hand-maintained.

    Wraps a completer so that (a) *any* exception escaping it — not just the
    ones an `except` clause happened to anticipate — becomes `[]`, and (b)
    anything it writes to stdout or stderr is discarded.

    The never-raise half is a backstop, not a replacement for the narrower
    `except` clauses already in this module: those still catch the expected
    failure modes and return `[]` at the point of failure, documenting *why*
    that failure is expected. This decorator exists for the failure nobody
    wrote down: a JSON payload shape that parses fine but breaks a completer's
    own comprehension or attribute access (`AttributeError`, `KeyError`,
    `TypeError`, ...).

    The never-print half exists because a completer is a thin shell over code
    written for interactive use, which prints advisories whenever it feels
    like it (see the module docstring for the two deprecation notices that
    reach here through `_load` alone). The sink is a single `StringIO` for
    both streams — nothing reads it back, and interleaving is irrelevant to
    something being thrown away. Restoring both streams is the context
    managers' job, so an exception raised mid-print still leaves them intact
    before the `except` below turns it into `[]`.

    `functools.wraps` is not just style here: Typer introspects a completer's
    *real* signature via `inspect.signature(fn, eval_str=True)` to bind
    parameters by annotation (see the module docstring), and `inspect.signature`
    follows `__wrapped__` by default — which `wraps` sets. Confirmed empirically
    (not assumed): `inspect.signature(complete_container, eval_str=True)` still
    reports `(ctx: typer.Context, incomplete: str)` after this decorator is
    applied, and `tests/test_completion_wiring.py` still distinguishes a
    correctly-wired argument from a wrong one through the wrapped object.

    The `[**P]` type parameter (rather than `*args: Any, **kwargs: Any) ->
    list[str]`) keeps the decorated function's parameter types precise for
    mypy, instead of collapsing them to `Any`.
    """

    @wraps(fn)
    def guard(*args: P.args, **kwargs: P.kwargs) -> list[str]:
        sink = io.StringIO()
        try:
            with contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
                return fn(*args, **kwargs)
        except Exception:
            return []

    return guard


def _load() -> tuple[Config, Incus] | None:
    """Return (config, Incus) for the cwd's repo, or None if unavailable.

    Completion is best-effort: outside a repo root, or with a config that fails
    validation, there is nothing to complete and no way to report an error, so
    the caller turns None into an empty candidate list.

    `load_repo_config`, not `load_config(find_repo_config())`: a scratch
    directory has containers to complete just like a configured repo does, and
    the file-backed loader would raise `ConfigNotFoundError` there and silently
    complete nothing. `ConfigNotFoundError` is a `ConfigError`, so
    `scratch.enabled: false` still lands in the same never-raise branch below.
    """
    from pathlib import Path

    from jailbee.config import ConfigError, load_repo_config
    from jailbee.incus import Incus

    try:
        cfg = load_repo_config(Path.cwd())
    # A config file with invalid UTF-8 bytes makes Path.read_text() raise
    # UnicodeDecodeError, which config.py does not wrap (config.py:97).
    # ValueError covers it and keeps the never-raise contract airtight.
    except (ConfigError, OSError, ValueError):
        return None
    return cfg, Incus()


def _container_names(cfg: Config, incus: Incus) -> list[str]:
    """Full Incus names of this repo's jailbee-managed containers, [] on failure.

    ``fast=True`` skips the per-instance state fetch; only names are needed.
    ``ValueError`` covers a malformed JSON payload from the wrapper.
    """
    from jailbee.incus import IncusError
    from jailbee.lifecycle import list_containers

    try:
        infos = list_containers(cfg, incus, fast=True, timeout=QUERY_TIMEOUT)
    except (IncusError, ValueError, OSError):
        return []
    return [c.name for c in infos]


@_completion_guard
def complete_container(ctx: typer.Context, incomplete: str) -> list[str]:
    """Complete a container name from this repo's existing containers.

    Offers short names, because that is what users type and what
    ``lifecycle.resolve_container_name`` accepts. Once something is typed, both
    forms are candidates, so a user who began with the ``<prefix>-`` form still
    gets matches; with nothing typed, only the short forms are offered to keep
    the list from doubling.
    """
    from jailbee.lifecycle import short_name

    loaded = _load()
    if loaded is None:
        return []
    cfg, incus = loaded

    full = _container_names(cfg, incus)
    short = [short_name(cfg, name) for name in full]
    if not incomplete:
        return sorted(short)
    return sorted({n for n in (*short, *full) if n.startswith(incomplete)})


@_completion_guard
def complete_branch(ctx: typer.Context, incomplete: str) -> list[str]:
    """Complete a branch name from the host repo's local branches.

    Used by `jailbee new` (both positionals) and `jailbee retarget`, whose base must
    already exist on the host as ``refs/heads/<branch>``.
    """
    from jailbee.git import list_branches

    loaded = _load()
    if loaded is None:
        return []
    cfg, _incus = loaded
    return sorted(b for b in list_branches(cfg.repo_root) if b.startswith(incomplete))


@_completion_guard
def complete_pool_names(ctx: typer.Context, incomplete: str) -> list[str]:
    """Complete a pool name from this repo's configured cache pools.

    Used by `jailbee pool ls`/`prune`'s optional NAME positional.
    """
    from jailbee import pool as pool_mod

    loaded = _load()
    if loaded is None:
        return []
    cfg, _incus = loaded
    return [p.name for p in pool_mod.pools_for(cfg) if p.name.startswith(incomplete)]


@_completion_guard
def complete_app_name(ctx: typer.Context, incomplete: str) -> list[str]:
    """Complete an app name from this repo's GUI application registry.

    Used by `jailbee apps run`'s APP_NAME positional.
    """
    from jailbee.apps import resolve_apps

    loaded = _load()
    if loaded is None:
        return []
    cfg, _incus = loaded
    return [s.name for s in resolve_apps(cfg) if s.name.startswith(incomplete)]


def _agent_param(ctx: typer.Context) -> str | None:
    """The parsed `--agent` value, or None when the command has no such option.

    The same read `complete_snapshot` makes of `ctx.params["name"]`: a
    completer sees the options already parsed on the command line, so a user
    who typed `-a codex` gets codex's pool without the command ever running.
    """
    agent = ctx.params.get("agent") if ctx.params else None
    return agent if isinstance(agent, str) and agent else None


def _adapters_for_completion(agent: str | None) -> list[AccountAdapter]:
    """The adapters a pool completer may read, or [] when nothing is known.

    `_load()` is what discovers *enabled* agents — the config is the only
    source of the pooled set — so it is the first source. It is allowed to
    fail: with an explicit `--agent` the adapter is resolved directly instead,
    because the parked store is host-wide and a TAB press outside a repo still
    has logins to offer. Without one there is no way to know which agents
    exist, and the honest answer is nothing.

    The two halves of the no-config branch matter: `get_adapter` imports an
    adapter module lazily and raises `KeyError` for a name that has none, which
    must degrade to [] like every other failure here.
    """
    from jailbee.accounts.adapters import base

    loaded = _load()
    if loaded is None:
        if agent is None:
            return []
        try:
            return [base.get_adapter(agent)]
        except KeyError:
            return []
    cfg, _incus = loaded
    pooled = base.pooled_adapters(cfg)
    if agent is None:
        return pooled
    return [a for a in pooled if a.name == agent]


@_completion_guard
def complete_account(ctx: typer.Context, incomplete: str) -> list[str]:
    """Complete a stored login for `jailbee account use`/`rm`.

    `--agent` narrows the pool to one adapter; without it the names of every
    enabled agent's store are unioned. The union is deduplicated because the
    same email can be parked in two agents' stores, and a candidate offered
    twice is a candidate the shell shows twice.

    Only the *parked* slots, which is what both commands accept — each refuses
    the live one. `parked_slots` also globs the store with no Incus call,
    unlike `list_slots`, which resolves the holder's members by loading every
    registered repo's config: far too much for a TAB press.

    Full slot names rather than bare emails: a name is always an exact match,
    while an email is ambiguous once one account has two stored logins.
    """
    from jailbee.accounts.engine import parked_slots

    adapters = _adapters_for_completion(_agent_param(ctx))
    names = {s.name for adapter in adapters for s in parked_slots(adapter)}
    return sorted(n for n in names if n.startswith(incomplete))


@_completion_guard
def complete_credential_group(ctx: typer.Context, incomplete: str) -> list[str]:
    """Complete a credential group name from the ones present on this host.

    One group name is one directory per agent, so the candidates are the union
    of every adapter's directories, plus the literal `none` that spells "no
    group" on the command line. `--agent` narrows the union to one adapter's
    directories when the command has that option.

    A missing store is an empty pool (`none` is still offered); a store that
    cannot be read is a failure and yields [] — the same never-guess rule the
    rest of the module follows.
    """
    from jailbee.accounts import groups

    adapters = _adapters_for_completion(_agent_param(ctx))
    if not adapters:
        return []
    names = {"none"}
    for adapter in adapters:
        root = groups.group_dir(adapter.name, "x").parent
        try:
            entries = sorted(root.iterdir())
        except FileNotFoundError:
            continue
        except OSError:
            return []
        names.update(p.name for p in entries if p.is_dir() and not p.name.startswith("_"))
    return sorted(n for n in names if n.startswith(incomplete))


@_completion_guard
def complete_account_agent(ctx: typer.Context, incomplete: str) -> list[str]:
    """Complete an agent name for the account pool's `--agent` option.

    The candidates are the *enabled* agents that have an adapter, which is
    exactly the set `_account_adapters` accepts — a name this offers cannot
    then be refused. Discovering them needs a config, so outside a repo the
    answer is []: the pool commands need one anyway.
    """
    from jailbee.accounts.adapters import base

    loaded = _load()
    if loaded is None:
        return []
    cfg, _incus = loaded
    return sorted(a.name for a in base.pooled_adapters(cfg) if a.name.startswith(incomplete))


def _resolve_typed_container(cfg: Config, incus: Incus, typed: str) -> str | None:
    """Map a user-typed container name to its full Incus name, or None.

    A completion-time stand-in for ``lifecycle.resolve_container_name``, which
    would cost two extra ``incus`` calls via ``exists()``. Matches the same two
    forms — exact, then ``<prefix>-<typed>`` — against a single fast query.

    Deliberately narrower than ``resolve_container_name``: it only matches
    against *this repo's* containers (``_container_names``' ``list_containers``
    call is not ``all_repos=True``), while the command this feeds
    (``jailbee snapshot restore``/``delete``) resolves any container ``incus.exists``
    finds, including foreign-repo ones. Not a bug — completion only needs to
    offer candidates for the common case, and a foreign-repo container name
    typed by hand still works at the command itself, just without a tag list.
    """
    names = _container_names(cfg, incus)
    if typed in names:
        return typed
    prefixed = f"{cfg.container_prefix}-{typed}"
    return prefixed if prefixed in names else None


@_completion_guard
def complete_snapshot(ctx: typer.Context, incomplete: str) -> list[str]:
    """Complete a snapshot tag for the container already on the command line.

    Context-dependent: reads the container from the partially parsed command
    (``ctx.params["name"]``), so `jailbee snapshot restore feat-foo <TAB>` lists
    that container's tags. Returns [] when no container has been typed yet —
    there is nothing to enumerate, and guessing would query the wrong host.
    """
    from jailbee.incus import IncusError

    typed = ctx.params.get("name") if ctx.params else None
    if not isinstance(typed, str) or not typed:
        return []

    loaded = _load()
    if loaded is None:
        return []
    cfg, incus = loaded

    full = _resolve_typed_container(cfg, incus, typed)
    if full is None:
        return []

    try:
        snaps = incus.snapshot_list(full, timeout=QUERY_TIMEOUT)
    except (IncusError, ValueError, OSError):
        return []

    tags = [s.get("name") for s in snaps]
    return sorted(t for t in tags if isinstance(t, str) and t.startswith(incomplete))


@_completion_guard
def complete_submodule_path(ctx: typer.Context, incomplete: str) -> list[str]:
    """Complete a submodule path from the container already on the command line.

    Context-dependent like `complete_snapshot`: reads the container from
    `ctx.params["name"]` and lists that container's submodule paths, so
    `jailbee submodule pr feat-foo <TAB>` offers `lib/a`, `lib/b`. Returns []
    when no container has been typed yet — guessing would query the wrong repo.
    """
    from jailbee.incus import IncusError
    from jailbee.lifecycle import container_repo_dir

    typed = ctx.params.get("name") if ctx.params else None
    if not isinstance(typed, str) or not typed:
        return []
    loaded = _load()
    if loaded is None:
        return []
    cfg, incus = loaded
    full = _resolve_typed_container(cfg, incus, typed)
    if full is None:
        return []
    try:
        repo_dir = container_repo_dir(cfg, incus, full)
        out = incus.exec(
            full,
            ["git", "-C", repo_dir, "submodule", "status", "--recursive"],
            uid=cfg.container_user.uid,
            timeout=QUERY_TIMEOUT,
        )
    except (IncusError, ValueError, OSError):
        return []
    paths = [
        parts[1] for parts in (line.strip().split() for line in out.splitlines()) if len(parts) >= 2
    ]
    return sorted(p for p in paths if p.startswith(incomplete))


@_completion_guard
def complete_port_handle(ctx: typer.Context, incomplete: str) -> list[str]:
    """Complete a `jailbee port rm` handle from one container's forwards.

    Offers device names only — a bare port number also resolves at runtime, but
    two forwards can share one, so completing it would suggest something that
    then needs disambiguating.

    Unlike `complete_snapshot`, this does not give up when no container has
    been typed. `port rm` takes HANDLE first and the optional NAME second, so
    at completion time the name is usually still missing; with one container in
    the repo — the common case, and the one `_resolve_existing` auto-picks
    anyway — the union over every container is exactly the right answer, and
    with several it is a superset the user can filter by typing.
    """
    from jailbee.incus import IncusError
    from jailbee.ports import list_forwards

    loaded = _load()
    if loaded is None:
        return []
    cfg, incus = loaded

    typed = ctx.params.get("name") if ctx.params else None
    try:
        if isinstance(typed, str) and typed:
            full = _resolve_typed_container(cfg, incus, typed)
            names = [] if full is None else [full]
        else:
            names = _container_names(cfg, incus)
        devices = {
            fwd.device
            for fwds in list_forwards(incus, names, timeout=QUERY_TIMEOUT).values()
            for fwd in fwds
        }
    except (IncusError, ValueError, OSError):
        return []
    return sorted(d for d in devices if d.startswith(incomplete))


def complete_choices(*values: str) -> Callable[[str], list[str]]:
    """Build a completer for a fixed set of values, in declaration order.

    Used for options that are plain ``str`` with hand-rolled validation, so
    completion and validation stay independent: the runtime check remains the
    authority, this only saves typing.

    The returned callback's single parameter is annotated ``str``, which is
    how Typer binds it to the incomplete value (see the module docstring) —
    not because it happens to be named ``incomplete``.
    """

    @_completion_guard
    def _complete(incomplete: str) -> list[str]:
        return [v for v in values if v.startswith(incomplete)]

    return _complete
