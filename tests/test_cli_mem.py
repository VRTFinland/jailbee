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
    incus.config_show.return_value = "profiles: [base]"
    incus.profile_show.return_value = "config: {limits.memory: 16GiB}"
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
@pytest.mark.parametrize("size", ["4GiB", "default"])
def test_mem_below_usage_confirms_on_tty(rig, mocker, accept, size):
    _, incus, _, row, _, _ = rig
    row.memory_usage = 20 * GiB
    mocker.patch("jailbee.prompting.is_interactive", return_value=True)
    confirm = mocker.patch("jailbee.prompting._confirm", return_value=accept)
    result = runner.invoke(app, ["mem", "feat-1", size])
    assert result.exit_code == (0 if accept else 1), result.output
    assert "OOM" in confirm.call_args.args[0]
    assert incus.config_set.called is (accept and size != "default")
    assert incus.config_unset_checked.called is (accept and size == "default")


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
    assert [(o.value, o.title) for o in options] == [(o.value, o.label + (" *" if o.current else "")) for o in expected]
    incus.config_set.assert_called_once_with(
        full, "limits.memory", "12GiB" if choice == "other" else "8GiB"
    )
    assert ask.called is (choice == "other")


@pytest.mark.parametrize(('configured', 'inherited', 'force', 'allowed'), [
    ('32GiB', '8GiB', False, False), ('32GiB', '8GiB', True, True),
    ('8GiB', '32GiB', False, True), ('32GiB', None, True, False),
])
def test_clear_uses_actual_inheritance_not_unapplied_config(rig, configured, inherited, force, allowed):
    cfg, incus, _, row, _, _ = rig
    cfg.defaults.memory = configured
    row.memory_usage = 12 * GiB
    incus.profile_show.return_value = f'config: {{limits.memory: {inherited}}}' if inherited else 'config: {}'
    result = runner.invoke(app, ['mem', 'feat-1', 'default', *(['--force'] if force else [])])
    assert result.exit_code == (0 if allowed else 2), result.output
    assert incus.config_unset_checked.called is allowed
    incus.config_set.assert_not_called()
    assert (inherited or 'apply') in result.output


@pytest.mark.parametrize('accept', [False, True])
def test_clear_tty_confirmation_names_actual_target(rig, mocker, accept):
    cfg, incus, _, row, _, _ = rig
    cfg.defaults.memory = '32GiB'
    row.memory_usage = 12 * GiB
    incus.profile_show.return_value = 'config: {limits.memory: 8GiB}'
    mocker.patch('jailbee.prompting.is_interactive', return_value=True)
    confirm = mocker.patch('jailbee.prompting._confirm', return_value=accept)
    result = runner.invoke(app, ['mem', 'feat-1', 'default'])
    assert result.exit_code == (0 if accept else 1), result.output
    assert '8GiB' in confirm.call_args.args[0]
    assert incus.config_unset_checked.called is accept
    incus.config_set.assert_not_called()
    if accept:
        assert '(8GiB)' in result.output


@pytest.mark.parametrize(('current', 'override', 'marked'), [('16384MiB', True, '16GiB'), ('50%', True, '50%'), ('16GiB', False, 'default')])
def test_cli_picker_renders_current_marker(rig, mocker, current, override, marked):
    _, _, _, row, _, _ = rig
    row.memory_limit, row.memory_override = current, override
    mocker.patch('jailbee.sysstat.read_memory', return_value=None)
    mocker.patch('jailbee.prompting.is_interactive', return_value=True)
    select = mocker.patch('jailbee.prompting._select', return_value='24GiB')
    result = runner.invoke(app, ['mem', 'feat-1'])
    assert result.exit_code == 0, result.output
    options = select.call_args.args[1]
    assert [o.value for o in options if o.title.endswith(' *')] == [marked]


def test_confirmed_target_is_checked_again_in_cli(rig):
    _, incus, _, row, _, _ = rig
    row.memory_usage = 12 * GiB
    incus.profile_show.return_value = 'config: {limits.memory: 4GiB}'
    result = runner.invoke(app, ['mem', 'feat-1', 'default', '--force', '--confirmed-target', '8GiB'])
    assert result.exit_code == 2, result.output
    assert 'changed' in result.output
    incus.config_unset_checked.assert_not_called()


def test_clear_rechecks_profile_after_interactive_consent(rig, mocker):
    _, incus, _, row, _, _ = rig
    row.memory_usage = 20 * GiB
    incus.profile_show.side_effect = ['config: {limits.memory: 16GiB}', 'config: {limits.memory: 4GiB}']
    mocker.patch('jailbee.prompting.is_interactive', return_value=True)
    mocker.patch('jailbee.prompting._confirm', return_value=True)
    result = runner.invoke(app, ['mem', 'feat-1', 'default'])
    assert result.exit_code == 2, result.output
    assert 'changed' in result.output
    incus.config_unset_checked.assert_not_called()
