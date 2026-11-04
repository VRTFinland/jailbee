"""Read the host's minimal, read-only container PR binding snapshot."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from jailbee.container_outbox.store import OutboxCommandError

SNAPSHOT = Path("/opt/jailbee/metadata/bindings.json")
ERROR_MARKER = Path("/run/jailbee/pr-bindings-error")
KEYS = (
    "user.jailbee.repo_dir",
    "user.jailbee.pr",
    "user.jailbee.stacked_pr",
    "user.jailbee.sub_pr",
    "user.jailbee.branch_prs",
)


@dataclass(frozen=True)
class Binding:
    number: int
    cwd: str


def _check_refresh() -> None:
    try:
        ERROR_MARKER.read_text()
    except FileNotFoundError:
        return
    except OSError as exc:
        raise OutboxCommandError("PR bindings unavailable; restart with jb start", 2) from exc
    raise OutboxCommandError("PR bindings unavailable; restart with jb start", 2)


def read() -> list[Binding]:
    """Never infer a binding from the agent's current checkout or working directory."""
    _check_refresh()
    try:
        raw = json.loads(SNAPSHOT.read_text())
    except FileNotFoundError as exc:
        raise OutboxCommandError(
            "PR bindings unavailable; name the PR number or use --new; run jb start on the host", 2
        ) from exc
    except (OSError, ValueError) as exc:
        raise OutboxCommandError(
            f"cannot read PR bindings: {exc}; restart with jb start", 2
        ) from exc
    if not isinstance(raw, dict):
        raise OutboxCommandError("invalid PR bindings; restart with jb start", 2)
    if "error" in raw:
        raise OutboxCommandError("PR bindings unavailable; restart with jb start", 2)
    root = raw.get("user.jailbee.repo_dir")
    if not isinstance(root, str) or not root.startswith("/"):
        raise OutboxCommandError("missing repository path in PR bindings; restart with jb start", 2)
    result = []

    def add(value: object, cwd: str) -> None:
        if value is None or value == "":
            return
        if (
            isinstance(value, bool)
            or not str(value).isascii()
            or not str(value).isdecimal()
            or int(str(value)) < 1
        ):
            raise OutboxCommandError("invalid PR binding number; repair the host binding", 2)
        result.append(Binding(int(str(value)), cwd))

    for key in KEYS[1:3]:
        add(raw.get(key), root)
    try:
        branches = json.loads(raw.get(KEYS[4], "{}"))
        if not isinstance(branches, dict):
            raise ValueError("branch bindings must be an object")
        for record in branches.values():
            if not isinstance(record, dict):
                raise ValueError("invalid branch binding")
            add(record.get("pr"), root)
    except (ValueError, TypeError) as exc:
        raise OutboxCommandError(f"invalid branch PR bindings: {exc}", 2) from exc
    try:
        sub = json.loads(raw.get(KEYS[3], "{}"))
        if not isinstance(sub, dict):
            raise ValueError("submodule bindings must be an object")
        for path, record in sub.items():
            relative = PurePosixPath(path)
            if (
                not path
                or relative.is_absolute()
                or ".." in relative.parts
                or not isinstance(record, dict)
            ):
                raise ValueError("invalid submodule binding")
            add(record.get("pr"), str(PurePosixPath(root) / relative))
    except (ValueError, TypeError) as exc:
        raise OutboxCommandError(f"invalid submodule PR bindings: {exc}", 2) from exc
    return list(dict.fromkeys(result))


def resolve(number: int | None, *, allow_new: bool = False) -> Binding | None:
    _check_refresh()
    if number is not None and not SNAPSHOT.exists():
        return None
    choices = read()
    if number is not None:
        matches = list(dict.fromkeys(item for item in choices if item.number == number))
        if len(matches) > 1:
            raise OutboxCommandError(
                "PR number matches multiple repository bindings; "
                "unbind the unwanted target on the host before retrying",
                2,
            )
        return matches[0] if matches else None
    if len(choices) == 1:
        return choices[0]
    if not choices and allow_new:
        return None
    candidates = ", ".join(f"{item.number} ({item.cwd})" for item in choices) or "none"
    raise OutboxCommandError(f"name the PR number; container bindings: {candidates}", 2)
