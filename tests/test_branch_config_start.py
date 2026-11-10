"""Container HEAD is the source of branch autostart on restart."""

import pytest

from jailbee import branch_config
from jailbee.incus import IncusError
from tests.conftest import make_cfg


def test_clone_loads_head_autostart_and_services(mocker, tmp_path):
    cfg = make_cfg(tmp_path)
    cfg = cfg.model_copy(
        update={"container_user": cfg.container_user.model_copy(update={"uid": 2345, "gid": 3456})}
    )
    incus = mocker.Mock()
    incus.config_get.side_effect = lambda name, key: {
        "user.jailbee.mode": "clone",
        "user.jailbee.repo_dir": "/repo",
    }[key]
    checkout = tmp_path / ".jailbee"
    checkout.mkdir(exist_ok=True)
    (checkout / "config.yaml").write_text(
        "container_prefix: test\nautostart:\n  on_start:\n    - name: uncommitted\n"
        "      run: false\n"
    )
    incus.exec.return_value = (
        "container_prefix: test\nautostart:\n  on_start:\n    - name: build\n"
        "      run: make\nservices:\n  web:\n    run: serve\n"
    )

    result = branch_config.effective_autostart_config(cfg, incus, "box")

    assert result.autostart.on_start[0].name == "build"
    assert result.services["web"].run == "serve"
    assert cfg.services == {}
    incus.exec.assert_called_once_with(
        "box",
        ["git", "-C", "/repo", "show", "HEAD:.jailbee/config.yaml"],
        uid=2345,
        gid=3456,
        env={"HOME": "/home/dev"},
    )


def test_mount_uses_host_without_exec(mocker, tmp_path):
    cfg = make_cfg(tmp_path)
    incus = mocker.Mock()
    incus.config_get.return_value = "mount"
    assert branch_config.effective_autostart_config(cfg, incus, "box") is cfg
    incus.exec.assert_not_called()


def test_legacy_path_and_missing_config(mocker, tmp_path, capsys):
    cfg = make_cfg(tmp_path)
    incus = mocker.Mock()
    incus.config_get.return_value = "clone"
    incus.exec.side_effect = [
        IncusError("missing"),
        "container_prefix: test\nautostart:\n  on_start:\n    - name: old\n      run: make\n",
    ]
    result = branch_config.effective_autostart_config(cfg, incus, "box")
    assert result.autostart.on_start[0].name == "old"
    assert incus.exec.call_args.args[1][-1] == "HEAD:.gie/config.yaml"
    incus.exec.side_effect = IncusError("missing")
    assert branch_config.effective_autostart_config(cfg, incus, "box") is cfg
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize(
    "text",
    [
        "container_prefix: test\nautostart: [broken]",
        "container_prefix: test\noptional_mounts:\n  unknown:\n"
        "    host: /tmp\n    container: /creds\nautostart:\n  on_start:\n"
        "    - name: build\n      run: make\n      mounts: [unknown]\n",
    ],
)
def test_invalid_or_incompatible_falls_back(mocker, tmp_path, capsys, text):
    cfg = make_cfg(tmp_path)
    incus = mocker.Mock()
    incus.config_get.return_value = "clone"
    incus.exec.return_value = text
    assert branch_config.effective_autostart_config(cfg, incus, "box") is cfg
    assert "Falling back" in capsys.readouterr().out


@pytest.mark.parametrize("grant", ["network: loose", "mounts: [credentials]"])
@pytest.mark.parametrize(
    "interactive,allow,yes,answer,remote,accepted",
    [
        (True, True, False, True, False, True),
        (True, True, False, False, False, False),
        (False, True, False, True, False, False),
        (True, False, False, True, False, False),
        (False, False, True, False, False, True),
        (True, True, True, True, True, False),
    ],
)
def test_widening_requires_local_approval(
    mocker, tmp_path, capsys, grant, interactive, allow, yes, answer, remote, accepted
):
    from jailbee.config.models_host import OptionalMount

    cfg = make_cfg(tmp_path).model_copy(
        update={
            "optional_mounts": {"credentials": OptionalMount(host=tmp_path, container="/creds")}
        }
    )
    incus = mocker.Mock()
    incus.config_get.return_value = "clone"
    incus.exec.return_value = (
        "container_prefix: test\nautostart:\n  on_start:\n    - name: open\n"
        f"      run: make\n      {grant}\n"
    )
    mocker.patch("jailbee.git.show_file_at_ref", return_value=None)
    mocker.patch("jailbee.git.remote_ref_exists", return_value=True)
    mocker.patch("jailbee.prompting.is_interactive", return_value=interactive)
    confirm = mocker.patch("jailbee.tui.default_confirm", return_value=answer)
    mocker.patch("jailbee.remote_ssh.session.is_restricted_session", return_value=remote)
    result = branch_config.effective_autostart_config(
        cfg, incus, "box", assume_yes=yes, allow_prompt=allow
    )
    assert (result is not cfg) == accepted
    assert confirm.called == (not remote and not yes and allow and interactive)
    output = capsys.readouterr().out
    assert "widens privileges" in output
    assert ("Falling back" in output) == (not accepted)
    if not accepted:
        incus.config_set.assert_not_called()


@pytest.mark.parametrize(
    "requested,override,expected",
    [
        ("new", ",", '{"version":1,"services":{}}'),
        ("missing", None, None),
    ],
)
def test_requested_profile_respects_override_and_invalid_does_not_write(
    mocker, tmp_path, requested, override, expected
):
    cfg = make_cfg(tmp_path)
    incus = mocker.Mock()
    incus.config_get.side_effect = lambda name, key: {
        "user.jailbee.mode": "clone",
        "user.jailbee.repo_dir": "/repo",
        "user.jailbee.autostart_services": override,
    }.get(key)
    incus.exec.return_value = (
        "container_prefix: test\nautostart:\n  default_profile: new\n  profiles:\n"
        "    new:\n      stages: [services]\n      services: [web]\n"
        "services:\n  web:\n    run: serve\n"
        "    prepare:\n      - run: setup\n        network: loose\n"
    )
    mocker.patch("jailbee.remote_ssh.session.is_restricted_session", return_value=False)

    branch_config.effective_autostart_config(
        cfg, incus, "box", assume_yes=True, approval_profile=requested
    )

    if expected is None:
        incus.config_set.assert_not_called()
    else:
        incus.config_set.assert_called_once_with("box", "user.jailbee.services_approval", expected)


@pytest.mark.parametrize(
    "interactive,yes,answer,remote,accepted",
    [
        (True, False, True, False, True),
        (False, True, False, False, True),
        (True, False, False, False, False),
        (False, False, True, False, False),
        (True, True, True, True, False),
    ],
)
def test_service_widening_updates_approval_only_when_accepted(
    mocker, tmp_path, interactive, yes, answer, remote, accepted
):
    cfg = make_cfg(tmp_path)
    incus = mocker.Mock()
    incus.config_get.side_effect = lambda name, key: {
        "user.jailbee.mode": "clone",
        "user.jailbee.repo_dir": "/repo",
        "user.jailbee.autostart_services": None,
        "user.jailbee.autostart_profile": None,
    }.get(key)
    incus.exec.return_value = (
        "container_prefix: test\nservices:\n  web:\n    run: serve\n"
        "    prepare:\n      - run: setup\n        network: loose\n"
    )
    mocker.patch("jailbee.git.show_file_at_ref", return_value=None)
    mocker.patch("jailbee.git.remote_ref_exists", return_value=True)
    mocker.patch("jailbee.prompting.is_interactive", return_value=interactive)
    mocker.patch("jailbee.tui.default_confirm", return_value=answer)
    mocker.patch("jailbee.remote_ssh.session.is_restricted_session", return_value=remote)

    result = branch_config.effective_autostart_config(
        cfg, incus, "box", assume_yes=yes, allow_prompt=True
    )

    assert (result is not cfg) == accepted
    if accepted:
        incus.config_set.assert_called_once_with(
            "box",
            "user.jailbee.services_approval",
            '{"version":1,"services":{"web":{"network":"loose","mounts":[]}}}',
        )
    else:
        incus.config_set.assert_not_called()
