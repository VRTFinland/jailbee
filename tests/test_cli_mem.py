from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from jailbee.cli import app
from jailbee.memory_limit import size_options

GiB = 1024**3
runner = CliRunner()


@pytest.fixture
def rig(mocker, make_cfg, tmp_path):
    cfg = make_cfg(tmp_path)
    incus = mocker.Mock()
    full = f"{cfg.container_prefix}-feat-1"
    row = SimpleNamespace(
        name=full, memory_usage=5 * GiB, memory_limit="24GiB", memory_override=True
    )
    mocker.patch("jailbee.cli._load_or_exit", return_value=cfg)
    resolve = mocker.patch("jailbee.cli._resolve_existing", return_value=(incus, full))
    gather = mocker.patch("jailbee.lifecycle.list_containers", return_value=[row])
    mocker.patch("jailbee.prompting.is_interactive", return_value=False)
    return cfg, incus, full, row, resolve, gather


@pytest.mark.parametrize("size", ["24GiB", "default"])
def test_mem_sets_or_clears_override(rig, size):
    _, incus, full, _, _, _ = rig
    result = runner.invoke(app, ["mem", "feat-1", size])
    assert result.exit_code == 0, result.output
    if size == "default":
        incus.config_unset_checked.assert_called_once_with(full, "limits.memory")
        assert "feat-1: memory limit follows the default (16GiB)" in result.output
    else:
        incus.config_set.assert_called_once_with(full, "limits.memory", size)
        assert "feat-1: memory limit 24GiB (override)" in result.output


def test_mem_invalid_size_exits_two_without_writing(rig):
    _, incus, _, _, _, _ = rig
    result = runner.invoke(app, ["mem", "feat-1", "lots"])
    assert result.exit_code == 2
    assert "positive Incus memory size" in result.output
    incus.config_set.assert_not_called()
    incus.config_unset_checked.assert_not_called()


@pytest.mark.parametrize("size", ["4GiB", "default"])
@pytest.mark.parametrize("force", [False, True])
def test_mem_below_usage_requires_force_off_tty(rig, size, force):
    _, incus, full, row, _, _ = rig
    row.memory_usage = 20 * GiB
    result = runner.invoke(app, ["mem", "feat-1", size, *(["--force"] if force else [])])
    assert result.exit_code == (0 if force else 2), result.output
    if force:
        if size == "default":
            incus.config_unset_checked.assert_called_once_with(full, "limits.memory")
        else:
            incus.config_set.assert_called_once_with(full, "limits.memory", size)
    else:
        assert "--force" in result.output
        assert "usage" in result.output
        incus.config_set.assert_not_called()
        incus.config_unset_checked.assert_not_called()


@pytest.mark.parametrize("accept", [False, True])
def test_mem_below_usage_confirms_on_tty(rig, mocker, accept):
    _, incus, _, _, _, _ = rig
    mocker.patch("jailbee.prompting.is_interactive", return_value=True)
    confirm = mocker.patch("jailbee.prompting._confirm", return_value=accept)
    result = runner.invoke(app, ["mem", "feat-1", "4GiB"])
    assert result.exit_code == (0 if accept else 1), result.output
    assert "OOM" in confirm.call_args.args[0]
    assert incus.config_set.called is accept


def test_mem_missing_size_off_tty_names_size(rig):
    result = runner.invoke(app, ["mem", "feat-1"])
    assert result.exit_code == 2
    assert "missing memory limit" in result.output


@pytest.mark.parametrize("choice", ["8GiB", "other"])
def test_mem_missing_positionals_uses_resolver_and_size_options(rig, mocker, choice):
    cfg, incus, full, row, resolve, _ = rig
    mocker.patch("jailbee.prompting.is_interactive", return_value=True)
    mocker.patch("jailbee.sysstat.read_memory", return_value=SimpleNamespace(total=20 * GiB))
    select = mocker.patch("jailbee.prompting._select", return_value=choice)
    ask = mocker.patch("jailbee.prompting._ask", return_value="12GiB")
    result = runner.invoke(app, ["mem"])
    assert result.exit_code == 0, result.output
    resolve.assert_called_once_with(cfg, None)
    noun, options = select.call_args.args
    assert noun == "memory limit"
    expected = size_options(
        current=row.memory_limit, override=True, default=cfg.defaults.memory, host_total=20 * GiB
    )
    assert [(o.value, o.title) for o in options] == [(o.value, o.label) for o in expected]
    incus.config_set.assert_called_once_with(
        full, "limits.memory", "12GiB" if choice == "other" else "8GiB"
    )
    assert ask.called is (choice == "other")
