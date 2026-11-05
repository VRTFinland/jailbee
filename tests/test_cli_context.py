from pathlib import Path

import pytest

from jailbee import cli_context


def test_env_override_wins(tmp_path: Path) -> None:
    for context in ("host", "container"):
        assert cli_context.detect({"JAILBEE_CONTEXT": context}, tmp_path / "none", "/x") == context
    marker = tmp_path / "marker"
    marker.touch()
    assert (
        cli_context.detect(
            {"JAILBEE_CONTEXT": "host"}, marker, "/opt/jailbee/app/current/jailbee/__init__.py"
        )
        == "host"
    )


def test_marker_and_mount_path_means_container(tmp_path: Path) -> None:
    marker = tmp_path / "marker"
    marker.touch()
    assert (
        cli_context.detect({}, marker, "/opt/jailbee/app/current/jailbee/__init__.py")
        == "container"
    )


def test_marker_without_mount_path_is_host(tmp_path: Path) -> None:
    marker = tmp_path / "marker"
    marker.touch()
    assert cli_context.detect({}, marker, "/home/dev/repo/src/jailbee/__init__.py") == "host"
    assert cli_context.detect({}, marker, "/opt/jailbee/app-other/jailbee/__init__.py") == "host"


def test_wrong_side_message(monkeypatch: pytest.MonkeyPatch) -> None:
    assert "runs on the host" in (cli_context.wrong_side_message("new", "container") or "")
    assert cli_context.wrong_side_message("config", "container") is None
    monkeypatch.setattr(cli_context, "CONTAINER_COMMANDS", frozenset({"service"}))
    assert "only available inside a jailbee container" in (
        cli_context.wrong_side_message("service", "host") or ""
    )
    assert cli_context.wrong_side_message("unknown", "container") is None


def test_host_refuses_inbox() -> None:
    assert cli_context.CONTAINER_COMMANDS == frozenset({"inbox", "_agent-snapshot"})
    assert cli_context.wrong_side_message("inbox", "host") == (
        "`jailbee inbox` is only available inside a jailbee container."
    )


def test_import_time_version_fallback(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import importlib.metadata
    import runpy

    import jailbee

    package = tmp_path / "jailbee"
    package.mkdir()
    init = package / "__init__.py"
    init.write_text(Path(jailbee.__file__).read_text())
    (tmp_path / "VERSION").write_text("1.8.1 abcd\n")

    def missing(name: str) -> str:
        raise importlib.metadata.PackageNotFoundError(name)

    monkeypatch.setattr(importlib.metadata, "version", missing)
    assert runpy.run_path(str(init))["__version__"] == "1.8.1"


def test_version_falls_back_to_staged_file(tmp_path: Path) -> None:
    from jailbee import _version_from

    (tmp_path / "VERSION").write_text("1.8.1 abcd\n")
    assert _version_from(tmp_path / "jailbee") == "1.8.1"
    (tmp_path / "VERSION").write_text("")
    assert _version_from(tmp_path / "jailbee") == "0.0.0+unknown"
    assert _version_from(tmp_path / "missing" / "jailbee") == "0.0.0+unknown"
