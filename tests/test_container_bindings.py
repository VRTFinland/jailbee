"""The container sees host PR state, including stopped bind/unbind changes."""

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from jailbee import container_bindings
from jailbee.incus import IncusError


def rig(tmp_path, monkeypatch):
    monkeypatch.setattr(container_bindings, "metadata_root", lambda: tmp_path)
    config = {
        "user.jailbee.repo_dir": "/repo",
        "user.jailbee.pr": "12",
        "user.jailbee.stacked_pr": "13",
        "user.jailbee.sub_pr": '{"lib":{"pr":14}}',
        "secret": "never expose",
    }
    incus = MagicMock()
    incus.config_get_checked.side_effect = lambda name, key: config.get(key)
    incus.config_device_get.return_value = None
    return incus, config


@pytest.mark.parametrize("with_config", [False, True])
def test_fresh_home_guard_allows_user_role_and_outbox_bootstrap(
    tmp_path, monkeypatch, make_cfg, with_config
):
    import os

    from jailbee.orchestration import labels
    from jailbee.outbox.io import ensure_directories

    incus, _ = rig(tmp_path, monkeypatch)
    cfg = make_cfg(tmp_path / "repo")
    home = tmp_path / "fresh-home"
    home.mkdir(mode=0o700)
    incus.list_containers.return_value = [{"name": "proj-c", "status": "Running"}]
    home_exec_uids = []
    neutral = tmp_path / "run"
    guard = neutral / "jailbee/pr-bindings-error"

    def execute(name, argv, *, uid=None, **kwargs):
        if argv[:2] == ["id", "-u"]:
            return str(os.getuid())
        if argv[:2] == ["id", "-g"]:
            return str(os.getgid())
        if argv[-1] == "/run/jailbee/pr-bindings-error":
            assert uid == 0
        else:
            home_exec_uids.append(uid)
        # Execute the production payload with a restrictive umask against a
        # fresh home and neutral run directory, without a real Incus daemon.
        mapped = [arg.replace("/home/dev", str(home)).replace("/run/jailbee", str(neutral / "jailbee")) for arg in argv]
        pid = os.fork()
        if pid == 0:
            try:
                os.umask(0o077)
                os.execvp(mapped[0], mapped)
            finally:
                os._exit(127)
        _, status = os.waitpid(pid, 0)
        assert os.waitstatus_to_exitcode(status) == 0
        if guard.exists():
            assert guard.stat().st_mode & 0o777 == 0o644
            assert guard.parent.stat().st_mode & 0o777 == 0o755
            assert not (home / ".jailbee").exists()
        return ""

    incus.exec.side_effect = execute
    container_bindings.sync(incus, "proj-c", cfg=cfg if with_config else None)
    assert not (home / ".jailbee").exists()
    assert not guard.exists()
    assert (neutral / "jailbee").stat().st_mode & 0o777 == 0o755
    labels.write_child_role(incus, "proj-c", uid=cfg.container_user.uid)
    ensure_directories(cfg, incus, "proj-c")
    assert (home / ".jailbee/orchestration/role").read_text().strip() == "child"
    for relative in (".jailbee", ".jailbee/pr-outbox", ".jailbee/issue-outbox"):
        path = home / relative
        assert path.is_dir()
        assert path.stat().st_uid == os.getuid()
        assert os.access(path, os.W_OK | os.X_OK)
    assert home_exec_uids and all(uid == cfg.container_user.uid for uid in home_exec_uids)


@pytest.mark.parametrize("answer", ["", "not-a-uid", "0", "-1"])
def test_unknown_uid_and_old_mount_cannot_select_stale_binding(tmp_path, monkeypatch, answer):
    from jailbee.container_outbox import bindings
    from jailbee.container_outbox.store import OutboxCommandError

    incus, config = rig(tmp_path, monkeypatch)
    old = tmp_path / "old-visible.json"
    old.write_text('{"user.jailbee.repo_dir":"/repo","user.jailbee.pr":"42"}')
    marker = tmp_path / "run/jailbee/pr-bindings-error"
    monkeypatch.setattr(bindings, "SNAPSHOT", old)
    monkeypatch.setattr(bindings, "ERROR_MARKER", marker)
    incus.list_containers.return_value = [{"name": "proj-c", "status": "Running"}]
    incus.config_device_get.side_effect = lambda name, dev, key: {
        "type": "disk", "source": str(old.parent),
    }.get(key)
    incus.config_device_set.side_effect = IncusError("device repair failed")
    config["user.jailbee.pr"] = "43"

    def execute(name, argv, *, uid=None, **kwargs):
        if argv[0] == "id":
            return answer
        assert uid == 0
        assert argv[-1] == "/run/jailbee/pr-bindings-error"
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.touch(mode=0o644)
        return ""

    incus.exec.side_effect = execute
    assert not container_bindings.refresh(incus, "proj-c")
    assert json.loads(old.read_text())["user.jailbee.pr"] == "42"
    with pytest.raises(OutboxCommandError, match="restart with jb start"):
        bindings.resolve(42)
    assert "error" in json.loads((tmp_path / "proj-c/bindings.json").read_text())


def test_snapshot_exports_only_binding_fields_and_mounts_readonly(tmp_path, monkeypatch):
    incus, config = rig(tmp_path, monkeypatch)
    container_bindings.sync(incus, "proj-c")
    assert json.loads((tmp_path / "proj-c/bindings.json").read_text()) == {
        k: v for k, v in config.items() if k != "secret"
    }
    device = incus.config_device_add.call_args.args
    assert device[:3] == ("proj-c", "jailbee-metadata", "disk")
    assert device[3] == {
        "source": str(tmp_path / "proj-c"),
        "path": "/opt/jailbee/metadata",
        "readonly": "true",
    }
    incus.exec.assert_not_called()


def test_unbinding_replaces_snapshot_even_when_container_stopped(tmp_path, monkeypatch):
    incus, config = rig(tmp_path, monkeypatch)
    container_bindings.sync(incus, "proj-c")
    del config["user.jailbee.pr"]
    container_bindings.sync(incus, "proj-c")
    assert "user.jailbee.pr" not in json.loads((tmp_path / "proj-c/bindings.json").read_text())
    incus.exec.assert_not_called()


@pytest.mark.parametrize("kind", ["primary", "stacked", "submodule"])
def test_real_state_writes_and_clears_refresh_mounted_snapshot(tmp_path, monkeypatch, kind):
    from jailbee.pr_flow import STACKED_LABEL_PREFIX, ContainerLabelState
    from jailbee.submodule_pr import SubmodulePrState

    incus, config = rig(tmp_path, monkeypatch)
    incus.config_get.side_effect = incus.config_get_checked.side_effect
    incus.config_set.side_effect = lambda name, key, value: config.__setitem__(key, value)
    incus.config_unset_checked.side_effect = lambda name, key: config.pop(key, None)
    if kind == "submodule":
        state = SubmodulePrState(incus, "proj-c", "lib")
        key = "user.jailbee.sub_pr"
    else:
        key = STACKED_LABEL_PREFIX if kind == "stacked" else "user.jailbee.pr"
        state = ContainerLabelState(incus, "proj-c", prefix=key)
    assert state.record(head="new-head", author=True, adopted=False, number=99)
    written = json.loads((tmp_path / "proj-c/bindings.json").read_text())
    assert (json.loads(written[key])["lib"]["pr"] if kind == "submodule" else written[key]) == (
        99 if kind == "submodule" else "99"
    )
    assert state.clear()
    written = json.loads((tmp_path / "proj-c/bindings.json").read_text())
    assert key not in written


def test_branch_binding_put_and_drop_refresh_snapshot(tmp_path, monkeypatch):
    from jailbee.branch_pr import BranchPr, BranchPrMap

    incus, config = rig(tmp_path, monkeypatch)
    state = BranchPrMap(incus, "proj-c")
    incus.config_set.side_effect = lambda name, key, value: config.__setitem__(key, value)
    incus.config_unset_checked.side_effect = lambda name, key: config.pop(key, None)
    state.put(BranchPr("work", 77, "main", "refs/heads/work", "a" * 40))
    written = json.loads((tmp_path / "proj-c/bindings.json").read_text())
    assert json.loads(written["user.jailbee.branch_prs"])["work"]["pr"] == 77
    assert state.drop("work")
    assert "user.jailbee.branch_prs" not in json.loads(
        (tmp_path / "proj-c/bindings.json").read_text()
    )


def test_blocked_old_refresh_cannot_overwrite_new_unbind(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event, current_thread

    incus, config = rig(tmp_path, monkeypatch)
    old_blocked, resume_old, new_started = Event(), Event(), Event()
    real_write = container_bindings._write

    def write(directory, value):
        if current_thread().name.startswith("old"):
            old_blocked.set()
            assert resume_old.wait(5)
        real_write(directory, value)

    monkeypatch.setattr(container_bindings, "_write", write)
    with ThreadPoolExecutor(1, thread_name_prefix="old") as old_pool:
        with ThreadPoolExecutor(1, thread_name_prefix="new") as new_pool:
            old = old_pool.submit(container_bindings.sync, incus, "proj-c")
            assert old_blocked.wait(5)
            del config["user.jailbee.pr"]

            def unbind_refresh():
                new_started.set()
                container_bindings.sync(incus, "proj-c")

            new = new_pool.submit(unbind_refresh)
            assert new_started.wait(5)
            # Exercise the real production inode, never replace the lock with a fake.
            import fcntl

            from jailbee.paths import xdg_data_home

            path = xdg_data_home() / "jailbee/container-locks/proj-c.lock"
            path.parent.mkdir(parents=True, exist_ok=True)
            try:
                with path.open("a") as probe:
                    try:
                        fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    except BlockingIOError:
                        pass
                    else:
                        fcntl.flock(probe, fcntl.LOCK_UN)
                        new.result(timeout=5)
            finally:
                resume_old.set()
            old.result(timeout=5)
            new.result(timeout=5)
    assert "user.jailbee.pr" not in json.loads((tmp_path / "proj-c/bindings.json").read_text())


@pytest.mark.parametrize("registered", [False, True])
def test_legacy_repo_dir_refresh_resolves_primary_and_submodule(
    tmp_path, monkeypatch, make_cfg, registered
):
    from datetime import UTC, datetime

    from sqlmodel import Session

    from jailbee.container_outbox import bindings
    from jailbee.db import get_engine
    from jailbee.db.models import RegisteredRepo

    incus, config = rig(tmp_path, monkeypatch)
    del config["user.jailbee.repo_dir"]
    config["user.jailbee.pr"] = "42"
    config.pop("user.jailbee.stacked_pr")
    config.pop("user.jailbee.sub_pr")
    cfg = make_cfg(tmp_path / "host-only/legacy-repo")
    if registered:
        with Session(get_engine()) as session:
            session.add(
                RegisteredRepo(
                    container_prefix="proj",
                    repo_root=str(cfg.repo_root),
                    registered_at=datetime.now(UTC),
                )
            )
            session.commit()
        assert container_bindings.refresh(incus, "proj-c")
    else:
        container_bindings.sync(incus, "proj-c", cfg=cfg)
    monkeypatch.setattr(bindings, "SNAPSHOT", tmp_path / "proj-c/bindings.json")
    assert bindings.resolve(42) == bindings.Binding(42, "/home/dev/legacy-repo")
    assert bindings.resolve(None) == bindings.Binding(42, "/home/dev/legacy-repo")
    config.pop("user.jailbee.pr")
    config["user.jailbee.sub_pr"] = '{"lib/nested":{"pr":43}}'
    if registered:
        assert container_bindings.refresh(incus, "proj-c")
    else:
        container_bindings.sync(incus, "proj-c", cfg=cfg)
    assert bindings.resolve(None) == bindings.Binding(43, "/home/dev/legacy-repo/lib/nested")


@pytest.mark.parametrize("failure", [False, True])
def test_wrong_metadata_source_repairs_visible_binding_or_fails_closed(
    tmp_path, monkeypatch, failure
):
    from jailbee.container_outbox import bindings
    from jailbee.container_outbox.store import OutboxCommandError

    incus, config = rig(tmp_path, monkeypatch)
    config["user.jailbee.pr"] = "43"
    config.pop("user.jailbee.stacked_pr")
    config.pop("user.jailbee.sub_pr")
    old = tmp_path / "old-source"
    old.mkdir()
    (old / "bindings.json").write_text('{"user.jailbee.repo_dir":"/repo","user.jailbee.pr":"42"}')
    device = {"type": "disk", "source": str(old), "path": "/wrong", "readonly": "false"}
    incus.config_device_get.side_effect = lambda name, dev, key: device.get(key)
    incus.config_device_add.side_effect = IncusError("already exists")

    def update(name, dev, properties):
        if failure:
            raise IncusError("device update failed")
        device.update(properties)

    incus.config_device_set.side_effect = update

    marker = tmp_path / "container-error"
    monkeypatch.setattr(bindings, "ERROR_MARKER", marker)
    incus.list_containers.return_value = [{"name": "proj-c", "status": "Running"}]

    def execute(name, argv, **kwargs):
        if argv[:2] == ["id", "-u"]:
            return "1000"
        assert kwargs["uid"] == 0
        assert argv[-1] == "/run/jailbee/pr-bindings-error"
        if argv[0] == "rm":
            marker.unlink(missing_ok=True)
        else:
            marker.touch()

    incus.exec.side_effect = execute
    monkeypatch.setattr(bindings, "SNAPSHOT", old / "bindings.json")
    if failure:
        assert not container_bindings.refresh(incus, "proj-c")
        with pytest.raises(OutboxCommandError, match="restart with jb start"):
            bindings.resolve(42)
        # A subsequent refresh must recover rather than retain the error snapshot.
        failure = False
    assert container_bindings.refresh(incus, "proj-c")
    monkeypatch.setattr(bindings, "SNAPSHOT", Path(device["source"]) / "bindings.json")
    assert bindings.resolve(None) == bindings.Binding(43, "/repo")
    assert device["path"] == "/opt/jailbee/metadata"
    assert device["readonly"] == "true"


def test_restart_clears_failed_mount_guard_after_stopped_repair(tmp_path, monkeypatch):
    from jailbee.container_outbox import bindings

    incus, _ = rig(tmp_path, monkeypatch)
    guard = tmp_path / "guard"
    guard.touch()
    monkeypatch.setattr(bindings, "ERROR_MARKER", guard)
    monkeypatch.setattr(bindings, "SNAPSHOT", tmp_path / "proj-c/bindings.json")
    incus.list_containers.return_value = [{"name": "proj-c", "status": "Stopped"}]
    container_bindings.sync(incus, "proj-c")
    incus.exec.side_effect = lambda name, argv, **kwargs: (
        "1000" if argv[0] == "id" else guard.unlink(missing_ok=True)
    )
    # Boot must clear the guard after the repaired device has actually mounted.
    incus.list_containers.return_value = [{"name": "proj-c", "status": "Running"}]
    container_bindings.sync(incus, "proj-c")
    assert not guard.exists()
    assert bindings.resolve(12) == bindings.Binding(12, "/repo")


def test_failed_refresh_invalidates_old_binding(tmp_path, monkeypatch):
    incus, _ = rig(tmp_path, monkeypatch)
    container_bindings.sync(incus, "proj-c")
    incus.config_get_checked.side_effect = IncusError("unavailable")
    with pytest.raises(IncusError):
        container_bindings.sync(incus, "proj-c")
    assert "error" in json.loads((tmp_path / "proj-c/bindings.json").read_text())
