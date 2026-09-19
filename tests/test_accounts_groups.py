"""The two-layer Claude credential group model."""

from __future__ import annotations

from pathlib import Path

import pytest

from jailbee.accounts import groups
from jailbee.global_config import GlobalConfig
from jailbee.profiles import CLAUDE_CREDS_DEVICE, CLAUDE_CREDS_DIRNAME
from tests.conftest import make_cfg


def _cfg(tmp_path: Path, group: str | None = None):
    cfg = make_cfg(tmp_path / "myrepo", shared_dir=tmp_path / "shared")
    if group is not None:
        cfg = cfg.model_copy(update={"credential_group": group})
    return cfg


def test_validate_accepts_a_normal_name():
    assert groups.validate_group_name("work") == "work"
    assert groups.validate_group_name("work-2") == "work-2"


@pytest.mark.parametrize("bad", ["Work", "-work", "_work", "work_2", "work/x", ""])
def test_validate_rejects_names_outside_the_grammar(bad: str):
    with pytest.raises(groups.GroupError):
        groups.validate_group_name(bad)


def test_validate_rejects_the_reserved_word_none():
    with pytest.raises(groups.GroupError) as e:
        groups.validate_group_name("none")
    assert "reserved" in str(e.value).lower()


def test_repo_group_is_the_resolved_group_name(tmp_path: Path):
    from jailbee.accounts import engine

    cfg = _cfg(tmp_path, "work")
    assert engine.repo_group(cfg) == "work"


def test_repo_group_is_none_when_the_repo_shares_nothing(tmp_path: Path):
    from jailbee.accounts import engine

    assert engine.repo_group(_cfg(tmp_path)) is None


def test_container_override_absent_label_means_inherit(mocker, tmp_path: Path):
    incus = mocker.MagicMock()
    incus.config_get.return_value = None
    assert groups.container_override(incus, "myrepo-x") is None


def test_container_override_names_a_group(mocker):
    incus = mocker.MagicMock()
    incus.config_get.return_value = "personal"
    assert groups.container_override(incus, "myrepo-x") == groups.Override("personal")


def test_container_override_no_group_sentinel(mocker):
    incus = mocker.MagicMock()
    incus.config_get.return_value = groups.NO_GROUP
    assert groups.container_override(incus, "myrepo-x") == groups.Override(None)


def test_container_override_ignores_a_garbage_label(mocker):
    """A hand-edited label must not resolve to a path outside the store."""
    incus = mocker.MagicMock()
    incus.config_get.return_value = "../../etc"
    assert groups.container_override(incus, "myrepo-x") is None


def test_effective_group_prefers_the_container(mocker, tmp_path: Path):
    incus = mocker.MagicMock()
    incus.config_get.return_value = "personal"
    cfg = _cfg(tmp_path, "work")
    assert groups.effective_group(cfg, incus, "myrepo-x") == "personal"


def test_effective_group_falls_back_to_the_repo(mocker, tmp_path: Path):
    incus = mocker.MagicMock()
    incus.config_get.return_value = None
    cfg = _cfg(tmp_path, "work")
    assert groups.effective_group(cfg, incus, "myrepo-x") == "work"


def test_effective_group_container_can_opt_out_of_the_repos_group(mocker, tmp_path: Path):
    incus = mocker.MagicMock()
    incus.config_get.return_value = groups.NO_GROUP
    cfg = _cfg(tmp_path, "work")
    assert groups.effective_group(cfg, incus, "myrepo-x") is None


def test_group_dir_is_a_sibling_of_the_parked_store(monkeypatch, tmp_path: Path):
    from jailbee.accounts import engine
    from jailbee.accounts.adapters.claude import CLAUDE

    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    assert groups.group_dir("claude", "work").parent == engine.store_dir(CLAUDE).parent


def test_group_dir_is_keyed_by_agent(monkeypatch, tmp_path) -> None:
    """Claude's path is unchanged; a second agent gets a root of its own."""
    monkeypatch.setattr("jailbee.paths.xdg_data_home", lambda: tmp_path)

    assert groups.group_dir("claude", "work") == (
        tmp_path / "jailbee" / "claude-credentials" / "work"
    )
    assert groups.group_dir("codex", "work") == (
        tmp_path / "jailbee" / "codex-credentials" / "work"
    )


_ENV_KEY = "environment.CLAUDE_SECURESTORAGE_CONFIG_DIR"


def test_ensure_group_dir_creates_it_0700(monkeypatch, tmp_path: Path):
    import stat

    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    created = groups.ensure_group_dir("claude", "work")
    assert created.is_dir()
    assert stat.S_IMODE(created.stat().st_mode) == 0o700


def test_set_container_group_overrides_the_profile_device(monkeypatch, mocker, tmp_path: Path):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    incus = mocker.MagicMock()
    incus.list_containers.return_value = []  # no local device yet -> override path
    # The repo has a group, so the binds profile carries the device.
    cfg = _enabled_cfg(tmp_path, "work")

    groups.set_container_group(cfg, incus, "myrepo-x", "personal")

    incus.config_device_override.assert_called_once_with(
        "myrepo-x",
        CLAUDE_CREDS_DEVICE,
        {"source": str(groups.group_dir("claude", "personal"))},
    )
    incus.config_device_add.assert_not_called()


def test_set_container_group_adds_the_device_when_the_profile_has_none(
    monkeypatch, mocker, tmp_path: Path
):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    incus = mocker.MagicMock()
    incus.list_containers.return_value = []  # no local device yet -> add path
    cfg = _enabled_cfg(tmp_path)  # repo shares no group -> profiles.py renders no device

    groups.set_container_group(cfg, incus, "myrepo-x", "personal")

    incus.config_device_add.assert_called_once()
    args = incus.config_device_add.call_args.args
    assert args[1] == CLAUDE_CREDS_DEVICE
    assert args[2] == "disk"
    assert args[3]["source"] == str(groups.group_dir("claude", "personal"))
    assert args[3]["path"].endswith(f"/{CLAUDE_CREDS_DIRNAME}")
    incus.config_device_override.assert_not_called()


def test_set_container_group_always_sets_the_env_key(monkeypatch, mocker, tmp_path: Path):
    """The profile carries no env key for a group-less repo, and can lose it later."""
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    incus = mocker.MagicMock()
    incus.list_containers.return_value = []  # no local device yet -> override path

    groups.set_container_group(_enabled_cfg(tmp_path, "work"), incus, "myrepo-x", "personal")

    env_calls = [c for c in incus.config_set.call_args_list if c.args[1] == _ENV_KEY]
    assert len(env_calls) == 1
    assert env_calls[0].args[2].endswith(f"/{CLAUDE_CREDS_DIRNAME}")


def test_set_container_group_writes_the_label(monkeypatch, mocker, tmp_path: Path):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    incus = mocker.MagicMock()
    incus.list_containers.return_value = []  # no local device yet -> override path

    groups.set_container_group(_enabled_cfg(tmp_path, "work"), incus, "myrepo-x", "personal")

    label_calls = [c for c in incus.config_set.call_args_list if c.args[1] == groups.GROUP_LABEL]
    assert label_calls == [mocker.call("myrepo-x", groups.GROUP_LABEL, "personal")]


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
    cfg = _enabled_cfg(tmp_path, "work")

    groups.set_container_group(cfg, incus, "myrepo-x", "personal")

    incus.config_device_set.assert_called_once_with(
        "myrepo-x",
        CLAUDE_CREDS_DEVICE,
        {"source": str(groups.group_dir("claude", "personal"))},
    )
    incus.config_device_override.assert_not_called()
    incus.config_device_add.assert_not_called()


def test_set_container_group_to_no_group_removes_the_device(monkeypatch, mocker, tmp_path: Path):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    incus = mocker.MagicMock()

    groups.set_container_group(_enabled_cfg(tmp_path, "work"), incus, "myrepo-x", None)

    incus.config_device_remove.assert_called_once_with(
        "myrepo-x", CLAUDE_CREDS_DEVICE, missing_ok=True
    )
    # The env key points at the repo's own config home, not at the creds mount.
    env_calls = [c for c in incus.config_set.call_args_list if c.args[1] == _ENV_KEY]
    assert env_calls[0].args[2].endswith("/.claude")
    label_calls = [c for c in incus.config_set.call_args_list if c.args[1] == groups.GROUP_LABEL]
    assert label_calls[0].args[2] == groups.NO_GROUP


def test_set_container_group_rejects_a_reserved_name(mocker, tmp_path: Path):
    incus = mocker.MagicMock()
    with pytest.raises(groups.GroupError):
        groups.set_container_group(_cfg(tmp_path), incus, "myrepo-x", "none")
    incus.config_set.assert_not_called()


def test_clear_container_group_removes_all_three(monkeypatch, mocker, tmp_path: Path):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    incus = mocker.MagicMock()
    groups.clear_container_group(_enabled_cfg(tmp_path, "work"), incus, "myrepo-x")
    incus.config_device_remove.assert_called_once_with(
        "myrepo-x", CLAUDE_CREDS_DEVICE, missing_ok=True
    )
    unset = [c.args[1] for c in incus.config_unset.call_args_list]
    assert unset == [_ENV_KEY, groups.GROUP_LABEL]


class _RecordingAdapter:
    """A minimal pooled adapter recording the per-container wiring `groups` asks for."""

    credential_file = "cred.json"
    refresh_token_key = "refresh"
    live_switch = True

    def __init__(self, name: str, log: list[str]) -> None:
        self.name = name
        self._log = log

    def config_home(self, cfg):
        return cfg.shared_dir / self.name

    def holder_override(self, cfg):
        return None

    def grant_block(self, raw):
        return None

    def compose(self, target_raw, live_raw):
        return target_raw

    def locks(self, holder):
        from contextlib import nullcontext

        return nullcontext()

    def account_at(self, holder, found, *, prefer, authoritative):
        return None

    def record_for(self, slot, raw):
        return None

    def on_park(self, cfg, holder, parked, account):
        return None

    def on_activate(self, holder, record, credential_raw):
        return None

    def on_switch(self, found, unreachable, record, authoritative):
        return [], list(unreachable)

    def sessions(self, found):
        return []

    def blockers(self, cfg, incus, containers):
        return []

    def wiring(self, cfg, group_dir):
        from jailbee.accounts.adapters import base

        return base.Wiring()

    def prepare_config_home(self, cfg, home):
        return None

    def profile_has_group(self, cfg):
        return False

    def set_container_group(self, cfg, incus, container, group_dir):
        self._log.append(f"set:{self.name}:{group_dir}")

    def clear_container_group(self, cfg, incus, container):
        self._log.append(f"clear:{self.name}")


def _two_fake_adapters(monkeypatch, tmp_path):
    """Register two recording adapters and a Config that pools both."""
    from jailbee.accounts.adapters import base
    from tests.conftest import make_cfg

    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    log: list[str] = []
    base.register(_RecordingAdapter("fakea", log))
    base.register(_RecordingAdapter("fakeb", log))
    cfg = make_cfg(
        tmp_path / "myrepo",
        shared_dir=tmp_path / "shared",
        agents={
            "fakea": {"enabled": True, "command": "fakea"},
            "fakeb": {"enabled": True, "command": "fakeb"},
        },
    )
    return cfg, log


def test_set_container_group_invokes_every_adapter_before_the_label(
    monkeypatch, mocker, tmp_path: Path
):
    """One group value, one label, but every pooled agent gets its own wiring."""
    from jailbee.accounts import engine
    from jailbee.accounts.adapters import base

    cfg, log = _two_fake_adapters(monkeypatch, tmp_path)
    incus = mocker.MagicMock()
    incus.config_set.side_effect = lambda c, k, v: (
        log.append("label") if k == groups.GROUP_LABEL else None
    )
    try:
        groups.set_container_group(cfg, incus, "myrepo-x", "personal")
    finally:
        base.ADAPTERS.pop("fakea", None)
        base.ADAPTERS.pop("fakeb", None)

    assert log == [
        f"set:fakea:{engine.group_dir('fakea', 'personal')}",
        f"set:fakeb:{engine.group_dir('fakeb', 'personal')}",
        "label",
    ]


def test_clear_container_group_invokes_every_adapter_then_unsets_the_label(
    monkeypatch, mocker, tmp_path: Path
):
    from jailbee.accounts.adapters import base

    cfg, log = _two_fake_adapters(monkeypatch, tmp_path)
    incus = mocker.MagicMock()
    incus.config_unset.side_effect = lambda c, k: (
        log.append("label") if k == groups.GROUP_LABEL else None
    )
    try:
        groups.clear_container_group(cfg, incus, "myrepo-x")
    finally:
        base.ADAPTERS.pop("fakea", None)
        base.ADAPTERS.pop("fakeb", None)

    assert log == ["clear:fakea", "clear:fakeb", "label"]


def test_set_container_group_with_no_group_passes_none_to_every_adapter(
    monkeypatch, mocker, tmp_path: Path
):
    """`use none` is an explicit override, not a clear: the adapters are told."""
    from jailbee.accounts.adapters import base

    cfg, log = _two_fake_adapters(monkeypatch, tmp_path)
    incus = mocker.MagicMock()
    try:
        groups.set_container_group(cfg, incus, "myrepo-x", None)
    finally:
        base.ADAPTERS.pop("fakea", None)
        base.ADAPTERS.pop("fakeb", None)

    assert log == ["set:fakea:None", "set:fakeb:None"]
    label_calls = [c for c in incus.config_set.call_args_list if c.args[1] == groups.GROUP_LABEL]
    assert label_calls == [mocker.call("myrepo-x", groups.GROUP_LABEL, groups.NO_GROUP)]


def _gcfg(**creds):
    return GlobalConfig.model_validate({"credentials": creds} if creds else {})


def _raw(name: str, group: str | None = None) -> dict:
    config = {} if group is None else {groups.GROUP_LABEL: group}
    return {"name": name, "status": "Running", "profiles": [], "config": config, "state": None}


def test_authoritative_excludes_a_repo_spanning_two_groups(mocker, monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    incus = mocker.MagicMock()
    incus.list_containers.return_value = [
        _raw("mixed-a"),
        _raw("mixed-b", "personal"),
        _raw("clean-a"),
    ]
    gcfg = _gcfg(group="work")
    assert groups.authoritative_prefixes(gcfg, incus, "work", ["mixed", "clean"]) == {"clean"}


def test_claude_running_true(mocker, tmp_path):
    incus = mocker.MagicMock()
    incus.exec.return_value = "running\n"
    cfg = _cfg(tmp_path)
    assert groups.agent_running(cfg, incus, "myrepo-a", command=cfg.claude.command) is True


def test_claude_running_false(mocker, tmp_path):
    incus = mocker.MagicMock()
    incus.exec.return_value = "idle\n"
    cfg = _cfg(tmp_path)
    assert groups.agent_running(cfg, incus, "myrepo-a", command=cfg.claude.command) is False


def test_claude_running_unknown_when_the_probe_fails(mocker, tmp_path):
    from jailbee.incus import IncusError

    incus = mocker.MagicMock()
    incus.exec.side_effect = IncusError("container is not running")
    cfg = _cfg(tmp_path)
    assert groups.agent_running(cfg, incus, "myrepo-a", command=cfg.claude.command) is None


def test_claude_running_probe_uses_pgrep_x_not_f(mocker, tmp_path):
    """`pgrep -f` matches its own command line and would always say yes."""
    incus = mocker.MagicMock()
    incus.exec.return_value = "idle\n"
    cfg = _cfg(tmp_path)
    groups.agent_running(cfg, incus, "myrepo-a", command=cfg.claude.command)
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
    assert groups.groups_by_prefix_from(_gcfg(group="work"), rows, ["myrepo"]) == {
        "myrepo": {"work", "personal"}
    }


def test_groups_by_prefix_from_falls_back_to_the_repos_group_with_no_containers(
    monkeypatch, tmp_path
):
    """With nothing writing the shared config home, the repo's own resolved
    group is the best evidence there is."""
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    assert groups.groups_by_prefix_from(_gcfg(group="work"), [], ["myrepo"]) == {"myrepo": {"work"}}


def test_authoritative_prefixes_from_reuses_prefetched_rows(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    rows = [_raw("mixed-a"), _raw("mixed-b", "personal"), _raw("clean-a")]
    assert groups.authoritative_prefixes_from(
        _gcfg(group="work"), rows, "work", ["mixed", "clean"]
    ) == {"clean"}


def test_container_groups_reports_the_repos_group_for_an_unlabelled_container(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    rows = [_raw("myrepo-a")]
    assert groups.container_groups(_gcfg(group="work"), rows, ["myrepo"]) == [
        ("myrepo-a", "myrepo", "work")
    ]


def test_container_groups_reports_a_temporary_override(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    rows = [_raw("myrepo-a", "personal")]
    assert groups.container_groups(_gcfg(group="work"), rows, ["myrepo"]) == [
        ("myrepo-a", "myrepo", "personal")
    ]


def test_container_groups_names_the_repo_of_an_ungrouped_container(monkeypatch, tmp_path):
    """`None` is not one holder: a container in no group reads *its own repo's*
    config home, so the prefix is part of the answer."""
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    rows = [_raw("myrepo-a", groups.NO_GROUP)]
    assert groups.container_groups(_gcfg(group="work"), rows, ["myrepo"]) == [
        ("myrepo-a", "myrepo", None)
    ]


def test_container_groups_attributes_a_container_to_its_longest_prefix(monkeypatch, tmp_path):
    """`app-web-x` belongs to `app-web`, not to `app`, when both are registered."""
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    gcfg = GlobalConfig.model_validate({"credentials": {"repos": {"app": "one", "app-web": "two"}}})
    rows = [_raw("app-web-x")]
    assert groups.container_groups(gcfg, rows, ["app", "app-web"]) == [
        ("app-web-x", "app-web", "two")
    ]


def test_container_groups_ignores_a_container_of_an_unknown_repo(monkeypatch, tmp_path):
    """Nothing on the host says which group an unregistered repo resolves to,
    and guessing one would put a container under the wrong login."""
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    rows = [_raw("stranger-a"), _raw("myrepo-a")]
    assert groups.container_groups(_gcfg(group="work"), rows, ["myrepo"]) == [
        ("myrepo-a", "myrepo", "work")
    ]


def test_container_groups_ignores_a_garbage_label(monkeypatch, tmp_path):
    """A hand-edited label must never become a path component; the container
    falls back to its repo's group, as `container_override` does."""
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    rows = [_raw("myrepo-a", "../../etc")]
    assert groups.container_groups(_gcfg(group="work"), rows, ["myrepo"]) == [
        ("myrepo-a", "myrepo", "work")
    ]


def test_authoritative_in_answers_the_no_group_question_too(monkeypatch, tmp_path):
    """An ungrouped holder is the same question with "no group" as the answer:
    a repo whose containers span a group can no longer name its own login."""
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    by_prefix = {"clean": {None}, "spanning": {None, "personal"}}

    assert groups.authoritative_in(by_prefix, None) == {"clean"}


def test_authoritative_in_is_the_rule_authoritative_prefixes_applies(monkeypatch, tmp_path):
    """One implementation, so a caller holding a prefetched `groups_by_prefix_from`
    cannot drift from the `Incus`-taking wrapper."""
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    rows = [_raw("mixed-a"), _raw("mixed-b", "personal"), _raw("clean-a")]
    gcfg = _gcfg(group="work")

    by_prefix = groups.groups_by_prefix_from(gcfg, rows, ["mixed", "clean"])

    assert groups.authoritative_in(by_prefix, "work") == (
        groups.authoritative_prefixes_from(gcfg, rows, "work", ["mixed", "clean"])
    )


# --- An override that only repeats the repo's own group -----------------------


def _enabled_cfg(tmp_path: Path, group: str | None = None):
    """A repo with Claude *enabled*, so the binds profile carries the device.

    `make_cfg` leaves `agents.claude` off, and `override_is_redundant` asks
    whether the profile would mount the same credential — which it only does
    when Claude is enabled. Every test here that is not about the disabled
    case has to say so.
    """
    from tests.conftest import make_cfg

    cfg = make_cfg(tmp_path / "myrepo", shared_dir=tmp_path / "shared", claude={"enabled": True})
    if group is None:
        return cfg
    return cfg.model_copy(update={"credential_group": group})


def test_an_override_naming_the_repos_own_group_is_redundant(tmp_path: Path):
    assert groups.override_is_redundant(_enabled_cfg(tmp_path, "work"), "work") is True


def test_an_override_naming_another_group_is_not_redundant(tmp_path: Path):
    cfg = _enabled_cfg(tmp_path, "work")
    assert groups.override_is_redundant(cfg, "personal") is False


def test_an_opt_out_override_is_redundant_when_the_repo_shares_nothing(tmp_path: Path):
    """`use none` on a repo that already shares no group: neither the profile
    nor the label mounts a credential, and the env key the label writes names
    the config home Claude Code would default to anyway."""
    assert groups.override_is_redundant(_enabled_cfg(tmp_path), None) is True


def test_an_opt_out_override_is_not_redundant_while_the_repo_has_a_group(tmp_path: Path):
    cfg = _enabled_cfg(tmp_path, "work")
    assert groups.override_is_redundant(cfg, None) is False


def test_a_matching_override_is_not_redundant_with_claude_disabled(tmp_path: Path):
    """With `claude.enabled: false` the profile carries no credential device,
    so the label is the only thing mounting one — dropping it would change
    what the container reads, which is what "redundant" must never mean."""
    from tests.conftest import make_cfg

    cfg = make_cfg(
        tmp_path / "myrepo", shared_dir=tmp_path / "shared", claude={"enabled": False}
    ).model_copy(update={"credential_group": "work"})

    assert groups.override_is_redundant(cfg, "work") is False


def test_redundant_overrides_lists_this_repos_matching_containers(mocker, tmp_path: Path):
    incus = mocker.MagicMock()
    incus.list_containers.return_value = [
        _raw("myrepo-matching", "work"),
        _raw("myrepo-deviating", "personal"),
        _raw("myrepo-inheriting"),
        _raw("other-matching", "work"),
    ]
    cfg = _enabled_cfg(tmp_path, "work")

    assert groups.redundant_overrides(cfg, incus) == ["myrepo-matching"]


def test_redundant_overrides_ignores_a_garbage_label(mocker, tmp_path: Path):
    """An unusable label is already treated as "inherits" everywhere else, and
    clearing it here would report a container as having been cleaned up when
    the label it carries is not the one this rule is about."""
    incus = mocker.MagicMock()
    incus.list_containers.return_value = [_raw("myrepo-a", "../../etc")]
    cfg = _enabled_cfg(tmp_path, "work")

    assert groups.redundant_overrides(cfg, incus) == []
