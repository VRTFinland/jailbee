"""Persisted and current runtime arguments for services."""

import json
import os
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class Launch:
    args: tuple[str, ...] = ()
    env: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class LaunchRequest:
    args: tuple[str, ...] | None
    env: Mapping[str, str]
    persist: bool
    reset: bool


class LaunchStore:
    def __init__(self, path: Path) -> None:
        self.path = path

    def _read(self) -> dict[str, dict[str, object]]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, ValueError, RecursionError):
            return {}
        if not isinstance(data, dict):
            return {}
        return {name: value for name, value in data.items() if isinstance(name, str) and isinstance(value, dict)}

    @staticmethod
    def _launch(value: object) -> Launch | None:
        if not isinstance(value, dict):
            return None
        args = value.get("args")
        env = value.get("env")
        if (
            not isinstance(args, list)
            or not all(isinstance(arg, str) for arg in args)
            or not isinstance(env, dict)
            or not all(isinstance(key, str) and isinstance(item, str) for key, item in env.items())
        ):
            return None
        return Launch(tuple(args), env)

    def persisted(self, name: str) -> Launch:
        launch = self._launch(self._read().get(name, {}).get("persisted"))
        return launch if launch is not None else Launch()

    def current(self, name: str) -> tuple[Launch, bool] | None:
        current = self._read().get(name, {}).get("current")
        if not isinstance(current, dict):
            return None
        launch = self._launch(current)
        flag = current.get("persisted_args")
        if launch is None or not isinstance(flag, bool):
            return None
        return launch, flag

    def set_persisted(self, name: str, launch: Launch) -> None:
        data = self._read()
        item = data.setdefault(name, {})
        item["persisted"] = {"args": list(launch.args), "env": dict(launch.env)}
        self._write(data)

    def clear_persisted(self, name: str) -> None:
        data = self._read()
        item = data.get(name)
        if item is not None:
            item.pop("persisted", None)
            if not item:
                data.pop(name)
            self._write(data)

    def set_current(self, name: str, launch: Launch, args_persisted: bool) -> None:
        data = self._read()
        item = data.setdefault(name, {})
        item["current"] = {
            "args": list(launch.args),
            "env": dict(launch.env),
            "persisted_args": args_persisted,
        }
        self._write(data)

    def clear_current(self, name: str) -> None:
        data = self._read()
        item = data.get(name)
        if item is not None:
            item.pop("current", None)
            if not item:
                data.pop(name)
            self._write(data)

    def _write(self, data: dict[str, dict[str, object]]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=self.path.name + ".", dir=self.path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(data, stream)
            os.chmod(temporary, 0o600)
            os.replace(temporary, self.path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)


def resolve_launch(
    persisted: Launch,
    *,
    args: Sequence[str] | None,
    env: Mapping[str, str],
    persist: bool,
    reset: bool,
) -> tuple[Launch, Launch, bool]:
    base = Launch() if reset else persisted
    effective_args = tuple(args) if args is not None else base.args
    effective_env = {**base.env, **env}
    effective = Launch(effective_args, effective_env)
    if persist:
        new_persisted = effective
        effective = new_persisted
        args_persisted = True
    else:
        new_persisted = base
        args_persisted = args is None and not reset
    return new_persisted, effective, args_persisted
