"""Which background jobs are only the inbox follower or an inbox waiter.

Claude Code reports a session as ``shell`` when it is idle but a background
task still runs. The JailBee plugin's inbox follower is such a task in every
session, so an idle session would read ``shell`` forever. `settle_state`
reads that as idle when every process under the agent is an inbox waiter or
the shell wrapper Claude runs it in.

This module is shared by two callers and must stay stdlib-only, free of
jailbee imports and runnable as plain source: the orchestration probe
(`orchestration.agent_watch`) sends its text into the container and executes
it there with the container's ``python3``, and the dashboards call it on the
host with argv read from ``/proc``.

The classification is deliberately strict: an unknown wrapper or any queued
work after the waiter is "not a waiter", so real background work is never
hidden.
"""

from __future__ import annotations

import pathlib
import re
import shlex
from collections.abc import Sequence


def waiter(argv: Sequence[str]) -> bool:
    # Match inbox listener entry points, not arguments that mention wait or follow.
    if not argv:
        return False
    entry = pathlib.Path(argv[0]).name
    args = list(argv[1:])
    if entry in ("python", "python3") or entry.startswith("python3."):
        # The container entry point uses safe-path mode; without it, -m can
        # import a different jailbee from the wrapper's working directory.
        if args[:1] != ["-P"]:
            return False
        args = args[1:]
        if (
            len(args) < 2
            or args[0] != "-m"
            or args[1] not in ("jailbee", "jailbee.container_main", "jailbee.inbox")
        ):
            return False
        args = args[2:]
    elif entry not in ("jailbee", "jb"):
        return False
    if args[:1] == ["inbox"]:
        args = args[1:]
    return args[:1] in (["wait"], ["follow"])


def strip_redirects(script: str) -> str:
    return re.sub(r"(?:[ \t]+(?:> ?/dev/null|2>&1|2> ?/dev/null|< ?/dev/null))+$", "", script)


def plain_waiter(script: str) -> bool:
    script = strip_redirects(script)
    # A single invocation: no expansion, control operators, or other redirects,
    # even inside quotes. shlex validates quoting but never executes anything.
    if re.fullmatch(r"""[A-Za-z0-9_./: ,='"-]+""", script) is None:
        return False
    return waiter(shlex.split(script))


def statements(script: str) -> list[tuple[str, str]] | None:
    # Retain raw quotes for eval decoding. Split only at unquoted, top-level
    # operators; completed brace/subshell prefixes are opaque, not candidates.
    result: list[tuple[str, str]] = []
    stack: list[str] = []
    quote: str | None = None
    start, index = 0, 0
    while index < len(script):
        char = script[index]
        if quote:
            if char == quote:
                quote = None
            elif quote == '"' and char in "$`":
                return None
            elif quote == '"' and char == "\\":
                index += 1
        elif char == "\\":
            index += 1
            if index == len(script):
                return None
        elif char in "'\"":
            quote = char
        elif char in "$`#" or script.startswith("<<", index):
            return None
        elif script.startswith(("2>&1", ">&1", ">|"), index):
            redirect = next(
                value for value in ("2>&1", ">&1", ">|") if script.startswith(value, index)
            )
            index += len(redirect) - 1
        elif char in "{(":
            stack.append("}" if char == "{" else ")")
        elif char in "})":
            if not stack or stack.pop() != char:
                return None
        elif char in "&|;":
            operator = char
            if script.startswith(("&&", "||"), index):
                operator *= 2
                index += 1
            elif char == "&":
                return None
            if not stack:
                result.append((script[start : index + 1 - len(operator)].strip(), operator))
                start = index + 1
        index += 1
    if quote or stack:
        return None
    result.append((script[start:].strip(), ""))
    # Keyword compounds are not parsed; refusing them also prevents an inner
    # waiter from being mistaken for a top-level command after a semicolon.
    reserved = {
        "while", "until", "for", "select", "if", "then", "elif", "else",
        "fi", "do", "done", "case", "esac", "function", "!",
    }  # fmt: skip
    for statement, operator in result:
        words = shlex.split(statement)
        if not words or words[0] in reserved or operator == "|":
            return None
    return result


def waiter_statement(script: str) -> bool:
    script = strip_redirects(script)
    if plain_waiter(script):
        return True
    match = re.fullmatch(r"""eval ('(?:[^']|'"'"')*')""", script)
    if match is None:
        return False
    inner = match[1][1:-1].replace("'\"'\"'", "'")
    return plain_waiter(inner)


def builtin_prefix(script: str, home: pathlib.Path) -> bool:
    script = re.sub(r"\s+\|\|\s+(?:true|false)$", "", script)
    script = strip_redirects(script)
    if script.startswith("{ "):
        if not script.endswith("}"):
            return False
        body = script[2:-1].strip().removesuffix(";").strip()
        parts = statements(body)
        return parts is not None and all(builtin_prefix(part, home) for part, _ in parts)
    if any(char in script for char in "(){}$`<>"):
        return False
    words = shlex.split(script)
    if not words:
        return False
    if words[0] in ("builtin", "command"):
        words = words[1:]
    if not words:
        return False
    if words[0] in ("source", "."):
        # Trust only Claude-generated snapshots under the agent user's home.
        folder = home / ".claude" / "shell-snapshots"
        return (
            len(words) == 2
            and pathlib.Path(words[1]).parent == folder
            and re.fullmatch(r"[A-Za-z0-9_.-]+\.sh", pathlib.Path(words[1]).name) is not None
        )
    # Setup must not install a different implementation of a later command.
    if words[0] == "hash" and any(word.startswith("-") and "p" in word for word in words[1:]):
        return False
    # With `set -k` and POSIX mode even `: NAME=value` can export NAME.
    # Refuse assignment-shaped arguments for every setup builtin, not just
    # declarations; this also excludes alias definitions.
    if any("=" in word for word in words[1:]):
        return False
    if words[0] in ("export", "declare", "typeset"):
        # Bare names only: assignments and function/nameref flags can rebind
        # later commands even when the observed leaf still looks like a waiter.
        sensitive = {
            "PATH", "BASH_ENV", "ENV", "IFS", "CDPATH", "SHELLOPTS",
            "BASHOPTS", "PS4", "PROMPT_COMMAND",
        }  # fmt: skip
        for word in words[1:]:
            if word == "--" or re.fullmatch(r"-[rxp]+", word):
                continue
            if (
                re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", word) is None
                or word in sensitive
                or word.startswith(("LD_", "PYTHON"))
            ):
                return False
    return words[0] in {
        "shopt", "set", "unalias", "unset", "export", "declare",
        "typeset", "cd", "true", "false", ":", "umask", "hash", "alias",
    }  # fmt: skip


def claude_waiter(script: str, home: pathlib.Path) -> bool:
    parts = statements(script)
    if parts is None:
        return False
    candidates = [
        index for index, (statement, _) in enumerate(parts) if waiter_statement(statement)
    ]
    if len(candidates) != 1:
        return False
    index = candidates[0]
    # Nonbuiltin calls could still be running an earlier waiter with queued
    # work. Only known setup builtins and trusted snapshots can precede it.
    if not all(builtin_prefix(prefix, home) for prefix, _ in parts[:index]):
        return False
    _, operator = parts[index]
    if index and parts[index - 1][1] not in ("&&", ";"):
        return False
    if index == len(parts) - 1:
        return operator == ""
    if index != len(parts) - 2 or operator not in ("&&", ";"):
        return False
    tail, final_operator = parts[-1]
    return (
        not final_operator
        and re.fullmatch(
            r"""pwd(?: -P)?[ \t]*>\|?[ \t]*(?:[A-Za-z0-9_./-]+|'[A-Za-z0-9_./ -]+')""", tail
        )
        is not None
    )


def waiter_shell(argv: Sequence[str], home: pathlib.Path) -> bool:
    # Unknown harness wrappers must refuse rather than hide queued work.
    if (
        len(argv) != 3
        or pathlib.Path(argv[0]).name not in ("bash", "sh", "dash")
        or argv[1] != "-c"
        or "\n" in argv[2]
        or "\r" in argv[2]
    ):
        return False
    if plain_waiter(argv[2]):
        return True
    return pathlib.Path(argv[0]).name in ("bash", "sh") and claude_waiter(argv[2], home)


def settle_state(state: str, children: Sequence[Sequence[str]], *, home: pathlib.Path) -> str:
    """`state` as shown: ``shell`` becomes ``idle`` when only inbox waiters run.

    `children` is the argv of every process under the agent process (any
    depth); `home` is the agent user's home, where Claude's trusted shell
    snapshots live. Only ``shell`` is ever changed, and only when there is
    at least one child and each is an inbox waiter or a shell wrapper around
    exactly one. No children means Claude's own word stands: nothing is
    invented from an empty process list.
    """
    if state != "shell" or not children:
        return state
    if all(waiter(argv) or waiter_shell(argv, home) for argv in children):
        return "idle"
    return state
