"""End-to-end shell-completion tests, driven through the real Typer command tree.

tests/test_completion.py calls the completers in `completion.py` directly —
useful for the module's own contract (never raise, filter correctly), but it
cannot catch a bug in how Typer *wires up* a callback, because it never goes
through Typer's introspection layer at all. That is exactly where the one bug
that actually bit this branch lived: `NameError: name 'typer' is not defined`
(commit 63566b4), raised by `typer.main.get_param_completion`'s
`inspect.signature(func, eval_str=True)` when `completion.py` had `typer`
imported only under `TYPE_CHECKING`. Every test calling the completers
directly passed throughout; only running the real command tree exposes it.

These tests do that: they build the real `BashComplete` driver Typer installs
for itself against `jailbee.cli.app` and ask it for completions, the same way
`_GIE_COMPLETE=bash_complete gie ...<TAB>` would. Incus and git are mocked
(the `completion_repo` fixture, shared with tests/test_completion.py, lives in
tests/conftest.py); nothing here touches a real daemon or network.
"""

from __future__ import annotations

from subprocess import CompletedProcess
from typing import Any

import typer

# Typer vendored Click in 0.26 and no longer depends on it, so there is no
# third-party `click` to import here. `_completion_classes` holds the concrete
# drivers Typer registers for its own `--install-completion`; the base
# `typer._click.shell_completion.ShellComplete` is abstract and cannot be
# instantiated. Typer exposes no public API for driving a completion, so a
# private module is the only way to run one — the alternative, shelling out
# with `_JAILBEE_COMPLETE=bash_complete`, cannot mock Incus or git.
from typer._completion_classes import BashComplete

from jailbee.cli import app

_CLI = typer.main.get_command(app)


def _complete(args: list[str], incomplete: str) -> list[str]:
    """Completion values the shell would be offered, in the order offered."""
    shell = BashComplete(_CLI, {}, "gie", "_GIE_COMPLETE")
    return [item.value for item in shell.get_completions(args, incomplete)]


def _mock_branches(mocker: Any, *branches: str) -> None:
    mocker.patch(
        "jailbee.git.subprocess.run",
        return_value=CompletedProcess(
            args=[], returncode=0, stdout="".join(f"{b}\n" for b in branches), stderr=""
        ),
    )


def test_container_name_completes_through_a_real_command(completion_repo):
    """`gie shell <TAB>` — the introspection layer test_completion.py bypasses."""
    assert _complete(["shell"], "") == ["bugfix", "feat-foo"]


def test_hidden_alias_inherits_the_container_completer(completion_repo):
    """`gie fetch` is `app.command("fetch", hidden=True)(fetch)` — a top-level
    alias for `gie git fetch`. Hidden from `--help`, but still wired.
    """
    assert _complete(["fetch"], "") == ["bugfix", "feat-foo"]


def test_snapshot_restore_resolves_tags_from_short_container_name(completion_repo):
    """`gie snapshot restore feat-foo <TAB>` reads "feat-foo" from ctx.params,
    resolves it to "myrepo-feat-foo", and queries that container's tags.
    """
    _cfg, incus = completion_repo
    incus.snapshot_list.return_value = [{"name": "clean"}, {"name": "pre-upgrade"}]

    assert _complete(["snapshot", "restore", "feat-foo"], "") == ["clean", "pre-upgrade"]
    incus.snapshot_list.assert_called_once_with("myrepo-feat-foo", timeout=2)


def test_snapshot_restore_resolves_tags_from_full_container_name(completion_repo):
    """Same, but the user typed the full `<prefix>-<name>` form."""
    _cfg, incus = completion_repo
    incus.snapshot_list.return_value = [{"name": "clean"}]

    assert _complete(["snapshot", "restore", "myrepo-feat-foo"], "") == ["clean"]


def test_snapshot_create_tag_offers_nothing(completion_repo):
    """`snapshot create`'s tag names a snapshot that doesn't exist yet —
    deliberately not wired to a completer (tests/test_completion_wiring.py
    checks the wiring; this checks what a real TAB press actually returns).
    """
    assert _complete(["snapshot", "create", "feat-foo"], "") == []


def test_new_second_positional_completes_branches_once_first_is_filled(completion_repo, mocker):
    """`gie new NAME <TAB>` — BASE, the second positional, must resolve once
    NAME is already on the command line, not just when it is the only token.
    """
    _mock_branches(mocker, "main", "feat/bar")

    assert _complete(["new", "feat-foo"], "") == ["feat/bar", "main"]


def test_format_option_completes_in_declaration_order():
    """`--format` values are `("table", "json")` — order matters, not just membership."""
    assert _complete(["ls", "--format"], "") == ["table", "json"]


def test_user_option_completes_in_declaration_order():
    """`--user` values are `("dev", "root")`, likewise order-sensitive."""
    assert _complete(["shell", "--user"], "") == ["dev", "root"]
