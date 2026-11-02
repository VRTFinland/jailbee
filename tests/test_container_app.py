from concurrent.futures import ThreadPoolExecutor
from threading import Event, Lock
from unittest.mock import Mock, call

import pytest

from jailbee import __version__, container_app
from jailbee.incus import ExecResult


def test_stage_creates_current_and_version(tmp_path):
    result = container_app.stage(root=tmp_path)
    assert result.changed
    assert (tmp_path / "current").readlink() == result.path.relative_to(tmp_path)
    assert (result.path / "VERSION").read_text() == f"{__version__} {container_app.code_hash()}\n"
    assert (result.path / "jailbee" / "__init__.py").is_file()
    assert not list(result.path.rglob("__pycache__"))
    before = result.path.stat().st_mtime_ns
    assert not container_app.stage(root=tmp_path).changed
    assert result.path.stat().st_mtime_ns == before


def test_stage_keeps_two_versions(tmp_path, monkeypatch):
    for value in ("a", "b", "c"):
        monkeypatch.setattr(container_app, "code_hash", lambda value=value: value)
        assert container_app.stage(root=tmp_path).changed
    assert sorted(p.name for p in tmp_path.glob("app-*")) == ["app-b", "app-c"]
    assert (tmp_path / "current").readlink().name == "app-c"


def test_concurrent_staging_publishes_only_complete_version(tmp_path, monkeypatch):
    from pathlib import Path

    monkeypatch.setattr(container_app, "code_hash", lambda: "old")
    old = container_app.stage(root=tmp_path).path
    monkeypatch.setattr(container_app, "code_hash", lambda: "new")
    current = tmp_path / "current"
    first_copy, continue_copy, second_lock = Event(), Event(), Event()
    copytree, flock, replace, unlink = (
        container_app.shutil.copytree,
        container_app.fcntl.flock,
        container_app.os.replace,
        Path.unlink,
    )
    guard = Lock()
    lock_calls = 0
    swaps = []

    def observe_flock(fd, operation):
        nonlocal lock_calls
        with guard:
            lock_calls += 1
            if lock_calls == 2:
                second_lock.set()
        return flock(fd, operation)

    def slow_copy(*args, **kwargs):
        first_copy.set()
        assert continue_copy.wait(10)
        return copytree(*args, **kwargs)

    def observe_replace(source, destination):
        if destination == current:
            assert current.is_symlink() and current.resolve(strict=True) == old
            assert (current / "VERSION").read_text() == f"{__version__} old\n"
            target = (tmp_path / Path(source).readlink()).resolve(strict=True)
            assert (target / "VERSION").read_text() == f"{__version__} new\n"
            replace(source, destination)
            assert current.is_symlink() and current.resolve(strict=True) == target
            swaps.append(target)
        else:
            replace(source, destination)

    def guard_unlink(path, *args, **kwargs):
        assert path != current, "current must not be unlinked before replacement"
        return unlink(path, *args, **kwargs)

    monkeypatch.setattr(container_app.fcntl, "flock", observe_flock)
    monkeypatch.setattr(container_app.shutil, "copytree", slow_copy)
    monkeypatch.setattr(container_app.os, "replace", observe_replace)
    monkeypatch.setattr(Path, "unlink", guard_unlink)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(container_app.stage, root=tmp_path)
        try:
            assert first_copy.wait(10)
            second = pool.submit(container_app.stage, root=tmp_path)
            assert second_lock.wait(10)
            assert current.is_symlink() and current.resolve(strict=True) == old
            assert (current / "VERSION").read_text() == f"{__version__} old\n"
        finally:
            continue_copy.set()
        results = [first.result(timeout=20), second.result(timeout=20)]
    assert sorted(result.changed for result in results) == [False, True]
    assert swaps == [tmp_path / "app-new"]
    assert (current / "jailbee" / "__init__.py").is_file()
    assert not list(tmp_path.glob("*.tmp-*"))


def test_run_in_container_retries_once_on_skew(monkeypatch):
    incus = Mock()
    incus.exec_bytes.side_effect = [ExecResult(86, b"", b"old"), ExecResult(0, b"ok", b"")]
    stage = Mock(return_value=container_app.StageResult(path=Mock(), changed=False))
    monkeypatch.setattr(container_app, "stage", stage)
    result = container_app.run_in_container(Mock(), incus, "one", ["version"], uid=123)
    assert result.stdout == b"ok"
    stage.assert_called_once_with(force=True)
    expected = f"{__version__} {container_app.code_hash()}"
    assert incus.exec_bytes.call_args_list == [
        call("one", ["jailbee", "--expect-version", expected, "version"], uid=123),
        call("one", ["jailbee", "--expect-version", expected, "version"], uid=123),
    ]
    incus.exec_bytes.side_effect = [ExecResult(86, b"", b"old"), ExecResult(86, b"", b"old")]
    with pytest.raises(container_app.ContainerJailbeeError) as exc:
        container_app.run_in_container(Mock(), incus, "one", [], uid=None)
    assert "old" in str(exc.value)
    assert expected in str(exc.value)


@pytest.mark.parametrize("changed", [True, False])
def test_retry_syncs_skills_only_when_changed(monkeypatch, tmp_path, changed):
    cfg = Mock()
    incus = Mock()
    incus.exec_bytes.side_effect = [ExecResult(86, b"", b"old"), ExecResult(0, b"", b"")]
    monkeypatch.setattr(
        container_app, "stage", Mock(return_value=container_app.StageResult(tmp_path, changed))
    )
    sync = Mock()
    monkeypatch.setattr("jailbee.agent_skills.sync_agent_skills", sync)
    container_app.run_in_container(cfg, incus, "one", ["version"], uid=7)
    assert sync.call_args_list == ([call(cfg)] if changed else [])


def test_run_in_container_old_image():
    incus = Mock()
    incus.exec_bytes.return_value = ExecResult(127, b"", b"")
    with pytest.raises(container_app.ContainerJailbeeError, match="jb base build"):
        container_app.run_in_container(Mock(), incus, "one", [], uid=None)


def test_binds_profile_mounts_app_readonly(make_cfg, tmp_path):
    import yaml

    from jailbee.profiles import binds_profile_yaml

    device = yaml.safe_load(binds_profile_yaml(make_cfg(tmp_path)))["devices"]["jailbee-app"]
    assert device["type"] == "disk"
    assert device["source"] == str(container_app.staging_root())
    assert device["path"] == "/opt/jailbee/app"
    assert device["readonly"] == "true"


def test_broker_start_syncs_skills_on_change(monkeypatch, tmp_path):
    from jailbee.orchestration import broker

    cfgs = [Mock(), Mock()]
    sync = Mock()
    monkeypatch.setattr(
        container_app, "stage", Mock(return_value=container_app.StageResult(tmp_path, True))
    )
    monkeypatch.setattr(broker, "repo_configs", Mock(return_value={"a": cfgs[0], "b": cfgs[1]}))
    monkeypatch.setattr("jailbee.agent_skills.sync_agent_skills", sync)
    broker._stage_container_app(broker.BrokerMemory())
    assert sync.call_args_list == [call(cfgs[0]), call(cfgs[1])]
    sync.reset_mock()
    monkeypatch.setattr(
        container_app, "stage", Mock(return_value=container_app.StageResult(tmp_path, False))
    )
    broker._stage_container_app(broker.BrokerMemory())
    sync.assert_not_called()
