"""The two-layer Claude credential group model."""

from __future__ import annotations

from pathlib import Path

import pytest

from jailbee import claude_groups
from jailbee.global_config import GlobalConfig
from jailbee.profiles import CLAUDE_CREDS_DEVICE, CLAUDE_CREDS_DIRNAME
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
    incus.list_containers.return_value = []  # no local device yet -> override path
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
    incus.list_containers.return_value = []  # no local device yet -> add path
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
    incus.list_containers.return_value = []  # no local device yet -> override path
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
    incus.list_containers.return_value = []  # no local device yet -> override path
    mocker.patch("jailbee.claude_groups._profile_has_creds_device", return_value=True)

    claude_groups.set_container_group(
        _cfg(tmp_path, claude_groups.group_dir("work")), incus, "myrepo-x", "personal"
    )

    label_calls = [
        c for c in incus.config_set.call_args_list if c.args[1] == claude_groups.GROUP_LABEL
    ]
    assert label_calls == [mocker.call("myrepo-x", claude_groups.GROUP_LABEL, "personal")]


def test_set_container_group_updates_an_already_local_device(monkeypatch, mocker, tmp_path: Path):
    """A second `use` call must update the device in place, not override it.

    `config_device_override` fails once a local device already shadows the
    profile (`incus.py:504`) — the exact scenario a repeated `jailbee claude
    group use` on the same container hits.
    """
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    incus = mocker.MagicMock()
    incus.list_containers.return_value = [
        {
            "name": "myrepo-x",
            "devices": {CLAUDE_CREDS_DEVICE: {"source": "/some/old/path"}},
        }
    ]
    cfg = _cfg(tmp_path, claude_groups.group_dir("work"))
    mocker.patch("jailbee.claude_groups._profile_has_creds_device", return_value=True)

    claude_groups.set_container_group(cfg, incus, "myrepo-x", "personal")

    incus.config_device_set.assert_called_once_with(
        "myrepo-x",
        CLAUDE_CREDS_DEVICE,
        {"source": str(claude_groups.group_dir("personal"))},
    )
    incus.config_device_override.assert_not_called()
    incus.config_device_add.assert_not_called()


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


def _gcfg(**creds):
    return GlobalConfig.model_validate({"claude_credentials": creds} if creds else {})


def _raw(name: str, group: str | None = None) -> dict:
    config = {} if group is None else {claude_groups.GROUP_LABEL: group}
    return {"name": name, "status": "Running", "profiles": [], "config": config, "state": None}


def test_groups_by_prefix_uses_the_repo_group_for_unlabelled_containers(
    mocker, monkeypatch, tmp_path
):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    incus = mocker.MagicMock()
    incus.list_containers.return_value = [_raw("myrepo-a"), _raw("myrepo-b")]
    gcfg = _gcfg(group="work")
    assert claude_groups.groups_by_prefix(gcfg, incus, ["myrepo"]) == {"myrepo": {"work"}}


def test_groups_by_prefix_sees_a_deviating_container(mocker, monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    incus = mocker.MagicMock()
    incus.list_containers.return_value = [_raw("myrepo-a"), _raw("myrepo-b", "personal")]
    gcfg = _gcfg(group="work")
    assert claude_groups.groups_by_prefix(gcfg, incus, ["myrepo"]) == {
        "myrepo": {"work", "personal"}
    }


def test_groups_by_prefix_falls_back_to_the_repo_group_with_no_containers(
    mocker, monkeypatch, tmp_path
):
    """A repo with no containers is still authoritative for its own group."""
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    incus = mocker.MagicMock()
    incus.list_containers.return_value = []
    assert claude_groups.groups_by_prefix(_gcfg(group="work"), incus, ["myrepo"]) == {
        "myrepo": {"work"}
    }


def test_groups_by_prefix_counts_stopped_containers(mocker, monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    incus = mocker.MagicMock()
    stopped = _raw("myrepo-b", "personal")
    stopped["status"] = "Stopped"
    incus.list_containers.return_value = [_raw("myrepo-a"), stopped]
    assert claude_groups.groups_by_prefix(_gcfg(group="work"), incus, ["myrepo"]) == {
        "myrepo": {"work", "personal"}
    }


def test_authoritative_excludes_a_repo_spanning_two_groups(mocker, monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    incus = mocker.MagicMock()
    incus.list_containers.return_value = [
        _raw("mixed-a"),
        _raw("mixed-b", "personal"),
        _raw("clean-a"),
    ]
    gcfg = _gcfg(group="work")
    assert claude_groups.authoritative_prefixes(gcfg, incus, "work", ["mixed", "clean"]) == {
        "clean"
    }


def test_deviating_containers_lists_only_the_odd_ones_out(mocker, monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    incus = mocker.MagicMock()
    incus.list_containers.return_value = [_raw("myrepo-a"), _raw("myrepo-b", "personal")]
    cfg = _cfg(tmp_path, claude_groups.group_dir("work"))
    assert claude_groups.deviating_containers(cfg, incus) == [("myrepo-b", "personal")]


def test_claude_running_true(mocker, tmp_path):
    incus = mocker.MagicMock()
    incus.exec.return_value = "running\n"
    assert claude_groups.claude_running(_cfg(tmp_path), incus, "myrepo-a") is True


def test_claude_running_false(mocker, tmp_path):
    incus = mocker.MagicMock()
    incus.exec.return_value = "idle\n"
    assert claude_groups.claude_running(_cfg(tmp_path), incus, "myrepo-a") is False


def test_claude_running_unknown_when_the_probe_fails(mocker, tmp_path):
    from jailbee.incus import IncusError

    incus = mocker.MagicMock()
    incus.exec.side_effect = IncusError("container is not running")
    assert claude_groups.claude_running(_cfg(tmp_path), incus, "myrepo-a") is None


def test_claude_running_probe_uses_pgrep_x_not_f(mocker, tmp_path):
    """`pgrep -f` matches its own command line and would always say yes."""
    incus = mocker.MagicMock()
    incus.exec.return_value = "idle\n"
    claude_groups.claude_running(_cfg(tmp_path), incus, "myrepo-a")
    script = incus.exec.call_args.args[1][-1]
    assert "pgrep -u" in script
    assert " -x " in script
    assert " -f " not in script


# --- Reading one `incus list` for the whole host ------------------------------


def test_groups_by_prefix_from_reuses_prefetched_rows(monkeypatch, tmp_path):
    """The host-wide listing resolves every group from one `incus list`; a
    per-group call would cost one subprocess per group."""
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    rows = [_raw("myrepo-a"), _raw("myrepo-b", "personal")]
    assert claude_groups.groups_by_prefix_from(_gcfg(group="work"), rows, ["myrepo"]) == {
        "myrepo": {"work", "personal"}
    }


def test_authoritative_prefixes_from_reuses_prefetched_rows(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    rows = [_raw("mixed-a"), _raw("mixed-b", "personal"), _raw("clean-a")]
    assert claude_groups.authoritative_prefixes_from(
        _gcfg(group="work"), rows, "work", ["mixed", "clean"]
    ) == {"clean"}


def test_container_groups_reports_the_repos_group_for_an_unlabelled_container(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    rows = [_raw("myrepo-a")]
    assert claude_groups.container_groups(_gcfg(group="work"), rows, ["myrepo"]) == [
        ("myrepo-a", "myrepo", "work")
    ]


def test_container_groups_reports_a_temporary_override(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    rows = [_raw("myrepo-a", "personal")]
    assert claude_groups.container_groups(_gcfg(group="work"), rows, ["myrepo"]) == [
        ("myrepo-a", "myrepo", "personal")
    ]


def test_container_groups_names_the_repo_of_an_ungrouped_container(monkeypatch, tmp_path):
    """`None` is not one holder: a container in no group reads *its own repo's*
    config home, so the prefix is part of the answer."""
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    rows = [_raw("myrepo-a", claude_groups.NO_GROUP)]
    assert claude_groups.container_groups(_gcfg(group="work"), rows, ["myrepo"]) == [
        ("myrepo-a", "myrepo", None)
    ]


def test_container_groups_attributes_a_container_to_its_longest_prefix(monkeypatch, tmp_path):
    """`app-web-x` belongs to `app-web`, not to `app`, when both are registered."""
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    gcfg = GlobalConfig.model_validate(
        {"claude_credentials": {"repos": {"app": "one", "app-web": "two"}}}
    )
    rows = [_raw("app-web-x")]
    assert claude_groups.container_groups(gcfg, rows, ["app", "app-web"]) == [
        ("app-web-x", "app-web", "two")
    ]


def test_container_groups_ignores_a_container_of_an_unknown_repo(monkeypatch, tmp_path):
    """Nothing on the host says which group an unregistered repo resolves to,
    and guessing one would put a container under the wrong login."""
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    rows = [_raw("stranger-a"), _raw("myrepo-a")]
    assert claude_groups.container_groups(_gcfg(group="work"), rows, ["myrepo"]) == [
        ("myrepo-a", "myrepo", "work")
    ]


def test_container_groups_ignores_a_garbage_label(monkeypatch, tmp_path):
    """A hand-edited label must never become a path component; the container
    falls back to its repo's group, as `container_override` does."""
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    rows = [_raw("myrepo-a", "../../etc")]
    assert claude_groups.container_groups(_gcfg(group="work"), rows, ["myrepo"]) == [
        ("myrepo-a", "myrepo", "work")
    ]


def test_authoritative_in_answers_the_no_group_question_too(monkeypatch, tmp_path):
    """An ungrouped holder is the same question with "no group" as the answer:
    a repo whose containers span a group can no longer name its own login."""
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    by_prefix = {"clean": {None}, "spanning": {None, "personal"}}

    assert claude_groups.authoritative_in(by_prefix, None) == {"clean"}


def test_authoritative_in_is_the_rule_authoritative_prefixes_applies(monkeypatch, tmp_path):
    """One implementation, so a caller holding a prefetched `groups_by_prefix`
    cannot drift from the `Incus`-taking wrapper."""
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    rows = [_raw("mixed-a"), _raw("mixed-b", "personal"), _raw("clean-a")]
    gcfg = _gcfg(group="work")

    by_prefix = claude_groups.groups_by_prefix_from(gcfg, rows, ["mixed", "clean"])

    assert claude_groups.authoritative_in(by_prefix, "work") == (
        claude_groups.authoritative_prefixes_from(gcfg, rows, "work", ["mixed", "clean"])
    )
