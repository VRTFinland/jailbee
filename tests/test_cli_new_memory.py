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
