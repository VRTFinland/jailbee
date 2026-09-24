"""Pure argv construction and completion for dashboard command input."""

from __future__ import annotations

import shlex
from collections.abc import Sequence

from typer._click.core import ParameterSource

from jailbee.config.models_remote import RemoteSSHConfig
from jailbee.remote_ssh import router
from jailbee.remote_ssh.router import RouteError

# Only these leaf positionals are unambiguously a container selector/source.
# In particular, branch-creating commands and multi-container commands are
# intentionally absent.
_CONTAINER_POSITIONALS: dict[str, str] = {
    "shell": "name",
    "tmux": "name",
    "git diff": "name",
    "merge": "sources",
    "git merge": "sources",
}


def check_dashboard_command(
    argv: Sequence[str], policy: RemoteSSHConfig | None, *, over_ssh: bool
) -> None:
    """Refuse dashboard-launched SSH commands outside the effective policy."""
    if not over_ssh:
        return
    if policy is None:
        raise RouteError("remote SSH dashboard has no server policy")
    if not policy.exec:
        raise RouteError("remote command execution is disabled")
    router.policy_allows(argv, policy.commands, restrict_host=policy.restrict_host)


def command_argv(text: str, selected_container: str | None) -> list[str]:
    """Parse command text as argv and fill a safe omitted container positional."""
    try:
        argv = shlex.split(text)
    except ValueError as error:
        raise ValueError(f"cannot parse command: {error}") from error
    if not argv:
        raise ValueError("command cannot be empty")
    typed, command = router.command_leaf(argv)
    positional = _CONTAINER_POSITIONALS.get(typed)
    if selected_container is None or positional is None:
        return argv
    context = command.make_context(typed.split()[-1], argv[len(typed.split()):], resilient_parsing=True)
    with context:
        if context.get_parameter_source(positional) is ParameterSource.DEFAULT:
            argv.append(selected_container)
    return argv


def _partial_words(text: str) -> tuple[list[str], str]:
    """Split completed words from the current fragment, tolerating open quotes."""
    words: list[str] = []
    token: list[str] = []
    quote: str | None = None
    escaped = False
    ended_with_separator = False
    for char in text:
        if escaped:
            token.append(char)
            escaped = False
            ended_with_separator = False
        elif char == "\\" and quote != "'":
            escaped = True
            ended_with_separator = False
        elif quote is not None:
            if char == quote:
                quote = None
            else:
                token.append(char)
            ended_with_separator = False
        elif char in ("'", '"'):
            quote = char
            ended_with_separator = False
        elif char.isspace():
            if token:
                words.append("".join(token))
                token.clear()
            ended_with_separator = True
        else:
            token.append(char)
            ended_with_separator = False
    if escaped:
        token.append("\\")
    if ended_with_separator and quote is None:
        return words, ""
    return words, "".join(token)


def completion_candidates(
    text: str,
    containers: Sequence[str],
    allowed_paths: frozenset[str] | None = None,
) -> tuple[str, ...]:
    """Complete cached command paths, Click options, or selected-repo containers."""
    words, fragment = _partial_words(text)
    paths = set(router.known_command_paths())
    aliases = router.known_command_aliases()
    paths.update(aliases)
    candidates: set[str] = set()
    for path in paths:
        canonical = aliases.get(path, path)
        if allowed_paths is not None and canonical not in allowed_paths:
            continue
        pieces = path.split()
        prefix_words = words
        if pieces[: len(prefix_words)] != prefix_words:
            continue
        next_piece = pieces[len(prefix_words)] if len(pieces) > len(prefix_words) else ""
        if next_piece.startswith(fragment):
            candidates.add(" ".join((*prefix_words, next_piece)))
        if not words and len(pieces) > 1 and pieces[0].startswith(fragment):
            candidates.add(pieces[0])

    try:
        typed, command = router.command_leaf(words)
    except ValueError:
        typed = ""
        command = None
    permitted = False
    if command is not None:
        canonical = router.command_path(words)
        permitted = allowed_paths is None or canonical in allowed_paths
    if command is not None and permitted:
        candidates.update(
            option
            for param in command.params
            for option in param.opts
            if option.startswith("-") and option.startswith(fragment)
        )
        positional = _CONTAINER_POSITIONALS.get(typed)
        if positional is not None and not fragment.startswith("-"):
            candidates.update(name for name in containers if name.startswith(fragment))
    return tuple(sorted(candidates))
