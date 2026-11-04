"""The container sees host PR state, including stopped bind/unbind changes."""

import json
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


def test_failed_refresh_invalidates_old_binding(tmp_path, monkeypatch):
    incus, _ = rig(tmp_path, monkeypatch)
    container_bindings.sync(incus, "proj-c")
    incus.config_get_checked.side_effect = IncusError("unavailable")
    with pytest.raises(IncusError):
        container_bindings.sync(incus, "proj-c")
    assert "error" in json.loads((tmp_path / "proj-c/bindings.json").read_text())
