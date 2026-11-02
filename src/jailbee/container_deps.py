"""Check the dependency versions baked into a container image."""

import importlib.metadata
import json
import re
from pathlib import Path

CONTAINER_REQUIREMENTS: dict[str, str] = {
    "typer": "0.12",
    "rich": "13.7",
    "pyyaml": "6.0",
    "pydantic": "2.6",
    "questionary": "2.0",
    "prompt-toolkit": "3.0",
}
STAMP = Path("/opt/jailbee/deps.json")
_ERROR = (
    "the container's jailbee dependencies are older than this jailbee needs — "
    "run `jb base build` on the host, then recreate or restart the container from the new image"
)


def _version_parts(value: str) -> tuple[int, ...]:
    match = re.match(r"\d+(?:\.\d+)*", value)
    if match is None:
        return ()
    return tuple(int(part) for part in match.group().split("."))


def check(stamp: Path = STAMP) -> str | None:
    try:
        data = json.loads(stamp.read_text())
        packages = data["packages"]
        if not isinstance(packages, dict):
            return _ERROR
        for name, minimum in CONTAINER_REQUIREMENTS.items():
            installed = packages.get(name)
            if not isinstance(installed, str):
                return _ERROR
            actual = _version_parts(installed)
            required = _version_parts(minimum)
            if not actual or actual + (0,) * max(0, len(required) - len(actual)) < required:
                return _ERROR
    except (OSError, ValueError, KeyError, TypeError):
        return _ERROR
    return None


def pinned_requirements() -> list[str]:
    return [f"{name}=={importlib.metadata.version(name)}" for name in CONTAINER_REQUIREMENTS]
