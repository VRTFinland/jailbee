"""`new_container` / `boot_container` hand the initial prompt to the container."""

from __future__ import annotations

from unittest.mock import MagicMock

from jailbee.lifecycle import NewContainerOptions, boot_container, new_container
from tests.test_lifecycle import _boot_events, _cfg_for_new, _patch_new_container_deps


def _opts(**kw):
    return NewContainerOptions(
        container_branch="feat/foo",
        name=None,
        network="strict",
        memory="4GB",
        cpu=4,
        from_base="gie-golden",
        clone=True,
        **kw,
    )


def _drive_new(make_cfg, tmp_path, mocker, opts):
    repo = tmp_path / "myrepo"
    repo.mkdir()
    cfg = make_cfg(repo, default_branch="main")
    incus = MagicMock()
    incus.exists.return_value = False
    _patch_new_container_deps(mocker)
    events: list[str] = []
    incus.start.side_effect = lambda _n: events.append("start")
    write = mocker.patch(
        "jailbee.initial_prompt.write_prompt",
        side_effect=lambda *a, **kw: events.append("write"),
    )
    mocker.patch(
        "jailbee.autostart.run_autostart",
        side_effect=lambda *a, **kw: events.append("autostart") or MagicMock(detached=[]),
    )
    new_container(cfg, incus, opts)
    return cfg, incus, write, events


def test_new_container_writes_prompt_before_autostart(make_cfg, tmp_path, mocker):
    cfg, incus, write, events = _drive_new(
        make_cfg, tmp_path, mocker, _opts(autostart=True, initial_prompt="do it\n")
    )

    write.assert_called_once_with(
        incus,
        write.call_args.args[1],
        "do it\n",
        uid=cfg.container_user.uid,
        gid=cfg.container_user.gid,
    )
    assert "autostart" in events
    assert events.index("start") < events.index("write") < events.index("autostart")


def test_new_container_writes_prompt_even_without_autostart(make_cfg, tmp_path, mocker):
    _, _, write, events = _drive_new(
        make_cfg, tmp_path, mocker, _opts(autostart=False, initial_prompt="x")
    )

    write.assert_called_once()
    assert "autostart" not in events


def test_new_container_without_prompt_never_writes(make_cfg, tmp_path, mocker):
    _, _, write, _ = _drive_new(make_cfg, tmp_path, mocker, _opts(autostart=False))

    write.assert_not_called()


def test_boot_container_writes_prompt_after_start(tmp_path, mocker):
    cfg = _cfg_for_new(tmp_path)
    incus = MagicMock()
    incus.list_containers.return_value = [{"name": "feat-x", "status": "Stopped"}]
    events = _boot_events(mocker, incus)
    write = mocker.patch(
        "jailbee.initial_prompt.write_prompt",
        side_effect=lambda *a, **kw: events.append("write"),
    )

    boot_container(cfg, incus, "feat-x", restart=False, initial_prompt="go")

    write.assert_called_once_with(
        incus, "feat-x", "go", uid=cfg.container_user.uid, gid=cfg.container_user.gid
    )
    assert events.index("start") < events.index("write")


def test_boot_container_without_prompt_never_writes(tmp_path, mocker):
    cfg = _cfg_for_new(tmp_path)
    incus = MagicMock()
    incus.list_containers.return_value = [{"name": "feat-x", "status": "Stopped"}]
    _boot_events(mocker, incus)
    write = mocker.patch("jailbee.initial_prompt.write_prompt")

    boot_container(cfg, incus, "feat-x", restart=False)

    write.assert_not_called()
