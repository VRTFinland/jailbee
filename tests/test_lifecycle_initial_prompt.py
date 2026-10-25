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


def _drive_new(make_cfg, tmp_path, mocker, opts, *, real_write=False):
    repo = tmp_path / "myrepo"
    repo.mkdir()
    cfg = make_cfg(repo, default_branch="main")
    incus = MagicMock()
    incus.exists.return_value = False
    _patch_new_container_deps(mocker)
    events: list[str] = []
    incus.start.side_effect = lambda _n: events.append("start")
    if real_write:
        write = None
    else:
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


def _flatten(value):
    if isinstance(value, (list, tuple)):
        for v in value:
            yield from _flatten(v)
    elif isinstance(value, dict):
        for v in value.values():
            yield from _flatten(v)
    else:
        yield str(value)


def test_prompt_text_reaches_the_container_only_as_stdin(make_cfg, tmp_path, mocker):
    from jailbee.autostart import agent_autostart_steps
    from jailbee.initial_prompt import PROMPT_PATH

    text = 'say "hi" $(rm -rf /) `x`\nline2'
    _cfg, incus, _, _ = _drive_new(
        make_cfg,
        tmp_path,
        mocker,
        _opts(autostart=False, initial_prompt=text),
        real_write=True,
    )

    # The prompt did go through exec_with_input's stdin argument ...
    stdin_calls = [c for c in incus.exec_with_input.call_args_list if c.args[2] == text]
    assert len(stdin_calls) == 1
    assert stdin_calls[0].args[1][-1] == PROMPT_PATH

    # ... and appears in no other argument of any incus call.
    leaks = []
    for call in incus.mock_calls:
        _, args, kwargs = call
        if call[0] == "exec_with_input":
            args = (args[0], args[1], *args[3:])
        for piece in _flatten([args, kwargs]):
            if "rm -rf" in piece or "line2" in piece or text in piece:
                leaks.append((call[0], piece))
    assert leaks == []

    # The launch step references the file, never the text.
    [step] = agent_autostart_steps(
        make_cfg(tmp_path / "myrepo", agents={"claude": {"enabled": True, "autostart": True}}),
        initial_prompt=True,
    )
    for piece in [step.run, *step.env.values()]:
        assert "rm -rf" not in piece and "line2" not in piece and "$(rm" not in piece


def test_boot_container_rejects_unusable_prompt_before_touching_anything(tmp_path, mocker):
    import pytest

    cfg = _cfg_for_new(tmp_path)
    incus = MagicMock()
    incus.list_containers.return_value = [{"name": "feat-x", "status": "Stopped"}]
    _boot_events(mocker, incus)
    write = mocker.patch("jailbee.initial_prompt.write_prompt")

    with pytest.raises(ValueError, match="empty"):
        boot_container(cfg, incus, "feat-x", restart=False, initial_prompt="  \n")
    mocker.patch(
        "jailbee.initial_prompt.prompt_problem", return_value="agent `aider` has no prompt_args"
    )
    with pytest.raises(ValueError, match="aider"):
        boot_container(cfg, incus, "feat-x", restart=False, initial_prompt="go")

    incus.start.assert_not_called()
    write.assert_not_called()
    incus.config_set.assert_not_called()


def test_boot_container_checks_prompt_against_the_runner(tmp_path, mocker):
    from jailbee.orchestration import labels
    from jailbee.orchestration.runner import Runner

    cfg = _cfg_for_new(tmp_path)
    incus = MagicMock()
    incus.list_containers.return_value = [{"name": "feat-x", "status": "Stopped"}]
    incus.config_get.side_effect = lambda _n, key: (
        Runner(agent="claude").to_label() if key == labels.RUNNER else None
    )
    _boot_events(mocker, incus)
    mocker.patch("jailbee.initial_prompt.write_prompt")
    check = mocker.patch("jailbee.initial_prompt.prompt_problem", return_value=None)

    boot_container(cfg, incus, "feat-x", restart=False, initial_prompt="go")

    check.assert_called_once_with(cfg, Runner(agent="claude"))
