"""Structural guard: every container-name argument must offer completion.

The wiring is 38 nearly identical edits, which is exactly the kind of list a
later commit forgets to extend. This walks the real Typer command tree instead
of trusting a hand-maintained list.

`typer.main.get_command()` builds the tree out of Typer's own public
`TyperGroup`/`TyperCommand`/`TyperArgument`/`TyperOption` subclasses — verified
by walking the whole tree and finding no other class — so the isinstance
checks below key on those rather than on Click, which Typer vendored in 0.26
and no longer exposes as a dependency.
"""

from __future__ import annotations

import typer
from typer.core import TyperArgument, TyperCommand, TyperGroup, TyperOption

from jailbee.cli import app

# Positional arguments named `name` that are NOT container names.
#
# `jailbee pool ls`/`jailbee pool prune` take an optional pool name under the
# same "name" parameter name as every container-name positional, but it's a
# `pool.py` name (gradle, chrome-profile, ...), completed by
# `completion.complete_pool_names`, not `completion.complete_container` —
# hence the exclusion.
#
# Blind spots this guard does not cover, because both checks below key on the
# literal parameter name "name" and on `TyperArgument`: a container-name
# *positional* under any other name (e.g. a future `container: ...` parameter)
# would not be walked at all, and a container-name *Option* (e.g. `--container`)
# would not be walked either, since `_walk` never filters on `TyperOption`.
# Neither has arisen yet; if one does, extend the walk rather than assume this
# set already covers it.
NON_CONTAINER_NAME_ARGS: set[tuple[str, str]] = {
    ("jailbee pool ls", "name"),
    ("jailbee pool prune", "name"),
}


def _walk(cmd: TyperCommand | TyperGroup, path: str = ""):
    """Yield (command path, parameter) for every command in the tree."""
    here = f"{path} {cmd.name}".strip()
    if isinstance(cmd, TyperGroup):
        for sub in cmd.commands.values():
            yield from _walk(sub, here)
        return
    for param in cmd.params:
        yield here, param


def _has_completion(param: TyperArgument | TyperOption) -> bool:
    """True when the param was given an autocompletion callback.

    Typer's vendored Click stores it privately (`_custom_shell_complete`) and
    exposes no public accessor, so the private name is the only way to assert
    on the wiring.
    """
    return getattr(param, "_custom_shell_complete", None) is not None


def _underlying_completer(param: TyperArgument | TyperOption) -> object | None:
    """Undo Typer's two layers of ``autocompletion=`` wrapping.

    A `typer.Argument(autocompletion=complete_container)` callback is never
    stored as-is. Typer wraps it twice before it reaches
    `param._custom_shell_complete`:

    1. `typer.main.get_param_completion` builds a `wrapper(ctx, args,
       incomplete)` closure and calls `functools.update_wrapper(wrapper,
       callback)` on it — so `wrapper is not callback`, but
       `wrapper.__wrapped__ is callback`.
    2. `typer.core._typer_param_setup_autocompletion_compat` wraps *that*
       wrapper again in a `compat_autocompletion(ctx, param, incomplete)`
       closure (captured as the free variable `autocompletion`), without
       calling `update_wrapper` — so this outer layer has no `__wrapped__`
       shortcut.

    Net effect: `param._custom_shell_complete is complete_container` is
    always False for a correctly-wired argument, regardless of which
    completer was passed in — so a bare identity check can't tell "wired
    right" from "wired wrong". Reach through both layers instead: pull the
    middle `wrapper` out of the outer closure by its free-variable name, then
    follow its `__wrapped__` to the real completer. Verified against a
    throwaway two-command Typer app where one argument was wired to a
    different completer, to confirm this actually distinguishes them rather
    than collapsing everything to the same object.

    Delete this helper only if Typer changes
    `_typer_param_setup_autocompletion_compat`'s closure shape (in which case
    it must be revisited, not deleted) or if Typer ever exposes a public
    accessor for a parameter's underlying completer.

    `completion.py`'s public completers are themselves wrapped by
    `completion._never_raises` (a `functools.wraps`-preserving decorator), so
    the object `cli.py` actually passes as `autocompletion=` — and the object
    `from jailbee.completion import complete_container` yields here —
    is the *decorated* function, not the bare one underneath. That does not
    add a third hop to unwrap: `middle.__wrapped__` (step 1 above) is set by
    `functools.update_wrapper(wrapper, callback)`, where `callback` is
    whatever was passed as `autocompletion=` — i.e. the decorated function —
    so it already lands on the same object this module imports and compares
    against. Reverified after `_never_raises` was introduced: this helper was
    not changed, and `test_the_completion_callback_is_the_container_completer`
    still fails when a `name` argument is deliberately wired to the wrong
    completer (checked by temporarily wiring `shell`'s `name` to
    `complete_branch` and confirming the assertion fires, then reverting).
    """
    outer = getattr(param, "_custom_shell_complete", None)
    if outer is None:
        return None
    code = getattr(outer, "__code__", None)
    closure = getattr(outer, "__closure__", None)
    if code is None or closure is None:
        return outer
    for name, cell in zip(code.co_freevars, closure, strict=True):
        if name == "autocompletion":
            middle = cell.cell_contents
            return getattr(middle, "__wrapped__", middle)
    return outer


def test_every_container_name_argument_offers_completion():
    cli = typer.main.get_command(app)
    missing = [
        f"{cmd_path}:{param.name}"
        for cmd_path, param in _walk(cli)
        if isinstance(param, TyperArgument)
        and param.name == "name"
        and (cmd_path, param.name) not in NON_CONTAINER_NAME_ARGS
        and not _has_completion(param)
    ]
    assert not missing, f"container-name arguments without autocompletion: {missing}"


def test_the_completion_callback_is_the_container_completer():
    """Guard against wiring a name argument to the wrong completer."""
    from jailbee.completion import complete_container

    cli = typer.main.get_command(app)
    wrong = [
        f"{cmd_path}:{param.name}"
        for cmd_path, param in _walk(cli)
        if isinstance(param, TyperArgument)
        and param.name == "name"
        and (cmd_path, param.name) not in NON_CONTAINER_NAME_ARGS
        and _underlying_completer(param) is not complete_container
    ]
    assert not wrong, f"name arguments wired to something else: {wrong}"


def _param(cmd_path: str, param_name: str) -> TyperArgument | TyperOption:
    cli = typer.main.get_command(app)
    for path, param in _walk(cli):
        if path == cmd_path and param.name == param_name:
            return param
    raise AssertionError(f"no such parameter: {cmd_path}:{param_name}")


def test_branch_arguments_complete_branches():
    from jailbee.completion import complete_branch

    for cmd_path, param_name in [
        ("jailbee new", "container_branch"),
        ("jailbee new", "base"),
        ("jailbee retarget", "new_base"),
    ]:
        param = _param(cmd_path, param_name)
        assert _underlying_completer(param) is complete_branch, f"{cmd_path}:{param_name}"


def test_snapshot_tag_arguments_complete_tags():
    """restore and delete take an existing tag; create takes a new one."""
    from jailbee.completion import complete_snapshot

    for cmd_path in ["jailbee snapshot restore", "jailbee snapshot delete"]:
        assert _underlying_completer(_param(cmd_path, "tag")) is complete_snapshot, cmd_path

    assert _underlying_completer(_param("jailbee snapshot create", "tag")) is None


def test_fixed_choice_options_complete_their_values():
    """All five --format sites plus the three single-site options."""
    cases = [
        ("jailbee ls", "fmt", ["table", "json"]),
        ("jailbee job ls", "fmt", ["table", "json"]),
        ("jailbee snapshot ls", "fmt", ["table", "json"]),
        ("jailbee pool ls", "fmt", ["table", "json"]),
        ("jailbee chrome-pool ls", "fmt", ["table", "json"]),
        ("jailbee disk-usage", "fmt", ["table", "json"]),
        ("jailbee config show", "layer", ["global", "repo", "effective"]),
        ("jailbee new", "attach", ["shell", "tmux", "none"]),
        ("jailbee shell", "user", ["dev", "root"]),
    ]
    for cmd_path, param_name, expected in cases:
        complete = _underlying_completer(_param(cmd_path, param_name))
        assert complete is not None, f"{cmd_path}:{param_name} has no completer"
        assert complete("") == expected, f"{cmd_path}:{param_name}"
