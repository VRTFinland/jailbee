"""The two-layer Claude credential group model."""

from __future__ import annotations

from pathlib import Path

import pytest

from jailbee import claude_groups
from tests.conftest import make_cfg


def _cfg(tmp_path: Path, group_dir: Path | None = None):
    cfg = make_cfg(tmp_path / "myrepo", shared_dir=tmp_path / "shared")
    if group_dir is not None:
        cfg = cfg.model_copy(update={"claude_credentials_dir": group_dir})
    return cfg


def test_validate_accepts_a_normal_name():
    assert claude_groups.validate_group_name("work") == "work"
    assert claude_groups.validate_group_name("work-2") == "work-2"


@pytest.mark.parametrize("bad", ["Work", "-work", "_work", "work_2", "work/x", ""])
def test_validate_rejects_names_outside_the_grammar(bad: str):
    with pytest.raises(claude_groups.GroupError):
        claude_groups.validate_group_name(bad)


def test_validate_rejects_the_reserved_word_none():
    with pytest.raises(claude_groups.GroupError) as e:
        claude_groups.validate_group_name("none")
    assert "reserved" in str(e.value).lower()


def test_repo_group_is_the_resolved_directory_name(tmp_path: Path):
    cfg = _cfg(tmp_path, tmp_path / "creds" / "work")
    assert claude_groups.repo_group(cfg) == "work"


def test_repo_group_is_none_when_the_repo_shares_nothing(tmp_path: Path):
    assert claude_groups.repo_group(_cfg(tmp_path)) is None


def test_container_override_absent_label_means_inherit(mocker, tmp_path: Path):
    incus = mocker.MagicMock()
    incus.config_get.return_value = None
    assert claude_groups.container_override(incus, "myrepo-x") is None


def test_container_override_names_a_group(mocker):
    incus = mocker.MagicMock()
    incus.config_get.return_value = "personal"
    assert claude_groups.container_override(incus, "myrepo-x") == claude_groups.Override("personal")


def test_container_override_no_group_sentinel(mocker):
    incus = mocker.MagicMock()
    incus.config_get.return_value = claude_groups.NO_GROUP
    assert claude_groups.container_override(incus, "myrepo-x") == claude_groups.Override(None)


def test_container_override_ignores_a_garbage_label(mocker):
    """A hand-edited label must not resolve to a path outside the store."""
    incus = mocker.MagicMock()
    incus.config_get.return_value = "../../etc"
    assert claude_groups.container_override(incus, "myrepo-x") is None


def test_effective_group_prefers_the_container(mocker, tmp_path: Path):
    incus = mocker.MagicMock()
    incus.config_get.return_value = "personal"
    cfg = _cfg(tmp_path, tmp_path / "creds" / "work")
    assert claude_groups.effective_group(cfg, incus, "myrepo-x") == "personal"


def test_effective_group_falls_back_to_the_repo(mocker, tmp_path: Path):
    incus = mocker.MagicMock()
    incus.config_get.return_value = None
    cfg = _cfg(tmp_path, tmp_path / "creds" / "work")
    assert claude_groups.effective_group(cfg, incus, "myrepo-x") == "work"


def test_effective_group_container_can_opt_out_of_the_repos_group(mocker, tmp_path: Path):
    incus = mocker.MagicMock()
    incus.config_get.return_value = claude_groups.NO_GROUP
    cfg = _cfg(tmp_path, tmp_path / "creds" / "work")
    assert claude_groups.effective_group(cfg, incus, "myrepo-x") is None


def test_group_dir_is_a_sibling_of_the_parked_store(monkeypatch, tmp_path: Path):
    from jailbee import claude_pool

    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    assert claude_groups.group_dir("work").parent == claude_pool.store_dir().parent


from jailbee.profiles import CLAUDE_CREDS_DEVICE, CLAUDE_CREDS_DIRNAME

_ENV_KEY = "environment.CLAUDE_SECURESTORAGE_CONFIG_DIR"


def test_ensure_group_dir_creates_it_0700(monkeypatch, tmp_path: Path):
    import stat

    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    created = claude_groups.ensure_group_dir("work")
    assert created.is_dir()
    assert stat.S_IMODE(created.stat().st_mode) == 0o700


def test_set_container_group_overrides_the_profile_device(monkeypatch, mocker, tmp_path: Path):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    incus = mocker.MagicMock()
    # The repo has a group, so the binds profile carries the device.
    cfg = _cfg(tmp_path, claude_groups.group_dir("work"))
    mocker.patch("jailbee.claude_groups._profile_has_creds_device", return_value=True)

    claude_groups.set_container_group(cfg, incus, "myrepo-x", "personal")

    incus.config_device_override.assert_called_once_with(
        "myrepo-x",
        CLAUDE_CREDS_DEVICE,
        {"source": str(claude_groups.group_dir("personal"))},
    )
    incus.config_device_add.assert_not_called()


def test_set_container_group_adds_the_device_when_the_profile_has_none(
    monkeypatch, mocker, tmp_path: Path
):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    incus = mocker.MagicMock()
    cfg = _cfg(tmp_path)  # repo shares no group -> profiles.py renders no device
    mocker.patch("jailbee.claude_groups._profile_has_creds_device", return_value=False)

    claude_groups.set_container_group(cfg, incus, "myrepo-x", "personal")

    incus.config_device_add.assert_called_once()
    args = incus.config_device_add.call_args.args
    assert args[1] == CLAUDE_CREDS_DEVICE
    assert args[2] == "disk"
    assert args[3]["source"] == str(claude_groups.group_dir("personal"))
    assert args[3]["path"].endswith(f"/{CLAUDE_CREDS_DIRNAME}")
    incus.config_device_override.assert_not_called()


def test_set_container_group_always_sets_the_env_key(monkeypatch, mocker, tmp_path: Path):
    """The profile carries no env key for a group-less repo, and can lose it later."""
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    incus = mocker.MagicMock()
    mocker.patch("jailbee.claude_groups._profile_has_creds_device", return_value=True)

    claude_groups.set_container_group(
        _cfg(tmp_path, claude_groups.group_dir("work")), incus, "myrepo-x", "personal"
    )

    env_calls = [c for c in incus.config_set.call_args_list if c.args[1] == _ENV_KEY]
    assert len(env_calls) == 1
    assert env_calls[0].args[2].endswith(f"/{CLAUDE_CREDS_DIRNAME}")


def test_set_container_group_writes_the_label(monkeypatch, mocker, tmp_path: Path):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    incus = mocker.MagicMock()
    mocker.patch("jailbee.claude_groups._profile_has_creds_device", return_value=True)

    claude_groups.set_container_group(
        _cfg(tmp_path, claude_groups.group_dir("work")), incus, "myrepo-x", "personal"
    )

    label_calls = [
        c for c in incus.config_set.call_args_list if c.args[1] == claude_groups.GROUP_LABEL
    ]
    assert label_calls == [mocker.call("myrepo-x", claude_groups.GROUP_LABEL, "personal")]


def test_set_container_group_to_no_group_removes_the_device(monkeypatch, mocker, tmp_path: Path):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    incus = mocker.MagicMock()

    claude_groups.set_container_group(
        _cfg(tmp_path, claude_groups.group_dir("work")), incus, "myrepo-x", None
    )

    incus.config_device_remove.assert_called_once_with(
        "myrepo-x", CLAUDE_CREDS_DEVICE, missing_ok=True
    )
    # The env key points at the repo's own config home, not at the creds mount.
    env_calls = [c for c in incus.config_set.call_args_list if c.args[1] == _ENV_KEY]
    assert env_calls[0].args[2].endswith("/.claude")
    label_calls = [
        c for c in incus.config_set.call_args_list if c.args[1] == claude_groups.GROUP_LABEL
    ]
    assert label_calls[0].args[2] == claude_groups.NO_GROUP


def test_set_container_group_rejects_a_reserved_name(mocker, tmp_path: Path):
    incus = mocker.MagicMock()
    with pytest.raises(claude_groups.GroupError):
        claude_groups.set_container_group(_cfg(tmp_path), incus, "myrepo-x", "none")
    incus.config_set.assert_not_called()


def test_clear_container_group_removes_all_three(mocker):
    incus = mocker.MagicMock()
    claude_groups.clear_container_group(incus, "myrepo-x")
    incus.config_device_remove.assert_called_once_with(
        "myrepo-x", CLAUDE_CREDS_DEVICE, missing_ok=True
    )
    unset = [c.args[1] for c in incus.config_unset.call_args_list]
    assert unset == [_ENV_KEY, claude_groups.GROUP_LABEL]
