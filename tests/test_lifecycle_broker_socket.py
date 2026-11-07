"""A new container carries its broker socket device before any agent starts."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from jailbee.incus import ORCHESTRATION_DEVICE, IncusError
from jailbee.lifecycle import NewContainerOptions, new_container
from jailbee.orchestration import socket_device

UUID = "0123-uuid"


def _rig(tmp_path: Path, mocker, make_cfg, events: list[str], *, uuid: str | None = UUID):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    cfg = make_cfg(repo, shared_dir=tmp_path / "shared", new={"clone_from": "local"})
    incus = MagicMock()
    incus.exists.return_value = False
    incus.exec.return_value = ""
    incus.config_get.side_effect = lambda _n, key: uuid if key == "volatile.uuid" else None
    incus.config_device_add.side_effect = lambda _n, device, *a, **k: (
        events.append("socket") if device == ORCHESTRATION_DEVICE else None
    )
    mocker.patch("jailbee.lifecycle.branch_exists_locally", return_value=True)
    mocker.patch("jailbee.pool.allocate_startup")
    mocker.patch("jailbee.agent_private.attach")
    mocker.patch("jailbee.outbox.io.ensure_directories", create=True)
    mocker.patch(
        "jailbee.agents.ensure_agents", side_effect=lambda *a, **k: events.append("agents")
    )
    mocker.patch("jailbee.orchestration.state.state_root", return_value=tmp_path / "orch")
    return cfg, incus


def _create(cfg, incus) -> None:
    new_container(cfg, incus, NewContainerOptions("feat/x", None, "strict", "8GiB", 4, "base", True))


def test_socket_device_is_attached_before_the_first_agent(tmp_path, mocker, make_cfg):
    events: list[str] = []
    cfg, incus = _rig(tmp_path, mocker, make_cfg, events)
    _create(cfg, incus)
    assert events == ["socket", "agents"]
    name, device, kind, props = incus.config_device_add.call_args_list[-1].args
    assert device == ORCHESTRATION_DEVICE and kind == "disk"
    source = tmp_path / "orch" / "sock"
    assert props["source"].startswith(str(source / UUID))
    assert socket_device.dir_uuid(props["source"]) == UUID
    assert props["path"] == socket_device.CONTAINER_PATH

    assert Path(props["source"]).is_dir()


def test_a_failed_attach_warns_and_leaves_the_repair_to_the_broker(
    tmp_path, mocker, make_cfg, capsys
):
    events: list[str] = []
    cfg, incus = _rig(tmp_path, mocker, make_cfg, events)

    def busy(_n, device, *a, **k):
        if device == ORCHESTRATION_DEVICE:
            raise IncusError("busy")

    incus.config_device_add.side_effect = busy
    _create(cfg, incus)
    assert events == ["agents"]
    assert "broker socket was not attached" in capsys.readouterr().out
    assert list((tmp_path / "orch" / "sock").iterdir()) == []


def test_no_uuid_yet_attaches_nothing(tmp_path, mocker, make_cfg):
    events: list[str] = []
    cfg, incus = _rig(tmp_path, mocker, make_cfg, events, uuid=None)
    _create(cfg, incus)
    assert events == ["agents"]


def test_attach_new_is_reusable_by_the_broker(tmp_path):
    incus = MagicMock()
    incus.config_get.return_value = UUID
    path = socket_device.attach_new(incus, "proj-x", tmp_path)
    assert path is not None
    assert socket_device.reusable_dir(tmp_path, UUID, str(path)) == path


@pytest.mark.parametrize("value", [None, "", 5])
def test_attach_new_needs_a_string_uuid(tmp_path, value):
    incus = MagicMock()
    incus.config_get.return_value = value
    assert socket_device.attach_new(incus, "proj-x", tmp_path) is None
    incus.config_device_add.assert_not_called()
