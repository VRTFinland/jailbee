"""The upgrade hint's surfaces: where it appears, on which stream, and that
it can never fail the command it decorates."""

from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

runner = CliRunner()

FIXTURES = Path(__file__).parent / "fixtures"

_NOTES_SENTINEL = ["jailbee 9.9.9 changed what `jb base build` produces:", "    - test note"]


def _stub_advice(mocker, lines=None):
    """Patch the advice computation, not the DB, so these tests assert on
    plumbing (is it called, where does it print) rather than on manifest
    contents that change every release."""
    return mocker.patch(
        "jailbee.upgrade.advice_lines",
        return_value=_NOTES_SENTINEL if lines is None else lines,
    )


def test_advise_upgrade_writes_only_to_stderr(make_cfg, tmp_path, mocker, capsys) -> None:
    """`jailbee ls`'s table is parsed by scripts — the hint must not enter it.

    Called directly rather than through `CliRunner`, because whether the
    runner merges the two streams depends on the Click version and that is
    not what this test is about.
    """
    from jailbee.cli import _advise_upgrade

    _stub_advice(mocker)
    _advise_upgrade(make_cfg(tmp_path))

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "test note" in captured.err


def test_ls_asks_for_advice(mocker) -> None:
    from jailbee.cli import app

    mocker.patch("jailbee.lifecycle.list_containers", return_value=[])
    mocker.patch("jailbee.lifecycle.repo_has_submodules", return_value=False)
    mocker.patch("jailbee.incus.Incus")
    advice = _stub_advice(mocker)

    result = runner.invoke(app, ["ls", "--config", str(FIXTURES / "full_config.yaml")])

    assert result.exit_code == 0, result.output
    assert advice.call_count == 1


def test_new_asks_for_advice(mocker) -> None:
    """`new` is the command that consumes the golden image, so a stale base
    image is exactly what a user running it needs to hear about."""
    from jailbee.cli import app
    from jailbee.egress_pool import RefreshResult
    from jailbee.global_config import DockerRegistryMirror, GlobalConfig

    mocker.patch("jailbee.incus.Incus")
    # full_config declares extra_registries, which makes `new` demand a
    # running mirror container. Nothing here is about the mirror.
    mocker.patch(
        "jailbee.cli._load_global",
        return_value=GlobalConfig(docker_registry_mirror=DockerRegistryMirror(enabled=False)),
    )
    mocker.patch("jailbee.egress_pool.register_repo")
    mocker.patch(
        "jailbee.egress_pool.refresh_pool",
        return_value=RefreshResult(container_prefix="foo", status="ok"),
    )
    # Mount mode needs no real .git at cfg.repo_root; --no-autostart keeps
    # creation to the one mocked call.
    new_container = mocker.patch("jailbee.lifecycle.new_container", return_value="foo-smokebox")
    advice = _stub_advice(mocker)

    result = runner.invoke(
        app,
        [
            "new",
            "smokebox",
            "--mount",
            "--no-autostart",
            "--config",
            str(FIXTURES / "full_config.yaml"),
        ],
    )

    assert result.exit_code == 0, result.output
    assert new_container.call_count == 1
    assert advice.call_count == 1


def test_shell_asks_for_advice_before_attaching(mocker) -> None:
    """Order is the guarantee, not just the two calls: `shell` ends in
    `raise typer.Exit(_attach_shell(...))`, so advice placed after the attach
    would never reach the user."""
    from jailbee.cli import app

    mocker.patch("jailbee.cli._resolve_attachable", return_value=(mocker.MagicMock(), "c1"))
    attach = mocker.patch("jailbee.cli._attach_shell", return_value=0)
    advice = _stub_advice(mocker)

    # A shared parent is the only way mock records a cross-mock call order.
    calls = mocker.MagicMock()
    calls.attach_mock(advice, "advice")
    calls.attach_mock(attach, "attach")

    result = runner.invoke(app, ["shell", "c1", "--config", str(FIXTURES / "full_config.yaml")])

    assert result.exit_code == 0, result.output
    assert [name for name, _, _ in calls.mock_calls] == ["advice", "attach"]


def test_a_broken_state_db_does_not_break_the_command(mocker) -> None:
    """Advice is a courtesy. Anything going wrong in it must be swallowed."""
    from jailbee.cli import app

    mocker.patch("jailbee.lifecycle.list_containers", return_value=[])
    mocker.patch("jailbee.lifecycle.repo_has_submodules", return_value=False)
    mocker.patch("jailbee.incus.Incus")
    mocker.patch("jailbee.upgrade.advice_lines", side_effect=RuntimeError("db is locked"))

    result = runner.invoke(app, ["ls", "--config", str(FIXTURES / "full_config.yaml")])

    assert result.exit_code == 0, result.output
    assert "db is locked" not in result.output


def test_nothing_pending_prints_nothing(make_cfg, tmp_path, mocker, capsys) -> None:
    from jailbee.cli import _advise_upgrade

    _stub_advice(mocker, lines=[])
    _advise_upgrade(make_cfg(tmp_path))

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""
