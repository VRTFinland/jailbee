from jailbee import agent_private
from jailbee.incus import IncusError
from tests.conftest import make_cfg, with_agent


def _cfg_with_private(tmp_path):
    return with_agent(
        make_cfg(tmp_path),
        "codex",
        command="codex",
        enabled=True,
        shared=[
            {
                "subpath": "codex",
                "path": "~/.codex",
                "private": ["app-server-control", "app-server-daemon"],
            }
        ],
    )


def test_attach_creates_the_host_dir_and_adds_the_device(tmp_path, mocker):
    cfg = _cfg_with_private(tmp_path)
    incus = mocker.MagicMock()

    agent_private.attach(cfg, incus, "jb-repo-main")

    assert cfg.shared_dir is not None
    source = cfg.shared_dir / ".private" / "jb-repo-main" / "codex" / "app-server-control"
    assert source.is_dir()
    incus.config_device_add.assert_any_call(
        "jb-repo-main",
        "private-codex-app-server-control",
        "disk",
        {"source": str(source), "path": "/home/dev/.codex/app-server-control"},
    )


def test_attach_keeps_the_private_root_out_of_the_shared_mount(tmp_path, mocker):
    """`<shared_dir>/.private/`, not `<shared_dir>/codex/.private/`: the agent's
    own directory is bind-mounted into every container, so a per-container tree
    inside it would be listable from every other container."""
    cfg = _cfg_with_private(tmp_path)
    agent_private.attach(cfg, mocker.MagicMock(), "jb-repo-main")

    assert cfg.shared_dir is not None
    assert not (cfg.shared_dir / "codex" / ".private").exists()
    assert (cfg.shared_dir / ".private" / "jb-repo-main").is_dir()


def test_attach_removes_a_stale_device_first(tmp_path, mocker):
    """Remove-then-add is what makes the nested mount land on top of the shared
    one: hot-plugging into an already-mounted parent has no ordering ambiguity,
    while a device left from the previous boot may have been mounted before its
    parent."""
    cfg = _cfg_with_private(tmp_path)
    incus = mocker.MagicMock()
    calls: list[tuple[str, str]] = []
    incus.config_device_remove.side_effect = lambda *a, **k: calls.append(("rm", a[1]))
    incus.config_device_add.side_effect = lambda *a, **k: calls.append(("add", a[1]))

    agent_private.attach(cfg, incus, "jb-repo-main")

    assert calls[:2] == [
        ("rm", "private-codex-app-server-control"),
        ("add", "private-codex-app-server-control"),
    ]
    assert incus.config_device_remove.call_args_list[0].kwargs == {"missing_ok": True}


def test_attach_is_a_no_op_without_private_subpaths(tmp_path, mocker):
    cfg = make_cfg(tmp_path, agents={"codex": {"enabled": False}})
    incus = mocker.MagicMock()

    agent_private.attach(cfg, incus, "jb-repo-main")

    incus.config_device_add.assert_not_called()
    assert not agent_private.private_root(cfg, "jb-repo-main").exists()


def test_release_removes_devices_and_the_host_tree(tmp_path, mocker):
    cfg = _cfg_with_private(tmp_path)
    incus = mocker.MagicMock()
    agent_private.attach(cfg, incus, "jb-repo-main")
    root = agent_private.private_root(cfg, "jb-repo-main")
    assert root.is_dir()

    agent_private.release(cfg, incus, "jb-repo-main")

    assert not root.exists()
    incus.config_device_remove.assert_any_call(
        "jb-repo-main", "private-codex-app-server-daemon", missing_ok=True
    )


def test_release_survives_an_incus_failure(tmp_path, mocker):
    """A container the user asked to destroy must still be destroyable."""
    cfg = _cfg_with_private(tmp_path)
    incus = mocker.MagicMock()
    agent_private.attach(cfg, incus, "jb-repo-main")
    incus.config_device_remove.side_effect = IncusError("boom")

    agent_private.release(cfg, incus, "jb-repo-main")

    assert not agent_private.private_root(cfg, "jb-repo-main").exists()


def test_release_only_touches_the_named_container(tmp_path, mocker):
    cfg = _cfg_with_private(tmp_path)
    incus = mocker.MagicMock()
    agent_private.attach(cfg, incus, "jb-repo-main")
    agent_private.attach(cfg, incus, "jb-repo-other")

    agent_private.release(cfg, incus, "jb-repo-main")

    assert not agent_private.private_root(cfg, "jb-repo-main").exists()
    assert agent_private.private_root(cfg, "jb-repo-other").is_dir()
