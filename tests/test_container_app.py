from unittest.mock import Mock

import pytest

from jailbee import __version__, container_app
from jailbee.incus import ExecResult


def test_stage_creates_current_and_version(tmp_path):
    result = container_app.stage(root=tmp_path)
    assert result.changed
    assert (tmp_path / 'current').readlink() == result.path.relative_to(tmp_path)
    assert (result.path / 'VERSION').read_text() == f'{__version__} {container_app.code_hash()}\n'
    assert (result.path / 'jailbee' / '__init__.py').is_file()
    assert not list(result.path.rglob('__pycache__'))
    before = result.path.stat().st_mtime_ns
    assert not container_app.stage(root=tmp_path).changed
    assert result.path.stat().st_mtime_ns == before


def test_stage_keeps_two_versions(tmp_path, monkeypatch):
    for value in ('a', 'b', 'c'):
        monkeypatch.setattr(container_app, 'code_hash', lambda: value)
        assert container_app.stage(root=tmp_path).changed
    assert sorted(p.name for p in tmp_path.glob('app-*')) == ['app-b', 'app-c']
    assert (tmp_path / 'current').readlink().name == 'app-c'


def test_run_in_container_retries_once_on_skew(monkeypatch):
    incus = Mock()
    incus.exec_bytes.side_effect = [ExecResult(86, b'', b'old'), ExecResult(0, b'ok', b'')]
    stage = Mock()
    monkeypatch.setattr(container_app, 'stage', stage)
    result = container_app.run_in_container(Mock(), incus, 'one', ['version'], uid=123)
    assert result.stdout == b'ok'
    stage.assert_called_once_with(force=True)
    assert incus.exec_bytes.call_count == 2
    incus.exec_bytes.side_effect = [ExecResult(86, b'', b'old'), ExecResult(86, b'', b'old')]
    with pytest.raises(container_app.ContainerJailbeeError, match='old'):
        container_app.run_in_container(Mock(), incus, 'one', [], uid=None)


def test_run_in_container_old_image():
    incus = Mock()
    incus.exec_bytes.return_value = ExecResult(127, b'', b'')
    with pytest.raises(container_app.ContainerJailbeeError, match='jb base build'):
        container_app.run_in_container(Mock(), incus, 'one', [], uid=None)


def test_binds_profile_mounts_app_readonly(make_cfg, tmp_path):
    import yaml
    from jailbee.profiles import binds_profile_yaml
    device = yaml.safe_load(binds_profile_yaml(make_cfg(tmp_path)))['devices']['jailbee-app']
    assert device['path'] == '/opt/jailbee/app'
    assert device['readonly'] == 'true'


def test_broker_start_syncs_skills_on_change(monkeypatch, tmp_path):
    from jailbee.orchestration import broker
    cfgs = [Mock(), Mock()]
    sync = Mock()
    monkeypatch.setattr(container_app, 'stage', Mock(return_value=container_app.StageResult(tmp_path, True)))
    monkeypatch.setattr(broker, 'repo_configs', Mock(return_value={'a': cfgs[0], 'b': cfgs[1]}))
    monkeypatch.setattr('jailbee.agent_skills.sync_agent_skills', sync)
    broker._stage_container_app(broker.BrokerMemory())
    assert sync.call_count == 2
    sync.reset_mock()
    monkeypatch.setattr(container_app, 'stage', Mock(return_value=container_app.StageResult(tmp_path, False)))
    broker._stage_container_app(broker.BrokerMemory())
    sync.assert_not_called()
