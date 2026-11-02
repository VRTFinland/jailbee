import importlib.metadata
import json
import tomllib
from pathlib import Path

import pytest

from jailbee import container_deps


def test_requirements_mirror_pyproject() -> None:
    data = tomllib.loads((Path(__file__).resolve().parents[1] / "pyproject.toml").read_text())
    for name, minimum in container_deps.CONTAINER_REQUIREMENTS.items():
        assert f"{name}>={minimum}" in data["project"]["dependencies"]


def test_check_missing_stamp(tmp_path: Path) -> None:
    assert "jb base build" in (container_deps.check(tmp_path / "missing") or "")


def test_check_below_minimum(tmp_path: Path) -> None:
    stamp = tmp_path / "deps.json"
    stamp.write_text(json.dumps({"packages": {"typer": "0.11.9", "rich": "13.7", "pyyaml": "6.0", "pydantic": "2.6"}}))
    assert "jb base build" in (container_deps.check(stamp) or "")


def test_check_ok(tmp_path: Path) -> None:
    stamp = tmp_path / "deps.json"
    stamp.write_text(json.dumps({"packages": {"typer": "0.12.0", "rich": "13.7", "pyyaml": "6.0", "pydantic": "2.11.0rc1"}}))
    assert container_deps.check(stamp) is None


@pytest.mark.parametrize("content", ["invalid", "{}", '{"packages": null}', '{"packages": {"typer": 42}}'])
def test_check_malformed_stamp(tmp_path: Path, content: str) -> None:
    stamp = tmp_path / "deps.json"
    stamp.write_text(content)
    assert "jb base build" in (container_deps.check(stamp) or "")


def test_pinned_requirements_uses_installed_versions(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(importlib.metadata, "version", lambda name: "1.2.3")
    assert container_deps.pinned_requirements() == [
        "typer==1.2.3", "rich==1.2.3", "pyyaml==1.2.3", "pydantic==1.2.3"
    ]
