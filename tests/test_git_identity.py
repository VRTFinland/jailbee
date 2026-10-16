"""Tests for jailbee.git_identity: host-side resolution and the container write."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from jailbee import git_identity
from jailbee.config import GitConfig, GitUserConfig, GpgConfig
from jailbee.incus import IncusError
from tests.conftest import make_config


def _cfg(tmp_path, *, gpg=False, identity="host", name=None, email=None):
    cfg = make_config(tmp_path)
    return cfg.model_copy(
        update={
            "gpg": GpgConfig(enabled=gpg),
            "git": GitConfig(identity=identity, user=GitUserConfig(name=name, email=email)),
        }
    )


def _host(mocker, values):
    """Patch host `git config --get` to answer from `values`."""
    return mocker.patch(
        "jailbee.git_identity.git.config_get",
        side_effect=lambda root, key: values.get(key),
    )


def _argv(incus: MagicMock) -> list[str]:
    assert incus.exec.call_count == 1
    return list(incus.exec.call_args.args[1])


def test_resolve_reads_host_repo_values(tmp_path, mocker):
    cfg = _cfg(tmp_path)
    get = _host(mocker, {"user.name": "Host Name", "user.email": "host@example.com"})

    values = git_identity.resolve(cfg)

    assert values["user.name"] == "Host Name"
    assert values["user.email"] == "host@example.com"
    for key in git_identity.IDENTITY_KEYS:
        get.assert_any_call(cfg.repo_root, key)


def test_override_is_per_field(tmp_path, mocker):
    cfg = _cfg(tmp_path, email="cfg@example.com")
    _host(mocker, {"user.name": "Host", "user.email": "host@example.com"})

    values = git_identity.resolve(cfg)

    assert values["user.name"] == "Host"
    assert values["user.email"] == "cfg@example.com"


def test_signing_keys_none_when_gpg_disabled(tmp_path, mocker):
    cfg = _cfg(tmp_path, gpg=False)
    get = _host(mocker, {"user.signingkey": "ABC", "commit.gpgsign": "true"})

    values = git_identity.resolve(cfg)

    for key in git_identity.SIGNING_KEYS:
        assert values[key] is None
    asked = {c.args[1] for c in get.call_args_list}
    assert asked.isdisjoint(git_identity.SIGNING_KEYS)


def test_signing_keys_resolved_when_gpg_enabled(tmp_path, mocker):
    cfg = _cfg(tmp_path, gpg=True)
    _host(
        mocker,
        {
            "user.signingkey": "ABC123",
            "commit.gpgsign": "true",
            "tag.gpgsign": "true",
            "gpg.format": "openpgp",
        },
    )

    values = git_identity.resolve(cfg)

    assert values["user.signingkey"] == "ABC123"
    assert values["commit.gpgsign"] == "true"
    assert values["tag.gpgsign"] == "true"
    assert values["gpg.format"] == "openpgp"


def test_write_passes_values_verbatim(tmp_path):
    cfg = _cfg(tmp_path)
    incus = MagicMock()
    nasty = 'O\'Brien "Bob" $(touch /tmp/pwned) `id`'

    git_identity.write(cfg, incus, "c1", {"user.name": nasty, "user.email": "a@b"})

    argv = _argv(incus)
    assert nasty in argv
    script = argv[2]
    assert nasty not in script
    kwargs = incus.exec.call_args.kwargs
    assert incus.exec.call_args.args[0] == "c1"
    assert kwargs["uid"] == cfg.container_user.uid
    assert kwargs["gid"] == cfg.container_user.gid
    assert kwargs["env"]["HOME"] == "/home/dev"


def test_write_targets_the_xdg_file(tmp_path):
    cfg = _cfg(tmp_path)
    incus = MagicMock()

    git_identity.write(cfg, incus, "c1", {"user.name": "N"})

    assert git_identity.CONTAINER_GITCONFIG == "/home/dev/.config/git/config"
    assert git_identity.CONTAINER_GITCONFIG in _argv(incus)


def test_write_unsets_none_values(tmp_path):
    cfg = _cfg(tmp_path)
    incus = MagicMock()

    git_identity.write(cfg, incus, "c1", {"user.name": "N", "user.email": None})

    argv = _argv(incus)
    args = argv[argv.index(git_identity.CONTAINER_GITCONFIG) + 1 :]
    assert args == ["set", "user.name", "N", "unset", "user.email"]


def test_write_script_creates_dir_and_tolerates_missing_key():
    script = git_identity._WRITE_SCRIPT
    assert "mkdir -p" in script
    assert "--unset-all" in script
    assert "5" in script  # git config exits 5 when the key is absent


def test_write_script_runs_under_sh(tmp_path):
    """Execute the real script locally against a temp file."""
    import subprocess

    target = tmp_path / "home" / ".config" / "git" / "config"
    nasty = 'O\'Brien "Bob" $(echo pwned)'
    subprocess.run(
        [
            "sh",
            "-c",
            git_identity._WRITE_SCRIPT,
            "sh",
            str(target),
            "set",
            "user.name",
            nasty,
            "unset",
            "user.email",
        ],
        check=True,
    )
    out = subprocess.run(
        ["git", "config", "--file", str(target), "--get", "user.name"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert out == nasty


def test_signing_keys_unset_when_gpg_disabled(tmp_path, mocker):
    cfg = _cfg(tmp_path, gpg=False)
    _host(mocker, {"user.name": "N", "user.email": "e@x"})
    incus = MagicMock()

    git_identity.sync(cfg, incus, "c1")

    argv = _argv(incus)
    for key in git_identity.SIGNING_KEYS:
        i = argv.index(key)
        assert argv[i - 1] == "unset"


def test_sync_off_does_nothing(tmp_path, mocker):
    cfg = _cfg(tmp_path, identity="off")
    get = _host(mocker, {})
    incus = MagicMock()

    git_identity.sync(cfg, incus, "c1")

    incus.exec.assert_not_called()
    get.assert_not_called()


def test_sync_warns_on_exec_failure(tmp_path, mocker):
    cfg = _cfg(tmp_path)
    _host(mocker, {"user.name": "N", "user.email": "e@x"})
    warn = mocker.patch("jailbee.git_identity.warn")
    incus = MagicMock()
    incus.exec.side_effect = IncusError("boom")

    git_identity.sync(cfg, incus, "c1")

    warn.assert_called_once()
    assert "git identity" in warn.call_args.args[0]


def test_sync_warns_on_missing_identity(tmp_path, mocker):
    cfg = _cfg(tmp_path)
    _host(mocker, {"user.name": "N"})
    warn = mocker.patch("jailbee.git_identity.warn")
    incus = MagicMock()

    git_identity.sync(cfg, incus, "c1")

    warn.assert_called_once()
    assert "user.email" in warn.call_args.args[0]
    incus.exec.assert_called_once()


def test_sync_quiet_when_identity_complete(tmp_path, mocker):
    cfg = _cfg(tmp_path)
    _host(mocker, {"user.name": "N", "user.email": "e@x"})
    warn = mocker.patch("jailbee.git_identity.warn")

    git_identity.sync(cfg, MagicMock(), "c1")

    warn.assert_not_called()


@pytest.mark.parametrize("value", [None, "v"])
def test_config_get_is_public(tmp_path, mocker, value):
    from jailbee import git

    run = mocker.patch("jailbee.git.subprocess.run")
    run.return_value = MagicMock(returncode=0 if value else 1, stdout=(value or "") + "\n")

    assert git.config_get(tmp_path, "user.name") == value
    assert run.call_args.args[0] == ["git", "config", "--get", "user.name"]
    assert run.call_args.kwargs["cwd"] == tmp_path


def test_boot_container_survives_identity_write_failure(tmp_path, mocker):
    """The real `sync` inside the real boot path: a failed exec never fails a start."""
    from jailbee.lifecycle import boot_container

    cfg = _cfg(tmp_path)
    _host(mocker, {"user.name": "N", "user.email": "e@x"})
    mocker.patch("jailbee.runtime_mounts.detach_runtime_devices")
    mocker.patch("jailbee.runtime_mounts.attach_runtime_devices", return_value=True)
    mocker.patch("jailbee.agent_instructions.sync_global_instructions")
    mocker.patch("jailbee.agent_instructions.missing_staging_dirs", return_value=[])
    mocker.patch("jailbee.pool.allocate_startup")
    mocker.patch("jailbee.agent_private.attach")
    warn = mocker.patch("jailbee.git_identity.warn")
    incus = MagicMock()
    incus.list_containers.return_value = [{"name": "feat-x", "status": "Stopped"}]

    def exec_(name, cmd, **kw):
        if cmd[:2] == ["sh", "-c"]:
            raise IncusError("container not ready")
        return ""

    incus.exec.side_effect = exec_

    boot_container(cfg, incus, "feat-x", restart=False)

    incus.start.assert_called_once()
    warn.assert_called_once()
