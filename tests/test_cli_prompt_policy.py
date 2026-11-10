"""The missing-value policy, enforced over the whole tree (CLAUDE.md:
"Missing required values are asked for, never usage errors")."""

from __future__ import annotations

import ast
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import typer.main
from typer._click import Context

from jailbee.cli import app

SRC = Path(__file__).resolve().parents[1] / "src" / "jailbee"

# Modules that read stdin's TTY-ness for something other than "may I ask?",
# keyed by path relative to src/jailbee.
_NOT_A_PROMPT = {
    "prompting.py": "the predicate itself",
    "macos.py": "decides whether the delegated host command gets a pty",
    "dashboard/tui/session.py": (
        "a full-screen TUI needs a terminal on both ends, env override or not"
    ),
}


def _tty_probes(source: str) -> list[int]:
    """Line numbers of terminal-ness probes on stdin: `<x>.stdin.isatty()`,
    `os.isatty(...)`, and any use of `<x>.__stdin__` (the original stdin,
    which is how one would sidestep a patched `sys.stdin`)."""
    lines = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Attribute):
            continue
        stdin_isatty = (
            node.attr == "isatty"
            and isinstance(node.value, ast.Attribute)
            and node.value.attr == "stdin"
        )
        os_isatty = (
            node.attr == "isatty" and isinstance(node.value, ast.Name) and node.value.id == "os"
        )
        if stdin_isatty or os_isatty or node.attr == "__stdin__":
            lines.append(node.lineno)
    return lines


def test_stdin_isatty_only_in_prompting() -> None:
    offenders = []
    for path in sorted(SRC.rglob("*.py")):
        if path.relative_to(SRC).as_posix() in _NOT_A_PROMPT:
            continue
        offenders += [f"{path.relative_to(SRC)}:{line}" for line in _tty_probes(path.read_text())]
    assert offenders == [], (
        "use jailbee.prompting.is_interactive() to decide whether to ask: " + ", ".join(offenders)
    )


def test_the_tty_probe_checker_flags_what_it_should() -> None:
    assert _tty_probes("import os\nos.isatty(0)\n") == [2]
    assert _tty_probes("import sys\nsys.__stdin__.isatty()\n") == [2]
    assert _tty_probes("import sys\nsys.stdin.isatty()\n") == [2]
    assert _tty_probes("import sys\nsys.stdout.isatty()\n") == []
    assert _tty_probes("import sys\nsys.stderr.isatty()\n") == []


def _walk(cmd: Any, path: str) -> Iterator[tuple[str, Any]]:
    # Typer vendors Click: an isinstance check against top-level `click.Group`
    # is always False here, so groups are recognised by duck typing.
    if hasattr(cmd, "list_commands"):
        ctx = Context(cmd)
        for name in cmd.list_commands(ctx):
            sub = cmd.get_command(ctx, name)
            if sub is not None:
                yield from _walk(sub, f"{path} {name}")
    else:
        yield path, cmd


def test_no_command_has_a_required_positional() -> None:
    from jailbee.container_cli import container_app

    roots = [typer.main.get_command(app), typer.main.get_command(container_app)]
    offenders = [
        f"{path} {param.name}"
        for root in roots
        for path, cmd in _walk(root, "jailbee")
        for param in cmd.params
        if param.param_type_name == "argument" and param.required
    ]
    assert offenders == [], (
        "a missing required value is asked for, never a usage error "
        "(CLAUDE.md, jailbee.prompting): " + ", ".join(offenders)
    )


def test_the_walk_sees_the_whole_tree() -> None:
    paths = {p for p, _ in _walk(typer.main.get_command(app), "jailbee")}
    assert {
        "jailbee exec",
        "jailbee snapshot restore",
        "jailbee account group create",
        "jailbee outbox show",
    } <= paths


# ---- Direct prompt libraries: only `jailbee.prompting` may ask ---------------------
#
# Every question goes through `jailbee.prompting` so a dashboard can answer it over
# the prompt channel (ADR 0006). A direct `typer.confirm`, questionary, Rich prompt or
# `input()` would block on a terminal nobody is watching.

# Allowed places, keyed by path relative to src/jailbee. A key is a file, a directory
# (trailing "/") or "file::function" (the function's qualified name). Each names why.
_DIRECT_PROMPT_ALLOWED = {
    "prompting.py": "the one module that renders questions",
    "config_edit/": "full-screen config editor: a terminal app of its own, not a question",
    "qtui/": "deprecated Qt dashboard, out of scope of the prompt channel (spec #110)",
    "macsetup/cli.py::_confirm": (
        "`jailbee mac setup` runs on the Mac host before jailbee's prompt stack exists "
        "(vendor setup flow, stdlib only)"
    ),
    "remote_ssh/console.py::_select_repo": (
        "the remote console's own startup repository picker: the console holds the "
        "terminal itself, there is no command to forward a question from"
    ),
    "tui.py::_render_choice": "terminal renderer behind prompting.choose_one(picker=)",
    "tui.py::checkbox": "terminal renderer behind prompting.choose_many (forked questionary)",
    "tui.py::pick_container": "terminal renderer passed as choose_one(picker=)",
    "tui.py::pick_submodule": "terminal renderer passed as choose_one(picker=)",
    "tui.py::pick_containers_multi": "terminal renderer passed as choose_many(picker=)",
    "tui.py::pick_submodules_multi.render": "terminal renderer passed as choose_many(picker=)",
    "tui.py::pick_account": "terminal renderer passed as choose_one(picker=) by tui.choose_account",
}

_PROMPT_NAMES = {"Prompt", "Confirm", "IntPrompt", "FloatPrompt"}


class _PromptFinder(ast.NodeVisitor):
    """Collects `(line, enclosing function, what)` for each direct prompt use."""

    def __init__(self) -> None:
        self.hits: list[tuple[int, str, str]] = []
        self._scope: list[str] = []

    def _hit(self, node: ast.AST, what: str) -> None:
        self.hits.append((node.lineno, ".".join(self._scope), what))  # type: ignore[attr-defined]  # every node we pass has lineno

    def _function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        self._scope.append(node.name)
        self.generic_visit(node)
        self._scope.pop()

    visit_FunctionDef = _function  # noqa: N815  (ast.NodeVisitor dispatch name)
    visit_AsyncFunctionDef = _function  # noqa: N815

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            root = alias.name.split(".")[0]
            if root == "questionary":
                self._hit(node, f"import {alias.name}")
            if alias.name == "rich.prompt":
                self._hit(node, "import rich.prompt")

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        module = node.module or ""
        if module.split(".")[0] == "questionary":
            self._hit(node, f"from {module} import ...")
        if module == "rich.prompt" or (
            module == "rich" and any(a.name == "prompt" for a in node.names)
        ):
            self._hit(node, "from rich.prompt import ...")
        if module in {"typer", "typer._click", "click"}:
            for alias in node.names:
                if alias.name in {"confirm", "prompt"}:
                    self._hit(node, f"from {module} import {alias.name}")

    def visit_Call(self, node: ast.Call) -> None:
        func = node.func
        if isinstance(func, ast.Name) and func.id == "input":
            self._hit(node, "input()")
        elif isinstance(func, ast.Attribute):
            owner = func.value
            if (
                func.attr in {"confirm", "prompt"}
                and isinstance(owner, ast.Name)
                and owner.id in {"typer", "click"}
            ):
                self._hit(node, f"{owner.id}.{func.attr}()")
            if func.attr == "ask" and isinstance(owner, ast.Name) and owner.id in _PROMPT_NAMES:
                self._hit(node, f"{owner.id}.ask()")
        self.generic_visit(node)


def _direct_prompts(source: str) -> list[tuple[int, str, str]]:
    finder = _PromptFinder()
    finder.visit(ast.parse(source))
    return finder.hits


def _allowed(rel: str, scope: str) -> str | None:
    """The allow-list key covering this hit, if any."""
    for key in _DIRECT_PROMPT_ALLOWED:
        path, _, function = key.partition("::")
        if path.endswith("/"):
            covered = rel.startswith(path)
        else:
            covered = rel == path
        if covered and (not function or scope == function or scope.startswith(function + ".")):
            return key
    return None


def test_direct_prompt_libraries_only_in_prompting() -> None:
    offenders = []
    used: set[str] = set()
    for path in sorted(SRC.rglob("*.py")):
        rel = path.relative_to(SRC).as_posix()
        for line, scope, what in _direct_prompts(path.read_text()):
            key = _allowed(rel, scope)
            if key is None:
                offenders.append(f"{rel}:{line} ({what})")
            else:
                used.add(key)
    assert offenders == [], (
        "ask through jailbee.prompting (ask_confirm / choose_one / choose_many / ask_text / "
        "ask_secret), never a prompt library directly, so a dashboard can answer over the "
        "prompt channel. Found: " + ", ".join(offenders)
    )
    # Directory entries are standing exemptions; file and function entries must still match.
    stale = sorted(key for key in set(_DIRECT_PROMPT_ALLOWED) - used if not key.endswith("/"))
    assert stale == [], "remove allow-list entries that no longer match anything: " + ", ".join(
        stale
    )


@pytest.mark.parametrize(
    "source, what",
    [
        ("import typer\ntyper.confirm('x')\n", "typer.confirm()"),
        ("import typer\ntyper.prompt('x')\n", "typer.prompt()"),
        ("from typer import confirm\n", "from typer import confirm"),
        ("import questionary\n", "import questionary"),
        ("def f():\n    from questionary import select\n", "from questionary import ..."),
        ("from rich.prompt import Confirm\n", "from rich.prompt import ..."),
        ("from rich.prompt import Confirm\nConfirm.ask('x')\n", "Confirm.ask()"),
        ("IntPrompt.ask('x')\n", "IntPrompt.ask()"),
        ("input('x')\n", "input()"),
    ],
)
def test_the_direct_prompt_checker_flags_what_it_should(source: str, what: str) -> None:
    assert what in [hit[2] for hit in _direct_prompts(source)]


def test_the_direct_prompt_checker_ignores_look_alikes() -> None:
    source = (
        "import typer\n"
        "typer.Option('--yes')\n"
        "console.input\n"
        "x.confirm('a')\n"
        "prompting.ask_confirm('a')\n"
        "def prompt(): ...\n"
    )
    assert _direct_prompts(source) == []


def test_the_direct_prompt_checker_names_the_enclosing_function() -> None:
    source = "def outer():\n    def inner():\n        input('x')\n"
    assert _direct_prompts(source) == [(3, "outer.inner", "input()")]


def test_the_allow_list_matches_files_directories_and_functions() -> None:
    assert _allowed("config_edit/app.py", "") is not None
    assert _allowed("tui.py", "checkbox") is not None
    assert _allowed("tui.py", "pick_containers_multi.render") is not None
    assert _allowed("tui.py", "some_new_prompt") is None
    assert _allowed("cli.py", "") is None


# `tui`'s pickers render a question on the terminal and nothing else. They are
# reachable only as `picker=` of a prompting function (a lambda or a reference), so
# the question still goes through the prompt channel when one is present.
_RENDERERS = {"pick_container", "pick_submodule", "pick_account", "pick_container_for_group"}


def _direct_renderer_calls(source: str) -> list[int]:
    """Lines where a tui renderer is called outside a lambda or a function named
    `*picker*` (a `picker=` callable defined by name)."""
    lines: list[int] = []

    def visit(node: ast.AST, in_lambda: bool) -> None:
        if isinstance(node, ast.Call):
            func = node.func
            name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
            if name in _RENDERERS and not in_lambda:
                lines.append(node.lineno)
        for child in ast.iter_child_nodes(node):
            named_picker = isinstance(node, ast.FunctionDef) and "picker" in node.name
            visit(child, in_lambda or isinstance(node, ast.Lambda) or named_picker)

    visit(ast.parse(source), False)
    return lines


def test_tui_renderers_are_only_called_as_prompting_pickers() -> None:
    offenders = []
    for path in sorted(SRC.rglob("*.py")):
        rel = path.relative_to(SRC).as_posix()
        if rel in {"tui.py", "prompting.py"} or rel.startswith("qtui/"):
            continue
        offenders += [f"{rel}:{line}" for line in _direct_renderer_calls(path.read_text())]
    assert offenders == [], (
        "a tui picker renders on the terminal only; pass it as "
        "`picker=lambda opts: ...` to jailbee.prompting.choose_one so a dashboard can "
        "answer over the prompt channel: " + ", ".join(offenders)
    )


def test_the_renderer_checker_allows_a_picker_lambda_only() -> None:
    assert _direct_renderer_calls("x = pick_container(c)\n") == [1]
    assert _direct_renderer_calls("x = tui.pick_submodule(c)\n") == [1]
    assert (
        _direct_renderer_calls("choose_one('n', o, picker=lambda _o: tui.pick_container(c))\n")
        == []
    )
