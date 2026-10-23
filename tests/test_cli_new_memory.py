"""Creation follows the profile unless a valid memory override is explicit."""

import pytest
from typer.testing import CliRunner

from jailbee.cli import app
from jailbee.memory_limit import validate_size
from tests.test_cli_fork import SRC, _setup
from tests.test_cli_new_storage import _new


@pytest.mark.parametrize("memory", [None, "24GiB", "50%"])
def test_new_preserves_explicit_memory(tmp_path, mocker, memory):
    args = [] if memory is None else ["--memory", memory]
    result, create = _new(tmp_path, mocker, *args)
    assert result.exit_code == 0, result.output
    assert create.call_args.args[2].memory == memory


@pytest.mark.parametrize("memory", [None, "8GiB"])
def test_fork_preserves_explicit_memory(tmp_path, mocker, memory):
    _, create = _setup(tmp_path, mocker)
    mocker.patch("jailbee.cli._resolve_existing", return_value=(mocker.MagicMock(), "myrepo-src"))
    mocker.patch("jailbee.forking.prepare_fork", return_value=SRC)
    args = [] if memory is None else ["--memory", memory]
    result = CliRunner().invoke(
        app, ["fork", "src", "b", "--no-autostart", "--no-background", *args]
    )
    assert result.exit_code == 0, result.output
    assert create.call_args.args[2].memory == memory
    assert create.call_args.args[2].fork_of == SRC.full_name


@pytest.mark.parametrize("command", ["new", "fork"])
@pytest.mark.parametrize("memory", ["", "0GiB", "101%", "default", "lots"])
def test_invalid_memory_exits_before_creation(tmp_path, mocker, command, memory):
    _, create = _setup(tmp_path, mocker)
    mocker.patch("jailbee.cli._resolve_existing", return_value=(mocker.MagicMock(), "myrepo-src"))
    prepare = mocker.patch("jailbee.forking.prepare_fork", return_value=SRC)
    pin = mocker.patch("jailbee.forking.pin_fork_commit")
    args = ["new", "feat-a"] if command == "new" else ["fork", "src", "b"]
    result = CliRunner().invoke(app, [*args, "--memory", memory, "--no-background"])
    assert result.exit_code == 2, result.output
    assert validate_size(memory) in " ".join(result.output.split())
    create.assert_not_called()
    prepare.assert_not_called()
    pin.assert_not_called()


@pytest.mark.parametrize("command", ["new", "fork"])
def test_cli_creation_reaches_shared_unupgraded_profile_guard(tmp_path, mocker, command):
    from jailbee.lifecycle import new_container

    _, create = _setup(tmp_path, mocker)
    create.side_effect = new_container
    incus = __import__("jailbee.incus", fromlist=["Incus"]).Incus.return_value
    incus.list_containers.return_value = []
    incus.profile_show.return_value = "config: {}"
    mocker.patch("jailbee.cli._resolve_existing", return_value=(incus, "myrepo-src"))
    mocker.patch("jailbee.forking.prepare_fork", return_value=SRC)
    mocker.patch("jailbee.lifecycle.branch_exists_locally", return_value=True)
    args = ["new", "feat-a", "--no-clone"] if command == "new" else ["fork", "src", "b"]
    result = CliRunner().invoke(app, [*args, "--no-autostart", "--no-background"])
    assert result.exit_code == 2, result.output
    assert "jb apply" in result.output
    incus.init.assert_not_called()
    incus.copy.assert_not_called()
    incus.start.assert_not_called()
