"""Lift the global `--repo PREFIX` option from anywhere in command argv."""

from __future__ import annotations

import os
from collections.abc import Sequence
from pathlib import Path

CTX_KEY = "jailbee.repo"

_OPTION = "--repo"


class RepoOptionError(ValueError):
    """`--repo` was given twice, or without a prefix."""


def _occurrence(argv: Sequence[str], i: int, end: int) -> tuple[str, int]:
    """The prefix at `argv[i]` and how many tokens it spans."""
    token = argv[i]
    if token.startswith(_OPTION + "="):
        value, width = token[len(_OPTION) + 1 :], 1
    else:
        value = argv[i + 1] if i + 1 < end else ""
        width = 2
    if not value or value.startswith("-"):
        raise RepoOptionError("--repo needs a registered repository prefix")
    return value, width


def _is_repo(token: str) -> bool:
    return token == _OPTION or token.startswith(_OPTION + "=")


def check_config_selection(path: Path | None) -> None:
    """Guard direct Click config loaders as well as preprocessed entry points."""
    if path is None:
        return
    from typer._click.globals import get_current_context

    ctx = get_current_context(silent=True)
    if ctx is not None and CTX_KEY in ctx.find_root().meta:
        raise RepoOptionError("--config and --repo both name the repository; give one of them.")


def lift_repo(argv: Sequence[str]) -> tuple[str | None, list[str]]:
    """Return the global repo prefix and argv without that option."""
    args = list(argv)
    end = args.index("--") if "--" in args else len(args)
    if not any(_is_repo(token) for token in args[:end]):
        return None, args

    from jailbee.remote_ssh.router import leaf_owns_option, routable_leaf_paths

    leaves = routable_leaf_paths()
    groups = {" ".join(leaf.split()[:n]) for leaf in leaves for n in range(1, len(leaf.split()))}
    spans: list[tuple[int, int]] = []
    found: list[str] = []
    path: list[str] = []
    leaf: str | None = None
    i = 0
    while i < end:
        token = args[i]
        if _is_repo(token):
            value, width = _occurrence(args, i, end)
            found.append(value)
            spans.append((i, width))
            i += width
            continue
        if token.startswith("-"):
            break
        candidate = " ".join([*path, token])
        if candidate in leaves:
            leaf = candidate
            i += 1
            break
        if candidate not in groups:
            break
        path.append(token)
        i += 1
    if leaf is not None and not leaf_owns_option(leaf, _OPTION):
        while i < end:
            if _is_repo(args[i]):
                value, width = _occurrence(args, i, end)
                found.append(value)
                spans.append((i, width))
                i += width
            else:
                i += 1
    if len(found) > 1:
        raise RepoOptionError("--repo given more than once")
    if not found:
        return None, args
    if any(
        token == "--config" or token.startswith("--config=") or token.startswith("-c")
        for token in args[:end]
    ):
        raise RepoOptionError("--config and --repo both name the repository; give one of them.")
    start, width = spans[0]
    return found[0], args[:start] + args[start + width :]


def with_repo_first(prefix: str | None, argv: Sequence[str]) -> list[str]:
    """Return argv with the global option first, where Click expects it."""
    return [_OPTION, prefix, *argv] if prefix is not None else list(argv)


def resolve_repo_root(prefix: str) -> Path:
    """Resolve a registered repo root, honouring the session's exclusions."""
    from jailbee.remote_ssh.repo_scope import registered_repos, scope_for_session
    from jailbee.remote_ssh.router import RouteError, resolve_repo

    scope = scope_for_session()
    try:
        return resolve_repo(prefix, scope=scope)
    except RouteError as exc:
        known = ", ".join(repo.prefix for repo in registered_repos(scope=scope)) or "none"
        raise RepoOptionError(f"{exc} (registered: {known})") from exc


def enter_repo(prefix: str | None, *, pick: bool) -> str:
    """Enter the registered repository named by prefix, or picked when requested."""
    if pick:
        if prefix is not None:
            raise RepoOptionError("--repo and --pick-repo are exclusive")
        from jailbee import prompting
        from jailbee.remote_ssh.repo_scope import registered_repos, scope_for_session

        choices = registered_repos(scope=scope_for_session())
        prefix = prompting.choose_one(
            "repository",
            [prompting.Option(r.prefix, f"{r.prefix}\t{r.root}", r.prefix) for r in choices],
            empty_reason="no registered repositories",
        )
    if prefix is None:
        raise RepoOptionError("--repo needs a registered repository prefix")
    os.chdir(resolve_repo_root(prefix))
    return prefix
