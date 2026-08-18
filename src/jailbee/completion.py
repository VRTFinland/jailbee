"""Shell-completion callbacks for jailbee's CLI arguments.

Typer/Click runs these inside a fresh `jailbee` process on every TAB press
(`_JAILBEE_COMPLETE=bash_complete`; each installed console script — `jailbee`,
`jb`, the legacy `gie` alias — gets its own variable derived from its invoked
name, e.g. `_JB_COMPLETE` / `_GIE_COMPLETE`, per Click's convention, not
something this code chooses), which dictates two rules:

* **Never raise.** An exception escaping a completion callback prints a
  traceback into the middle of the user's command line. An empty list is the
  honest answer to "what could this be?" when we cannot tell, so every
  completer must return `[]` on any failure rather than propagate one.

  This is enforced structurally, not just by convention: `_never_raises`
  wraps each public completer and turns any escaping exception into `[]`.
  The narrower `except` clauses inside `_load`, `_container_names` and
  `complete_snapshot` stay — they catch the *expected* failure modes
  (`ConfigError`, `IncusError`, a malformed JSON payload) at the point of
  failure, which keeps that reasoning visible in the code and in the tests
  that assert on it. `_never_raises` is the backstop for everything else —
  e.g. a payload shape `json.loads` accepts but a completer's own
  comprehension does not (`AttributeError`, `KeyError`, `TypeError`) — so a
  shape nobody has imagined yet still degrades to `[]` instead of a
  traceback. A whole-branch review found three such shapes escaping through
  `complete_snapshot` alone; see `tests/test_completion.py`.
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

from functools import wraps
from typing import TYPE_CHECKING

import typer

if TYPE_CHECKING:
    from collections.abc import Callable

    from jailbee.config import Config
    from jailbee.incus import Incus

# Bound on a single completion query. The measured cost of
# `incus list --format json --fast` is 17-29 ms (Incus 6.0.5, 11 instances), so
# this is not a performance knob — it is the ceiling on how long a TAB press can
# hang when the daemon is unresponsive.
QUERY_TIMEOUT = 2


def _never_raises[**P](fn: Callable[P, list[str]]) -> Callable[P, list[str]]:
    """Make the "never raise" contract structural instead of hand-maintained.

    Wraps a completer so that *any* exception escaping it — not just the ones
    an `except` clause happened to anticipate — becomes `[]`. This is a
    backstop, not a replacement for the narrower `except` clauses already in
    this module: those still catch the expected failure modes and return `[]`
    at the point of failure, documenting *why* that failure is expected. This
    decorator exists for the failure nobody wrote down: a JSON payload shape
    that parses fine but breaks a completer's own comprehension or attribute
    access (`AttributeError`, `KeyError`, `TypeError`, ...).

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
        try:
            return fn(*args, **kwargs)
        except Exception:
            return []

    return guard


def _load() -> tuple[Config, Incus] | None:
    """Return (config, Incus) for the cwd's repo, or None if unavailable.

    Completion is best-effort: outside a repo root, or with a config that fails
    validation, there is nothing to complete and no way to report an error, so
    the caller turns None into an empty candidate list.
    """
    from jailbee.config import ConfigError, load_config
    from jailbee.incus import Incus
    from jailbee.paths import find_repo_config

    try:
        cfg = load_config(find_repo_config())
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


@_never_raises
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


@_never_raises
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


@_never_raises
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


@_never_raises
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
    from jailbee.lifecycle import list_containers
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
            names = [c.name for c in list_containers(cfg, incus, fast=True)]
        devices = {
            fwd.device for fwds in list_forwards(incus, names).values() for fwd in fwds
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

    @_never_raises
    def _complete(incomplete: str) -> list[str]:
        return [v for v in values if v.startswith(incomplete)]

    return _complete
